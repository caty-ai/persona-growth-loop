from __future__ import annotations

# NOTES:
# - Deferred review follow-up: share install.yml literal validation between
#   mirror/applier once the lock change lands cleanly.
# - Deferred review follow-up: deduplicate shared directory/symlink/replace
#   helpers without widening the core lock diff.
# - Deferred review follow-up: consider an explicit `--dir` applier persona
#   invocation after the lock scope is settled.

import json
import errno
import os
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from applier.apply import ApplyError, _build
from growthlane.faces import get_profile
from growthlane.locking import (
    OWNER_FILE,
    acquire_staging_lock,
    release_lock,
    staging_lock_path,
)
from mirror.common import MirrorError
from mirror.staging import regenerate_staging


HASH_A = "a" * 64


def _completed(
    command: list[str] | tuple[str, ...],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class StagingLockFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.clone = self.root / "luca-repo"
        self.staging = self.root / "luca-staging"
        self.home = self.root / "pgl-home"
        self.profile = get_profile("luca")
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
        self.staging.mkdir()
        (self.staging / "install.yml").write_text("schema_version: 2\npack: pack\n", encoding="utf-8")

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

    def runner(self):
        def run(
            command: list[str] | tuple[str, ...], cwd: Path
        ) -> subprocess.CompletedProcess[str]:
            if tuple(command[:3]) == ("git", "pull", "--ff-only"):
                return _completed(command)
            if "build" in command:
                build = self.staging / "build"
                build.mkdir(parents=True, exist_ok=True)
                (build / "manifest.json").write_text(
                    json.dumps({"content_hash": HASH_A}) + "\n",
                    encoding="utf-8",
                )
                return _completed(
                    command,
                    stdout=json.dumps({"ok": True, "manifest": {"content_hash": HASH_A}}),
                )
            if "doctor" in command:
                return _completed(command, stdout=json.dumps({"ok": True}))
            raise AssertionError(f"unexpected command: {command}")

        return run

    def _staging_tree(self) -> list[tuple[str, bytes | None]]:
        snapshot: list[tuple[str, bytes | None]] = []
        if not self.staging.exists():
            return snapshot
        for path in sorted(self.staging.rglob("*")):
            relative = path.relative_to(self.staging).as_posix()
            if path.is_dir():
                snapshot.append((relative + "/", None))
            else:
                snapshot.append((relative, path.read_bytes()))
        return snapshot

    def _install_fake_persona(
        self,
        *,
        build_exit: int = 0,
        manifest_content: str | None = None,
        doctor_stdout: str | None = None,
        doctor_exit: int = 0,
    ) -> dict[str, str]:
        fake_bin = self.root / "bin"
        fake_bin.mkdir(exist_ok=True)
        persona = fake_bin / "persona"
        manifest_payload = (
            manifest_content
            if manifest_content is not None
            else json.dumps({"content_hash": HASH_A}) + "\n"
        )
        doctor_payload = (
            doctor_stdout
            if doctor_stdout is not None
            else json.dumps({"ok": True, "issues": []})
        )
        persona.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "cwd = pathlib.Path.cwd()\n"
            "lock = pathlib.Path(os.environ['EXPECTED_STAGING_LOCK'])\n"
            "command = sys.argv[1]\n"
            "(cwd / 'build').mkdir(parents=True, exist_ok=True)\n"
            "(cwd / 'build' / f'{command}-lock.txt').write_text('1' if lock.is_dir() else '0', encoding='utf-8')\n"
            "if command == 'build':\n"
            f"    (cwd / 'build' / 'manifest.json').write_text({manifest_payload!r}, encoding='utf-8')\n"
            f"    sys.exit({build_exit})\n"
            f"sys.stdout.write({doctor_payload!r})\n"
            f"sys.exit({doctor_exit})\n",
            encoding="utf-8",
        )
        persona.chmod(0o700)
        return {
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "EXPECTED_STAGING_LOCK": str(staging_lock_path(self.staging, "luca")),
        }

    def _assert_applier_failure_releases_lock(
        self, env: dict[str, str], expected_error: str
    ) -> None:
        lock_path = staging_lock_path(self.staging, "luca")
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(ApplyError, expected_error):
                _build(self.profile, self.clone, {}, self.config)
        self.assertFalse(lock_path.exists())
        with mock.patch.dict(os.environ, self._install_fake_persona(), clear=False):
            self.assertEqual(_build(self.profile, self.clone, {}, self.config), HASH_A)


class _LockAssertingManifest(dict[str, str]):
    def __init__(self, lock: Path) -> None:
        super().__init__({"content_hash": HASH_A})
        self._lock = lock

    def get(self, key: str, default: object = None) -> object:
        if key == "content_hash":
            assert self._lock.is_dir(), "content_hash accessed after lock release"
        return super().get(key, default)


class _LockAssertingBuildResult(dict[str, object]):
    def __init__(self, lock: Path) -> None:
        super().__init__({"ok": True, "manifest": _LockAssertingManifest(lock)})
        self._lock = lock

    def get(self, key: str, default: object = None) -> object:
        if key in {"ok", "manifest"}:
            assert self._lock.is_dir(), f"{key} accessed after lock release"
        return super().get(key, default)


class StagingLockTests(StagingLockFixture):
    def test_mirror_and_applier_share_one_lock_identity_with_owner_metadata(self) -> None:
        mirror_lock = staging_lock_path(Path(str(self.config["staging_root"])), "luca")
        apply_lock = staging_lock_path(self.profile.resolve_staging_root(self.config), self.profile.name)
        self.assertEqual(mirror_lock, apply_lock)
        other_root = self.root / "other-staging"
        other_lock = staging_lock_path(other_root, "luca")
        self.assertNotEqual(
            mirror_lock,
            other_lock,
        )
        alpha_lock = staging_lock_path(self.staging, "alpha")

        lock = acquire_staging_lock(self.staging, "luca")
        self.assertEqual(lock, mirror_lock)
        try:
            self.assertEqual(lock.stat().st_mode & 0o777, 0o700)
            owner = json.loads((lock / OWNER_FILE).read_text(encoding="utf-8"))
            self.assertEqual(set(owner), {"pid", "host", "started_at"})
            other = acquire_staging_lock(other_root, "luca")
            alpha = acquire_staging_lock(self.staging, "alpha")
            self.assertEqual(other, other_lock)
            self.assertEqual(alpha, alpha_lock)
            self.assertIsNotNone(other)
            self.assertIsNotNone(alpha)
            try:
                self.assertTrue(other.is_dir())
                self.assertTrue(alpha.is_dir())
            finally:
                self.assertTrue(release_lock(alpha))
                self.assertTrue(release_lock(other))
        finally:
            self.assertTrue(release_lock(lock))

    def test_staging_lock_alias_paths_canonicalize_to_one_identity(self) -> None:
        real_parent = self.root / "real-parent"
        real_parent.mkdir()
        alias_parent = self.root / "alias-parent"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        real_staging = real_parent / "staging"
        alias_staging = alias_parent / "staging"
        real_lock = staging_lock_path(real_staging, "luca")
        alias_lock = staging_lock_path(alias_staging, "luca")
        self.assertEqual(real_lock, alias_lock)

        first = acquire_staging_lock(real_staging, "luca")
        self.assertEqual(first, real_lock)
        try:
            self.assertIsNone(acquire_staging_lock(alias_staging, "luca"))
        finally:
            self.assertTrue(release_lock(first))

    def test_staging_lock_rejects_unsafe_existing_shape_without_chmodding_target(self) -> None:
        lock_path = staging_lock_path(self.staging, "luca")
        target = self.root / "chmod-target"
        target.mkdir()
        os.chmod(target, 0o755)
        lock_path.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(OSError, "unsafe shape"):
            acquire_staging_lock(self.staging, "luca")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_acquire_retries_mkdir_once_when_released_before_existing_lock_open(self) -> None:
        lock_path = staging_lock_path(self.staging, "luca")
        lock_path.mkdir()
        original_open = os.open
        raced = False

        def racing_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
            nonlocal raced
            if Path(path) == lock_path and not raced:
                raced = True
                lock_path.rmdir()
                raise FileNotFoundError(errno.ENOENT, "released during acquire", str(path))
            return original_open(path, flags, mode)

        with mock.patch("growthlane.locking.os.open", side_effect=racing_open):
            acquired = acquire_staging_lock(self.staging, "luca")
        self.assertTrue(raced)
        self.assertEqual(acquired, lock_path)
        self.assertTrue(release_lock(acquired))

    def test_release_self_heals_empty_ghost_lock_directory(self) -> None:
        lock = acquire_staging_lock(self.staging, "luca")
        self.assertIsNotNone(lock)
        (lock / OWNER_FILE).unlink()

        self.assertTrue(release_lock(lock))
        self.assertFalse(lock.exists())
        reacquired = acquire_staging_lock(self.staging, "luca")
        self.assertEqual(reacquired, lock)
        self.assertTrue(release_lock(reacquired))

    def test_contention_leaves_staging_tree_untouched_for_mirror_and_applier(self) -> None:
        (self.staging / "pack").mkdir()
        (self.staging / "pack" / "keep.txt").write_text("keep\n", encoding="utf-8")
        (self.staging / "build").mkdir()
        (self.staging / "build" / "keep.txt").write_text("keep\n", encoding="utf-8")
        before = self._staging_tree()
        lock = acquire_staging_lock(self.staging, "luca")
        try:
            with self.assertRaisesRegex(MirrorError, "weekly staging: skipped: lock contention"):
                regenerate_staging(self.config, command_runner=self.runner())
            with self.assertRaisesRegex(
                ApplyError, r"engine staging: skipped: lock contention"
            ):
                _build(self.profile, self.clone, {}, self.config)
        finally:
            self.assertTrue(release_lock(lock))
        self.assertEqual(self._staging_tree(), before)

    def test_stale_lock_requires_manual_clear_then_recovers(self) -> None:
        lock = staging_lock_path(self.staging, "luca")
        lock.mkdir()
        os.chmod(lock, 0o700)
        stale_started = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        (lock / OWNER_FILE).write_text(
            json.dumps(
                {
                    "pid": 4242,
                    "host": "fixture-host",
                    "started_at": stale_started,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MirrorError, "stale lock contention age=") as caught:
            regenerate_staging(self.config, command_runner=self.runner())
        self.assertIn("clear manually:", str(caught.exception))
        (lock / OWNER_FILE).unlink()
        lock.rmdir()

        build = regenerate_staging(self.config, command_runner=self.runner())
        self.assertEqual(build.content_hash, HASH_A)

    def test_future_owner_timestamp_falls_back_to_lock_mtime_for_stale_diagnostics(self) -> None:
        lock = staging_lock_path(self.staging, "luca")
        lock.mkdir()
        os.chmod(lock, 0o700)
        future_started = (datetime.now(timezone.utc) + timedelta(hours=25)).isoformat()
        (lock / OWNER_FILE).write_text(
            json.dumps(
                {
                    "pid": 4343,
                    "host": "fixture-host",
                    "started_at": future_started,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        old_ts = old.timestamp()
        os.utime(lock, (old_ts, old_ts))
        with self.assertRaisesRegex(MirrorError, "stale lock contention age=") as caught:
            regenerate_staging(self.config, command_runner=self.runner())
        self.assertIn("clear manually:", str(caught.exception))
        (lock / OWNER_FILE).unlink()
        lock.rmdir()

    def test_mirror_holds_lock_through_build_and_doctor(self) -> None:
        seen: list[bool] = []
        expected_lock = staging_lock_path(self.staging, "luca")

        def run(
            command: list[str] | tuple[str, ...], cwd: Path
        ) -> subprocess.CompletedProcess[str]:
            if "build" in command or "doctor" in command:
                seen.append(expected_lock.is_dir())
            return self.runner()(command, cwd)

        build = regenerate_staging(self.config, command_runner=run)
        self.assertEqual(build.content_hash, HASH_A)
        self.assertEqual(seen, [True, True])

    def test_mirror_holds_lock_through_post_build_hash_processing(self) -> None:
        expected_lock = staging_lock_path(self.staging, "luca")

        def run(
            command: list[str] | tuple[str, ...], cwd: Path
        ) -> subprocess.CompletedProcess[str]:
            if "build" in command:
                (self.staging / "build").mkdir(parents=True, exist_ok=True)
            return _completed(command, stdout="{}\n")

        def completed_json(
            completed: subprocess.CompletedProcess[str], label: str
        ) -> Mapping[str, object]:
            del completed
            if label == "Luca staging build":
                return _LockAssertingBuildResult(expected_lock)
            return {"ok": True}

        with mock.patch("mirror.staging._completed_json", side_effect=completed_json):
            build = regenerate_staging(self.config, command_runner=run)
        self.assertEqual(build.content_hash, HASH_A)

    def test_mirror_failure_releases_lock(self) -> None:
        def failing_run(
            command: list[str] | tuple[str, ...], cwd: Path
        ) -> subprocess.CompletedProcess[str]:
            if "build" in command:
                return _completed(command, returncode=2, stderr="build rejected")
            return self.runner()(command, cwd)

        lock_path = staging_lock_path(self.staging, "luca")
        with self.assertRaisesRegex(MirrorError, "staging build failed with exit 2"):
            regenerate_staging(self.config, command_runner=failing_run)
        self.assertFalse(lock_path.exists())

    def test_mirror_release_failure_is_surfaced_after_success(self) -> None:
        with mock.patch("mirror.staging.release_lock", return_value=False):
            with self.assertRaisesRegex(
                MirrorError, "Luca staging lock directory could not be removed"
            ):
                regenerate_staging(self.config, command_runner=self.runner())

    def test_mirror_release_failure_chains_original_failure(self) -> None:
        def failing_run(
            command: list[str] | tuple[str, ...], cwd: Path
        ) -> subprocess.CompletedProcess[str]:
            if "build" in command:
                return _completed(command, returncode=2, stderr="build rejected")
            return self.runner()(command, cwd)

        with mock.patch("mirror.staging.release_lock", return_value=False):
            with self.assertRaisesRegex(
                MirrorError, "Luca staging lock directory could not be removed"
            ) as caught:
                regenerate_staging(
                    self.config,
                    command_runner=failing_run,
                )
        self.assertIsInstance(caught.exception.__cause__, MirrorError)
        self.assertIn(
            "staging build failed with exit 2",
            str(caught.exception.__cause__),
        )

    def test_mirror_wraps_staging_lock_path_value_error(self) -> None:
        with mock.patch(
            "mirror.staging.acquire_staging_lock", side_effect=ValueError("bad lock path")
        ):
            with self.assertRaisesRegex(
                MirrorError, "Luca staging lock acquisition failed: bad lock path"
            ):
                regenerate_staging(self.config, command_runner=self.runner())

    def test_applier_holds_lock_through_manifest_read_build_and_doctor(self) -> None:
        env = self._install_fake_persona()
        manifest_path = self.staging / "build" / "manifest.json"
        expected_lock = staging_lock_path(self.staging, "luca")
        original_read_text = Path.read_text
        seen_manifest_read: list[bool] = []

        def read_text(
            path: Path, *args: object, **kwargs: object
        ) -> str:
            if path == manifest_path:
                seen_manifest_read.append(expected_lock.is_dir())
            return original_read_text(path, *args, **kwargs)

        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(Path, "read_text", new=read_text):
                content_hash = _build(self.profile, self.clone, {}, self.config)
        self.assertEqual(content_hash, HASH_A)
        self.assertEqual(seen_manifest_read, [True])
        self.assertEqual(
            (self.staging / "build" / "build-lock.txt").read_text(encoding="utf-8"),
            "1",
        )
        self.assertEqual(
            (self.staging / "build" / "doctor-lock.txt").read_text(encoding="utf-8"),
            "1",
        )

    def test_applier_release_failure_is_surfaced_after_success(self) -> None:
        with mock.patch.dict(os.environ, self._install_fake_persona(), clear=False):
            with mock.patch("applier.apply.release_lock", return_value=False):
                with self.assertRaisesRegex(
                    ApplyError, "luca staging lock directory could not be removed"
                ):
                    _build(self.profile, self.clone, {}, self.config)

    def test_applier_release_failure_chains_original_failure(self) -> None:
        with mock.patch.dict(
            os.environ, self._install_fake_persona(build_exit=2), clear=False
        ):
            with mock.patch("applier.apply.release_lock", return_value=False):
                with self.assertRaisesRegex(
                    ApplyError, "luca staging lock directory could not be removed"
                ) as caught:
                    _build(self.profile, self.clone, {}, self.config)
        self.assertIsInstance(caught.exception.__cause__, ApplyError)
        self.assertIn(
            "persona build exited 2",
            str(caught.exception.__cause__),
        )

    def test_applier_wraps_staging_lock_path_value_error(self) -> None:
        with mock.patch(
            "applier.apply.acquire_staging_lock", side_effect=ValueError("bad lock path")
        ):
            with self.assertRaisesRegex(
                ApplyError, "luca staging lock acquisition failed: bad lock path"
            ):
                _build(self.profile, self.clone, {}, self.config)

    def test_applier_build_failure_releases_lock_and_next_build_reacquires(self) -> None:
        self._assert_applier_failure_releases_lock(
            self._install_fake_persona(build_exit=2),
            "persona build exited 2",
        )

    def test_applier_manifest_failure_releases_lock_and_next_build_reacquires(self) -> None:
        self._assert_applier_failure_releases_lock(
            self._install_fake_persona(manifest_content=json.dumps({"content_hash": "bad"}) + "\n"),
            "persona build manifest omitted a valid content_hash",
        )

    def test_applier_doctor_failure_releases_lock_and_next_build_reacquires(self) -> None:
        self._assert_applier_failure_releases_lock(
            self._install_fake_persona(
                doctor_stdout=json.dumps({"ok": True, "issues": ["x"]}),
            ),
            "persona doctor report is not clean",
        )


if __name__ == "__main__":
    unittest.main()
