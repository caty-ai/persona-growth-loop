"""Private Tier L storage helpers for Hermes Luca."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from datetime import date as Date
from pathlib import Path
from typing import Any, Iterable, Mapping


_FACE = "luca"
_MARKER_SCHEMA_VERSION = 1
_OBS_FILE = re.compile(r"(?:usage-)?(\d{4}-\d{2}-\d{2})\.jsonl\Z")


def write_jsonl(path: str | Path, records: Iterable[Mapping[str, Any]]) -> None:
    """Write scrubbed JSONL records directly to the final path."""

    target = Path(path).expanduser()
    _write_bytes(target, _jsonl_bytes(records))


def remove_file(path: str | Path) -> bool:
    """Remove a regular file or symlink without following links."""

    target = Path(path).expanduser()
    try:
        _require_safe_directory(target.parent)
    except FileNotFoundError:
        return False
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        target.unlink()
        return True
    raise OSError(f"unsafe file shape: {target}")


def write_marker(
    obs_root: str | Path,
    *,
    run_at: str,
    run_date: str | Date,
    sources_scanned: int,
    records_written: int,
    errors: int,
    usage_enabled: bool,
    ucd: str,
) -> None:
    """Write the Luca collector liveness marker as private JSON.

    The key set here must match `mirror/weekly.py`'s
    `_COLLECTOR_LAST_RUN_KEYS` exactly -- that frozenset is the shared
    QUIET/BROKEN evidence contract also written by the alpha-face collector
    (`collectors/claude_code/collector.py::_write_last_run_marker`).
    Luca-specific counts (pruned files, outsider sessions, usage records)
    are intentionally NOT written here; they stay observable through
    `collect()`'s return value instead of leaking a face-only key into this
    shared schema.
    """

    iso_date = _iso_date(run_date)
    marker = Path(obs_root).expanduser() / "state" / "collector" / f"{_FACE}.last-run.json"
    payload = {
        "schema_version": _MARKER_SCHEMA_VERSION,
        "face": _FACE,
        "run_at": run_at,
        "date": iso_date,
        "sources_scanned": sources_scanned,
        "records_written": records_written,
        "usage_enabled": usage_enabled,
        "errors": errors,
        "ucd": ucd,
    }
    _write_bytes(marker, _json_bytes(payload, sort_keys=True))


def write_atomic_json(path: str | Path, value: Mapping[str, Any]) -> None:
    """Atomically replace a private JSON file and fsync file plus directory."""

    target = Path(path).expanduser()
    _ensure_private_dir(target.parent)
    directory_fd = os.open(target.parent, _directory_open_flags())
    temporary_path: Path | None = None
    try:
        _reject_existing_non_regular(target, directory_fd)
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary_path = Path(raw_temporary_path)
        try:
            payload = _json_bytes(value, sort_keys=True)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(f"short write: {temporary_path}")
                view = view[written:]
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary_path, target)
        temporary_path = None
        os.fsync(directory_fd)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def prune_luca(obs_root: str | Path, run_date: str | Date) -> list[Path]:
    """Delete Luca observation files older than 30 days."""

    anchor = Date.fromisoformat(_iso_date(run_date))
    face_dir = Path(obs_root).expanduser() / "obslog" / _FACE
    try:
        _require_existing_directory(face_dir)
    except FileNotFoundError:
        return []

    removed: list[Path] = []
    for path in sorted(face_dir.glob("*.jsonl")):
        match = _OBS_FILE.fullmatch(path.name)
        if match is None:
            continue
        try:
            bucket = Date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if (anchor - bucket).days > 30 and remove_file(path):
            removed.append(path)
    return removed


def _jsonl_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    payload = bytearray()
    for record in records:
        payload.extend(_json_text(record).encode("utf-8"))
        payload.extend(b"\n")
    return bytes(payload)


def _json_bytes(value: object, *, sort_keys: bool) -> bytes:
    return (_json_text(value, sort_keys=sort_keys) + "\n").encode("utf-8")


def _json_text(value: object, *, sort_keys: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys)


def _write_bytes(path: Path, payload: bytes) -> None:
    _ensure_private_dir(path.parent)
    directory_fd = os.open(path.parent, _directory_open_flags())
    try:
        _reject_existing_non_regular(path, directory_fd)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(f"unsafe file shape: {path}")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(f"short write: {path}")
                view = view[written:]
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _reject_existing_non_regular(path: Path, directory_fd: int) -> None:
    try:
        mode = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISREG(mode):
        return
    raise OSError(f"unsafe file shape: {path}")


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    return flags


def _ensure_private_dir(path: Path) -> Path:
    current = Path(path.anchor) if path.is_absolute() else Path()
    if path == Path("."):
        return path
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        created = False
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            created = True
            mode = os.lstat(current).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise OSError(f"private directory has unsafe shape: {current}")
        if created:
            os.chmod(current, 0o700)
    os.chmod(path, 0o700)
    return path


def _require_safe_directory(path: Path) -> Path:
    current = Path(path.anchor) if path.is_absolute() else Path()
    if path == Path("."):
        return path
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        mode = os.lstat(current).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise OSError(f"private directory has unsafe shape: {current}")
    return path


def _require_existing_directory(path: Path) -> Path:
    try:
        return _require_safe_directory(path)
    except FileNotFoundError:
        raise


def _iso_date(value: str | Date) -> str:
    return value.isoformat() if isinstance(value, Date) else Date.fromisoformat(value).isoformat()


__all__ = [
    "prune_luca",
    "remove_file",
    "write_atomic_json",
    "write_jsonl",
    "write_marker",
]
