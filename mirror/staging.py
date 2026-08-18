"""Regenerate the Luca staging install from its dedicated overlay clone."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import yaml

from growthlane.faces import get_profile
from growthlane.locking import (
    OWNER_FILE,
    acquire_staging_lock,
    release_lock,
    staging_contention_detail,
    staging_lock_path,
)
from growthlane.persona_cli import PersonaCliError, persona_argv

from .common import MirrorError, atomic_write, ensure_private_dir, read_bytes_nofollow


_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_PACK_DECLARATION = re.compile(r"^pack\s*:", re.MULTILINE)
CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class StagingBuild:
    root: Path
    content_hash: str


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise MirrorError(f"duplicate install declaration: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _configured_root(config: Mapping[str, object], key: str) -> Path:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MirrorError(f"luca config requires non-empty {key}")
    return Path(value).expanduser().absolute()


def _regular_directory(path: Path, label: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError as exc:
        raise MirrorError(f"{label} missing: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise MirrorError(f"{label} has unsafe shape: {path}")


def _run_command(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MirrorError(f"command unavailable: {command[0]}: {exc}") from exc


def _completed_json(
    completed: subprocess.CompletedProcess[str], label: str
) -> Mapping[str, object]:
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise MirrorError(f"{label} failed with exit {completed.returncode}: {detail}")
    try:
        value = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MirrorError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise MirrorError(f"{label} JSON must be an object")
    return value


def _expected_placeholders(config: Mapping[str, object]) -> dict[str, str]:
    owner_display = config.get("display_name")
    if not isinstance(owner_display, str) or not owner_display.strip():
        raise MirrorError("luca config requires non-empty display_name")
    return {
        "agent-name": get_profile("luca").persona_name,
        "user": owner_display,
        "owner-name": owner_display,
    }


def _persona_argv(clone: Path, command: str, staging_root: Path) -> tuple[str, ...]:
    try:
        return persona_argv(clone, command, staging_root)
    except PersonaCliError as exc:
        raise MirrorError(f"Luca staging {command} cannot launch: {exc}") from exc


def _load_install(
    path: Path, expected_placeholders: Mapping[str, str]
) -> tuple[str, Mapping[str, object]]:
    try:
        text = read_bytes_nofollow(path).decode("utf-8")
        value = yaml.load(text, Loader=_UniqueSafeLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise MirrorError(f"invalid Luca install template {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MirrorError("Luca install template must be a mapping")
    placeholders = value.get("placeholders")
    if placeholders != expected_placeholders:
        expected = ", ".join(
            f"{key}={value}" for key, value in expected_placeholders.items()
        )
        raise MirrorError(
            "Luca install placeholder declarations must exactly match "
            + expected
        )
    declarations = list(_PACK_DECLARATION.finditer(text))
    if len(declarations) != 1 or not isinstance(value.get("pack"), str):
        raise MirrorError("Luca install template must declare pack exactly once")
    return text, value


def _adapt_install(
    template: Path, expected_placeholders: Mapping[str, str]
) -> bytes:
    text, _ = _load_install(template, expected_placeholders)
    lines = text.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if re.match(r"^pack\s*:", line)]
    if len(matches) != 1:
        raise MirrorError("Luca install template must declare a root-level pack")
    index = matches[0]
    ending = "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"pack: pack{ending}"
    payload = "".join(lines).encode("utf-8")
    try:
        adapted = yaml.load(payload.decode("utf-8"), Loader=_UniqueSafeLoader)
    except yaml.YAMLError as exc:
        raise MirrorError(f"adapted Luca install is invalid: {exc}") from exc
    if not isinstance(adapted, dict) or adapted.get("pack") != "pack":
        raise MirrorError("adapted Luca install must declare pack: pack")
    if adapted.get("placeholders") != expected_placeholders:
        raise MirrorError("adapted Luca install changed placeholder declarations")
    return payload


def _reject_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise MirrorError(f"symlinked Luca pack entry rejected: {path}")


def _replace_pack(source: Path, staging_root: Path) -> None:
    _regular_directory(source, "Luca persona-engine source")
    _reject_symlinks(source)
    ensure_private_dir(staging_root)
    _regular_directory(staging_root, "Luca staging root")
    temporary = Path(tempfile.mkdtemp(prefix=".pack-sync-", dir=staging_root))
    destination = staging_root / "pack"
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True, symlinks=False)
        if destination.exists() or destination.is_symlink():
            mode = os.lstat(destination).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise MirrorError(f"Luca staging pack has unsafe shape: {destination}")
            shutil.rmtree(destination)
        os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def regenerate_staging(
    config: Mapping[str, object],
    *,
    command_runner: CommandRunner = _run_command,
    held_lock: Path | None = None,
) -> StagingBuild:
    """Pull the Luca clone and completely rebuild its git-external staging install."""

    clone = _configured_root(config, "overlay_home_root")
    staging_root = _configured_root(config, "staging_root")
    expected_placeholders = _expected_placeholders(config)
    owns_lock = held_lock is None
    if owns_lock:
        try:
            lock = acquire_staging_lock(staging_root, "luca")
        except (OSError, ValueError) as exc:
            detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            raise MirrorError(f"Luca staging lock acquisition failed: {detail}") from exc
        if lock is None:
            raise MirrorError(
                staging_contention_detail(staging_root, "luca", "weekly staging")
            )
    else:
        try:
            expected_lock = staging_lock_path(staging_root, "luca")
            lock = Path(held_lock)
            lock_metadata = os.lstat(lock)
            owner_metadata = os.lstat(lock / OWNER_FILE)
        except (OSError, ValueError) as exc:
            detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            raise MirrorError(f"Luca caller-held staging lock is invalid: {detail}") from exc
        if (
            lock != expected_lock
            or stat.S_ISLNK(lock_metadata.st_mode)
            or not stat.S_ISDIR(lock_metadata.st_mode)
            or stat.S_ISLNK(owner_metadata.st_mode)
            or not stat.S_ISREG(owner_metadata.st_mode)
        ):
            raise MirrorError("Luca caller-held staging lock is absent or invalid")
    failure: BaseException | None = None
    try:
        _regular_directory(clone, "Luca overlay clone")
        clone_resolved = clone.resolve(strict=False)
        staging_resolved = staging_root.resolve(strict=False)
        if (
            staging_resolved == clone_resolved
            or clone_resolved in staging_resolved.parents
            or staging_resolved in clone_resolved.parents
        ):
            raise MirrorError("Luca staging_root must be outside overlay_home_root")

        pull = command_runner(("git", "pull", "--ff-only"), clone)
        if pull.returncode != 0:
            detail = (pull.stderr or pull.stdout or "").strip()
            raise MirrorError(f"Luca clone git pull --ff-only failed: {detail}")

        _replace_pack(clone / "persona-engine", staging_root)
        atomic_write(
            staging_root / "install.yml",
            _adapt_install(
                clone / "tests" / "luca-pack" / "install.yml",
                expected_placeholders,
            ),
        )

        build = staging_root / "build"
        if build.exists() or build.is_symlink():
            mode = os.lstat(build).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise MirrorError(f"Luca staging build has unsafe shape: {build}")
            shutil.rmtree(build)

        build_result = _completed_json(
            command_runner(_persona_argv(clone, "build", staging_root), clone),
            "Luca staging build",
        )
        if build_result.get("ok") is not True:
            raise MirrorError("Luca staging build JSON did not report ok")
        manifest = build_result.get("manifest")
        content_hash = manifest.get("content_hash") if isinstance(manifest, dict) else None
        if not isinstance(content_hash, str) or _CONTENT_HASH.fullmatch(content_hash) is None:
            raise MirrorError("Luca staging build omitted a valid content_hash")

        doctor_result = _completed_json(
            command_runner(_persona_argv(clone, "doctor", staging_root), clone),
            "Luca staging doctor",
        )
        if doctor_result.get("ok") is not True:
            raise MirrorError("Luca staging doctor JSON did not report ok")
        _regular_directory(build, "Luca staging build directory")
        return StagingBuild(root=staging_root, content_hash=content_hash)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if owns_lock:
            released = release_lock(lock)
            if not released:
                message = "Luca staging lock directory could not be removed"
                if failure is None:
                    raise MirrorError(message)
                raise MirrorError(message) from failure
