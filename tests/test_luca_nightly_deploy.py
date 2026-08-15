from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import yaml

from collectors.hermes_luca import journal
from collectors.hermes_luca.adapters import Turn
from growthlane import deploy, nightly
from tests import test_luca_dispatch as dispatch_tests
from tests.support import MACOS_RSYNC_CLIENT_SKIP_REASON


CONTENT_HASH = "a" * 64
OLD_HASH = "b" * 64
TIMESTAMP = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)
WINDOW_UUID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
RECOVERY_WINDOW_UUID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SESSION_IDS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
)


def completed(
    command: tuple[str, ...],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class LucaNightlyDeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.pgl_home = self.root / "pgl"
        self.obs_root = self.pgl_home
        self.staging = self.root / "staging"
        self.staging.mkdir(parents=True)
        (self.staging / "pack").mkdir()
        self.production_template = self.root / "known-production-install.yml"
        self.ledger_path = self.pgl_home / "state/luca-verify-sessions.jsonl"
        self.deploy_key = self.root / "deploy-key"
        self._write_install(
            self.staging / "install.yml",
            {"agent-name": "ルカ", "user": "オーナー", "owner-name": "オーナー"},
        )
        self._write_install(
            self.production_template,
            {
                "agent-name": "different-value",
                "user": "different-value",
                "owner-name": "different-value",
            },
        )
        self._seed_ledger()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_install(self, path: Path, placeholders: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 2,
                    "pack": "pack",
                    "runtime": "generic",
                    "placeholders": placeholders,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def _seed_ledger(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(
            '{"session_id":"historical-seed","recorded_at":"2026-08-01T00:00:00+09:00","origin":"seed"}\n',
            encoding="utf-8",
        )
        os.chmod(self.ledger_path, 0o600)

    def _write_collector_config(
        self,
        path: Path,
        *,
        obs_root: Path | None = None,
        ledger_path: Path | None = None,
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "face": "luca",
                    "host": "vps-hermes",
                    "speaker": "owner",
                    "obs_root": str(obs_root or self.pgl_home),
                    "source": {
                        "ssh_host": "example-vps",
                        "kind": "hermes-state-db",
                        "db_path": "~/.hermes/profiles/luca/state.db",
                        "owner_uids": {"telegram": ["owner"], "slack": ["owner"]},
                        "expected_dm_entries": {
                            "telegram": ["owner"],
                            "slack": ["dm"],
                        },
                        "sources": ["telegram", "slack", "api_server"],
                        "dm_only": True,
                        "exclude_session_prefixes": ["pgl-verify-"],
                        "exclude_session_ledger": str(
                            ledger_path or self.ledger_path
                        ),
                        "voice_enabled": True,
                    },
                    "denylist_path": "denylist.txt",
                }
            ),
            encoding="utf-8",
        )

    def _runner(
        self,
        events: list[tuple[str, ...]],
        *,
        fail_command: tuple[str, ...] | None = None,
        accept_stdout: str | None = None,
    ):
        def run(
            command: tuple[str, ...], _cwd: Path
        ) -> subprocess.CompletedProcess[str]:
            command = tuple(command)
            events.append(command)
            if fail_command is not None and command[-len(fail_command) :] == fail_command:
                return completed(command, returncode=1, stderr="synthetic failure")
            if command[-1:] == ("accept",):
                body = (
                    json.dumps(list(SESSION_IDS)) + "\n"
                    if accept_stdout is None
                    else accept_stdout
                )
                return completed(command, stdout=body)
            return completed(command)

        return run

    def _call(
        self,
        *,
        command_runner=None,
        killswitch_check=lambda: None,
        private_activity_check=lambda: False,
        attempt: deploy.DeployAttempt | None = None,
    ) -> deploy.DeployResult:
        return deploy.deploy_and_accept(
            pgl_home=self.pgl_home,
            obs_root=self.obs_root,
            staging_root=self.staging,
            known_production_install=self.production_template,
            ledger_path=self.ledger_path,
            content_hash=CONTENT_HASH,
            deploy_key=self.deploy_key,
            command_runner=command_runner or self._runner([]),
            killswitch_check=killswitch_check,
            private_activity_check=private_activity_check,
            clock=lambda: TIMESTAMP,
            window_id_factory=lambda: WINDOW_UUID,
            attempt=attempt,
        )

    def test_content_hash_preflight_rejects_empty_hash_before_side_effects(self) -> None:
        commands: list[tuple[str, ...]] = []

        with self.assertRaisesRegex(
            deploy.DeployError,
            "64 lowercase hex characters",
        ) as caught:
            deploy.deploy_and_accept(
                pgl_home=self.pgl_home,
                obs_root=self.obs_root,
                staging_root=self.staging,
                known_production_install=self.production_template,
                ledger_path=self.ledger_path,
                content_hash="",
                deploy_key=self.deploy_key,
                command_runner=self._runner(commands),
            )

        self.assertEqual(caught.exception.phase, "preflight")
        self.assertEqual(commands, [])
        self.assertFalse(
            (self.obs_root / journal.INTENT_JOURNAL_RELATIVE_PATH).exists()
        )

    def test_e2_exact_order_and_durable_window_before_accept(self) -> None:
        timeline: list[str] = []
        killswitch_calls = 0
        activity_calls = 0

        def killswitch() -> None:
            nonlocal killswitch_calls
            killswitch_calls += 1
            timeline.append(f"killswitch-{killswitch_calls}")

        def activity() -> bool:
            nonlocal activity_calls
            activity_calls += 1
            timeline.append(f"activity-{activity_calls}")
            return False

        def runner(
            command: tuple[str, ...], _cwd: Path
        ) -> subprocess.CompletedProcess[str]:
            if command[-2:] == ("deploy", "backup"):
                timeline.append("backup")
            elif command[0] == "rsync":
                timeline.append("transfer")
            elif command[-3:-1] == ("deploy", "promote"):
                timeline.append("promote")
            elif command[-2:] == ("deploy", "restart"):
                timeline.append("restart")
            elif command[-1:] == ("accept",):
                timeline.append("accept")
                state = journal.load_journal(self.obs_root)
                self.assertTrue(state.has_open_window)
                return completed(command, stdout=json.dumps(list(SESSION_IDS)))
            else:
                self.fail(f"unexpected command: {command}")
            return completed(command)

        original_started = journal.append_deploy_started
        original_open = journal.append_acceptance_window_open
        original_close = journal.append_acceptance_window_close
        original_accepted = journal.append_acceptance_succeeded
        original_equivalence = deploy.require_install_equivalence
        original_ledger = deploy.acceptance_ledger.append_acceptance_sessions

        def record(name, function):
            def wrapped(*args, **kwargs):
                timeline.append(name)
                return function(*args, **kwargs)

            return wrapped

        with (
            mock.patch.object(
                journal,
                "append_deploy_started",
                side_effect=record("deploy-started", original_started),
            ),
            mock.patch.object(
                deploy,
                "require_install_equivalence",
                side_effect=record("install-equivalence", original_equivalence),
            ),
            mock.patch.object(
                journal,
                "append_acceptance_window_open",
                side_effect=record("window-open", original_open),
            ),
            mock.patch.object(
                deploy.acceptance_ledger,
                "append_acceptance_sessions",
                side_effect=record("ledger-append", original_ledger),
            ),
            mock.patch.object(
                journal,
                "append_acceptance_window_close",
                side_effect=record("window-close", original_close),
            ),
            mock.patch.object(
                journal,
                "append_acceptance_succeeded",
                side_effect=record("acceptance-succeeded", original_accepted),
            ),
        ):
            result = self._call(
                command_runner=runner,
                killswitch_check=killswitch,
                private_activity_check=activity,
            )

        self.assertEqual(
            timeline,
            [
                "activity-1",
                "killswitch-1",
                "deploy-started",
                "backup",
                "install-equivalence",
                "transfer",
                "promote",
                "killswitch-2",
                "restart",
                "activity-2",
                "window-open",
                "accept",
                "ledger-append",
                "window-close",
                "acceptance-succeeded",
            ],
        )
        self.assertEqual(result.session_ids, SESSION_IDS)
        self.assertEqual(result.window_id, str(WINDOW_UUID))
        self.assertFalse(journal.load_journal(self.obs_root).has_open_window)

    def test_private_activity_preflight_skips_before_backup(self) -> None:
        commands: list[tuple[str, ...]] = []
        attempt = deploy.DeployAttempt()
        with self.assertRaises(deploy.DeployError) as caught:
            self._call(
                command_runner=self._runner(commands),
                private_activity_check=lambda: True,
                attempt=attempt,
            )
        self.assertEqual(caught.exception.phase, "private-activity-preflight")
        self.assertIsInstance(caught.exception, deploy.PrivateActivityHold)
        self.assertFalse(caught.exception.recovery_required)
        self.assertFalse(attempt.backup_taken)
        self.assertEqual(commands, [])
        self.assertFalse(self.obs_root.joinpath(journal.INTENT_JOURNAL_RELATIVE_PATH).exists())

    def test_missing_journal_is_a_clean_first_run_lifecycle(self) -> None:
        state = deploy.load_lifecycle_state(self.obs_root)
        self.assertFalse(state.deploy_started)
        self.assertFalse(state.acceptance_succeeded)
        self.assertFalse(state.commit_completed)
        self.assertFalse(state.resume_required)
        self.assertIsNone(state.production_state)
        self.assertIsNone(state.red_reason)

    def test_backup_failure_requires_a_truthful_outer_abort_resolution(self) -> None:
        with self.assertRaises(deploy.DeployError) as caught:
            self._call(
                command_runner=self._runner(
                    [],
                    fail_command=("deploy", "backup"),
                )
            )
        self.assertEqual(caught.exception.phase, "backup")
        self.assertFalse(caught.exception.recovery_required)
        self.assertEqual(
            journal.load_event_names(self.obs_root),
            (journal.DEPLOY_STARTED_EVENT,),
        )
        state = deploy.load_lifecycle_state(self.obs_root)
        self.assertTrue(state.resume_required)
        self.assertIn("deploy-started", state.red_reason or "")

        deploy.record_deploy_aborted(self.obs_root, clock=lambda: TIMESTAMP)
        aborted = deploy.load_lifecycle_state(self.obs_root)
        self.assertFalse(aborted.resume_required)
        self.assertTrue(aborted.deploy_aborted)
        self.assertEqual(aborted.production_state, "OLD")

    def test_late_private_activity_requires_recovery_after_backup(self) -> None:
        commands: list[tuple[str, ...]] = []
        checks = iter((False, True))
        attempt = deploy.DeployAttempt()
        with self.assertRaises(deploy.DeployError) as caught:
            self._call(
                command_runner=self._runner(commands),
                private_activity_check=lambda: next(checks),
                attempt=attempt,
            )
        self.assertEqual(caught.exception.phase, "private-activity")
        self.assertIsInstance(caught.exception, deploy.PrivateActivityHold)
        self.assertTrue(caught.exception.recovery_required)
        self.assertTrue(attempt.backup_taken)
        self.assertNotIn(("accept",), [command[-1:] for command in commands])

    def test_both_killswitch_positions_are_fail_closed(self) -> None:
        for failing_call, recovery_required in ((1, False), (2, True)):
            with self.subTest(failing_call=failing_call):
                commands: list[tuple[str, ...]] = []
                count = 0

                def check() -> None:
                    nonlocal count
                    count += 1
                    if count == failing_call:
                        raise RuntimeError("killswitch")

                with self.assertRaises(deploy.DeployError) as caught:
                    self._call(
                        command_runner=self._runner(commands),
                        killswitch_check=check,
                    )
                self.assertEqual(
                    caught.exception.phase,
                    "killswitch-before-backup"
                    if failing_call == 1
                    else "killswitch-before-restart",
                )
                self.assertEqual(
                    caught.exception.recovery_required, recovery_required
                )

    def test_failure_classification_across_backup_boundary(self) -> None:
        """Pin §3.5 rows 2-5 at their pre/post-backup recovery boundary."""

        pre_backup: list[tuple[tuple[str, ...], str]] = [
            (("deploy", "backup"), "backup"),
        ]
        post_backup: list[tuple[tuple[str, ...], str]] = [
            (("rsync-placeholder",), "unused"),
            (("deploy", "promote", CONTENT_HASH), "promote"),
            (("deploy", "restart"), "restart"),
            (("accept",), "acceptance"),
        ]
        for suffix, phase in pre_backup:
            with self.subTest(phase=phase):
                with self.assertRaises(deploy.DeployError) as caught:
                    self._call(command_runner=self._runner([], fail_command=suffix))
                self.assertEqual(caught.exception.phase, phase)
                self.assertFalse(caught.exception.recovery_required)

        for suffix, phase in post_backup[1:]:
            with self.subTest(phase=phase):
                with self.assertRaises(deploy.DeployError) as caught:
                    self._call(command_runner=self._runner([], fail_command=suffix))
                self.assertEqual(caught.exception.phase, phase)
                self.assertTrue(caught.exception.recovery_required)

        mismatched = self.production_template.read_text(encoding="utf-8").replace(
            "owner-name:", "extra-name:"
        )
        self.production_template.write_text(mismatched, encoding="utf-8")
        with self.assertRaises(deploy.DeployError) as caught:
            self._call()
        self.assertEqual(caught.exception.phase, "install-equivalence")
        self.assertTrue(caught.exception.recovery_required)

    def test_transfer_failure_and_accept_no_body_are_fail_closed(self) -> None:
        """Pin §3.5 rows 3, 5, and 10 without inventing recovery state."""

        def runner(command: tuple[str, ...], _cwd: Path):
            if command[0] == "rsync":
                return completed(command, returncode=1, stderr="transfer failed")
            return completed(command)

        with self.assertRaises(deploy.DeployError) as caught:
            self._call(command_runner=runner)
        self.assertEqual(caught.exception.phase, "transfer")
        self.assertTrue(caught.exception.recovery_required)

        ledger_before = self.ledger_path.read_bytes()
        with self.assertRaises(deploy.DeployError) as caught:
            self._call(command_runner=self._runner([], accept_stdout=""))
        self.assertEqual(caught.exception.phase, "acceptance")
        self.assertTrue(journal.load_journal(self.obs_root).has_open_window)
        self.assertEqual(self.ledger_path.read_bytes(), ledger_before)

    def test_ledger_append_failure_leaves_window_open_and_requires_recovery(self) -> None:
        """Pin §3.5 row 10 when accept returned IDs but durable append failed."""

        ledger_before = self.ledger_path.read_bytes()
        with mock.patch.object(
            deploy.acceptance_ledger,
            "append_acceptance_sessions",
            side_effect=OSError("ledger unavailable"),
        ):
            with self.assertRaises(deploy.DeployError) as caught:
                self._call()
        self.assertEqual(caught.exception.phase, "acceptance")
        self.assertTrue(caught.exception.recovery_required)
        self.assertTrue(journal.load_journal(self.obs_root).has_open_window)
        self.assertEqual(self.ledger_path.read_bytes(), ledger_before)

    def test_same_attempt_cannot_overwrite_backup_and_requires_recovery(self) -> None:
        """Pin §3.5 row 9: a taken backup can never be overwritten by retry."""

        commands: list[tuple[str, ...]] = []
        attempt = deploy.DeployAttempt(backup_taken=True)
        with self.assertRaises(deploy.DeployError) as caught:
            self._call(
                command_runner=self._runner(commands),
                attempt=attempt,
            )
        self.assertEqual(caught.exception.phase, "resume-blocked")
        self.assertTrue(caught.exception.recovery_required)
        self.assertEqual(commands, [])

    def test_install_equivalence_compares_declarations_not_values(self) -> None:
        self.assertEqual(
            deploy.FROZEN_LUCA_PLACEHOLDER_DECLARATIONS,
            frozenset({"agent-name", "user", "owner-name"}),
        )
        deploy.require_install_equivalence(
            self.staging / "install.yml", self.production_template
        )
        coordinated_extra = {
            "agent-name": "x",
            "user": "y",
            "owner-name": "z",
            "coordinated-extra": "must still fail",
        }
        self._write_install(self.staging / "install.yml", coordinated_extra)
        self._write_install(self.production_template, coordinated_extra)
        with self.assertRaisesRegex(RuntimeError, "declaration sets"):
            deploy.require_install_equivalence(
                self.staging / "install.yml", self.production_template
            )

    def test_collector_journal_and_ledger_paths_must_match_pgl_home(self) -> None:
        config = self.root / "collector.json"
        self._write_collector_config(config)
        self.assertEqual(
            deploy.require_collector_path_agreement(self.pgl_home, config),
            (self.pgl_home, self.ledger_path),
        )

        self._write_collector_config(config, obs_root=self.root / "other-home")
        with self.assertRaisesRegex(RuntimeError, "journal paths disagree"):
            deploy.require_collector_path_agreement(self.pgl_home, config)

        self._write_collector_config(config, ledger_path=self.root / "other-ledger")
        with self.assertRaisesRegex(RuntimeError, "ledger paths disagree"):
            deploy.require_collector_path_agreement(self.pgl_home, config)

    def test_rsync_remote_shell_preserves_identity_path_with_spaces(self) -> None:
        key = self.root / "keys with spaces" / "luca deploy"
        argv = deploy._rsync_argv(key)
        remote_shell = argv[argv.index("-e") + 1]
        self.assertEqual(shlex.split(remote_shell), ["ssh", "-i", str(key)])
        self.assertEqual(argv[-2:], ("./pack/", deploy.PRODUCTION_PACK_DESTINATION))

    def test_accept_rejects_noncanonical_uuid_before_ledger_append(self) -> None:
        before = self.ledger_path.read_bytes()
        with mock.patch.object(
            deploy.acceptance_ledger,
            "append_acceptance_sessions",
        ) as append:
            with self.assertRaises(deploy.DeployError) as caught:
                self._call(
                    command_runner=self._runner(
                        [],
                        accept_stdout='["AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"]',
                    )
                )
        self.assertEqual(caught.exception.phase, "acceptance")
        append.assert_not_called()
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertTrue(journal.load_journal(self.obs_root).has_open_window)

    def test_recovery_orders_restore_restart_hash_and_persisted_acceptance(self) -> None:
        """Pin §3.5 rows 3-5 recovery and row 10 restored acceptance persistence."""

        timeline: list[str] = []

        def runner(command: tuple[str, ...], _cwd: Path):
            if command[-1:] == ("restore",):
                timeline.append("restore")
            elif command[-2:] == ("deploy", "restart"):
                timeline.append("restart")
            elif command[-1:] == ("accept",):
                timeline.append("accept")
                self.assertTrue(journal.load_journal(self.obs_root).has_open_window)
                return completed(command, stdout=json.dumps(list(SESSION_IDS)))
            return completed(command)

        def digest() -> str:
            timeline.append("hash")
            return OLD_HASH

        result = deploy.recover_production(
            obs_root=self.obs_root,
            ledger_path=self.ledger_path,
            staging_root=self.staging,
            deploy_key=self.deploy_key,
            old_content_hash=OLD_HASH,
            command_runner=runner,
            production_digest=digest,
            clock=lambda: TIMESTAMP,
            window_id_factory=lambda: WINDOW_UUID,
        )
        self.assertEqual(timeline, ["restore", "restart", "hash", "accept"])
        self.assertEqual(result, SESSION_IDS)
        self.assertFalse(journal.load_journal(self.obs_root).has_open_window)
        self.assertNotIn(
            journal.ACCEPTANCE_SUCCEEDED_EVENT,
            journal.load_event_names(self.obs_root),
        )
        self.assertIn(
            journal.RECOVERY_COMPLETED_EVENT,
            journal.load_event_names(self.obs_root),
        )
        persisted = [
            json.loads(line)
            for line in self.ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [entry["session_id"] for entry in persisted[-2:]],
            list(SESSION_IDS),
        )

    def test_recovery_resolves_exact_orphan_and_marks_old_state_complete(self) -> None:
        journal.append_deploy_started(self.obs_root, ts=TIMESTAMP.isoformat())
        journal.append_acceptance_window_open(
            self.obs_root,
            ts=TIMESTAMP.isoformat(),
            window_id=str(WINDOW_UUID),
        )

        clock = lambda: TIMESTAMP
        with mock.patch.object(
            deploy,
            "resolve_acceptance_window",
            wraps=deploy.resolve_acceptance_window,
        ) as resolve:
            deploy.recover_production(
                obs_root=self.obs_root,
                ledger_path=self.ledger_path,
                staging_root=self.staging,
                deploy_key=self.deploy_key,
                old_content_hash=OLD_HASH,
                command_runner=self._runner([]),
                production_digest=lambda: OLD_HASH,
                clock=clock,
                window_id_factory=lambda: RECOVERY_WINDOW_UUID,
                orphan_window_id=str(WINDOW_UUID),
            )
        resolve.assert_called_once_with(
            self.obs_root,
            str(WINDOW_UUID),
            clock=clock,
        )

        self.assertFalse(journal.load_journal(self.obs_root).has_open_window)
        events = journal.load_event_names(self.obs_root)
        self.assertEqual(events[-2:], (
            "acceptance-window-resolved",
            journal.RECOVERY_COMPLETED_EVENT,
        ))
        state = deploy.load_lifecycle_state(self.obs_root)
        self.assertFalse(state.resume_required)
        self.assertEqual(state.production_state, "OLD")

    def test_operator_resolution_requires_exact_open_canonical_uuid_without_writes(self) -> None:
        journal.append_acceptance_window_open(
            self.obs_root,
            ts=TIMESTAMP.isoformat(),
            window_id=str(WINDOW_UUID),
        )
        original = (
            self.obs_root / journal.INTENT_JOURNAL_RELATIVE_PATH
        ).read_bytes()

        invalid_ids = (
            str(WINDOW_UUID).upper(),
            str(RECOVERY_WINDOW_UUID),
            "not-a-uuid",
        )
        for invalid_id in invalid_ids:
            with self.subTest(window_id=invalid_id):
                with self.assertRaises(journal.JournalError):
                    deploy.resolve_acceptance_window(
                        self.obs_root,
                        invalid_id,
                        clock=lambda: TIMESTAMP,
                    )
                self.assertEqual(
                    (self.obs_root / journal.INTENT_JOURNAL_RELATIVE_PATH).read_bytes(),
                    original,
                )

        deploy.resolve_acceptance_window(
            self.obs_root,
            str(WINDOW_UUID),
            clock=lambda: TIMESTAMP,
        )
        resolved = (
            self.obs_root / journal.INTENT_JOURNAL_RELATIVE_PATH
        ).read_bytes()
        self.assertNotEqual(resolved, original)
        self.assertEqual(journal.load_journal(self.obs_root).open_window_ids, ())
        with self.assertRaisesRegex(journal.JournalError, "not currently open"):
            deploy.resolve_acceptance_window(
                self.obs_root,
                str(WINDOW_UUID),
                clock=lambda: TIMESTAMP,
            )
        self.assertEqual(
            (self.obs_root / journal.INTENT_JOURNAL_RELATIVE_PATH).read_bytes(),
            resolved,
        )

    def test_failed_restore_is_durable_unknown_recovery_next_night(self) -> None:
        journal.append_deploy_started(self.obs_root, ts=TIMESTAMP.isoformat())
        journal.append_acceptance_succeeded(self.obs_root, ts=TIMESTAMP.isoformat())
        with self.assertRaisesRegex(deploy.RecoveryError, "restore"):
            deploy.recover_production(
                obs_root=self.obs_root,
                ledger_path=self.ledger_path,
                staging_root=self.staging,
                deploy_key=self.deploy_key,
                old_content_hash=OLD_HASH,
                command_runner=self._runner([], fail_command=("restore",)),
                clock=lambda: TIMESTAMP,
            )

        state = deploy.load_lifecycle_state(self.obs_root)
        self.assertTrue(state.recovery_started)
        self.assertTrue(state.resume_required)
        self.assertEqual(state.production_state, "UNKNOWN")
        self.assertIn("recovery-started", state.red_reason or "")
        stop = nightly._luca_lifecycle_stop(self.obs_root)
        self.assertIn("recovery incomplete", stop or "")
        self.assertIn("UNKNOWN", stop or "")
        self.assertNotIn("self-crash", stop or "")
        self.assertNotIn("production state NEW", stop or "")

    def test_failed_recovery_retry_can_resolve_same_attempt_old(self) -> None:
        journal.append_deploy_started(self.obs_root, ts=TIMESTAMP.isoformat())
        journal.append_acceptance_succeeded(self.obs_root, ts=TIMESTAMP.isoformat())

        with self.assertRaisesRegex(deploy.RecoveryError, "restore"):
            deploy.recover_production(
                obs_root=self.obs_root,
                ledger_path=self.ledger_path,
                staging_root=self.staging,
                deploy_key=self.deploy_key,
                old_content_hash=OLD_HASH,
                command_runner=self._runner([], fail_command=("restore",)),
                clock=lambda: TIMESTAMP,
            )

        deploy.recover_production(
            obs_root=self.obs_root,
            ledger_path=self.ledger_path,
            staging_root=self.staging,
            deploy_key=self.deploy_key,
            old_content_hash=OLD_HASH,
            command_runner=self._runner([]),
            production_digest=lambda: OLD_HASH,
            clock=lambda: TIMESTAMP,
            window_id_factory=lambda: RECOVERY_WINDOW_UUID,
        )

        events = journal.load_event_names(self.obs_root)
        self.assertEqual(events.count(journal.RECOVERY_STARTED_EVENT), 2)
        self.assertEqual(events[-1], journal.RECOVERY_COMPLETED_EVENT)
        state = deploy.load_lifecycle_state(self.obs_root)
        self.assertTrue(state.recovery_started)
        self.assertTrue(state.recovery_completed)
        self.assertFalse(state.resume_required)
        self.assertEqual(state.production_state, "OLD")
        self.assertIsNone(state.red_reason)
        self.assertEqual(journal.load_journal(self.obs_root).open_window_ids, ())

    def test_invalid_old_anchor_does_not_claim_recovery_started(self) -> None:
        journal.append_deploy_started(self.obs_root, ts=TIMESTAMP.isoformat())
        journal.append_acceptance_succeeded(self.obs_root, ts=TIMESTAMP.isoformat())
        before = (
            self.obs_root / journal.INTENT_JOURNAL_RELATIVE_PATH
        ).read_bytes()
        with self.assertRaisesRegex(deploy.RecoveryError, "old content_hash"):
            deploy.recover_production(
                obs_root=self.obs_root,
                ledger_path=self.ledger_path,
                staging_root=self.staging,
                deploy_key=self.deploy_key,
                old_content_hash="invalid",
                command_runner=self._runner([]),
            )
        self.assertEqual(
            (self.obs_root / journal.INTENT_JOURNAL_RELATIVE_PATH).read_bytes(),
            before,
        )
        state = deploy.load_lifecycle_state(self.obs_root)
        self.assertFalse(state.recovery_started)
        self.assertEqual(state.production_state, "NEW")

    def test_recovery_secondary_failures_are_loud(self) -> None:
        """Pin §3.5 row 8: every secondary recovery failure remains RED."""

        cases = (
            (("restore",), "restore"),
            (("deploy", "restart"), "restart"),
            (("accept",), "restored-acceptance"),
        )
        for suffix, phase in cases:
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(
                    deploy.RecoveryError, rf"\[RED\].*{phase}"
                ):
                    deploy.recover_production(
                        obs_root=self.obs_root,
                        ledger_path=self.ledger_path,
                        staging_root=self.staging,
                        deploy_key=self.deploy_key,
                        old_content_hash=OLD_HASH,
                        command_runner=self._runner([], fail_command=suffix),
                        production_digest=lambda: OLD_HASH,
                        clock=lambda: TIMESTAMP,
                        window_id_factory=lambda: WINDOW_UUID,
                    )
        with self.assertRaisesRegex(deploy.RecoveryError, "old-hash"):
            deploy.recover_production(
                obs_root=self.obs_root,
                ledger_path=self.ledger_path,
                staging_root=self.staging,
                deploy_key=self.deploy_key,
                old_content_hash=OLD_HASH,
                command_runner=self._runner([]),
                production_digest=lambda: CONTENT_HASH,
                clock=lambda: TIMESTAMP,
                window_id_factory=lambda: WINDOW_UUID,
            )

        commands: list[tuple[str, ...]] = []
        with self.assertRaisesRegex(deploy.RecoveryError, "old content_hash"):
            deploy.recover_production(
                obs_root=self.obs_root,
                ledger_path=self.ledger_path,
                staging_root=self.staging,
                deploy_key=self.deploy_key,
                old_content_hash="invalid",
                command_runner=self._runner(commands),
            )
        self.assertEqual(commands, [])

    def test_reconciliation_green_red_and_unavailable(self) -> None:
        ledger_text = yaml.safe_dump(
            {
                "schema_version": 1,
                "face": "luca",
                "phrases": [],
                "retired_ids": [],
                "retired_text_hashes": [],
                "history": [],
                "snapshots": [
                    {
                        "at": "2026-08-10",
                        "source_sha": "abcdef0",
                        "content_hash": CONTENT_HASH,
                    }
                ],
            }
        )
        green = deploy.reconcile_production(
            ledger_text, production_digest=lambda: CONTENT_HASH
        )
        red = deploy.reconcile_production(
            ledger_text, production_digest=lambda: OLD_HASH
        )
        unavailable = deploy.reconcile_production(
            ledger_text,
            production_digest=lambda: (_ for _ in ()).throw(OSError("offline")),
        )
        self.assertEqual(green.status, "GREEN")
        self.assertEqual(red.status, "RED")
        self.assertEqual(unavailable.status, "UNAVAILABLE")
        self.assertIsNone(unavailable.production_hash)
        corrupt = ledger_text.replace("phrases: []", "phrases: invalid")
        self.assertEqual(
            deploy.reconcile_production(
                corrupt, production_digest=lambda: CONTENT_HASH
            ).status,
            "UNAVAILABLE",
        )

    def test_lifecycle_distinguishes_pending_new_from_resolved_old_and_new(self) -> None:
        journal.append_deploy_started(self.obs_root, ts=TIMESTAMP.isoformat())
        started = deploy.load_lifecycle_state(self.obs_root)
        self.assertTrue(started.resume_required)
        self.assertIsNone(started.production_state)
        self.assertIn("[RED]", started.red_reason or "")

        journal.append_acceptance_succeeded(self.obs_root, ts=TIMESTAMP.isoformat())
        accepted = deploy.load_lifecycle_state(self.obs_root)
        self.assertIn("acceptance-succeeded", accepted.red_reason or "")
        self.assertEqual(accepted.production_state, "NEW")

        deploy.record_commit_completed(self.obs_root, clock=lambda: TIMESTAMP)
        committed = deploy.load_lifecycle_state(self.obs_root)
        self.assertFalse(committed.resume_required)
        self.assertTrue(committed.commit_completed)
        self.assertEqual(committed.production_state, "NEW")
        self.assertIsNone(committed.red_reason)

        journal.append_deploy_started(self.obs_root, ts=TIMESTAMP.isoformat())
        journal.append_acceptance_succeeded(self.obs_root, ts=TIMESTAMP.isoformat())
        journal.append_recovery_started(self.obs_root, ts=TIMESTAMP.isoformat())
        journal.append_recovery_completed(self.obs_root, ts=TIMESTAMP.isoformat())
        recovered = deploy.load_lifecycle_state(self.obs_root)
        self.assertFalse(recovered.resume_required)
        self.assertTrue(recovered.acceptance_succeeded)
        self.assertTrue(recovered.recovery_completed)
        self.assertEqual(recovered.production_state, "OLD")

    def test_lifecycle_rejects_corrupt_acceptance_window_order(self) -> None:
        journal.append_acceptance_window_close(
            self.obs_root,
            ts=TIMESTAMP.isoformat(),
            window_id=str(WINDOW_UUID),
        )
        with self.assertRaises(journal.JournalError):
            deploy.load_lifecycle_state(self.obs_root)

    def test_production_anchor_is_strict_private_and_durable(self) -> None:
        original_fsync = os.fsync
        modes: list[int] = []

        def fsync(descriptor: int) -> None:
            modes.append(os.fstat(descriptor).st_mode)
            original_fsync(descriptor)

        with mock.patch("mirror.common.os.fsync", side_effect=fsync):
            deploy.write_production_anchor(self.pgl_home, CONTENT_HASH)
        path = self.pgl_home / "state/luca-prod-anchor.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), {"content_hash": CONTENT_HASH})
        self.assertTrue(any(stat.S_ISREG(mode) for mode in modes))
        self.assertTrue(any(stat.S_ISDIR(mode) for mode in modes))

    def test_recent_private_activity_uses_owner_dm_user_attribution_only(self) -> None:
        config = self.root / "collector.json"
        config.write_text(
            json.dumps(
                {
                    "face": "luca",
                    "host": "vps-hermes",
                    "speaker": "owner",
                    "obs_root": str(self.obs_root),
                    "source": {
                        "ssh_host": "fixture-ssh",
                        "kind": "hermes-state-db",
                        "db_path": "unused",
                        "owner_uids": {
                            "telegram": ["owner"],
                            "slack": ["owner"],
                        },
                        "expected_dm_entries": {
                            "telegram": ["owner"],
                            "slack": ["DOWNER"],
                        },
                        "sources": ["telegram", "slack", "api_server"],
                        "dm_only": True,
                        "exclude_session_prefixes": ["pgl-verify-"],
                        "exclude_session_ledger": str(self.ledger_path),
                        "voice_enabled": True,
                    },
                    "denylist_path": "unused",
                }
            ),
            encoding="utf-8",
        )
        self.ledger_path.write_text(
            "".join(
                (
                    '{"session_id":"historical-seed","recorded_at":"2026-08-01T00:00:00+09:00","origin":"seed"}\n',
                    '{"session_id":"ledger-acceptance","recorded_at":"2026-08-10T03:00:00+09:00","origin":"acceptance"}\n',
                )
            ),
            encoding="utf-8",
        )
        os.chmod(self.ledger_path, 0o600)
        baseline = self.obs_root / "state/collector/luca.ledger-lines.json"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(
            '{"schema_version":1,"line_count":2,"recorded_at":"2026-08-10T03:00:00+09:00"}\n',
            encoding="utf-8",
        )
        os.chmod(baseline, 0o600)
        turns: list[Turn] = []

        class Adapter:
            def __init__(self, host: str) -> None:
                self.host = host

            def fetch_turns(self, _start, _end, sources):
                self_sources = tuple(sources)
                self_test.assertEqual(
                    self_sources, ("telegram", "slack", "api_server")
                )
                return turns

        self_test = self
        common = {
            "chat_type": "dm",
            "session_key": None,
            "content": "text",
            "timestamp": TIMESTAMP.timestamp(),
            "role": "user",
        }
        turns.append(
            Turn(
                session_id="outsider",
                source="telegram",
                user_id="outsider",
                **common,
            )
        )
        self.assertFalse(
            deploy.recent_private_activity(
                clock=lambda: TIMESTAMP,
                config_path=config,
                adapter_factory=Adapter,
            )
        )
        turns.append(
            Turn(
                session_id="ordinary-voice",
                source="api_server",
                user_id=None,
                **common,
            )
        )
        self.assertTrue(
            deploy.recent_private_activity(
                clock=lambda: TIMESTAMP,
                config_path=config,
                adapter_factory=Adapter,
            )
        )
        turns.clear()
        turns.extend(
            (
                Turn(
                    session_id="pgl-verify-window",
                    source="api_server",
                    user_id=None,
                    **common,
                ),
                Turn(
                    session_id="ledger-acceptance",
                    source="api_server",
                    user_id=None,
                    **common,
                ),
            )
        )
        self.assertFalse(
            deploy.recent_private_activity(
                clock=lambda: TIMESTAMP,
                config_path=config,
                adapter_factory=Adapter,
            )
        )
        turns.clear()
        turns.append(
            Turn(
                session_id="owner",
                source="telegram",
                user_id="owner",
                **common,
            )
        )
        self.assertTrue(
            deploy.recent_private_activity(
                clock=lambda: TIMESTAMP,
                config_path=config,
                adapter_factory=Adapter,
            )
        )

        os.chmod(self.ledger_path, 0o644)
        with self.assertRaises(deploy.acceptance_ledger.LedgerError):
            deploy.recent_private_activity(
                clock=lambda: TIMESTAMP,
                config_path=config,
                adapter_factory=Adapter,
            )
        commands: list[tuple[str, ...]] = []
        with self.assertRaises(deploy.DeployError) as caught:
            self._call(
                command_runner=self._runner(commands),
                private_activity_check=lambda: deploy.recent_private_activity(
                    clock=lambda: TIMESTAMP,
                    config_path=config,
                    adapter_factory=Adapter,
                ),
            )
        self.assertEqual(caught.exception.phase, "private-activity-preflight")
        self.assertNotIsInstance(caught.exception, deploy.PrivateActivityHold)
        self.assertFalse(caught.exception.recovery_required)
        self.assertEqual(commands, [])

    @unittest.skipUnless(dispatch_tests.HAS_REAL_RSYNC, "/usr/bin/rsync unavailable")
    @unittest.skipUnless(
        sys.platform == "darwin",
        MACOS_RSYNC_CLIENT_SKIP_REASON,
    )
    def test_real_dispatcher_surface_via_fake_ssh_and_real_rsync(self) -> None:
        fixture = dispatch_tests.LucaDispatchTests(
            "test_deploy_transfer_real_rsync_push_lands_under_pack_and_deletes_removed_entries"
        )
        fixture.setUp()
        server = thread = None
        try:
            fixture._install_real_server_rsync()
            try:
                server, thread, requests, expected_session = fixture._run_accept_server()
            except PermissionError:
                self.skipTest("loopback bind unavailable in this sandbox")
            staging = self.root / "real-staging"
            staging.mkdir()
            shutil.copytree(fixture.pack, staging / "pack")
            self._write_install(
                staging / "install.yml",
                {"agent-name": "ルカ", "user": "オーナー", "owner-name": "オーナー"},
            )
            known = self.root / "real-known-install.yml"
            self._write_install(
                known,
                {"agent-name": "x", "user": "y", "owner-name": "z"},
            )

            fake_bin = self.root / "fake-bin"
            fake_bin.mkdir()
            ssh = fake_bin / "ssh"
            ssh.write_text(
                "\n".join(
                    [
                        "#!/bin/sh",
                        'while [ "$1" = "-i" ] || [ "$1" = "-l" ]; do shift 2; done',
                        'host="$1"',
                        "shift",
                        "export PGL_LUCA_DISPATCH_TESTING=1",
                        f"export PGL_LUCA_DISPATCH_TEST_ROOT={shlex.quote(str(fixture.root))}",
                        'export SSH_ORIGINAL_COMMAND="$*"',
                        f"exec {shlex.quote(str(Path(__file__).resolve().parents[1] / 'vps/pgl-luca-dispatch'))} deploy",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            ssh.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment.get('PATH', '')}"

            def real_runner(command: tuple[str, ...], cwd: Path):
                return subprocess.run(
                    list(command),
                    cwd=cwd,
                    env=environment,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=30,
                )

            result = deploy.deploy_and_accept(
                pgl_home=self.pgl_home,
                obs_root=self.obs_root,
                staging_root=staging,
                known_production_install=known,
                ledger_path=self.ledger_path,
                content_hash=fixture.expected_hash,
                deploy_key=self.deploy_key,
                command_runner=real_runner,
                killswitch_check=lambda: None,
                private_activity_check=lambda: False,
                clock=lambda: TIMESTAMP,
                window_id_factory=lambda: WINDOW_UUID,
            )
            self.assertEqual(result.session_ids, (expected_session,))
            self.assertEqual(
                [request[2]["input"] for request in requests],
                ["deployment warm-up", "/persona mode-b", "/persona public"],
            )
            self.assertEqual(
                (fixture.install / "systemctl.argv").read_text().splitlines(),
                ["--user", "restart", "hermes-gateway-luca", "hermes-api-luca"],
            )
        finally:
            if server is not None:
                server.shutdown()
                if thread is not None:
                    thread.join(timeout=5)
                server.server_close()
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
