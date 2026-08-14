from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from collectors.hermes_luca import rules
from collectors.hermes_luca.adapters import AdapterError, LocalSQLiteAdapter, SSHAdapter, Turn
from collectors.hermes_luca.collector import collect, jst_epoch_window, main
from collectors.hermes_luca.config import ConfigError, load_config
from growthlane.locking import acquire_lock
from growthlane.ucd_runtime import runtime_status


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "hermes_luca"
RUN_DATE = "2026-08-02"


class LucaCollectorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "state.db"
        self.config_path = self.root / "obs-collector-luca.json"
        self.owners_path = self.root / "owners.json"
        self.denylist_path = self.root / "obs-denylist.txt"
        self.obs_root = self.root / "obs-home"
        self.ledger_fixture = FIXTURES / "overlay-ledger.yml"
        self._message_id = 1

        shutil.copyfile(FIXTURES / "owners.json", self.owners_path)
        shutil.copyfile(FIXTURES / "denylist.txt", self.denylist_path)
        self._seed_ledger(self.obs_root)

        self._reset_db()
        self._write_config()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _reset_db(self) -> None:
        if self.db_path.exists():
            self.db_path.unlink()
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            connection.executescript((FIXTURES / "schema.sql").read_text(encoding="utf-8"))
            connection.commit()

    def _base_config(self) -> dict[str, object]:
        return {
            "face": "luca",
            "host": "vps-hermes",
            "speaker": "owner",
            "obs_root": str(self.obs_root),
            "source": {
                "ssh_host": "fixture-ssh",
                "kind": "hermes-state-db",
                "db_path": str(self.db_path),
                "owner_uids": {
                    "telegram": ["100000001"],
                    "slack": ["U0EXAMPLE01"],
                },
                "expected_dm_entries": {
                    "telegram": ["100000001"],
                    "slack": ["D0EXAMPLE01"],
                },
                "sources": ["telegram", "slack", "api_server"],
                "dm_only": True,
                "exclude_session_prefixes": ["pgl-verify-"],
                "exclude_session_ledger": str(
                    self.obs_root / "state" / "luca-verify-sessions.jsonl"
                ),
                "voice_enabled": False,
            },
            "denylist_path": str(self.denylist_path),
        }

    def _write_config(self, payload: dict[str, object] | None = None) -> None:
        self.config_path.write_text(
            json.dumps(payload or self._base_config(), ensure_ascii=False),
            encoding="utf-8",
        )

    def _add_session(
        self,
        session_id: str,
        *,
        source: str,
        chat_type: str | None,
        user_id: str | None,
        session_key: str | None,
    ) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO sessions (id, source, chat_type, user_id, session_key) VALUES (?, ?, ?, ?, ?)",
                (session_id, source, chat_type, user_id, session_key),
            )
            connection.commit()

    def _add_message(self, session_id: str, *, content: str | None, timestamp: int, role: str) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO messages (id, session_id, content, timestamp, role) VALUES (?, ?, ?, ?, ?)",
                (self._message_id, session_id, content, timestamp, role),
            )
            connection.commit()
        self._message_id += 1

    def _add_turns(
        self,
        session_id: str,
        *,
        source: str,
        chat_type: str | None,
        user_id: str | None,
        session_key: str | None,
        turns: list[tuple[str, str, int]],
    ) -> None:
        self._add_session(
            session_id,
            source=source,
            chat_type=chat_type,
            user_id=user_id,
            session_key=session_key,
        )
        for role, content, timestamp in turns:
            self._add_message(session_id, content=content, timestamp=timestamp, role=role)

    def _collect(self, *, obs_root: Path | None = None) -> dict[str, object]:
        target_obs_root = obs_root or self.obs_root
        self._seed_ledger(target_obs_root)
        return collect(
            RUN_DATE,
            config_path=self.config_path,
            sqlite_path=self.db_path,
            owners_json=self.owners_path,
            obs_root=target_obs_root,
        )

    def _records_path(self, obs_root: Path | None = None) -> Path:
        return (obs_root or self.obs_root) / "obslog" / "luca" / f"{RUN_DATE}.jsonl"

    def _usage_path(self, obs_root: Path | None = None) -> Path:
        return (obs_root or self.obs_root) / "obslog" / "luca" / f"usage-{RUN_DATE}.jsonl"

    def _marker_path(self, obs_root: Path | None = None) -> Path:
        return (obs_root or self.obs_root) / "state" / "collector" / "luca.last-run.json"

    def _marker_payload_without_run_at(self, obs_root: Path | None = None) -> dict[str, object]:
        # run_at is a real wall-clock read (see collector.py's collect()), so
        # two collect() calls a moment apart are not required to agree on it
        # even though every other marker field is fully deterministic given
        # identical inputs. Callers compare this stable payload for
        # byte-for-byte determinism and check run_at's shape separately.
        payload = json.loads(self._marker_path(obs_root).read_text(encoding="utf-8"))
        run_at = payload.pop("run_at")
        self.assertTrue(run_at.endswith("+09:00"), run_at)
        datetime.fromisoformat(run_at)
        return payload

    def _load_jsonl(self, path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _snapshot_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                files[str(path.relative_to(self.root))] = path.read_bytes()
        return files

    def _seed_ledger(self, obs_root: Path) -> None:
        faces_dir = obs_root / "faces" / "luca" / "growth"
        faces_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.ledger_fixture, faces_dir / "overlay-ledger.yml")

    def _write_valid_voice_safety_files(self, obs_root: Path | None = None) -> None:
        target_obs_root = obs_root or self.obs_root
        state_dir = target_obs_root / "state"
        collector_dir = state_dir / "collector"
        collector_dir.mkdir(parents=True, exist_ok=True)
        journal_path = state_dir / "luca-intent-journal.jsonl"
        journal_path.write_bytes(b"")
        acceptance_ledger_path = state_dir / "luca-verify-sessions.jsonl"
        acceptance_ledger_path.write_text(
            json.dumps(
                {
                    "session_id": "seed-fixture-session",
                    "recorded_at": "2026-08-01T00:00:00+09:00",
                    "origin": "seed",
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        baseline_path = collector_dir / "luca.ledger-lines.json"
        baseline_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "line_count": 1,
                    "recorded_at": "2026-08-01T00:00:00+09:00",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        for directory in (target_obs_root, state_dir, collector_dir):
            os.chmod(directory, 0o700)
        for path in (journal_path, acceptance_ledger_path, baseline_path):
            os.chmod(path, 0o600)

    def _ssh_dispatcher_side_effect(self, sessions_payload: bytes):
        # Mimics the read-only ssh dispatcher: `read-owners` always returns
        # the fixture owner directory; `read-sessions` returns whatever
        # JSONL stream the caller staged for this test.
        owners_payload = (FIXTURES / "owners.json").read_bytes()

        def _run(cmd, **kwargs):
            subcommand = cmd[2]
            if subcommand == "read-owners":
                return mock.Mock(returncode=0, stdout=owners_payload, stderr=b"")
            if subcommand == "read-sessions":
                return mock.Mock(returncode=0, stdout=sessions_payload, stderr=b"")
            raise AssertionError(f"unexpected ssh subcommand: {subcommand}")

        return _run


class LucaConfigTests(LucaCollectorTestCase):
    def test_load_config_rejects_transcripts_root(self) -> None:
        payload = self._base_config()
        payload["transcripts_root"] = "/tmp/forbidden"
        self._write_config(payload)
        with self.assertRaisesRegex(ConfigError, "transcripts_root is not allowed"):
            load_config(self.config_path)

    def test_load_config_rejects_unknown_top_level_key(self) -> None:
        payload = self._base_config()
        payload["unexpected"] = True
        self._write_config(payload)
        with self.assertRaisesRegex(ConfigError, "config has unknown keys: unexpected"):
            load_config(self.config_path)

    def test_load_config_rejects_missing_source_key(self) -> None:
        payload = self._base_config()
        del payload["source"]["db_path"]  # type: ignore[index]
        self._write_config(payload)
        with self.assertRaisesRegex(ConfigError, "config.source is missing keys: db_path"):
            load_config(self.config_path)

    def test_load_config_rejects_missing_expected_dm_entries(self) -> None:
        payload = self._base_config()
        del payload["source"]["expected_dm_entries"]  # type: ignore[index]
        self._write_config(payload)
        with self.assertRaisesRegex(
            ConfigError, "config.source is missing keys: expected_dm_entries"
        ):
            load_config(self.config_path)

    def test_load_config_rejects_unsupported_expected_dm_entries_platform(self) -> None:
        payload = self._base_config()
        payload["source"]["expected_dm_entries"] = {  # type: ignore[index]
            "telegram": ["100000001"],
            "api_server": ["x"],
        }
        self._write_config(payload)
        with self.assertRaisesRegex(
            ConfigError,
            "config.source.expected_dm_entries has unknown keys: api_server",
        ):
            load_config(self.config_path)

    def test_load_config_loads_expected_dm_entries(self) -> None:
        config = load_config(self.config_path)
        self.assertEqual(
            config.source.expected_dm_entries,
            {"telegram": ("100000001",), "slack": ("D0EXAMPLE01",)},
        )

    def test_load_config_resolves_acceptance_ledger_path(self) -> None:
        payload = self._base_config()
        payload["source"]["exclude_session_ledger"] = "state/ledger.jsonl"  # type: ignore[index]
        self._write_config(payload)
        config = load_config(self.config_path)
        self.assertEqual(
            config.source.exclude_session_ledger,
            self.config_path.parent.absolute() / "state" / "ledger.jsonl",
        )

    def test_load_config_absolutizes_acceptance_ledger_path_without_resolving_symlinks(self) -> None:
        sandbox = self.root / "ledger-path-sandbox"
        real_dir = sandbox / "real"
        real_dir.mkdir(parents=True)
        link_dir = sandbox / "linked"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        config_path = link_dir / "obs-collector-luca.json"
        payload = self._base_config()
        payload["source"]["exclude_session_ledger"] = "state/ledger.jsonl"  # type: ignore[index]
        config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        config = load_config(config_path)
        self.assertEqual(
            config.source.exclude_session_ledger,
            link_dir.absolute() / "state" / "ledger.jsonl",
        )
        self.assertNotEqual(
            config.source.exclude_session_ledger,
            real_dir.absolute() / "state" / "ledger.jsonl",
        )

    def test_load_config_requires_ledger_when_voice_enabled(self) -> None:
        payload = self._base_config()
        payload["source"]["sources"] = ["telegram", "slack"]  # type: ignore[index]
        payload["source"]["voice_enabled"] = True  # type: ignore[index]
        del payload["source"]["exclude_session_ledger"]  # type: ignore[index]
        self._write_config(payload)
        with self.assertRaisesRegex(ConfigError, "exclude_session_ledger"):
            load_config(self.config_path)

    def test_load_config_requires_ledger_when_api_server_is_sourced(self) -> None:
        payload = self._base_config()
        payload["source"]["voice_enabled"] = False  # type: ignore[index]
        del payload["source"]["exclude_session_ledger"]  # type: ignore[index]
        self._write_config(payload)
        with self.assertRaisesRegex(ConfigError, "exclude_session_ledger"):
            load_config(self.config_path)

    def test_load_config_rejects_non_boolean_voice_enabled(self) -> None:
        payload = self._base_config()
        payload["source"]["voice_enabled"] = "yes"  # type: ignore[index]
        self._write_config(payload)
        with self.assertRaisesRegex(ConfigError, "config.source.voice_enabled must be a boolean"):
            load_config(self.config_path)

    def test_load_config_rejects_ssh_option_instead_of_host_alias(self) -> None:
        payload = self._base_config()
        payload["source"]["ssh_host"] = "-oProxyCommand=echo"  # type: ignore[index]
        self._write_config(payload)
        with self.assertRaisesRegex(ConfigError, "must be a host alias"):
            load_config(self.config_path)

    def test_load_config_defaults_voice_enabled_false(self) -> None:
        payload = self._base_config()
        del payload["source"]["voice_enabled"]  # type: ignore[index]
        self._write_config(payload)
        config = load_config(self.config_path)
        self.assertFalse(config.source.voice_enabled)

    def test_jst_epoch_window_returns_integer_bounds(self) -> None:
        start_epoch, end_epoch = jst_epoch_window(RUN_DATE)
        self.assertEqual((start_epoch, end_epoch), (1785596400, 1785682800))
        self.assertIsInstance(start_epoch, int)
        self.assertIsInstance(end_epoch, int)


class LucaAdapterTests(LucaCollectorTestCase):
    def test_ssh_adapter_rejects_option_instead_of_host_alias(self) -> None:
        with self.assertRaisesRegex(AdapterError, "must be a host alias"):
            SSHAdapter("-oProxyCommand=echo")

    def test_local_sqlite_adapter_uses_read_only_uri_and_preserves_db_bytes(self) -> None:
        start_epoch, end_epoch = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "tg-session-1",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="tg-session-key-1",
            turns=[("user", "owner text", start_epoch + 60)],
        )
        before = self.db_path.read_bytes()
        rows = LocalSQLiteAdapter(self.db_path).fetch_turns(start_epoch, end_epoch, ("telegram",))
        after = self.db_path.read_bytes()
        self.assertEqual([(row.session_id, row.content) for row in rows], [("tg-session-1", "owner text")])
        self.assertEqual(before, after)
        self.assertEqual(
            LocalSQLiteAdapter(self.db_path)._connection_uri(),
            f"file:{self.db_path}?mode=ro",
        )

    def test_local_sqlite_adapter_rejects_schema_mismatch(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("DROP TABLE messages")
            connection.commit()
        start_epoch, end_epoch = jst_epoch_window(RUN_DATE)
        with self.assertRaisesRegex(AdapterError, "sqlite schema/query failure"):
            LocalSQLiteAdapter(self.db_path).fetch_turns(start_epoch, end_epoch, ("telegram",))

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_validate_owner_directory_uses_exact_argv(self, run_mock: mock.Mock) -> None:
        run_mock.return_value = mock.Mock(
            returncode=0,
            stdout=(FIXTURES / "owners.json").read_bytes(),
            stderr=b"",
        )
        SSHAdapter("fixture-host").validate_owner_directory(
            {"telegram": ["100000001"], "slack": ["D0EXAMPLE01"]},
            ("telegram", "slack"),
        )
        run_mock.assert_called_once_with(
            ["ssh", "fixture-host", "read-owners"],
            stdout=mock.ANY,
            stderr=mock.ANY,
            text=False,
            check=False,
        )
        self.assertIs(run_mock.call_args.kwargs["stdout"], -1)
        self.assertIs(run_mock.call_args.kwargs["stderr"], -1)

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_validate_owner_directory_accepts_slack_thread_suffix(
        self, run_mock: mock.Mock
    ) -> None:
        # Hermes channel_directory.json DM entries are thread-scoped
        # (`D0EXAMPLE01:<thread_ts>`), not the Slack member id; the
        # self-check must accept this real shape (multiple thread entries).
        run_mock.return_value = mock.Mock(
            returncode=0,
            stdout=(FIXTURES / "owners.json").read_bytes(),
            stderr=b"",
        )
        SSHAdapter("fixture-host").validate_owner_directory(
            {"telegram": ["100000001"], "slack": ["D0EXAMPLE01"]},
            ("telegram", "slack"),
        )

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_validate_owner_directory_rejects_zero_dm_entries_for_sourced_platform(
        self, run_mock: mock.Mock
    ) -> None:
        payload = {
            "platforms": {
                "telegram": [{"id": "100000001", "type": "dm"}],
                "slack": [],
            }
        }
        run_mock.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stderr=b"",
        )
        with self.assertRaisesRegex(AdapterError, "owner directory mismatch"):
            SSHAdapter("fixture-host").validate_owner_directory(
                {"telegram": ["100000001"], "slack": ["D0EXAMPLE01"]},
                ("telegram", "slack"),
            )

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_fetch_turns_uses_exact_argv(self, run_mock: mock.Mock) -> None:
        start_epoch, end_epoch = jst_epoch_window(RUN_DATE)
        payload = (
            json.dumps(
                {
                    "session_id": "ssh-session-1",
                    "source": "telegram",
                    "chat_type": "dm",
                    "user_id": "100000001",
                    "session_key": "ssh-key-1",
                    "content": "over ssh",
                    "timestamp": start_epoch + 1,
                    "role": "user",
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        run_mock.return_value = mock.Mock(returncode=0, stdout=payload, stderr=b"")
        turns = SSHAdapter("fixture-host").fetch_turns(start_epoch, end_epoch, ("telegram",))
        self.assertEqual([(turn.session_id, turn.content) for turn in turns], [("ssh-session-1", "over ssh")])
        run_mock.assert_called_once_with(
            ["ssh", "fixture-host", "read-sessions", str(start_epoch), str(end_epoch)],
            stdout=mock.ANY,
            stderr=mock.ANY,
            text=False,
            check=False,
        )
        self.assertIs(run_mock.call_args.kwargs["stdout"], -1)
        self.assertIs(run_mock.call_args.kwargs["stderr"], -1)

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_rejects_row_with_missing_role(self, run_mock: mock.Mock) -> None:
        start_epoch, end_epoch = jst_epoch_window(RUN_DATE)
        payload = {
            "session_id": "documented-user-row",
            "source": "telegram",
            "chat_type": "dm",
            "user_id": "100000001",
            "session_key": "documented-user-key",
            "content": "documented user payload",
            "timestamp": start_epoch + 1,
        }
        run_mock.return_value = mock.Mock(
            returncode=0,
            stdout=(json.dumps(payload) + "\n").encode("utf-8"),
            stderr=b"",
        )
        with self.assertRaisesRegex(AdapterError, "missing keys: role"):
            SSHAdapter("fixture-host").fetch_turns(
                start_epoch, end_epoch, ("telegram",)
            )

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_rejects_extra_owner_directory_entry(self, run_mock: mock.Mock) -> None:
        payload = {
            "platforms": {
                "telegram": [{"id": "100000001", "type": "dm"}],
                "slack": [
                    {"id": "D0EXAMPLE01:1700000000.000002", "type": "dm"},
                    {"id": "D0ZZZZZZZZZ:1700000000.000100", "type": "dm"},
                ],
            }
        }
        run_mock.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stderr=b"",
        )
        with self.assertRaisesRegex(AdapterError, "owner directory mismatch"):
            SSHAdapter("fixture-host").validate_owner_directory(
                {"telegram": ["100000001"], "slack": ["D0EXAMPLE01"]},
                ("telegram", "slack"),
            )

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_rejects_malformed_session_output(self, run_mock: mock.Mock) -> None:
        run_mock.return_value = mock.Mock(returncode=0, stdout=b"{not-json}\n", stderr=b"")
        start_epoch, end_epoch = jst_epoch_window(RUN_DATE)
        with self.assertRaisesRegex(AdapterError, "invalid read-sessions JSONL at line 1"):
            SSHAdapter("fixture-host").fetch_turns(start_epoch, end_epoch, ("telegram",))

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_accepts_real_dispatcher_array_shape_owner_directory(
        self, run_mock: mock.Mock
    ) -> None:
        # Real `read-owners` output observed on the production VPS dispatcher:
        # a single-line JSON array of {"platform","id","type"} objects,
        # dm-only, sorted by (platform, id) -- not the {"platforms": {...}}
        # object shape. Slack ids carry a thread suffix; telegram ids do not.
        payload = (
            b'[{"platform":"slack","id":"D0EXAMPLE01:1700000000.000001","type":"dm"},'
            b'{"platform":"telegram","id":"100000001","type":"dm"}]\n'
        )
        run_mock.return_value = mock.Mock(returncode=0, stdout=payload, stderr=b"")
        SSHAdapter("fixture-host").validate_owner_directory(
            {"telegram": ["100000001"], "slack": ["D0EXAMPLE01"]},
            ("telegram", "slack"),
        )

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_rejects_array_entry_with_unknown_key(self, run_mock: mock.Mock) -> None:
        payload = json.dumps(
            [{"platform": "telegram", "id": "100000001", "type": "dm", "name": "leak"}]
        ).encode("utf-8")
        run_mock.return_value = mock.Mock(returncode=0, stdout=payload, stderr=b"")
        with self.assertRaisesRegex(AdapterError, "invalid read-owners array entry"):
            SSHAdapter("fixture-host").load_owner_directory()

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_rejects_array_entry_with_unsupported_platform(
        self, run_mock: mock.Mock
    ) -> None:
        payload = json.dumps(
            [{"platform": "discord", "id": "123", "type": "dm"}]
        ).encode("utf-8")
        run_mock.return_value = mock.Mock(returncode=0, stdout=payload, stderr=b"")
        with self.assertRaisesRegex(AdapterError, "unsupported owner platform"):
            SSHAdapter("fixture-host").load_owner_directory()

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_rejects_array_entry_with_non_dm_type(self, run_mock: mock.Mock) -> None:
        payload = json.dumps(
            [{"platform": "telegram", "id": "100000001", "type": "group"}]
        ).encode("utf-8")
        run_mock.return_value = mock.Mock(returncode=0, stdout=payload, stderr=b"")
        with self.assertRaisesRegex(AdapterError, "must have type 'dm'"):
            SSHAdapter("fixture-host").load_owner_directory()

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_rejects_array_with_duplicate_platform_id_pair(
        self, run_mock: mock.Mock
    ) -> None:
        payload = json.dumps(
            [
                {"platform": "telegram", "id": "100000001", "type": "dm"},
                {"platform": "telegram", "id": "100000001", "type": "dm"},
            ]
        ).encode("utf-8")
        run_mock.return_value = mock.Mock(returncode=0, stdout=payload, stderr=b"")
        with self.assertRaisesRegex(AdapterError, "duplicate owner directory entry"):
            SSHAdapter("fixture-host").load_owner_directory()

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_ssh_adapter_rejects_empty_array(self, run_mock: mock.Mock) -> None:
        run_mock.return_value = mock.Mock(returncode=0, stdout=b"[]\n", stderr=b"")
        with self.assertRaisesRegex(AdapterError, "read-owners array returned no DM entries"):
            SSHAdapter("fixture-host").load_owner_directory()


class LucaRealSchemaShapeTests(LucaCollectorTestCase):
    """Fixes the adapter's required/optional field split to the real
    Hermes production state.db nullability, measured 2026-08-07 via
    `PRAGMA table_info(sessions)` / `PRAGMA table_info(messages)` against
    the live VPS profile (issue #30, 3rd real-VPS shape mismatch):

        messages.session_id   NOT NULL (notnull=1)
        messages.role         NOT NULL (notnull=1)
        messages.timestamp    NOT NULL (notnull=1)
        messages.content      nullable (notnull=0) -- and the same measurement
                               found 1,962 real assistant rows with
                               LENGTH(content)=0 (tool-call-only turns)
        sessions.source       NOT NULL (notnull=1)
        sessions.user_id      nullable (notnull=0)
        sessions.chat_type    nullable (notnull=0)
        sessions.session_key  nullable (notnull=0)

    These tests assert that split as adapter *behavior* (insert/query
    outcomes), not by reading adapters.py's internal constants, so a
    future change that quietly narrows/widens the accepted shape again
    breaks a test instead of shipping straight to the nightly VPS run.
    """

    def test_fixture_schema_matches_measured_real_db_nullability(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            messages_notnull = {
                row[1]: row[3] for row in connection.execute("PRAGMA table_info(messages)")
            }
            sessions_notnull = {
                row[1]: row[3] for row in connection.execute("PRAGMA table_info(sessions)")
            }
        self.assertEqual(
            {name: messages_notnull[name] for name in ("session_id", "role", "timestamp")},
            {"session_id": 1, "role": 1, "timestamp": 1},
        )
        self.assertEqual(messages_notnull["content"], 0)
        self.assertEqual(sessions_notnull["source"], 1)
        self.assertEqual(
            {name: sessions_notnull[name] for name in ("user_id", "chat_type", "session_key")},
            {"user_id": 0, "chat_type": 0, "session_key": 0},
        )

    def test_messages_required_columns_reject_null(self) -> None:
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_session(
            "required-cols-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="required-cols-key",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO messages (id, session_id, content, timestamp, role) VALUES (?, ?, ?, ?, ?)",
                    (9001, None, "x", start_epoch + 1, "user"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO messages (id, session_id, content, timestamp, role) VALUES (?, ?, ?, ?, ?)",
                    (9002, "required-cols-session", "x", start_epoch + 1, None),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO messages (id, session_id, content, timestamp, role) VALUES (?, ?, ?, ?, ?)",
                    (9003, "required-cols-session", "x", None, "user"),
                )

    def test_sessions_source_rejects_null(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO sessions (id, source, chat_type, user_id, session_key) VALUES (?, ?, ?, ?, ?)",
                    ("null-source-session", None, "dm", "100000001", "null-source-key"),
                )

    def test_fetch_turns_parses_null_content_user_id_chat_type_session_key(self) -> None:
        start_epoch, end_epoch = jst_epoch_window(RUN_DATE)
        self._add_session(
            "null-optional-session",
            source="api_server",
            chat_type=None,
            user_id=None,
            session_key=None,
        )
        self._add_message(
            "null-optional-session", content=None, timestamp=start_epoch + 1, role="assistant"
        )
        rows = LocalSQLiteAdapter(self.db_path).fetch_turns(start_epoch, end_epoch, ("api_server",))
        self.assertEqual(len(rows), 1)
        turn = rows[0]
        self.assertIsNone(turn.content)
        self.assertIsNone(turn.user_id)
        self.assertIsNone(turn.chat_type)
        self.assertIsNone(turn.session_key)

    def test_fetch_turns_parses_empty_string_content(self) -> None:
        start_epoch, end_epoch = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "empty-content-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="empty-content-key",
            turns=[("assistant", "", start_epoch + 1)],
        )
        rows = LocalSQLiteAdapter(self.db_path).fetch_turns(start_epoch, end_epoch, ("telegram",))
        self.assertEqual([row.content for row in rows], [""])


class LucaCollectorIntegrationTests(LucaCollectorTestCase):
    def test_collect_rejects_local_owner_fixture_mismatch(self) -> None:
        # Unexpected slack DM entry (does not match expected_dm_entries.slack
        # exactly or by thread-suffix prefix) must stop the run.
        wrong_owners = self.root / "owners-extra.json"
        wrong_owners.write_text(
            json.dumps(
                {
                    "platforms": {
                        "telegram": [{"id": "100000001", "type": "dm"}],
                        "slack": [
                            {"id": "D0EXAMPLE01:1700000000.000002", "type": "dm"},
                            {"id": "D0ZZZZZZZZZ:1700000000.000100", "type": "dm"},
                        ],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "owner directory mismatch"):
            collect(
                RUN_DATE,
                config_path=self.config_path,
                sqlite_path=self.db_path,
                owners_json=wrong_owners,
                obs_root=self.obs_root,
            )

    def test_collect_rejects_unexpected_telegram_owner_entry(self) -> None:
        # Unexpected telegram DM entry alongside the expected uid must stop
        # the run (telegram allows exact match only, no prefix match).
        wrong_owners = self.root / "owners-telegram-extra.json"
        wrong_owners.write_text(
            json.dumps(
                {
                    "platforms": {
                        "telegram": [
                            {"id": "100000001", "type": "dm"},
                            {"id": "9999999999", "type": "dm"},
                        ],
                        "slack": [
                            {"id": "D0EXAMPLE01:1700000000.000002", "type": "dm"},
                        ],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "owner directory mismatch"):
            collect(
                RUN_DATE,
                config_path=self.config_path,
                sqlite_path=self.db_path,
                owners_json=wrong_owners,
                obs_root=self.obs_root,
            )

    def test_collect_rejects_telegram_prefix_match(self) -> None:
        # Unlike slack, telegram DM entry ids must match expected_dm_entries
        # exactly; a suffixed id (thread-suffix style) must be rejected.
        wrong_owners = self.root / "owners-telegram-prefix.json"
        wrong_owners.write_text(
            json.dumps(
                {
                    "platforms": {
                        "telegram": [{"id": "100000001x", "type": "dm"}],
                        "slack": [
                            {"id": "D0EXAMPLE01:1700000000.000002", "type": "dm"},
                        ],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "owner directory mismatch"):
            collect(
                RUN_DATE,
                config_path=self.config_path,
                sqlite_path=self.db_path,
                owners_json=wrong_owners,
                obs_root=self.obs_root,
            )

    def test_collect_rejects_zero_dm_entries_for_sourced_platform(self) -> None:
        # config.source.sources includes slack, so a directory with no slack
        # DM entries at all (route disappearance) must stop the run.
        wrong_owners = self.root / "owners-slack-missing.json"
        wrong_owners.write_text(
            json.dumps([["telegram", "100000001", "dm"]], ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "owner directory mismatch"):
            collect(
                RUN_DATE,
                config_path=self.config_path,
                sqlite_path=self.db_path,
                owners_json=wrong_owners,
                obs_root=self.obs_root,
            )

    def test_collect_accepts_real_shaped_owner_directory(self) -> None:
        # Real Hermes channel_directory.json DM entries for slack are
        # thread-scoped (`D0EXAMPLE01:<thread_ts>`), not the member id, and
        # there can be many of them. The self-check must pass against this
        # real shape via expected_dm_entries thread-suffix prefix matching.
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "tg-owner-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="owner-key-1",
            turns=[("user", "Telegram owner", start_epoch + 10)],
        )
        stats = self._collect()
        self.assertEqual(stats["records"], 1)

    def test_collect_skips_empty_content_assistant_rows_without_raising(self) -> None:
        # Real state.db has 1,962 real assistant rows with LENGTH(content)=0
        # (tool-call-only turns; measured 2026-08-07 against the live VPS
        # profile: api_server 1006 / cli 569 / slack 182 / telegram 205 /
        # webui 1). Before this fix, `_require_nonempty_string(content,
        # "content")` killed the whole nightly window with "[RED] luca:
        # collector: content must be a non-empty string" the instant one
        # such row appeared in the stream -- exit 1, zero records written,
        # even with owner Telegram/Slack turns present in the same window.
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "tg-owner-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="owner-key-1",
            turns=[("user", "Telegram owner", start_epoch + 10)],
        )
        self._add_turns(
            "slack-owner-session",
            source="slack",
            chat_type="dm",
            user_id="U0EXAMPLE01",
            session_key="owner-key-2",
            turns=[("user", "Slack owner", start_epoch + 20)],
        )
        self._add_turns(
            "tool-call-only-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="tool-call-key",
            turns=[("assistant", "", start_epoch + 30)],
        )
        stats = self._collect()
        records = self._load_jsonl(self._records_path())
        self.assertEqual([record["text"] for record in records], ["Telegram owner", "Slack owner"])
        self.assertEqual(stats["records"], 2)
        self.assertEqual(stats["usage_records"], 0)
        self.assertFalse(self._usage_path().exists())
        record_bytes = self._records_path().read_bytes()
        self.assertNotIn(b'"text":""', record_bytes)

    def test_collect_drops_voice_sessions_when_voice_disabled(self) -> None:
        payload = self._base_config()
        payload["source"]["voice_enabled"] = False  # type: ignore[index]
        self._write_config(payload)
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "voice-session-1",
            source="api_server",
            chat_type=None,
            user_id=None,
            session_key=None,
            turns=[("user", "音声だけ", start_epoch + 5)],
        )
        stats = self._collect()
        self.assertEqual(stats["records"], 0)
        self.assertEqual(self._records_path().read_text(encoding="utf-8"), "")

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_collect_survives_null_api_server_rows_from_ssh_dispatcher_stream(
        self, run_mock: mock.Mock
    ) -> None:
        # Real Hermes state.db api_server rows stream with chat_type,
        # session_key, and user_id all JSON null (measured 2026-08-07 against
        # the live VPS dispatcher). read-sessions streams every configured
        # source together server-side, so a null api_server row must not
        # raise AdapterError and kill the whole night even when voice is
        # disabled and Telegram/Slack rows are present in the same stream.
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        session_lines = [
            json.dumps(
                {
                    "session_id": "09d12e77-c1fa-43a3-b8c2-62de6b724810",
                    "source": "api_server",
                    "chat_type": None,
                    "user_id": None,
                    "session_key": None,
                    "content": "deployment warm-up",
                    "timestamp": start_epoch + 5,
                    "role": "user",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "session_id": "tg-ssh-owner-session",
                    "source": "telegram",
                    "chat_type": "dm",
                    "user_id": "100000001",
                    "session_key": "tg-ssh-owner-key",
                    "content": "ssh telegram owner",
                    "timestamp": start_epoch + 10,
                    "role": "user",
                },
                ensure_ascii=False,
            ),
        ]
        payload = ("\n".join(session_lines) + "\n").encode("utf-8")
        run_mock.side_effect = self._ssh_dispatcher_side_effect(payload)

        config_payload = self._base_config()
        config_payload["source"]["voice_enabled"] = False  # type: ignore[index]
        self._write_config(config_payload)

        stats = collect(
            RUN_DATE,
            config_path=self.config_path,
            obs_root=self.obs_root,
        )
        records = self._load_jsonl(self._records_path())
        self.assertEqual([record["text"] for record in records], ["ssh telegram owner"])
        self.assertEqual(stats["records"], 1)
        self.assertEqual(stats["outsider_sessions"], 0)
        record_bytes = self._records_path().read_bytes()
        self.assertNotIn(b"deployment warm-up", record_bytes)

    @mock.patch("collectors.hermes_luca.adapters.subprocess.run")
    def test_collect_accepts_null_shape_voice_session_when_enabled(
        self, run_mock: mock.Mock
    ) -> None:
        # voice_enabled: true must accept the real null-shaped api_server
        # row (chat_type/session_key/user_id all null), not only the
        # fictional chat_type="voice" shape the old fixtures used.
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        session_lines = [
            json.dumps(
                {
                    "session_id": "09d12e77-c1fa-43a3-b8c2-62de6b724810",
                    "source": "api_server",
                    "chat_type": None,
                    "user_id": None,
                    "session_key": None,
                    "content": "voice enabled null shape",
                    "timestamp": start_epoch + 5,
                    "role": "user",
                },
                ensure_ascii=False,
            ),
        ]
        payload = ("\n".join(session_lines) + "\n").encode("utf-8")
        run_mock.side_effect = self._ssh_dispatcher_side_effect(payload)

        config_payload = self._base_config()
        config_payload["source"]["voice_enabled"] = True  # type: ignore[index]
        self._write_config(config_payload)
        self._write_valid_voice_safety_files()

        stats = collect(
            RUN_DATE,
            config_path=self.config_path,
            obs_root=self.obs_root,
        )
        records = self._load_jsonl(self._records_path())
        self.assertEqual([record["text"] for record in records], ["voice enabled null shape"])
        self.assertEqual(stats["records"], 1)

    def test_collect_excludes_prefixed_session_id(self) -> None:
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "pgl-verify-session-1",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="safe-key-1",
            turns=[("user", "must be excluded", start_epoch + 5)],
        )
        stats = self._collect()
        self.assertEqual(stats["records"], 0)

    def test_collect_excludes_prefixed_session_key(self) -> None:
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "safe-session-1",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="pgl-verify-key-1",
            turns=[("user", "must be excluded", start_epoch + 5)],
        )
        stats = self._collect()
        self.assertEqual(stats["records"], 0)

    def test_session_attribution_rejects_mixed_chat_type(self) -> None:
        turns = [
            Turn("mix-1", "telegram", "dm", "100000001", "mix-key", "first", 1.0, "user"),
            Turn("mix-1", "telegram", "group", "100000001", "mix-key", "second", 2.0, "user"),
        ]
        allowed, outsider, reason = rules.session_attribution(
            turns,
            {"telegram": ("100000001",), "slack": ("U0EXAMPLE01",)},
            True,
        )
        self.assertEqual((allowed, outsider, reason), (False, False, "mixed_chat_type"))

    def test_session_attribution_rejects_null_chat_type_telegram(self) -> None:
        # Real Hermes state.db api_server rows are the only known null-shape
        # source; this asserts telegram/slack attribution stays fail-closed
        # even if a null chat_type ever reached this layer directly.
        turns = [
            Turn("null-ct-1", "telegram", None, "100000001", "null-ct-key", "hello", 1.0, "user"),
        ]
        allowed, outsider, reason = rules.session_attribution(
            turns,
            {"telegram": ("100000001",), "slack": ("U0EXAMPLE01",)},
            True,
        )
        self.assertEqual((allowed, outsider, reason), (False, False, "not_dm"))

    def test_session_attribution_rejects_empty_chat_type_slack(self) -> None:
        # Empty string must be treated the same as null/absent: it must not
        # satisfy the chat_type == "dm" requirement.
        turns = [
            Turn("empty-ct-1", "slack", "", "U0EXAMPLE01", "empty-ct-key", "hello", 1.0, "user"),
        ]
        allowed, outsider, reason = rules.session_attribution(
            turns,
            {"telegram": ("100000001",), "slack": ("U0EXAMPLE01",)},
            True,
        )
        self.assertEqual((allowed, outsider, reason), (False, False, "not_dm"))

    def test_session_is_excluded_handles_null_session_key(self) -> None:
        # session_key can be null for api_server rows; the pgl-verify- P2
        # prefix check must fall back to session_id alone instead of
        # crashing on None.startswith(...).
        self.assertTrue(rules.session_is_excluded("pgl-verify-session-x", None, ["pgl-verify-"]))
        self.assertFalse(rules.session_is_excluded("safe-session-x", None, ["pgl-verify-"]))

    def test_collect_keeps_only_owner_dm_and_voice_sessions(self) -> None:
        payload = self._base_config()
        payload["source"]["voice_enabled"] = True  # type: ignore[index]
        self._write_config(payload)
        self._write_valid_voice_safety_files()
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "tg-owner-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="owner-key-1",
            turns=[("user", "Telegram owner", start_epoch + 10)],
        )
        self._add_turns(
            "slack-owner-session",
            source="slack",
            chat_type="dm",
            user_id="U0EXAMPLE01",
            session_key="owner-key-2",
            turns=[("user", "Slack owner", start_epoch + 20)],
        )
        self._add_turns(
            "voice-owner-session",
            source="api_server",
            chat_type=None,
            user_id=None,
            session_key=None,
            turns=[("user", "Voice owner", start_epoch + 30)],
        )
        self._add_turns(
            "missing-uid-session",
            source="telegram",
            chat_type="dm",
            user_id=None,
            session_key="missing-key",
            turns=[("user", "missing uid", start_epoch + 40)],
        )
        self._add_turns(
            "outsider-session",
            source="slack",
            chat_type="dm",
            user_id="outsider",
            session_key="outsider-key",
            turns=[("user", "outsider", start_epoch + 50)],
        )
        self._add_turns(
            "group-session",
            source="telegram",
            chat_type="group",
            user_id="100000001",
            session_key="group-key",
            turns=[("user", "group text", start_epoch + 60)],
        )
        stats = self._collect()
        records = self._load_jsonl(self._records_path())
        self.assertEqual([record["text"] for record in records], ["Telegram owner", "Slack owner", "Voice owner"])
        self.assertEqual(stats["outsider_sessions"], 1)

    def test_collect_writes_private_sanitized_records_and_usage_metadata(self) -> None:
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._write_valid_voice_safety_files()
        exact_240 = "あ" * 240
        exact_241 = "あ" * 241
        assistant_text = "定型の返答です。RAW_ASSISTANT_MUST_NOT_PERSIST"

        self._add_turns(
            "tg-clean-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="tg-clean-key",
            turns=[
                ("user", "plain owner text", start_epoch + 1),
                ("assistant", assistant_text, start_epoch + 2),
            ],
        )
        self._add_turns(
            "slack-mask-session",
            source="slack",
            chat_type="dm",
            user_id="U0EXAMPLE01",
            session_key="slack-mask-key",
            turns=[("user", "mail me at owner@example.invalid or call 090-1234-5678", start_epoch + 3)],
        )
        self._add_turns(
            "inject-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="inject-key",
            turns=[("user", "prefix<memory-context>RAW_MARKER</memory-context> clean residue", start_epoch + 4)],
        )
        self._add_turns(
            "partial-inject-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="partial-inject-key",
            turns=[("user", "broken <memory-context residue", start_epoch + 5)],
        )
        self._add_turns(
            "tg-group-prefix-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="tg-group-prefix-key",
            turns=[("user", "[Group|123]\nshould be rejected", start_epoch + 6)],
        )
        self._add_turns(
            "len-240-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="len-240-key",
            turns=[("user", exact_240, start_epoch + 7)],
        )
        self._add_turns(
            "len-241-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="len-241-key",
            turns=[("user", exact_241, start_epoch + 8)],
        )
        self._add_turns(
            "secret-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="secret-key",
            turns=[("user", "AKIADUMMY", start_epoch + 9)],
        )
        self._add_turns(
            "base64-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="base64-key",
            turns=[("user", "Q" * 40, start_epoch + 10)],
        )
        self._add_turns(
            "cli-session",
            source="cli",
            chat_type="dm",
            user_id="100000001",
            session_key="cli-key",
            turns=[("user", "cli must stay invisible", start_epoch + 11)],
        )
        self._add_turns(
            "webui-session",
            source="webui",
            chat_type="dm",
            user_id="100000001",
            session_key="webui-key",
            turns=[("user", "webui must stay invisible", start_epoch + 12)],
        )

        stats = self._collect()
        records = self._load_jsonl(self._records_path())
        usage_records = self._load_jsonl(self._usage_path())
        marker = json.loads(self._marker_path().read_text(encoding="utf-8"))
        assistant_ts = "2026-08-02T00:00:02+09:00"

        expected_texts = [
            "plain owner text",
            "mail me at ***@*** or call 0**-****-****",
            "prefix clean residue",
            exact_240,
        ]
        self.assertEqual([record["text"] for record in records], expected_texts)
        self.assertEqual(
            stats,
            {
                "records": 4,
                "usage_records": 1,
                "outsider_sessions": 0,
                "pruned": 0,
                "voice_dropped_reason": None,
                "voice_rows_window_excluded": 0,
                "voice_sessions_ledger_excluded": 0,
                "persona_marker_count": 0,
                "warmup_marker_count": 0,
                "prefix_warn_sessions": 0,
                "digest_lines": ("[INFO] luca: collector: surviving /persona markers=0",),
            },
        )
        self.assertEqual(
            set(marker),
            {
                "schema_version",
                "face",
                "run_at",
                "date",
                "sources_scanned",
                "records_written",
                "usage_enabled",
                "errors",
                "ucd",
            },
        )
        self.assertEqual(marker["usage_enabled"], True)
        self.assertEqual(marker["records_written"], 4)
        self.assertEqual(marker["errors"], 0)
        self.assertEqual(marker["face"], "luca")
        self.assertEqual(marker["schema_version"], 1)
        self.assertEqual(marker["date"], RUN_DATE)
        self.assertEqual(marker["ucd"], runtime_status().runtime_version)
        # cli/webui sessions are excluded by the config's source allowlist
        # before they ever reach session grouping, so sources_scanned only
        # counts the 9 telegram/slack sessions the dispatcher actually
        # returned for this bucket.
        self.assertEqual(marker["sources_scanned"], 9)
        self.assertTrue(marker["run_at"].endswith("+09:00"))
        self.assertTrue(all(record["ts"].endswith("+09:00") for record in records))
        self.assertTrue(all(record["host"] == "vps-hermes" for record in records))
        self.assertTrue(all(record["face"] == "luca" for record in records))
        self.assertTrue(all(record["project"] == "hermes-luca" for record in records))
        self.assertTrue(all(record["speaker"] == "owner" for record in records))
        self.assertTrue(all(record["ucd"] == runtime_status().runtime_version for record in records))
        expected_hashes = {
            "tg-clean-session": hashlib.sha256("tg-clean-session".encode("utf-8")).hexdigest()[:12],
            "slack-mask-session": hashlib.sha256("slack-mask-session".encode("utf-8")).hexdigest()[:12],
            "inject-session": hashlib.sha256("inject-session".encode("utf-8")).hexdigest()[:12],
            "len-240-session": hashlib.sha256("len-240-session".encode("utf-8")).hexdigest()[:12],
        }
        self.assertEqual({record["session"] for record in records}, set(expected_hashes.values()))
        self.assertEqual(
            usage_records,
            [
                {
                    "ts": assistant_ts,
                    "session": expected_hashes["tg-clean-session"],
                    "face": "luca",
                    "phrase_id": "p-0001",
                    "state": "staged",
                    "ucd": runtime_status().runtime_version,
                }
            ],
        )
        usage_bytes = self._usage_path().read_bytes()
        self.assertNotIn("定型の返答です".encode("utf-8"), usage_bytes)
        self.assertNotIn(b"RAW_ASSISTANT_MUST_NOT_PERSIST", usage_bytes)

    def test_collect_creates_only_final_durable_files_and_is_deterministic(self) -> None:
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "deterministic-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="deterministic-key",
            turns=[
                ("user", "visible text", start_epoch + 1),
                ("assistant", "定型の返答です。RAW_ASSISTANT_MUST_NOT_PERSIST", start_epoch + 2),
                ("user", "prefix<memory-context>RAW_MARKER</memory-context> residue", start_epoch + 3),
            ],
        )

        first_obs = self.root / "obs-first"
        second_obs = self.root / "obs-second"
        self._seed_ledger(first_obs)
        self._seed_ledger(second_obs)
        self._write_valid_voice_safety_files(first_obs)
        self._write_valid_voice_safety_files(second_obs)
        before = self._snapshot_files()
        first_stats = self._collect(obs_root=first_obs)
        after_first = self._snapshot_files()
        second_stats = self._collect(obs_root=second_obs)

        created_after_first = sorted(set(after_first) - set(before))
        self.assertEqual(
            created_after_first,
            sorted(
                [
                    "obs-first/obslog/luca/2026-08-02.jsonl",
                    "obs-first/obslog/luca/usage-2026-08-02.jsonl",
                    "obs-first/state/collector/luca.last-run.json",
                ]
            ),
        )
        self.assertEqual(first_stats, second_stats)

        durable_first = {
            "records": self._records_path(first_obs).read_bytes(),
            "usage": self._usage_path(first_obs).read_bytes(),
        }
        durable_second = {
            "records": self._records_path(second_obs).read_bytes(),
            "usage": self._usage_path(second_obs).read_bytes(),
        }
        self.assertEqual(durable_first, durable_second)
        # marker is deterministic except for the wall-clock run_at field
        # (see _marker_payload_without_run_at).
        self.assertEqual(
            self._marker_payload_without_run_at(first_obs),
            self._marker_payload_without_run_at(second_obs),
        )
        self.assertFalse(any(part in path for path in after_first for part in ("temp", "spool", "debug", "intermediate")))
        combined = b"".join(durable_first.values()) + self._marker_path(first_obs).read_bytes()
        self.assertNotIn(b"RAW_ASSISTANT_MUST_NOT_PERSIST", combined)
        self.assertNotIn(b"RAW_MARKER", combined)
        self.assertEqual(stat.S_IMODE(self._records_path(first_obs).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self._usage_path(first_obs).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self._marker_path(first_obs).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self._records_path(first_obs).parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self._marker_path(first_obs).parent.stat().st_mode), 0o700)

    def test_bin_local_sqlite_two_runs_are_byte_identical(self) -> None:
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "bin-deterministic-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="bin-deterministic-key",
            turns=[
                ("user", "bin visible text", start_epoch + 1),
                ("assistant", "定型の返答です。bin raw context", start_epoch + 2),
            ],
        )
        roots = (self.root / "bin-first", self.root / "bin-second")
        command = REPO_ROOT / "bin" / "pgl-obs-collector-luca"
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        outputs: list[dict[str, bytes]] = []
        for root in roots:
            self._seed_ledger(root)
            result = subprocess.run(
                [
                    str(command),
                    "--date",
                    RUN_DATE,
                    "--config",
                    str(self.config_path),
                    "--sqlite",
                    str(self.db_path),
                    "--owners-json",
                    str(self.owners_path),
                    "--obs-root",
                    str(root),
                ],
                cwd=REPO_ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=result.stderr.decode("utf-8", errors="replace"),
            )
            outputs.append(
                {
                    "records": self._records_path(root).read_bytes(),
                    "usage": self._usage_path(root).read_bytes(),
                }
            )
        self.assertEqual(outputs[0], outputs[1])
        # marker is deterministic except for the wall-clock run_at field
        # (see _marker_payload_without_run_at); the two CLI subprocesses ran
        # at different real timestamps.
        self.assertEqual(
            self._marker_payload_without_run_at(roots[0]),
            self._marker_payload_without_run_at(roots[1]),
        )

    def test_marker_is_accepted_directly_by_mirror_collector_liveness(self) -> None:
        # #36 core assertion: the real marker this collector writes to a real
        # file is fed straight into mirror.weekly's own validator -- no mock
        # stands in for either side.
        from mirror.weekly import _collector_liveness

        jst = timezone(timedelta(hours=9))
        today = datetime.now(jst).date()
        start_epoch, _ = jst_epoch_window(today.isoformat())
        self._add_turns(
            "liveness-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="liveness-key",
            turns=[("user", "liveness owner text", start_epoch + 1)],
        )

        collect(
            today.isoformat(),
            config_path=self.config_path,
            sqlite_path=self.db_path,
            owners_json=self.owners_path,
            obs_root=self.obs_root,
        )

        healthy, detail, usage_enabled = _collector_liveness(self.obs_root, "luca", today)

        self.assertTrue(healthy, msg=detail)
        self.assertEqual(detail, "collector last-run marker healthy")
        self.assertTrue(usage_enabled)

    def test_marker_forgery_matrix_is_rejected_by_mirror_collector_liveness(self) -> None:
        # #36 acceptance: forged/corrupt markers must be BROKEN, in both
        # directions from a real, healthy, collector-written baseline.
        from mirror.weekly import _collector_liveness

        jst = timezone(timedelta(hours=9))
        today = datetime.now(jst).date()
        start_epoch, _ = jst_epoch_window(today.isoformat())
        self._add_turns(
            "forgery-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="forgery-key",
            turns=[("user", "forgery owner text", start_epoch + 1)],
        )
        collect(
            today.isoformat(),
            config_path=self.config_path,
            sqlite_path=self.db_path,
            owners_json=self.owners_path,
            obs_root=self.obs_root,
        )
        marker_path = self._marker_path()
        healthy_payload = json.loads(marker_path.read_text(encoding="utf-8"))

        def write(payload: dict[str, object]) -> None:
            marker_path.write_text(json.dumps(payload), encoding="utf-8")

        cases = (
            (
                "face-mismatch",
                {**healthy_payload, "face": "alpha"},
                today,
                "collector last-run marker face mismatch",
            ),
            (
                "future-run-at",
                {
                    **healthy_payload,
                    "run_at": (datetime.now(jst) + timedelta(days=2))
                    .replace(microsecond=0)
                    .isoformat(),
                },
                today,
                "collector last-run marker run_at too far in the future",
            ),
            (
                "date-run-at-inconsistent",
                {**healthy_payload, "date": (today + timedelta(days=1)).isoformat()},
                today + timedelta(days=1),
                "collector last-run marker date is newer than the collector run_at JST day",
            ),
            (
                "missing-key",
                {key: value for key, value in healthy_payload.items() if key != "sources_scanned"},
                today,
                "collector last-run marker schema mismatch",
            ),
            (
                "type-mismatch",
                {**healthy_payload, "errors": "0"},
                today,
                "collector last-run marker errors invalid",
            ),
        )
        for label, payload, run_day, expected_detail in cases:
            with self.subTest(label=label):
                write(payload)
                healthy, detail, usage_enabled = _collector_liveness(self.obs_root, "luca", run_day)
                self.assertFalse(healthy)
                self.assertEqual(detail, expected_detail)
                self.assertIsNone(usage_enabled)

        # Leave a healthy marker behind so this test doesn't poison anything
        # that might inspect the marker afterward.
        write(healthy_payload)

    def test_schedule_matches_weekly_liveness_window(self) -> None:
        # #38 core assertion: the collector's default bucket is "JST
        # previous day" (see main()'s
        # `run_date = args.date or (now(JST).date() - 1)`), while
        # mirror._collector_liveness requires the marker's `date` to equal
        # the weekly run_day or the day before it. Whether that holds is a
        # function of which launchd job's clock time comes first in the
        # day: whichever job runs later determines which calendar day's
        # collector run is "most recent" by the time the weekly mirror
        # fires. This reads both schedules from the real shipped plist
        # templates (not a hardcoded assumption), derives the marker
        # date/run_at that schedule implies, and drives the real `main()`
        # entrypoint with `--date` omitted -- production's actual bucket
        # selection, not a bucket the test computed and handed to
        # collect() -- so a regression in main()'s default-bucket logic
        # itself would fail this test, then feeds the resulting marker
        # through the real mirror validator -- no hand-built JSON.
        from mirror.weekly import _collector_liveness

        templates = REPO_ROOT / "templates" / "launchd"
        with (templates / "ai.caty.pgl.obs-collector-luca.plist").open("rb") as handle:
            collector_interval = plistlib.load(handle)["StartCalendarInterval"]
        with (templates / "ai.caty.pgl.mirror-weekly-luca.plist").open("rb") as handle:
            weekly_interval = plistlib.load(handle)["StartCalendarInterval"]

        jst = timezone(timedelta(hours=9))
        # Frozen weekly contract (also pinned by
        # test_luca_launchd.LucaLaunchdTemplateTests): Tuesday 01:30.
        self.assertEqual(weekly_interval["Weekday"], 2)
        weekly_hm = (weekly_interval["Hour"], weekly_interval["Minute"])
        run_day = date(2026, 7, 28)  # a Tuesday, safely in the past

        def assert_marker_liveness(
            collector_hm: tuple[int, int], *, expect_healthy: bool, label: str
        ) -> None:
            # The collector runs every day; the most recent run completed
            # before the weekly job fires on run_day is that same
            # calendar day if the collector's time-of-day precedes the
            # weekly job's, otherwise it is the day before.
            execution_day = run_day if collector_hm < weekly_hm else run_day - timedelta(days=1)
            bucket = execution_day - timedelta(days=1)  # collector's default "previous day" bucket
            fixed_now = datetime(
                execution_day.year,
                execution_day.month,
                execution_day.day,
                collector_hm[0],
                collector_hm[1],
                tzinfo=jst,
            )

            class _FixedNow(datetime):
                @classmethod
                def now(cls, tz=None):
                    return fixed_now

            start_epoch, _ = jst_epoch_window(bucket)
            self._add_turns(
                f"schedule-{label}-session",
                source="telegram",
                chat_type="dm",
                user_id="100000001",
                session_key=f"schedule-{label}-key",
                turns=[("user", "schedule congruence owner text", start_epoch + 1)],
            )
            # `--date` is deliberately omitted: main() must pick the same
            # bucket the test computed by hand (execution_day - 1 day)
            # entirely on its own, through its real default-bucket code
            # path, not because the test told it what date to use.
            with mock.patch("collectors.hermes_luca.collector.datetime", _FixedNow):
                rc = main(
                    [
                        "--config",
                        str(self.config_path),
                        "--sqlite",
                        str(self.db_path),
                        "--owners-json",
                        str(self.owners_path),
                        "--obs-root",
                        str(self.obs_root),
                    ]
                )
            self.assertEqual(rc, 0, msg=f"main() failed for label={label}")
            healthy, detail, _ = _collector_liveness(self.obs_root, "luca", run_day)
            if expect_healthy:
                self.assertTrue(healthy, msg=detail)
            else:
                self.assertFalse(healthy, msg=detail)
                self.assertEqual(
                    detail,
                    "collector last-run marker date is not the weekly run_day or previous JST bucket",
                )

        # Current shipped schedule (00:05): must be HEALTHY.
        assert_marker_liveness(
            (collector_interval["Hour"], collector_interval["Minute"]),
            expect_healthy=True,
            label="current",
        )
        # Regression guard: the previously shipped 23:55 schedule must
        # still be BROKEN under the identical weekly window, or this test
        # would not actually exercise the #38 fix.
        assert_marker_liveness((23, 55), expect_healthy=False, label="old")

    def test_corrupt_ledger_is_a_marker_error_not_silent_healthy(self) -> None:
        # #36 fail-open regression: load_phrases() treats an unparseable
        # overlay-ledger as a soft failure -- it returns invalid_phrases=1
        # instead of raising (see
        # collectors/claude_code/usage_log.py::load_phrases) -- so a broken
        # ledger must still surface as a marker error. Before this fix,
        # collect() hardcoded errors=0, so a corrupt-ledger night wrote a
        # healthy marker and the mirror's liveness check silently missed it.
        from mirror.weekly import _collector_liveness

        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "corrupt-ledger-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="corrupt-ledger-key",
            turns=[("user", "owner text during a broken ledger night", start_epoch + 1)],
        )
        faces_dir = self.obs_root / "faces" / "luca" / "growth"
        faces_dir.mkdir(parents=True, exist_ok=True)
        (faces_dir / "overlay-ledger.yml").write_text(
            "phrases: [{id: p-0001, text: 'unterminated\n", encoding="utf-8"
        )

        stats = collect(
            RUN_DATE,
            config_path=self.config_path,
            sqlite_path=self.db_path,
            owners_json=self.owners_path,
            obs_root=self.obs_root,
        )

        # The observation log itself is unaffected -- only usage-phrase
        # collection degrades when the ledger is unreadable.
        self.assertEqual(stats["records"], 1)
        marker = json.loads(self._marker_path().read_text(encoding="utf-8"))
        self.assertGreaterEqual(marker["errors"], 1, msg=marker)
        self.assertEqual(marker["usage_enabled"], False)

        healthy, detail, usage_enabled = _collector_liveness(
            self.obs_root, "luca", date.fromisoformat(RUN_DATE)
        )
        self.assertFalse(healthy)
        self.assertEqual(detail, "collector last-run marker recorded collector errors")
        self.assertIsNone(usage_enabled)

    def test_collect_prunes_only_luca_history(self) -> None:
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        self._add_turns(
            "prune-session",
            source="telegram",
            chat_type="dm",
            user_id="100000001",
            session_key="prune-key",
            turns=[("user", "today", start_epoch + 1)],
        )
        luca_dir = self.obs_root / "obslog" / "luca"
        alpha_dir = self.obs_root / "obslog" / "alpha"
        luca_dir.mkdir(parents=True)
        alpha_dir.mkdir(parents=True)
        (luca_dir / "2026-07-01.jsonl").write_text("old\n", encoding="utf-8")
        (alpha_dir / "2026-07-01.jsonl").write_text("alpha-old\n", encoding="utf-8")
        stats = self._collect()
        self.assertEqual(stats["pruned"], 1)
        self.assertFalse((luca_dir / "2026-07-01.jsonl").exists())
        self.assertTrue((alpha_dir / "2026-07-01.jsonl").exists())

    def test_main_writes_single_digest_line_on_lock_contention(self) -> None:
        lock = acquire_lock(self.obs_root, "luca")
        assert lock is not None
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = main(
                    [
                        "--date",
                        RUN_DATE,
                        "--config",
                        str(self.config_path),
                        "--sqlite",
                        str(self.db_path),
                        "--owners-json",
                        str(self.owners_path),
                        "--obs-root",
                        str(self.obs_root),
                    ]
                )
        finally:
            shutil.rmtree(lock)
        digest_path = self.obs_root / "digest" / f"{RUN_DATE}.md"
        digest_lines = digest_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(rc, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue().strip(), "[RED] luca: collector: skipped: lock contention")
        self.assertEqual(digest_lines, ["- [RED] luca: collector: skipped: lock contention"])

    def test_main_writes_digest_and_returns_nonzero_on_lock_error(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "collectors.hermes_luca.collector.acquire_lock",
            side_effect=PermissionError(13, "Permission denied"),
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = main(
                [
                    "--date",
                    RUN_DATE,
                    "--config",
                    str(self.config_path),
                    "--sqlite",
                    str(self.db_path),
                    "--owners-json",
                    str(self.owners_path),
                    "--obs-root",
                    str(self.obs_root),
                ]
            )
        digest_lines = (
            self.obs_root / "digest" / f"{RUN_DATE}.md"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(rc, 1)
        self.assertEqual(
            stdout.getvalue().strip(),
            "[RED] luca: collector: skipped: lock acquisition failed "
            "error=Permission denied",
        )
        self.assertIn("lock acquisition failed", stderr.getvalue())
        self.assertEqual(
            digest_lines,
            [
                "- [RED] luca: collector: skipped: lock acquisition failed "
                "error=Permission denied"
            ],
        )
