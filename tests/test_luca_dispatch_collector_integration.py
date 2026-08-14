"""Cross-component regression test for #30's real-VPS contract mismatch.

tests/test_luca_dispatch.py exercises `vps/pgl-luca-dispatch` in isolation
(asserting its exact stdout shape). tests/test_luca_collector.py exercises
`collectors.hermes_luca.adapters.SSHAdapter` in isolation (mocking
`subprocess.run` with hand-written fixture bytes). Both suites were green
while the real dispatcher's `read-owners` shape -- a single-line JSON array
of `{"platform","id","type"}` objects -- broke the collector's parser in
production: `[RED] luca: collector: read-owners JSON must be {'platforms':
{...}}`, exit 1, no nightly collection.

The tests below close that gap: they start the real dispatcher script as a
subprocess (the same forced-command entrypoint SSH would invoke) and feed
its real, unmodified stdout straight into the collector's `SSHAdapter`
parsing/self-check path, for both `read-owners` and `read-sessions`. Only
the SSH transport (`ssh <host> <argv...>`) is replaced with a direct
subprocess launch of the dispatcher through its documented test-root seam
(see `vps/pgl-luca-dispatch`'s module docstring and
`tests/test_luca_dispatch.py`); the dispatcher and collector code paths
themselves are both real.

`LucaDispatchCollectorIntegrationTests` above stops at the adapter's parser
boundary (`SSHAdapter.validate_owner_directory` / `.fetch_turns`). Three
review-flagged gaps remain past that boundary, closed by the classes below:

  * `LucaCollectFullPipelineIntegrationTests` drives `collect()`'s *entire*
    pipeline (self-check -> attribution -> filter/scrub -> obslog/usage
    write -> marker) through the real dispatcher subprocess, with
    production-shaped rows mixed in (owner DMs, an empty-content assistant
    turn, a null-shaped api_server row, a P2 `pgl-verify-` session, and a
    non-allowlisted uid) so only the two owner rows survive to obslog.
  * `LucaCollectNullContentIntegrationTests` closes the SQL-null-content
    case specifically by comparing otherwise identical databases with and
    without a NULL row through the real dispatcher and full collector.
  * `LucaDispatchOutputShapeGoldenTests` pins the *structural* contract of
    the real dispatcher's `read-owners`/`read-sessions` stdout -- key sets
    and value types, including which fields tolerate JSON null -- using
    synthetic placeholder ids/content only (no real conversation text or
    production ids are embedded as golden data).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from collectors.hermes_luca.adapters import SSHAdapter
from collectors.hermes_luca.collector import collect, jst_epoch_window


REPO = Path(__file__).resolve().parents[1]
DISPATCH = REPO / "vps" / "pgl-luca-dispatch"
FIXTURES = REPO / "tests" / "fixtures" / "hermes_luca"
RUN_DATE = "2026-08-07"

# `collectors.hermes_luca.adapters` does `import subprocess` -- the same
# module object this test file imports, not a private copy. Patching
# "collectors.hermes_luca.adapters.subprocess.run" therefore patches
# `subprocess.run` globally. Capture the real function up front so the
# side_effect below can launch the dispatcher without recursing into itself.
_REAL_SUBPROCESS_RUN = subprocess.run


def _run_real_dispatcher(root: Path, cmd, **_kwargs):
    """Swap the ssh transport for a real subprocess launch of the dispatcher
    script itself, routed through the `PGL_LUCA_DISPATCH_TESTING` test-root
    seam (see `vps/pgl-luca-dispatch`'s module docstring), so the actual
    dispatcher code -- not a stand-in -- produces the stdout bytes under
    test. `cmd` is exactly what `SSHAdapter._run_command` built:
    `["ssh", host, *dispatcher_argv]`.
    """
    dispatcher_argv = cmd[2:]
    original_command = " ".join(shlex.quote(part) for part in dispatcher_argv)
    env = os.environ.copy()
    env.pop("SSH_ORIGINAL_COMMAND", None)
    env.update(
        {
            "PGL_LUCA_DISPATCH_TESTING": "1",
            "PGL_LUCA_DISPATCH_TEST_ROOT": str(root),
            "SSH_ORIGINAL_COMMAND": original_command,
        }
    )
    return _REAL_SUBPROCESS_RUN(
        [str(DISPATCH), "read"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        check=False,
    )


class LucaDispatchCollectorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".pgl-luca-dispatch-test-root").write_bytes(
            b"pgl-luca-dispatch test root\n"
        )
        self.profile = self.root / "home/admin/.hermes/profiles/luca"
        self.profile.mkdir(parents=True)
        self._write_owners()
        self._write_database()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_owners(self) -> None:
        # Real channel_directory.json shape: a slack DM channel with
        # multiple thread-scoped entries, one telegram DM, and non-dm
        # entries (group/private) the dispatcher must exclude from
        # `read-owners` output.
        value = {
            "updated_at": "2026-08-07T12:00:00+00:00",
            "platforms": {
                "telegram": [
                    {"id": "100000001", "name": "owner", "type": "dm"},
                    {"id": "-100999", "name": "group chat", "type": "group"},
                ],
                "slack": [
                    {"id": "D0EXAMPLE01:1700000000.000001", "name": "thread a", "type": "dm"},
                    {"id": "D0EXAMPLE01:1700000000.000002", "name": "thread b", "type": "dm"},
                    {"id": "C0TEAM", "name": "team channel", "type": "private"},
                ],
            },
        }
        (self.profile / "channel_directory.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def _write_database(self) -> None:
        # The sessions/messages schema lives in one place --
        # tests/fixtures/hermes_luca/schema.sql -- so this fixture DB can
        # never quietly drift from the schema that pins the real state.db
        # nullability (see LucaRealSchemaShapeTests in
        # tests/test_luca_collector.py). Only rows are test-specific.
        database = self.profile / "state.db"
        connection = sqlite3.connect(database)
        connection.executescript((FIXTURES / "schema.sql").read_text(encoding="utf-8"))
        connection.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            [
                ("tg-session", "telegram", "dm", "100000001", "telegram:dm:100000001"),
                ("slack-session", "slack", "dm", "UOWNER", "slack:dm:D0EXAMPLE01"),
            ],
        )
        connection.executemany(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)",
            [
                ("tg-session", "user", "hello from telegram", 110.0),
                ("slack-session", "assistant", "hello from slack", 120.0),
            ],
        )
        connection.commit()
        connection.close()

    def _dispatch_via_test_root(self, cmd, **_kwargs):
        return _run_real_dispatcher(self.root, cmd, **_kwargs)

    def test_real_dispatcher_read_owners_output_passes_collector_self_check(self) -> None:
        # This is the exact regression from production: the collector's
        # self-check must accept the dispatcher's real read-owners shape
        # without raising AdapterError.
        with mock.patch(
            "collectors.hermes_luca.adapters.subprocess.run",
            side_effect=self._dispatch_via_test_root,
        ):
            SSHAdapter("unused-host").validate_owner_directory(
                {"telegram": ["100000001"], "slack": ["D0EXAMPLE01"]},
                ("telegram", "slack"),
            )

    def test_real_dispatcher_read_owners_output_parses_to_expected_pairs(self) -> None:
        with mock.patch(
            "collectors.hermes_luca.adapters.subprocess.run",
            side_effect=self._dispatch_via_test_root,
        ):
            actual = SSHAdapter("unused-host").load_owner_directory()
        self.assertEqual(
            actual,
            {
                ("telegram", "100000001"),
                ("slack", "D0EXAMPLE01:1700000000.000001"),
                ("slack", "D0EXAMPLE01:1700000000.000002"),
            },
        )

    def test_real_dispatcher_read_sessions_survives_empty_content_and_null_triple_api_server_row(
        self,
    ) -> None:
        # Real state.db shapes measured 2026-08-07 against the live VPS
        # profile: (1) assistant rows with LENGTH(content)=0 -- tool-call-only
        # turns, 1,962 of them counted the same day across api_server/cli/
        # slack/telegram/webui; (2) api_server rows with
        # chat_type/user_id/session_key all JSON null. Both shapes must
        # stream through the real (unmodified) dispatcher script and the
        # collector's SSHAdapter parser without raising, and must not
        # suppress the sibling Telegram/Slack rows in the same window.
        database = self.profile / "state.db"
        connection = sqlite3.connect(database)
        connection.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            [("api-null-session", "api_server", None, None, None)],
        )
        connection.executemany(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)",
            [
                ("tg-session", "assistant", "", 115.0),
                ("api-null-session", "assistant", "tool call only", 130.0),
            ],
        )
        connection.commit()
        connection.close()

        with mock.patch(
            "collectors.hermes_luca.adapters.subprocess.run",
            side_effect=self._dispatch_via_test_root,
        ):
            turns = SSHAdapter("unused-host").fetch_turns(100, 200, ("telegram", "slack"))
        self.assertEqual(
            [(turn.session_id, turn.source, turn.role, turn.content) for turn in turns],
            [
                ("tg-session", "telegram", "user", "hello from telegram"),
                ("tg-session", "telegram", "assistant", ""),
                ("slack-session", "slack", "assistant", "hello from slack"),
            ],
        )

    def test_real_dispatcher_read_sessions_output_parses_to_expected_turns(self) -> None:
        with mock.patch(
            "collectors.hermes_luca.adapters.subprocess.run",
            side_effect=self._dispatch_via_test_root,
        ):
            turns = SSHAdapter("unused-host").fetch_turns(100, 200, ("telegram", "slack"))
        self.assertEqual(
            [(turn.session_id, turn.source, turn.role, turn.content) for turn in turns],
            [
                ("tg-session", "telegram", "user", "hello from telegram"),
                ("slack-session", "slack", "assistant", "hello from slack"),
            ],
        )


class LucaCollectFullPipelineIntegrationTests(unittest.TestCase):
    """Closes the review-flagged gap: `collect()`'s full pipeline (self-check
    -> attribution -> filter/scrub -> obslog/usage write -> marker) was
    previously only exercised against `LocalSQLiteAdapter` fixtures or
    hand-mocked SSH bytes -- never against the real
    `vps/pgl-luca-dispatch` subprocess end-to-end. This starts the real
    dispatcher (via the same test-root seam as the class above) and drives
    `collect()` through it with six production-shaped rows mixed into one
    fixture DB: a normal telegram DM owner row, a normal slack DM owner row,
    an empty-content assistant row, a null-shaped api_server row, a
    `pgl-verify-` (P2) prefixed session, and a non-allowlisted telegram uid
    -- asserting only the two owner rows reach obslog. Run with
    `voice_enabled: false`, matching the shipped config
    (config/obs-collector-luca.json).
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".pgl-luca-dispatch-test-root").write_bytes(
            b"pgl-luca-dispatch test root\n"
        )
        self.profile = self.root / "home/admin/.hermes/profiles/luca"
        self.profile.mkdir(parents=True)
        self.obs_root = self.root / "obs-home"
        self.config_path = self.root / "obs-collector-luca.json"
        self._write_owners()
        self._write_database()
        self._write_config()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_owners(self) -> None:
        value = {
            "updated_at": "2026-08-07T12:00:00+00:00",
            "platforms": {
                "telegram": [{"id": "100000001", "name": "owner", "type": "dm"}],
                "slack": [
                    {"id": "D0EXAMPLE01:1700000000.000002", "name": "thread", "type": "dm"},
                ],
            },
        }
        (self.profile / "channel_directory.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def _write_database(self) -> None:
        start_epoch, _ = jst_epoch_window(RUN_DATE)
        database = self.profile / "state.db"
        connection = sqlite3.connect(database)
        connection.executescript((FIXTURES / "schema.sql").read_text(encoding="utf-8"))
        connection.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            [
                # 1. normal telegram DM owner row -- must be collected.
                ("tg-owner-session", "telegram", "dm", "100000001", "tg-owner-key"),
                # 2. normal slack DM owner row -- must be collected.
                ("slack-owner-session", "slack", "dm", "U0EXAMPLE01", "slack-owner-key"),
                # 3. empty-content assistant row -- excluded by content, not attribution.
                ("tg-empty-content-session", "telegram", "dm", "100000001", "tg-empty-content-key"),
                # 4. api_server row with chat_type/user_id/session_key all null.
                ("api-null-session", "api_server", None, None, None),
                # 5. pgl-verify- prefixed session -- must be P2-excluded.
                ("pgl-verify-session-1", "telegram", "dm", "100000001", "pgl-verify-key-1"),
                # 6. non-allowlisted telegram uid -- must be rejected as an outsider.
                ("tg-outsider-session", "telegram", "dm", "9999999999", "tg-outsider-key"),
            ],
        )
        connection.executemany(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)",
            [
                ("tg-owner-session", "user", "Telegram owner message", start_epoch + 10),
                ("slack-owner-session", "user", "Slack owner message", start_epoch + 20),
                ("tg-empty-content-session", "assistant", "", start_epoch + 30),
                ("api-null-session", "user", "api null shape should be excluded", start_epoch + 40),
                ("pgl-verify-session-1", "user", "must be excluded via P2 prefix", start_epoch + 50),
                ("tg-outsider-session", "user", "outsider telegram message", start_epoch + 60),
            ],
        )
        connection.commit()
        connection.close()

    def _write_config(self) -> None:
        payload = {
            "face": "luca",
            "host": "vps-hermes",
            "speaker": "owner",
            "obs_root": str(self.obs_root),
            "source": {
                "ssh_host": "fixture-ssh",
                "kind": "hermes-state-db",
                "db_path": str(self.profile / "state.db"),
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
                    self.obs_root / "state/luca-verify-sessions.jsonl"
                ),
                "voice_enabled": False,
            },
            "denylist_path": str(FIXTURES / "denylist.txt"),
        }
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_collect_full_pipeline_through_real_dispatcher(self) -> None:
        with mock.patch(
            "collectors.hermes_luca.adapters.subprocess.run",
            side_effect=lambda cmd, **kwargs: _run_real_dispatcher(self.root, cmd, **kwargs),
        ):
            stats = collect(RUN_DATE, config_path=self.config_path, obs_root=self.obs_root)

        records_path = self.obs_root / "obslog" / "luca" / f"{RUN_DATE}.jsonl"
        usage_path = self.obs_root / "obslog" / "luca" / f"usage-{RUN_DATE}.jsonl"
        marker_path = self.obs_root / "state" / "collector" / "luca.last-run.json"

        records = [
            json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [record["text"] for record in records],
            ["Telegram owner message", "Slack owner message"],
        )
        self.assertEqual(stats["records"], 2)
        self.assertEqual(stats["outsider_sessions"], 1)
        self.assertEqual(stats["usage_records"], 0)

        combined = records_path.read_bytes()
        for forbidden in (
            b"api null shape",
            b"must be excluded via P2 prefix",
            b"outsider telegram message",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertNotIn(b'"text":""', combined)

        # voice_enabled: false and no usage-eligible assistant text in this
        # fixture -- the existing convention is no usage file at all.
        self.assertFalse(usage_path.exists())

        self.assertTrue(marker_path.exists())
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
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
        self.assertEqual(marker["records_written"], 2)
        self.assertEqual(marker["usage_enabled"], False)
        self.assertEqual(marker["errors"], 0)

        self.assertEqual(stat.S_IMODE(records_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(records_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(marker_path.parent.stat().st_mode), 0o700)


class LucaCollectNullContentIntegrationTests(unittest.TestCase):
    """Exercise SQL NULL content through the real dispatcher and collector."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_valid_voice_safety_files(self, obs_root: Path) -> None:
        state_dir = obs_root / "state"
        collector_dir = state_dir / "collector"
        collector_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "luca-intent-journal.jsonl").write_bytes(b"")
        (state_dir / "luca-verify-sessions.jsonl").write_text(
            '{"session_id":"seed-fixture-session","recorded_at":"2026-08-01T00:00:00+09:00","origin":"seed"}\n',
            encoding="utf-8",
        )
        (collector_dir / "luca.ledger-lines.json").write_text(
            '{"line_count":1,"recorded_at":"2026-08-01T00:00:00+09:00","schema_version":1}\n',
            encoding="utf-8",
        )
        for directory in (obs_root, state_dir, collector_dir):
            os.chmod(directory, 0o700)
        for path in (
            state_dir / "luca-intent-journal.jsonl",
            state_dir / "luca-verify-sessions.jsonl",
            collector_dir / "luca.ledger-lines.json",
        ):
            os.chmod(path, 0o600)

    def _prepare_case(self, name: str, *, include_null: bool) -> tuple[Path, Path, Path]:
        root = self.root / name
        root.mkdir()
        (root / ".pgl-luca-dispatch-test-root").write_bytes(
            b"pgl-luca-dispatch test root\n"
        )
        profile = root / "home/admin/.hermes/profiles/luca"
        profile.mkdir(parents=True)
        obs_root = root / "obs-home"
        config_path = root / "obs-collector-luca.json"

        owners = {
            "platforms": {
                "telegram": [{"id": "100000001", "type": "dm"}],
                "slack": [{"id": "D0EXAMPLE01", "type": "dm"}],
            }
        }
        (profile / "channel_directory.json").write_text(
            json.dumps(owners), encoding="utf-8"
        )

        start_epoch, _ = jst_epoch_window(RUN_DATE)
        connection = sqlite3.connect(profile / "state.db")
        # The shared real-shape fixture declares messages.content as nullable TEXT.
        connection.executescript((FIXTURES / "schema.sql").read_text(encoding="utf-8"))
        connection.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            [
                ("tg-owner-session", "telegram", "dm", "100000001", "tg-owner-key"),
                ("tg-usage-session", "telegram", "dm", "100000001", "tg-usage-key"),
                ("tg-null-content-session", "telegram", "dm", "100000001", "tg-null-content-key"),
            ],
        )
        messages: list[tuple[str, str, str | None, int]] = [
            ("tg-owner-session", "user", "Telegram owner message", start_epoch + 10),
            ("tg-usage-session", "assistant", "定型の返答です", start_epoch + 20),
        ]
        if include_null:
            messages.append(
                ("tg-null-content-session", "assistant", None, start_epoch + 30)
            )
        connection.executemany(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)",
            messages,
        )
        connection.commit()
        connection.close()

        payload = {
            "face": "luca",
            "host": "vps-hermes",
            "speaker": "owner",
            "obs_root": str(obs_root),
            "source": {
                "ssh_host": "fixture-ssh",
                "kind": "hermes-state-db",
                "db_path": str(profile / "state.db"),
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
                    obs_root / "state/luca-verify-sessions.jsonl"
                ),
                "voice_enabled": False,
            },
            "denylist_path": str(FIXTURES / "denylist.txt"),
        }
        config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        ledger_dir = obs_root / "faces/luca/growth"
        ledger_dir.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "overlay-ledger.yml", ledger_dir / "overlay-ledger.yml")
        self._write_valid_voice_safety_files(obs_root)
        return root, config_path, obs_root

    def _collect_case(
        self, name: str, *, include_null: bool
    ) -> tuple[dict[str, int], bytes, bytes, dict[str, object]]:
        root, config_path, obs_root = self._prepare_case(name, include_null=include_null)
        with mock.patch(
            "collectors.hermes_luca.adapters.subprocess.run",
            side_effect=lambda cmd, **kwargs: _run_real_dispatcher(root, cmd, **kwargs),
        ):
            stats = collect(RUN_DATE, config_path=config_path, obs_root=obs_root)

        records_path = obs_root / "obslog" / "luca" / f"{RUN_DATE}.jsonl"
        usage_path = obs_root / "obslog" / "luca" / f"usage-{RUN_DATE}.jsonl"
        marker_path = obs_root / "state" / "collector" / "luca.last-run.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker.pop("run_at")
        return stats, records_path.read_bytes(), usage_path.read_bytes(), marker

    def test_real_dispatcher_pipeline_skips_null_content_without_changing_aggregation(
        self,
    ) -> None:
        baseline = self._collect_case("baseline", include_null=False)
        with_null = self._collect_case("with-null", include_null=True)

        self.assertEqual(with_null, baseline)
        stats, records, usage, _marker = with_null
        self.assertEqual(stats["records"], 1)
        self.assertEqual(stats["usage_records"], 1)
        null_session_hash = hashlib.sha256(
            b"tg-null-content-session"
        ).hexdigest()[:12].encode("ascii")
        self.assertNotIn(null_session_hash, records)
        self.assertNotIn(null_session_hash, usage)
        self.assertNotIn(b'"content":null', records)
        self.assertNotIn(b'"content":null', usage)


class LucaVoiceFullPipelineIntegrationTests(unittest.TestCase):
    """Exercise P2 voice safety through the real dispatcher stdout stream."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".pgl-luca-dispatch-test-root").write_bytes(
            b"pgl-luca-dispatch test root\n"
        )
        self.profile = self.root / "home/admin/.hermes/profiles/luca"
        self.profile.mkdir(parents=True)
        self.obs_root = self.root / "obs-home"
        self.config_path = self.root / "obs-collector-luca.json"
        self.ledger_path = self.obs_root / "state/luca-verify-sessions.jsonl"
        self.journal_path = self.obs_root / "state/luca-intent-journal.jsonl"
        self.baseline_path = self.obs_root / "state/collector/luca.ledger-lines.json"
        self._write_owners()
        self._write_database()
        self._write_config()
        self._write_private_files()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_owners(self) -> None:
        value = {
            "platforms": {
                "telegram": [{"id": "100000001", "type": "dm"}],
                "slack": [{"id": "D0EXAMPLE01", "type": "dm"}],
            }
        }
        (self.profile / "channel_directory.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def _write_database(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        connection = sqlite3.connect(self.profile / "state.db")
        connection.executescript((FIXTURES / "schema.sql").read_text(encoding="utf-8"))
        connection.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            [
                ("tg-owner", "telegram", "dm", "100000001", "tg-key"),
                ("slack-owner", "slack", "dm", "U0EXAMPLE01", "slack-key"),
                ("voice-window", "api_server", None, None, None),
                ("voice-ledger", "api_server", None, None, None),
                ("voice-survivor", "api_server", None, None, None),
            ],
        )
        connection.executemany(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)",
            [
                ("tg-owner", "user", "real-dispatch telegram", start + 1),
                ("slack-owner", "user", "real-dispatch slack", start + 2),
                ("voice-window", "user", "window must not survive", start + 101),
                ("voice-ledger", "user", "ledger must not survive", start + 3),
                ("voice-ledger", "assistant", "定型の返答です", start + 4),
                ("voice-survivor", "user", "voice survives both layers", start + 5),
            ],
        )
        connection.commit()
        connection.close()

    def _write_config(self) -> None:
        payload = {
            "face": "luca",
            "host": "vps-hermes",
            "speaker": "owner",
            "obs_root": str(self.obs_root),
            "source": {
                "ssh_host": "fixture-ssh",
                "kind": "hermes-state-db",
                "db_path": str(self.profile / "state.db"),
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
                "exclude_session_ledger": str(self.ledger_path),
                "voice_enabled": True,
            },
            "denylist_path": str(FIXTURES / "denylist.txt"),
        }
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")

    def _write_private_files(self) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "ts": "2026-08-07T00:01:40+09:00",
                            "event": "acceptance-window-open",
                            "window_id": "integration-window",
                        },
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        {
                            "ts": "2026-08-07T00:03:20+09:00",
                            "event": "acceptance-window-close",
                            "window_id": "integration-window",
                        },
                        separators=(",", ":"),
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.ledger_path.write_text(
            json.dumps(
                {
                    "session_id": "voice-ledger",
                    "recorded_at": "2026-08-06T00:00:00+09:00",
                    "origin": "seed",
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        self.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        self.baseline_path.write_text(
            '{"line_count":1,"recorded_at":"2026-08-06T00:00:00+09:00","schema_version":1}\n',
            encoding="utf-8",
        )
        for path in (self.journal_path, self.ledger_path, self.baseline_path):
            os.chmod(path, 0o600)

    def _collect_through_dispatcher(self) -> dict[str, object]:
        with mock.patch(
            "collectors.hermes_luca.adapters.subprocess.run",
            side_effect=lambda cmd, **kwargs: _run_real_dispatcher(self.root, cmd, **kwargs),
        ):
            return collect(RUN_DATE, config_path=self.config_path, obs_root=self.obs_root)

    def _record_texts(self) -> list[str]:
        path = self.obs_root / f"obslog/luca/{RUN_DATE}.jsonl"
        return [json.loads(line)["text"] for line in path.read_text(encoding="utf-8").splitlines()]

    def test_real_dispatcher_pipeline_applies_window_and_ledger_exclusions(self) -> None:
        stats = self._collect_through_dispatcher()
        self.assertEqual(
            self._record_texts(),
            ["real-dispatch telegram", "real-dispatch slack", "voice survives both layers"],
        )
        self.assertEqual(stats["voice_rows_window_excluded"], 1)
        self.assertEqual(stats["voice_sessions_ledger_excluded"], 1)
        self.assertEqual(stats["records"], 3)

    def test_real_dispatcher_voice_fail_closed_keeps_telegram_and_slack(self) -> None:
        self.journal_path.unlink()
        stats = self._collect_through_dispatcher()
        self.assertEqual(
            self._record_texts(),
            ["real-dispatch telegram", "real-dispatch slack"],
        )
        self.assertIsNotNone(stats["voice_dropped_reason"])
        self.assertTrue(any(line.startswith("[RED]") for line in stats["digest_lines"]))
        self.assertEqual(stats["records"], 2)


class LucaDispatchOutputShapeGoldenTests(unittest.TestCase):
    """Pins the *structural* contract of the real dispatcher's
    `read-owners`/`read-sessions` stdout -- key sets and value types
    (including which fields tolerate JSON null) -- without embedding any
    real conversation text or production ids as golden data (privacy): all
    ids/content here are synthetic placeholders, not values seen in
    production. A structural regression here (a renamed/added/removed key,
    or a field silently becoming non-nullable) is exactly the shape
    mismatch class that broke the collector three times in production for
    this issue.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".pgl-luca-dispatch-test-root").write_bytes(
            b"pgl-luca-dispatch test root\n"
        )
        self.profile = self.root / "home/admin/.hermes/profiles/luca"
        self.profile.mkdir(parents=True)
        self._write_synthetic_owners()
        self._write_synthetic_database()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_synthetic_owners(self) -> None:
        value = {
            "platforms": {
                "telegram": [{"id": "golden-tg-id", "type": "dm"}],
                "slack": [{"id": "golden-slack-id:0000000000.000000", "type": "dm"}],
            }
        }
        (self.profile / "channel_directory.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def _write_synthetic_database(self) -> None:
        database = self.profile / "state.db"
        connection = sqlite3.connect(database)
        connection.executescript((FIXTURES / "schema.sql").read_text(encoding="utf-8"))
        connection.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            [
                ("golden-typed-session", "telegram", "dm", "golden-tg-id", "golden-session-key"),
                ("golden-null-session", "api_server", None, None, None),
            ],
        )
        connection.executemany(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)",
            [
                ("golden-typed-session", "user", "golden-fixture-content", 200.0),
                ("golden-null-session", "assistant", "golden-null-shape-content", 210.0),
            ],
        )
        connection.commit()
        connection.close()

    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        result = _run_real_dispatcher(self.root, ["ssh", "unused-host", *argv])
        return result

    def test_read_owners_output_shape_is_pinned(self) -> None:
        result = self._run("read-owners")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        entries = json.loads(result.stdout.decode("utf-8"))
        self.assertIsInstance(entries, list)
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(set(entry), {"platform", "id", "type"})
            self.assertIsInstance(entry["platform"], str)
            self.assertIn(entry["platform"], {"telegram", "slack"})
            self.assertIsInstance(entry["id"], str)
            self.assertTrue(entry["id"])
            self.assertEqual(entry["type"], "dm")

    def test_read_sessions_output_shape_is_pinned_key_set_and_null_tolerance(self) -> None:
        result = self._run("read-sessions", "0", "999999999999")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        lines = result.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        records = [json.loads(line) for line in lines]

        expected_keys = {
            "session_id",
            "source",
            "chat_type",
            "user_id",
            "session_key",
            "content",
            "timestamp",
            "role",
        }
        by_session = {record["session_id"]: record for record in records}

        typed_record = by_session["golden-typed-session"]
        self.assertEqual(set(typed_record), expected_keys)
        self.assertIsInstance(typed_record["session_id"], str)
        self.assertEqual(typed_record["source"], "telegram")
        self.assertIsInstance(typed_record["chat_type"], str)
        self.assertIsInstance(typed_record["user_id"], str)
        self.assertIsInstance(typed_record["session_key"], str)
        self.assertIsInstance(typed_record["content"], str)
        self.assertIsInstance(typed_record["timestamp"], (int, float))
        self.assertEqual(typed_record["role"], "user")

        # api_server rows measured in production carry chat_type/user_id/
        # session_key as JSON null -- the golden fixes that those three
        # (and only those three) tolerate null, while content/session_id/
        # source/timestamp/role never do.
        null_record = by_session["golden-null-session"]
        self.assertEqual(set(null_record), expected_keys)
        self.assertIsInstance(null_record["session_id"], str)
        self.assertEqual(null_record["source"], "api_server")
        self.assertIsNone(null_record["chat_type"])
        self.assertIsNone(null_record["user_id"])
        self.assertIsNone(null_record["session_key"])
        self.assertIsInstance(null_record["content"], str)
        self.assertIsInstance(null_record["timestamp"], (int, float))
        self.assertEqual(null_record["role"], "assistant")


if __name__ == "__main__":
    unittest.main()
