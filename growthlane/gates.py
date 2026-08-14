"""Fail-closed governance, killswitch, and mirror-liveness gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import json
import os
import stat


class GateError(RuntimeError):
    pass


GateCheckpoint = Literal["document", "cp2", "cp3"]


class GateExplicitNo(GateError):
    """A present governance value explicitly denied the operation."""

    def __init__(
        self,
        message: str,
        *,
        checkpoint: Literal["cp2", "cp3"],
        face: str | None = None,
    ) -> None:
        super().__init__(message)
        self.checkpoint = checkpoint
        self.face = face


class GateUnavailable(GateError):
    """Governance could not be verified because required evidence is unavailable."""

    def __init__(
        self,
        message: str,
        *,
        checkpoint: GateCheckpoint = "document",
        face: str | None = None,
    ) -> None:
        super().__init__(message)
        self.checkpoint = checkpoint
        self.face = face


_JST = timezone(timedelta(hours=9))
_DELETION_OPERATIONS = frozenset({"block", "forget", "eject"})


@dataclass(frozen=True)
class GateAudit:
    gates_state: str | None = None
    mirror_liveness: str | None = None
    monotonicity: str | None = None

    def merge(
        self,
        *,
        gates_state: str | None = None,
        mirror_liveness: str | None = None,
        monotonicity: str | None = None,
    ) -> GateAudit:
        return GateAudit(
            gates_state=self.gates_state or gates_state,
            mirror_liveness=self.mirror_liveness or mirror_liveness,
            monotonicity=self.monotonicity or monotonicity,
        )


@dataclass(frozen=True)
class DeletionContext:
    operation: str
    target_phrase_ids: tuple[str, ...] = ()
    target_phrase_texts: tuple[str, ...] = ()
    audit: GateAudit = field(default_factory=GateAudit)

    def __post_init__(self) -> None:
        if self.operation not in _DELETION_OPERATIONS:
            raise ValueError(f"unknown deletion operation: {self.operation}")


def deletion_context(
    operation: str,
    *,
    target_phrase_ids: tuple[str, ...] = (),
    target_phrase_texts: tuple[str, ...] = (),
) -> DeletionContext:
    if operation not in _DELETION_OPERATIONS:
        raise ValueError(f"unknown deletion operation: {operation}")
    return DeletionContext(
        operation=operation,
        target_phrase_ids=target_phrase_ids,
        target_phrase_texts=target_phrase_texts,
    )


def _scalar(value: str) -> object:
    cleaned = value.strip()
    if cleaned in {"true", "True"}:
        return True
    if cleaned in {"false", "False"}:
        return False
    if cleaned in {"null", "~"}:
        return None
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (
        cleaned.startswith("'") and cleaned.endswith("'")
    ):
        return cleaned[1:-1]
    return cleaned


def _parse_simple_yaml(lines: list[str]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for number, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            raise GateError(f"invalid YAML indentation line {number}")
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line or line.startswith("-"):
            raise GateError(f"invalid gates YAML line {number}")
        key, value = (part.strip() for part in line.split(":", 1))
        if not key:
            raise GateError(f"invalid gates YAML key line {number}")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise GateError(f"invalid gates YAML nesting line {number}")
        parent = stack[-1][1]
        if key in parent:
            raise GateError(f"duplicate gates YAML key line {number}")
        if value:
            parent[key] = _scalar(value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def _simple_yaml(path: Path) -> dict[str, Any]:
    """Strictly parse the small human gate marker mapping."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise GateUnavailable(f"missing or unreadable gates file: {path}") from exc
    return _parse_simple_yaml(lines)


def _killswitch_yaml(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise GateError("killswitch marker changed to a non-regular shape")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            return _parse_simple_yaml(stream.read().splitlines())
    except (OSError, UnicodeError) as exc:
        raise GateError("killswitch marker cannot be read without following links") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _killswitch_shape(path: Path) -> str | None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"
    if stat.S_ISREG(mode):
        return "regular-file"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character-device"
    if stat.S_ISBLK(mode):
        return "block-device"
    return "non-regular"


def killswitch_mode(pgl_home: Path) -> str | None:
    path = pgl_home / "KILLSWITCH"
    shape = _killswitch_shape(path)
    if shape is None:
        return None
    if shape != "regular-file":
        return "freeze"
    try:
        parsed = _killswitch_yaml(path)
    except GateError:
        return "freeze"
    mode = parsed.get("mode", "freeze")
    return mode if mode in {"freeze", "eject"} else "freeze"


def check_killswitch(pgl_home: Path, *, exception: str | None = None) -> None:
    path = pgl_home / "KILLSWITCH"
    initial_shape = _killswitch_shape(path)
    mode = killswitch_mode(pgl_home)
    if mode is None:
        if initial_shape is not None:
            raise GateError(
                f"[RED] killswitch marker shape={initial_shape} disappeared during check "
                "(mode=freeze)"
            )
        return
    final_shape = _killswitch_shape(path)
    if exception == "safety":
        # Contract v1.1 §10 already records the manual deletion exception for
        # immediate block/forget despite the normal killswitch freeze.
        return
    if exception == "eject" and mode == "eject":
        return
    non_regular_shape = next(
        (
            shape
            for shape in (final_shape, initial_shape)
            if shape is not None and shape != "regular-file"
        ),
        None,
    )
    if non_regular_shape is not None:
        raise GateError(f"[RED] killswitch marker shape={non_regular_shape} (mode=freeze)")
    raise GateError(f"killswitch detected (mode={mode})")


def _cp_marker_established(pgl_home: Path, face: str) -> bool:
    baseline_dir = pgl_home / "soul-baseline"
    # This is not a replacement for CP-2's record of record. It is only a
    # durable witness that this face has already crossed the baseline command's
    # existing CP gate, so missing/corrupt gates.yml does not open deletion
    # exceptions before first effectiveness.
    for name, required_keys in (
        (f"{face}.manifest", {"overlay_home", "files"}),
        (f"{face}.overlay-home", {"overlay_home"}),
    ):
        path = baseline_dir / name
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(value, dict)
                and set(value) == required_keys
                and isinstance(value.get("overlay_home"), str)
                and value["overlay_home"].strip()
            ):
                return True
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return False


def _cp_exception_allowed(
    pgl_home: Path,
    face: str,
    deletion: DeletionContext | None,
) -> bool:
    return deletion is not None and _cp_marker_established(pgl_home, face)


def _merge_audit(
    deletion: DeletionContext | None,
    *,
    gates_state: str | None = None,
    mirror_liveness: str | None = None,
) -> GateAudit | None:
    if deletion is None:
        return None
    return deletion.audit.merge(
        gates_state=gates_state,
        mirror_liveness=mirror_liveness,
    )


def check_cp(
    pgl_home: Path,
    face: str,
    *,
    deletion: DeletionContext | None = None,
) -> GateAudit | None:
    path = pgl_home / "gates.yml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        if _cp_exception_allowed(pgl_home, face, deletion):
            return _merge_audit(deletion, gates_state="gates-missing")
        raise GateUnavailable(f"missing or unreadable gates file: {path}") from exc
    try:
        gates = _parse_simple_yaml(lines)
    except GateError as exc:
        if _cp_exception_allowed(pgl_home, face, deletion):
            return _merge_audit(deletion, gates_state="gates-invalid")
        raise GateUnavailable(str(exc)) from exc
    cp2_in_force = gates.get("cp2_in_force")
    if cp2_in_force is False:
        raise GateExplicitNo("CP-2 is not in force", checkpoint="cp2")
    if cp2_in_force is not True:
        if _cp_exception_allowed(pgl_home, face, deletion):
            return _merge_audit(deletion, gates_state="gates-invalid")
        raise GateUnavailable("CP-2 state is unavailable", checkpoint="cp2")
    if not all(isinstance(gates.get(key), str) and gates[key].strip() for key in ("decided_by", "ref")):
        if _cp_exception_allowed(pgl_home, face, deletion):
            return _merge_audit(deletion, gates_state="cp2-pointers-missing")
        raise GateUnavailable(
            "CP-2 governance pointers are missing", checkpoint="cp2"
        )
    faces = gates.get("faces")
    if not isinstance(faces, dict) or not isinstance(faces.get(face), dict):
        if _cp_exception_allowed(pgl_home, face, deletion):
            return _merge_audit(deletion, gates_state="cp3-missing")
        raise GateUnavailable(
            f"CP-3 gate is missing for {face}", checkpoint="cp3", face=face
        )
    gate = faces[face]
    if gate.get("cp3_go") is False:
        if _cp_exception_allowed(pgl_home, face, deletion):
            return _merge_audit(deletion, gates_state="cp3-not-go")
        raise GateExplicitNo(
            f"CP-3 is not GO for {face}", checkpoint="cp3", face=face
        )
    if gate.get("cp3_go") is not True:
        if _cp_exception_allowed(pgl_home, face, deletion):
            return _merge_audit(deletion, gates_state="cp3-not-go")
        raise GateUnavailable(
            f"CP-3 state is unavailable for {face}", checkpoint="cp3", face=face
        )
    if not all(isinstance(gate.get(key), str) and gate[key].strip() for key in ("decided_by", "ref")):
        if _cp_exception_allowed(pgl_home, face, deletion):
            return _merge_audit(deletion, gates_state="cp3-pointers-missing")
        raise GateUnavailable(
            f"CP-3 governance pointers are missing for {face}",
            checkpoint="cp3",
            face=face,
        )
    return deletion.audit if deletion is not None else None


def check_mirror(
    pgl_home: Path,
    face: str,
    run_date: str,
    *,
    deletion: DeletionContext | None = None,
) -> GateAudit | None:
    path = pgl_home / "reports" / "weekly" / f"latest-{face}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if deletion is not None:
            return _merge_audit(deletion, mirror_liveness="stale(missing)")
        raise GateError(f"mirror marker missing or invalid for {face}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("generated_at"), str):
        if deletion is not None:
            return _merge_audit(deletion, mirror_liveness="stale(missing)")
        raise GateError(f"mirror generated_at missing for {face}")
    try:
        parsed = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
        generated = parsed.astimezone(_JST).date() if parsed.tzinfo is not None else parsed.date()
    except ValueError as exc:
        if deletion is not None:
            return _merge_audit(deletion, mirror_liveness="stale(missing)")
        raise GateError(f"mirror generated_at invalid for {face}") from exc
    current = datetime.now(_JST).date()
    age = (current - generated).days
    if age < 0 or age > 14:
        if deletion is not None:
            return _merge_audit(deletion, mirror_liveness=f"stale({age}d)")
        raise GateError(f"mirror marker stale for {face}: age={age}d")
    return deletion.audit if deletion is not None else None


def check_all(
    pgl_home: Path,
    face: str,
    run_date: str,
    *,
    killswitch_exception: str | None = None,
    deletion: DeletionContext | None = None,
) -> GateAudit | None:
    audit = check_cp(pgl_home, face, deletion=deletion)
    check_killswitch(pgl_home, exception=killswitch_exception)
    if deletion is None:
        check_mirror(pgl_home, face, run_date)
        return None
    merged = deletion.audit if audit is None else audit
    mirror_audit = check_mirror(
        pgl_home,
        face,
        run_date,
        deletion=DeletionContext(
            operation=deletion.operation,
            target_phrase_ids=deletion.target_phrase_ids,
            target_phrase_texts=deletion.target_phrase_texts,
            audit=merged,
        ),
    )
    return merged if mirror_audit is None else mirror_audit
