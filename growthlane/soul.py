"""Read-only soul-baseline verification and the explicit baseline writer."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from .faces import FaceProfile


class SoulError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SoulError(f"cannot hash soul file: {path}") from exc
    return digest.hexdigest()


def overlay_home_pin(profile: FaceProfile, pgl_home: Path) -> Path:
    return pgl_home.expanduser().resolve() / "soul-baseline" / f"{profile.name}.overlay-home"


def read_overlay_home_pin(profile: FaceProfile, pgl_home: Path) -> str | None:
    path = overlay_home_pin(profile, pgl_home)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SoulError(f"invalid overlay-home pin for {profile.name}") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"overlay_home"}
        or not isinstance(value.get("overlay_home"), str)
        or not value["overlay_home"]
    ):
        raise SoulError(f"invalid overlay-home pin for {profile.name}")
    return value["overlay_home"]


def write_overlay_home_pin(profile: FaceProfile, pgl_home: Path, overlay_home: str) -> Path:
    path = overlay_home_pin(profile, pgl_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = (
        json.dumps({"overlay_home": overlay_home}, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return path


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _validate_soul_path(
    profile: FaceProfile,
    pgl_home: Path,
    config: Mapping[str, object],
    resolved: Path,
) -> None:
    overlay_home = profile.resolve_home(pgl_home, config)
    roots = profile.resolved_soul_roots(config)
    if not any(_inside(resolved, root) for root in roots):
        raise SoulError(f"baseline path is outside declared soul roots: {resolved}")
    # The mutable overlay planes are never valid soul anchors. Luca's declared
    # pack roots are repo-relative, so reject their overlay/growth subtrees
    # explicitly while allowing the code-owned pack soul locations.
    for rel in profile.allowlist:
        target = (overlay_home / rel).resolve()
        if resolved == target:
            raise SoulError(f"baseline path is inside overlay home: {resolved}")
    for rel in profile.expected_dirs:
        mutable_root = (overlay_home / rel).resolve()
        if _inside(resolved, mutable_root):
            raise SoulError(f"baseline path is inside overlay home: {resolved}")


def verify_manifest(
    profile: FaceProfile, pgl_home: Path, config: Mapping[str, object]
) -> list[dict[str, str]]:
    path = profile.baseline_manifest(pgl_home)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SoulError(f"missing or invalid soul baseline for {profile.name}") from exc
    if not isinstance(value, dict) or set(value) != {"overlay_home", "files"}:
        raise SoulError(
            f"soul baseline lacks overlay-home pin for {profile.name}; "
            "re-run bin/pgl-baseline"
        )
    recorded_home = value["overlay_home"]
    expected_home = str(profile.resolve_home(pgl_home, config))
    if not isinstance(recorded_home, str) or recorded_home != expected_home:
        raise SoulError(
            f"overlay home mismatch for {profile.name}: recorded={recorded_home!r} "
            f"resolved={expected_home!r}; re-run bin/pgl-baseline only after verifying the intended root"
        )
    files = value["files"]
    if not isinstance(files, list) or not files:
        raise SoulError(f"empty soul baseline for {profile.name}")
    result: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise SoulError(f"invalid soul baseline entry for {profile.name}")
        raw_path, expected = item["path"], item["sha256"]
        if not isinstance(raw_path, str) or not isinstance(expected, str):
            raise SoulError(f"invalid soul baseline entry for {profile.name}")
        resolved = Path(raw_path).expanduser().resolve()
        _validate_soul_path(profile, pgl_home, config, resolved)
        if sha256_file(resolved) != expected:
            raise SoulError(f"soul hash mismatch: {resolved}")
        result.append({"path": str(resolved), "sha256": expected})
    return result


def write_manifest(
    profile: FaceProfile,
    pgl_home: Path,
    config: Mapping[str, object],
    files: list[Path],
) -> Path:
    overlay_home = profile.resolve_home(pgl_home, config)
    if not (overlay_home / ".git").exists():
        raise SoulError(f"overlay home is not a git repository: {overlay_home}")
    missing = [rel for rel in profile.expected_dirs if not (overlay_home / rel).is_dir()]
    if missing:
        raise SoulError(f"overlay home structure missing for {profile.name}: {','.join(missing)}")
    records: list[dict[str, str]] = []
    for supplied in files:
        resolved = supplied.expanduser().resolve()
        _validate_soul_path(profile, pgl_home, config, resolved)
        if not resolved.is_file():
            raise SoulError(f"baseline path is not a file: {resolved}")
        records.append({"path": str(resolved), "sha256": sha256_file(resolved)})
    if not records:
        raise SoulError("at least one soul file is required")
    records.sort(key=lambda item: item["path"])
    path = profile.baseline_manifest(pgl_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    manifest = {"overlay_home": str(overlay_home), "files": records}
    payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return path
