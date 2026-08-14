"""Fail-closed acceptance-session ledger validation for Luca voice."""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import storage


LEDGER_LINE_COUNT_RELATIVE_PATH = Path("state/collector/luca.ledger-lines.json")
LEDGER_ENTRY_KEYS = frozenset({"session_id", "recorded_at", "origin"})
LEDGER_ORIGINS = frozenset({"seed", "acceptance"})
LINE_COUNT_SCHEMA_VERSION = 1


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class LedgerState:
    session_ids: frozenset[str]
    line_count: int


def load_ledger(path: str | Path, obs_root: str | Path) -> LedgerState:
    ledger_path = Path(path).expanduser()
    lines = _read_private_lines(ledger_path, "acceptance ledger")
    session_ids: set[str] = set()
    has_seed = False
    for number, line in enumerate(lines, 1):
        entry = _parse_entry(line, number)
        session_ids.add(entry["session_id"])
        has_seed = has_seed or entry["origin"] == "seed"
    if not lines:
        raise LedgerError("acceptance ledger has zero entries")
    if not has_seed:
        raise LedgerError("acceptance ledger has no seed entry")

    previous_count = _load_previous_line_count(obs_root)
    if len(lines) < previous_count:
        raise LedgerError(
            f"acceptance ledger line count decreased: previous={previous_count} current={len(lines)}"
        )
    return LedgerState(session_ids=frozenset(session_ids), line_count=len(lines))


def write_line_count(obs_root: str | Path, line_count: int, recorded_at: str) -> None:
    if isinstance(line_count, bool) or not isinstance(line_count, int) or line_count < 0:
        raise ValueError("line_count must be a non-negative integer")
    _parse_aware_timestamp(recorded_at, "recorded_at")
    path = Path(obs_root).expanduser() / LEDGER_LINE_COUNT_RELATIVE_PATH
    storage.write_atomic_json(
        path,
        {
            "schema_version": LINE_COUNT_SCHEMA_VERSION,
            "line_count": line_count,
            "recorded_at": recorded_at,
        },
    )


def append_acceptance_sessions(
    path: str | Path,
    session_ids: list[str] | tuple[str, ...],
    *,
    recorded_at: str,
) -> None:
    """Durably append every dispatcher-returned acceptance session UUID."""

    if not isinstance(session_ids, (list, tuple)) or not session_ids:
        raise LedgerError("acceptance session_ids must be a non-empty list or tuple")
    if not isinstance(recorded_at, str) or not recorded_at:
        raise LedgerError("acceptance recorded_at must be a non-empty string")
    try:
        _parse_aware_timestamp(recorded_at, "acceptance recorded_at")
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc

    records: list[dict[str, str]] = []
    for index, session_id in enumerate(session_ids, 1):
        if not isinstance(session_id, str) or not _is_canonical_uuid(session_id):
            raise LedgerError(
                f"acceptance session_id {index} must be a canonical UUID"
            )
        records.append(
            {
                "session_id": session_id,
                "recorded_at": recorded_at,
                "origin": "acceptance",
            }
        )

    target = Path(path).expanduser()
    payload = b"".join(
        (
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        for record in records
    )
    _append_private_payload(target, payload)


def _parse_entry(line: str, number: int) -> dict[str, str]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"acceptance ledger line {number}: invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != LEDGER_ENTRY_KEYS:
        raise LedgerError(
            f"acceptance ledger line {number}: keys must be exactly session_id, recorded_at, origin"
        )
    for key in LEDGER_ENTRY_KEYS:
        if not isinstance(payload[key], str) or not payload[key]:
            raise LedgerError(
                f"acceptance ledger line {number}: {key} must be a non-empty string"
            )
    try:
        _parse_aware_timestamp(
            payload["recorded_at"],
            f"acceptance ledger line {number}: recorded_at",
        )
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc
    if payload["origin"] not in LEDGER_ORIGINS:
        raise LedgerError(f"acceptance ledger line {number}: unknown origin")
    return payload


def _load_previous_line_count(obs_root: str | Path) -> int:
    path = Path(obs_root).expanduser() / LEDGER_LINE_COUNT_RELATIVE_PATH
    try:
        payload = json.loads(_read_private_text(path, "ledger line-count baseline"))
    except json.JSONDecodeError as exc:
        raise LedgerError(f"cannot read ledger line-count baseline: {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "line_count",
        "recorded_at",
    }:
        raise LedgerError("ledger line-count baseline has invalid keys")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != LINE_COUNT_SCHEMA_VERSION
    ):
        raise LedgerError("ledger line-count baseline has unsupported schema_version")
    line_count = payload["line_count"]
    if isinstance(line_count, bool) or not isinstance(line_count, int) or line_count < 0:
        raise LedgerError("ledger line-count baseline has invalid line_count")
    recorded_at = payload["recorded_at"]
    if not isinstance(recorded_at, str) or not recorded_at:
        raise LedgerError("ledger line-count baseline has invalid recorded_at")
    try:
        _parse_aware_timestamp(recorded_at, "ledger line-count baseline recorded_at")
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc
    return line_count


def _parse_aware_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include an offset")
    return parsed


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (AttributeError, ValueError):
        return False


def _read_private_lines(path: Path, label: str) -> list[str]:
    return _read_private_text(path, label).splitlines()


def _read_private_text(path: Path, label: str) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise LedgerError(f"{label} not found: {path}") from exc
    except OSError as exc:
        raise LedgerError(f"cannot open {label}: {path}: {exc}") from exc
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            raise LedgerError(f"{label} must be a regular 0600 file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerError(f"cannot decode {label} as UTF-8: {path}") from exc
    except OSError as exc:
        raise LedgerError(f"cannot read {label}: {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _append_private_payload(path: Path, payload: bytes) -> None:
    flags = os.O_RDWR | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise LedgerError(f"acceptance ledger not found: {path}") from exc
    except OSError as exc:
        raise LedgerError(f"cannot open acceptance ledger: {path}: {exc}") from exc
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            raise LedgerError(f"acceptance ledger must be a regular 0600 file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        existing = b"".join(chunks)
        if existing and not existing.endswith(b"\n"):
            raise LedgerError("acceptance ledger ends with an incomplete entry")
        try:
            lines = existing.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise LedgerError(f"cannot decode acceptance ledger as UTF-8: {path}") from exc
        has_seed = False
        for number, line in enumerate(lines, 1):
            entry = _parse_entry(line, number)
            has_seed = has_seed or entry["origin"] == "seed"
        if not lines:
            raise LedgerError("acceptance ledger has zero entries")
        if not has_seed:
            raise LedgerError("acceptance ledger has no seed entry")

        written = os.write(descriptor, payload)
        if written != len(payload):
            raise LedgerError(f"short write to acceptance ledger: {path}")
        os.fsync(descriptor)
    except LedgerError:
        raise
    except OSError as exc:
        raise LedgerError(f"cannot append acceptance ledger: {path}: {exc}") from exc
    finally:
        os.close(descriptor)


__all__ = [
    "LEDGER_ENTRY_KEYS",
    "LEDGER_LINE_COUNT_RELATIVE_PATH",
    "LEDGER_ORIGINS",
    "LedgerError",
    "LedgerState",
    "append_acceptance_sessions",
    "load_ledger",
    "write_line_count",
]
