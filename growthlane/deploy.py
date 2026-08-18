"""Luca production deployment and recovery orchestration."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

import yaml

from collectors.hermes_luca import journal, ledger as acceptance_ledger
from collectors.hermes_luca import rules as luca_rules
from collectors.hermes_luca.adapters import SSHAdapter
from collectors.hermes_luca.config import load_config as load_luca_collector_config
from growthlane.gates import check_killswitch
from growthlane.ledger import validate_ledger
from growthlane.production_anchor import (
    read_production_anchor as _read_production_anchor,
    write_production_anchor as _write_production_anchor,
)
from mirror.common import (
    MirrorError,
    atomic_write,
    read_bytes_nofollow,
)
from mirror.weekly import fetch_luca_production_digest


def _require_ssh_operand(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MirrorError(f"{label} must be a non-empty string")
    if value.startswith("-") or any(character.isspace() for character in value):
        raise MirrorError(
            f"{label} must be a host, user@host, or user@host:path, not an ssh option"
        )
    if "::" in value:
        raise MirrorError(f"{label} must not use rsync daemon (host::module) syntax")
    return value


DEPLOY_TARGET = _require_ssh_operand(
    os.environ.get("PGL_DEPLOY_TARGET", "admin@example-vps"), "PGL_DEPLOY_TARGET"
)
DEPLOY_KEY = Path("~/.ssh/pgl-luca-deploy").expanduser()
DEFAULT_LUCA_COLLECTOR_CONFIG = (
    Path(__file__).resolve().parents[1] / "config/obs-collector-luca.json"
)
PRODUCTION_PACK_DESTINATION = _require_ssh_operand(
    os.environ.get(
        "PGL_PRODUCTION_PACK_DESTINATION",
        f"{DEPLOY_TARGET}:/home/admin/.persona-engine/luca/pack",
    ),
    "PGL_PRODUCTION_PACK_DESTINATION",
)
KNOWN_PRODUCTION_INSTALL_RELATIVE_PATH = Path("tests/luca-pack/install.yml")
ACCEPTANCE_LEDGER_RELATIVE_PATH = Path("state/luca-verify-sessions.jsonl")
PRIVATE_ACTIVITY_HOLD_MINUTES = 15
PRIVATE_ACTIVITY_PREFLIGHT_PHASE = "private-activity-preflight"
PRIVATE_ACTIVITY_PHASE = "private-activity"
FROZEN_LUCA_PLACEHOLDER_DECLARATIONS = frozenset(
    {"agent-name", "user", "owner-name"}
)
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_JST = timezone(timedelta(hours=9))
_COMMAND_TIMEOUT_SECONDS = 300
_ACCEPT_COMMAND_TIMEOUT_SECONDS = 380


CommandRunner = Callable[
    [Sequence[str], Path], subprocess.CompletedProcess[str]
]
KillswitchCheck = Callable[[], None]
PrivateActivityCheck = Callable[[], bool]
Clock = Callable[[], datetime]
ProductionDigest = Callable[[], str]
AcceptanceMode = Literal["standard", "deletion"]


class DeployError(RuntimeError):
    """A classified e2 failure for the outer nightly recovery boundary."""

    def __init__(self, message: str, *, phase: str, backup_completed: bool) -> None:
        super().__init__(message)
        self.phase = phase
        self.backup_completed = backup_completed

    @property
    def recovery_required(self) -> bool:
        return self.backup_completed


class PrivateActivityHold(DeployError):
    """A classified acceptance hold caused by observed private activity."""


def is_private_activity_hold(error: BaseException) -> bool:
    """Return whether an e2 failure is the acceptance private-activity hold."""

    return isinstance(error, PrivateActivityHold)


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeployResult:
    content_hash: str
    session_ids: tuple[str, ...]
    window_id: str


@dataclass
class DeployAttempt:
    deploy_started: bool = False
    backup_taken: bool = False
    orphan_window_id: str | None = None


@dataclass
class AttendedSafetyTransport:
    """One attended manual Luca production transaction with rollback direction."""

    operation: Literal["deletion", "rollback"]
    attended: bool
    pgl_home: Path
    obs_root: Path
    staging_root: Path
    known_production_install: Path
    ledger_path: Path
    deploy_key: Path
    staging_rebuilder: Callable[[], None]
    command_runner: CommandRunner | None = None
    production_digest: ProductionDigest = fetch_luca_production_digest
    full_gate_check: KillswitchCheck | None = None
    clock: Clock = lambda: datetime.now(_JST)
    monotonic: Callable[[], float] = time.monotonic
    window_id_factory: Callable[[], uuid.UUID] = uuid.uuid4
    attempt: DeployAttempt = field(default_factory=DeployAttempt)
    old_content_hash: str | None = field(default=None, init=False)
    reflected_content_hash: str | None = field(default=None, init=False)
    production_reflected_at: float | None = field(default=None, init=False)
    _anchor_before: bytes | None = field(default=None, init=False, repr=False)
    _anchor_existed: bool = field(default=False, init=False, repr=False)
    _anchor_changed: bool = field(default=False, init=False, repr=False)
    _recovery_attempted: bool = field(default=False, init=False, repr=False)
    _rollback_direction: Literal["reduction", "increase"] | None = field(
        default=None, init=False, repr=False
    )
    _deletion_repo_baseline_hash: str | None = field(
        default=None, init=False, repr=False
    )
    _direction_production_hash: str | None = field(
        default=None, init=False, repr=False
    )
    _transport_performed: bool = field(default=False, init=False, repr=False)
    _lifecycle_commit_required: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.operation not in {"deletion", "rollback"}:
            raise ValueError("manual Luca transport operation must be deletion or rollback")
        if self.attended is not True:
            raise ValueError("manual Luca safety transport requires attended=True")

    @property
    def rollback_direction(self) -> Literal["reduction", "increase"] | None:
        return self._rollback_direction

    def _capture_direction_baseline(
        self,
        *,
        direction: Literal["deletion", "rollback"],
        repo_baseline_hash: str,
        unavailable_message: str,
    ) -> str:
        """Validate and capture the shared repo/live direction baseline."""

        if self._direction_production_hash is not None:
            raise RuntimeError(f"{direction} baseline was already configured")
        if _CONTENT_HASH.fullmatch(repo_baseline_hash) is None:
            raise DeployError(
                f"{direction} repo-before baseline is not a valid content hash",
                phase=f"{direction}-direction",
                backup_completed=False,
            )
        production_hash = self.production_digest()
        if (
            not isinstance(production_hash, str)
            or _CONTENT_HASH.fullmatch(production_hash) is None
        ):
            raise DeployError(
                unavailable_message,
                phase=f"{direction}-direction",
                backup_completed=False,
            )
        self._direction_production_hash = production_hash
        return production_hash

    def configure_rollback_direction(
        self,
        *,
        repo_baseline_hash: str,
        target_is_monotone_non_increasing: bool,
    ) -> Literal["reduction", "increase"]:
        """Anchor the client-side direction verdict to live production."""

        if self.operation != "rollback":
            raise RuntimeError("rollback direction applies only to rollback transport")
        if self._rollback_direction is not None:
            raise RuntimeError("rollback direction was already configured")
        production_hash = self._capture_direction_baseline(
            direction="rollback",
            repo_baseline_hash=repo_baseline_hash,
            unavailable_message=(
                "rollback direction=increase/not-proven: production content_hash "
                "is unavailable"
            ),
        )
        self._rollback_direction = (
            "reduction"
            if production_hash == repo_baseline_hash
            and target_is_monotone_non_increasing
            else "increase"
        )
        return self._rollback_direction

    def configure_deletion_baseline(self, *, repo_baseline_hash: str) -> bool:
        """Anchor deletion relaxation to the repo state seen before mutation."""

        if self.operation != "deletion":
            raise RuntimeError("deletion baseline applies only to deletion transport")
        production_hash = self._capture_direction_baseline(
            direction="deletion",
            repo_baseline_hash=repo_baseline_hash,
            unavailable_message=(
                "deletion direction is unavailable: production content_hash "
                "cannot be compared with the repo-before baseline"
            ),
        )
        self._deletion_repo_baseline_hash = repo_baseline_hash
        return production_hash == repo_baseline_hash

    def before_commit(
        self,
        content_hash: str,
        *,
        target_is_empty_render: bool = False,
        relaxed_killswitch: bool | None = None,
    ) -> None:
        """Reflect staged bytes in production before the local commit is allowed."""

        if self.production_reflected_at is not None:
            raise RuntimeError("manual Luca transport cannot run twice")
        if self.operation == "rollback" and self._rollback_direction is None:
            raise DeployError(
                "rollback direction must be proven before transport",
                phase="rollback-direction",
                backup_completed=False,
            )
        if self.operation == "deletion" and self._deletion_repo_baseline_hash is None:
            raise DeployError(
                "deletion repo-before baseline must be proven before transport",
                phase="deletion-direction",
                backup_completed=False,
            )
        if type(target_is_empty_render) is not bool:
            raise TypeError("target_is_empty_render must be a bool")
        old_content_hash = self.production_digest()
        if (
            not isinstance(old_content_hash, str)
            or _CONTENT_HASH.fullmatch(old_content_hash) is None
        ):
            raise DeployError(
                "existing production content_hash is unavailable",
                phase="old-production-hash",
                backup_completed=False,
            )
        self.old_content_hash = old_content_hash
        if (
            self._direction_production_hash is not None
            and old_content_hash != self._direction_production_hash
        ):
            direction = "rollback" if self.operation == "rollback" else "deletion"
            raise DeployError(
                f"{direction} direction is no longer proven: production changed "
                "between baseline and before-commit digest checks",
                phase=f"{direction}-direction",
                backup_completed=False,
            )

        try:
            lifecycle = load_lifecycle_state(self.obs_root)
        except journal.JournalError as exc:
            raise DeployError(
                f"prior lifecycle attempt cannot be classified: {exc}",
                phase="journal-preflight",
                backup_completed=False,
            ) from exc
        if lifecycle.resume_required:
            accepted_new = (
                lifecycle.acceptance_succeeded
                and not lifecycle.recovery_started
                and lifecycle.production_state == "NEW"
            )
            if not accepted_new:
                raise DeployError(
                    "prior lifecycle attempt cannot be classified as accepted NEW; "
                    "manual recovery is required",
                    phase="journal-preflight",
                    backup_completed=False,
                )
            if old_content_hash == content_hash:
                # The attended operation is completing the exact accepted NEW
                # bytes; its local commit may close the original row-6 attempt.
                self._lifecycle_commit_required = True
            else:
                journal.append_attended_superseded(
                    self.obs_root,
                    ts=_now_iso(self.clock),
                    operation=self.operation,
                    production_hash=old_content_hash,
                )
                # The resolution event also durably opens this attended
                # successor, so deploy_and_accept must not append a second
                # deploy-started and pre-backup recovery can terminate it.
                self.attempt.deploy_started = True

        if old_content_hash == content_hash:
            if self._lifecycle_commit_required:
                self.reflected_content_hash = content_hash
                self.production_reflected_at = self.monotonic()
                return

        relaxation_allowed = self.operation == "deletion" and (
            target_is_empty_render
            or old_content_hash == self._deletion_repo_baseline_hash
        )
        relaxation_allowed = relaxation_allowed or (
            self.operation == "rollback" and self._rollback_direction == "reduction"
        )
        use_relaxed_killswitch = (
            relaxation_allowed
            if relaxed_killswitch is None
            else relaxed_killswitch
        )
        if use_relaxed_killswitch and not relaxation_allowed:
            raise DeployError(
                "relaxed killswitch transport is not authorized by the proven direction",
                phase=f"{self.operation}-direction",
                backup_completed=False,
            )
        if self.operation == "deletion" and not use_relaxed_killswitch:
            divergence = (
                "Luca deletion production divergence: live production does not match "
                "the repo-before baseline and the target render is non-empty"
            )
            if self.full_gate_check is None:
                raise DeployError(
                    f"{divergence}; use pgl-eject to force an empty render or restore "
                    "full gates and run reconciliation",
                    phase="deletion-divergence",
                    backup_completed=False,
                )
            try:
                self.full_gate_check()
            except Exception as exc:
                raise DeployError(
                    f"{divergence}; full-gate reconciliation refused: {exc}; "
                    "pgl-eject remains available",
                    phase="deletion-divergence",
                    backup_completed=False,
                ) from exc

        if old_content_hash == content_hash:
            self.reflected_content_hash = content_hash
            self.production_reflected_at = self.monotonic()
            return

        # Recent private activity is an unattended restart hold, not authority
        # to block the human running this operation.  Killswitch independence is
        # narrower: deletion and a production-anchored reduction rollback only.
        deploy_and_accept(
            pgl_home=self.pgl_home,
            obs_root=self.obs_root,
            staging_root=self.staging_root,
            known_production_install=self.known_production_install,
            ledger_path=self.ledger_path,
            content_hash=content_hash,
            deploy_key=self.deploy_key,
            command_runner=self.command_runner or _run_command,
            killswitch_check=(
                (lambda: None)
                if use_relaxed_killswitch
                else self.full_gate_check
            ),
            private_activity_check=lambda: False,
            clock=self.clock,
            window_id_factory=self.window_id_factory,
            attempt=self.attempt,
            acceptance_mode=(
                "deletion"
                if self.operation == "deletion" and use_relaxed_killswitch
                else "standard"
            ),
        )
        self._transport_performed = True
        self.reflected_content_hash = content_hash
        self.production_reflected_at = self.monotonic()

    def reflection_elapsed(self, operation_started_at: float) -> float:
        if self.production_reflected_at is None:
            raise RuntimeError("production reflection has not completed")
        elapsed = self.production_reflected_at - operation_started_at
        if elapsed < 0:
            raise RuntimeError("manual transport monotonic clock moved backwards")
        return elapsed

    def complete(self, content_hash: str) -> None:
        """Resolve the accepted attempt only after the local commit exists."""

        if self.reflected_content_hash != content_hash:
            raise RuntimeError("manual Luca transport completion hash mismatch")
        anchor_path = self.pgl_home / "state/luca-prod-anchor.json"
        try:
            self._anchor_before = anchor_path.read_bytes()
            self._anchor_existed = True
        except FileNotFoundError:
            self._anchor_before = None
            self._anchor_existed = False
        write_production_anchor(self.pgl_home, content_hash)
        self._anchor_changed = True
        # Keep the terminal lifecycle marker last.  Until it is durable, a
        # caller can still revert the local commit and recover production OLD.
        if self._transport_performed or self._lifecycle_commit_required:
            record_commit_completed(self.obs_root, clock=self.clock)

    def recover_after_local_revert(self) -> None:
        """Recover production only after the caller has restored its repo side."""

        if self._recovery_attempted:
            raise RecoveryError("[RED] manual Luca recovery was already attempted")
        self._recovery_attempted = True
        staging_error: Exception | None = None
        recovery_error: Exception | None = None
        try:
            if self.attempt.deploy_started and not self.attempt.backup_taken:
                record_deploy_aborted(self.obs_root, clock=self.clock)
            elif self.attempt.backup_taken:
                # Conservative by design: backup_taken means always restore
                # OLD, including after successful transport whose bookkeeping
                # or completion marker failed.
                try:
                    self.staging_rebuilder()
                except Exception as exc:
                    staging_error = exc
                try:
                    recover_production(
                        obs_root=self.obs_root,
                        ledger_path=self.ledger_path,
                        staging_root=self.staging_root,
                        deploy_key=self.deploy_key,
                        old_content_hash=self.old_content_hash or "",
                        command_runner=self.command_runner or _run_command,
                        production_digest=self.production_digest,
                        clock=self.clock,
                        window_id_factory=self.window_id_factory,
                        orphan_window_id=self.attempt.orphan_window_id,
                    )
                except Exception as exc:
                    recovery_error = exc
        finally:
            if self._anchor_changed:
                anchor_path = self.pgl_home / "state/luca-prod-anchor.json"
                if self._anchor_existed and self._anchor_before is not None:
                    atomic_write(anchor_path, self._anchor_before)
                else:
                    anchor_path.unlink(missing_ok=True)
                self._anchor_changed = False
        if recovery_error is not None:
            suffix = (
                f"; staging rebuild also failed: {staging_error}"
                if staging_error is not None
                else ""
            )
            raise RecoveryError(f"{recovery_error}{suffix}") from recovery_error
        if staging_error is not None:
            raise RecoveryError(
                f"[RED] Luca staging rebuild failed after production recovery: {staging_error}"
            ) from staging_error


@dataclass(frozen=True)
class LifecycleState:
    deploy_started: bool
    acceptance_succeeded: bool
    commit_completed: bool
    recovery_started: bool
    recovery_completed: bool
    deploy_aborted: bool
    resume_required: bool
    production_state: str | None
    red_reason: str | None


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    production_hash: str | None
    committed_hash: str | None
    detail: str
    expected_source: Literal["ledger", "anchor"] | None


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


class _DuplicateInstallDeclarationError(ValueError):
    def __init__(self, key: object) -> None:
        self.key = key
        super().__init__(f"duplicate install declaration: {key}")


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise _DuplicateInstallDeclarationError(key)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _run_command(
    command: Sequence[str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    arguments = tuple(command)
    timeout = (
        _ACCEPT_COMMAND_TIMEOUT_SECONDS
        if len(arguments) >= 5
        and arguments[0] == "ssh"
        and arguments[1] == "-i"
        and arguments[4:] in {("accept",), ("accept", "deletion")}
        else _COMMAND_TIMEOUT_SECONDS
    )
    try:
        return subprocess.run(
            list(arguments),
            cwd=cwd,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"command unavailable: {command[0]}: {exc}") from exc


def _checked(
    command: Sequence[str],
    *,
    cwd: Path,
    label: str,
    command_runner: CommandRunner,
) -> subprocess.CompletedProcess[str]:
    completed = command_runner(tuple(command), cwd)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"{label} failed with exit {completed.returncode}: {detail}"
        )
    return completed


def _ssh_argv(deploy_key: Path, *remote_command: str) -> tuple[str, ...]:
    return (
        "ssh",
        "-i",
        str(deploy_key),
        DEPLOY_TARGET,
        *remote_command,
    )


def _rsync_argv(deploy_key: Path) -> tuple[str, ...]:
    # rsync accepts its remote shell as one command string.  Serialize the
    # complete argv so identity paths containing spaces remain one ssh operand.
    remote_shell = shlex.join(("ssh", "-i", str(deploy_key)))
    return (
        "rsync",
        "-rpt",
        "--checksum",
        "--compress",
        "--itemize-changes",
        "--delete",
        "--partial",
        "--inplace",
        "--safe-links",
        "--rsync-path=deploy transfer",
        "-e",
        remote_shell,
        "./pack/",
        PRODUCTION_PACK_DESTINATION,
    )


def _now_iso(clock: Clock) -> str:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("deployment clock must return an offset-aware datetime")
    return value.replace(microsecond=0).isoformat()


def _placeholder_declarations(path: Path) -> frozenset[str]:
    try:
        value = yaml.load(
            read_bytes_nofollow(path).decode("utf-8"),
            Loader=_UniqueSafeLoader,
        )
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise RuntimeError(f"invalid Luca install file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Luca install file must be a mapping: {path}")
    placeholders = value.get("placeholders")
    if not isinstance(placeholders, dict):
        raise RuntimeError(f"Luca install placeholders must be a mapping: {path}")
    declarations = frozenset(placeholders)
    if any(not isinstance(item, str) or not item for item in declarations):
        raise RuntimeError(f"Luca install placeholder names are invalid: {path}")
    return declarations


def require_install_equivalence(
    staging_install: Path, known_production_install: Path
) -> None:
    staging = _placeholder_declarations(staging_install)
    production = _placeholder_declarations(known_production_install)
    if staging != production or staging != FROZEN_LUCA_PLACEHOLDER_DECLARATIONS:
        raise RuntimeError(
            "Luca staging/production placeholder declaration sets differ from the frozen set"
        )


def require_collector_path_agreement(
    pgl_home: Path,
    config_path: Path = DEFAULT_LUCA_COLLECTOR_CONFIG,
) -> tuple[Path, Path]:
    """Pin nightly journal/ledger writes to the collector's exact state files."""

    expected_root = Path(pgl_home).expanduser().resolve(strict=False)
    config = load_luca_collector_config(config_path)
    collector_root = config.obs_root.expanduser().resolve(strict=False)
    expected_journal = expected_root / journal.INTENT_JOURNAL_RELATIVE_PATH
    collector_journal = collector_root / journal.INTENT_JOURNAL_RELATIVE_PATH
    if collector_journal != expected_journal:
        raise RuntimeError(
            "PGL_HOME and Luca collector journal paths disagree: "
            f"nightly={expected_journal} collector={collector_journal}"
        )
    configured_ledger = config.source.exclude_session_ledger
    expected_ledger = expected_root / ACCEPTANCE_LEDGER_RELATIVE_PATH
    if configured_ledger is None:
        raise RuntimeError("Luca collector config requires an acceptance ledger")
    collector_ledger = configured_ledger.expanduser().resolve(strict=False)
    if collector_ledger != expected_ledger:
        raise RuntimeError(
            "PGL_HOME and Luca collector ledger paths disagree: "
            f"nightly={expected_ledger} collector={collector_ledger}"
        )
    return collector_root, collector_ledger


def recent_private_activity(
    *,
    clock: Clock = lambda: datetime.now(_JST),
    config_path: Path = DEFAULT_LUCA_COLLECTOR_CONFIG,
    adapter_factory: Callable[[str], SSHAdapter] = SSHAdapter,
) -> bool:
    """Hold for recent attributed owner DM activity, excluding acceptance traffic."""

    now = clock()
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise RuntimeError("private-activity clock must be offset-aware")
    config = load_luca_collector_config(config_path)
    if config.source.exclude_session_ledger is None:
        raise RuntimeError("private-activity check requires an acceptance ledger")
    excluded_sessions = acceptance_ledger.load_ledger(
        config.source.exclude_session_ledger,
        config.obs_root,
    ).session_ids
    end = int(now.timestamp()) + 1
    start = end - PRIVATE_ACTIVITY_HOLD_MINUTES * 60
    turns = adapter_factory(config.source.ssh_host).fetch_turns(
        start,
        end,
        config.source.sources,
    )
    sessions: dict[str, list[object]] = defaultdict(list)
    for turn in turns:
        sessions[turn.session_id].append(turn)
    for session_id, session_turns in sessions.items():
        first = session_turns[0]
        if luca_rules.session_is_excluded(
            session_id,
            first.session_key,
            config.source.exclude_session_prefixes,
        ) or session_id in excluded_sessions:
            continue
        allowed, _outsider, _reason = luca_rules.session_attribution(
            session_turns,
            config.source.owner_uids,
            config.source.voice_enabled,
        )
        if allowed and any(turn.role == "user" for turn in session_turns):
            return True
    return False


def _parse_acceptance_body(completed: subprocess.CompletedProcess[str]) -> tuple[str, ...]:
    if not completed.stdout.strip():
        raise RuntimeError("dispatcher accept returned no body")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("dispatcher accept returned invalid JSON") from exc
    if not isinstance(value, list) or not value:
        raise RuntimeError("dispatcher accept must return a non-empty UUID array")
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError("dispatcher accept UUID array contains a non-string value")
        try:
            canonical = str(uuid.UUID(item))
        except ValueError as exc:
            raise RuntimeError("dispatcher accept returned a non-UUID value") from exc
        if canonical != item:
            raise RuntimeError("dispatcher accept returned a non-canonical UUID")
    return tuple(value)


def _accept_with_persistence(
    *,
    cwd: Path,
    obs_root: Path,
    ledger_path: Path,
    deploy_key: Path,
    command_runner: CommandRunner,
    clock: Clock,
    window_id_factory: Callable[[], uuid.UUID],
    attempt: DeployAttempt | None = None,
    acceptance_mode: AcceptanceMode = "standard",
) -> tuple[tuple[str, ...], str]:
    window_id = str(window_id_factory())
    journal.append_acceptance_window_open(
        obs_root,
        ts=_now_iso(clock),
        window_id=window_id,
    )
    if attempt is not None:
        attempt.orphan_window_id = window_id
    accept_command = (
        ("accept", "deletion")
        if acceptance_mode == "deletion"
        else ("accept",)
    )
    accepted = _checked(
        _ssh_argv(deploy_key, *accept_command),
        cwd=cwd,
        label="dispatcher accept",
        command_runner=command_runner,
    )
    session_ids = _parse_acceptance_body(accepted)
    acceptance_ledger.append_acceptance_sessions(
        ledger_path,
        list(session_ids),
        recorded_at=_now_iso(clock),
    )
    journal.append_acceptance_window_close(
        obs_root,
        ts=_now_iso(clock),
        window_id=window_id,
    )
    if attempt is not None:
        attempt.orphan_window_id = None
    return session_ids, window_id


def deploy_and_accept(
    *,
    pgl_home: Path,
    obs_root: Path,
    staging_root: Path,
    known_production_install: Path,
    ledger_path: Path,
    content_hash: str,
    deploy_key: Path,
    command_runner: CommandRunner = _run_command,
    killswitch_check: KillswitchCheck | None = None,
    private_activity_check: PrivateActivityCheck = recent_private_activity,
    clock: Clock = lambda: datetime.now(_JST),
    window_id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    attempt: DeployAttempt | None = None,
    acceptance_mode: AcceptanceMode = "standard",
) -> DeployResult:
    """Run frozen e2 ordering; classify failures for the outer recovery flow."""

    active_attempt = attempt or DeployAttempt()
    if acceptance_mode not in {"standard", "deletion"}:
        raise DeployError(
            "unknown dispatcher acceptance mode",
            phase="preflight",
            backup_completed=active_attempt.backup_taken,
        )
    if active_attempt.backup_taken:
        raise DeployError(
            "backup already taken; production recovery is required before another deploy",
            phase="resume-blocked",
            backup_completed=True,
        )
    if _CONTENT_HASH.fullmatch(content_hash) is None:
        raise DeployError(
            "staging content_hash must be 64 lowercase hex characters",
            phase="preflight",
            backup_completed=active_attempt.backup_taken,
        )
    check = killswitch_check or (lambda: check_killswitch(pgl_home))
    phase = PRIVATE_ACTIVITY_PREFLIGHT_PHASE
    try:
        if private_activity_check():
            raise PrivateActivityHold(
                f"Luca deploy failed during {phase}: private activity observed within "
                f"{PRIVATE_ACTIVITY_HOLD_MINUTES} minutes",
                phase=phase,
                backup_completed=active_attempt.backup_taken,
            )
        phase = "killswitch-before-backup"
        check()
        phase = "journal-deploy-started"
        if not active_attempt.deploy_started:
            journal.append_deploy_started(obs_root, ts=_now_iso(clock))
            active_attempt.deploy_started = True
        phase = "backup"
        _checked(
            _ssh_argv(deploy_key, "deploy", "backup"),
            cwd=staging_root,
            label="dispatcher backup",
            command_runner=command_runner,
        )
        active_attempt.backup_taken = True
        phase = "install-equivalence"
        require_install_equivalence(
            staging_root / "install.yml", known_production_install
        )
        phase = "transfer"
        _checked(
            _rsync_argv(deploy_key),
            cwd=staging_root,
            label="rsync transfer",
            command_runner=command_runner,
        )
        phase = "promote"
        _checked(
            _ssh_argv(deploy_key, "deploy", "promote", content_hash),
            cwd=staging_root,
            label="dispatcher promote",
            command_runner=command_runner,
        )
        phase = "killswitch-before-restart"
        check()
        phase = "restart"
        _checked(
            _ssh_argv(deploy_key, "deploy", "restart"),
            cwd=staging_root,
            label="dispatcher restart",
            command_runner=command_runner,
        )
        phase = PRIVATE_ACTIVITY_PHASE
        if private_activity_check():
            raise PrivateActivityHold(
                f"Luca deploy failed during {phase}: private activity observed within "
                f"{PRIVATE_ACTIVITY_HOLD_MINUTES} minutes",
                phase=phase,
                backup_completed=active_attempt.backup_taken,
            )
        phase = "acceptance"
        session_ids, window_id = _accept_with_persistence(
            cwd=staging_root,
            obs_root=obs_root,
            ledger_path=ledger_path,
            deploy_key=deploy_key,
            command_runner=command_runner,
            clock=clock,
            window_id_factory=window_id_factory,
            attempt=active_attempt,
            acceptance_mode=acceptance_mode,
        )
        phase = "journal-acceptance-succeeded"
        journal.append_acceptance_succeeded(obs_root, ts=_now_iso(clock))
        return DeployResult(
            content_hash=content_hash,
            session_ids=session_ids,
            window_id=window_id,
        )
    except DeployError:
        raise
    except Exception as exc:
        raise DeployError(
            f"Luca deploy failed during {phase}: {exc}",
            phase=phase,
            backup_completed=active_attempt.backup_taken,
        ) from exc


def recover_production(
    *,
    obs_root: Path,
    ledger_path: Path,
    staging_root: Path,
    deploy_key: Path,
    old_content_hash: str,
    command_runner: CommandRunner = _run_command,
    production_digest: ProductionDigest = fetch_luca_production_digest,
    clock: Clock = lambda: datetime.now(_JST),
    window_id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    orphan_window_id: str | None = None,
) -> tuple[str, ...]:
    """Restore/restart/verify/accept old production after outer local recovery."""

    # Restore cannot be accepted without a trustworthy target digest.  Reject
    # an invalid OLD anchor before making any production-side change.
    if _CONTENT_HASH.fullmatch(old_content_hash) is None:
        raise RecoveryError("[RED] recovery old content_hash is invalid")
    phase = "journal-recovery-started"
    try:
        # This marker must be durable before the first restore-side effect so a
        # process death can never be misclassified as accepted NEW next night.
        journal.append_recovery_started(obs_root, ts=_now_iso(clock))
        phase = "restore"
        _checked(
            _ssh_argv(deploy_key, "restore"),
            cwd=staging_root,
            label="dispatcher restore",
            command_runner=command_runner,
        )
        phase = "restart"
        _checked(
            _ssh_argv(deploy_key, "deploy", "restart"),
            cwd=staging_root,
            label="dispatcher recovery restart",
            command_runner=command_runner,
        )
        # Verify after restart: restored files alone do not prove which build is
        # serving.  Old-state acceptance is meaningful only after this check.
        phase = "old-hash"
        restored_hash = production_digest()
        if restored_hash != old_content_hash:
            raise RuntimeError(
                f"restored production hash mismatch: expected={old_content_hash} actual={restored_hash}"
            )
        phase = "restored-acceptance"
        session_ids, _window_id = _accept_with_persistence(
            cwd=staging_root,
            obs_root=obs_root,
            ledger_path=ledger_path,
            deploy_key=deploy_key,
            command_runner=command_runner,
            clock=clock,
            window_id_factory=window_id_factory,
        )
        if orphan_window_id is not None:
            phase = "resolve-orphan-window"
            resolve_acceptance_window(
                obs_root,
                orphan_window_id,
                clock=clock,
            )
        phase = "journal-recovery-completed"
        journal.append_recovery_completed(obs_root, ts=_now_iso(clock))
        return session_ids
    except Exception as exc:
        raise RecoveryError(
            f"[RED] Luca secondary recovery failure during {phase}: {exc}"
        ) from exc


def committed_snapshot_hash(git_show_ledger_text: str) -> str:
    validated = _validated_committed_ledger(git_show_ledger_text)
    return _committed_snapshot_hash(validated)


def _committed_snapshot_hash(validated: Mapping[str, object]) -> str:
    snapshots = validated["snapshots"]
    if not isinstance(snapshots, list):
        raise ValueError("committed Luca ledger snapshots are invalid")
    if not snapshots:
        raise ValueError("committed Luca ledger has no snapshot anchor")
    last = snapshots[-1]
    content_hash = last.get("content_hash") if isinstance(last, dict) else None
    if not isinstance(content_hash, str) or _CONTENT_HASH.fullmatch(content_hash) is None:
        raise ValueError("committed Luca ledger last snapshot has invalid content_hash")
    return content_hash


def _committed_ledger_parse_error(exc: Exception) -> str:
    detail = str(exc)
    if isinstance(exc, _DuplicateInstallDeclarationError):
        return (
            "committed Luca ledger has a duplicate key: "
            f"{exc.key}"
        )
    if isinstance(exc, TypeError) and "unhashable type:" in detail:
        return f"committed Luca ledger has an unhashable key: {detail}"
    return f"committed Luca ledger could not be parsed: {detail}"


def _validated_committed_ledger(git_show_ledger_text: str) -> dict[str, object]:
    try:
        # yaml.safe_dump never emits merge keys, so rejecting << cannot affect a
        # writer-produced ledger. Shared-list aliases can be emitted and are
        # accepted normally by this loader.
        value = yaml.load(git_show_ledger_text, Loader=_UniqueSafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError("committed Luca ledger is invalid YAML") from exc
    except (ValueError, TypeError) as exc:
        raise ValueError(_committed_ledger_parse_error(exc)) from exc
    # The production seed contains only face/phrases/schema_version. Requiring
    # snapshots would stop every bootstrap night; validate_ledger canonicalizes
    # the missing key, and forging snapshots is no lesser bar than forging the anchor.
    return validate_ledger(value, "luca")


def reconcile_production(
    git_show_ledger_text: str,
    *,
    pgl_home: Path | None = None,
    production_digest: ProductionDigest = fetch_luca_production_digest,
) -> ReconciliationResult:
    try:
        validated = _validated_committed_ledger(git_show_ledger_text)
        if validated["snapshots"]:
            expected = _committed_snapshot_hash(validated)
            expected_from_anchor = False
            expected_source: Literal["ledger", "anchor"] | None = "ledger"
        else:
            if pgl_home is None:
                raise ValueError(
                    "production anchor source unavailable for committed Luca ledger with no snapshots"
                )
            expected = read_production_anchor(pgl_home)
            expected_from_anchor = True
            expected_source = "anchor"
        production = production_digest()
        if not isinstance(production, str) or _CONTENT_HASH.fullmatch(production) is None:
            raise MirrorError("production digest helper returned a non-hash value")
    except Exception as exc:
        return ReconciliationResult("UNAVAILABLE", None, None, str(exc), None)
    if production != expected:
        return ReconciliationResult(
            "RED",
            production,
            expected,
            (
                "production digest differs from expected production anchor "
                "(no committed snapshot)"
                if expected_from_anchor
                else "production digest differs from committed Luca snapshot"
            ),
            expected_source,
        )
    return ReconciliationResult(
        "GREEN",
        production,
        expected,
        (
            "production digest matches expected from production anchor "
            "(no committed snapshot)"
            if expected_from_anchor
            else "production digest matches committed Luca snapshot"
        ),
        expected_source,
    )


def incomplete_acceptance_reason(obs_root: Path) -> str | None:
    return load_lifecycle_state(obs_root).red_reason


def load_lifecycle_state(obs_root: Path) -> LifecycleState:
    journal_path = Path(obs_root).expanduser() / journal.INTENT_JOURNAL_RELATIVE_PATH
    try:
        journal_path.lstat()
    except FileNotFoundError:
        return LifecycleState(
            deploy_started=False,
            acceptance_succeeded=False,
            commit_completed=False,
            recovery_started=False,
            recovery_completed=False,
            deploy_aborted=False,
            resume_required=False,
            production_state=None,
            red_reason=None,
        )
    except OSError as exc:
        raise journal.JournalError(
            f"cannot inspect intent journal: {journal_path}: {exc}"
        ) from exc
    journal.load_journal(obs_root)
    started = False
    accepted = False
    committed = False
    recovery_started = False
    recovered = False
    aborted = False
    for event in journal.load_event_names(obs_root):
        if event == journal.DEPLOY_STARTED_EVENT:
            if started and not (committed or recovered or aborted):
                raise journal.JournalError(
                    "deploy-started repeated before the prior attempt was resolved"
                )
            started = True
            accepted = False
            committed = False
            recovery_started = False
            recovered = False
            aborted = False
        elif event == journal.ACCEPTANCE_SUCCEEDED_EVENT:
            if (
                not started
                or committed
                or recovery_started
                or recovered
                or aborted
            ):
                raise journal.JournalError(
                    "acceptance-succeeded has no active deploy attempt"
                )
            accepted = True
        elif event == journal.COMMIT_COMPLETED_EVENT:
            if (
                not started
                or not accepted
                or committed
                or recovery_started
                or recovered
                or aborted
            ):
                raise journal.JournalError(
                    "commit-completed has no accepted active deploy attempt"
                )
            committed = True
        elif event == journal.RECOVERY_STARTED_EVENT:
            if not started or committed or recovered or aborted:
                raise journal.JournalError(
                    "recovery-started has no unresolved deploy attempt"
                )
            # A failed restore/restart remains the same unresolved deploy
            # attempt.  Each operator retry truthfully records another start;
            # only the later recovery-completed event resolves it to OLD.
            recovery_started = True
        elif event == journal.RECOVERY_COMPLETED_EVENT:
            if not started or committed or recovered or aborted:
                raise journal.JournalError(
                    "recovery-completed has no unresolved deploy attempt"
                )
            recovered = True
        elif event == journal.DEPLOY_ABORTED_EVENT:
            if (
                not started
                or accepted
                or committed
                or recovery_started
                or recovered
                or aborted
            ):
                raise journal.JournalError(
                    "deploy-aborted is not a pre-acceptance unresolved attempt"
                )
            aborted = True
        elif event == journal.ATTENDED_SUPERSEDED_EVENT:
            if (
                not started
                or not accepted
                or committed
                or recovery_started
                or recovered
                or aborted
            ):
                raise journal.JournalError(
                    "attended-superseded has no accepted NEW attempt"
                )
            # One durable transition both resolves the prior accepted NEW and
            # opens its attended successor.  A crash here is therefore the
            # same fail-closed pre-backup state as deploy-started.
            accepted = False
            committed = False
            recovery_started = False
            recovered = False
            aborted = False

    resolved = committed or recovered or aborted
    resume_required = started and not resolved
    reason: str | None = None
    if started and recovery_started and not resolved:
        reason = (
            "[RED] recovery-started has no later recovery-completed event; "
            "production state is UNKNOWN and manual escalation is required"
        )
    elif started and accepted and not resolved:
        reason = "[RED] acceptance-succeeded has no later commit-completed event"
    elif started and not resolved:
        reason = "[RED] deploy-started has no later commit-completed event"
    production_state = (
        "NEW"
        if committed or (accepted and not recovery_started and not resolved)
        else "OLD"
        if recovered or aborted
        else "UNKNOWN"
        if recovery_started and not resolved
        else None
    )
    return LifecycleState(
        deploy_started=started,
        acceptance_succeeded=accepted,
        commit_completed=committed,
        recovery_started=recovery_started,
        recovery_completed=recovered,
        deploy_aborted=aborted,
        resume_required=resume_required,
        production_state=production_state,
        red_reason=reason,
    )


def record_commit_completed(
    obs_root: Path,
    *,
    clock: Clock = lambda: datetime.now(_JST),
) -> None:
    journal.append_commit_completed(obs_root, ts=_now_iso(clock))


def record_deploy_aborted(
    obs_root: Path,
    *,
    clock: Clock = lambda: datetime.now(_JST),
) -> None:
    journal.append_deploy_aborted(obs_root, ts=_now_iso(clock))


def resolve_acceptance_window(
    obs_root: Path,
    window_id: str,
    *,
    clock: Clock = lambda: datetime.now(_JST),
) -> None:
    """Resolve one operator-confirmed orphan while retaining its exclusion span."""

    if not isinstance(window_id, str):
        raise journal.JournalError("acceptance window_id must be a canonical UUID")
    try:
        canonical = str(uuid.UUID(window_id))
    except ValueError as exc:
        raise journal.JournalError(
            "acceptance window_id must be a canonical UUID"
        ) from exc
    if canonical != window_id:
        raise journal.JournalError("acceptance window_id must be a canonical UUID")
    state = journal.load_journal(obs_root)
    if window_id not in state.open_window_ids:
        raise journal.JournalError(
            f"acceptance window_id is not currently open: {window_id}"
        )

    journal.append_acceptance_window_resolved(
        obs_root,
        ts=_now_iso(clock),
        window_id=window_id,
    )


def read_production_anchor(pgl_home: Path) -> str:
    return _read_production_anchor(pgl_home)


def write_production_anchor(pgl_home: Path, content_hash: str) -> None:
    _write_production_anchor(pgl_home, content_hash)


__all__ = [
    "AcceptanceMode",
    "AttendedSafetyTransport",
    "DEFAULT_LUCA_COLLECTOR_CONFIG",
    "ACCEPTANCE_LEDGER_RELATIVE_PATH",
    "DEPLOY_KEY",
    "DEPLOY_TARGET",
    "FROZEN_LUCA_PLACEHOLDER_DECLARATIONS",
    "KNOWN_PRODUCTION_INSTALL_RELATIVE_PATH",
    "PRIVATE_ACTIVITY_HOLD_MINUTES",
    "PRIVATE_ACTIVITY_PHASE",
    "PRIVATE_ACTIVITY_PREFLIGHT_PHASE",
    "PrivateActivityHold",
    "PRODUCTION_PACK_DESTINATION",
    "DeployError",
    "DeployAttempt",
    "DeployResult",
    "LifecycleState",
    "ReconciliationResult",
    "RecoveryError",
    "committed_snapshot_hash",
    "deploy_and_accept",
    "incomplete_acceptance_reason",
    "is_private_activity_hold",
    "load_lifecycle_state",
    "recent_private_activity",
    "read_production_anchor",
    "reconcile_production",
    "record_commit_completed",
    "record_deploy_aborted",
    "recover_production",
    "require_install_equivalence",
    "require_collector_path_agreement",
    "resolve_acceptance_window",
    "write_production_anchor",
]
