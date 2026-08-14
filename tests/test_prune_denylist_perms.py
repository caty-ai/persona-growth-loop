from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from collectors.claude_code.collector import main as collector_main
from collectors.claude_code.prune import prune
from growthlane import nightly as nightly_module
from growthlane.locking import acquire_lock, release_lock
from growthlane.notify import Digest
from growthlane.operations import _locked
from growthlane.ucd_runtime import UcdRuntimeStatus


REPO_ROOT = Path(__file__).resolve().parents[1]


class PruneAndLockTests(unittest.TestCase):
    def _stable_runtime(self) -> UcdRuntimeStatus:
        return UcdRuntimeStatus(
            corpus_version="16.0.0",
            runtime_version="16.0.0",
            drifted=False,
        )

    def _config_payload(self, obs_root: Path, transcripts: Path) -> dict[str, str]:
        return {
            "host": "test-host",
            "face": "alpha",
            "speaker": "test-speaker",
            "obs_root": str(obs_root),
            "transcripts_root": str(transcripts),
            "denylist_path": str(REPO_ROOT / "config" / "obs-denylist.example.txt"),
        }

    def test_prune_removes_31_day_file_but_keeps_29_day_file_under_killswitch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            obs_root = Path(temporary)
            face_dir = obs_root / "obslog" / "alpha"
            face_dir.mkdir(parents=True)
            old = face_dir / "2026-07-02.jsonl"
            recent = face_dir / "2026-07-04.jsonl"
            old.write_text("old\n", encoding="utf-8")
            recent.write_text("recent\n", encoding="utf-8")
            (obs_root / "KILLSWITCH").write_text("mode: freeze\n", encoding="utf-8")
            removed = prune(obs_root, "2026-08-02")
            self.assertEqual(removed, [old])
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())

    def test_face_locks_are_independent_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alpha = acquire_lock(root, "alpha")
            luca = acquire_lock(root, "luca")
            self.assertIsNotNone(alpha)
            self.assertIsNotNone(luca)
            assert alpha is not None and luca is not None
            self.assertNotEqual(alpha, luca)
            self.assertEqual(alpha.name, "lock-alpha.d")
            self.assertEqual(luca.name, "lock-luca.d")
            self.assertEqual(os.stat(alpha).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(alpha / "owner.json").st_mode & 0o777, 0o600)
            self.assertIsNone(acquire_lock(root, "alpha"))
            self.assertTrue(release_lock(alpha))
            self.assertTrue(release_lock(luca))

    def test_acquire_lock_owner_is_private_and_opened_nofollow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_open = os.open
            observed_flags: list[int] = []

            def capture_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
                observed_flags.append(flags)
                return real_open(path, flags, mode)

            with mock.patch("growthlane.locking.os.open", side_effect=capture_open):
                with mock.patch("growthlane.locking.os.getpid", return_value=4321):
                    with mock.patch(
                        "growthlane.locking.socket.gethostname", return_value="test-host"
                    ):
                        lock = acquire_lock(root, "alpha")
            self.assertIsNotNone(lock)
            assert lock is not None
            owner = lock / "owner.json"
            payload = owner.read_bytes()
            self.assertTrue(payload.endswith(b"\n"))
            self.assertEqual(payload.count(b"\n"), 1)
            value = json.loads(payload)
            self.assertEqual(list(value), ["host", "pid", "started_at"])
            self.assertEqual(value["host"], "test-host")
            self.assertEqual(value["pid"], 4321)
            self.assertIsInstance(value["started_at"], str)
            self.assertEqual(os.stat(owner).st_mode & 0o777, 0o600)
            self.assertEqual(len(observed_flags), 1)
            if hasattr(os, "O_NOFOLLOW"):
                self.assertTrue(observed_flags[0] & os.O_NOFOLLOW)
            self.assertTrue(release_lock(lock))

    def _assert_contended_face_does_not_block_other(self, contended: str, other: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs_root = root / "pgl-home"
            obs_root.mkdir()
            held = acquire_lock(obs_root, contended)
            self.assertIsNotNone(held)
            assert held is not None
            stdout = io.StringIO()
            run_face = mock.Mock()
            try:
                with mock.patch.dict(os.environ, {"PGL_HOME": str(obs_root)}):
                    with mock.patch(
                        "growthlane.nightly.emit_admission_refusal", return_value=None
                    ), mock.patch(
                        "growthlane.nightly.load_json_object",
                        return_value={"classifier_argv": ["configured"]},
                    ), mock.patch(
                        "growthlane.nightly.check_all"
                    ), mock.patch(
                        "growthlane.nightly.parse_scalar_yaml",
                        return_value=SimpleNamespace(deviations=[]),
                    ), mock.patch(
                        "growthlane.nightly.run_face", run_face
                    ), mock.patch(
                        "growthlane.nightly.inspect"
                    ), contextlib.redirect_stdout(stdout):
                        result = nightly_module.main(
                            [
                                "--face",
                                "alpha",
                                "--face",
                                "luca",
                                "--date",
                                "2026-08-02",
                                "--config-dir",
                                str(root),
                            ]
                        )
                self.assertEqual(result, 1)
                self.assertEqual([call.args[0].name for call in run_face.call_args_list], [other])
                self.assertIn(
                    f"[RED] {contended}: nightly: skipped: lock contention",
                    stdout.getvalue(),
                )
                self.assertTrue(held.is_dir())
                self.assertTrue((held / "owner.json").is_file())
                self.assertFalse((obs_root / f"lock-{other}.d").exists())
            finally:
                self.assertTrue(release_lock(held))

    def test_alpha_contention_does_not_block_luca_nightly(self) -> None:
        self._assert_contended_face_does_not_block_other("alpha", "luca")

    def test_luca_contention_does_not_block_alpha_nightly(self) -> None:
        self._assert_contended_face_does_not_block_other("luca", "alpha")

    def test_held_face_lock_skips_loudly_with_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs_root = root / "pgl-home"
            lock = obs_root / "lock-alpha.d"
            lock.mkdir(parents=True)
            transcripts = root / "transcripts"
            transcripts.mkdir()
            config = root / "config.json"
            config.write_text(json.dumps(self._config_payload(obs_root, transcripts)), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch(
                "collectors.claude_code.collector.runtime_status",
                return_value=self._stable_runtime(),
            ):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = collector_main(
                        [
                            "--date",
                            "2026-08-02",
                            "--config",
                            str(config),
                            "--transcripts-root",
                            str(transcripts),
                        ]
                    )
            self.assertEqual(result, 1)
            self.assertEqual(len(stderr.getvalue().strip().splitlines()), 1)
            self.assertIn("RUN SKIP date=2026-08-02", stderr.getvalue())
            self.assertEqual(
                stdout.getvalue().strip(),
                "[RED] alpha: collector: skipped: lock contention",
            )
            digest = obs_root / "digest" / "2026-08-02.md"
            self.assertEqual(
                digest.read_text(encoding="utf-8"),
                "- [RED] alpha: collector: skipped: lock contention\n",
            )
            self.assertFalse((obs_root / "obslog").exists())
            self.assertFalse((obs_root / "state" / "collector" / "alpha.last-run.json").exists())

    def test_non_file_exists_lock_error_skips_loudly_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs_root = root / "pgl-home"
            transcripts = root / "transcripts"
            transcripts.mkdir()
            config = root / "config.json"
            config.write_text(
                json.dumps(self._config_payload(obs_root, transcripts)), encoding="utf-8"
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch(
                "collectors.claude_code.collector.runtime_status",
                return_value=self._stable_runtime(),
            ), mock.patch(
                "collectors.claude_code.collector.acquire_lock",
                side_effect=OSError(13, "Permission denied"),
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = collector_main(
                    [
                        "--date",
                        "2026-08-02",
                        "--config",
                        str(config),
                        "--transcripts-root",
                        str(transcripts),
                    ]
                )
            self.assertEqual(result, 1)
            self.assertEqual(
                stderr.getvalue().strip(),
                f"RUN SKIP date=2026-08-02 lock={obs_root / 'lock-alpha.d'} "
                "error=Permission denied",
            )
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(
                stdout.getvalue().strip(),
                "[RED] alpha: collector: skipped: lock acquisition failed "
                "error=Permission denied",
            )
            digest = obs_root / "digest" / "2026-08-02.md"
            self.assertEqual(
                digest.read_text(encoding="utf-8"),
                "- [RED] alpha: collector: skipped: lock acquisition failed "
                "error=Permission denied\n",
            )
            self.assertFalse((obs_root / "obslog").exists())
            self.assertFalse((obs_root / "state" / "collector" / "alpha.last-run.json").exists())

    def test_nightly_lock_oserror_skips_loudly_with_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs_root = root / "pgl-home"
            obs_root.mkdir()
            stdout = io.StringIO()
            run_face = mock.Mock()
            with mock.patch.dict(os.environ, {"PGL_HOME": str(obs_root)}):
                with mock.patch(
                    "growthlane.nightly.emit_admission_refusal", return_value=None
                ), mock.patch(
                    "growthlane.nightly.load_json_object",
                    return_value={"classifier_argv": ["configured"]},
                ), mock.patch(
                    "growthlane.nightly.check_all"
                ), mock.patch(
                    "growthlane.nightly.parse_scalar_yaml",
                    return_value=SimpleNamespace(deviations=[]),
                ), mock.patch(
                    "growthlane.nightly.run_face", run_face
                ), mock.patch(
                    "growthlane.nightly.inspect"
                ), mock.patch(
                    "growthlane.nightly.acquire_lock",
                    side_effect=OSError(13, "Permission denied"),
                ), contextlib.redirect_stdout(stdout):
                    result = nightly_module.main(
                        [
                            "--face",
                            "alpha",
                            "--date",
                            "2026-08-02",
                            "--config-dir",
                            str(root),
                        ]
                    )
            self.assertEqual(result, 1)
            run_face.assert_not_called()
            self.assertIn(
                "[RED] alpha: nightly: skipped: lock acquisition failed "
                "error=Permission denied",
                stdout.getvalue(),
            )
            digest = obs_root / "digest" / "2026-08-02.md"
            self.assertIn(
                "- [RED] alpha: nightly: skipped: lock acquisition failed "
                "error=Permission denied",
                digest.read_text(encoding="utf-8"),
            )

    def test_locked_oserror_skips_loudly_with_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs_root = root / "pgl-home"
            obs_root.mkdir()
            digest = Digest(obs_root, "2026-08-02")
            action = mock.Mock()
            with mock.patch(
                "growthlane.operations.acquire_lock",
                side_effect=OSError(13, "Permission denied"),
            ):
                result = _locked(obs_root, "alpha", digest, action)
            self.assertEqual(result, 1)
            action.assert_not_called()
            digest_path = obs_root / "digest" / "2026-08-02.md"
            self.assertEqual(
                digest_path.read_text(encoding="utf-8"),
                "- [RED] alpha: manual operation: skipped: lock acquisition failed "
                "error=Permission denied\n",
            )

    def test_successful_run_writes_last_run_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs_root = root / "pgl-home"
            transcripts = root / "transcripts"
            transcripts.mkdir()
            config = root / "config.json"
            config.write_text(json.dumps(self._config_payload(obs_root, transcripts)), encoding="utf-8")
            with mock.patch(
                "collectors.claude_code.collector.runtime_status",
                return_value=self._stable_runtime(),
            ):
                result = collector_main(
                    [
                        "--date",
                        "2026-08-02",
                        "--config",
                        str(config),
                        "--transcripts-root",
                        str(transcripts),
                    ]
                )
            self.assertEqual(result, 0)
            marker = obs_root / "state" / "collector" / "alpha.last-run.json"
            self.assertTrue(marker.is_file())
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(
                list(payload),
                [
                    "schema_version",
                    "face",
                    "run_at",
                    "date",
                    "sources_scanned",
                    "records_written",
                    "usage_enabled",
                    "errors",
                    "ucd",
                ],
            )
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["face"], "alpha")
            self.assertEqual(payload["date"], "2026-08-02")
            self.assertEqual(payload["sources_scanned"], 0)
            self.assertEqual(payload["records_written"], 0)
            self.assertFalse(payload["usage_enabled"])
            self.assertEqual(payload["errors"], 0)
            self.assertTrue(payload["run_at"].endswith("+09:00"))
            self.assertIsInstance(payload["ucd"], str)
            self.assertEqual(os.stat(marker).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(marker.parent).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(marker.parent.parent).st_mode & 0o777, 0o700)

    def test_ucd_drift_refuses_run_and_skips_success_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs_root = root / "pgl-home"
            transcripts = root / "transcripts"
            transcripts.mkdir()
            config = root / "config.json"
            config.write_text(json.dumps(self._config_payload(obs_root, transcripts)), encoding="utf-8")
            stderr = []

            def capture(*args: object, **kwargs: object) -> None:
                if kwargs.get("file") is sys.stderr and args:
                    stderr.append(str(args[0]))

            with mock.patch(
                "collectors.claude_code.collector.runtime_status",
                return_value=UcdRuntimeStatus(
                    corpus_version="16.0.0",
                    runtime_version="17.0.0",
                    drifted=True,
                ),
            ):
                with mock.patch("builtins.print", side_effect=capture):
                    result = collector_main(
                        [
                            "--date",
                            "2026-08-02",
                            "--config",
                            str(config),
                            "--transcripts-root",
                            str(transcripts),
                        ]
                    )
            self.assertEqual(result, 1)
            self.assertTrue(
                any("[RED] alpha: UCD drift runtime=17.0.0 corpus=16.0.0 direction=runtime>corpus" in line for line in stderr),
                stderr,
            )
            self.assertFalse((obs_root / "obslog").exists())
            self.assertFalse((obs_root / "state" / "collector" / "alpha.last-run.json").exists())


if __name__ == "__main__":
    unittest.main()
