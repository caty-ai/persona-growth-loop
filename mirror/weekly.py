"""Weekly light drift report and liveness-marker producer."""

import argparse
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from collectors.claude_code.filter import contains_secret_like
from growthlane.faces import FaceProfile
from growthlane.gates import killswitch_mode
from growthlane.guard import matching_views
from growthlane.holdout import exposed
from growthlane.ledger import load_ledger
from growthlane.locking import (
    acquire_staging_lock,
    release_lock,
    staging_contention_detail,
)
from growthlane.notify import Digest
from growthlane.soul import SoulError, verify_manifest
from growthlane.tripwire import CAPS
from growthlane.ucd_runtime import runtime_status

from .common import (
    REPO,
    MirrorError,
    atomic_json,
    atomic_monotonic_date_json,
    atomic_text,
    injected_bytes,
    list_snapshots,
    load_configs,
    manifest_for,
    manifest_identity,
    mirror_lock,
    parse_run_date,
    read_bytes_nofollow,
    read_json_nofollow,
    sha256_bytes,
    write_snapshot,
)
from .probes import latest_probe_run
from .staging import StagingBuild, regenerate_staging


TIER_S_KEYS = frozenset(
    {
        "candidate_count",
        "staged_count",
        "adopted_count",
        "window_exposure_total",
        "holdout_opportunity_total",
        "negative_signal_count",
        "explicit_mention_count",
        "promotion_count",
        "demotion_count",
        "block_count",
        "cap_usage",
        "soul_check",
        "killswitch",
    }
)
_JST = timezone(timedelta(hours=9))
_COLLECTOR_POST_DAY_GRACE = timedelta(hours=1)
_COLLECTOR_LAST_RUN_KEYS = frozenset(
    {
        "schema_version",
        "face",
        "run_at",
        "date",
        "sources_scanned",
        "records_written",
        "usage_enabled",
        "errors",
        "ucd",
    }
)
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")


def _require_ssh_host_operand(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MirrorError(f"{label} must be a non-empty string")
    if value.startswith("-") or any(character.isspace() for character in value):
        raise MirrorError(f"{label} must be a host alias, not an ssh option")
    return value


_LUCA_PRODUCTION_HOST = _require_ssh_host_operand(
    os.environ.get("PGL_LUCA_PRODUCTION_HOST", "example-vps"),
    "PGL_LUCA_PRODUCTION_HOST",
)
_LUCA_PRODUCTION_COMMAND = "hash"


def _luca_staging_root(config: Mapping[str, object]) -> Path:
    value = config.get("staging_root")
    if not isinstance(value, str) or not value.strip():
        raise MirrorError("growth staging_root must be a non-empty string")
    return Path(value).expanduser().resolve(strict=False)


def _collect_snapshot_state(
    profile: FaceProfile,
    pgl_home: Path,
    snapshot_growth: Mapping[str, object],
    face: str,
    run_date: str,
    previous_snapshot_manifest: Mapping[str, object] | None,
    problems: List[str],
) -> tuple[Mapping[str, object] | None, bool, bool, str, bool]:
    injected_available = False
    try:
        files, injected_warning = injected_bytes(profile, pgl_home, snapshot_growth)
        if injected_warning:
            problems.append(injected_warning)
            files = {}
        injected_available = bool(files) and injected_warning is None
    except (OSError, MirrorError) as exc:
        files = {}
        problems.append(f"injected bytes unavailable: {exc}")
    if injected_available:
        try:
            _, snapshot_manifest, snapshot_written = write_snapshot(
                pgl_home, face, run_date, files, always=False
            )
        except (OSError, MirrorError) as exc:
            snapshot_manifest = manifest_for(files)
            snapshot_written = False
            problems.append(f"snapshot unavailable: {exc}")
        byte_change = (
            previous_snapshot_manifest is not None
            and manifest_identity(previous_snapshot_manifest) != manifest_identity(snapshot_manifest)
        )
        byte_status = (
            "changed since previous snapshot"
            if byte_change
            else ("initial anchor" if previous_snapshot_manifest is None else "unchanged")
        )
        return snapshot_manifest, snapshot_written, byte_change, byte_status, True
    return (
        None,
        False,
        False,
        "UNAVAILABLE (current injected bytes unavailable; no snapshot recorded)",
        False,
    )


@dataclass(frozen=True)
class ParityResult:
    status: str
    production_hash: Optional[str]
    anchor_hash: Optional[str]
    detail: str


@dataclass
class WeeklyReportContext:
    ledger_summary: Dict[str, Any]
    ledger_status: str
    cap_usage: Dict[str, object]
    soul: str


def fetch_luca_production_digest() -> str:
    """Read the production build digest through the forced read-only SSH route."""

    command = ("ssh", _LUCA_PRODUCTION_HOST, _LUCA_PRODUCTION_COMMAND)
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MirrorError(f"production digest SSH unavailable: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise MirrorError(
            f"production digest SSH failed with exit {completed.returncode}: {detail}"
        )
    value = completed.stdout.strip()
    if _CONTENT_HASH.fullmatch(value) is None:
        raise MirrorError("production digest SSH returned a non-hash value")
    return value


def _luca_anchor(pgl_home: Path) -> str:
    path = pgl_home / "state" / "luca-prod-anchor.json"
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise MirrorError(f"production anchor unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MirrorError(f"production anchor has unsafe shape: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise MirrorError(f"production anchor must have mode 0600: {path}")
    value = read_json_nofollow(path)
    if not isinstance(value, dict) or set(value) != {"content_hash"}:
        raise MirrorError("production anchor schema must contain only content_hash")
    content_hash = value.get("content_hash")
    if not isinstance(content_hash, str) or _CONTENT_HASH.fullmatch(content_hash) is None:
        raise MirrorError("production anchor content_hash must be 64 lowercase hex characters")
    return content_hash


def luca_parity(
    pgl_home: Path, production_digest: Callable[[], str] = fetch_luca_production_digest
) -> ParityResult:
    try:
        production_hash = production_digest()
        if not isinstance(production_hash, str) or _CONTENT_HASH.fullmatch(production_hash) is None:
            raise MirrorError("production digest helper returned a non-hash value")
        anchor_hash = _luca_anchor(pgl_home)
    except Exception as exc:
        return ParityResult("UNAVAILABLE", None, None, str(exc))
    if production_hash != anchor_hash:
        return ParityResult(
            "RED",
            production_hash,
            anchor_hash,
            "production digest differs from the immutable anchor",
        )
    return ParityResult("GREEN", production_hash, anchor_hash, "production digest matches anchor")


def _in_window(value: object, start: date, end: date) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return start <= parsed <= end


def _ledger_summary(ledger: Mapping[str, object], start: date, end: date) -> Dict[str, Any]:
    phrases = ledger.get("phrases", [])
    result = {
        "candidate_count": 0,
        "staged_count": 0,
        "adopted_count": 0,
        "window_exposure_total": 0,
        "holdout_opportunity_total": 0,
        "negative_signal_count": 0,
        "explicit_mention_count": 0,
        "promotion_count": 0,
        "demotion_count": 0,
        "block_count": 0,
        "adoptions": [],
        "demotions": [],
        "blocks": [],
        "candidates": [],
    }
    if not isinstance(phrases, list):
        raise MirrorError("ledger phrases must be a list")
    for phrase in phrases:
        if not isinstance(phrase, dict):
            continue
        state = phrase.get("state")
        if state in {"candidate", "staged", "adopted"}:
            result[f"{state}_count"] += 1
        evidence = phrase.get("evidence", {})
        if isinstance(evidence, dict):
            result["negative_signal_count"] += int(evidence.get("negative_signals", 0))
            result["explicit_mention_count"] += int(evidence.get("explicit_mentions", 0))
        text = str(phrase.get("text", ""))
        source = phrase.get("source", {})
        if state == "candidate" and isinstance(source, dict) and _in_window(source.get("first_seen"), start, end):
            result["candidates"].append(text)
        history = phrase.get("history", [])
        if not isinstance(history, list):
            continue
        for event in history:
            if not isinstance(event, dict) or not _in_window(event.get("at"), start, end):
                continue
            target = event.get("to")
            if target == "adopted":
                result["promotion_count"] += 1
                result["adoptions"].append(text)
            elif target == "demoted":
                result["demotion_count"] += 1
                result["demotions"].append(text)
            elif target == "blocked":
                result["block_count"] += 1
                result["blocks"].append(text)
    return result


def _killswitch(pgl_home: Path) -> Dict[str, object]:
    path = pgl_home / "KILLSWITCH"
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return {"exists": False, "mtime": None, "sha256": None, "state": "OFF", "mode": None}
    exists = True
    digest = None
    if not os.path.islink(path) and os.path.isfile(path):
        try:
            digest = sha256_bytes(read_bytes_nofollow(path))
        except OSError:
            digest = None
    return {
        "exists": exists,
        "mtime": metadata.st_mtime_ns,
        "sha256": digest,
        "state": "ON",
        "mode": killswitch_mode(pgl_home) or "freeze",
    }


def _valid_killswitch_tracker(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"exists", "mtime", "sha256"}:
        return False
    if type(value.get("exists")) is not bool:
        return False
    mtime = value.get("mtime")
    if mtime is not None and (type(mtime) is not int or mtime < 0):
        return False
    digest = value.get("sha256")
    if digest is not None and (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return False
    if value["exists"] is False and (mtime is not None or digest is not None):
        return False
    return True


def _prepare_killswitch_tracking(
    pgl_home: Path, current: Mapping[str, object]
) -> Tuple[str, Dict[str, object], Optional[str]]:
    path = pgl_home / "mirror" / "state" / "killswitch-track.json"
    tracked = {key: current[key] for key in ("exists", "mtime", "sha256")}
    if tracked["exists"] is True and tracked["sha256"] is None:
        problem = f"killswitch transition not verified: current identity unavailable: {path}"
        return (
            f"UNAVAILABLE: current killswitch identity unavailable; current state {current['state']} "
            f"mode={current['mode']}; transition not verified",
            tracked,
            problem,
        )
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return f"initial state {current['state']} mode={current['mode']}", tracked, None
    except OSError as exc:
        problem = f"killswitch tracker unavailable: {path}: {exc}"
        return (
            f"UNAVAILABLE: previous tracker unreadable; current state {current['state']} "
            f"mode={current['mode']}; transition not verified",
            tracked,
            problem,
        )
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        problem = f"killswitch tracker unavailable: unsafe tracker shape: {path}"
        return (
            f"UNAVAILABLE: previous tracker invalid; current state {current['state']} "
            f"mode={current['mode']}; transition not verified",
            tracked,
            problem,
        )
    try:
        previous = read_json_nofollow(path)
    except (OSError, MirrorError) as exc:
        problem = f"killswitch tracker unavailable: {exc}"
        return (
            f"UNAVAILABLE: previous tracker unreadable; current state {current['state']} "
            f"mode={current['mode']}; transition not verified",
            tracked,
            problem,
        )
    if not _valid_killswitch_tracker(previous):
        problem = f"killswitch tracker unavailable: invalid tracker state: {path}"
        return (
            f"UNAVAILABLE: previous tracker invalid; current state {current['state']} "
            f"mode={current['mode']}; transition not verified",
            tracked,
            problem,
        )
    if previous["exists"] is True and previous["sha256"] is None:
        problem = f"killswitch transition not verified: previous identity unavailable: {path}"
        return (
            f"UNAVAILABLE: previous killswitch identity unavailable; current state {current['state']} "
            f"mode={current['mode']}; transition not verified",
            tracked,
            problem,
        )
    if (previous["exists"], previous["sha256"]) == (
        tracked["exists"],
        tracked["sha256"],
    ):
        return "unchanged", tracked, None
    old = "ON" if isinstance(previous, dict) and previous.get("exists") else "OFF"
    return f"changed {old}->{current['state']} mode={current['mode']}", tracked, None


def _usage_observation(
    pgl_home: Path,
    face: str,
    start: date,
    end: date,
    *,
    ledger_status: str,
    staged_count: int,
    adopted_count: int,
) -> Tuple[str, List[str], Optional[Dict[str, int]]]:
    usage_counts: Dict[Tuple[str, str], int] = {}
    errors: List[str] = []
    holdout_total = 0
    holdout_violations = 0
    exposure_total = 0
    holdout_opportunity_total = 0
    files_seen = 0
    root = pgl_home / "obslog" / face
    if root.is_symlink():
        return "UNAVAILABLE: symlinked obslog directory", ["symlinked obslog directory"], None
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        path = root / f"usage-{day.isoformat()}.jsonl"
        if not path.exists():
            continue
        files_seen += 1
        try:
            for line in read_bytes_nofollow(path).decode("utf-8").splitlines():
                value = json.loads(line)
                if (
                    not isinstance(value, dict)
                    or not isinstance(value.get("session"), str)
                    or not isinstance(value.get("phrase_id"), str)
                ):
                    raise ValueError("invalid usage record")
                usage_key = (value["session"], value["phrase_id"])
                usage_counts[usage_key] = usage_counts.get(usage_key, 0) + 1
                holdout_total += 1
                if exposed(face, value["phrase_id"], day.isoformat()):
                    exposure_total += 1
                else:
                    holdout_opportunity_total += 1
                    holdout_violations += 1
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
    if files_seen == 0:
        collector_ok, collector_detail, usage_enabled = _collector_liveness(pgl_home, face, end)
        if (
            collector_ok
            and ledger_status == "OK"
            and staged_count == 0
            and adopted_count == 0
            and usage_enabled is False
        ):
            return (
                "[OK] quiet week (bootstrap: 0 staged/adopted — usage collection structurally disabled)",
                [],
                {
                    "window_exposure_total": 0,
                    "holdout_opportunity_total": 0,
                },
            )
        if collector_ok:
            reasons = []
            if ledger_status != "OK":
                reasons.append("ledger unavailable for quiet zero-usage certification")
            if usage_enabled is not False:
                reasons.append("usage files missing while collector marker says usage collection was enabled")
            if staged_count or adopted_count:
                reasons.append("usage files missing while staged/adopted phrases exist")
            errors.append("no usage files in weekly window")
            errors.extend(reasons)
            return (
                "BROKEN: collector last-run marker healthy but " + "; ".join(reasons),
                errors,
                None,
            )
        errors.append(f"no usage files in weekly window; {collector_detail}")
        return (
            f"BROKEN: {collector_detail}",
            errors,
            None,
        )
    if errors:
        return f"UNAVAILABLE: {'; '.join(errors)}", errors, None
    violating = sum(1 for count in usage_counts.values() if count > 1)
    rate = violating / len(usage_counts) if usage_counts else 0.0
    holdout_rate = holdout_violations / holdout_total if holdout_total else 0.0
    return (
        "session_phrase_pairs={pairs}, violations={violations}, violation_rate={rate:.3f}, "
        "holdout_deviations={holdout_violations}/{holdout_total} ({holdout_rate:.3f})".format(
            pairs=len(usage_counts),
            violations=violating,
            rate=rate,
            holdout_violations=holdout_violations,
            holdout_total=holdout_total,
            holdout_rate=holdout_rate,
        ),
        [],
        {
            "window_exposure_total": exposure_total,
            "holdout_opportunity_total": holdout_opportunity_total,
        },
    )


def _collector_liveness(pgl_home: Path, face: str, run_day: date) -> Tuple[bool, str, Optional[bool]]:
    path = pgl_home / "state" / "collector" / f"{face}.last-run.json"
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False, "collector last-run marker missing", None
    except OSError as exc:
        return False, f"collector last-run marker unreadable: {exc}", None
    if stat.S_ISLNK(mode):
        return False, "symlinked collector last-run marker rejected", None
    if not stat.S_ISREG(mode):
        return False, "collector last-run marker is not a regular file", None
    try:
        value = read_json_nofollow(path)
    except (OSError, MirrorError) as exc:
        return False, f"collector last-run marker invalid: {exc}", None
    if not isinstance(value, dict) or set(value) != _COLLECTOR_LAST_RUN_KEYS:
        return False, "collector last-run marker schema mismatch", None
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        return False, "collector last-run marker schema_version mismatch", None
    if value.get("face") != face:
        return False, "collector last-run marker face mismatch", None
    run_at_text = value.get("run_at")
    run_day_text = value.get("date")
    if not isinstance(run_at_text, str) or not isinstance(run_day_text, str):
        return False, "collector last-run marker run_at/date invalid", None
    try:
        run_at = datetime.fromisoformat(run_at_text)
        processed_day = date.fromisoformat(run_day_text)
    except ValueError:
        return False, "collector last-run marker run_at/date invalid", None
    if run_at.tzinfo is None:
        return False, "collector last-run marker run_at missing timezone", None
    for key in ("sources_scanned", "records_written", "errors"):
        if type(value.get(key)) is not int or int(value[key]) < 0:
            return False, f"collector last-run marker {key} invalid", None
    if type(value.get("usage_enabled")) is not bool:
        return False, "collector last-run marker usage_enabled invalid", None
    if not isinstance(value.get("ucd"), str) or not value["ucd"]:
        return False, "collector last-run marker ucd invalid", None
    now = datetime.now(_JST)
    run_at_jst_day = run_at.astimezone(_JST).date()
    oldest_healthy_run_at = datetime.combine(run_day, datetime.min.time(), tzinfo=_JST) - timedelta(hours=48)
    latest_healthy_run_at = (
        datetime.combine(run_day + timedelta(days=1), datetime.min.time(), tzinfo=_JST)
        + _COLLECTOR_POST_DAY_GRACE
    )
    if run_at > now + timedelta(minutes=5):
        return False, "collector last-run marker run_at too far in the future", None
    if run_at < oldest_healthy_run_at:
        return False, "collector last-run marker run_at older than 48h", None
    if value["errors"] != 0:
        return False, "collector last-run marker recorded collector errors", None
    if processed_day not in {run_day, run_day - timedelta(days=1)}:
        return False, "collector last-run marker date is not the weekly run_day or previous JST bucket", None
    if processed_day > run_at_jst_day:
        return False, "collector last-run marker date is newer than the collector run_at JST day", None
    if run_at > latest_healthy_run_at:
        return False, "collector last-run marker run_at later than weekly run_day grace window", None
    return True, "collector last-run marker healthy", value["usage_enabled"]


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
        f"direction={status.direction}; weekly mirror continued"
    )


def _cap_usage(
    profile: FaceProfile, pgl_home: Path, growth: Mapping[str, object]
) -> Tuple[Dict[str, object], Optional[str]]:
    try:
        home = profile.resolve_home(pgl_home, growth)
        if profile.engine:
            sizes = {
                plane: len(read_bytes_nofollow(home / profile.render_files[plane]))
                for plane in ("adopted", "candidates")
            }
        else:
            payload = read_bytes_nofollow(home / profile.render_files["combined"])
            sizes = {"adopted": 0, "candidates": 0}
            for line in payload.splitlines(keepends=True):
                plane = "candidates" if line.startswith("試用中".encode("utf-8")) else "adopted"
                sizes[plane] += len(line)
        return {
            plane: {"bytes": sizes[plane], "cap": CAPS[plane]}
            for plane in ("adopted", "candidates")
        }, None
    except Exception as exc:
        return {
            plane: {"bytes": None, "cap": CAPS[plane]}
            for plane in ("adopted", "candidates")
        }, str(exc)


def _latest_probe_text(pgl_home: Path, face: str, run_day: date) -> str:
    latest = latest_probe_run(pgl_home, face)
    if latest is None:
        return "no probe run recorded yet"
    try:
        age = (run_day - date.fromisoformat(str(latest["recorded_at"]))).days
    except ValueError:
        return "UNAVAILABLE: latest probe run date invalid"
    retention = latest.get("pushback_retention")
    agreement = None if retention is None else 1.0 - float(retention)
    reliability = "reliable" if latest.get("reliable") else "UNRELIABLE"
    return f"pushback_retention={retention}, agreement_rate={agreement}, n={latest.get('scored_total')}, age={age}d, {reliability}"


def _previous_sla(marker: Path, run_day: date) -> Optional[int]:
    try:
        value = read_json_nofollow(marker)
        previous = date.fromisoformat(str(value["generated_at"])[:10])
    except (OSError, KeyError, ValueError, MirrorError):
        return None
    gap = (run_day - previous).days
    return gap if gap > 7 else None


def _write_tier_s(path: Path, aggregate: Mapping[str, object]) -> None:
    if set(aggregate) != TIER_S_KEYS:
        raise MirrorError("Tier S allowlist mismatch")
    serialized = (json.dumps(aggregate, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    candidates = (serialized, *matching_views(serialized))
    if any(contains_secret_like(candidate) for candidate in candidates):
        raise MirrorError("Tier S secret scan rejected aggregate")
    atomic_text(path, serialized)


def _weekly_report_context(
    face: str,
    profile: FaceProfile,
    pgl_home: Path,
    growth: Mapping[str, object],
    start: date,
    run_day: date,
    digest: Digest,
    problems: List[str],
) -> WeeklyReportContext:
    drift_problem = _unicode_corpus_drift_problem()
    if drift_problem is not None:
        problems.append(drift_problem)
    home = profile.resolve_home(pgl_home, growth)
    ledger_path = home / profile.ledger_path
    try:
        if ledger_path.is_symlink():
            raise MirrorError("symlinked ledger rejected")
        ledger = load_ledger(ledger_path, face)
        ledger_summary = _ledger_summary(ledger, start, run_day)
        ledger_status = "OK"
    except Exception as exc:
        ledger_status = f"UNAVAILABLE: {exc}"
        problems.append(f"ledger unavailable: {exc}")
        ledger_summary = _ledger_summary({"phrases": []}, start, run_day)
    cap_usage, cap_error = _cap_usage(profile, pgl_home, growth)
    if cap_error:
        problems.append(f"render sizes unavailable: {cap_error}")
    manifest_path = profile.baseline_manifest(pgl_home)
    if manifest_path.is_symlink():
        soul = "MISMATCH"
        digest.emit(f"[RED] {face}: mirror soul check failed: symlinked baseline manifest rejected")
    elif not manifest_path.exists():
        soul = "NO-BASELINE"
        digest.emit(f"[WARN] {face}: mirror soul check has no baseline")
    else:
        try:
            verify_manifest(profile, pgl_home, growth)
            soul = "OK"
        except SoulError as exc:
            soul = "MISMATCH"
            digest.emit(f"[RED] {face}: mirror soul check failed: {exc}")
    return WeeklyReportContext(ledger_summary, ledger_status, cap_usage, soul)


def run_weekly(
    face: str,
    run_date: str,
    config_dir: Path,
    pgl_home: Path,
    digest: Digest,
    *,
    staging_builder: Optional[Callable[[Mapping[str, object]], StagingBuild]] = None,
    production_digest: Optional[Callable[[], str]] = None,
) -> int:
    growth, mirror, profile = load_configs(face, config_dir)
    run_day = date.fromisoformat(run_date)
    start = run_day - timedelta(days=6)
    marker = pgl_home / "reports" / "weekly" / f"latest-{face}.json"
    sla_gap = _previous_sla(marker, run_day)
    problems: List[str] = []

    context: Optional[WeeklyReportContext] = None
    if face == "alpha":
        context = _weekly_report_context(
            face, profile, pgl_home, growth, start, run_day, digest, problems
        )

    previous_snapshot_manifest = None
    previous_snapshots = list_snapshots(pgl_home, face)
    if previous_snapshots:
        previous_snapshot_manifest = previous_snapshots[-1][2]
    snapshot_growth: Mapping[str, object] = growth
    if face == "luca":
        staging_root = _luca_staging_root(growth)
        try:
            staging_lock = acquire_staging_lock(staging_root, "luca")
        except (OSError, ValueError) as exc:
            detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            raise MirrorError(f"Luca staging lock acquisition failed: {detail}") from exc
        if staging_lock is None:
            raise MirrorError(
                staging_contention_detail(staging_root, "luca", "weekly staging")
            )
        failure: BaseException | None = None
        try:
            if staging_builder is None:
                staging = regenerate_staging(growth, held_lock=staging_lock)
            else:
                staging = staging_builder(growth)
            if staging.root.resolve() != staging_root:
                raise MirrorError("Luca staging builder returned a root different from staging_root")
            snapshot_growth = {**growth, "overlay_home_root": str(staging.root)}
            (
                snapshot_manifest,
                snapshot_written,
                byte_change,
                byte_status,
                injected_available,
            ) = _collect_snapshot_state(
                profile,
                pgl_home,
                snapshot_growth,
                face,
                run_date,
                previous_snapshot_manifest,
                problems,
            )
        except BaseException as exc:
            failure = exc
            raise
        finally:
            released = release_lock(staging_lock)
            if not released:
                message = "Luca staging lock directory could not be removed"
                if failure is None:
                    raise MirrorError(message)
                raise MirrorError(message) from failure
    else:
        (
            snapshot_manifest,
            snapshot_written,
            byte_change,
            byte_status,
            injected_available,
        ) = _collect_snapshot_state(
            profile,
            pgl_home,
            snapshot_growth,
            face,
            run_date,
            previous_snapshot_manifest,
            problems,
        )

    parity: Optional[ParityResult] = None
    if face == "luca":
        parity = luca_parity(
            pgl_home, production_digest or fetch_luca_production_digest
        )
        if parity.status == "RED":
            digest.emit(
                f"[RED] luca: production parity RED "
                f"production={parity.production_hash} anchor={parity.anchor_hash}"
            )
        elif parity.status == "UNAVAILABLE":
            digest.emit(f"[RED] luca: production parity UNAVAILABLE: {parity.detail}")

    if face == "luca":
        context = _weekly_report_context(
            face, profile, pgl_home, growth, start, run_day, digest, problems
        )
    if context is None:
        raise MirrorError(f"weekly report context unavailable for face: {face}")
    ledger_summary = context.ledger_summary
    ledger_status = context.ledger_status
    cap_usage = context.cap_usage
    soul = context.soul
    killswitch = _killswitch(pgl_home)
    killswitch_change, killswitch_tracked, killswitch_problem = _prepare_killswitch_tracking(
        pgl_home, killswitch
    )
    if killswitch_problem is not None:
        problems.append(killswitch_problem)
    usage, usage_errors, usage_totals = _usage_observation(
        pgl_home,
        face,
        start,
        run_day,
        ledger_status=ledger_status,
        staged_count=int(ledger_summary.get("staged_count", 0) or 0),
        adopted_count=int(ledger_summary.get("adopted_count", 0) or 0),
    )
    problems.extend(f"usage unavailable: {item}" for item in usage_errors)
    if usage_errors:
        digest.emit(f"[RED] {face}: usage discipline unavailable")
    if usage_totals is None:
        ledger_summary["window_exposure_total"] = None
        ledger_summary["holdout_opportunity_total"] = None
    else:
        ledger_summary.update(usage_totals)
    changed = any(
        ledger_summary[key]
        for key in ("adoptions", "demotions", "blocks", "candidates")
    ) or killswitch_change.startswith("changed") or byte_change
    if killswitch_problem is not None:
        change_line = (
            "transition not verified; injected-byte comparison UNAVAILABLE"
            if not injected_available
            else "transition not verified"
        )
    elif not injected_available:
        change_line = (
            "changes detected; injected-byte comparison UNAVAILABLE"
            if changed
            else "UNAVAILABLE"
        )
    else:
        change_line = "changes detected" if changed else "no changes"
    if parity is not None and parity.status != "GREEN":
        if parity.status == "UNAVAILABLE":
            change_line = "UNAVAILABLE; production parity UNAVAILABLE"
        elif change_line == "no changes":
            change_line = "changes detected; production parity RED"
        else:
            change_line = f"{change_line}; production parity RED"
    lines = [
        f"# Weekly drift mirror — {face} — {run_date}",
        "",
        f"Window: {start.isoformat()} through {run_date} (JST)",
        f"Status: {change_line}",
        "",
        "## Ledger transitions",
        f"Ledger: {ledger_status}",
        f"Adopted: {ledger_summary['adoptions'] or 'none'}",
        f"Demoted: {ledger_summary['demotions'] or 'none'}",
        f"Blocked: {ledger_summary['blocks'] or 'none'}",
        f"Candidates first seen: {ledger_summary['candidates'] or 'none'}",
        "",
        "## Injected bytes and caps",
        "Snapshot manifest: {value}".format(
            value=json.dumps(snapshot_manifest, ensure_ascii=False, sort_keys=True)
            if snapshot_manifest is not None
            else "UNAVAILABLE"
        ),
        f"Snapshot: {'recorded' if snapshot_written else ('deduplicated' if injected_available else 'UNAVAILABLE')}",
        f"Injected-byte drift: {byte_status}",
        f"Cap usage: {json.dumps(cap_usage, ensure_ascii=False, sort_keys=True)}",
    ]
    if parity is not None:
        lines.extend(
            [
                "",
                "## Production parity",
                f"Parity: {parity.status}",
                f"Production content_hash: {parity.production_hash or 'UNAVAILABLE'}",
                f"Anchor content_hash: {parity.anchor_hash or 'UNAVAILABLE'}",
                f"Detail: {parity.detail}",
            ]
        )
    lines.extend(
        [
            "",
            "## Soul fixed-point",
            f"Soul check: {soul}",
            "",
            "## Contrariness score",
            _latest_probe_text(pgl_home, face, run_day),
            "",
            "## Killswitch",
            f"State: {killswitch['state']} mode={killswitch['mode']}",
            f"Change tracking: {killswitch_change}",
            "",
            "## Usage discipline",
            usage,
            "Window exposures: {value}".format(
                value=ledger_summary["window_exposure_total"]
                if usage_totals is not None
                else "UNAVAILABLE"
            ),
            "Holdout opportunities: {value}".format(
                value=ledger_summary["holdout_opportunity_total"]
                if usage_totals is not None
                else "UNAVAILABLE"
            ),
        ]
    )
    if change_line == "no changes":
        lines.extend(["", "no changes"])
    if problems:
        lines.extend(["", "## Degraded inputs"] + [_degraded_report_line(item) for item in problems])
    report = pgl_home / "reports" / "weekly" / face / f"{run_date}.md"
    atomic_text(report, "\n".join(lines) + "\n")
    if usage_totals is None:
        digest.emit(f"[RED] {face}: Tier S write aborted: weekly usage counts UNAVAILABLE")
    else:
        tier_s = {
            key: ledger_summary[key]
            for key in (
                "candidate_count", "staged_count", "adopted_count", "window_exposure_total",
                "holdout_opportunity_total", "negative_signal_count", "explicit_mention_count",
                "promotion_count", "demotion_count", "block_count",
            )
        }
        tier_s.update(
            {
                "cap_usage": cap_usage,
                "soul_check": soul,
                "killswitch": {"state": killswitch["state"], "mode": killswitch["mode"]},
            }
        )
        try:
            _write_tier_s(pgl_home / "reports" / "tier-s" / face / f"{run_date}.json", tier_s)
            if mirror["vault_dir"]:
                digest.emit(f"[WARN] {face}: tier-s vault_dir ignored in v1; staged locally")
            else:
                digest.emit(f"{face}: tier-s staged locally (vault emission awaits CP-4)")
        except Exception as exc:
            digest.emit(f"[RED] {face}: Tier S write aborted: {exc}")
    marker_payload = {
        "generated_at": run_date,
        "report": str(report.relative_to(pgl_home)),
        "soul_check": soul,
        "change": change_line,
    }
    if parity is not None:
        marker_payload["parity"] = parity.status
    atomic_monotonic_date_json(
        marker,
        marker_payload,
        "generated_at",
        lambda message: digest.emit(f"[RED] {face}: {message}"),
    )
    atomic_json(
        pgl_home / "mirror" / "state" / "killswitch-track.json",
        killswitch_tracked,
    )
    if sla_gap is not None:
        digest.emit(f"[WARN] {face}: weekly SLA gap {sla_gap}d")
    if not mirror["responder_argv"] or not mirror["scorer_argv"]:
        digest.emit("[WARN] probes skipped: adapters not configured")
    for problem in problems:
        digest.emit(f"[RED] {face}: {_degraded_digest_text(problem)}")
    digest.emit(f"{face}: weekly mirror: {change_line}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the weekly light drift mirror")
    parser.add_argument("face", choices=("alpha", "luca"))
    parser.add_argument("--date", help="JST run date (YYYY-MM-DD)")
    parser.add_argument("--config-dir", default=str(REPO / "config"))
    args = parser.parse_args(argv)
    try:
        run_date = parse_run_date(args.date)
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
            return run_weekly(args.face, run_date, Path(args.config_dir), pgl_home, digest)
        except Exception as exc:
            digest.emit(f"[RED] {args.face}: weekly mirror failed: {exc}")
            return 1
        finally:
            digest.ensure_line()


if __name__ == "__main__":
    raise SystemExit(main())
