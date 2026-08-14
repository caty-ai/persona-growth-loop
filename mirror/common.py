"""Private IO, configuration, locking, and injected-byte snapshot helpers."""

import errno
import hashlib
import json
import os
import socket
import stat
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from growthlane.faces import FaceProfile, get_profile


JST = timezone(timedelta(hours=9))
REPO = Path(__file__).resolve().parents[1]
MIRROR_LOCK_STALE_HOURS = 24
MIRROR_LOCK_OWNER = "owner.json"
_GROWTH_COMMON_KEYS = frozenset(
    {
        "display_name",
        "speaker",
        "transcripts_root",
        "writer_argv",
        "reviewer_argv",
        "classifier_argv",
    }
)
_GROWTH_KEYS = {
    "alpha": _GROWTH_COMMON_KEYS,
    "luca": _GROWTH_COMMON_KEYS | {"overlay_home_root", "staging_root"},
}


class MirrorError(RuntimeError):
    pass


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    return flags


def parse_run_date(value: Optional[str]) -> str:
    if value is None:
        return datetime.now(JST).date().isoformat()
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise MirrorError(f"invalid JST date: {value}") from exc
    if parsed > datetime.now(JST).date():
        raise MirrorError(f"future JST date rejected: {value}")
    return parsed.isoformat()


def ensure_private_dir(path: Path) -> Path:
    current = Path(path.anchor) if path.is_absolute() else Path()
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
            raise MirrorError(f"private directory has unsafe shape: {current}")
        if created:
            os.chmod(current, 0o700)
    os.chmod(path, 0o700)
    return path


def read_bytes_nofollow(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise MirrorError(f"not a regular file: {path}")
        chunks: List[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_json_nofollow(path: Path) -> Any:
    try:
        return json.loads(read_bytes_nofollow(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MirrorError(f"invalid JSON {path}: {exc}") from exc


def atomic_write(path: Path, payload: bytes) -> None:
    ensure_private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    directory = os.open(path.parent, _directory_open_flags())
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def atomic_json(path: Path, value: object) -> None:
    atomic_write(
        path,
        (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )


def atomic_monotonic_date_json(
    path: Path,
    value: Mapping[str, object],
    key: str,
    alert: Optional[Callable[[str], None]] = None,
) -> None:
    """Atomically write a dated marker without allowing a valid marker to regress.

    Invalid or unreadable existing markers are deliberately replaceable so a
    damaged liveness marker does not become a permanent wedge.
    """

    incoming_text = value.get(key)
    if not isinstance(incoming_text, str):
        raise MirrorError(f"marker {key} must be an ISO date")
    try:
        incoming = date.fromisoformat(incoming_text)
    except ValueError as exc:
        raise MirrorError(f"marker {key} must be an ISO date") from exc
    replaced_future: Optional[date] = None
    try:
        existing = read_json_nofollow(path)
        if not isinstance(existing, dict):
            raise MirrorError("existing marker is not an object")
        existing_text = existing.get(key)
        if not isinstance(existing_text, str):
            raise MirrorError(f"existing marker has no valid {key}")
        previous = date.fromisoformat(existing_text)
    except (OSError, ValueError, MirrorError):
        previous = None
    if previous is not None and previous > datetime.now(JST).date():
        replaced_future = previous
        previous = None
    if previous is not None and incoming < previous:
        raise MirrorError(
            f"marker {key} regression rejected: {incoming.isoformat()} < {previous.isoformat()}"
        )
    atomic_json(path, dict(value))
    if replaced_future is not None and alert is not None:
        alert(
            "future marker replaced "
            f"{key}={replaced_future.isoformat()} with {incoming.isoformat()}"
        )


def atomic_text(path: Path, value: str) -> None:
    atomic_write(path, value.encode("utf-8"))


def _mirror_lock_owner(lock: Path) -> Tuple[datetime, object, object]:
    owner = lock / MIRROR_LOCK_OWNER
    try:
        metadata = os.lstat(owner)
        if not stat.S_ISREG(metadata.st_mode):
            raise MirrorError("mirror lock owner is not a regular file")
        value = read_json_nofollow(owner)
        if not isinstance(value, dict):
            raise MirrorError("mirror lock owner is not an object")
        started_text = value.get("started_at")
        if not isinstance(started_text, str):
            raise MirrorError("mirror lock owner has no started_at")
        started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
        if started.tzinfo is None:
            raise MirrorError("mirror lock owner started_at has no timezone")
        started = started.astimezone(timezone.utc)
        if started > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise MirrorError("mirror lock owner started_at is in the future")
        return started, value.get("pid", "unknown"), value.get("host", "unknown")
    except (OSError, UnicodeError, ValueError, MirrorError):
        metadata = os.lstat(lock)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise MirrorError("mirror lock has unsafe shape")
        return datetime.fromtimestamp(metadata.st_mtime, timezone.utc), "unknown", "unknown"


def _reclaimable_lock_children(lock: Path) -> Optional[List[Path]]:
    try:
        children = list(lock.iterdir())
        for child in children:
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return None
            if child.name != MIRROR_LOCK_OWNER and not child.name.startswith(
                f".{MIRROR_LOCK_OWNER}.tmp-"
            ):
                return None
        return children
    except OSError:
        return None


def _delete_reclaim_tombstone(tombstone: Path, children: Sequence[Path]) -> None:
    owner = tombstone / MIRROR_LOCK_OWNER
    remaining = list(children)
    if owner in remaining:
        owner.unlink()
        remaining.remove(owner)
    for child in remaining:
        child.unlink()
    tombstone.rmdir()


def _restore_reclaim_tombstone(lock: Path, tombstone: Path) -> bool:
    try:
        os.lstat(lock)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    else:
        return False
    try:
        os.rename(tombstone, lock)
        return True
    except OSError:
        return False


def _manual_review_reclaim_message(tombstone: Path, reason: str) -> str:
    return (
        "reclaim mismatch: live lock preserved as "
        f"{tombstone.name}; manual review reason={reason} path={tombstone}"
    )


def _reclaim_mirror_lock(
    lock: Path, expected_identity: Optional[Tuple[int, int]] = None
) -> Tuple[bool, Optional[str]]:
    try:
        metadata = os.lstat(lock)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return False, None
        identity = (metadata.st_dev, metadata.st_ino)
        if expected_identity is not None and identity != expected_identity:
            return False, None
        if _reclaimable_lock_children(lock) is None:
            return False, None
        tombstone = lock.with_name(
            f"{lock.name}.reclaim-{os.getpid()}-{time.time_ns()}"
        )
        os.rename(lock, tombstone)
        try:
            moved = os.lstat(tombstone)
        except OSError as exc:
            if _restore_reclaim_tombstone(lock, tombstone):
                return False, None
            reason = exc.strerror or exc.__class__.__name__
            return False, _manual_review_reclaim_message(
                tombstone, f"post-rename-lstat-failed:{reason}"
            )
        # The pre-rename lstat window is narrowed to this syscall-scale gap and
        # self-heals by restoring the tombstone on mismatch, but it is not eliminated.
        if (moved.st_dev, moved.st_ino) != identity:
            if _restore_reclaim_tombstone(lock, tombstone):
                return False, None
            return False, _manual_review_reclaim_message(
                tombstone, "identity-mismatch-after-rename"
            )
        children = _reclaimable_lock_children(tombstone)
        if children is None:
            if _restore_reclaim_tombstone(lock, tombstone):
                return False, None
            return False, _manual_review_reclaim_message(
                tombstone, "unexpected-children-after-rename"
            )
        try:
            _delete_reclaim_tombstone(tombstone, children)
        except OSError as exc:
            if _restore_reclaim_tombstone(lock, tombstone):
                return False, None
            reason = exc.strerror or exc.__class__.__name__
            return False, _manual_review_reclaim_message(
                tombstone, f"cleanup-failed-after-rename:{reason}"
            )
        return True, None
    except OSError:
        return False, None


def _sweep_reclaim_tombstones(
    root: Path, lock_name: str, alert: Optional[Callable[[str], None]] = None
) -> None:
    prefix = f"{lock_name}.reclaim-"
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    now_ns = time.time_ns()
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        try:
            metadata = os.lstat(entry)
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            if alert is not None:
                alert(
                    "mirror reclaim sweep left unsafe tombstone "
                    f"path={entry} reason=unsafe-shape"
                )
            continue
        children = _reclaimable_lock_children(entry)
        if children is None:
            if alert is not None:
                alert(
                    "mirror reclaim sweep left unsafe tombstone "
                    f"path={entry} reason=unexpected-children"
                )
            continue
        suffix = entry.name[len(prefix) :]
        pid_text, separator, created_text = suffix.partition("-")
        if (
            not separator
            or not pid_text.isascii()
            or not pid_text.isdigit()
            or not created_text.isascii()
            or not created_text.isdigit()
        ):
            if alert is not None:
                alert(
                    "mirror reclaim sweep left unsafe tombstone "
                    f"path={entry} reason=nonconforming-name"
                )
            continue
        created_ns = int(created_text)
        age_hours = max(0, now_ns - created_ns) / 1_000_000_000 / 3600
        if age_hours <= MIRROR_LOCK_STALE_HOURS:
            continue
        try:
            _delete_reclaim_tombstone(entry, children)
        except OSError as exc:
            if alert is not None:
                reason = exc.strerror or exc.__class__.__name__
                alert(
                    _manual_review_reclaim_message(
                        entry, f"sweep-delete-failed:{reason}"
                    )
                )


@contextmanager
def mirror_lock(
    pgl_home: Path, alert: Optional[Callable[[str], None]] = None
) -> Iterator[bool]:
    root = ensure_private_dir(pgl_home / "mirror")
    lock = root / "lock.d"
    _sweep_reclaim_tombstones(root, lock.name, alert)
    recovered = False
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            try:
                lock_metadata = os.lstat(lock)
                if stat.S_ISLNK(lock_metadata.st_mode) or not stat.S_ISDIR(lock_metadata.st_mode):
                    raise MirrorError("mirror lock has unsafe shape")
                lock_identity = (lock_metadata.st_dev, lock_metadata.st_ino)
                started, pid, host = _mirror_lock_owner(lock)
                age_hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
            except (OSError, MirrorError):
                age_hours = 0.0
                pid = "unknown"
                host = "unknown"
                lock_identity = None
            if (
                not recovered
                and age_hours > MIRROR_LOCK_STALE_HOURS
            ):
                reclaimed, note = _reclaim_mirror_lock(lock, lock_identity)
                if reclaimed:
                    recovered = True
                    if alert is not None:
                        alert(
                            "stale mirror lock recovered "
                            f"age={age_hours:.1f}h pid={pid} host={host}"
                        )
                    continue
                if note is not None and alert is not None:
                    alert(note)
            if alert is not None:
                alert(
                    "mirror lock contention "
                    f"age={age_hours:.1f}h pid={pid} host={host}"
                )
            yield False
            return
    try:
        lock_metadata = os.lstat(lock)
        lock_identity = (lock_metadata.st_dev, lock_metadata.st_ino)
    except OSError as exc:
        try:
            lock.rmdir()
        except OSError:
            pass
        if alert is not None:
            reason = str(exc) or exc.strerror or exc.__class__.__name__
            alert(
                "mirror lock acquisition failed after mkdir; "
                f"post-mkdir lstat unavailable: {reason}"
            )
        yield False
        return
    try:
        atomic_json(
            lock / MIRROR_LOCK_OWNER,
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except BaseException:
        _, note = _reclaim_mirror_lock(lock, lock_identity)
        if note is not None and alert is not None:
            alert(note)
        raise
    try:
        yield True
    finally:
        try:
            (lock / MIRROR_LOCK_OWNER).unlink()
        except OSError:
            pass
        try:
            lock.rmdir()
        except OSError:
            pass


def load_configs(face: str, config_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Any], FaceProfile]:
    profile = get_profile(face)
    growth_path = config_dir / f"growth-{face}.json"
    mirror_path = config_dir / f"mirror-{face}.json"
    growth = read_json_nofollow(growth_path)
    mirror = read_json_nofollow(mirror_path)
    if not isinstance(growth, dict) or not isinstance(mirror, dict):
        raise MirrorError("mirror and growth configs must be JSON objects")
    expected_growth_keys = _GROWTH_KEYS[face]
    if set(growth) != expected_growth_keys:
        raise MirrorError(
            f"{face} growth config keys must be exactly "
            + ", ".join(sorted(expected_growth_keys))
        )
    for key in ("display_name", "speaker"):
        if not isinstance(growth[key], str) or not growth[key].strip():
            raise MirrorError(f"growth {key} must be a non-empty string")
    if not isinstance(growth["transcripts_root"], str):
        raise MirrorError("growth transcripts_root must be a string")
    for key in ("overlay_home_root", "staging_root") if face == "luca" else ():
        if not isinstance(growth[key], str) or not growth[key].strip():
            raise MirrorError(f"growth {key} must be a non-empty string")
    for key in ("writer_argv", "reviewer_argv", "classifier_argv"):
        value = growth[key]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise MirrorError(f"growth {key} must be a string array")
    if growth["writer_argv"] and growth["writer_argv"] == growth["reviewer_argv"]:
        raise MirrorError("growth writer_argv and reviewer_argv must be distinct")
    if set(mirror) != {"responder_argv", "scorer_argv", "vault_dir"}:
        raise MirrorError("mirror config keys must be responder_argv, scorer_argv, vault_dir")
    for key in ("responder_argv", "scorer_argv"):
        value = mirror[key]
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            raise MirrorError(f"{key} must be a string array")
    if mirror["responder_argv"] and mirror["responder_argv"] == mirror["scorer_argv"]:
        raise MirrorError("responder_argv and scorer_argv must be distinct")
    if not isinstance(mirror["vault_dir"], str):
        raise MirrorError("vault_dir must be a string")
    return growth, mirror, profile


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def injected_bytes(
    profile: FaceProfile, pgl_home: Path, config: Mapping[str, object]
) -> Tuple[Dict[str, bytes], Optional[str]]:
    home = profile.resolve_home(pgl_home, config)
    if profile.engine:
        build = home / "build"
        if not build.is_dir() or build.is_symlink():
            return {}, f"build directory absent or unsafe: {build}"
        result: Dict[str, bytes] = {}
        for path in sorted(build.rglob("*")):
            if path.is_symlink():
                raise MirrorError(f"symlinked build artifact rejected: {path}")
            if path.is_file():
                result[path.relative_to(build).as_posix()] = read_bytes_nofollow(path)
        if not result:
            return {}, f"build directory has no artifacts: {build}"
        return result, None
    result = {}
    missing = []
    for rel in profile.render_files.values():
        path = home / rel
        try:
            result[rel] = read_bytes_nofollow(path)
        except FileNotFoundError:
            missing.append(rel)
    if missing:
        return {}, "missing injected artifact(s): {items}".format(items=", ".join(sorted(missing)))
    return result, None


def manifest_for(files: Mapping[str, bytes]) -> Dict[str, object]:
    records = [
        {"path": name, "sha256": sha256_bytes(payload), "size": len(payload)}
        for name, payload in sorted(files.items())
    ]
    return {"files": records, "total_size": sum(item["size"] for item in records)}


def manifest_identity(manifest: Mapping[str, object]) -> str:
    return sha256_bytes(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def list_snapshots(pgl_home: Path, face: str) -> List[Tuple[date, Path, Dict[str, object]]]:
    root = pgl_home / "mirror" / "snapshots" / face
    if not root.is_dir() or root.is_symlink():
        return []
    result = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            day = date.fromisoformat(directory.name)
            value = read_json_nofollow(directory / "manifest.json")
            if not isinstance(value, dict) or set(value) != {"files", "total_size"}:
                continue
        except (ValueError, OSError, MirrorError):
            continue
        result.append((day, directory, value))
    return result


def write_snapshot(
    pgl_home: Path,
    face: str,
    run_date: str,
    files: Mapping[str, bytes],
    *,
    always: bool,
) -> Tuple[Optional[Path], Dict[str, object], bool]:
    if not files:
        raise MirrorError("empty injected-byte snapshot rejected")
    manifest = manifest_for(files)
    snapshots = list_snapshots(pgl_home, face)
    if not always and snapshots and manifest_identity(snapshots[-1][2]) == manifest_identity(manifest):
        return snapshots[-1][1], manifest, False
    directory = ensure_private_dir(pgl_home / "mirror" / "snapshots" / face / run_date)
    file_root = ensure_private_dir(directory / "files")
    for name, payload in sorted(files.items()):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise MirrorError(f"unsafe injected artifact name: {name}")
        atomic_write(file_root / relative, payload)
    atomic_json(directory / "manifest.json", manifest)
    return directory, manifest, True


def snapshot_files(directory: Path) -> Dict[str, bytes]:
    value = read_json_nofollow(directory / "manifest.json")
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        raise MirrorError(f"invalid snapshot manifest: {directory}")
    result = {}
    for item in value["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise MirrorError(f"invalid snapshot entry: {directory}")
        name = item["path"]
        path = directory / "files" / name
        payload = read_bytes_nofollow(path)
        if sha256_bytes(payload) != item.get("sha256") or len(payload) != item.get("size"):
            raise MirrorError(f"snapshot byte mismatch: {path}")
        result[name] = payload
    return result


def nearest_snapshot(
    snapshots: Sequence[Tuple[date, Path, Dict[str, object]]], target: date
) -> Optional[Tuple[date, Path, Dict[str, object]]]:
    eligible = [item for item in snapshots if item[0] <= target]
    return eligible[-1] if eligible else None


def safe_marker_create(path: Path, payload: bytes) -> bool:
    """Atomically create a durable proposal without replacing an existing one.

    O_EXCL is the preserve-existing equivalent of the tripwire temp/replace
    pattern: the final name itself is created atomically, fsync'd, and the
    containing directory is then fsync'd. There is no overwrite race.
    """
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    directory = os.open(path.parent, _directory_open_flags())
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True
