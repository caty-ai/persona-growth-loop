from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import mirror.weekly as weekly_module
from collectors.hermes_luca.collector import collect as luca_collect, jst_epoch_window
from growthlane.faces import get_profile
from growthlane.ledger import dump_ledger, empty_ledger
from growthlane.locking import staging_lock_path
from growthlane.notify import Digest
from mirror.common import MirrorError, injected_bytes, load_configs
from mirror.staging import StagingBuild, regenerate_staging
from mirror.weekly import fetch_luca_production_digest, luca_parity, run_weekly
from support import canonical_temporary_directory
from test_mirror_support import MirrorHarness


REPO = Path(__file__).resolve().parents[1]
HASH_A = "a" * 64
HASH_B = "b" * 64


def _completed(
    command: list[str] | tuple[str, ...], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _persona_content_hash(pack: Path) -> str:
    paths = [
        path
        for path in (pack / "manifest.yml", pack / "aliases.yml")
        if path.is_file()
    ]
    for relative_root in ("modes", "catalogs"):
        root = pack / relative_root
        if root.is_dir():
            paths.extend(path for path in root.rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(
        paths, key=lambda item: item.relative_to(pack).as_posix().encode("utf-8")
    ):
        relative = path.relative_to(pack).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class LucaFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = canonical_temporary_directory()
        self.root = Path(self.temporary.name)
        self.clone = self.root / "luca-repo"
        self.staging = self.root / "luca-staging"
        self.home = self.root / "pgl-home"
        self.config = {
            "display_name": "オーナー",
            "speaker": "owner",
            "transcripts_root": "",
            "overlay_home_root": str(self.clone),
            "staging_root": str(self.staging),
            "writer_argv": [],
            "reviewer_argv": [],
            "classifier_argv": [],
        }
        self.make_clone()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_clone(self) -> None:
        pack = self.clone / "persona-engine"
        (pack / "catalogs" / "overlay").mkdir(parents=True)
        (pack / "catalogs" / "overlay" / "adopted.txt").write_bytes(b"")
        (pack / "catalogs" / "overlay" / "candidates.txt").write_bytes(b"")
        (pack / "modes").mkdir()
        (pack / "modes" / "public.yml").write_text(
            "schema_version: 2\nid: public\n", encoding="utf-8"
        )
        (pack / "manifest.yml").write_text(
            "schema_version: 2\nname: structural-fixture\n", encoding="utf-8"
        )
        (pack / "aliases.yml").write_text(
            "schema_version: 2\naliases: []\n", encoding="utf-8"
        )
        template = self.clone / "tests" / "luca-pack" / "install.yml"
        template.parent.mkdir(parents=True)
        template.write_text(
            "schema_version: 2\n"
            "pack: ../../persona-engine\n"
            "placeholders:\n"
            "  agent-name: ルカ\n"
            "  user: オーナー\n"
            "  owner-name: オーナー\n"
            "runtime: generic\n"
            "routes:\n"
            "  - id: fixture-public\n"
            "    match: {}\n"
            "    allowed_modes: [public]\n"
            "    switching: deny\n"
            "    state_domain: quarantine\n"
            "default_route:\n"
            "  state_domain: quarantine\n",
            encoding="utf-8",
        )
        persona = self.clone / "packages" / "core" / "bin" / "persona"
        persona.parent.mkdir(parents=True)
        persona.write_text("fixture\n", encoding="utf-8")
        growth = self.clone / "growth"
        growth.mkdir()
        (growth / "overlay-ledger.yml").write_bytes(dump_ledger(empty_ledger("luca")))
        (growth / "blocklist.txt").write_bytes(b"")

    def runner(
        self,
        *,
        pull_code: int = 0,
        build_code: int = 0,
        doctor_code: int = 0,
        doctor_ok: bool = True,
    ):
        def run(command: list[str] | tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
            if tuple(command[:3]) == ("git", "pull", "--ff-only"):
                return _completed(command, pull_code, stderr="pull rejected" if pull_code else "")
            if "build" in command:
                content_hash = _persona_content_hash(self.staging / "pack")
                if build_code == 0:
                    build = self.staging / "build"
                    build.mkdir(parents=True, exist_ok=True)
                    (build / "manifest.json").write_text(
                        json.dumps({"content_hash": content_hash}) + "\n", encoding="utf-8"
                    )
                    (build / "public.txt").write_text("structural output\n", encoding="utf-8")
                return _completed(
                    command,
                    build_code,
                    json.dumps({"ok": True, "manifest": {"content_hash": content_hash}}),
                    "build rejected" if build_code else "",
                )
            if "doctor" in command:
                return _completed(
                    command,
                    doctor_code,
                    json.dumps({"ok": doctor_ok}),
                    "doctor rejected" if doctor_code else "",
                )
            raise AssertionError(f"unexpected command: {command}")

        return run

    def write_anchor(self, value: str = HASH_A) -> Path:
        path = self.home / "state" / "luca-prod-anchor.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"content_hash": value}) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return path


class LucaStagingTests(LucaFixture):
    def test_same_input_has_stable_hash_and_full_sync_deletes_stale_files(self) -> None:
        expected = _persona_content_hash(self.clone / "persona-engine")
        first = regenerate_staging(self.config, command_runner=self.runner())
        stale = self.staging / "pack" / "stale.txt"
        stale.write_text("stale\n", encoding="utf-8")
        second = regenerate_staging(self.config, command_runner=self.runner())

        self.assertEqual(first.content_hash, expected)
        self.assertEqual(second.content_hash, expected)
        self.assertEqual(
            second.content_hash, _persona_content_hash(self.staging / "pack")
        )
        self.assertFalse(stale.exists())
        install = (self.staging / "install.yml").read_text(encoding="utf-8")
        self.assertIn("\npack: pack\n", "\n" + install)
        self.assertIn("agent-name: ルカ", install)
        self.assertIn("user: オーナー", install)
        self.assertIn("owner-name: オーナー", install)

    def test_missing_clone_and_pull_failure_are_fatal(self) -> None:
        missing = {**self.config, "overlay_home_root": str(self.root / "missing")}
        with self.assertRaisesRegex(MirrorError, "overlay clone missing"):
            regenerate_staging(missing, command_runner=self.runner())
        with self.assertRaisesRegex(MirrorError, "git pull --ff-only failed"):
            regenerate_staging(self.config, command_runner=self.runner(pull_code=1))

    def test_build_doctor_and_placeholder_failures_are_fatal(self) -> None:
        with self.assertRaisesRegex(MirrorError, "staging build failed"):
            regenerate_staging(self.config, command_runner=self.runner(build_code=2))
        with self.assertRaisesRegex(MirrorError, "staging doctor JSON did not report ok"):
            regenerate_staging(self.config, command_runner=self.runner(doctor_ok=False))
        template = self.clone / "tests" / "luca-pack" / "install.yml"
        template.write_text(
            template.read_text(encoding="utf-8").replace("agent-name: ルカ", "agent-name: fixture"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MirrorError, "placeholder declarations"):
            regenerate_staging(self.config, command_runner=self.runner())


class InjectedBytesTests(LucaFixture):
    def test_luca_reads_staging_build_instead_of_distinct_overlay_build(self) -> None:
        overlay_build = self.clone / "build"
        overlay_build.mkdir()
        (overlay_build / "artifact.bin").write_bytes(b"overlay-home bytes\x00")
        staging_build = self.staging / "build"
        staging_build.mkdir(parents=True)
        (staging_build / "artifact.bin").write_bytes(b"staging bytes\x00\xff")

        files, warning = injected_bytes(get_profile("luca"), self.home, self.config)

        self.assertIsNone(warning)
        self.assertEqual(files, {"artifact.bin": b"staging bytes\x00\xff"})

    def test_luca_missing_staging_build_warns_with_staging_path(self) -> None:
        overlay_build = self.clone / "build"
        overlay_build.mkdir()
        (overlay_build / "artifact.bin").write_bytes(b"must not be used")

        files, warning = injected_bytes(get_profile("luca"), self.home, self.config)

        self.assertEqual(files, {})
        self.assertEqual(
            warning,
            f"build directory absent or unsafe: {self.staging / 'build'}",
        )

    def test_luca_missing_staging_root_fails_closed_with_warning(self) -> None:
        files, warning = injected_bytes(
            get_profile("luca"),
            self.home,
            {**self.config, "staging_root": None},
        )

        self.assertEqual(files, {})
        self.assertIn("staging root absent or unsafe", warning or "")

    def test_alpha_combined_bytes_remain_byte_for_byte_unchanged(self) -> None:
        alpha_home = self.home / "faces" / "alpha"
        alpha_home.mkdir(parents=True)
        expected = b"alpha combined bytes\x00\xff\n"
        (alpha_home / "overlay.md").write_bytes(expected)

        files, warning = injected_bytes(get_profile("alpha"), self.home, {})

        self.assertIsNone(warning)
        self.assertEqual(files, {"overlay.md": expected})


class LucaParityTests(LucaFixture):
    def test_parity_match_mismatch_ssh_failure_and_anchor_missing(self) -> None:
        self.write_anchor(HASH_A)
        self.assertEqual(luca_parity(self.home, lambda: HASH_A).status, "GREEN")
        mismatch = luca_parity(self.home, lambda: HASH_B)
        self.assertEqual(mismatch.status, "RED")
        self.assertEqual(mismatch.production_hash, HASH_B)
        self.assertEqual(mismatch.anchor_hash, HASH_A)

        unavailable = luca_parity(
            self.home, lambda: (_ for _ in ()).throw(MirrorError("ssh failed"))
        )
        self.assertEqual(unavailable.status, "UNAVAILABLE")
        (self.home / "state" / "luca-prod-anchor.json").unlink()
        self.assertEqual(luca_parity(self.home, lambda: HASH_A).status, "UNAVAILABLE")

    def test_production_digest_uses_strict_ssh_argv_without_shell(self) -> None:
        completed = _completed(["ssh"], stdout=HASH_A + "\n")
        with mock.patch("mirror.weekly._LUCA_PRODUCTION_HOST", "example-vps"), mock.patch(
            "mirror.weekly.subprocess.run", return_value=completed
        ) as invoked:
            self.assertEqual(fetch_luca_production_digest(), HASH_A)
        args, kwargs = invoked.call_args
        self.assertEqual(args[0], ("ssh", "example-vps", "hash"))
        self.assertNotIn("shell", kwargs)


class LucaWeeklyTests(LucaFixture):
    def setUp(self) -> None:
        super().setUp()
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        (self.config_dir / "growth-luca.json").write_text(
            json.dumps(self.config), encoding="utf-8"
        )
        (self.config_dir / "mirror-luca.json").write_text(
            json.dumps({"responder_argv": [], "scorer_argv": [], "vault_dir": ""}),
            encoding="utf-8",
        )
        self.write_anchor()

    def write_growth_config(self, value: dict[str, object]) -> None:
        (self.config_dir / "growth-luca.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def test_luca_growth_config_rejects_unknown_and_missing_keys(self) -> None:
        self.write_growth_config({**self.config, "unexpected": True})
        with self.assertRaisesRegex(MirrorError, "luca growth config keys must be exactly"):
            load_configs("luca", self.config_dir)

        missing = dict(self.config)
        missing.pop("staging_root")
        self.write_growth_config(missing)
        with self.assertRaisesRegex(MirrorError, "luca growth config keys must be exactly"):
            load_configs("luca", self.config_dir)

    def staging_builder(self, config: dict[str, object]) -> StagingBuild:
        build = self.staging / "build"
        build.mkdir(parents=True, exist_ok=True)
        (build / "manifest.json").write_text(
            json.dumps({"content_hash": HASH_A}) + "\n", encoding="utf-8"
        )
        return StagingBuild(self.staging, HASH_A)

    def test_weekly_snapshot_read_and_write_hold_one_staging_lock(self) -> None:
        run_day = date(2026, 8, 7)
        expected_lock = staging_lock_path(self.staging, "luca")
        original_injected = weekly_module.injected_bytes
        original_write_snapshot = weekly_module.write_snapshot
        seen: list[tuple[str, bool]] = []

        def builder(config: dict[str, object]) -> StagingBuild:
            seen.append(("builder", expected_lock.is_dir()))
            return self.staging_builder(config)

        def injected(*args, **kwargs):
            seen.append(("injected", expected_lock.is_dir()))
            return original_injected(*args, **kwargs)

        def write_snapshot(*args, **kwargs):
            seen.append(("snapshot", expected_lock.is_dir()))
            return original_write_snapshot(*args, **kwargs)

        with mock.patch("mirror.weekly.injected_bytes", side_effect=injected):
            with mock.patch("mirror.weekly.write_snapshot", side_effect=write_snapshot):
                self.assertEqual(
                    run_weekly(
                        "luca",
                        run_day.isoformat(),
                        self.config_dir,
                        self.home,
                        Digest(self.home, run_day.isoformat()),
                        staging_builder=builder,
                        production_digest=lambda: HASH_A,
                    ),
                    0,
                )

        self.assertEqual(seen, [("builder", True), ("injected", True), ("snapshot", True)])
        self.assertFalse(expected_lock.exists())

    def test_weekly_default_builder_does_not_reacquire_staging_lock(self) -> None:
        run_day = date(2026, 8, 7)
        weekly_acquire = weekly_module.acquire_staging_lock

        def builder(config, *, held_lock):
            return regenerate_staging(
                config,
                command_runner=self.runner(),
                held_lock=held_lock,
            )

        with mock.patch(
            "mirror.weekly.acquire_staging_lock", wraps=weekly_acquire
        ) as outer_acquire:
            with mock.patch("mirror.staging.acquire_staging_lock") as nested_acquire:
                with mock.patch("mirror.weekly.regenerate_staging", side_effect=builder):
                    self.assertEqual(
                        run_weekly(
                            "luca",
                            run_day.isoformat(),
                            self.config_dir,
                            self.home,
                            Digest(self.home, run_day.isoformat()),
                            production_digest=lambda: HASH_A,
                        ),
                        0,
                    )

        self.assertEqual(outer_acquire.call_count, 1)
        nested_acquire.assert_not_called()

    def test_luca_weekly_zero_usage_week_is_quiet_with_real_collector_marker(self) -> None:
        # #36: mirror.weekly's usage-discipline gate must certify a
        # zero-usage week as QUIET (not BROKEN) when fed a marker the *real*
        # Luca collector wrote -- and Tier S must write zero-valued
        # exposure/holdout totals instead of aborting.
        jst = timezone(timedelta(hours=9))
        run_day = datetime.now(jst).date()

        fixtures = REPO / "tests" / "fixtures" / "hermes_luca"
        db_path = self.root / "luca-state.db"
        owners_path = self.root / "luca-owners.json"
        denylist_path = self.root / "luca-denylist.txt"
        collector_config_path = self.root / "obs-collector-luca.json"
        shutil.copyfile(fixtures / "owners.json", owners_path)
        shutil.copyfile(fixtures / "denylist.txt", denylist_path)
        with contextlib.closing(sqlite3.connect(db_path)) as connection:
            connection.executescript((fixtures / "schema.sql").read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO sessions (id, source, chat_type, user_id, session_key) VALUES (?, ?, ?, ?, ?)",
                ("quiet-session", "telegram", "dm", "100000001", "quiet-key"),
            )
            start_epoch, _ = jst_epoch_window(run_day.isoformat())
            connection.execute(
                "INSERT INTO messages (id, session_id, content, timestamp, role) VALUES (?, ?, ?, ?, ?)",
                (1, "quiet-session", "quiet owner text", start_epoch + 1, "user"),
            )
            connection.commit()
        collector_config_path.write_text(
            json.dumps(
                {
                    "face": "luca",
                    "host": "vps-hermes",
                    "speaker": "owner",
                    "obs_root": str(self.home),
                    "source": {
                        "ssh_host": "fixture-ssh",
                        "kind": "hermes-state-db",
                        "db_path": str(db_path),
                        "owner_uids": {"telegram": ["100000001"], "slack": ["U0EXAMPLE01"]},
                        "expected_dm_entries": {
                            "telegram": ["100000001"],
                            "slack": ["D0EXAMPLE01"],
                        },
                        "sources": ["telegram", "slack"],
                        "dm_only": True,
                        "exclude_session_prefixes": ["pgl-verify-"],
                        "voice_enabled": False,
                    },
                    "denylist_path": str(denylist_path),
                }
            ),
            encoding="utf-8",
        )

        stats = luca_collect(
            run_day.isoformat(),
            config_path=collector_config_path,
            sqlite_path=db_path,
            owners_json=owners_path,
            obs_root=self.home,
        )
        self.assertEqual(stats["records"], 1)
        # No ledger seeded under {home}/faces/luca -> usage_enabled is False
        # in the marker, which is what makes this a QUIET (not BROKEN)
        # zero-usage week.
        self.assertFalse((self.home / "faces" / "luca").exists())

        digest = Digest(self.home, run_day.isoformat())
        self.assertEqual(
            run_weekly(
                "luca",
                run_day.isoformat(),
                self.config_dir,
                self.home,
                digest,
                staging_builder=self.staging_builder,
                production_digest=lambda: HASH_A,
            ),
            0,
        )
        digest.ensure_line()

        report = (
            self.home / "reports" / "weekly" / "luca" / f"{run_day.isoformat()}.md"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "[OK] quiet week (bootstrap: 0 staged/adopted — usage collection structurally disabled)",
            report,
        )
        self.assertNotIn("BROKEN", report)

        tier_s = json.loads(
            (
                self.home / "reports" / "tier-s" / "luca" / f"{run_day.isoformat()}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(tier_s["window_exposure_total"], 0)
        self.assertEqual(tier_s["holdout_opportunity_total"], 0)

        digest_text = (self.home / "digest" / f"{run_day.isoformat()}.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Tier S write aborted", digest_text)
        self.assertNotIn("usage discipline unavailable", digest_text)

    def test_luca_marker_report_and_red_digest_include_parity(self) -> None:
        run_day = date(2026, 8, 7)
        clone_build = self.clone / "build"
        clone_build.mkdir()
        (clone_build / "clone-only.txt").write_text("must not be snapped\n", encoding="utf-8")
        digest = Digest(self.home, run_day.isoformat())
        self.assertEqual(
            run_weekly(
                "luca",
                run_day.isoformat(),
                self.config_dir,
                self.home,
                digest,
                staging_builder=self.staging_builder,
                production_digest=lambda: HASH_B,
            ),
            0,
        )
        digest.ensure_line()
        marker = json.loads(
            (self.home / "reports" / "weekly" / "latest-luca.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["parity"], "RED")
        report = (
            self.home / "reports" / "weekly" / "luca" / f"{run_day.isoformat()}.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## Production parity", report)
        self.assertIn("Parity: RED", report)
        self.assertNotIn("\nno changes\n", report)
        self.assertIn(
            "[RED] luca: production parity RED",
            (self.home / "digest" / f"{run_day.isoformat()}.md").read_text(encoding="utf-8"),
        )
        snapshot = json.loads(
            (
                self.home
                / "mirror"
                / "snapshots"
                / "luca"
                / run_day.isoformat()
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual([item["path"] for item in snapshot["files"]], ["manifest.json"])

    def test_unavailable_parity_publishes_marker_and_never_reports_no_change(self) -> None:
        run_day = date(2026, 8, 7)
        (self.home / "state" / "luca-prod-anchor.json").unlink()
        digest = Digest(self.home, run_day.isoformat())
        self.assertEqual(
            run_weekly(
                "luca",
                run_day.isoformat(),
                self.config_dir,
                self.home,
                digest,
                staging_builder=self.staging_builder,
                production_digest=lambda: HASH_A,
            ),
            0,
        )
        digest.ensure_line()
        marker = json.loads(
            (self.home / "reports" / "weekly" / "latest-luca.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["parity"], "UNAVAILABLE")
        report = (
            self.home / "reports" / "weekly" / "luca" / f"{run_day.isoformat()}.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Parity: UNAVAILABLE", report)
        self.assertNotIn("\nno changes\n", report)
        self.assertIn(
            "[RED] luca: production parity UNAVAILABLE",
            (self.home / "digest" / f"{run_day.isoformat()}.md").read_text(encoding="utf-8"),
        )

    def test_staging_failure_is_nonzero_and_does_not_publish_marker(self) -> None:
        run_day = date(2026, 8, 7)
        env = {**os.environ, "PGL_HOME": str(self.home)}
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "mirror.weekly",
                "luca",
                "--date",
                run_day.isoformat(),
                "--config-dir",
                str(self.config_dir),
            ],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("[RED] luca: weekly mirror failed", completed.stdout)
        self.assertFalse((self.home / "reports" / "weekly" / "latest-luca.json").exists())

    def test_bin_weekly_luca_path_regenerates_staging(self) -> None:
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        git = fake_bin / "git"
        git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        node = fake_bin / "node"
        node.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "root = pathlib.Path(sys.argv[sys.argv.index('--dir') + 1])\n"
            "if 'build' in sys.argv:\n"
            "    (root / 'build').mkdir(parents=True, exist_ok=True)\n"
            "    (root / 'build' / 'manifest.json').write_text(json.dumps({'content_hash': 'a' * 64}) + '\\n')\n"
            "    print(json.dumps({'ok': True, 'manifest': {'content_hash': 'a' * 64}}))\n"
            "else:\n"
            "    print(json.dumps({'ok': True}))\n",
            encoding="utf-8",
        )
        ssh = fake_bin / "ssh"
        ssh.write_text(f"#!/bin/sh\nprintf '%s\\n' '{HASH_A}'\n", encoding="utf-8")
        for command in (git, node, ssh):
            command.chmod(0o700)

        run_day = date(2026, 8, 7)
        env = {
            **os.environ,
            "PGL_HOME": str(self.home),
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO / "bin" / "pgl-mirror-weekly"),
                "luca",
                "--date",
                run_day.isoformat(),
                "--config-dir",
                str(self.config_dir),
            ],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertTrue((self.staging / "build" / "manifest.json").is_file())
        marker = json.loads(
            (self.home / "reports" / "weekly" / "latest-luca.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["parity"], "GREEN")


class AlphaMarkerCompatibilityTests(MirrorHarness):
    def test_alpha_growth_config_schema_remains_compatible(self) -> None:
        growth, _, profile = load_configs("alpha", self.config_dir)
        self.assertEqual(profile.name, "alpha")
        self.assertEqual(
            set(growth),
            {
                "display_name",
                "speaker",
                "transcripts_root",
                "writer_argv",
                "reviewer_argv",
                "classifier_argv",
            },
        )

    def test_alpha_marker_and_report_do_not_gain_parity_fields(self) -> None:
        run_day = date(2026, 8, 1)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        self.run_module("mirror.weekly", run_day)

        marker = json.loads(
            (self.home / "reports" / "weekly" / "latest-alpha.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(marker), {"generated_at", "report", "soul_check", "change"}
        )
        self.assertNotIn("Parity:", self.weekly_report_text(run_day))

    def test_alpha_report_checks_still_precede_injected_snapshot(self) -> None:
        run_day = date(2026, 8, 1)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        events: list[str] = []
        original_context = weekly_module._weekly_report_context
        original_injected = weekly_module.injected_bytes

        def report_context(*args, **kwargs):
            events.append("report-context")
            return original_context(*args, **kwargs)

        def injected(*args, **kwargs):
            events.append("injected-bytes")
            return original_injected(*args, **kwargs)

        digest = Digest(self.home, run_day.isoformat())
        with mock.patch("mirror.weekly._weekly_report_context", side_effect=report_context):
            with mock.patch("mirror.weekly.injected_bytes", side_effect=injected):
                run_weekly(
                    "alpha",
                    run_day.isoformat(),
                    self.config_dir,
                    self.home,
                    digest,
                )

        self.assertEqual(events, ["report-context", "injected-bytes"])


if __name__ == "__main__":
    unittest.main()
