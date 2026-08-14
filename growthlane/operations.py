"""Manual safety, eject, baseline, and rollback operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from aggregator.aggregate import apply_transition
from growthlane import deploy as luca_deploy
from applier.apply import (
    ApplyError,
    BlocklistState,
    _assert_guarded_payloads,
    _build,
    _enforce_render_monotonicity,
    _git,
    _require_overlay_home,
    _require_clean_allowlist,
    _restore_files,
    _snapshot_files,
    append_blocklist_state_preserving_invalid_bytes,
    commit_state,
    read_blocklist_state,
)
from growthlane.config import load_json_object
from growthlane.faces import FaceProfile, get_profile
from growthlane.gates import (
    GateError,
    GateExplicitNo,
    GateUnavailable,
    check_all,
    check_cp,
    check_killswitch,
    deletion_context,
    killswitch_mode,
)
from growthlane.guard import canonicalize_for_matching, matching_contains
from growthlane.holdout import proposal_id
from growthlane.ledger import load_ledger
from growthlane.locking import acquire_lock, contention_message, release_lock
from growthlane.notify import Digest
from growthlane.soul import (
    read_overlay_home_pin,
    verify_manifest,
    write_manifest,
    write_overlay_home_pin,
)
from growthlane.ucd_runtime import emit_deletion_drift_note
from harvester.harvest import (
    append_blocked_text,
    normalize_blocklist,
    normalized_hash,
)


_JST = timezone(timedelta(hours=9))
_REPO = Path(__file__).resolve().parents[1]


def _context(face: str, config_path: str | None, run_date: str | None):
    pgl_home = Path(os.environ.get("PGL_HOME", "~/.persona-growth-loop")).expanduser().resolve()
    date_value = run_date or datetime.now(_JST).date().isoformat()
    config = load_json_object(Path(config_path) if config_path else _REPO / "config" / f"growth-{face}.json")
    return pgl_home, date_value, config, get_profile(face), Digest(pgl_home, date_value)


def _locked(pgl_home: Path, face: str, digest: Digest, action: Callable[[], int]) -> int:
    try:
        lock = acquire_lock(pgl_home, face)
    except OSError as exc:
        digest.emit(
            f"[RED] {face}: manual operation: skipped: lock acquisition failed "
            f"error={exc.strerror or exc}"
        )
        return 1
    if lock is None:
        digest.emit(contention_message(pgl_home, face, "manual operation"))
        return 1
    try:
        return action()
    finally:
        if not release_lock(lock):
            digest.emit("[WARN] manual operation: lock directory could not be removed")
        digest.ensure_line()


def _load_required_ledger(path: Path, face: str, operation: str) -> dict[str, Any]:
    try:
        return load_ledger(path, face)
    except Exception as exc:
        raise ApplyError(
            f"{operation} requires a valid ledger; pgl-eject is the ledger-independent "
            f"path to zero the injection: {exc}"
        ) from exc


class _LucaTransportBoundary:
    """Lazy manual transport whose no-op verdict is production-aware."""

    def __init__(
        self,
        *,
        profile: FaceProfile,
        pgl_home: Path,
        config: Mapping[str, object],
        config_path: str | None,
        run_date: str,
        operation: Literal["deletion", "rollback"],
    ) -> None:
        self.profile = profile
        self.pgl_home = pgl_home
        self.config = config
        self.config_path = config_path
        self.run_date = run_date
        self.operation = operation
        self.transaction: luca_deploy.AttendedSafetyTransport | None = None

    @property
    def enabled(self) -> bool:
        return self.profile.name == "luca"

    def _ensure_transaction(self) -> luca_deploy.AttendedSafetyTransport:
        if not self.enabled:
            raise ApplyError("alpha manual operations must not enter Luca transport")
        if self.transaction is not None:
            return self.transaction
        collector_config = (
            Path(self.config_path).expanduser().resolve().with_name(
                "obs-collector-luca.json"
            )
            if self.config_path is not None
            else luca_deploy.DEFAULT_LUCA_COLLECTOR_CONFIG
        )
        obs_root, ledger_path = luca_deploy.require_collector_path_agreement(
            self.pgl_home,
            collector_config,
        )
        staging_root = self.profile.resolve_staging_root(self.config)
        if staging_root is None:
            raise ApplyError("luca manual transport requires staging_root")

        from mirror.staging import regenerate_staging

        self.transaction = luca_deploy.AttendedSafetyTransport(
            operation=self.operation,
            attended=True,
            pgl_home=self.pgl_home,
            obs_root=obs_root,
            staging_root=staging_root,
            known_production_install=(
                self.profile.resolve_home(self.pgl_home, self.config)
                / luca_deploy.KNOWN_PRODUCTION_INSTALL_RELATIVE_PATH
            ),
            ledger_path=ledger_path,
            deploy_key=luca_deploy.DEPLOY_KEY,
            staging_rebuilder=lambda: regenerate_staging(self.config),
            full_gate_check=lambda: check_all(
                self.pgl_home, self.profile.name, self.run_date
            ),
        )
        return self.transaction

    @property
    def rollback_direction(self) -> Literal["reduction", "increase"] | None:
        return (
            self.transaction.rollback_direction
            if self.transaction is not None
            else None
        )

    def configure_rollback_direction(
        self,
        *,
        repo_baseline_hash: str,
        target_is_monotone_non_increasing: bool,
    ) -> Literal["reduction", "increase"]:
        return self._ensure_transaction().configure_rollback_direction(
            repo_baseline_hash=repo_baseline_hash,
            target_is_monotone_non_increasing=target_is_monotone_non_increasing,
        )

    def configure_deletion_baseline(self, *, repo_baseline_hash: str) -> bool:
        return self._ensure_transaction().configure_deletion_baseline(
            repo_baseline_hash=repo_baseline_hash
        )

    def before_commit(self, content_hash: str) -> None:
        transaction = self._ensure_transaction()
        if transaction.reflected_content_hash is not None:
            raise ApplyError("Luca manual transport boundary was entered twice")
        home = self.profile.resolve_home(self.pgl_home, self.config)
        target_is_empty_render = all(
            (home / relative).read_bytes() == b""
            for relative in self.profile.render_files.values()
        )
        transaction.before_commit(
            content_hash,
            target_is_empty_render=target_is_empty_render,
        )

    def complete(self, content_hash: str | None) -> None:
        if not self.enabled:
            return
        if self.transaction is None or content_hash is None:
            raise ApplyError("changed Luca operation did not perform attended transport")
        self.transaction.complete(content_hash)

    def recover_after_local_revert(self) -> None:
        if self.transaction is not None:
            self.transaction.recover_after_local_revert()

    def reflection_elapsed(self, operation_started_at: float) -> float:
        if self.transaction is None:
            raise ApplyError("Luca operation has no production reflection measurement")
        return self.transaction.reflection_elapsed(operation_started_at)


def _revert_completed_manual_commit(
    profile: FaceProfile,
    home: Path,
    source_head: str,
    tag: str | None,
) -> tuple[str, ...]:
    failures: list[str] = []

    def run(*args: str) -> bytes:
        try:
            completed = _git(home, *args, check=False)
        except BaseException as exc:
            failures.append(f"git {' '.join(args)} raised {exc}")
            return b""
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).decode(
                "utf-8", "replace"
            ).strip()
            failures.append(
                f"git {' '.join(args)} failed with exit {completed.returncode}: {detail}"
            )
        return completed.stdout

    if tag is not None:
        run("tag", "-d", tag)
    current = run("rev-parse", "HEAD").decode().strip()
    if current and current != source_head:
        run("reset", "--soft", source_head)
    run("checkout", source_head, "--", *profile.allowlist)
    run("reset", "HEAD", "--", *profile.allowlist)
    return tuple(failures)


def _failure_with_local_revert(
    failure: BaseException,
    revert_failures: tuple[str, ...],
) -> BaseException:
    if not revert_failures:
        return failure
    detail = "; ".join(revert_failures)
    if isinstance(failure, Exception):
        return ApplyError(f"{failure}; local commit revert failures: {detail}")
    failure.add_note(f"local commit revert failures: {detail}")
    return failure


def _failure_text(failure: BaseException) -> str:
    notes = getattr(failure, "__notes__", ())
    suffix = "" if not notes else "; " + "; ".join(str(item) for item in notes)
    return f"{failure}{suffix}"


def _commit_deletion_state(
    profile: FaceProfile,
    pgl_home: Path,
    config: Mapping[str, object],
    run_date: str,
    ledger: dict[str, Any] | None,
    blocklist: list[str] | BlocklistState | None,
    digest: Digest,
    boundary: _LucaTransportBoundary,
    **kwargs: Any,
):
    home = profile.resolve_home(pgl_home, config)
    source_head = _git(home, "rev-parse", "HEAD").stdout.decode().strip()
    success_events = list(kwargs.get("success_events") or [])
    if boundary.enabled:
        before_render = {
            relative: (home / relative).read_bytes()
            for relative in profile.render_files.values()
        }
        repo_baseline_hash = _build(profile, home, before_render, config)
        _assert_guarded_payloads(profile, home, before_render)
        boundary.configure_deletion_baseline(
            repo_baseline_hash=repo_baseline_hash
        )
    result = commit_state(
        profile,
        pgl_home,
        config,
        run_date,
        ledger,
        blocklist,
        digest,
        before_commit=boundary.before_commit if boundary.enabled else None,
        emit_success=not boundary.enabled,
        **kwargs,
    )
    try:
        if boundary.enabled and not result.changed:
            if result.content_hash is None:
                raise ApplyError("unchanged Luca state has no production digest target")
            boundary.before_commit(result.content_hash)
        boundary.complete(result.content_hash)
    except BaseException as exc:
        revert_failures = (
            _revert_completed_manual_commit(
                profile, home, source_head, result.tag
            )
            if result.changed
            else ()
        )
        raise _failure_with_local_revert(exc, revert_failures)
    if boundary.enabled and result.changed:
        digest.emit(
            f"{profile.name}: committed {result.tag} content_hash={result.content_hash}"
        )
        for event in success_events:
            digest.emit(event)
    return result


def _with_transport_recovery(
    boundary: _LucaTransportBoundary,
    failure: BaseException,
) -> BaseException:
    try:
        boundary.recover_after_local_revert()
    except BaseException as recovery_failure:
        detail = f"production recovery failed after local revert: {recovery_failure}"
        if isinstance(failure, Exception):
            return ApplyError(f"{_failure_text(failure)}; {detail}")
        failure.add_note(detail)
    return failure


def block_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("face", choices=("alpha", "luca"))
    parser.add_argument("phrase_id")
    parser.add_argument("--date")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    pgl_home, run_date, config, profile, digest = _context(args.face, args.config, args.date)
    boundary = _LucaTransportBoundary(
        profile=profile,
        pgl_home=pgl_home,
        config=config,
        config_path=args.config,
        run_date=run_date,
        operation="deletion",
    )

    def action() -> int:
        try:
            drift = emit_deletion_drift_note(digest, profile.name, "pgl-block")
            drift_suffix = (
                f" UCD drift runtime={drift.runtime_version} corpus={drift.corpus_version} "
                f"— matching may miss variants; direction={drift.direction}"
                if drift is not None
                else ""
            )
            context = deletion_context(
                "block",
                target_phrase_ids=(args.phrase_id,),
            )
            audit = check_all(
                pgl_home,
                profile.name,
                run_date,
                killswitch_exception="safety",
                deletion=context,
            )
            if audit is not None:
                context = replace(context, audit=audit)
            home = profile.resolve_home(pgl_home, config)
            _require_overlay_home(profile, home)
            verify_manifest(profile, pgl_home, config)
            ledger = _load_required_ledger(
                home / profile.ledger_path, profile.name, "pgl-block"
            )
            phrase = next(
                (item for item in ledger["phrases"] if item["id"] == args.phrase_id),
                None,
            )
            if phrase is None:
                raise ApplyError(f"phrase not found {args.phrase_id}")
            context = replace(
                context,
                target_phrase_texts=(phrase["text"],),
            )
            if phrase["state"] == "blocked":
                if boundary.enabled:
                    try:
                        blocklist_state = read_blocklist_state(
                            home / profile.blocklist_path
                        )
                    except (OSError, UnicodeError):
                        blocklist_state = BlocklistState(
                            None, None, managed=False
                        )
                    _commit_deletion_state(
                        profile,
                        pgl_home,
                        config,
                        run_date,
                        ledger,
                        blocklist_state,
                        digest,
                        boundary,
                        killswitch_exception="safety",
                        deletion=context,
                    )
                digest.emit(f"{profile.name}: block no-op {args.phrase_id}{drift_suffix}")
                return 0
            pid = proposal_id(profile.name, args.phrase_id, f"{phrase['state']}->blocked", run_date)
            apply_transition(ledger, args.phrase_id, f"{phrase['state']}->blocked", run_date, pid)
            try:
                blocklist_state = read_blocklist_state(home / profile.blocklist_path)
            except UnicodeError as exc:
                digest.emit(
                    f"[RED] {profile.name}: block blocklist fault; preserving bytes: "
                    f"{home / profile.blocklist_path}: {exc}"
                )
                blocklist_state = append_blocklist_state_preserving_invalid_bytes(
                    home / profile.blocklist_path, phrase["text"]
                )
            else:
                append_blocked_text(blocklist_state.entries, phrase["text"])
                blocklist_state = _normalized_blocklist_state(blocklist_state.entries)
            _commit_deletion_state(
                profile,
                pgl_home,
                config,
                run_date,
                ledger,
                blocklist_state,
                digest,
                boundary,
                killswitch_exception="safety",
                deletion=context,
                success_events=[f"{profile.name}: immediate block {args.phrase_id}{drift_suffix}"],
            )
            return 0
        except BaseException as exc:
            failure = _with_transport_recovery(boundary, exc)
            digest.emit(
                f"[RED] {profile.name}: block failed: {_failure_text(failure)}"
            )
            if isinstance(exc, Exception):
                return 1
            raise

    return _locked(pgl_home, profile.name, digest, action)


def eject_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("face", choices=("alpha", "luca"))
    parser.add_argument("--date")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    pgl_home, run_date, config, profile, digest = _context(args.face, args.config, args.date)
    boundary = _LucaTransportBoundary(
        profile=profile,
        pgl_home=pgl_home,
        config=config,
        config_path=args.config,
        run_date=run_date,
        operation="deletion",
    )

    def action() -> int:
        try:
            drift = emit_deletion_drift_note(digest, profile.name, "pgl-eject")
            drift_suffix = (
                f" UCD drift runtime={drift.runtime_version} corpus={drift.corpus_version} "
                f"— matching may miss variants; direction={drift.direction}"
                if drift is not None
                else ""
            )
            if killswitch_mode(pgl_home) != "eject":
                raise ApplyError("eject requires KILLSWITCH mode: eject")
            context = deletion_context("eject")
            audit = check_all(
                pgl_home,
                profile.name,
                run_date,
                killswitch_exception="eject",
                deletion=context,
            )
            if audit is not None:
                context = replace(context, audit=audit)
            home = profile.resolve_home(pgl_home, config)
            _require_overlay_home(profile, home)
            # This manifest is a required soul-integrity gate, unlike ledger
            # and blocklist content, so eject intentionally still verifies it.
            verify_manifest(profile, pgl_home, config)
            ledger_fault: Exception | None = None
            try:
                ledger = load_ledger(home / profile.ledger_path, profile.name)
            except Exception as exc:
                ledger = None
                ledger_fault = exc
                digest.emit(
                    f"[RED] {profile.name}: eject ledger fault; preserving ledger bytes: {exc}"
                )
            if ledger is not None:
                ledger["history"].append({"at": run_date, "action": "eject"})
            blocklist_fault: Exception | None = None
            try:
                (home / profile.blocklist_path).read_text(encoding="utf-8")
            except Exception as exc:
                blocklist_fault = exc
                digest.emit(
                    f"[RED] {profile.name}: eject blocklist fault; preserving blocklist bytes: {exc}"
                )
            _commit_deletion_state(
                profile,
                pgl_home,
                config,
                run_date,
                ledger,
                None,
                digest,
                boundary,
                killswitch_exception="eject",
                deletion=context,
                force_empty_render=True,
            )
            suffix = " ledger-independent" if ledger_fault is not None else ""
            if blocklist_fault is not None:
                suffix += " blocklist-independent"
            digest.emit(f"{profile.name}: eject completed{suffix}{drift_suffix}")
            return 0
        except BaseException as exc:
            failure = _with_transport_recovery(boundary, exc)
            digest.emit(
                f"[RED] {profile.name}: eject failed: {_failure_text(failure)}"
            )
            if isinstance(exc, Exception):
                return 1
            raise

    return _locked(pgl_home, profile.name, digest, action)


def _atomic_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    payload = "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in records).encode("utf-8")
    temporary = path.with_name(f".{path.name}.forget-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.forget-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


_USAGE_KEYS = {"ts", "session", "face", "phrase_id", "state"}
_USAGE_OPTIONAL_KEYS = {"ucd"}
_TIER_L_KEYS = {"ts", "host", "face", "session", "project", "speaker", "text", "len"}
_TIER_L_OPTIONAL_KEYS = {"ucd"}


def _validate_usage_record(item: object, path: Path, number: int) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ApplyError(f"invalid usage record {path}:{number}")
    keys = set(item)
    if not (_USAGE_KEYS <= keys <= (_USAGE_KEYS | _USAGE_OPTIONAL_KEYS)):
        raise ApplyError(f"invalid usage record {path}:{number}")
    if item.get("state") not in {"staged", "adopted"}:
        raise ApplyError(f"invalid usage record {path}:{number}")
    if not all(isinstance(item.get(key), str) for key in ("ts", "session", "face", "phrase_id")):
        raise ApplyError(f"invalid usage record {path}:{number}")
    if "ucd" in item and not isinstance(item.get("ucd"), str):
        raise ApplyError(f"invalid usage record {path}:{number}")
    return dict(item)


def _validate_tier_l_record(item: object, path: Path, number: int) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ApplyError(f"invalid Tier L record {path}:{number}")
    keys = set(item)
    if not (_TIER_L_KEYS <= keys <= (_TIER_L_KEYS | _TIER_L_OPTIONAL_KEYS)):
        raise ApplyError(f"invalid Tier L record {path}:{number}")
    if any(not isinstance(item.get(key), str) for key in _TIER_L_KEYS - {"len"}):
        raise ApplyError(f"invalid Tier L record {path}:{number}")
    if type(item.get("len")) is not int or item["len"] != len(str(item["text"])):
        raise ApplyError(f"invalid Tier L record {path}:{number}")
    if "ucd" in item and not isinstance(item.get("ucd"), str):
        raise ApplyError(f"invalid Tier L record {path}:{number}")
    return dict(item)


def _rewrite_obslog_file(
    path: Path,
    *,
    ids: set[str],
    pattern: str,
) -> tuple[bytes, int, int, list[str]]:
    rewritten = bytearray()
    tier_l_removed = 0
    usage_removed = 0
    malformed: list[str] = []
    for number, raw_line in enumerate(path.read_bytes().splitlines(keepends=True), 1):
        try:
            decoded = raw_line.decode("utf-8")
            line = decoded[:-1] if decoded.endswith("\n") else decoded
            parsed = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed.append(f"{path}:{number}")
            rewritten.extend(raw_line)
            continue
        if path.name.startswith("usage-"):
            item = _validate_usage_record(parsed, path, number)
            remove = item.get("phrase_id") in ids
            if remove:
                usage_removed += 1
            else:
                rewritten.extend(raw_line)
            continue
        item = _validate_tier_l_record(parsed, path, number)
        remove = matching_contains(pattern, str(item.get("text", "")))
        if remove:
            tier_l_removed += 1
        else:
            rewritten.extend(raw_line)
    return bytes(rewritten), tier_l_removed, usage_removed, malformed


def _normalized_blocklist_state(entries: list[str]) -> BlocklistState:
    payload = (("\n".join(sorted(set(entries))) + "\n").encode("utf-8") if entries else b"")
    return BlocklistState(entries, payload)


def forget_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("face", choices=("alpha", "luca"))
    parser.add_argument("pattern")
    parser.add_argument("--date")
    parser.add_argument("--config")
    parser.add_argument("--yes", "--confirm", dest="confirmed", action="store_true")
    args = parser.parse_args(argv)
    pgl_home, run_date, config, profile, digest = _context(args.face, args.config, args.date)
    boundary = _LucaTransportBoundary(
        profile=profile,
        pgl_home=pgl_home,
        config=config,
        config_path=args.config,
        run_date=run_date,
        operation="deletion",
    )

    def action() -> int:
        backups: dict[Path, bytes] = {}
        written: set[Path] = set()
        try:
            drift = emit_deletion_drift_note(digest, profile.name, "pgl-forget")
            drift_suffix = (
                f" UCD drift runtime={drift.runtime_version} corpus={drift.corpus_version} "
                f"— matching may miss variants; direction={drift.direction}"
                if drift is not None
                else ""
            )
            context = deletion_context("forget")
            audit = check_all(
                pgl_home,
                profile.name,
                run_date,
                killswitch_exception="safety",
                deletion=context,
            )
            if audit is not None:
                context = replace(context, audit=audit)
            home = profile.resolve_home(pgl_home, config)
            _require_overlay_home(profile, home)
            verify_manifest(profile, pgl_home, config)
            ledger = _load_required_ledger(
                home / profile.ledger_path, profile.name, "pgl-forget"
            )
            matching_pattern = canonicalize_for_matching(args.pattern)
            if not matching_pattern:
                raise ValueError("forget pattern must not be empty or whitespace-only")
            matched = [
                item
                for item in ledger["phrases"]
                if matching_contains(args.pattern, item["text"])
            ]
            context = replace(
                context,
                target_phrase_ids=tuple(item["id"] for item in matched),
                target_phrase_texts=tuple(item["text"] for item in matched),
            )
            ids = {item["id"] for item in matched}
            counts = {"tier_l": 0, "usage": 0, "ledger": len(matched)}
            obs_dir = pgl_home / "obslog" / profile.name
            if obs_dir.is_symlink():
                raise ApplyError(f"symlinked observation directory rejected: {obs_dir}")
            rewrites: dict[Path, bytes] = {}
            malformed_lines: list[str] = []
            for path in sorted(obs_dir.glob("*.jsonl")):
                if path.is_symlink():
                    raise ApplyError(f"symlinked observation file rejected: {path}")
                backups[path] = path.read_bytes()
                rewritten, tier_l_removed, usage_removed, malformed = _rewrite_obslog_file(
                    path,
                    ids=ids,
                    pattern=args.pattern,
                )
                rewrites[path] = rewritten
                counts["tier_l"] += tier_l_removed
                counts["usage"] += usage_removed
                malformed_lines.extend(malformed)
            digest.emit(
                f"{profile.name}: forget dry-run "
                f"phrases={counts['ledger']} tier_l={counts['tier_l']} usage={counts['usage']}"
            )
            if malformed_lines:
                digest.emit(
                    f"[RED] {profile.name}: "
                    + "; ".join(
                        f"{location}: unparseable obslog line preserved"
                        for location in malformed_lines
                    )
                    + f" — forget pattern match NOT verified for {len(malformed_lines)} line(s){drift_suffix}"
                )
            if (counts["ledger"] > 1 or counts["tier_l"] + counts["usage"] > 20) and not args.confirmed:
                raise ValueError(
                    "forget confirmation required above 1 phrase or 20 observation records; re-run with --yes"
                )
            try:
                blocklist_state = read_blocklist_state(home / profile.blocklist_path)
            except (OSError, UnicodeError) as exc:
                blocklist_state = BlocklistState(None, None, managed=False)
                digest.emit(
                    f"[RED] {profile.name}: forget blocklist fault; preserving blocklist bytes: {home / profile.blocklist_path}: {exc}{drift_suffix}"
                )
            for item in matched:
                if blocklist_state.entries is not None:
                    append_blocked_text(blocklist_state.entries, item["text"])
                ledger["phrases"].remove(item)
                ledger["retired_ids"].append(item["id"])
                digest_value = normalized_hash(item["text"])
                if digest_value not in ledger["retired_text_hashes"]:
                    ledger["retired_text_hashes"].append(digest_value)
            if blocklist_state.entries is not None:
                blocklist_state = _normalized_blocklist_state(blocklist_state.entries)
            for path, rewritten in rewrites.items():
                if rewritten != backups[path]:
                    written.add(path)
                    _atomic_bytes(path, rewritten)
            ledger["history"].append(
                {
                    "at": run_date,
                    "action": "forget",
                    "pattern_hash": hashlib.sha256(
                        matching_pattern.encode()
                    ).hexdigest(),
                    "counts": counts,
                }
            )
            _commit_deletion_state(
                profile,
                pgl_home,
                config,
                run_date,
                ledger,
                blocklist_state,
                digest,
                boundary,
                killswitch_exception="safety",
                deletion=context,
                success_events=[f"{profile.name}: forget completed counts={counts}{drift_suffix}"],
            )
            return 0
        except BaseException as exc:
            restore_failures: list[str] = []
            for path in written:
                try:
                    _atomic_bytes(path, backups[path])
                except BaseException as restore_failure:
                    restore_failures.append(f"{path}: {restore_failure}")
            failure: BaseException = exc
            if restore_failures:
                failure = _failure_with_local_revert(
                    failure,
                    tuple(f"observation restore {item}" for item in restore_failures),
                )
            failure = _with_transport_recovery(boundary, failure)
            digest.emit(
                f"[RED] {profile.name}: forget failed/reverted: "
                f"{_failure_text(failure)}"
            )
            if isinstance(exc, Exception):
                return 1
            raise

    return _locked(pgl_home, profile.name, digest, action)


def baseline_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("face", choices=("alpha", "luca"))
    parser.add_argument("files", nargs="+")
    parser.add_argument("--config")
    parser.add_argument("--confirm-root-change", action="store_true")
    args = parser.parse_args(argv)
    pgl_home, run_date, config, profile, digest = _context(args.face, args.config, None)
    pgl_home.mkdir(parents=True, exist_ok=True)
    os.chmod(pgl_home, 0o700)

    def established_face(home: Path) -> bool:
        ledger_path = home / profile.ledger_path
        if ledger_path.is_symlink():
            return True
        try:
            ledger = load_ledger(ledger_path, profile.name)
        except Exception:
            try:
                if ledger_path.read_bytes().strip():
                    return True
            except OSError:
                if ledger_path.exists():
                    return True
        else:
            if ledger["phrases"]:
                return True
        for rel_path in profile.render_files.values():
            render_path = home / rel_path
            if render_path.is_symlink():
                return True
            try:
                if render_path.read_bytes().strip():
                    return True
            except OSError:
                if render_path.exists():
                    return True
        return False

    def action() -> int:
        try:
            check_cp(pgl_home, profile.name)
            check_killswitch(pgl_home)
            manifest_path = profile.baseline_manifest(pgl_home)
            resolved_home = profile.resolve_home(pgl_home, config)
            new_home = str(resolved_home)
            recorded_home = read_overlay_home_pin(profile, pgl_home)
            if recorded_home is None and manifest_path.exists():
                try:
                    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    existing = None
                recorded_home = existing.get("overlay_home") if isinstance(existing, dict) else None
            changed_recorded_root = (
                isinstance(recorded_home, str) and recorded_home != new_home
            )
            if (
                (changed_recorded_root or established_face(resolved_home))
                and not args.confirm_root_change
            ):
                raise ApplyError(
                    "baseline overlay home change requires --confirm-root-change: "
                    f"recorded={recorded_home!r} resolved={new_home!r}"
                )
            path = write_manifest(profile, pgl_home, config, [Path(item) for item in args.files])
            pinned = verify_manifest(profile, pgl_home, config)
            write_overlay_home_pin(profile, pgl_home, new_home)
        except Exception as exc:
            digest.emit(f"[RED] {profile.name}: baseline failed: {exc}")
            return 1
        pinned_files = ",".join(
            f"{item['path']}@{item['sha256']}" for item in pinned
        )
        coverage_parts: list[str] = []
        for item in pinned:
            coverage = item.get("coverage")
            if not isinstance(coverage, dict):
                continue
            sections = coverage.get("sections")
            if not isinstance(sections, list):
                continue
            section_sizes = ", ".join(
                f"{section['prefix']!r}={section['lines']} lines/{section['bytes']} bytes"
                for section in sections
                if isinstance(section, dict)
            )
            coverage_parts.append(
                f"{item['path']}[{section_sizes}; markers={coverage.get('markers')}]"
            )
        coverage_text = (
            f" coverage={'; '.join(coverage_parts)}" if coverage_parts else ""
        )
        digest.emit(
            f"{profile.name}: baseline pinned overlay_home={new_home} "
            f"files={pinned_files}{coverage_text}"
        )
        print(path)
        return 0

    return _locked(pgl_home, profile.name, digest, action)


def rollback_main(argv: list[str] | None = None) -> int:
    # R13 starts at command entry, so config parsing and lock contention cannot
    # disappear from the attended operator's production-stop measurement.
    operation_started_at = time.monotonic()
    parser = argparse.ArgumentParser()
    parser.add_argument("face", choices=("alpha", "luca"))
    parser.add_argument("tag")
    parser.add_argument("--date")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    pgl_home, run_date, config, profile, digest = _context(args.face, args.config, args.date)
    boundary = _LucaTransportBoundary(
        profile=profile,
        pgl_home=pgl_home,
        config=config,
        config_path=args.config,
        run_date=run_date,
        operation="rollback",
    )

    def action() -> int:
        home = profile.resolve_home(pgl_home, config)
        originals: dict[str, bytes | None] = {}
        source_head = ""
        commit_created = False
        rollback_audit_emitted = False
        rollback_direction: Literal["reduction", "increase"] | None = None

        def emit_rollback_audit(reason: str) -> None:
            nonlocal rollback_audit_emitted
            if rollback_audit_emitted:
                return
            digest.emit(
                f"[WARN] luca: rollback audit gates=unverified({reason}) "
                "attended=true direction=reduction"
            )
            rollback_audit_emitted = True

        def gate_check() -> None:
            if profile.name != "luca":
                check_all(pgl_home, profile.name, run_date)
                return
            if rollback_direction == "increase":
                try:
                    check_all(pgl_home, profile.name, run_date)
                except GateError as exc:
                    raise ApplyError(
                        "rollback direction=increase requires full gates: "
                        f"{exc}"
                    ) from exc
                return
            if rollback_direction != "reduction":
                raise ApplyError("rollback direction is not classified")
            # Only a production-anchored reduction inherits §3.4's attended
            # killswitch relaxation.  CP-2 explicit NO remains binding; CP-3
            # explicit NO and unavailable CP evidence are audited in this lane.
            try:
                check_cp(pgl_home, profile.name)
            except GateExplicitNo as exc:
                if exc.checkpoint == "cp2":
                    raise
                emit_rollback_audit("cp3-not-go")
            except GateUnavailable as exc:
                emit_rollback_audit(str(exc))

        try:
            if re.fullmatch(rf"overlay-snap-{re.escape(profile.name)}-\d{{8}}-\d+", args.tag) is None:
                raise ApplyError("invalid rollback snapshot tag")
            _require_overlay_home(profile, home)
            if profile.name == "alpha":
                # Preserve the pre-#51 alpha byte-order exactly: base gates,
                # clean allowlist, then the manifest integrity check.
                check_all(pgl_home, profile.name, run_date)
                _require_clean_allowlist(profile, home)
                verify_manifest(profile, pgl_home, config)
            else:
                # Luca must render the verified target before it can classify
                # direction and choose the full-gate or reduction gate posture.
                verify_manifest(profile, pgl_home, config)
                _require_clean_allowlist(profile, home)
            originals = _snapshot_files(profile, home)
            source_head = _git(home, "rev-parse", "HEAD").stdout.decode().strip()
            before_render = {path: (home / path).read_bytes() for path in profile.render_files.values()}
            before_hash = _build(profile, home, before_render, config)
            _assert_guarded_payloads(profile, home, {rel: payload for rel, payload in originals.items() if payload is not None})
            _git(home, "checkout", args.tag, "--", *profile.allowlist)
            ledger = load_ledger(home / profile.ledger_path, profile.name)
            rendered = {path: (home / path).read_bytes() for path in profile.render_files.values()}
            restored_payloads = {rel: (home / rel).read_bytes() for rel in profile.allowlist}
            after_hash = _build(profile, home, rendered, config)
            _assert_guarded_payloads(profile, home, restored_payloads)
            expected = next((item["content_hash"] for item in reversed(ledger["snapshots"]) if isinstance(item, dict) and "content_hash" in item), None)
            if expected is None or expected != after_hash:
                raise ApplyError("rollback content_hash verification failed")
            verify_manifest(profile, pgl_home, config)
            if profile.name == "luca":
                user = config.get("display_name")
                if not isinstance(user, str) or not user:
                    raise ApplyError("face config requires display_name")
                try:
                    _enforce_render_monotonicity(
                        profile,
                        before_render,
                        rendered,
                        user,
                    )
                except ApplyError:
                    target_is_reduction = False
                else:
                    target_is_reduction = True
                rollback_direction = boundary.configure_rollback_direction(
                    repo_baseline_hash=before_hash,
                    target_is_monotone_non_increasing=target_is_reduction,
                )
            gate_check()
            worktree_clean = _git(home, "diff", "--quiet", "--", *profile.allowlist, check=False).returncode == 0
            index_clean = _git(home, "diff", "--cached", "--quiet", "--", *profile.allowlist, check=False).returncode == 0
            if worktree_clean and index_clean:
                if boundary.enabled:
                    boundary.before_commit(after_hash)
                    boundary.complete(after_hash)
                print(f"before={before_hash} after={after_hash}")
                if rollback_direction == "reduction":
                    elapsed = boundary.reflection_elapsed(operation_started_at)
                    print(
                        "R13-Luca production-stop-seconds="
                        f"{elapsed:.3f} endpoint=production-stops-injecting"
                    )
                digest.emit(f"{profile.name}: rollback no-op {args.tag} before={before_hash} after={after_hash}")
                return 0
            _git(home, "add", "--", *profile.allowlist)
            verify_manifest(profile, pgl_home, config)
            gate_check()
            if boundary.enabled:
                boundary.before_commit(after_hash)
            _git(home, "commit", "--only", "-m", f"Restore verified {profile.name} overlay snapshot\n\nSource-Tag: {args.tag}\nContent-Hash: {after_hash}\n", "--", *profile.allowlist)
            commit_created = True
            verify_manifest(profile, pgl_home, config)
            gate_check()
            _git(home, "checkout", "HEAD", "--", *profile.allowlist)
            boundary.complete(after_hash)
            print(f"before={before_hash} after={after_hash}")
            if rollback_direction == "reduction":
                elapsed = boundary.reflection_elapsed(operation_started_at)
                print(
                    "R13-Luca production-stop-seconds="
                    f"{elapsed:.3f} endpoint=production-stops-injecting"
                )
            digest.emit(f"{profile.name}: rollback {args.tag} before={before_hash} after={after_hash}")
            return 0
        except BaseException as exc:
            revert_failures: list[str] = []
            if commit_created and source_head:
                revert_failures.extend(
                    _revert_completed_manual_commit(
                        profile, home, source_head, None
                    )
                )
            elif originals:
                try:
                    _restore_files(profile, home, originals)
                except BaseException as restore_failure:
                    revert_failures.append(
                        f"allowlist restore raised {restore_failure}"
                    )
                try:
                    reset = _git(
                        home,
                        "reset",
                        "HEAD",
                        "--",
                        *profile.allowlist,
                        check=False,
                    )
                    if reset.returncode:
                        detail = (reset.stderr or reset.stdout).decode(
                            "utf-8", "replace"
                        ).strip()
                        revert_failures.append(
                            f"git reset HEAD failed with exit {reset.returncode}: {detail}"
                        )
                except BaseException as reset_failure:
                    revert_failures.append(f"git reset HEAD raised {reset_failure}")
            failure = _failure_with_local_revert(exc, tuple(revert_failures))
            failure = _with_transport_recovery(boundary, failure)
            digest.emit(
                f"[RED] {profile.name}: rollback failed/reverted: "
                f"{_failure_text(failure)}"
            )
            if isinstance(exc, Exception):
                return 1
            raise

    return _locked(pgl_home, profile.name, digest, action)
