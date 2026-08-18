"""Synchronous whole-night orchestration."""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from applier import apply as applier_apply
from growthlane import deploy as luca_deploy
from growthlane.config import load_json_object, parse_scalar_yaml
from growthlane.faces import get_profile
from growthlane.gates import check_all
from growthlane.locking import acquire_lock, contention_message, release_lock
from growthlane.notify import Digest, send_soul_alert
from growthlane.soul import SoulError
from growthlane.tripwire import inspect
from growthlane.ucd_runtime import emit_admission_refusal
from mirror.common import read_json_nofollow
from mirror.weekly import luca_parity


_JST = timezone(timedelta(hours=9))
_REPO = Path(__file__).resolve().parents[1]
LUCA_NIGHTLY_LAUNCHD_LABEL = "ai.caty.pgl.nightly-luca"
LUCA_NIGHTLY_HOUR = 4
LUCA_NIGHTLY_MINUTE = 0
LUCA_NIGHTLY_PROGRAM = _REPO / "bin" / "pgl-nightly-luca"


class LucaNightlyError(RuntimeError):
    """A fail-closed Luca nightly stop that has already been classified."""


class _LucaRunDigest:
    """Buffer applier's abort line until private-activity treatment succeeds."""

    def __init__(self, digest: Digest) -> None:
        self._digest = digest
        self._hold_abort_line: str | None = None
        self._expected_hold_abort_line: str | None = None

    def mark_private_activity_hold(self, error: luca_deploy.DeployError) -> None:
        self._expected_hold_abort_line = f"[RED] luca: abort/revert: {error}"

    def emit(self, line: str) -> None:
        if line == self._expected_hold_abort_line:
            self._hold_abort_line = line
            return
        self._digest.emit(line)

    def flush_hold_abort(self) -> None:
        if self._hold_abort_line is not None:
            self._digest.emit(self._hold_abort_line)
            self._hold_abort_line = None

    def discard_hold_abort(self) -> None:
        self._hold_abort_line = None


def run_face(
    profile: object,
    pgl_home: Path,
    config: Mapping[str, object],
    thresholds: Mapping[str, object],
    run_date: str,
    digest: Digest,
    *,
    before_commit: Callable[[str], None] | None = None,
) -> object:
    """Keep the generic lane compatible while reserving Luca for its dispatcher."""

    if getattr(profile, "name", None) == "luca":
        raise LucaNightlyError(
            "generic pgl-nightly refuses deployment; use bin/pgl-nightly-luca"
        )
    args = (profile, pgl_home, config, thresholds, run_date, digest)
    if before_commit is None:
        return applier_apply.run_face(*args)
    return applier_apply.run_face(*args, before_commit=before_commit)


def _overlay_git(home: Path, *args: str) -> str:
    return applier_apply._git(home, *args).stdout.decode("utf-8", "replace")


def _ensure_not_ahead_of_upstream(home: Path) -> None:
    """Do not quietly retain the row-7 local-only state after the top pull."""

    upstream = _overlay_git(home, "rev-parse", "--abbrev-ref", "@{upstream}").strip()
    if not upstream:
        raise LucaNightlyError("Luca overlay has no upstream branch")
    count = _overlay_git(home, "rev-list", "--count", f"{upstream}..HEAD").strip()
    try:
        ahead = int(count)
    except ValueError as exc:
        raise LucaNightlyError("Luca overlay upstream divergence check was invalid") from exc
    if ahead:
        raise LucaNightlyError(
            f"Luca overlay HEAD is ahead of {upstream}; manual push recovery required"
        )


def _push_luca_commit(home: Path, tag: str) -> None:
    """Push explicitly without force; git itself enforces the FF branch rule."""

    branch = _overlay_git(home, "branch", "--show-current").strip()
    if not branch:
        raise LucaNightlyError("Luca overlay is detached; cannot push nightly commit")
    command = (
        "git",
        "push",
        "--atomic",
        "origin",
        f"HEAD:refs/heads/{branch}",
        f"refs/tags/{tag}:refs/tags/{tag}",
    )
    completed = subprocess.run(
        command,
        cwd=home,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise LucaNightlyError(
            f"Luca non-force atomic push failed ({' '.join(command[3:])}): {detail}"
        )


def _rollback_unperformed_e2_commit(
    home: Path,
    profile: object,
    source_head: str,
    source_tags: frozenset[str],
) -> None:
    """Remove a changed result that escaped the required e2 callback."""

    current_tags = frozenset(_overlay_git(home, "tag", "--list").splitlines())
    for tag in sorted(current_tags - source_tags):
        applier_apply._git(home, "tag", "-d", tag)
    current = _overlay_git(home, "rev-parse", "HEAD").strip()
    if source_head and current and current != source_head:
        applier_apply._git(home, "reset", "--soft", source_head)
    if source_head:
        applier_apply._git(
            home,
            "checkout",
            source_head,
            "--",
            *profile.allowlist,
        )
    restored_head = _overlay_git(home, "rev-parse", "HEAD").strip()
    dirty = applier_apply._git(
        home,
        "status",
        "--porcelain",
        "--",
        *profile.allowlist,
    ).stdout.strip()
    restored_tags = frozenset(_overlay_git(home, "tag", "--list").splitlines())
    if restored_head != source_head or dirty or restored_tags != source_tags:
        raise LucaNightlyError(
            "Luca changed-without-e2 rollback could not prove a clean source HEAD"
        )


def _luca_lifecycle_stop(pgl_home: Path) -> str | None:
    """Classify journal crash windows before treating a g0 RED as external."""

    lifecycle = luca_deploy.load_lifecycle_state(pgl_home)
    if lifecycle.resume_required and getattr(lifecycle, "production_state", None) == "UNKNOWN":
        return (
            "Luca recovery incomplete: production state UNKNOWN; "
            "manual escalation required"
        )
    if lifecycle.resume_required and lifecycle.acceptance_succeeded:
        return (
            "Luca self-crash: acceptance-succeeded has no later commit-completed; "
            "manual recovery required"
        )
    if lifecycle.resume_required:
        return (
            "Luca incomplete deploy resume detected; do not take another backup; "
            "manual recovery required"
        )
    return None


def _weekly_luca_parity(pgl_home: Path) -> str:
    marker_path = pgl_home / "reports" / "weekly" / "latest-luca.json"
    marker = read_json_nofollow(marker_path)
    if not isinstance(marker, dict):
        raise LucaNightlyError("Luca weekly marker must be an object")
    parity = marker.get("parity")
    if not isinstance(parity, str):
        raise LucaNightlyError("Luca weekly marker parity is missing or invalid")
    return parity


def _luca_deploy_arguments(
    profile: object,
    config: Mapping[str, object],
    pgl_home: Path,
    home: Path,
    collector_config_path: Path,
    obs_root: Path,
    ledger_path: Path,
    content_hash: str,
) -> dict[str, object]:
    staging_root = profile.resolve_staging_root(config)
    if staging_root is None:
        raise LucaNightlyError("Luca engine profile has no staging root")
    return {
        "pgl_home": pgl_home,
        "obs_root": obs_root,
        "staging_root": staging_root,
        # This committed fixture pins the production declaration contract.  The
        # dispatcher intentionally exposes no live install.yml read surface;
        # the runbook records that limitation and the live content-hash gate.
        "known_production_install": (
            home / luca_deploy.KNOWN_PRODUCTION_INSTALL_RELATIVE_PATH
        ),
        "ledger_path": ledger_path,
        "content_hash": content_hash,
        "deploy_key": luca_deploy.DEPLOY_KEY,
        "private_activity_check": lambda: luca_deploy.recent_private_activity(
            config_path=collector_config_path
        ),
    }


def luca_main(argv: list[str] | None = None) -> int:
    """Run the Luca-only nightly lane without ever invoking alpha."""

    parser = argparse.ArgumentParser(description="Run the Luca nightly deployment lane")
    parser.add_argument("--date", help="JST run date (YYYY-MM-DD)")
    parser.add_argument("--config-dir", default=str(_REPO / "config"))
    args = parser.parse_args(argv)
    run_date = args.date or datetime.now(_JST).date().isoformat()
    pgl_home = Path(os.environ.get("PGL_HOME", "~/.persona-growth-loop")).expanduser().resolve()
    pgl_home.mkdir(parents=True, exist_ok=True)
    os.chmod(pgl_home, 0o700)
    digest = Digest(pgl_home, run_date)
    run_digest = _LucaRunDigest(digest)
    profile = get_profile("luca")
    lock = None
    config: Mapping[str, object] | None = None
    pull_attempted = False
    pull_completed = False
    try:
        config = load_json_object(Path(args.config_dir) / "growth-luca.json")
        home = profile.resolve_home(pgl_home, config)
        # This must remain the very first Luca pipeline action.
        pull_attempted = True
        applier_apply._git(home, "pull", "--ff-only")
        pull_completed = True
        lifecycle_stop = _luca_lifecycle_stop(pgl_home)
        if lifecycle_stop is not None:
            raise LucaNightlyError(lifecycle_stop)
        _ensure_not_ahead_of_upstream(home)

        collector_config_path = Path(args.config_dir) / "obs-collector-luca.json"
        obs_root, ledger_path = luca_deploy.require_collector_path_agreement(
            pgl_home,
            collector_config_path,
        )

        committed_ledger = _overlay_git(home, "show", f"HEAD:{profile.ledger_path}")
        reconciliation = luca_deploy.reconcile_production(
            committed_ledger,
            pgl_home=pgl_home,
        )
        if reconciliation.status != "GREEN":
            raise LucaNightlyError(
                f"Luca g0 reconciliation {reconciliation.status}: {reconciliation.detail}"
            )
        if reconciliation.expected_source == "anchor":
            digest.emit(f"luca: g0 {reconciliation.detail}")

        # Shared a--a2 remains the source of CP/killswitch/mirror liveness.
        check_all(pgl_home, "luca", run_date)
        weekly_parity = _weekly_luca_parity(pgl_home)
        live_parity = luca_parity(pgl_home)
        if weekly_parity != "GREEN" or live_parity.status != "GREEN":
            raise LucaNightlyError(
                "Luca a3 parity failed: "
                f"weekly-marker={weekly_parity} "
                f"live={live_parity.status} ({live_parity.detail})"
            )
        if emit_admission_refusal(
            digest,
            "luca",
            context="nightly refused admission-bearing work",
        ) is not None:
            raise LucaNightlyError("Luca nightly refused admission-bearing work")
        thresholds = parse_scalar_yaml(Path(args.config_dir) / "evidence.yml")
        for deviation in thresholds.deviations:
            digest.emit(deviation)

        lock = acquire_lock(pgl_home, "luca")
        if lock is None:
            raise LucaNightlyError("Luca nightly: skipped: lock contention")

        attempt = luca_deploy.DeployAttempt()
        e2_performed = False
        pre_run_head = _overlay_git(home, "rev-parse", "HEAD").strip()
        if not pre_run_head:
            raise LucaNightlyError("Luca overlay HEAD could not be captured before apply")
        pre_run_tags = frozenset(_overlay_git(home, "tag", "--list").splitlines())

        def deploy_before_commit(content_hash: str) -> None:
            nonlocal e2_performed
            try:
                luca_deploy.deploy_and_accept(
                    **_luca_deploy_arguments(
                        profile,
                        config,
                        pgl_home,
                        home,
                        collector_config_path,
                        obs_root,
                        ledger_path,
                        content_hash,
                    ),
                    attempt=attempt,
                )
            except luca_deploy.DeployError as exc:
                if luca_deploy.is_private_activity_hold(exc):
                    run_digest.mark_private_activity_hold(exc)
                raise
            e2_performed = True

        try:
            result = applier_apply.run_face(
                profile,
                pgl_home,
                config,
                thresholds,
                run_date,
                run_digest,
                before_commit=deploy_before_commit,
            )
        except Exception as run_failure:
            if attempt.deploy_started and not attempt.backup_taken:
                try:
                    luca_deploy.record_deploy_aborted(obs_root)
                except Exception as abort_exc:
                    raise LucaNightlyError(
                        f"Luca pre-backup abort could not be journaled: {abort_exc}"
                    ) from abort_exc
            # run_face has already restored its allowlist.  Only a confirmed
            # backup warrants remote restoration; pre-backup failures never do.
            # This intentionally includes failures after successful acceptance
            # but before run_face returns: the local commit was reverted, so the
            # production side must return to the committed OLD hash as well.
            if attempt.backup_taken:
                staging_error: Exception | None = None
                try:
                    from mirror.staging import regenerate_staging

                    regenerate_staging(config)
                except Exception as exc:
                    staging_error = exc
                try:
                    luca_deploy.recover_production(
                        obs_root=obs_root,
                        ledger_path=ledger_path,
                        staging_root=profile.resolve_staging_root(config),
                        deploy_key=luca_deploy.DEPLOY_KEY,
                        old_content_hash=reconciliation.committed_hash or "",
                        orphan_window_id=attempt.orphan_window_id,
                    )
                except Exception as recovery_exc:
                    suffix = f"; staging rebuild also failed: {staging_error}" if staging_error else ""
                    raise LucaNightlyError(
                        "Luca recovery failed; Luca may be stopped; human escalation required: "
                        f"{recovery_exc}{suffix}"
                    ) from recovery_exc
                if staging_error is not None:
                    raise LucaNightlyError(
                        f"Luca staging rebuild failed after recovery: {staging_error}"
                    ) from staging_error
                if (
                    isinstance(run_failure, luca_deploy.DeployError)
                    and run_failure.phase == "restart"
                ):
                    raise LucaNightlyError(
                        "Luca deployment restart failed; Luca may be stopped even though "
                        "backup recovery completed"
                    ) from run_failure
            raise

        if result.changed:
            if not e2_performed:
                _rollback_unperformed_e2_commit(
                    home,
                    profile,
                    pre_run_head,
                    pre_run_tags,
                )
                raise LucaNightlyError(
                    "Luca changed commit returned without a performed e2 deployment; "
                    "commit and tag were rolled back"
                )
            if result.content_hash is None or result.tag is None:
                raise LucaNightlyError("Luca changed run omitted commit content_hash or tag")
            luca_deploy.record_commit_completed(pgl_home)
            luca_deploy.write_production_anchor(pgl_home, result.content_hash)
            _push_luca_commit(home, result.tag)
        inspect(profile, pgl_home, config, digest)
        digest.ensure_line()
        return 0
    except Exception as exc:
        if luca_deploy.is_private_activity_hold(exc):
            run_digest.discard_hold_abort()
        else:
            run_digest.flush_hold_abort()
        if pull_attempted and not pull_completed:
            try:
                lifecycle_stop = _luca_lifecycle_stop(pgl_home)
            except Exception as lifecycle_exc:
                exc = LucaNightlyError(
                    f"Luca top pull failed: {exc}; additionally lifecycle read failed: {lifecycle_exc}"
                )
            else:
                if lifecycle_stop is not None:
                    exc = LucaNightlyError(f"Luca top pull failed: {exc}; {lifecycle_stop}")
        if config is not None:
            try:
                inspect(profile, pgl_home, config, digest)
            except Exception as tripwire_exc:
                digest.emit(f"[RED] luca: additionally tripwire failed: {tripwire_exc}")
        if luca_deploy.is_private_activity_hold(exc):
            production_state = (
                "production restored" if exc.recovery_required else "production unchanged"
            )
            digest.emit(
                "luca: nightly acceptance skipped: private activity within "
                f"{luca_deploy.PRIVATE_ACTIVITY_HOLD_MINUTES} minutes; {production_state}"
            )
        else:
            digest.emit(f"[RED] luca: nightly stopped: {exc}")
        return 1
    finally:
        if lock is not None and not release_lock(lock):
            digest.emit("[WARN] luca: nightly lock directory could not be removed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic growth lane")
    parser.add_argument("--date", help="JST run date (YYYY-MM-DD)")
    parser.add_argument("--face", action="append", choices=("alpha", "luca"))
    parser.add_argument("--config-dir", default=str(_REPO / "config"))
    args = parser.parse_args(argv)
    run_date = args.date or datetime.now(_JST).date().isoformat()
    pgl_home = Path(os.environ.get("PGL_HOME", "~/.persona-growth-loop")).expanduser().resolve()
    pgl_home.mkdir(parents=True, exist_ok=True)
    os.chmod(pgl_home, 0o700)
    digest = Digest(pgl_home, run_date)
    faces = args.face or ["alpha"]
    configs: dict[str, dict[str, object]] = {}
    runnable: list[str] = []
    refused = False
    for face in faces:
        if face == "luca":
            digest.emit(
                "[RED] luca: generic pgl-nightly refuses deployment; "
                "use bin/pgl-nightly-luca"
            )
            refused = True
        if emit_admission_refusal(
            digest,
            face,
            context="nightly refused admission-bearing work",
        ) is not None:
            refused = True
            continue
        try:
            configs[face] = load_json_object(Path(args.config_dir) / f"growth-{face}.json")
            if face == "luca":
                # Keep the established read-only root-drift diagnostic even
                # though the generic mutation path is refused by run_face.
                try:
                    applier_apply._verify_manifest_or_red(
                        get_profile(face),
                        pgl_home,
                        configs[face],
                        digest,
                    )
                except Exception:
                    pass
            if not configs[face].get("classifier_argv"):
                digest.emit(
                    f"[WARNING] {face}: two-stage signal classification requirement is not fully met; patterns only"
                )
            check_all(pgl_home, face, run_date)
        except Exception as exc:
            digest.emit(f"{face}: skipped: {exc}")
        else:
            runnable.append(face)
    def run_tripwire() -> None:
        for selected_face in faces:
            selected_config = configs.get(selected_face)
            if selected_config is not None:
                inspect(get_profile(selected_face), pgl_home, selected_config, digest)

    if not runnable:
        run_tripwire()
        digest.ensure_line()
        return 1 if refused else 0
    failed = refused
    try:
        thresholds = parse_scalar_yaml(Path(args.config_dir) / "evidence.yml")
        for deviation in thresholds.deviations:
            digest.emit(deviation)
    except Exception as exc:
        digest.emit(f"nightly: abort: {exc}")
        run_tripwire()
        digest.ensure_line()
        return 1
    for face in runnable:
        try:
            lock = acquire_lock(pgl_home, face)
        except OSError as exc:
            digest.emit(
                f"[RED] {face}: nightly: skipped: lock acquisition failed "
                f"error={exc.strerror or exc}"
            )
            failed = True
            continue
        if lock is None:
            digest.emit(contention_message(pgl_home, face, "nightly"))
            failed = True
            continue
        profile = get_profile(face)
        try:
            run_face(profile, pgl_home, configs[face], thresholds, run_date, digest)
        except Exception as exc:
            digest.emit(f"{face}: lane stopped for night: {exc}")
            if isinstance(exc, SoulError):
                send_soul_alert(configs[face], face, exc, digest)
            failed = True
        finally:
            if not release_lock(lock):
                digest.emit("[WARN] nightly: lock directory could not be removed")
    run_tripwire()
    digest.ensure_line()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
