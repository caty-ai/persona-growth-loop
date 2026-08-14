"""Single-writer lock ownership metadata and stale-lock visibility.

Luca's staging root is shared by two independent launchd lanes: the weekly
mirror on Tuesday 01:30 JST and the nightly acceptance path at 04:00 JST. The
schedule should keep them disjoint, but a shared staging lock still enforces
that assumption at runtime when manual runs or scheduling drift overlap.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import stat
from datetime import datetime, timezone
from pathlib import Path


OWNER_FILE = "owner.json"
STALE_HOURS = 24


def _validated_face(face: str) -> str:
    if not face or face in {".", ".."} or "/" in face or "\\" in face:
        raise ValueError("lock face must not contain path components")
    return face


def lock_path(pgl_home: Path, face: str) -> Path:
    face = _validated_face(face)
    return pgl_home / f"lock-{face}.d"


def staging_lock_path(staging_root: Path, face: str) -> Path:
    face = _validated_face(face)
    root = Path(staging_root).expanduser().resolve(strict=False)
    if not root.name or root.name in {".", ".."}:
        raise ValueError("staging_root must have a final path component")
    return root.parent / f".{root.name}.lock-{face}.d"


def _write_owner(lock: Path) -> Path:
    os.chmod(lock, 0o700)
    owner = lock / OWNER_FILE
    payload = (
        json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(owner, flags, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        try:
            owner.unlink()
        except FileNotFoundError:
            pass
        try:
            lock.rmdir()
        except OSError:
            pass
        raise
    return lock


def _acquire_path_lock(lock: Path, *, retry_missing_once: bool = True) -> Path | None:
    try:
        lock.mkdir()
    except FileExistsError:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            descriptor = os.open(lock, flags)
        except FileNotFoundError:
            if retry_missing_once:
                return _acquire_path_lock(lock, retry_missing_once=False)
            raise
        except OSError as exc:
            raise OSError(f"lock has unsafe shape: {lock}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError(f"lock has unsafe shape: {lock}")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
        return None
    return _write_owner(lock)


def acquire_lock(pgl_home: Path, face: str) -> Path | None:
    return _acquire_path_lock(lock_path(pgl_home, face))


def acquire_staging_lock(staging_root: Path, face: str) -> Path | None:
    return _acquire_path_lock(staging_lock_path(staging_root, face))


def release_lock(lock: Path) -> bool:
    try:
        (lock / OWNER_FILE).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        lock.rmdir()
    except OSError:
        return False
    return True


def _contention_detail(lock: Path, prefix: str) -> str:
    owner_path = lock / OWNER_FILE
    pid: object = "unknown"
    host: object = "unknown"
    started_text: object = "unknown"
    started: datetime | None = None
    owner_exists = False
    try:
        metadata = os.lstat(owner_path)
        owner_exists = True
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("non-regular owner metadata")
        value = json.loads(owner_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            pid = value.get("pid", pid)
            host = value.get("host", host)
            started_text = value.get("started_at", started_text)
            if isinstance(started_text, str):
                parsed = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    started = parsed.astimezone(timezone.utc)
                    if started > datetime.now(timezone.utc):
                        raise ValueError("future owner metadata")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        try:
            started = datetime.fromtimestamp(lock.stat().st_mtime, timezone.utc)
            started_text = started.isoformat()
        except OSError:
            pass
    age_hours = (
        (datetime.now(timezone.utc) - started).total_seconds() / 3600
        if started is not None
        else 0.0
    )
    if age_hours <= STALE_HOURS:
        return f"{prefix}: skipped: lock contention"
    clear = f"rmdir -- {shlex.quote(str(lock))}"
    if owner_exists:
        clear = f"rm -- {shlex.quote(str(owner_path))} && {clear}"
    return (
        f"{prefix}: stale lock contention age={age_hours:.1f}h "
        f"pid={pid} host={host} started_at={started_text}; "
        f"after verifying the owner is dead, clear manually: {clear}"
    )


def contention_message(pgl_home: Path, face: str, prefix: str) -> str:
    face = _validated_face(face)
    return f"[RED] {face}: {_contention_detail(lock_path(pgl_home, face), prefix)}"


def staging_contention_detail(staging_root: Path, face: str, prefix: str) -> str:
    # Deliberately unprefixed: the emitting layer applies the status tag.
    _validated_face(face)
    return _contention_detail(staging_lock_path(staging_root, face), prefix)
