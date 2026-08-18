from __future__ import annotations

import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from typing import Any
from unittest import mock

import yaml

from applier import apply as apply_module
from growthlane import deploy, operations
from growthlane import nightly
from growthlane.faces import get_profile
from growthlane.gates import GateExplicitNo, check_cp
from growthlane.ledger import dump_ledger, empty_ledger, load_ledger, new_phrase
from growthlane.render import render_files
from growthlane.soul import write_manifest


REPO = Path(__file__).resolve().parents[1]
DISPATCH = REPO / "vps/pgl-luca-dispatch"
DAY = "2026-08-10"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
REAL_ATTENDED_TRANSPORT = deploy.AttendedSafetyTransport


def _load_dispatch_module():
    loader = importlib.machinery.SourceFileLoader(
        "issue51_luca_dispatch", str(DISPATCH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise AssertionError("dispatcher import spec unavailable")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class LucaOperationFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.pgl_home = self.root / "pgl"
        self.overlay = self.root / "overlay"
        self.staging = self.root / "staging"
        self.config_dir = self.root / "config"
        self.production = self.root / "production"
        self.backups = self.root / "backups"
        self.backup_plain = self.root / "backup-plain"
        self.profile = get_profile("luca")
        self.events: list[tuple[str, ...]] = []
        self.head_during_backup: str | None = None
        self.dirty_during_backup: bool | None = None
        self.head_during_rebuild: str | None = None
        self.dirty_during_rebuild: bool | None = None
        self.fail_once_suffix: tuple[str, ...] | None = None
        self.interrupt_once_suffix: tuple[str, ...] | None = None
        self.dispatch = _load_dispatch_module()
        self._setup()

    def close(self) -> None:
        self.temporary.cleanup()

    def _setup(self) -> None:
        (self.overlay / "persona-engine/catalogs/overlay").mkdir(parents=True)
        (self.overlay / "growth").mkdir()
        (self.overlay / "tests/luca-pack").mkdir(parents=True)
        self.staging.mkdir()
        self.config_dir.mkdir()
        self.production.mkdir()
        self.backups.mkdir()
        (self.pgl_home / "obslog/luca").mkdir(parents=True)
        (self.pgl_home / "reports/weekly").mkdir(parents=True)
        (self.pgl_home / "state").mkdir(parents=True)

        (self.overlay / "persona-engine/manifest.yml").write_text(
            "name: luca-issue51-fixture\n", encoding="utf-8"
        )
        install = {
            "schema_version": 2,
            "pack": "pack",
            "runtime": "generic",
            "placeholders": {
                "agent-name": "ルカ",
                "user": "オーナー",
                "owner-name": "オーナー",
            },
        }
        install_text = yaml.safe_dump(
            install, allow_unicode=True, sort_keys=False
        )
        (self.staging / "install.yml").write_text(install_text, encoding="utf-8")
        (self.overlay / "tests/luca-pack/install.yml").write_text(
            install_text, encoding="utf-8"
        )
        (self.overlay / self.profile.blocklist_path).write_bytes(b"")
        self._write_phrase_ledger()

        subprocess.run(["git", "init", "-q"], cwd=self.overlay, check=True)
        subprocess.run(
            ["git", "config", "user.name", "PGL Test"],
            cwd=self.overlay,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "pgl@example.invalid"],
            cwd=self.overlay,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=self.overlay, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "seed Luca injection"],
            cwd=self.overlay,
            check=True,
        )

        self.config = {
            "display_name": "テスト利用者",
            "speaker": "owner",
            "transcripts_root": str(self.root / "transcripts"),
            "overlay_home_root": str(self.overlay),
            "staging_root": str(self.staging),
            "writer_argv": [],
            "reviewer_argv": [],
            "classifier_argv": [],
        }
        self.config_path = self.config_dir / "growth-luca.json"
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False), encoding="utf-8"
        )
        self.acceptance_ledger = (
            self.pgl_home / deploy.ACCEPTANCE_LEDGER_RELATIVE_PATH
        )
        self.acceptance_ledger.write_text(
            '{"session_id":"historical-seed","recorded_at":"2026-08-01T00:00:00+09:00","origin":"seed"}\n',
            encoding="utf-8",
        )
        self.acceptance_ledger.chmod(0o600)
        (self.config_dir / "obs-collector-luca.json").write_text(
            json.dumps(
                {
                    "face": "luca",
                    "host": "vps-hermes",
                    "speaker": "owner",
                    "obs_root": str(self.pgl_home),
                    "source": {
                        "ssh_host": "example-vps",
                        "kind": "hermes-state-db",
                        "db_path": "~/.hermes/profiles/luca/state.db",
                        "owner_uids": {
                            "telegram": ["owner"],
                            "slack": ["owner"],
                        },
                        "expected_dm_entries": {
                            "telegram": ["owner"],
                            "slack": ["dm"],
                        },
                        "sources": ["telegram", "slack", "api_server"],
                        "dm_only": True,
                        "exclude_session_prefixes": ["pgl-verify-"],
                        "exclude_session_ledger": str(self.acceptance_ledger),
                        "voice_enabled": True,
                    },
                    "denylist_path": "denylist.txt",
                }
            ),
            encoding="utf-8",
        )
        (self.pgl_home / "gates.yml").write_text(
            "cp2_in_force: true\n"
            "decided_by: owner\n"
            "ref: cp2\n"
            "faces:\n"
            "  luca:\n"
            "    cp3_go: true\n"
            "    decided_by: owner\n"
            "    ref: cp3\n",
            encoding="utf-8",
        )
        (self.pgl_home / "reports/weekly/latest-luca.json").write_text(
            json.dumps({"generated_at": DAY}), encoding="utf-8"
        )
        write_manifest(
            self.profile,
            self.pgl_home,
            self.config,
            [self.overlay / "persona-engine/manifest.yml"],
        )
        (self.pgl_home / "KILLSWITCH").write_text(
            "mode: freeze\n", encoding="utf-8"
        )
        self._reset_production_from_repo()

    def _phrase_ledger(self):
        ledger = empty_ledger("luca")
        phrase = new_phrase(
            "p-0001",
            "unsafe phrase",
            {
                "first_seen": "2026-08-01",
                "window_count": 8,
                "distinct_days": 5,
                "echo_ratio": 0.0,
            },
        )
        phrase["state"] = "adopted"
        phrase["staged_at"] = "2026-08-01"
        ledger["phrases"].append(phrase)
        return ledger

    def _write_phrase_ledger(self) -> None:
        ledger = self._phrase_ledger()
        (self.overlay / self.profile.ledger_path).write_bytes(dump_ledger(ledger))
        for relative, payload in render_files(
            self.profile, ledger, DAY, "テスト利用者"
        ).items():
            (self.overlay / relative).write_bytes(payload)

    def prepare_nonempty_block_noop(self) -> None:
        ledger = self._phrase_ledger()
        ledger["phrases"][0]["state"] = "blocked"
        self._add_surviving_phrase(ledger)
        self._commit_ledger_fixture(ledger, "seed non-empty block no-op")

    def prepare_nonempty_changed_block(self) -> None:
        ledger = self._phrase_ledger()
        self._add_surviving_phrase(ledger)
        self._commit_ledger_fixture(ledger, "seed non-empty changed block")

    def _add_surviving_phrase(self, ledger: dict[str, Any]) -> None:
        survivor = new_phrase(
            "p-0002",
            "surviving phrase",
            {
                "first_seen": "2026-08-01",
                "window_count": 8,
                "distinct_days": 5,
                "echo_ratio": 0.0,
            },
        )
        survivor["state"] = "adopted"
        survivor["staged_at"] = "2026-08-01"
        ledger["phrases"].append(survivor)

    def _commit_ledger_fixture(
        self, ledger: dict[str, Any], message: str
    ) -> None:
        (self.overlay / self.profile.ledger_path).write_bytes(dump_ledger(ledger))
        for relative, payload in render_files(
            self.profile, ledger, DAY, "テスト利用者"
        ).items():
            (self.overlay / relative).write_bytes(payload)
        self.git("add", "--", *self.profile.allowlist)
        self.git("commit", "-qm", message)
        self._reset_production_from_repo()

    def make_production_empty(self) -> None:
        for relative in self.profile.render_files.values():
            pack_relative = Path(relative).relative_to("persona-engine")
            (self.production / "pack" / pack_relative).write_bytes(b"")
        self.production_hash = self._content_hash(self.production / "pack")

    def _content_hash(self, pack: Path) -> str:
        digest = hashlib.sha256()
        for relative in sorted(self.profile.render_files.values()):
            pack_relative = Path(relative).relative_to("persona-engine")
            digest.update(pack_relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update((pack / pack_relative).read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _reset_production_from_repo(self) -> None:
        pack = self.production / "pack"
        if pack.exists():
            shutil.rmtree(pack)
        shutil.copytree(self.overlay / "persona-engine", pack)
        self.production_hash = self._content_hash(pack)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.overlay,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def allowlist_bytes(self) -> dict[str, bytes]:
        return {
            relative: (self.overlay / relative).read_bytes()
            for relative in self.profile.allowlist
        }

    def _fake_persona(
        self, command: str, clone: Path, install_root: Path
    ) -> subprocess.CompletedProcess[bytes]:
        if command == "build":
            build = install_root / "build"
            build.mkdir()
            content_hash = self._content_hash(install_root / "pack")
            (build / "manifest.json").write_text(
                json.dumps({"content_hash": content_hash}), encoding="utf-8"
            )
            return subprocess.CompletedProcess(
                ["node", str(clone / "packages/core/bin/persona"), command],
                0,
                b'{"ok":true}\n',
                b"",
            )
        if command == "doctor":
            return subprocess.CompletedProcess(
                ["node", str(clone / "packages/core/bin/persona"), command],
                0,
                b'{"ok":true,"issues":[]}\n',
                b"",
            )
        raise AssertionError(command)

    def _make_backup(self) -> None:
        if self.backup_plain.exists():
            shutil.rmtree(self.backup_plain)
        shutil.copytree(self.production / "pack", self.backup_plain)
        self.backup_hash = self.production_hash
        shutil.rmtree(self.backups)
        generation = self.backups / "luca-20260810T000000000000Z-aaaaaaaa"
        (generation / "pack").mkdir(parents=True)
        shutil.copytree(
            self.production / "pack", generation / "pack", dirs_exist_ok=True
        )
        (generation / ".complete").write_bytes(b"pgl-luca-backup-v1\n")

    def runner(
        self, command: tuple[str, ...], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(command)
        self.events.append(command)
        if (
            self.interrupt_once_suffix is not None
            and command[-len(self.interrupt_once_suffix) :]
            == self.interrupt_once_suffix
        ):
            self.interrupt_once_suffix = None
            raise KeyboardInterrupt("injected manual transport interrupt")
        if (
            self.fail_once_suffix is not None
            and command[-len(self.fail_once_suffix) :] == self.fail_once_suffix
        ):
            self.fail_once_suffix = None
            return subprocess.CompletedProcess(
                command, 1, "", "injected manual transport failure"
            )
        if command[0] == "rsync":
            shutil.rmtree(self.production / "pack")
            shutil.copytree(cwd / "pack", self.production / "pack")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-2:] == ("deploy", "backup"):
            self.head_during_backup = self.git("rev-parse", "HEAD").stdout.strip()
            self.dirty_during_backup = bool(
                self.git("status", "--porcelain", "--", *self.profile.allowlist)
                .stdout.strip()
            )
            self._make_backup()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-3:-1] == ("deploy", "promote"):
            self.production_hash = command[-1]
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-2:] == ("deploy", "restart"):
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-2:] == ("accept", "deletion"):
            try:
                self.dispatch._assert_deletion_subset(
                    self.production, self.backups
                )
            except self.dispatch.DispatchError:
                return subprocess.CompletedProcess(
                    command, 1, "", "production deletion subset rejected"
                )
            return subprocess.CompletedProcess(
                command, 0, json.dumps([SESSION_ID]), ""
            )
        if command[-1:] == ("accept",):
            return subprocess.CompletedProcess(
                command, 0, json.dumps([SESSION_ID]), ""
            )
        if command[-1:] == ("restore",):
            shutil.rmtree(self.production / "pack")
            shutil.copytree(self.backup_plain, self.production / "pack")
            self.production_hash = self.backup_hash
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected transport argv: {command}")

    def transport_factory(self, **kwargs):
        def rebuild_after_local_revert() -> None:
            self.head_during_rebuild = self.git("rev-parse", "HEAD").stdout.strip()
            self.dirty_during_rebuild = bool(
                self.git("status", "--porcelain", "--", *self.profile.allowlist)
                .stdout.strip()
            )
            self.events.append(("local-staging-rebuilt",))

        kwargs["command_runner"] = self.runner
        kwargs["production_digest"] = lambda: self.production_hash
        kwargs["monotonic"] = self._production_reflection_clock
        kwargs["staging_rebuilder"] = rebuild_after_local_revert
        return REAL_ATTENDED_TRANSPORT(**kwargs)

    def _production_reflection_clock(self) -> float:
        self.events.append(("production-reflection-clock",))
        return 104.25

    def _operation_start_clock(self) -> float:
        self.events.append(("operation-start-clock",))
        return 100.0

    def run(self, operation: str) -> tuple[int, str]:
        arguments: list[str]
        if operation == "block":
            arguments = [
                "luca",
                "p-0001",
                "--date",
                DAY,
                "--config",
                str(self.config_path),
            ]
            entry = operations.block_main
        elif operation == "forget":
            arguments = [
                "luca",
                "unsafe",
                "--date",
                DAY,
                "--config",
                str(self.config_path),
            ]
            entry = operations.forget_main
        elif operation == "eject":
            (self.pgl_home / "KILLSWITCH").write_text(
                "mode: eject\n", encoding="utf-8"
            )
            arguments = [
                "luca",
                "--date",
                DAY,
                "--config",
                str(self.config_path),
            ]
            entry = operations.eject_main
        else:
            raise AssertionError(operation)
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"PGL_HOME": str(self.pgl_home)}),
            mock.patch.object(apply_module, "_run_persona", self._fake_persona),
            mock.patch.object(
                operations.luca_deploy,
                "AttendedSafetyTransport",
                side_effect=self.transport_factory,
            ),
            contextlib.redirect_stdout(output),
        ):
            result = entry(arguments)
        return result, output.getvalue()

    def prepare_rollback(self) -> str:
        tag = "overlay-snap-luca-20260810-1"
        safe_ledger = empty_ledger("luca")
        safe_render = render_files(
            self.profile, safe_ledger, DAY, "テスト利用者"
        )
        for relative, payload in safe_render.items():
            (self.overlay / relative).write_bytes(payload)
        safe_hash = self._content_hash(self.overlay / "persona-engine")
        safe_ledger["snapshots"].append(
            {
                "at": DAY,
                "parent_sha": "0" * 40,
                "tag": tag,
                "content_hash": safe_hash,
            }
        )
        (self.overlay / self.profile.ledger_path).write_bytes(
            dump_ledger(safe_ledger)
        )
        self.git("add", "--", *self.profile.allowlist)
        self.git("commit", "-qm", "seed safe rollback tag")
        self.git("tag", tag)
        self._write_phrase_ledger()
        self.git("add", "--", *self.profile.allowlist)
        self.git("commit", "-qm", "restore unsafe current state")
        self._reset_production_from_repo()
        return tag

    def prepare_increase_rollback(self) -> str:
        tag = "overlay-snap-luca-20260810-1"
        injecting_ledger = self._phrase_ledger()
        injecting_hash = self._content_hash(self.overlay / "persona-engine")
        injecting_ledger["snapshots"].append(
            {
                "at": DAY,
                "parent_sha": "0" * 40,
                "tag": tag,
                "content_hash": injecting_hash,
            }
        )
        (self.overlay / self.profile.ledger_path).write_bytes(
            dump_ledger(injecting_ledger)
        )
        self.git("add", "--", *self.profile.allowlist)
        self.git("commit", "-qm", "seed injecting rollback tag")
        self.git("tag", tag)

        safe_ledger = empty_ledger("luca")
        for relative, payload in render_files(
            self.profile, safe_ledger, DAY, "テスト利用者"
        ).items():
            (self.overlay / relative).write_bytes(payload)
        (self.overlay / self.profile.ledger_path).write_bytes(
            dump_ledger(safe_ledger)
        )
        self.git("add", "--", *self.profile.allowlist)
        self.git("commit", "-qm", "make current state non-injecting")
        self._reset_production_from_repo()
        return tag

    def run_rollback(self, tag: str) -> tuple[int, str]:
        output = io.StringIO()
        real_locked = operations._locked

        def observed_lock(*args, **kwargs):
            self.events.append(("manual-operation-lock",))
            return real_locked(*args, **kwargs)

        with (
            mock.patch.dict(os.environ, {"PGL_HOME": str(self.pgl_home)}),
            mock.patch.object(apply_module, "_run_persona", self._fake_persona),
            mock.patch.object(
                operations.luca_deploy,
                "AttendedSafetyTransport",
                side_effect=self.transport_factory,
            ),
            mock.patch.object(operations, "_locked", side_effect=observed_lock),
            mock.patch.object(
                operations.time,
                "monotonic",
                side_effect=self._operation_start_clock,
            ),
            contextlib.redirect_stdout(output),
        ):
            result = operations.rollback_main(
                [
                    "luca",
                    tag,
                    "--date",
                    DAY,
                    "--config",
                    str(self.config_path),
                ]
            )
        return result, output.getvalue()

    def assert_production_matches_repo(self) -> None:
        for relative in self.profile.render_files.values():
            pack_relative = Path(relative).relative_to("persona-engine")
            self_value = (self.overlay / relative).read_bytes()
            production_value = (
                self.production / "pack" / pack_relative
            ).read_bytes()
            if production_value != self_value:
                raise AssertionError(f"production mismatch: {relative}")

    def digest_text(self) -> str:
        return (self.pgl_home / "digest" / f"{DAY}.md").read_text(
            encoding="utf-8"
        )


class LucaDeletionTransportTests(unittest.TestCase):
    def _bare_transport(self, **overrides):
        arguments = {
            "operation": "deletion",
            "attended": True,
            "pgl_home": Path("/tmp/pgl-manual-transport-test"),
            "obs_root": Path("/tmp/pgl-manual-transport-test"),
            "staging_root": Path("/tmp/pgl-manual-staging-test"),
            "known_production_install": Path("/tmp/pgl-manual-install.yml"),
            "ledger_path": Path("/tmp/pgl-manual-ledger.jsonl"),
            "deploy_key": Path("/tmp/pgl-manual-key"),
            "staging_rebuilder": lambda: None,
        }
        arguments.update(overrides)
        return deploy.AttendedSafetyTransport(**arguments)

    def test_attended_is_a_required_runtime_property(self) -> None:
        with self.assertRaisesRegex(ValueError, "attended=True"):
            self._bare_transport(attended=False)

    def test_manual_transport_state_guards_fail_closed(self) -> None:
        with self.subTest("closed operation set"):
            with self.assertRaisesRegex(ValueError, "deletion or rollback"):
                self._bare_transport(operation="admission")

        with self.subTest("single reflection"):
            transport = self._bare_transport()
            transport.production_reflected_at = 1.0
            with self.assertRaisesRegex(RuntimeError, "cannot run twice"):
                transport.before_commit("a" * 64)

        with self.subTest("trusted old production hash"):
            digests = iter(("a" * 64, "unavailable"))
            transport = self._bare_transport(production_digest=lambda: next(digests))
            transport.configure_deletion_baseline(repo_baseline_hash="a" * 64)
            with self.assertRaisesRegex(
                deploy.DeployError, "existing production content_hash"
            ):
                transport.before_commit("a" * 64)

        with self.subTest("reflection measurement"):
            transport = self._bare_transport()
            with self.assertRaisesRegex(RuntimeError, "has not completed"):
                transport.reflection_elapsed(1.0)
            transport.production_reflected_at = 1.0
            with self.assertRaisesRegex(RuntimeError, "moved backwards"):
                transport.reflection_elapsed(2.0)

        with self.subTest("completion hash"):
            transport = self._bare_transport()
            transport.reflected_content_hash = "a" * 64
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                transport.complete("b" * 64)

        with self.subTest("single recovery"):
            transport = self._bare_transport()
            transport.recover_after_local_revert()
            with self.assertRaisesRegex(deploy.RecoveryError, "already attempted"):
                transport.recover_after_local_revert()

        with self.subTest("closed acceptance mode"):
            with self.assertRaisesRegex(
                deploy.DeployError, "unknown dispatcher acceptance mode"
            ):
                deploy.deploy_and_accept(
                    pgl_home=Path("/tmp/pgl-manual-transport-test"),
                    obs_root=Path("/tmp/pgl-manual-transport-test"),
                    staging_root=Path("/tmp/pgl-manual-staging-test"),
                    known_production_install=Path("/tmp/pgl-manual-install.yml"),
                    ledger_path=Path("/tmp/pgl-manual-ledger.jsonl"),
                    content_hash="a" * 64,
                    deploy_key=Path("/tmp/pgl-manual-key"),
                    private_activity_check=lambda: False,
                    acceptance_mode="invalid",
                )

    def test_direction_digest_toctou_is_caught_for_rollback_and_deletion(self) -> None:
        for operation in ("rollback", "deletion"):
            with self.subTest(operation=operation):
                digests = iter(("a" * 64, "b" * 64))
                commands: list[tuple[str, ...]] = []

                def runner(command, _cwd):
                    commands.append(tuple(command))
                    raise AssertionError("transport must not start after digest drift")

                transport = self._bare_transport(
                    operation=operation,
                    production_digest=lambda: next(digests),
                    command_runner=runner,
                )
                if operation == "rollback":
                    transport.configure_rollback_direction(
                        repo_baseline_hash="a" * 64,
                        target_is_monotone_non_increasing=True,
                    )
                else:
                    transport.configure_deletion_baseline(
                        repo_baseline_hash="a" * 64
                    )
                with self.assertRaisesRegex(
                    deploy.DeployError,
                    "production changed between baseline and before-commit",
                ) as caught:
                    transport.before_commit(
                        "c" * 64,
                        target_is_empty_render=operation == "deletion",
                    )
                self.assertEqual(caught.exception.phase, f"{operation}-direction")
                self.assertEqual(commands, [])

    def test_non_relaxed_transport_rechecks_real_killswitch_and_rejects_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "KILLSWITCH").write_text("mode: freeze\n", encoding="utf-8")
            commands: list[tuple[str, ...]] = []

            def runner(command, _cwd):
                commands.append(tuple(command))
                raise AssertionError("killswitch must stop before backup")

            transport = self._bare_transport(
                operation="rollback",
                pgl_home=root,
                obs_root=root,
                production_digest=lambda: "a" * 64,
                command_runner=runner,
            )
            self.assertEqual(
                transport.configure_rollback_direction(
                    repo_baseline_hash="b" * 64,
                    target_is_monotone_non_increasing=False,
                ),
                "increase",
            )
            with self.assertRaises(deploy.DeployError) as caught:
                transport.before_commit("c" * 64)
            self.assertEqual(caught.exception.phase, "killswitch-before-backup")
            self.assertIn("killswitch detected", str(caught.exception))
            self.assertEqual(commands, [])

            forced = self._bare_transport(
                operation="rollback",
                pgl_home=root,
                obs_root=root,
                production_digest=lambda: "a" * 64,
                command_runner=runner,
            )
            forced.configure_rollback_direction(
                repo_baseline_hash="b" * 64,
                target_is_monotone_non_increasing=False,
            )
            with self.assertRaisesRegex(
                deploy.DeployError, "relaxed killswitch transport is not authorized"
            ):
                forced.before_commit("c" * 64, relaxed_killswitch=True)
            self.assertEqual(commands, [])

    def test_row6_exact_target_completion_records_commit_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "state").mkdir()
            target = "a" * 64
            deploy.journal.append_deploy_started(
                root, ts="2026-08-10T01:00:00+09:00"
            )
            deploy.journal.append_acceptance_succeeded(
                root, ts="2026-08-10T01:01:00+09:00"
            )
            transport = self._bare_transport(
                pgl_home=root,
                obs_root=root,
                production_digest=lambda: target,
            )
            transport.configure_deletion_baseline(repo_baseline_hash=target)
            transport.before_commit(target)
            transport.complete(target)
            self.assertEqual(
                deploy.journal.load_event_names(root)[-1],
                deploy.journal.COMMIT_COMPLETED_EVENT,
            )
            self.assertFalse(deploy.load_lifecycle_state(root).resume_required)

    def test_deletion_operations_reflect_production_before_commit_with_killswitch_on(self) -> None:
        for operation in ("block", "forget", "eject"):
            with self.subTest(operation=operation):
                fixture = LucaOperationFixture()
                try:
                    source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
                    fixture.pgl_home.joinpath("gates.yml").unlink()
                    result, output = fixture.run(operation)
                    self.assertEqual(result, 0, output)
                    fixture.assert_production_matches_repo()
                    self.assertEqual(fixture.head_during_backup, source_head)
                    self.assertTrue(fixture.dirty_during_backup)
                    self.assertNotEqual(
                        fixture.git("rev-parse", "HEAD").stdout.strip(), source_head
                    )
                    accept_index = next(
                        index
                        for index, command in enumerate(fixture.events)
                        if command[-2:] == ("accept", "deletion")
                    )
                    backup_index = next(
                        index
                        for index, command in enumerate(fixture.events)
                        if command[-2:] == ("deploy", "backup")
                    )
                    self.assertLess(backup_index, accept_index)
                    self.assertNotIn("alpha", " ".join(" ".join(x) for x in fixture.events))
                finally:
                    fixture.close()

    def test_explicit_cp3_false_allows_and_audits_luca_deletion_operations(self) -> None:
        for operation in ("block", "forget", "eject"):
            with self.subTest(operation=operation):
                fixture = LucaOperationFixture()
                try:
                    fixture.pgl_home.joinpath("gates.yml").write_text(
                        "cp2_in_force: true\n"
                        "decided_by: owner\n"
                        "ref: cp2\n"
                        "faces:\n"
                        "  luca:\n"
                        "    cp3_go: false\n",
                        encoding="utf-8",
                    )
                    result, output = fixture.run(operation)
                    self.assertEqual(result, 0, output)
                    self.assertIn("deletion audit", output)
                    self.assertIn("cp3-not-go", output)
                    fixture.assert_production_matches_repo()
                finally:
                    fixture.close()

    def test_explicit_cp2_false_blocks_all_luca_deletion_operations(self) -> None:
        for operation in ("block", "forget", "eject"):
            with self.subTest(operation=operation):
                fixture = LucaOperationFixture()
                try:
                    fixture.pgl_home.joinpath("gates.yml").write_text(
                        "cp2_in_force: false\n",
                        encoding="utf-8",
                    )
                    before = fixture.allowlist_bytes()
                    source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
                    result, output = fixture.run(operation)
                    self.assertEqual(result, 1)
                    self.assertIn("CP-2 is not in force", output)
                    self.assertEqual(fixture.allowlist_bytes(), before)
                    self.assertEqual(
                        fixture.git("rev-parse", "HEAD").stdout.strip(),
                        source_head,
                    )
                    self.assertEqual(
                        fixture.git("status", "--porcelain").stdout,
                        "",
                    )
                    self.assertFalse(
                        any(
                            command[-2:] == ("deploy", "backup")
                            for command in fixture.events
                        )
                    )
                finally:
                    fixture.close()

    def test_dispatcher_deletion_subset_failure_reverts_repo_then_recovers_production(self) -> None:
        fixture = LucaOperationFixture()
        try:
            # Production's backup lacks candidates.txt.  The real dispatcher
            # subset helper therefore rejects the actual generated
            # ``accept deletion`` argv after transfer adds that file.
            candidates = fixture.production / "pack/catalogs/overlay/candidates.txt"
            candidates.unlink()
            source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
            before = fixture.allowlist_bytes()
            result, output = fixture.run("block")
            self.assertEqual(result, 1)
            self.assertIn("[RED]", output)
            self.assertEqual(
                fixture.git("rev-parse", "HEAD").stdout.strip(), source_head
            )
            self.assertEqual(fixture.allowlist_bytes(), before)
            deletion_accept = next(
                index
                for index, command in enumerate(fixture.events)
                if command[-2:] == ("accept", "deletion")
            )
            local_rebuild = fixture.events.index(("local-staging-rebuilt",))
            restore = next(
                index
                for index, command in enumerate(fixture.events)
                if command[-1:] == ("restore",)
            )
            self.assertLess(deletion_accept, local_rebuild)
            self.assertLess(local_rebuild, restore)
            self.assertEqual(fixture.head_during_rebuild, source_head)
            self.assertFalse(fixture.dirty_during_rebuild)
            self.assertEqual(fixture.production_hash, fixture.backup_hash)
        finally:
            fixture.close()

    def test_rollback_runs_attended_with_cp_unavailable_and_reports_r13_endpoint(self) -> None:
        fixture = LucaOperationFixture()
        try:
            tag = fixture.prepare_rollback()
            source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
            fixture.pgl_home.joinpath("gates.yml").unlink()
            result, output = fixture.run_rollback(tag)
            self.assertEqual(result, 0, output)
            self.assertNotIn("[RED]", output)
            self.assertIn("[WARN] luca: rollback audit gates=unverified", output)
            fixture.assert_production_matches_repo()
            self.assertIn(
                "R13-Luca production-stop-seconds=4.250", output
            )
            self.assertIn("endpoint=production-stops-injecting", output)
            self.assertFalse(
                any(command[-2:] == ("accept", "deletion") for command in fixture.events)
            )
            self.assertTrue(
                any(command[-1:] == ("accept",) for command in fixture.events)
            )
            accept_index = next(
                index
                for index, command in enumerate(fixture.events)
                if command[-1:] == ("accept",)
            )
            self.assertLess(
                accept_index,
                fixture.events.index(("production-reflection-clock",)),
            )
            self.assertLess(
                fixture.events.index(("operation-start-clock",)),
                fixture.events.index(("manual-operation-lock",)),
            )
            self.assertLess(
                fixture.events.index(("manual-operation-lock",)),
                next(
                    index
                    for index, command in enumerate(fixture.events)
                    if command[-2:] == ("deploy", "backup")
                ),
            )
            self.assertEqual(fixture.head_during_backup, source_head)
            self.assertTrue(fixture.dirty_during_backup)
        finally:
            fixture.close()

    def test_rollback_still_rejects_explicit_cp2_false(self) -> None:
        fixture = LucaOperationFixture()
        try:
            tag = fixture.prepare_rollback()
            fixture.pgl_home.joinpath("gates.yml").write_text(
                "cp2_in_force: false\n", encoding="utf-8"
            )
            with self.assertRaises(GateExplicitNo) as caught:
                check_cp(fixture.pgl_home, "luca")
            self.assertEqual(caught.exception.checkpoint, "cp2")
            self.assertIsNone(caught.exception.face)
            source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
            result, output = fixture.run_rollback(tag)
            self.assertEqual(result, 1)
            self.assertIn("CP-2 is not in force", output)
            self.assertEqual(
                fixture.events,
                [("operation-start-clock",), ("manual-operation-lock",)],
            )
            self.assertEqual(
                fixture.git("rev-parse", "HEAD").stdout.strip(), source_head
            )
        finally:
            fixture.close()

    def test_successful_manual_operations_finish_anchor_and_lifecycle(self) -> None:
        cases = ("block", "rollback")
        for operation in cases:
            with self.subTest(operation=operation):
                fixture = LucaOperationFixture()
                try:
                    if operation == "rollback":
                        tag = fixture.prepare_rollback()
                        result, output = fixture.run_rollback(tag)
                    else:
                        result, output = fixture.run(operation)
                    self.assertEqual(result, 0, output)
                    anchor = json.loads(
                        (fixture.pgl_home / "state/luca-prod-anchor.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(anchor, {"content_hash": fixture.production_hash})
                    lifecycle_events = deploy.journal.load_event_names(
                        fixture.pgl_home
                    )
                    self.assertEqual(
                        lifecycle_events[-1], deploy.journal.COMMIT_COMPLETED_EVENT
                    )
                    self.assertFalse(
                        deploy.load_lifecycle_state(fixture.pgl_home).resume_required
                    )
                finally:
                    fixture.close()

    def test_complete_failure_reverts_every_boundary_and_emits_no_success(self) -> None:
        fixture = LucaOperationFixture()
        try:
            source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
            source_tags = fixture.git("tag", "--list").stdout.splitlines()
            before = fixture.allowlist_bytes()
            anchor_path = fixture.pgl_home / "state/luca-prod-anchor.json"
            anchor_before = b'{  "content_hash" : "' + b"f" * 64 + b'" }\n'
            anchor_path.write_bytes(anchor_before)
            anchor_path.chmod(0o600)
            with mock.patch.object(
                deploy,
                "record_commit_completed",
                side_effect=OSError("injected completion failure"),
            ):
                result, output = fixture.run("block")
            self.assertEqual(result, 1)
            self.assertIn("[RED]", output)
            self.assertEqual(fixture.git("rev-parse", "HEAD").stdout.strip(), source_head)
            self.assertEqual(fixture.git("tag", "--list").stdout.splitlines(), source_tags)
            self.assertEqual(fixture.allowlist_bytes(), before)
            self.assertEqual(fixture.production_hash, fixture.backup_hash)
            self.assertEqual(anchor_path.read_bytes(), anchor_before)
            digest = fixture.digest_text()
            self.assertNotIn("luca: committed", digest)
            self.assertNotIn("luca: immediate block", digest)
            self.assertIn("[RED] luca: block failed", digest)
        finally:
            fixture.close()

    def test_rollback_transport_failure_reverts_then_recovers_production(self) -> None:
        fixture = LucaOperationFixture()
        try:
            tag = fixture.prepare_rollback()
            source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
            before = fixture.allowlist_bytes()
            fixture.fail_once_suffix = ("deploy", "restart")
            result, output = fixture.run_rollback(tag)
            self.assertEqual(result, 1)
            self.assertIn("[RED]", output)
            self.assertEqual(fixture.git("rev-parse", "HEAD").stdout.strip(), source_head)
            self.assertEqual(fixture.allowlist_bytes(), before)
            self.assertEqual(fixture.production_hash, fixture.backup_hash)
            failed_restart = next(
                index
                for index, command in enumerate(fixture.events)
                if command[-2:] == ("deploy", "restart")
            )
            rebuild = fixture.events.index(("local-staging-rebuilt",))
            restore = next(
                index
                for index, command in enumerate(fixture.events)
                if command[-1:] == ("restore",)
            )
            self.assertLess(failed_restart, rebuild)
            self.assertLess(rebuild, restore)
            self.assertEqual(fixture.head_during_rebuild, source_head)
            self.assertFalse(fixture.dirty_during_rebuild)
        finally:
            fixture.close()

    def test_rollback_completion_failure_resets_commit_and_recovers(self) -> None:
        fixture = LucaOperationFixture()
        try:
            tag = fixture.prepare_rollback()
            source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
            before = fixture.allowlist_bytes()
            with mock.patch.object(
                deploy,
                "record_commit_completed",
                side_effect=OSError("injected rollback completion failure"),
            ):
                result, output = fixture.run_rollback(tag)
            self.assertEqual(result, 1)
            self.assertIn("[RED]", output)
            self.assertEqual(
                fixture.git("rev-parse", "HEAD").stdout.strip(), source_head
            )
            self.assertEqual(fixture.allowlist_bytes(), before)
            self.assertEqual(fixture.production_hash, fixture.backup_hash)
            self.assertNotIn("before=", fixture.digest_text())
            rebuild = fixture.events.index(("local-staging-rebuilt",))
            restore = next(
                index
                for index, command in enumerate(fixture.events)
                if command[-1:] == ("restore",)
            )
            self.assertLess(rebuild, restore)
            self.assertEqual(fixture.head_during_rebuild, source_head)
            self.assertFalse(fixture.dirty_during_rebuild)
        finally:
            fixture.close()

    def test_pre_backup_failure_journals_deploy_aborted(self) -> None:
        fixture = LucaOperationFixture()
        try:
            source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
            before = fixture.allowlist_bytes()
            fixture.fail_once_suffix = ("deploy", "backup")
            result, output = fixture.run("block")
            self.assertEqual(result, 1)
            self.assertIn("[RED]", output)
            self.assertEqual(fixture.git("rev-parse", "HEAD").stdout.strip(), source_head)
            self.assertEqual(fixture.allowlist_bytes(), before)
            names = deploy.journal.load_event_names(fixture.pgl_home)
            self.assertEqual(
                names[-2:],
                (
                    deploy.journal.DEPLOY_STARTED_EVENT,
                    deploy.journal.DEPLOY_ABORTED_EVENT,
                ),
            )
            self.assertFalse(deploy.load_lifecycle_state(fixture.pgl_home).resume_required)
        finally:
            fixture.close()

    def test_rollback_direction_lanes_enforce_their_gate_posture(self) -> None:
        with self.subTest("reduction bypasses killswitch"):
            fixture = LucaOperationFixture()
            try:
                tag = fixture.prepare_rollback()
                fixture.pgl_home.joinpath("gates.yml").unlink()
                result, output = fixture.run_rollback(tag)
                self.assertEqual(result, 0, output)
                self.assertIn("direction=reduction", output)
                self.assertIn("endpoint=production-stops-injecting", output)
            finally:
                fixture.close()

        with self.subTest("increase refuses killswitch"):
            fixture = LucaOperationFixture()
            try:
                tag = fixture.prepare_increase_rollback()
                source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
                result, output = fixture.run_rollback(tag)
                self.assertEqual(result, 1)
                self.assertIn("direction=increase requires full gates", output)
                self.assertNotIn("endpoint=production-stops-injecting", output)
                self.assertFalse(
                    any(
                        command[-2:] == ("deploy", "backup")
                        for command in fixture.events
                    )
                )
                self.assertEqual(
                    fixture.git("rev-parse", "HEAD").stdout.strip(), source_head
                )
            finally:
                fixture.close()

        with self.subTest("increase proceeds only with full green gates"):
            fixture = LucaOperationFixture()
            try:
                tag = fixture.prepare_increase_rollback()
                fixture.pgl_home.joinpath("KILLSWITCH").unlink()
                result, output = fixture.run_rollback(tag)
                self.assertEqual(result, 0, output)
                fixture.assert_production_matches_repo()
                self.assertNotIn("endpoint=production-stops-injecting", output)
                self.assertTrue(
                    any(command[-1:] == ("accept",) for command in fixture.events)
                )
            finally:
                fixture.close()

    def test_luca_noop_requires_production_equality(self) -> None:
        with self.subTest("baseline-equal non-empty deletion no-op remains allowed"):
            fixture = LucaOperationFixture()
            try:
                fixture.prepare_nonempty_block_noop()
                before = fixture.allowlist_bytes()
                source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
                fixture.events.clear()
                result, output = fixture.run("block")
                self.assertEqual(result, 0, output)
                self.assertIn("block no-op", output)
                self.assertEqual(fixture.allowlist_bytes(), before)
                self.assertEqual(
                    fixture.git("rev-parse", "HEAD").stdout.strip(), source_head
                )
                self.assertTrue(
                    any(
                        (fixture.overlay / relative).read_bytes()
                        for relative in fixture.profile.render_files.values()
                    )
                )
                self.assertFalse(
                    any(
                        command[-2:] == ("deploy", "backup")
                        for command in fixture.events
                    )
                )
            finally:
                fixture.close()

        with self.subTest("baseline-equal changed deletion may keep surviving render"):
            fixture = LucaOperationFixture()
            try:
                fixture.prepare_nonempty_changed_block()
                source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
                fixture.events.clear()
                result, output = fixture.run("block")
                self.assertEqual(result, 0, output)
                fixture.assert_production_matches_repo()
                self.assertNotEqual(
                    fixture.git("rev-parse", "HEAD").stdout.strip(), source_head
                )
                self.assertTrue(
                    any(
                        (fixture.overlay / relative).read_bytes()
                        for relative in fixture.profile.render_files.values()
                    )
                )
                self.assertTrue(
                    any(
                        command[-2:] == ("deploy", "backup")
                        for command in fixture.events
                    )
                )
                self.assertTrue(
                    any(
                        command[-2:] == ("accept", "deletion")
                        for command in fixture.events
                    )
                )
                self.assertFalse(
                    any(
                        command[-1:] == ("accept",)
                        for command in fixture.events
                    )
                )
            finally:
                fixture.close()

        with self.subTest("diverged non-empty deletion no-op refuses under killswitch"):
            fixture = LucaOperationFixture()
            try:
                fixture.prepare_nonempty_block_noop()
                fixture.make_production_empty()
                before = fixture.allowlist_bytes()
                source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
                fixture.events.clear()
                result, output = fixture.run("block")
                self.assertEqual(result, 1)
                self.assertIn("production divergence", output)
                self.assertIn("full-gate reconciliation refused", output)
                self.assertIn("pgl-eject remains available", output)
                self.assertEqual(fixture.allowlist_bytes(), before)
                self.assertEqual(
                    fixture.git("rev-parse", "HEAD").stdout.strip(), source_head
                )
                self.assertEqual(
                    fixture.git("status", "--porcelain").stdout, ""
                )
                self.assertFalse(
                    any(
                        command[-2:] == ("deploy", "backup")
                        for command in fixture.events
                    )
                )

                fixture.events.clear()
                eject_result, eject_output = fixture.run("eject")
                self.assertEqual(eject_result, 0, eject_output)
                fixture.assert_production_matches_repo()
                self.assertTrue(
                    all(
                        (fixture.overlay / relative).read_bytes() == b""
                        for relative in fixture.profile.render_files.values()
                    )
                )
            finally:
                fixture.close()

        with self.subTest("full-green deletion reconciliation may converge"):
            fixture = LucaOperationFixture()
            try:
                fixture.prepare_nonempty_block_noop()
                fixture.make_production_empty()
                fixture.pgl_home.joinpath("KILLSWITCH").unlink()
                fixture.events.clear()
                result, output = fixture.run("block")
                self.assertEqual(result, 0, output)
                self.assertIn("block no-op", output)
                fixture.assert_production_matches_repo()
                self.assertTrue(
                    any(command[-1:] == ("accept",) for command in fixture.events)
                )
                self.assertFalse(
                    any(
                        command[-2:] == ("accept", "deletion")
                        for command in fixture.events
                    )
                )
            finally:
                fixture.close()

        with self.subTest("rollback no-op transports or refuses divergence"):
            fixture = LucaOperationFixture()
            try:
                tag = fixture.prepare_rollback()
                first_result, first_output = fixture.run_rollback(tag)
                self.assertEqual(first_result, 0, first_output)
                shutil.rmtree(fixture.production / "pack")
                shutil.copytree(fixture.backup_plain, fixture.production / "pack")
                fixture.production_hash = fixture.backup_hash
                fixture.pgl_home.joinpath("KILLSWITCH").unlink()
                fixture.events.clear()
                result, output = fixture.run_rollback(tag)
                self.assertEqual(result, 0, output)
                self.assertIn("rollback no-op", output)
                self.assertNotIn("endpoint=production-stops-injecting", output)
                fixture.assert_production_matches_repo()
                self.assertTrue(
                    any(
                        command[-2:] == ("deploy", "backup")
                        for command in fixture.events
                    )
                )
            finally:
                fixture.close()

    def test_row6_attended_rollback_supersedes_and_keeps_journal_parseable(self) -> None:
        fixture = LucaOperationFixture()
        try:
            tag = fixture.prepare_rollback()
            deploy.journal.append_deploy_started(
                fixture.pgl_home, ts="2026-08-10T01:00:00+09:00"
            )
            deploy.journal.append_acceptance_succeeded(
                fixture.pgl_home, ts="2026-08-10T01:01:00+09:00"
            )
            self.assertTrue(deploy.load_lifecycle_state(fixture.pgl_home).resume_required)
            result, output = fixture.run_rollback(tag)
            self.assertEqual(result, 0, output)
            names = deploy.journal.load_event_names(fixture.pgl_home)
            self.assertEqual(
                names[:3],
                (
                    deploy.journal.DEPLOY_STARTED_EVENT,
                    deploy.journal.ACCEPTANCE_SUCCEEDED_EVENT,
                    deploy.journal.ATTENDED_SUPERSEDED_EVENT,
                ),
            )
            lifecycle = deploy.load_lifecycle_state(fixture.pgl_home)
            self.assertFalse(lifecycle.resume_required)
            self.assertEqual(names[-1], deploy.journal.COMMIT_COMPLETED_EVENT)
            self.assertIsNone(nightly._luca_lifecycle_stop(fixture.pgl_home))
        finally:
            fixture.close()

    def test_row6_successor_pre_backup_failure_is_aborted_and_parseable(self) -> None:
        fixture = LucaOperationFixture()
        try:
            tag = fixture.prepare_rollback()
            row6_production_hash = fixture.production_hash
            deploy.journal.append_deploy_started(
                fixture.pgl_home, ts="2026-08-10T01:00:00+09:00"
            )
            deploy.journal.append_acceptance_succeeded(
                fixture.pgl_home, ts="2026-08-10T01:01:00+09:00"
            )
            fixture.fail_once_suffix = ("deploy", "backup")
            result, output = fixture.run_rollback(tag)
            self.assertEqual(result, 1)
            self.assertIn("[RED]", output)
            names = deploy.journal.load_event_names(fixture.pgl_home)
            self.assertEqual(
                names,
                (
                    deploy.journal.DEPLOY_STARTED_EVENT,
                    deploy.journal.ACCEPTANCE_SUCCEEDED_EVENT,
                    deploy.journal.ATTENDED_SUPERSEDED_EVENT,
                    deploy.journal.DEPLOY_ABORTED_EVENT,
                ),
            )
            lifecycle = deploy.load_lifecycle_state(fixture.pgl_home)
            self.assertFalse(lifecycle.resume_required)
            self.assertTrue(lifecycle.deploy_aborted)
            self.assertEqual(lifecycle.production_state, "OLD")
            self.assertEqual(fixture.production_hash, row6_production_hash)
            journal_lines = (
                fixture.pgl_home / deploy.journal.INTENT_JOURNAL_RELATIVE_PATH
            ).read_text(encoding="utf-8").splitlines()
            superseded = json.loads(journal_lines[2])
            self.assertEqual(superseded["operation"], "rollback")
            self.assertEqual(
                superseded["production_hash"], row6_production_hash
            )
            self.assertIsNone(nightly._luca_lifecycle_stop(fixture.pgl_home))
        finally:
            fixture.close()

    def test_crash_immediately_after_supersession_opens_unresolved_successor(self) -> None:
        fixture = LucaOperationFixture()
        try:
            deploy.journal.append_deploy_started(
                fixture.pgl_home, ts="2026-08-10T01:00:00+09:00"
            )
            deploy.journal.append_acceptance_succeeded(
                fixture.pgl_home, ts="2026-08-10T01:01:00+09:00"
            )
            deploy.journal.append_attended_superseded(
                fixture.pgl_home,
                ts="2026-08-10T01:02:00+09:00",
                operation="rollback",
                production_hash=fixture.production_hash,
            )
            lifecycle = deploy.load_lifecycle_state(fixture.pgl_home)
            self.assertTrue(lifecycle.resume_required)
            self.assertTrue(lifecycle.deploy_started)
            self.assertFalse(lifecycle.acceptance_succeeded)
            self.assertIsNone(lifecycle.production_state)
            stop = nightly._luca_lifecycle_stop(fixture.pgl_home)
            self.assertIsNotNone(stop)
            self.assertIn("incomplete deploy resume", stop or "")
        finally:
            fixture.close()

    def test_unclassifiable_attempt_still_fails_closed(self) -> None:
        fixture = LucaOperationFixture()
        try:
            tag = fixture.prepare_rollback()
            deploy.journal.append_deploy_started(
                fixture.pgl_home, ts="2026-08-10T01:00:00+09:00"
            )
            deploy.journal.append_recovery_started(
                fixture.pgl_home, ts="2026-08-10T01:01:00+09:00"
            )
            result, output = fixture.run_rollback(tag)
            self.assertEqual(result, 1)
            self.assertIn("cannot be classified", output)
            self.assertFalse(
                any(
                    command[-2:] == ("deploy", "backup")
                    for command in fixture.events
                )
            )
            self.assertIn("manual escalation", nightly._luca_lifecycle_stop(fixture.pgl_home) or "")
        finally:
            fixture.close()

    def test_typed_cp_outcomes_distinguish_no_from_unavailable(self) -> None:
        with self.subTest("explicit cp3 no is structural and reduction-audited"):
            fixture = LucaOperationFixture()
            try:
                tag = fixture.prepare_rollback()
                fixture.pgl_home.joinpath("gates.yml").write_text(
                    "cp2_in_force: true\n"
                    "decided_by: owner\n"
                    "ref: cp2\n"
                    "faces:\n"
                    "  luca:\n"
                    "    cp3_go: false\n",
                    encoding="utf-8",
                )
                with self.assertRaises(GateExplicitNo) as caught:
                    check_cp(fixture.pgl_home, "luca")
                self.assertEqual(caught.exception.checkpoint, "cp3")
                self.assertEqual(caught.exception.face, "luca")
                result, output = fixture.run_rollback(tag)
                self.assertEqual(result, 0, output)
                self.assertIn(
                    "rollback audit gates=unverified(cp3-not-go)", output
                )
            finally:
                fixture.close()

        with self.subTest("explicit cp3 no blocks increase through full gates"):
            fixture = LucaOperationFixture()
            try:
                tag = fixture.prepare_increase_rollback()
                fixture.pgl_home.joinpath("KILLSWITCH").unlink()
                fixture.pgl_home.joinpath("gates.yml").write_text(
                    "cp2_in_force: true\n"
                    "decided_by: owner\n"
                    "ref: cp2\n"
                    "faces:\n"
                    "  luca:\n"
                    "    cp3_go: false\n",
                    encoding="utf-8",
                )
                result, output = fixture.run_rollback(tag)
                self.assertEqual(result, 1)
                self.assertIn("direction=increase requires full gates", output)
                self.assertFalse(
                    any(
                        command[-2:] == ("deploy", "backup")
                        for command in fixture.events
                    )
                )
            finally:
                fixture.close()

        for unavailable, gates_text in (
            ("missing-key", "decided_by: owner\nref: cp2\n"),
            ("unparseable", "cp2_in_force:\n  - invalid\n"),
        ):
            with self.subTest(unavailable=unavailable):
                fixture = LucaOperationFixture()
                try:
                    tag = fixture.prepare_rollback()
                    fixture.pgl_home.joinpath("gates.yml").write_text(
                        gates_text, encoding="utf-8"
                    )
                    result, output = fixture.run_rollback(tag)
                    self.assertEqual(result, 0, output)
                    self.assertIn("[WARN] luca: rollback audit", output)
                    self.assertIn("attended=true direction=reduction", output)
                finally:
                    fixture.close()

    def test_keyboard_interrupt_between_transport_and_commit_recovers_both_paths(self) -> None:
        for operation in ("block", "rollback"):
            with self.subTest(operation=operation):
                fixture = LucaOperationFixture()
                try:
                    tag = fixture.prepare_rollback() if operation == "rollback" else None
                    source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
                    before = fixture.allowlist_bytes()
                    interrupted = False
                    real_git = (
                        operations._git if operation == "rollback" else apply_module._git
                    )

                    def interrupt_commit(home, *args, **kwargs):
                        nonlocal interrupted
                        if args[:1] == ("commit",) and not interrupted:
                            interrupted = True
                            raise KeyboardInterrupt(
                                "injected interrupt after attended transport"
                            )
                        return real_git(home, *args, **kwargs)

                    patch_target = (
                        mock.patch.object(operations, "_git", side_effect=interrupt_commit)
                        if operation == "rollback"
                        else mock.patch.object(
                            apply_module, "_git", side_effect=interrupt_commit
                        )
                    )
                    with patch_target, self.assertRaises(KeyboardInterrupt):
                        if tag is None:
                            fixture.run("block")
                        else:
                            fixture.run_rollback(tag)
                    self.assertTrue(interrupted)
                    self.assertEqual(
                        fixture.git("rev-parse", "HEAD").stdout.strip(), source_head
                    )
                    self.assertEqual(fixture.allowlist_bytes(), before)
                    self.assertEqual(fixture.production_hash, fixture.backup_hash)
                    self.assertIn("[RED]", fixture.digest_text())
                    self.assertTrue(
                        any(command[-1:] == ("restore",) for command in fixture.events)
                    )
                finally:
                    fixture.close()

    def test_git_revert_failures_are_returned_for_red_folding(self) -> None:
        profile = get_profile("luca")
        completed = [
            subprocess.CompletedProcess(("git", "tag"), 1, b"", b"tag failure"),
            subprocess.CompletedProcess(("git", "rev-parse"), 0, b"new\n", b""),
            subprocess.CompletedProcess(("git", "reset"), 1, b"", b"reset failure"),
            subprocess.CompletedProcess(("git", "checkout"), 1, b"", b"checkout failure"),
            subprocess.CompletedProcess(("git", "reset"), 1, b"", b"index failure"),
        ]
        with mock.patch.object(operations, "_git", side_effect=completed):
            failures = operations._revert_completed_manual_commit(
                profile, Path("/tmp/not-used"), "old", "tag"
            )
        self.assertEqual(len(failures), 4)
        self.assertTrue(any("tag failure" in item for item in failures))
        self.assertTrue(any("reset failure" in item for item in failures))
        self.assertTrue(any("checkout failure" in item for item in failures))


if __name__ == "__main__":
    unittest.main()
