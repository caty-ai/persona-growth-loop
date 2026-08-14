"""Atomic, path-scoped overlay transaction and nightly face pipeline."""

from __future__ import annotations

import copy
import difflib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml
from aggregator.aggregate import AggregateError, aggregate, apply_transition
from growthlane.faces import FaceProfile
from growthlane.gates import DeletionContext, GateAudit, GateError, check_all
from growthlane.guard import lint_phrase
from growthlane.holdout import proposal_id as compute_proposal_id
from growthlane.ledger import dump_ledger, load_ledger, new_phrase, validate_ledger
from growthlane.locking import (
    acquire_staging_lock,
    release_lock,
    staging_contention_detail,
)
from growthlane.notify import Digest, send_soul_alert
from growthlane.render import ADOPTED_TEMPLATE, CANDIDATES_TEMPLATE, render_files
from growthlane.soul import SoulError, verify_manifest
from growthlane.ucd_runtime import runtime_status
from harvester.harvest import (
    TranscriptUnavailable,
    append_blocked_text,
    matching_bucket,
    harvest,
    normalize_blocklist,
    transcript_inputs,
)
from reviewd.diff_review import review
from writerd.propose import AdapterError, propose


ADOPTED_CAP_BYTES = 1800
ADOPTED_CAP_ENTRIES = 40
CANDIDATE_CAP_BYTES = 720
CANDIDATE_CAP_ENTRIES = 12


class ApplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class BlocklistState:
    entries: list[str] | None
    payload: bytes | None
    managed: bool = True


def _canonical_blocklist_payload(entries: list[str]) -> bytes:
    return (("\n".join(sorted(set(entries))) + "\n").encode("utf-8") if entries else b"")


def _coerce_blocklist_state(
    blocklist: list[str] | BlocklistState | None,
) -> BlocklistState:
    if isinstance(blocklist, BlocklistState):
        return blocklist
    if blocklist is None:
        return BlocklistState(None, None)
    return BlocklistState(list(blocklist), _canonical_blocklist_payload(list(blocklist)))


def _normalized_blocklist_entries_from_payload(payload: bytes | None) -> list[str]:
    if payload is None:
        return []
    decoded_lines: list[str] = []
    for raw_line in payload.splitlines():
        try:
            decoded_lines.append(raw_line.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return normalize_blocklist(decoded_lines)


def read_blocklist_state(path: Path) -> BlocklistState:
    entries = normalize_blocklist(path.read_text(encoding="utf-8").splitlines())
    return BlocklistState(entries, _canonical_blocklist_payload(entries))


def append_blocklist_state_preserving_invalid_bytes(path: Path, text: str) -> BlocklistState:
    payload = path.read_bytes()
    entries = _normalized_blocklist_entries_from_payload(payload)
    stored = list(entries)
    existing_views = {
        view
        for existing in entries
        for view in matching_bucket(existing)
    }
    append_blocked_text(stored, text)
    appended = payload
    stored_text = next(
        (
            line
            for line in stored
            if matching_bucket(line).isdisjoint(existing_views)
        ),
        None,
    )
    if stored_text is not None:
        if appended and not appended.endswith(b"\n"):
            appended += b"\n"
        appended += (stored_text + "\n").encode("utf-8")
    return BlocklistState(stored, appended)


def _verify_manifest_or_red(
    profile: FaceProfile,
    pgl_home: Path,
    config: Mapping[str, object],
    digest: Digest,
) -> None:
    try:
        verify_manifest(profile, pgl_home, config)
    except SoulError as exc:
        digest.emit(f"[RED] {profile.name}: soul/root baseline check failed: {exc}")
        send_soul_alert(config, profile.name, exc, digest)
        raise
    except Exception as exc:
        digest.emit(f"[RED] {profile.name}: soul/root baseline check failed: {exc}")
        raise


def _resolved_allowlist(profile: FaceProfile, overlay_home: Path) -> dict[str, Path]:
    home = overlay_home.resolve()
    result: dict[str, Path] = {}
    for rel_path in profile.allowlist:
        candidate = home / rel_path
        cursor = candidate.parent
        while cursor != home:
            if cursor.is_symlink():
                raise ApplyError(f"symlinked allowlist parent rejected: {cursor}")
            if home not in cursor.parents and cursor != home:
                raise ApplyError(f"allowlist escaped overlay home: {rel_path}")
            cursor = cursor.parent
        result[rel_path] = candidate.resolve(strict=False)
    return result


def _guarded_target(profile: FaceProfile, overlay_home: Path, rel_path: str) -> Path:
    if not isinstance(rel_path, str) or rel_path not in profile.allowlist:
        raise ApplyError(f"path outside code allowlist: {rel_path!r}")
    lexical = overlay_home.resolve() / rel_path
    if lexical.is_symlink():
        raise ApplyError(f"symlinked allowlist target rejected: {rel_path}")
    _resolved_allowlist(profile, overlay_home)
    return lexical


def _open_final_nofollow(target: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ApplyError(f"symlinked allowlist target rejected: {target.name}") from exc
        raise ApplyError(f"allowlist target cannot be opened safely: {target.name}") from exc
    else:
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ApplyError(f"non-regular allowlist target rejected: {target.name}")
        finally:
            os.close(descriptor)


def write_guarded(profile: FaceProfile, overlay_home: Path, rel_path: str, payload: bytes) -> None:
    """The single application write boundary for every overlay file."""

    if not isinstance(payload, bytes):
        raise ApplyError("guarded writes require bytes")
    target = _guarded_target(profile, overlay_home, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise ApplyError(f"symlinked target parent rejected: {target.parent}")
    temporary = target.with_name(f".{target.name}.pgl-tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        _open_final_nofollow(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, target)
    os.chmod(target, 0o600)


def _git(home: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryDirectory(prefix="pgl-empty-hooks-") as hooks:
        completed = subprocess.run(
            ["git", "-c", f"core.hooksPath={hooks}", *args],
            cwd=home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    if check and completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise ApplyError(f"git {' '.join(args)} failed: {message}")
    return completed


def _require_overlay_home(profile: FaceProfile, home: Path) -> None:
    if not (home / ".git").exists():
        raise ApplyError(f"overlay home is not a git repository: {home}")
    missing = [rel for rel in profile.expected_dirs if not (home / rel).is_dir()]
    if missing:
        raise ApplyError(f"overlay home structure missing for {profile.name}: {','.join(missing)}")


def _require_clean_allowlist(profile: FaceProfile, home: Path) -> None:
    output = _git(home, "status", "--porcelain", "--", *profile.allowlist).stdout
    if output.strip():
        raise ApplyError("dirty overlay allowlist paths")


def _snapshot_files(
    profile: FaceProfile,
    home: Path,
    rel_paths: tuple[str, ...] | list[str] | None = None,
) -> dict[str, bytes | None]:
    originals: dict[str, bytes | None] = {}
    for rel in rel_paths if rel_paths is not None else profile.allowlist:
        target = _guarded_target(profile, home, rel)
        if not target.is_file():
            raise ApplyError(f"required bootstrap allowlist file missing: {rel}")
        originals[rel] = target.read_bytes()
    return originals


def _restore_files(profile: FaceProfile, home: Path, originals: Mapping[str, bytes | None]) -> None:
    _git(home, "checkout", "--", *profile.allowlist, check=False)
    for rel, payload in originals.items():
        target = _guarded_target(profile, home, rel)
        if payload is None:
            if target.exists() or target.is_symlink():
                target.unlink()
        elif not target.is_file() or target.read_bytes() != payload:
            write_guarded(profile, home, rel, payload)


def _lint_render_ledger(
    ledger: Mapping[str, object], *, skip: bool = False
) -> tuple[dict[str, Any], list[tuple[str, list[str]]]]:
    render_ledger = copy.deepcopy(dict(ledger))
    phrases = render_ledger["phrases"]
    adopted = [item for item in phrases if item["state"] == "adopted"]
    staged = [item for item in phrases if item["state"] == "staged"]
    dropped: list[tuple[str, list[str]]] = []
    if not skip:
        for item in adopted + staged:
            violations = lint_phrase(item["text"])
            if violations:
                dropped.append((item["id"], violations))
        dropped_ids = {item[0] for item in dropped}
        render_ledger["phrases"] = [item for item in phrases if item["id"] not in dropped_ids]
    return render_ledger, dropped


def _demote_lint_drops(
    ledger: dict[str, Any], dropped: list[tuple[str, list[str]]], run_date: str
) -> None:
    by_id = {item["id"]: item for item in ledger["phrases"]}
    for phrase_id, violations in dropped:
        phrase = by_id[phrase_id]
        previous = phrase["state"]
        phrase["state"] = "demoted"
        phrase["history"].append(
            {
                "at": run_date,
                "from": previous,
                "to": "demoted",
                "by": "lint_guard",
                "proposal_id": f"lint-rules:{','.join(violations)}",
            }
        )


def _render_caps(
    profile: FaceProfile,
    ledger: Mapping[str, object],
    run_date: str,
    user: str,
    *,
    candidate_render_ledger: Mapping[str, object] | None = None,
) -> None:
    phrases = ledger["phrases"]
    adopted = [item for item in phrases if item["state"] == "adopted"]
    staged = [item for item in phrases if item["state"] == "staged"]
    if len(adopted) > ADOPTED_CAP_ENTRIES:
        raise ApplyError("adopted entry cap exceeded")
    if len(staged) > CANDIDATE_CAP_ENTRIES:
        raise ApplyError("candidate entry cap exceeded")
    adopted_bytes = (
        len((ADOPTED_TEMPLATE.format(user=user, phrases="、".join(item["text"] for item in adopted)) + "\n").encode("utf-8"))
        if adopted else 0
    )
    rendered = render_files(
        profile,
        candidate_render_ledger if candidate_render_ledger is not None else ledger,
        run_date,
        user,
    )
    if profile.engine:
        candidate_bytes = len(rendered[profile.render_files["candidates"]])
    else:
        payload = rendered[profile.render_files["combined"]].decode("utf-8")
        candidate_bytes = sum(len((line + "\n").encode("utf-8")) for line in payload.splitlines() if line.startswith("試用中"))
    if adopted_bytes > ADOPTED_CAP_BYTES:
        raise ApplyError("adopted byte cap exceeded")
    if candidate_bytes > CANDIDATE_CAP_BYTES:
        raise ApplyError("candidate byte cap exceeded")


def _content_hash(profile: FaceProfile, rendered: Mapping[str, bytes]) -> str:
    if not profile.engine and len(rendered) == 1:
        return hashlib.sha256(next(iter(rendered.values()))).hexdigest()
    digest = hashlib.sha256()
    for path in sorted(rendered):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(rendered[path])
    return digest.hexdigest()


def _regular_directory(path: Path, label: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError as exc:
        raise ApplyError(f"{label} missing: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ApplyError(f"{label} has unsafe shape: {path}")


def _regular_file(path: Path, label: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError as exc:
        raise ApplyError(f"{label} missing: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ApplyError(f"{label} has unsafe shape: {path}")


def _reject_tree_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ApplyError(f"symlinked engine source entry rejected: {path}")


def _replace_staging_pack(source: Path, staging_root: Path) -> None:
    _regular_directory(source, "engine source")
    _reject_tree_symlinks(source)
    _regular_directory(staging_root, "engine staging root")
    destination = staging_root / "pack"
    if destination.exists() or destination.is_symlink():
        mode = os.lstat(destination).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ApplyError(f"engine staging pack has unsafe shape: {destination}")

    temporary = Path(tempfile.mkdtemp(prefix=".pack-sync-", dir=staging_root))
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True, symlinks=False)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _sweep_pack_sync_temps(staging_root: Path) -> None:
    for entry in staging_root.iterdir():
        if not entry.name.startswith(".pack-sync-"):
            continue
        mode = os.lstat(entry).st_mode
        if stat.S_ISDIR(mode):
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _validate_engine_staging(
    profile: FaceProfile,
    home: Path,
    staging_root: Path,
) -> Path | None:
    if not profile.engine:
        return None
    _regular_directory(staging_root, "engine staging root")
    home_resolved = home.resolve()
    staging_resolved = staging_root.resolve()
    if (
        staging_resolved == home_resolved
        or home_resolved in staging_resolved.parents
        or staging_resolved in home_resolved.parents
    ):
        raise ApplyError("engine staging_root must be outside overlay_home_root")

    source = home / "persona-engine"
    _regular_directory(source, "engine source")
    _reject_tree_symlinks(source)
    for name in ("pack", "build"):
        destination = staging_root / name
        if destination.exists() or destination.is_symlink():
            mode = os.lstat(destination).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ApplyError(f"engine staging {name} has unsafe shape: {destination}")
    return staging_root


def _configured_engine_staging_root(
    profile: FaceProfile,
    config: Mapping[str, object],
) -> Path | None:
    if not profile.engine:
        return None
    try:
        staging_root = profile.resolve_staging_root(config)
    except ValueError as exc:
        raise ApplyError(str(exc)) from exc
    if staging_root is None:
        raise ApplyError(f"{profile.name} engine profile has no staging root")
    return staging_root


def _prepare_engine_staging(
    home: Path,
    staging_root: Path,
) -> None:
    _sweep_pack_sync_temps(staging_root)
    _regular_file(staging_root / "install.yml", "engine staging install.yml")
    _replace_staging_pack(home / "persona-engine", staging_root)
    build = staging_root / "build"
    if build.exists():
        _regular_directory(build, "engine staging build directory")
        shutil.rmtree(build)


def _run_persona(command: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            ["persona", command],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApplyError(f"persona {command} failed: {exc}") from exc
    if completed.returncode != 0:
        raw_detail = completed.stderr or completed.stdout
        truncated = len(raw_detail) > 500
        detail = raw_detail[:500].decode("utf-8", "replace").strip()
        if truncated:
            detail = f"{detail} [truncated]" if detail else "[truncated]"
        suffix = f": {detail}" if detail else ""
        raise ApplyError(f"persona {command} exited {completed.returncode}{suffix}")
    return completed


def _build(
    profile: FaceProfile,
    home: Path,
    rendered: Mapping[str, bytes],
    config: Mapping[str, object] | None = None,
) -> str:
    if not profile.engine:
        return _content_hash(profile, rendered)
    if config is None:
        raise ApplyError(f"{profile.name} engine build requires face config")
    staging_root = _configured_engine_staging_root(profile, config)
    if staging_root is None:
        raise ApplyError(f"{profile.name} engine profile has no staging root")
    try:
        lock = acquire_staging_lock(staging_root, profile.name)
    except (OSError, ValueError) as exc:
        detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        raise ApplyError(
            f"{profile.name} staging lock acquisition failed: {detail}"
        ) from exc
    if lock is None:
        raise ApplyError(
            staging_contention_detail(staging_root, profile.name, "engine staging")
        )
    failure: BaseException | None = None
    try:
        _validate_engine_staging(profile, home, staging_root)
        _prepare_engine_staging(home, staging_root)
        _run_persona("build", staging_root)
        _regular_directory(staging_root / "build", "engine staging build directory")
        try:
            manifest = json.loads(
                (staging_root / "build" / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ApplyError("persona build manifest is missing or invalid") from exc
        content_hash = manifest.get("content_hash") if isinstance(manifest, dict) else None
        if (
            not isinstance(content_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
        ):
            raise ApplyError("persona build manifest omitted a valid content_hash")
        doctor = _run_persona("doctor", staging_root)
        try:
            report = json.loads(doctor.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ApplyError("persona doctor output was not valid JSON") from exc
        if (
            not isinstance(report, dict)
            or report.get("ok") is not True
            or report.get("issues") != []
        ):
            raise ApplyError("persona doctor report is not clean")
        return content_hash
    except BaseException as exc:
        failure = exc
        raise
    finally:
        released = release_lock(lock)
        if not released:
            message = f"{profile.name} staging lock directory could not be removed"
            if failure is None:
                raise ApplyError(message)
            raise ApplyError(message) from failure


def _assert_guarded_payloads(
    profile: FaceProfile, home: Path, expected: Mapping[str, bytes]
) -> None:
    for rel_path, payload in expected.items():
        target = _guarded_target(profile, home, rel_path)
        if not target.is_file() or target.read_bytes() != payload:
            raise ApplyError(f"guarded payload mutated after build: {rel_path}")


def _tag_name(profile: FaceProfile, home: Path, run_date: str) -> str:
    prefix = f"overlay-snap-{profile.name}-{run_date.replace('-', '')}-"
    existing = set(_git(home, "tag", "--list", f"{prefix}*").stdout.decode().splitlines())
    number = 1
    while f"{prefix}{number}" in existing:
        number += 1
    return f"{prefix}{number}"


def _has_proposal(ledger: Mapping[str, object], proposal_id: str) -> bool:
    return any(
        event.get("proposal_id") == proposal_id
        for phrase in ledger["phrases"]
        for event in phrase.get("history", [])
        if isinstance(event, dict)
    )


def _diff_preview(
    profile: FaceProfile,
    before: Mapping[str, object],
    after: Mapping[str, object],
    run_date: str,
    user: str,
) -> dict[str, object]:
    ledger_diff = "".join(
        difflib.unified_diff(
            dump_ledger(before).decode().splitlines(keepends=True),
            dump_ledger(after).decode().splitlines(keepends=True),
            fromfile=profile.ledger_path,
            tofile=profile.ledger_path,
        )
    )
    before_render = render_files(profile, before, run_date, user)
    after_render = render_files(profile, after, run_date, user)
    render_diff: dict[str, str] = {}
    for path in sorted(set(before_render) | set(after_render)):
        render_diff[path] = "".join(
            difflib.unified_diff(
                before_render.get(path, b"").decode().splitlines(keepends=True),
                after_render.get(path, b"").decode().splitlines(keepends=True),
                fromfile=path,
                tofile=path,
            )
        )
    return {"ledger": ledger_diff, "render": render_diff}


def _decode_utf8(payload: bytes | None, label: str) -> str:
    if payload is None:
        return ""
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApplyError(f"invalid utf-8 in {label}") from exc


def _active_phrase_sets(ledger: Mapping[str, object]) -> dict[str, set[tuple[str, str]]]:
    phrases = ledger.get("phrases")
    if not isinstance(phrases, list):
        raise ApplyError("ledger phrases must be a list")
    result = {state: set() for state in ("candidate", "staged", "adopted")}
    for item in phrases:
        if not isinstance(item, dict):
            raise ApplyError("ledger phrase must be a mapping")
        state = item.get("state")
        if state in result:
            phrase_id = item.get("id")
            text = item.get("text")
            if not isinstance(phrase_id, str) or not isinstance(text, str):
                raise ApplyError("ledger phrase identity is invalid")
            result[state].add((phrase_id, text))
    return result


def _render_entry_sets(
    profile: FaceProfile,
    rendered: Mapping[str, bytes],
    user: str,
) -> dict[str, set[str]]:
    adopted_prefix = ADOPTED_TEMPLATE.format(user=user, phrases="")
    candidates_prefix = CANDIDATES_TEMPLATE.format(phrases="")
    result: dict[str, set[str]] = {}
    for rel_path, payload in rendered.items():
        entries: set[str] = set()
        for raw_line in _decode_utf8(payload, rel_path).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(adopted_prefix):
                suffix = line[len(adopted_prefix) :]
            elif line.startswith(candidates_prefix):
                suffix = line[len(candidates_prefix) :]
            else:
                # Preserve arbitrary historical lines as entries, so deletion
                # cannot introduce an untemplated phrase while eject may still
                # reduce that historical text to an empty render.
                entries.add(line)
                continue
            entries.update(item for item in suffix.split("、") if item)
        result[rel_path] = entries
    return result


def _render_entries_from_originals(
    profile: FaceProfile,
    originals: Mapping[str, bytes | None],
    user: str,
) -> dict[str, set[str]]:
    return _render_entry_sets(
        profile,
        {
            rel_path: (originals.get(rel_path) or b"")
            for rel_path in profile.render_files.values()
        },
        user,
    )


def _enforce_render_monotonicity(
    profile: FaceProfile,
    before_render: Mapping[str, bytes | None],
    after_render: Mapping[str, bytes],
    user: str,
) -> None:
    """Apply the shared §5.5 render-subset rule to two explicit states."""

    before_entries = _render_entries_from_originals(profile, before_render, user)
    after_entries = _render_entry_sets(profile, after_render, user)
    for rel_path in profile.render_files.values():
        if not after_entries.get(rel_path, set()).issubset(
            before_entries.get(rel_path, set())
        ):
            raise ApplyError(
                f"deletion monotonicity violated: render expanded in {rel_path}"
            )


def _candidate_render_entry_sets(
    rendered: Mapping[str, bytes],
) -> dict[str, set[str]]:
    candidates_prefix = CANDIDATES_TEMPLATE.format(phrases="")
    result: dict[str, set[str]] = {}
    for rel_path, payload in rendered.items():
        entries: set[str] = set()
        for raw_line in _decode_utf8(payload, rel_path).splitlines():
            line = raw_line.strip()
            if line.startswith(candidates_prefix):
                entries.update(item for item in line[len(candidates_prefix) :].split("、") if item)
        result[rel_path] = entries
    return result


def _candidate_render_entries_from_originals(
    profile: FaceProfile,
    originals: Mapping[str, bytes | None],
) -> dict[str, set[str]]:
    return _candidate_render_entry_sets(
        {
            rel_path: (originals.get(rel_path) or b"")
            for rel_path in profile.render_files.values()
        }
    )


def _constrain_staged_deletion_render_ledger(
    profile: FaceProfile,
    originals: Mapping[str, bytes | None],
    render_ledger: dict[str, Any],
) -> dict[str, Any]:
    before_candidates = _candidate_render_entries_from_originals(profile, originals)
    candidate_path = (
        profile.render_files["candidates"]
        if profile.engine
        else profile.render_files["combined"]
    )
    allowed = before_candidates.get(candidate_path, set())
    constrained = copy.deepcopy(render_ledger)
    constrained["phrases"] = [
        item
        for item in constrained["phrases"]
        if item["state"] != "staged" or item["text"] in allowed
    ]
    return constrained


def _history_audit_meta(audit: GateAudit) -> dict[str, str]:
    meta: dict[str, str] = {}
    if audit.gates_state is not None:
        meta["gates_state"] = f"unverified({audit.gates_state})"
    if audit.mirror_liveness is not None:
        meta["mirror_liveness"] = audit.mirror_liveness
    if audit.monotonicity is not None:
        meta["monotonicity"] = audit.monotonicity
    return meta


def _emit_deletion_audit(
    digest: Digest,
    profile: FaceProfile,
    deletion: DeletionContext,
    audit: GateAudit,
) -> None:
    if audit.gates_state is not None:
        digest.emit(
            f"[RED] {profile.name}: deletion audit op={deletion.operation} "
            f"gates=unverified({audit.gates_state})"
        )
    if audit.mirror_liveness is not None:
        digest.emit(
            f"[RED] {profile.name}: deletion audit op={deletion.operation} "
            f"mirror={audit.mirror_liveness}"
        )
    if audit.monotonicity is not None:
        digest.emit(
            f"[RED] {profile.name}: deletion audit op={deletion.operation} "
            f"monotonicity={audit.monotonicity}"
        )


def _enforce_deletion_monotonicity(
    profile: FaceProfile,
    originals: Mapping[str, bytes | None],
    ledger: dict[str, Any] | None,
    blocklist: BlocklistState,
    rendered: Mapping[str, bytes],
    user: str,
    deletion: DeletionContext,
) -> GateAudit:
    audit = deletion.audit
    _enforce_render_monotonicity(profile, originals, rendered, user)
    after_render = _render_entry_sets(profile, rendered, user)
    if blocklist.entries is not None:
        before_block = set(
            _normalized_blocklist_entries_from_payload(originals.get(profile.blocklist_path))
        )
        after_block = set(blocklist.entries)
        if not before_block.issubset(after_block):
            raise ApplyError("deletion monotonicity violated: blocklist shrank")
    if ledger is None:
        if deletion.operation != "eject":
            raise ApplyError("deletion monotonicity requires a readable ledger")
        return audit.merge(monotonicity="unverifiable-ledger")
    before_payload = originals.get(profile.ledger_path)
    if before_payload is None:
        raise ApplyError("deletion monotonicity requires the prior ledger bytes")
    try:
        before_ledger = validate_ledger(
            yaml.safe_load(_decode_utf8(before_payload, profile.ledger_path)),
            profile.name,
        )
    except Exception as exc:
        if deletion.operation != "eject":
            raise ApplyError("deletion monotonicity requires a readable ledger") from exc
        return audit.merge(monotonicity="unverifiable-ledger")
    before_sets = _active_phrase_sets(before_ledger)
    after_sets = _active_phrase_sets(ledger)
    for state in ("candidate", "staged", "adopted"):
        if not after_sets[state].issubset(before_sets[state]):
            raise ApplyError(f"deletion monotonicity violated: {state} expanded")
    after_active_ids = {phrase_id for values in after_sets.values() for phrase_id, _ in values}
    after_all_ids = {
        str(item["id"])
        for item in ledger.get("phrases", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    after_render_texts = set().union(*after_render.values()) if after_render else set()
    if deletion.operation == "forget":
        if any(phrase_id in after_all_ids for phrase_id in deletion.target_phrase_ids):
            raise ApplyError("deletion monotonicity violated: forgotten phrase remains in ledger")
    elif any(phrase_id in after_active_ids for phrase_id in deletion.target_phrase_ids):
        raise ApplyError("deletion monotonicity violated: target phrase remains injected")
    if any(text in after_render_texts for text in deletion.target_phrase_texts):
        raise ApplyError("deletion monotonicity violated: target phrase remains rendered")
    return audit


@dataclass
class FaceResult:
    changed: bool
    tag: str | None
    content_hash: str | None
    deviations: int = 0


def commit_state(
    profile: FaceProfile,
    pgl_home: Path,
    config: Mapping[str, object],
    run_date: str,
    ledger: dict[str, Any] | None,
    blocklist: list[str] | BlocklistState | None,
    digest: Digest,
    *,
    killswitch_exception: str | None = None,
    deletion: DeletionContext | None = None,
    force_empty_render: bool = False,
    success_events: list[str] | None = None,
    expected_source_sha: str | None = None,
    before_commit: Callable[[str], None] | None = None,
    emit_success: bool = True,
) -> FaceResult:
    home = profile.resolve_home(pgl_home, config)
    _require_overlay_home(profile, home)
    staging_root = _configured_engine_staging_root(profile, config)
    if staging_root is not None:
        _validate_engine_staging(profile, home, staging_root)
    blocklist_state = _coerce_blocklist_state(blocklist)
    audit = deletion.audit if deletion is not None else GateAudit()
    emitted_audit = GateAudit()

    def maybe_emit_deletion_audit() -> None:
        nonlocal emitted_audit
        if deletion is None:
            return
        delta = GateAudit(
            gates_state=audit.gates_state if emitted_audit.gates_state is None else None,
            mirror_liveness=(
                audit.mirror_liveness if emitted_audit.mirror_liveness is None else None
            ),
            monotonicity=audit.monotonicity if emitted_audit.monotonicity is None else None,
        )
        if any(
            value is not None
            for value in (delta.gates_state, delta.mirror_liveness, delta.monotonicity)
        ):
            _emit_deletion_audit(digest, profile, deletion, delta)
            emitted_audit = emitted_audit.merge(
                gates_state=delta.gates_state,
                mirror_liveness=delta.mirror_liveness,
                monotonicity=delta.monotonicity,
            )

    def gate_check() -> None:
        nonlocal audit, deletion
        if deletion is None:
            check_all(pgl_home, profile.name, run_date, killswitch_exception=killswitch_exception)
            return
        current = check_all(
            pgl_home,
            profile.name,
            run_date,
            killswitch_exception=killswitch_exception,
            deletion=DeletionContext(
                operation=deletion.operation,
                target_phrase_ids=deletion.target_phrase_ids,
                target_phrase_texts=deletion.target_phrase_texts,
                audit=audit,
            ),
        )
        if current is not None:
            audit = current
            deletion = DeletionContext(
                operation=deletion.operation,
                target_phrase_ids=deletion.target_phrase_ids,
                target_phrase_texts=deletion.target_phrase_texts,
                audit=audit,
            )
        maybe_emit_deletion_audit()

    gate_check()
    _verify_manifest_or_red(profile, pgl_home, config, digest)
    _require_clean_allowlist(profile, home)
    if expected_source_sha is not None:
        current_source = _git(home, "rev-parse", "HEAD").stdout.decode().strip()
        if current_source != expected_source_sha:
            raise ApplyError("overlay HEAD changed during proposal/review phases")
    snapshot_paths = list(profile.render_files.values())
    if ledger is not None or deletion is not None:
        snapshot_paths.append(profile.ledger_path)
    if blocklist_state.managed and (
        blocklist_state.payload is not None or deletion is not None
    ):
        snapshot_paths.append(profile.blocklist_path)
    originals = _snapshot_files(profile, home, snapshot_paths)
    user = config.get("display_name")
    if not isinstance(user, str) or not user:
        raise ApplyError("face config requires display_name")
    if ledger is None and not force_empty_render:
        raise ApplyError("a valid ledger is required unless the render is forced empty")
    if ledger is not None:
        validate_ledger(ledger, profile.name)
    renderable = ledger is not None and any(
        item["state"] in {"staged", "adopted"} for item in ledger["phrases"]
    )
    render_ledger, dropped = (
        _lint_render_ledger(
            ledger,
            skip=force_empty_render or not renderable or deletion is not None,
        )
        if ledger is not None
        else ({}, [])
    )
    for phrase_id, violations in dropped:
        digest.emit(f"[RED] {profile.name}: render dropped {phrase_id} rules={','.join(violations)}")
    if dropped and ledger is not None:
        _demote_lint_drops(ledger, dropped, run_date)
        render_ledger, _ = _lint_render_ledger(ledger)
    candidate_render_ledger = render_ledger
    if deletion is not None and ledger is not None:
        candidate_render_ledger = _constrain_staged_deletion_render_ledger(
            profile,
            originals,
            render_ledger,
        )
    if not force_empty_render:
        _render_caps(
            profile,
            render_ledger,
            run_date,
            user,
            candidate_render_ledger=candidate_render_ledger,
        )
    _verify_manifest_or_red(profile, pgl_home, config, digest)
    rendered = (
        {path: b"" for path in profile.render_files.values()}
        if ledger is None
        else render_files(profile, candidate_render_ledger, run_date, user)
    )
    if force_empty_render:
        rendered = {path: b"" for path in profile.render_files.values()}
    if deletion is not None:
        try:
            audit = _enforce_deletion_monotonicity(
                profile,
                originals,
                ledger,
                blocklist_state,
                rendered,
                user,
                deletion,
            )
        except BaseException as exc:
            # No guarded write has happened yet, but this is still a rejected
            # transaction and needs the same red audit signal as a later abort.
            digest.emit(f"[RED] {profile.name}: abort/revert: {exc}")
            raise
        deletion = DeletionContext(
            operation=deletion.operation,
            target_phrase_ids=deletion.target_phrase_ids,
            target_phrase_texts=deletion.target_phrase_texts,
            audit=audit,
        )
        maybe_emit_deletion_audit()
    ledger_payload = dump_ledger(ledger) if ledger is not None else None
    source_head = _git(home, "rev-parse", "HEAD").stdout.decode().strip()
    created_tag: str | None = None
    try:
        gate_check()
        if ledger is not None and ledger_payload is not None:
            write_guarded(profile, home, profile.ledger_path, ledger_payload)
        for rel_path, payload in rendered.items():
            gate_check()
            write_guarded(profile, home, rel_path, payload)
        gate_check()
        block_payload = blocklist_state.payload if blocklist_state.managed else None
        if block_payload is not None:
            write_guarded(profile, home, profile.blocklist_path, block_payload)
        content_hash = _build(profile, home, rendered, config)
        guarded_payloads = {**rendered}
        if ledger_payload is not None:
            guarded_payloads[profile.ledger_path] = ledger_payload
        if block_payload is not None:
            guarded_payloads[profile.blocklist_path] = block_payload
        _assert_guarded_payloads(
            profile,
            home,
            guarded_payloads,
        )
        if _git(home, "diff", "--quiet", "--", *profile.allowlist, check=False).returncode == 0:
            gate_check()
            verify_manifest(profile, pgl_home, config)
            return FaceResult(False, None, content_hash)
        parent_sha = source_head
        tag = _tag_name(profile, home, run_date)
        history_meta = _history_audit_meta(audit)
        if ledger is not None and deletion is not None and history_meta:
            history = ledger.setdefault("history", [])
            if history and isinstance(history[-1], dict) and history[-1].get("action") in {
                "forget",
                "eject",
            }:
                history[-1].setdefault("audit", {}).update(history_meta)
        if ledger is not None:
            ledger["snapshots"].append(
                {"at": run_date, "parent_sha": parent_sha, "tag": tag, "content_hash": content_hash}
            )
            snapshot_event: dict[str, Any] = {
                "at": run_date,
                "action": "snapshot",
                "parent_sha": parent_sha,
                "tag": tag,
                "content_hash": content_hash,
            }
            if deletion is not None and history_meta:
                snapshot_event["audit"] = history_meta
            ledger["history"].append(snapshot_event)
            ledger_payload = dump_ledger(ledger)
            gate_check()
            write_guarded(profile, home, profile.ledger_path, ledger_payload)
        verify_manifest(profile, pgl_home, config)
        gate_check()
        changed = set(
            _git(home, "diff", "--name-only", "--", *profile.allowlist).stdout.decode().splitlines()
        )
        _git(home, "add", "--", *profile.allowlist)
        if not changed:
            raise ApplyError("overlay allowlist has no changed paths to commit")
        verify_manifest(profile, pgl_home, config)
        gate_check()
        audit_trailers: list[str] = []
        if deletion is not None:
            if audit.gates_state is not None:
                audit_trailers.append(f"Gates-State: unverified({audit.gates_state})")
            if audit.mirror_liveness is not None:
                audit_trailers.append(f"Mirror-Liveness: {audit.mirror_liveness}")
            if audit.monotonicity is not None:
                audit_trailers.append(f"Monotonicity: {audit.monotonicity}")
        message = (
            f"Preserve reviewed {profile.name} overlay state for {run_date}\n\n"
            f"Parent-SHA: {parent_sha}\nContent-Hash: {content_hash}\n"
            + "".join(f"{line}\n" for line in audit_trailers)
        )
        if before_commit is not None:
            before_commit(content_hash)
        _git(home, "commit", "--only", "-m", message, "--", *profile.allowlist)
        committed = set(
            _git(home, "show", "--name-only", "--pretty=format:", "HEAD").stdout.decode().splitlines()
        )
        if committed != changed:
            raise ApplyError("overlay commit file list did not match changed allowlist paths")
        verify_manifest(profile, pgl_home, config)
        gate_check()
        _git(home, "checkout", "HEAD", "--", *profile.allowlist)
        _git(home, "tag", tag)
        created_tag = tag
        if emit_success:
            digest.emit(f"{profile.name}: committed {tag} content_hash={content_hash}")
            for event in success_events or []:
                digest.emit(event)
        return FaceResult(True, tag, content_hash)
    except BaseException as exc:
        if created_tag is not None:
            _git(home, "tag", "-d", created_tag, check=False)
        current = _git(home, "rev-parse", "HEAD", check=False).stdout.decode().strip()
        # A commit can only have happened immediately before tag creation. Move
        # the branch back to its source while preserving the explicit file restore.
        if current and current != source_head:
            _git(home, "reset", "--soft", source_head, check=False)
        _restore_files(profile, home, originals)
        _git(home, "reset", "HEAD", "--", *profile.allowlist, check=False)
        digest.emit(f"[RED] {profile.name}: abort/revert: {exc}")
        raise


def run_face(
    profile: FaceProfile,
    pgl_home: Path,
    config: Mapping[str, object],
    thresholds: Mapping[str, object],
    run_date: str,
    digest: Digest,
    *,
    before_commit: Callable[[str], None] | None = None,
) -> FaceResult:
    home = profile.resolve_home(pgl_home, config)
    _require_overlay_home(profile, home)
    staging_root = _configured_engine_staging_root(profile, config)
    if staging_root is not None:
        _validate_engine_staging(profile, home, staging_root)
    check_all(pgl_home, profile.name, run_date)
    _verify_manifest_or_red(profile, pgl_home, config, digest)
    _require_clean_allowlist(profile, home)
    initial_source_sha = _git(home, "rev-parse", "HEAD").stdout.decode().strip()
    ledger = load_ledger(home / profile.ledger_path, profile.name)
    blocklist_path = home / profile.blocklist_path
    try:
        blocklist = read_blocklist_state(blocklist_path).entries
    except (OSError, UnicodeError) as exc:
        raise ApplyError(f"missing or invalid blocklist: {blocklist_path}") from exc

    def report_transcript(message: str) -> None:
        digest.emit(f"{profile.name}: {message}")

    drift = runtime_status()
    if drift.drifted:
        digest.emit(
            f"[RED] {profile.name}: UCD drift runtime={drift.runtime_version} "
            f"corpus={drift.corpus_version} direction={drift.direction}; admission skipped"
        )
        admission_reason = (
            f"UCD drift runtime={drift.runtime_version} "
            f"corpus={drift.corpus_version} direction={drift.direction}"
        )
        harvested = []
    else:
        transcript_paths, admission_reason = transcript_inputs(config, report_transcript)
    if not drift.drifted and admission_reason is None:
        try:
            harvested = harvest(
                pgl_home,
                profile.name,
                run_date,
                config,
                ledger,
                thresholds,
                blocklist_path,
                transcript_paths,
                report_transcript,
            )
        except TranscriptUnavailable as exc:
            admission_reason = str(exc)
            harvested = []
    elif not drift.drifted:
        harvested = []
    classifier_argv = config.get("classifier_argv")
    aggregated, eligible, deviations = aggregate(
        pgl_home,
        profile.name,
        run_date,
        ledger,
        thresholds,
        classifier_argv,
        blocklist,
    )
    if admission_reason is not None:
        digest.emit(
            f"{profile.name}: admission skipped; transcript echo indeterminate: {admission_reason}"
        )
        eligible = [
            item
            for item in eligible
            if str(item["transition"]).endswith(("->blocked", "->demoted"))
        ]
    approved: list[tuple[str, str, str]] = []
    writer_argv = config.get("writer_argv")
    reviewer_argv = config.get("reviewer_argv")
    pending_events: list[str] = []
    for item in eligible:
        phrase_id = str(item["phrase_id"])
        transition = str(item["transition"])
        expected_id = compute_proposal_id(profile.name, phrase_id, transition, run_date)
        if _has_proposal(aggregated, expected_id):
            continue
        if item.get("safety") is True:
            approved.append((phrase_id, transition, expected_id))
            continue
        phrase = next(value for value in aggregated["phrases"] if value["id"] == phrase_id)
        evidence_input = {
            "proposal_id": expected_id,
            "face": profile.name,
            "phrase_id": phrase_id,
            "transition": transition,
            "evidence": copy.deepcopy(phrase["evidence"]),
            "source": copy.deepcopy(phrase["source"]),
            "generated_at": run_date,
        }
        try:
            proposal = propose(writer_argv, evidence_input)
            if any(proposal[key] != evidence_input[key] for key in ("proposal_id", "face", "phrase_id", "transition", "generated_at")):
                raise AdapterError("writer proposal identity mismatch")
            preview_ledger = copy.deepcopy(aggregated)
            apply_transition(preview_ledger, phrase_id, transition, run_date, expected_id)
            review(
                reviewer_argv,
                {"proposal": proposal, "diff": _diff_preview(profile, aggregated, preview_ledger, run_date, str(config["display_name"]))},
            )
        except (AdapterError, AggregateError) as exc:
            digest.emit(f"{profile.name}: review blocked {phrase_id} {transition}: {exc}")
            continue
        # Deterministic authorization re-check from primary data immediately
        # before accepting the model nomination.
        rechecked, reeligible, _ = aggregate(
            pgl_home,
            profile.name,
            run_date,
            aggregated,
            thresholds,
            classifier_argv,
            blocklist,
        )
        if admission_reason is not None:
            reeligible = [
                item
                for item in reeligible
                if str(item["transition"]).endswith(("->blocked", "->demoted"))
            ]
        if not any(value["phrase_id"] == phrase_id and value["transition"] == transition for value in reeligible):
            digest.emit(f"{profile.name}: deterministic re-check rejected {phrase_id} {transition}")
            continue
        aggregated = rechecked
        approved.append((phrase_id, transition, expected_id))
    for phrase_id, transition, expected_id in approved:
        phrase = next(item for item in aggregated["phrases"] if item["id"] == phrase_id)
        text = phrase["text"]
        apply_transition(aggregated, phrase_id, transition, run_date, expected_id)
        if transition.endswith("->blocked"):
            append_blocked_text(blocklist, text)
            pending_events.append(f"{profile.name}: demotion/block {phrase_id}")
        elif transition.endswith("->demoted"):
            pending_events.append(f"{profile.name}: decay demotion {phrase_id}")
        elif transition.endswith("->adopted"):
            pending_events.append(f"{profile.name}: adoption {phrase_id}")
    for candidate in harvested[:3]:
        phrase = new_phrase(str(candidate["phrase_id"]), str(candidate["text"]), candidate["source"])
        phrase["history"].append(
            {
                "at": run_date,
                "from": "",
                "to": "candidate",
                "by": "applier",
                "proposal_id": str(candidate["proposal_id"]),
            }
        )
        aggregated["phrases"].append(phrase)
        predecessor_id = candidate.get("predecessor_id")
        if isinstance(predecessor_id, str):
            aggregated["history"].append(
                {
                    "at": run_date,
                    "action": "reharvest",
                    "predecessor_id": predecessor_id,
                    "new_id": phrase["id"],
                }
            )
    result = commit_state(
        profile,
        pgl_home,
        config,
        run_date,
        aggregated,
        blocklist,
        digest,
        success_events=pending_events,
        expected_source_sha=initial_source_sha,
        before_commit=before_commit,
    )
    result.deviations = deviations
    if deviations:
        digest.emit(f"{profile.name}: protocol deviations={deviations}")
    if not result.changed:
        digest.emit(f"{profile.name}: no-op (deterministic state unchanged)")
    return result
