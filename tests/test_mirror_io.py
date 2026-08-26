import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from growthlane.faces import get_profile
from mirror.common import (
    MIRROR_LOCK_STALE_HOURS,
    MirrorError,
    atomic_write,
    injected_bytes,
    mirror_lock,
    parse_run_date,
)


JST = timezone(timedelta(hours=9))


def _mark_stale(path: Path, *, hours: int = MIRROR_LOCK_STALE_HOURS + 1) -> None:
    timestamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    os.utime(path, (timestamp, timestamp))


def _make_reclaim_tombstone(root: Path, *, age_hours: int, pid: int = 12345) -> Path:
    created_ns = time.time_ns() - int(timedelta(hours=age_hours).total_seconds() * 1_000_000_000)
    tombstone = root / f"lock.d.reclaim-{pid}-{created_ns}"
    tombstone.mkdir()
    (tombstone / "owner.json").write_text("{}", encoding="utf-8")
    return tombstone


class MirrorIOTests(unittest.TestCase):
    def test_parse_run_date_rejects_explicit_future_jst_but_keeps_default(self) -> None:
        today = datetime.now(JST).date()

        self.assertEqual(parse_run_date(None), today.isoformat())
        self.assertEqual(parse_run_date(today.isoformat()), today.isoformat())
        with self.assertRaisesRegex(MirrorError, "future JST date"):
            parse_run_date((today + timedelta(days=1)).isoformat())

    def test_mirror_lock_writes_owner_and_recovers_stale_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            stale = datetime.now(timezone.utc) - timedelta(hours=25)
            (lock / "owner.json").write_text(
                json.dumps({"pid": 12345, "host": "old-host", "started_at": stale.isoformat()}),
                encoding="utf-8",
            )
            alerts: list[str] = []

            with mirror_lock(home, alerts.append) as acquired:
                self.assertTrue(acquired)
                owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
                self.assertEqual(owner["pid"], os.getpid())
                self.assertIn("started_at", owner)

            self.assertFalse(lock.exists())
            self.assertTrue(any("stale mirror lock recovered" in item for item in alerts), alerts)

    def test_mirror_lock_respects_fresh_owner_and_escalates_contention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            (lock / "owner.json").write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": "live-host",
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            alerts: list[str] = []

            with mirror_lock(home, alerts.append) as acquired:
                self.assertFalse(acquired)

            self.assertTrue(lock.is_dir())
            self.assertTrue(any("mirror lock contention" in item for item in alerts), alerts)

    def test_mirror_lock_recovers_stale_owner_with_atomic_write_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            stale = datetime.now(timezone.utc) - timedelta(hours=25)
            (lock / "owner.json").write_text(
                json.dumps({"pid": 12345, "host": "old-host", "started_at": stale.isoformat()}),
                encoding="utf-8",
            )
            (lock / ".owner.json.tmp-99999").write_text("partial", encoding="utf-8")

            with mirror_lock(home) as acquired:
                self.assertTrue(acquired)

            self.assertFalse(lock.exists())

    def test_mirror_lock_does_not_reclaim_unexpected_or_symlink_children(self) -> None:
        for child_name, shape in (
            ("unexpected", "file"),
            (".owner.json.tmp-linked", "symlink"),
            (".owner.json.tmp-directory", "directory"),
        ):
            with self.subTest(child=child_name), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary).resolve()
                lock = home / "mirror" / "lock.d"
                lock.mkdir(parents=True)
                stale = datetime.now(timezone.utc) - timedelta(hours=25)
                owner = lock / "owner.json"
                owner.write_text(
                    json.dumps({"pid": 12345, "host": "old-host", "started_at": stale.isoformat()}),
                    encoding="utf-8",
                )
                child = lock / child_name
                if shape == "symlink":
                    child.symlink_to(owner)
                elif shape == "directory":
                    child.mkdir()
                else:
                    child.write_text("do not remove", encoding="utf-8")

                with mirror_lock(home) as acquired:
                    self.assertFalse(acquired)

                self.assertTrue(lock.is_dir())
                self.assertTrue(owner.is_file())
                self.assertTrue(
                    child.is_symlink()
                    if shape == "symlink"
                    else child.is_dir() if shape == "directory" else child.is_file()
                )

    def test_mirror_lock_future_owner_falls_back_to_stale_directory_age(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            future = datetime.now(timezone.utc) + timedelta(hours=72)
            (lock / "owner.json").write_text(
                json.dumps({"pid": 12345, "host": "bad-clock", "started_at": future.isoformat()}),
                encoding="utf-8",
            )
            stale_timestamp = (datetime.now(timezone.utc) - timedelta(hours=25)).timestamp()
            os.utime(lock, (stale_timestamp, stale_timestamp))

            with mirror_lock(home) as acquired:
                self.assertTrue(acquired)

            self.assertFalse(lock.exists())

    def test_mirror_lock_rename_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            stale = datetime.now(timezone.utc) - timedelta(hours=25)
            owner = lock / "owner.json"
            owner.write_text(
                json.dumps({"pid": 12345, "host": "old-host", "started_at": stale.isoformat()}),
                encoding="utf-8",
            )

            with mock.patch("mirror.common.os.rename", side_effect=OSError("rename lost race")):
                with mirror_lock(home) as acquired:
                    self.assertFalse(acquired)

            self.assertTrue(lock.is_dir())
            self.assertTrue(owner.is_file())

    def test_mirror_lock_pinned_inode_defeats_simulated_inode_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            stale = datetime.now(timezone.utc) - timedelta(hours=25)
            owner = lock / "owner.json"
            owner.write_text(
                json.dumps({"pid": 12345, "host": "old-host", "started_at": stale.isoformat()}),
                encoding="utf-8",
            )

            real_lstat = os.lstat
            real_open = os.open
            stale_metadata = real_lstat(lock)
            lock_inode_pinned = False
            lock_replaced = False
            observed_fds: list[int] = []

            def tracking_open(
                path: os.PathLike[str] | str, flags: int, mode: int = 0o777
            ) -> int:
                nonlocal lock_inode_pinned
                fd = real_open(path, flags, mode)
                if Path(path) == lock:
                    lock_inode_pinned = True
                    observed_fds.append(fd)
                return fd

            def fake_lstat(path: os.PathLike[str] | str) -> os.stat_result | SimpleNamespace:
                result = real_lstat(path)
                candidate = Path(path)
                # Model ext4 reusing the released inode only when no directory fd
                # pinned it. The old tuple-only check then sees a false identity match.
                if lock_replaced and not lock_inode_pinned and (
                    candidate == lock or candidate.name.startswith("lock.d.reclaim-")
                ):
                    return SimpleNamespace(
                        st_mode=result.st_mode,
                        st_dev=stale_metadata.st_dev,
                        st_ino=stale_metadata.st_ino,
                        st_mtime=result.st_mtime,
                    )
                return result

            def replace_with_live_lock(_lock: Path) -> tuple[datetime, int, str]:
                nonlocal lock_replaced
                owner.unlink()
                lock.rmdir()
                lock.mkdir()
                owner.write_text(
                    json.dumps(
                        {
                            "pid": 67890,
                            "host": "live-host",
                            "started_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ),
                    encoding="utf-8",
                )
                lock_replaced = True
                return stale, 12345, "old-host"

            with mock.patch("mirror.common.os.open", side_effect=tracking_open):
                with mock.patch("mirror.common.os.lstat", side_effect=fake_lstat):
                    with mock.patch(
                        "mirror.common._mirror_lock_owner", side_effect=replace_with_live_lock
                    ):
                        with mirror_lock(home) as acquired:
                            self.assertFalse(acquired)

            live_owner = json.loads(owner.read_text(encoding="utf-8"))
            self.assertEqual(live_owner["pid"], 67890)
            self.assertTrue(lock.is_dir())
            self.assertEqual(len(observed_fds), 1)
            with self.assertRaises(OSError):
                os.fstat(observed_fds[0])

    def test_mirror_lock_closes_pinned_fds_after_reclaim_and_contention(self) -> None:
        for stale in (True, False):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary).resolve()
                lock = home / "mirror" / "lock.d"
                lock.mkdir(parents=True)
                started = datetime.now(timezone.utc) - timedelta(hours=25 if stale else 0)
                (lock / "owner.json").write_text(
                    json.dumps(
                        {"pid": 12345, "host": "host", "started_at": started.isoformat()}
                    ),
                    encoding="utf-8",
                )
                real_open = os.open
                real_close = os.close
                opened: list[int] = []
                closed: list[int] = []

                def tracking_open(
                    path: os.PathLike[str] | str, flags: int, mode: int = 0o777
                ) -> int:
                    fd = real_open(path, flags, mode)
                    if Path(path) == lock:
                        opened.append(fd)
                    return fd

                def tracking_close(fd: int) -> None:
                    if fd in opened:
                        closed.append(fd)
                    real_close(fd)

                with mock.patch("mirror.common.os.open", side_effect=tracking_open):
                    with mock.patch("mirror.common.os.close", side_effect=tracking_close):
                        with mirror_lock(home) as acquired:
                            self.assertEqual(acquired, stale)

                self.assertGreaterEqual(len(opened), 1)
                self.assertCountEqual(closed, opened)
                for fd in opened:
                    with self.assertRaises(OSError):
                        os.fstat(fd)

    def test_mirror_lock_vanishing_before_open_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            _mark_stale(lock)

            with mock.patch("mirror.common.os.open", side_effect=FileNotFoundError()):
                with mirror_lock(home) as acquired:
                    self.assertFalse(acquired)

            self.assertTrue(lock.is_dir())

    def test_mirror_lock_restores_tombstone_after_post_rename_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            stale = datetime.now(timezone.utc) - timedelta(hours=25)
            owner = lock / "owner.json"
            owner.write_text(
                json.dumps({"pid": 12345, "host": "old-host", "started_at": stale.isoformat()}),
                encoding="utf-8",
            )

            real_lstat = os.lstat

            def fake_lstat(path: os.PathLike[str] | str) -> os.stat_result | SimpleNamespace:
                result = real_lstat(path)
                candidate = Path(path)
                if candidate.name.startswith("lock.d.reclaim-"):
                    return SimpleNamespace(
                        st_mode=result.st_mode,
                        st_dev=result.st_dev,
                        st_ino=result.st_ino + 1,
                        st_mtime=result.st_mtime,
                    )
                return result

            with mock.patch("mirror.common.os.lstat", side_effect=fake_lstat):
                with mirror_lock(home) as acquired:
                    self.assertFalse(acquired)

            self.assertTrue(lock.is_dir())
            self.assertTrue(owner.is_file())
            self.assertEqual(
                sorted(path.name for path in (home / "mirror").iterdir()),
                ["lock.d"],
            )

    def test_mirror_lock_failed_restore_preserves_tombstone_and_alerts_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            stale = datetime.now(timezone.utc) - timedelta(hours=25)
            owner = lock / "owner.json"
            owner.write_text(
                json.dumps({"pid": 12345, "host": "old-host", "started_at": stale.isoformat()}),
                encoding="utf-8",
            )
            alerts: list[str] = []

            real_lstat = os.lstat
            real_rename = os.rename

            def fake_lstat(path: os.PathLike[str] | str) -> os.stat_result | SimpleNamespace:
                result = real_lstat(path)
                candidate = Path(path)
                if candidate.name.startswith("lock.d.reclaim-"):
                    return SimpleNamespace(
                        st_mode=result.st_mode,
                        st_dev=result.st_dev,
                        st_ino=result.st_ino + 1,
                        st_mtime=result.st_mtime,
                    )
                return result

            def fake_rename(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
                if Path(src).name.startswith("lock.d.reclaim-") and Path(dst).name == "lock.d":
                    raise OSError("rename back failed")
                real_rename(src, dst)

            with mock.patch("mirror.common.os.lstat", side_effect=fake_lstat):
                with mock.patch("mirror.common.os.rename", side_effect=fake_rename):
                    with mirror_lock(home, alerts.append) as acquired:
                        self.assertFalse(acquired)

            tombstones = sorted((home / "mirror").glob("lock.d.reclaim-*"))
            self.assertEqual(len(tombstones), 1)
            self.assertFalse(lock.exists())
            self.assertTrue((tombstones[0] / "owner.json").is_file())
            self.assertTrue(
                any(
                    "reclaim mismatch: live lock preserved as" in item
                    and "manual review" in item
                    for item in alerts
                ),
                alerts,
            )

    def test_mirror_lock_restore_rejects_live_empty_lock_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            stale = datetime.now(timezone.utc) - timedelta(hours=25)
            owner = lock / "owner.json"
            owner.write_text(
                json.dumps({"pid": 12345, "host": "old-host", "started_at": stale.isoformat()}),
                encoding="utf-8",
            )
            alerts: list[str] = []

            real_lstat = os.lstat
            recreated_live_lock = False

            def fake_lstat(path: os.PathLike[str] | str) -> os.stat_result | SimpleNamespace:
                nonlocal recreated_live_lock
                result = real_lstat(path)
                candidate = Path(path)
                if candidate.name.startswith("lock.d.reclaim-"):
                    if not recreated_live_lock:
                        lock.mkdir()
                        recreated_live_lock = True
                    return SimpleNamespace(
                        st_mode=result.st_mode,
                        st_dev=result.st_dev,
                        st_ino=result.st_ino + 1,
                        st_mtime=result.st_mtime,
                    )
                return result

            with mock.patch("mirror.common.os.lstat", side_effect=fake_lstat):
                with mirror_lock(home, alerts.append) as acquired:
                    self.assertFalse(acquired)

            tombstones = sorted((home / "mirror").glob("lock.d.reclaim-*"))
            self.assertTrue(lock.is_dir())
            self.assertEqual(list(lock.iterdir()), [])
            self.assertEqual(len(tombstones), 1)
            self.assertTrue((tombstones[0] / "owner.json").is_file())
            self.assertTrue(
                any(
                    "reclaim mismatch: live lock preserved as" in item
                    and "manual review" in item
                    for item in alerts
                ),
                alerts,
            )

    def test_mirror_lock_restores_after_post_rename_lstat_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            stale = datetime.now(timezone.utc) - timedelta(hours=25)
            owner = lock / "owner.json"
            owner.write_text(
                json.dumps({"pid": 12345, "host": "old-host", "started_at": stale.isoformat()}),
                encoding="utf-8",
            )

            real_lstat = os.lstat

            def fake_lstat(path: os.PathLike[str] | str) -> os.stat_result:
                candidate = Path(path)
                if candidate.name.startswith("lock.d.reclaim-"):
                    raise OSError("lost tombstone stat")
                return real_lstat(path)

            with mock.patch("mirror.common.os.lstat", side_effect=fake_lstat):
                with mirror_lock(home) as acquired:
                    self.assertFalse(acquired)

            self.assertTrue(lock.is_dir())
            self.assertTrue(owner.is_file())
            self.assertEqual(
                sorted(path.name for path in (home / "mirror").iterdir()),
                ["lock.d"],
            )

    def test_mirror_lock_missing_owner_uses_directory_age_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"
            lock.mkdir(parents=True)
            alerts: list[str] = []

            with mirror_lock(home, alerts.append) as acquired:
                self.assertFalse(acquired)
            self.assertTrue(lock.is_dir())

            stale_timestamp = (datetime.now(timezone.utc) - timedelta(hours=25)).timestamp()
            os.utime(lock, (stale_timestamp, stale_timestamp))
            alerts.clear()
            with mirror_lock(home, alerts.append) as acquired:
                self.assertTrue(acquired)
            self.assertFalse(lock.exists())
            self.assertTrue(any("stale mirror lock recovered" in item for item in alerts), alerts)

    def test_mirror_lock_sweep_uses_embedded_reclaim_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            root = home / "mirror"
            root.mkdir(parents=True)
            fresh_name_stale_mtime = _make_reclaim_tombstone(root, age_hours=0)
            _mark_stale(fresh_name_stale_mtime)
            stale_name_fresh_mtime = _make_reclaim_tombstone(
                root, age_hours=MIRROR_LOCK_STALE_HOURS + 1, pid=12346
            )
            malformed = root / "lock.d.reclaim-malformed"
            malformed.mkdir()
            (malformed / "owner.json").write_text("{}", encoding="utf-8")
            _mark_stale(malformed)

            with mirror_lock(home) as acquired:
                self.assertTrue(acquired)

            self.assertTrue(fresh_name_stale_mtime.is_dir())
            self.assertFalse(stale_name_fresh_mtime.exists())
            self.assertTrue(malformed.is_dir())

    def test_mirror_lock_sweep_keeps_malformed_tombstone_with_unexpected_child_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            root = home / "mirror"
            root.mkdir(parents=True)
            alerts: list[str] = []

            malformed = root / "lock.d.reclaim-malformed"
            malformed.mkdir()
            (malformed / "owner.json").write_text("{}", encoding="utf-8")
            (malformed / "unexpected").write_text("keep", encoding="utf-8")
            _mark_stale(malformed)

            with mirror_lock(home, alerts.append) as acquired:
                self.assertTrue(acquired)

            self.assertTrue(malformed.is_dir())
            self.assertTrue(
                any(
                    "left unsafe tombstone" in item
                    and f"path={malformed}" in item
                    and "reason=unexpected-children" in item
                    for item in alerts
                ),
                alerts,
            )

    def test_mirror_lock_snapshot_lstat_oserror_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            lock = home / "mirror" / "lock.d"

            real_lstat = os.lstat
            alerts: list[str] = []

            def fake_lstat(path: os.PathLike[str] | str) -> os.stat_result:
                candidate = Path(path)
                if candidate == lock and candidate.exists() and not (candidate / "owner.json").exists():
                    raise OSError("snapshot lost")
                return real_lstat(path)

            with mock.patch("mirror.common.os.lstat", side_effect=fake_lstat):
                with mirror_lock(home, alerts.append) as acquired:
                    self.assertFalse(acquired)

            self.assertFalse(lock.exists())
            self.assertEqual(len(alerts), 1)
            self.assertIn(
                "mirror lock acquisition failed after mkdir; post-mkdir lstat unavailable: snapshot lost",
                alerts[0],
            )

    def test_mirror_lock_leaves_unsafe_reclaim_tombstones_with_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            root = home / "mirror"
            root.mkdir(parents=True)
            alerts: list[str] = []

            unexpected = _make_reclaim_tombstone(root, age_hours=MIRROR_LOCK_STALE_HOURS + 1)
            (unexpected / "unexpected").write_text("keep", encoding="utf-8")

            symlinked = _make_reclaim_tombstone(
                root, age_hours=MIRROR_LOCK_STALE_HOURS + 1, pid=12346
            )
            owner = symlinked / "owner.json"
            (symlinked / ".owner.json.tmp-linked").symlink_to(owner)

            with mirror_lock(home, alerts.append) as acquired:
                self.assertTrue(acquired)

            self.assertTrue(unexpected.is_dir())
            self.assertTrue(symlinked.is_dir())
            self.assertTrue(any("left unsafe tombstone" in item for item in alerts), alerts)

    def test_mirror_lock_alerts_on_nonconforming_reclaim_tombstone_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            root = home / "mirror"
            root.mkdir(parents=True)
            alerts: list[str] = []

            nonconforming = root / "lock.d.reclaim-garbage"
            nonconforming.mkdir()
            (nonconforming / "owner.json").write_text("{}", encoding="utf-8")

            superscript = root / "lock.d.reclaim-1-²²²"
            superscript.mkdir()
            (superscript / "owner.json").write_text("{}", encoding="utf-8")

            with mirror_lock(home, alerts.append) as acquired:
                self.assertTrue(acquired)

            self.assertTrue(nonconforming.is_dir())
            self.assertTrue(superscript.is_dir())
            for target in (nonconforming, superscript):
                self.assertTrue(
                    any(
                        "left unsafe tombstone" in item
                        and f"path={target}" in item
                        and "reason=nonconforming-name" in item
                        for item in alerts
                    ),
                    alerts,
                )

    def test_luca_missing_build_returns_explicit_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            overlay_home = root / "luca"
            overlay_home.mkdir()
            staging_root = root / "luca-staging"
            staging_root.mkdir()

            files, warning = injected_bytes(
                get_profile("luca"),
                root / "pgl-home",
                {
                    "overlay_home_root": str(overlay_home),
                    "staging_root": str(staging_root),
                },
            )

            self.assertEqual(files, {})
            self.assertIsNotNone(warning)
            self.assertIn("build directory absent or unsafe", warning)
            self.assertIn(str(staging_root / "build"), warning)

    def test_atomic_write_secures_new_directories_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            existing = Path(temporary).resolve() / "existing"
            existing.mkdir(mode=0o755)
            os.chmod(existing, 0o755)
            target = existing / "first" / "second" / "payload.bin"

            previous_umask = os.umask(0o022)
            try:
                atomic_write(target, b"private payload")
            finally:
                os.umask(previous_umask)

            self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((existing / "first").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((existing / "first" / "second").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.read_bytes(), b"private payload")

    def test_atomic_write_removes_temporary_after_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / "payload.bin"
            target.write_bytes(b"original")
            expected_temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")

            with mock.patch("mirror.common.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    atomic_write(target, b"replacement")

            self.assertEqual(target.read_bytes(), b"original")
            self.assertFalse(expected_temporary.exists())

    def test_atomic_write_opens_parent_safely_for_directory_fsync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / "payload.bin"

            with mock.patch("mirror.common.os.open", wraps=os.open) as open_mock:
                atomic_write(target, b"payload")

            directory_calls = [
                call for call in open_mock.call_args_list if call.args[0] == target.parent
            ]
            self.assertEqual(len(directory_calls), 1)
            flags = directory_calls[0].args[1]
            if hasattr(os, "O_NOFOLLOW"):
                self.assertTrue(flags & os.O_NOFOLLOW)
            if hasattr(os, "O_DIRECTORY"):
                self.assertTrue(flags & os.O_DIRECTORY)


if __name__ == "__main__":
    unittest.main()
