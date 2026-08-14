"""Monthly deep byte-comparison and eval-probe drift report."""

import argparse
import difflib
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from growthlane.ledger import load_ledger
from growthlane.notify import Digest
from growthlane.ucd_runtime import runtime_status

from .common import (
    REPO,
    MirrorError,
    atomic_text,
    injected_bytes,
    list_snapshots,
    load_configs,
    mirror_lock,
    nearest_snapshot,
    parse_run_date,
    read_bytes_nofollow,
    read_json_nofollow,
    safe_marker_create,
    sha256_bytes,
    snapshot_files,
    write_snapshot,
)
from .probes import (
    MIN_RELIABLE_SCORED,
    ProbeError,
    load_corpus,
    material_sycophancy_rise,
    probe_score_counts,
    run_probes,
    write_probe_run,
)


def _diff_lines(left: bytes, right: bytes) -> int:
    before = left.decode("utf-8", "replace").splitlines()
    after = right.decode("utf-8", "replace").splitlines()
    return sum(1 for line in difflib.unified_diff(before, after, lineterm="") if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))


def _matrix(
    current: Mapping[str, bytes],
    month: Optional[Mapping[str, bytes]],
    quarter: Optional[Mapping[str, bytes]],
    soul_expected: Mapping[str, str],
    soul_current: Mapping[str, bytes],
) -> List[str]:
    injected_names = set(current) | set(month or {}) | set(quarter or {})
    names = sorted(injected_names) + [f"soul:{name}" for name in sorted(soul_expected)]
    lines = [
        "| file | soul baseline sha256 | current sha256 / baseline equal | ~1m equal / size delta / diff lines | ~3m equal / size delta / diff lines |",
        "|---|---|---|---|---|",
    ]
    for display_name in names:
        is_soul = display_name.startswith("soul:")
        name = display_name[5:] if is_soul else display_name
        now = soul_current.get(name) if is_soul else current.get(name)
        expected = soul_expected.get(name) if is_soul else None
        cells = []
        for anchor in (month, quarter):
            old = None if is_soul or anchor is None else anchor.get(name)
            if old is None or now is None:
                cells.append("MISSING")
            else:
                cells.append(
                    f"{sha256_bytes(old) == sha256_bytes(now)} / {len(now) - len(old):+d}B / {_diff_lines(old, now)}"
                )
        current_hash = sha256_bytes(now) if now is not None else "MISSING"
        baseline_cell = expected or "MISSING"
        equality = expected == current_hash if expected is not None else "N/A"
        lines.append(
            f"| {display_name} | {baseline_cell} | {current_hash} / {equality} | {cells[0]} | {cells[1]} |"
        )
    if not names:
        lines.append("| (no artifacts) | MISSING | MISSING | MISSING | MISSING |")
    return lines


def _soul_data(
    pgl_home: Path, profile_name: str
) -> Tuple[Dict[str, str], Dict[str, bytes], List[str]]:
    path = pgl_home / "soul-baseline" / f"{profile_name}.manifest"
    try:
        if path.is_symlink():
            raise MirrorError("symlinked soul baseline manifest rejected")
        value = read_json_nofollow(path)
        if not isinstance(value, dict) or set(value) != {"overlay_home", "files"}:
            raise MirrorError("invalid soul baseline manifest shape")
        files = value.get("files")
        if not isinstance(files, list):
            raise MirrorError("invalid soul baseline entries")
    except (OSError, MirrorError) as exc:
        return {}, {}, [f"soul baseline unavailable: {exc}"]
    expected = {}
    current = {}
    problems = []
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("sha256"), str)
        ):
            return {}, {}, ["soul baseline unavailable: invalid soul baseline entry"]
        name = item["path"]
        expected[name] = item["sha256"]
        try:
            current[name] = read_bytes_nofollow(Path(name))
        except OSError as exc:
            problems.append(f"soul baseline file unavailable: {name}: {exc}")
    return expected, current, problems


def _month_story(
    ledger: Mapping[str, object], report_month: str
) -> Tuple[List[str], Dict[str, int]]:
    adopted = []
    demoted = []
    provenance = {}
    prefix = report_month
    phrases = ledger.get("phrases", [])
    if not isinstance(phrases, list):
        raise MirrorError("ledger phrases must be a list")
    for phrase in phrases:
        if not isinstance(phrase, dict):
            continue
        source = phrase.get("source", {})
        label = "unknown"
        if isinstance(source, dict):
            for key in ("provenance", "project", "source_type", "host"):
                if isinstance(source.get(key), str) and source[key]:
                    label = str(source[key])
                    break
        provenance[label] = provenance.get(label, 0) + 1
        for event in phrase.get("history", []):
            if not isinstance(event, dict) or not str(event.get("at", "")).startswith(prefix):
                continue
            if event.get("to") == "adopted":
                adopted.append(str(phrase.get("text", "")))
            elif event.get("to") == "demoted":
                demoted.append(str(phrase.get("text", "")))
    return [
        f"Adopted this month: {adopted or 'none'}",
        f"Demoted this month: {demoted or 'none'}",
    ], provenance


def _resolve_report_month(run_day: date, requested: Optional[str]) -> str:
    if requested is None:
        return run_day.strftime("%Y-%m")
    if len(requested) != 7:
        raise MirrorError(f"invalid report month: {requested}")
    try:
        parsed = date.fromisoformat(f"{requested}-01")
    except ValueError as exc:
        raise MirrorError(f"invalid report month: {requested}") from exc
    if parsed.strftime("%Y-%m") != requested:
        raise MirrorError(f"invalid report month: {requested}")
    if parsed > run_day.replace(day=1):
        raise MirrorError(
            f"future report month rejected: {requested} after run date {run_day.isoformat()}"
        )
    return requested


def _probe_table(baseline_result: Mapping[str, object], current_result: Mapping[str, object]) -> List[str]:
    before = {
        item.get("id"): item.get("verdict")
        for item in baseline_result.get("per_probe", [])
        if isinstance(item, dict)
    }
    lines = ["| probe | baseline rerun | current | error |", "|---|---|---|---|"]
    for item in current_result.get("per_probe", []):
        if isinstance(item, dict):
            error = str(item.get("error", "")).replace("|", "/")
            lines.append(f"| {item.get('id')} | {before.get(item.get('id'), 'MISSING')} | {item.get('verdict')} | {error} |")
    return lines


def _degraded_report_line(problem: str) -> str:
    if problem.startswith(("UNAVAILABLE: ", "DRIFT: ")):
        return problem
    return f"UNAVAILABLE: {problem}"


def _degraded_digest_text(problem: str) -> str:
    for prefix in ("UNAVAILABLE: ", "DRIFT: "):
        if problem.startswith(prefix):
            return problem[len(prefix):]
    return problem


def _unicode_corpus_drift_problem() -> Optional[str]:
    status = runtime_status()
    if status.runtime_version == status.corpus_version:
        return None
    return (
        "DRIFT: UCD drift "
        f"runtime={status.runtime_version} corpus={status.corpus_version} "
        f"direction={status.direction}; monthly mirror continued"
    )


def run_monthly(
    face: str,
    run_date: str,
    config_dir: Path,
    pgl_home: Path,
    digest: Digest,
    report_month: Optional[str] = None,
) -> int:
    growth, mirror, profile = load_configs(face, config_dir)
    run_day = date.fromisoformat(run_date)
    month_key = _resolve_report_month(run_day, report_month)
    problems = []
    drift_problem = _unicode_corpus_drift_problem()
    if drift_problem is not None:
        problems.append(drift_problem)
    injected_available = False
    try:
        current, injected_warning = injected_bytes(profile, pgl_home, growth)
        if injected_warning:
            problems.append(injected_warning)
            current = {}
        injected_available = bool(current) and injected_warning is None
    except (OSError, MirrorError) as exc:
        current = {}
        problems.append(f"injected bytes unavailable: {exc}")
    if injected_available:
        try:
            write_snapshot(pgl_home, face, run_date, current, always=True)
        except (OSError, MirrorError) as exc:
            problems.append(f"snapshot unavailable: {exc}")
    snapshots = list_snapshots(pgl_home, face)
    one_month = nearest_snapshot(snapshots, run_day - timedelta(days=28))
    three_month = nearest_snapshot(snapshots, run_day - timedelta(days=84))
    try:
        month_files = snapshot_files(one_month[1]) if one_month is not None else None
    except (OSError, MirrorError) as exc:
        month_files = None
        problems.append(f"one-month anchor unavailable: {exc}")
    try:
        quarter_files = snapshot_files(three_month[1]) if three_month is not None else None
    except (OSError, MirrorError) as exc:
        quarter_files = None
        problems.append(f"three-month anchor unavailable: {exc}")
    try:
        ledger_path = profile.resolve_home(pgl_home, growth) / profile.ledger_path
        if ledger_path.is_symlink():
            raise MirrorError("symlinked ledger rejected")
        ledger = load_ledger(ledger_path, face)
        story, provenance = _month_story(ledger, month_key)
        provenance_text = json.dumps(provenance, ensure_ascii=False, sort_keys=True)
    except Exception as exc:
        ledger_problem = f"ledger unavailable: {exc}"
        problems.append(ledger_problem)
        story = [f"UNAVAILABLE: {ledger_problem}"]
        provenance_text = f"UNAVAILABLE: {ledger_problem}"
    soul_expected, soul_current, soul_problems = _soul_data(pgl_home, face)
    problems.extend(soul_problems)
    probe_lines = ["probes skipped: adapters not configured"]
    probe_summary = "not run"
    fatal_probe = False
    if mirror["responder_argv"] and mirror["scorer_argv"]:
        baseline_record_path = pgl_home / "mirror" / "probe-baseline" / f"{face}.json"
        baseline_block = pgl_home / "mirror" / "probe-baseline" / face / "block"
        try:
            baseline_record = read_json_nofollow(baseline_record_path)
            if not isinstance(baseline_record, dict):
                raise MirrorError("probe baseline record is not an object")
            _, verified_corpus_hash = load_corpus()
            if baseline_record.get("corpus") != "eval-v1":
                raise MirrorError("probe baseline corpus mismatch")
            if baseline_record.get("corpus_sha256") != verified_corpus_hash:
                raise MirrorError("probe baseline corpus sha256 mismatch")
            baseline_files = snapshot_files(baseline_block)
            if not injected_available:
                raise MirrorError("current injected bytes unavailable")
            baseline_result = run_probes(
                baseline_files,
                profile.persona_name,
                mirror["responder_argv"],
                mirror["scorer_argv"],
            )
            current_result = run_probes(
                current,
                profile.persona_name,
                mirror["responder_argv"],
                mirror["scorer_argv"],
            )
            write_probe_run(pgl_home, face, run_date, current_result)
            try:
                baseline_pushback, baseline_scored = probe_score_counts(baseline_record)
            except ProbeError as exc:
                raise ProbeError(
                    f"baseline probe count-space failed closed: {exc}"
                ) from exc
            current_per_probe = current_result.get("per_probe")
            if (
                current_result.get("scored_total") == 0
                and isinstance(current_per_probe, list)
                and current_per_probe
                and all(
                    isinstance(item, dict) and item.get("verdict") == "unclear"
                    for item in current_per_probe
                )
            ):
                raise ProbeError(
                    f"current probe run failed closed: all {len(current_per_probe)} probes unclear"
                )
            try:
                current_pushback, current_scored = probe_score_counts(current_result)
            except ProbeError as exc:
                raise ProbeError(
                    f"current probe count-space failed closed: {exc}"
                ) from exc
            baseline_agreement = 1.0 - baseline_pushback / baseline_scored
            current_agreement = 1.0 - current_pushback / current_scored
            rise = current_agreement - baseline_agreement
            reliable = (
                bool(current_result.get("reliable"))
                and baseline_scored >= MIN_RELIABLE_SCORED
            )
            probe_summary = (
                f"baseline agreement={baseline_agreement:.3f}; current agreement={current_agreement:.3f}; "
                f"delta={rise:+.3f}; n={current_result.get('scored_total')}; "
                f"{'reliable' if reliable else 'UNRELIABLE'}"
            )
            probe_lines = _probe_table(baseline_result, current_result)
            unclear = sum(
                1
                for item in current_result.get("per_probe", [])
                if isinstance(item, dict) and item.get("verdict") == "unclear"
            )
            if unclear:
                digest.emit(f"[WARN] {face}: probe run excluded {unclear} unclear or adapter-error probes")
            if not reliable:
                digest.emit(f"[WARN] {face}: probe score UNRELIABLE (n={current_result.get('scored_total')}); no HOLD proposal")
            elif material_sycophancy_rise(baseline_record, current_result):
                safe_marker_create(
                    pgl_home / "KILLSWITCH.proposed",
                    f"mirror proposed HOLD for {face}: agreement rise {rise:+.3f}\n".encode("utf-8"),
                )
                digest.emit(f"[RED] {face}: sycophancy agreement rate rose {rise:+.3f}; HOLD proposed")
            elif rise > 0:
                digest.emit(f"[WARN] {face}: sycophancy agreement rate rose {rise:+.3f}")
        except ProbeError as exc:
            fatal_probe = True
            detail = str(exc)
            if not detail.startswith(
                (
                    "baseline probe count-space failed closed:",
                    "current probe count-space failed closed:",
                    "current probe run failed closed:",
                )
            ):
                detail = f"probe corpus failed closed: {detail}"
            probe_lines = [f"UNAVAILABLE: {detail}"]
            probe_summary = "failed closed"
            digest.emit(f"[RED] {face}: {detail}")
        except Exception as exc:
            problems.append(f"probe run unavailable: {exc}")
            probe_lines = [f"UNAVAILABLE: {exc}"]
            probe_summary = "unavailable"
    else:
        digest.emit("[WARN] probes skipped: adapters not configured")
    lines = [
        f"# Monthly deep drift mirror — {face} — {month_key}",
        "",
        "Attribution: report_month={month}; run_date={run_date}; source={source}".format(
            month=month_key,
            run_date=run_date,
            source="explicit --month" if report_month is not None else "run-date anchored",
        ),
        "",
        "## Injected-byte equality matrix",
        f"~1 month anchor: {one_month[0].isoformat() if one_month else 'MISSING'}",
        f"~3 month anchor: {three_month[0].isoformat() if three_month else 'MISSING'}",
        *_matrix(current, month_files, quarter_files, soul_expected, soul_current),
        "",
        "## Eval probes",
        probe_summary,
        *probe_lines,
        "",
        "## 今月の私",
        *story,
        "Injected size trajectory: current={current_value}, ".format(
            current_value=f"{sum(len(value) for value in current.values())}B"
            if injected_available
            else "UNAVAILABLE"
        )
        + f"~1m={sum(len(value) for value in month_files.values()) if month_files is not None else 'MISSING'}, "
        f"~3m={sum(len(value) for value in quarter_files.values()) if quarter_files is not None else 'MISSING'}",
        f"Probe score movement: {probe_summary}",
        "",
        "## Candidate provenance distribution",
        provenance_text,
    ]
    if problems:
        lines.extend(["", "## Degraded inputs"] + [_degraded_report_line(item) for item in problems])
    report = pgl_home / "reports" / "monthly" / face / f"{month_key}.md"
    atomic_text(report, "\n".join(lines) + "\n")
    for problem in problems:
        digest.emit(f"[RED] {face}: {_degraded_digest_text(problem)}")
    status = "DEGRADED" if problems or fatal_probe else "OK"
    digest.emit(
        f"{face}: monthly mirror published {month_key} "
        f"(run_date={run_date} status={status})"
    )
    return 1 if fatal_probe else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the monthly deep drift mirror")
    parser.add_argument("face", choices=("alpha", "luca"))
    parser.add_argument("--date", help="JST run date (YYYY-MM-DD)")
    parser.add_argument("--month", help="report attribution month (YYYY-MM; defaults to run date month)")
    parser.add_argument("--config-dir", default=str(REPO / "config"))
    args = parser.parse_args(argv)
    try:
        run_date = parse_run_date(args.date)
        _resolve_report_month(date.fromisoformat(run_date), args.month)
    except MirrorError as exc:
        parser.error(str(exc))
    pgl_home = Path(os.environ.get("PGL_HOME", "~/.persona-growth-loop")).expanduser().resolve()
    digest = Digest(pgl_home, run_date)
    with mirror_lock(
        pgl_home, lambda message: digest.emit(f"[RED] {args.face}: {message}")
    ) as acquired:
        if not acquired:
            return 1
        try:
            return run_monthly(
                args.face,
                run_date,
                Path(args.config_dir),
                pgl_home,
                digest,
                args.month,
            )
        except Exception as exc:
            digest.emit(f"[RED] {args.face}: monthly mirror failed: {exc}")
            return 1
        finally:
            digest.ensure_line()


if __name__ == "__main__":
    raise SystemExit(main())
