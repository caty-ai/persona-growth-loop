"""Fail-closed acceptance-window journal validation for Luca voice."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


INTENT_JOURNAL_RELATIVE_PATH = Path("state/luca-intent-journal.jsonl")
ACCEPTANCE_WINDOW_EVENTS = frozenset(
    {
        "acceptance-window-open",
        "acceptance-window-close",
        "acceptance-window-resolved",
    }
)
DEPLOY_STARTED_EVENT = "deploy-started"
ACCEPTANCE_SUCCEEDED_EVENT = "acceptance-succeeded"
COMMIT_COMPLETED_EVENT = "commit-completed"
RECOVERY_STARTED_EVENT = "recovery-started"
RECOVERY_COMPLETED_EVENT = "recovery-completed"
DEPLOY_ABORTED_EVENT = "deploy-aborted"
ATTENDED_SUPERSEDED_EVENT = "attended-superseded"
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
# Measured clock-skew boundary is +/-1.9s; 60s is approximately 30x that bound.
WINDOW_MARGIN_SECONDS = 60


class JournalError(ValueError):
    pass


@dataclass(frozen=True)
class ExclusionWindow:
    start_epoch: float
    end_epoch: float | None

    def contains(self, timestamp: float) -> bool:
        return timestamp >= self.start_epoch and (
            self.end_epoch is None or timestamp <= self.end_epoch
        )


@dataclass(frozen=True)
class JournalState:
    windows: tuple[ExclusionWindow, ...]
    has_open_window: bool
    open_window_ids: tuple[str, ...]

    def excludes(self, timestamp: float) -> bool:
        return any(window.contains(timestamp) for window in self.windows)


def append_event(
    obs_root: str | Path,
    event: str,
    *,
    ts: str,
    window_id: str | None = None,
    operation: str | None = None,
    production_hash: str | None = None,
) -> None:
    """Durably append one private intent-journal event."""

    if not isinstance(event, str) or not event:
        raise JournalError("journal event must be a non-empty string")
    if not isinstance(ts, str) or not ts:
        raise JournalError("journal ts must be a non-empty string")
    _parse_aware_timestamp(ts, None)
    if event in ACCEPTANCE_WINDOW_EVENTS:
        if not isinstance(window_id, str) or not window_id:
            raise JournalError(
                f"journal event {event}: window_id must be a non-empty string"
            )
    elif window_id is not None:
        raise JournalError("window_id is only valid for acceptance-window events")
    if event == ATTENDED_SUPERSEDED_EVENT:
        if operation not in {"deletion", "rollback"}:
            raise JournalError(
                "attended-superseded operation must be deletion or rollback"
            )
        if (
            not isinstance(production_hash, str)
            or _CONTENT_HASH.fullmatch(production_hash) is None
        ):
            raise JournalError(
                "attended-superseded production_hash must be a content hash"
            )
    elif operation is not None or production_hash is not None:
        raise JournalError(
            "operator context is only valid for attended-superseded"
        )

    payload: dict[str, object] = {"ts": ts, "event": event}
    if window_id is not None:
        payload["window_id"] = window_id
    if event == ATTENDED_SUPERSEDED_EVENT:
        payload.update(
            {
                "attended": True,
                "operation": operation,
                "production_hash": production_hash,
            }
        )
    path = Path(obs_root).expanduser() / INTENT_JOURNAL_RELATIVE_PATH
    _append_private_line(path, payload)


def append_deploy_started(obs_root: str | Path, *, ts: str) -> None:
    append_event(obs_root, DEPLOY_STARTED_EVENT, ts=ts)


def append_acceptance_succeeded(obs_root: str | Path, *, ts: str) -> None:
    append_event(obs_root, ACCEPTANCE_SUCCEEDED_EVENT, ts=ts)


def append_commit_completed(obs_root: str | Path, *, ts: str) -> None:
    append_event(obs_root, COMMIT_COMPLETED_EVENT, ts=ts)


def append_recovery_completed(obs_root: str | Path, *, ts: str) -> None:
    append_event(obs_root, RECOVERY_COMPLETED_EVENT, ts=ts)


def append_recovery_started(obs_root: str | Path, *, ts: str) -> None:
    append_event(obs_root, RECOVERY_STARTED_EVENT, ts=ts)


def append_deploy_aborted(obs_root: str | Path, *, ts: str) -> None:
    append_event(obs_root, DEPLOY_ABORTED_EVENT, ts=ts)


def append_attended_superseded(
    obs_root: str | Path,
    *,
    ts: str,
    operation: str,
    production_hash: str,
) -> None:
    """Resolve accepted NEW and atomically open its attended successor."""

    append_event(
        obs_root,
        ATTENDED_SUPERSEDED_EVENT,
        ts=ts,
        operation=operation,
        production_hash=production_hash,
    )


def append_acceptance_window_open(
    obs_root: str | Path, *, ts: str, window_id: str
) -> None:
    append_event(
        obs_root,
        "acceptance-window-open",
        ts=ts,
        window_id=window_id,
    )


def append_acceptance_window_close(
    obs_root: str | Path, *, ts: str, window_id: str
) -> None:
    append_event(
        obs_root,
        "acceptance-window-close",
        ts=ts,
        window_id=window_id,
    )


def append_acceptance_window_resolved(
    obs_root: str | Path, *, ts: str, window_id: str
) -> None:
    append_event(
        obs_root,
        "acceptance-window-resolved",
        ts=ts,
        window_id=window_id,
    )


def load_journal(obs_root: str | Path) -> JournalState:
    path = Path(obs_root).expanduser() / INTENT_JOURNAL_RELATIVE_PATH
    lines = _read_private_lines(path, "intent journal")
    opened: dict[str, datetime] = {}
    seen_window_ids: set[str] = set()
    windows: list[ExclusionWindow] = []

    for number, line in enumerate(lines, 1):
        event = _parse_event(line, number)
        event_name = event["event"]
        if event_name not in ACCEPTANCE_WINDOW_EVENTS:
            continue
        window_id = event["window_id"]
        timestamp = event["parsed_ts"]
        if event_name == "acceptance-window-open":
            if window_id in seen_window_ids:
                raise JournalError(
                    f"intent journal line {number}: duplicate open window_id"
                )
            seen_window_ids.add(window_id)
            opened[window_id] = timestamp
            continue
        opened_at = opened.pop(window_id, None)
        if opened_at is None:
            raise JournalError(
                f"intent journal line {number}: close/resolved without open"
            )
        if timestamp < opened_at:
            raise JournalError(
                f"intent journal line {number}: close/resolved precedes open"
            )
        windows.append(
            ExclusionWindow(
                start_epoch=_epoch(opened_at, number) - WINDOW_MARGIN_SECONDS,
                end_epoch=_epoch(timestamp, number) + WINDOW_MARGIN_SECONDS,
            )
        )

    for opened_at in opened.values():
        windows.append(
            ExclusionWindow(
                start_epoch=_epoch(opened_at, None) - WINDOW_MARGIN_SECONDS,
                end_epoch=None,
            )
        )
    windows.sort(
        key=lambda window: (
            window.start_epoch,
            float("inf") if window.end_epoch is None else window.end_epoch,
        )
    )
    return JournalState(
        windows=tuple(windows),
        has_open_window=bool(opened),
        open_window_ids=tuple(opened),
    )


def load_event_names(obs_root: str | Path) -> tuple[str, ...]:
    """Return validated journal event names in append order."""

    path = Path(obs_root).expanduser() / INTENT_JOURNAL_RELATIVE_PATH
    return tuple(
        _parse_event(line, number)["event"]
        for number, line in enumerate(_read_private_lines(path, "intent journal"), 1)
    )


def _parse_event(line: str, number: int) -> dict[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise JournalError(f"intent journal line {number}: invalid JSON") from exc
    if not isinstance(payload, dict):
        raise JournalError(f"intent journal line {number}: entry must be an object")
    timestamp = payload.get("ts")
    event = payload.get("event")
    if not isinstance(timestamp, str) or not timestamp:
        raise JournalError(f"intent journal line {number}: ts must be a non-empty string")
    if not isinstance(event, str) or not event:
        raise JournalError(f"intent journal line {number}: event must be a non-empty string")
    parsed_ts = _parse_aware_timestamp(timestamp, number)
    parsed: dict[str, Any] = {"event": event, "parsed_ts": parsed_ts}
    if event in ACCEPTANCE_WINDOW_EVENTS:
        window_id = payload.get("window_id")
        if not isinstance(window_id, str) or not window_id:
            raise JournalError(
                f"intent journal line {number}: window_id must be a non-empty string"
            )
        parsed["window_id"] = window_id
    elif event == ATTENDED_SUPERSEDED_EVENT:
        if payload.get("attended") is not True:
            raise JournalError(
                f"intent journal line {number}: attended-superseded must be attended"
            )
        operation = payload.get("operation")
        if operation not in {"deletion", "rollback"}:
            raise JournalError(
                f"intent journal line {number}: attended-superseded operation is invalid"
            )
        production_hash = payload.get("production_hash")
        if (
            not isinstance(production_hash, str)
            or _CONTENT_HASH.fullmatch(production_hash) is None
        ):
            raise JournalError(
                f"intent journal line {number}: attended-superseded production_hash is invalid"
            )
        parsed["operation"] = operation
        parsed["production_hash"] = production_hash
    return parsed


def _parse_aware_timestamp(value: str, number: int | None) -> datetime:
    location = "" if number is None else f" line {number}"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise JournalError(f"intent journal{location}: ts is not ISO8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JournalError(f"intent journal{location}: ts must include an offset")
    return parsed


def _epoch(value: datetime, number: int | None) -> float:
    try:
        return value.timestamp()
    except (OSError, OverflowError, ValueError) as exc:
        location = "" if number is None else f" line {number}"
        raise JournalError(f"intent journal{location}: ts is outside epoch range") from exc


def _read_private_lines(path: Path, label: str) -> list[str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise JournalError(f"{label} not found: {path}") from exc
    except OSError as exc:
        raise JournalError(f"cannot open {label}: {path}: {exc}") from exc
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            raise JournalError(f"{label} must be a regular 0600 file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        try:
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JournalError(f"cannot decode {label} as UTF-8: {path}") from exc
        return text.splitlines()
    except OSError as exc:
        raise JournalError(f"cannot read {label}: {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _append_private_line(path: Path, payload: dict[str, object]) -> None:
    from . import storage

    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        storage._ensure_private_dir(path.parent)
        directory = os.open(path.parent, storage._directory_open_flags())
    except OSError as exc:
        raise JournalError(f"cannot prepare intent journal: {path}: {exc}") from exc

    descriptor: int | None = None
    created = False
    try:
        common_flags = os.O_RDWR | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            common_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            common_flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(
                path.name,
                common_flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(path.name, common_flags, dir_fd=directory)

        if created:
            os.fchmod(descriptor, 0o600)
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise JournalError(f"intent journal must be a regular 0600 file: {path}")
        if file_stat.st_size:
            os.lseek(descriptor, file_stat.st_size - 1, os.SEEK_SET)
            if os.read(descriptor, 1) != b"\n":
                raise JournalError("intent journal ends with an incomplete event")
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise JournalError(f"short write to intent journal: {path}")
        os.fsync(descriptor)
        if created:
            os.fsync(directory)
    except JournalError:
        raise
    except OSError as exc:
        raise JournalError(f"cannot append intent journal: {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


__all__ = [
    "ACCEPTANCE_SUCCEEDED_EVENT",
    "ACCEPTANCE_WINDOW_EVENTS",
    "ATTENDED_SUPERSEDED_EVENT",
    "COMMIT_COMPLETED_EVENT",
    "DEPLOY_ABORTED_EVENT",
    "DEPLOY_STARTED_EVENT",
    "INTENT_JOURNAL_RELATIVE_PATH",
    "JournalError",
    "JournalState",
    "RECOVERY_COMPLETED_EVENT",
    "RECOVERY_STARTED_EVENT",
    "WINDOW_MARGIN_SECONDS",
    "append_attended_superseded",
    "append_acceptance_succeeded",
    "append_acceptance_window_close",
    "append_acceptance_window_open",
    "append_acceptance_window_resolved",
    "append_commit_completed",
    "append_deploy_aborted",
    "append_deploy_started",
    "append_event",
    "append_recovery_completed",
    "append_recovery_started",
    "load_event_names",
    "load_journal",
]
