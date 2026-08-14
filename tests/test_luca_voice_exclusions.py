from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collectors.hermes_luca import journal
from collectors.hermes_luca.adapters import Turn
from collectors.hermes_luca.collector import (
    PERSONA_MARKER_PREFIX,
    WARMUP_MARKER,
    collect,
    jst_epoch_window,
    main,
)
from collectors.hermes_luca.config import ConfigError, load_config


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests/fixtures/hermes_luca"
RUN_DATE = "2026-08-02"

# Source: the private engine repo's deploy runbook (verify emitter).
EMITTER_PERSONA_MESSAGES = ("/persona public", "/persona mode-b")
EMITTER_WARMUP_MESSAGES = ("deployment warm-up",)
# Source: the private engine repo's acceptance script (journal writer).
EMITTER_JOURNAL_EVENTS = (
    {
        "ts": "2026-08-02T00:01:40+09:00",
        "event": "acceptance-window-open",
        "window_id": "11111111-1111-4111-8111-111111111111",
    },
    {
        "ts": "2026-08-02T00:03:20+09:00",
        "event": "acceptance-window-close",
        "window_id": "11111111-1111-4111-8111-111111111111",
    },
)


class LucaVoiceExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.obs_root = self.root / "obs-home"
        self.db_path = self.root / "state.db"
        self.config_path = self.root / "config.json"
        self.owners_path = self.root / "owners.json"
        self.denylist_path = self.root / "denylist.txt"
        self.acceptance_ledger_path = self.obs_root / "state/luca-verify-sessions.jsonl"
        self.journal_path = self.obs_root / "state/luca-intent-journal.jsonl"
        self.baseline_path = self.obs_root / "state/collector/luca.ledger-lines.json"
        self._message_id = 1
        shutil.copyfile(FIXTURES / "owners.json", self.owners_path)
        self.denylist_path.write_text("", encoding="utf-8")
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            connection.executescript((FIXTURES / "schema.sql").read_text(encoding="utf-8"))
            connection.commit()
        self._write_config()
        self._write_journal([])
        self._write_ledger(
            [
                {
                    "session_id": "seed-fixture-session",
                    "recorded_at": "2026-08-01T00:00:00+09:00",
                    "origin": "seed",
                }
            ]
        )
        self._write_baseline(1)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _config(self) -> dict[str, object]:
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
                "exclude_session_ledger": str(self.acceptance_ledger_path),
                "voice_enabled": True,
            },
            "denylist_path": str(self.denylist_path),
        }

    def _write_config(self, payload: dict[str, object] | None = None) -> None:
        self.config_path.write_text(
            json.dumps(payload or self._config(), ensure_ascii=False), encoding="utf-8"
        )

    def _write_private_jsonl(self, path: Path, entries: list[object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            for entry in entries
        )
        path.write_text(payload, encoding="utf-8")
        os.chmod(path, 0o600)

    def _write_journal(self, entries: list[object]) -> None:
        self._write_private_jsonl(self.journal_path, entries)

    def _write_ledger(self, entries: list[object]) -> None:
        self._write_private_jsonl(self.acceptance_ledger_path, entries)

    def _write_baseline(self, line_count: int) -> None:
        self.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        self.baseline_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "line_count": line_count,
                    "recorded_at": "2026-08-01T00:00:00+09:00",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(self.baseline_path, 0o600)

    def _add_session(
        self,
        session_id: str,
        source: str,
        turns: list[tuple[str, str, int]],
        *,
        session_key: str | None = None,
        user_id: str | None | object = ...,
    ) -> None:
        chat_type = None if source == "api_server" else "dm"
        if user_id is ...:
            if source == "telegram":
                user_id = "100000001"
            elif source == "slack":
                user_id = "U0EXAMPLE01"
            else:
                user_id = None
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?)",
                (session_id, source, chat_type, user_id, session_key),
            )
            for role, content, timestamp in turns:
                connection.execute(
                    "INSERT INTO messages(id,session_id,role,content,timestamp) VALUES (?,?,?,?,?)",
                    (self._message_id, session_id, role, content, timestamp),
                )
                self._message_id += 1
            connection.commit()

    def _add_three_sources(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        self._add_session("tg-ok", "telegram", [("user", "telegram survives", start + 1)])
        self._add_session("slack-ok", "slack", [("user", "slack survives", start + 2)])
        self._add_session("voice-ok", "api_server", [("user", "voice candidate", start + 3)])

    def _collect(self) -> dict[str, object]:
        return collect(
            RUN_DATE,
            config_path=self.config_path,
            sqlite_path=self.db_path,
            owners_json=self.owners_path,
            obs_root=self.obs_root,
        )

    def _texts(self) -> list[str]:
        path = self.obs_root / f"obslog/luca/{RUN_DATE}.jsonl"
        return [json.loads(line)["text"] for line in path.read_text(encoding="utf-8").splitlines()]

    def _assert_voice_fail_closed_with_other_sources(self, stats: dict[str, object]) -> None:
        self.assertEqual(self._texts(), ["telegram survives", "slack survives"])
        self.assertIsNotNone(stats["voice_dropped_reason"])
        self.assertTrue(any(line.startswith("[RED]") for line in stats["digest_lines"]))
        self.assertFalse(any("/persona markers=" in line for line in stats["digest_lines"]))
        marker = json.loads(
            (self.obs_root / "state/collector/luca.last-run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["records_written"], 2)
        self.assertEqual(marker["errors"], 0)

    def test_journal_missing_fails_closed_when_voice_enabled_without_api_source(self) -> None:
        payload = self._config()
        payload["source"]["sources"] = ["telegram", "slack"]  # type: ignore[index]
        self._write_config(payload)
        start, _ = jst_epoch_window(RUN_DATE)
        self._add_session("tg-ok", "telegram", [("user", "telegram survives", start + 1)])
        self._add_session("slack-ok", "slack", [("user", "slack survives", start + 2)])
        self.journal_path.unlink()
        stats = self._collect()
        self.assertEqual(self._texts(), ["telegram survives", "slack survives"])
        self.assertIsNotNone(stats["voice_dropped_reason"])
        self.assertTrue(any(line.startswith("[RED]") for line in stats["digest_lines"]))

    def test_journal_missing_fails_closed_when_api_source_is_present_but_voice_disabled(self) -> None:
        payload = self._config()
        payload["source"]["voice_enabled"] = False  # type: ignore[index]
        self._write_config(payload)
        self._add_three_sources()
        self.journal_path.unlink()
        self._assert_voice_fail_closed_with_other_sources(self._collect())

    def test_journal_one_bad_line_invalidates_whole_file(self) -> None:
        self._add_three_sources()
        self.journal_path.write_text(
            '{"ts":"2026-08-02T00:00:00+09:00","event":"future-event"}\nnot-json\n',
            encoding="utf-8",
        )
        os.chmod(self.journal_path, 0o600)
        self._assert_voice_fail_closed_with_other_sources(self._collect())

    def test_journal_unreadable_mode_fails_closed_only_voice(self) -> None:
        self._add_three_sources()
        os.chmod(self.journal_path, 0o000)
        self._assert_voice_fail_closed_with_other_sources(self._collect())

    def test_empty_journal_is_valid_and_unknown_event_is_forward_compatible(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        self._add_session("voice-ok", "api_server", [("user", "voice candidate", start + 3)])
        stats = self._collect()
        self.assertEqual(self._texts(), ["voice candidate"])
        self.assertIsNone(stats["voice_dropped_reason"])
        self._write_journal(
            [{"ts": "2026-08-02T00:00:00+09:00", "event": "future-deploy-start"}]
        )
        stats = self._collect()
        self.assertEqual(self._texts(), ["voice candidate"])
        self.assertIsNone(stats["voice_dropped_reason"])

    def test_window_is_line_granular_and_margin_boundaries_are_inclusive(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        open_epoch = start + 100
        self._write_journal(
            [
                {
                    "ts": "2026-08-02T00:01:40+09:00",
                    "event": "acceptance-window-open",
                    "window_id": "window-a",
                },
                {
                    "ts": "2026-08-02T00:03:20+09:00",
                    "event": "acceptance-window-close",
                    "window_id": "window-a",
                },
            ]
        )
        self._add_session(
            "same-voice-session",
            "api_server",
            [
                ("user", "outside before", open_epoch - 61),
                ("user", "boundary inside", open_epoch - 60),
                ("user", "inside", open_epoch + 1),
                ("user", "close boundary inside", start + 260),
                ("user", "outside after", start + 261),
            ],
        )
        stats = self._collect()
        self.assertEqual(self._texts(), ["outside before", "outside after"])
        self.assertEqual(stats["voice_rows_window_excluded"], 3)

    def test_open_window_stays_red_until_resolved_then_closes(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        opened = {
            "ts": "2026-08-02T00:01:40+09:00",
            "event": "acceptance-window-open",
            "window_id": "window-a",
        }
        self._write_journal([opened])
        self._add_session(
            "voice-crash-window",
            "api_server",
            [
                ("user", "before crash window", start + 39),
                ("user", "after crash open", start + 101),
            ],
        )
        stats = self._collect()
        self.assertEqual(self._texts(), ["before crash window"])
        self.assertTrue(any("unresolved acceptance window" in line for line in stats["digest_lines"]))

        self._write_journal(
            [
                opened,
                {
                    "ts": "2026-08-02T00:03:20+09:00",
                    "event": "acceptance-window-resolved",
                    "window_id": "window-a",
                },
            ]
        )
        stats = self._collect()
        self.assertFalse(any("unresolved acceptance window" in line for line in stats["digest_lines"]))

    def test_journal_consistency_violations_fail_closed(self) -> None:
        self._add_three_sources()
        invalid_cases = (
            [
                {
                    "ts": "2026-08-02T00:02:00+09:00",
                    "event": "acceptance-window-close",
                    "window_id": "missing",
                }
            ],
            [
                {
                    "ts": "2026-08-02T00:01:00+09:00",
                    "event": "acceptance-window-open",
                    "window_id": "duplicate",
                },
                {
                    "ts": "2026-08-02T00:02:00+09:00",
                    "event": "acceptance-window-open",
                    "window_id": "duplicate",
                },
            ],
            [
                {
                    "ts": "2026-08-02T00:02:00+09:00",
                    "event": "acceptance-window-open",
                    "window_id": "backwards",
                },
                {
                    "ts": "2026-08-02T00:01:00+09:00",
                    "event": "acceptance-window-close",
                    "window_id": "backwards",
                },
            ],
        )
        for entries in invalid_cases:
            with self.subTest(entries=entries):
                self._write_journal(entries)
                stats = self._collect()
                self.assertIsNotNone(stats["voice_dropped_reason"])

    def test_ledger_empty_seedless_malformed_and_unknown_origin_fail_closed(self) -> None:
        self._add_three_sources()
        cases: tuple[tuple[str, bytes], ...] = (
            ("empty", b""),
            (
                "seedless",
                b'{"session_id":"accept-only","recorded_at":"2026-08-01T00:00:00+09:00","origin":"acceptance"}\n',
            ),
            (
                "mixed-malformed",
                b'{"session_id":"seed","recorded_at":"2026-08-01T00:00:00+09:00","origin":"seed"}\nnot-json\n',
            ),
            (
                "unknown-origin",
                b'{"session_id":"seed","recorded_at":"2026-08-01T00:00:00+09:00","origin":"operator"}\n',
            ),
            (
                "invalid-recorded-at",
                b'{"session_id":"seed","recorded_at":"not-iso","origin":"seed"}\n',
            ),
        )
        for label, payload in cases:
            with self.subTest(label=label):
                self.acceptance_ledger_path.write_bytes(payload)
                os.chmod(self.acceptance_ledger_path, 0o600)
                self._assert_voice_fail_closed_with_other_sources(self._collect())

    def test_ledger_missing_and_unreadable_fail_closed_only_voice(self) -> None:
        self._add_three_sources()
        self.acceptance_ledger_path.unlink()
        self._assert_voice_fail_closed_with_other_sources(self._collect())
        self._write_ledger(
            [
                {
                    "session_id": "seed",
                    "recorded_at": "2026-08-01T00:00:00+09:00",
                    "origin": "seed",
                }
            ]
        )
        os.chmod(self.acceptance_ledger_path, 0o000)
        self._assert_voice_fail_closed_with_other_sources(self._collect())

    def test_symlinked_ledger_fails_closed(self) -> None:
        self._add_three_sources()
        real_ledger = self.root / "real-ledger.jsonl"
        real_ledger.write_text(
            '{"session_id":"seed","recorded_at":"2026-08-01T00:00:00+09:00","origin":"seed"}\n',
            encoding="utf-8",
        )
        os.chmod(real_ledger, 0o600)
        self.acceptance_ledger_path.unlink()
        self.acceptance_ledger_path.symlink_to(real_ledger)
        stats = self._collect()
        self._assert_voice_fail_closed_with_other_sources(stats)
        self.assertIn("cannot open acceptance ledger", str(stats["voice_dropped_reason"]))

    def test_wrong_uuid_does_not_exclude_real_session(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        self._write_ledger(
            [
                {
                    "session_id": "00000000-0000-4000-8000-000000000000",
                    "recorded_at": "2026-08-01T00:00:00+09:00",
                    "origin": "seed",
                }
            ]
        )
        self._add_session("real-voice-session", "api_server", [("user", "must survive", start + 1)])
        stats = self._collect()
        self.assertEqual(self._texts(), ["must survive"])
        self.assertEqual(stats["voice_sessions_ledger_excluded"], 0)

    def test_ledger_excludes_user_and_assistant_before_obslog_and_usage(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        self._write_ledger(
            [
                {
                    "session_id": "ledger-listed",
                    "recorded_at": "2026-08-01T00:00:00+09:00",
                    "origin": "seed",
                }
            ]
        )
        growth_dir = self.obs_root / "faces/luca/growth"
        growth_dir.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "overlay-ledger.yml", growth_dir / "overlay-ledger.yml")
        self._add_session(
            "ledger-listed",
            "api_server",
            [
                ("user", "listed user raw", start + 1),
                ("assistant", "定型の返答です", start + 2),
            ],
        )
        self._add_session(
            "usage-control",
            "api_server",
            [("assistant", "定型の返答です", start + 3)],
        )
        stats = self._collect()
        self.assertEqual(self._texts(), [])
        self.assertEqual(stats["voice_sessions_ledger_excluded"], 1)
        usage_path = self.obs_root / f"obslog/luca/usage-{RUN_DATE}.jsonl"
        usage_records = [
            json.loads(line) for line in usage_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(usage_records), 1)
        listed_hash = hashlib.sha256(b"ledger-listed").hexdigest()[:12]
        self.assertNotEqual(usage_records[0]["session"], listed_hash)

    def test_ledger_line_count_decrease_fails_closed_without_baseline_update(self) -> None:
        self._add_three_sources()
        self._write_ledger(
            [
                {
                    "session_id": f"seed-{index}",
                    "recorded_at": "2026-08-01T00:00:00+09:00",
                    "origin": "seed",
                }
                for index in range(3)
            ]
        )
        self._write_baseline(5)
        before = self.baseline_path.read_bytes()
        self._assert_voice_fail_closed_with_other_sources(self._collect())
        self.assertEqual(self.baseline_path.read_bytes(), before)

    def test_missing_initial_baseline_fails_closed(self) -> None:
        self._add_three_sources()
        self.baseline_path.unlink()
        self._assert_voice_fail_closed_with_other_sources(self._collect())

    def test_valid_ledger_updates_baseline_and_equal_count_passes(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        self._add_session("voice-ok", "api_server", [("user", "voice candidate", start + 1)])
        first = self._collect()
        second = self._collect()
        self.assertIsNone(first["voice_dropped_reason"])
        self.assertIsNone(second["voice_dropped_reason"])
        baseline = json.loads(self.baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(baseline["line_count"], 1)
        self.assertEqual(stat.S_IMODE(self.baseline_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.baseline_path.parent.stat().st_mode), 0o700)

    def test_markers_scan_only_surviving_raw_voice_user_turns(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        self._write_journal(
            [
                {
                    "ts": "2026-08-02T00:01:40+09:00",
                    "event": "acceptance-window-open",
                    "window_id": "marker-window",
                },
                {
                    "ts": "2026-08-02T00:03:20+09:00",
                    "event": "acceptance-window-close",
                    "window_id": "marker-window",
                },
            ]
        )
        self._write_ledger(
            [
                {
                    "session_id": "ledger-marker",
                    "recorded_at": "2026-08-01T00:00:00+09:00",
                    "origin": "seed",
                }
            ]
        )
        self._add_session("warmup-survivor", "api_server", [("user", "deployment warm-up", start + 1)])
        self._add_session("persona-survivor", "api_server", [("user", "/persona focus", start + 2)])
        self._add_session("window-marker", "api_server", [("user", "deployment warm-up", start + 101)])
        self._add_session("ledger-marker", "api_server", [("user", "/persona hidden", start + 3)])
        stats = self._collect()
        self.assertEqual(stats["warmup_marker_count"], 1)
        self.assertEqual(stats["persona_marker_count"], 1)
        self.assertEqual(
            sum(line.startswith("[RED]") for line in stats["digest_lines"]),
            1,
        )
        self.assertTrue(any("surviving /persona markers=1" in line for line in stats["digest_lines"]))

    def test_rejected_voice_session_still_emits_warmup_red_after_structural_filters(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        self._add_session(
            "voice-rejected",
            "api_server",
            [("user", "deployment warm-up", start + 1)],
            user_id="unexpected-owner",
        )
        stats = self._collect()
        self.assertEqual(self._texts(), [])
        self.assertEqual(stats["warmup_marker_count"], 1)
        self.assertTrue(
            any("surviving deployment warm-up markers=1" in line for line in stats["digest_lines"])
        )

    def test_persona_zero_count_line_is_emitted_without_red(self) -> None:
        stats = self._collect()
        self.assertEqual(
            stats["digest_lines"],
            ("[INFO] luca: collector: surviving /persona markers=0",),
        )

    def test_journal_writer_events_are_accepted_by_parser(self) -> None:
        for number, event in enumerate(EMITTER_JOURNAL_EVENTS, 1):
            parsed = journal._parse_event(
                json.dumps(event, ensure_ascii=False, separators=(",", ":")),
                number,
            )
            self.assertEqual(parsed["event"], event["event"])
            self.assertEqual(parsed["window_id"], event["window_id"])

    def test_emitter_messages_are_covered_by_detection_marker_contract(self) -> None:
        for message in EMITTER_WARMUP_MESSAGES:
            self.assertEqual(message, WARMUP_MARKER)
        for message in EMITTER_PERSONA_MESSAGES:
            self.assertTrue(message.startswith(PERSONA_MARKER_PREFIX))

    def test_prefix_warn_counts_mixed_source_prefixed_session_when_any_turn_is_messenger(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        mocked_turns = [
            Turn(
                session_id="mixed-prefixed",
                source="api_server",
                chat_type=None,
                user_id=None,
                session_key="pgl-verify-mixed",
                content="voice turn",
                timestamp=start + 1,
                role="user",
            ),
            Turn(
                session_id="mixed-prefixed",
                source="telegram",
                chat_type="dm",
                user_id="100000001",
                session_key="pgl-verify-mixed",
                content="telegram turn",
                timestamp=start + 2,
                role="user",
            ),
        ]
        with mock.patch(
            "collectors.hermes_luca.collector.LocalSQLiteAdapter.fetch_turns",
            return_value=mocked_turns,
        ):
            stats = self._collect()
        self.assertEqual(self._texts(), [])
        self.assertEqual(stats["prefix_warn_sessions"], 1)
        self.assertTrue(any(line.startswith("[WARN]") for line in stats["digest_lines"]))

    def test_telegram_and_slack_prefix_exclusions_emit_warn(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        self._add_session(
            "pgl-verify-telegram",
            "telegram",
            [("user", "must be excluded", start + 1)],
        )
        self._add_session(
            "safe-slack-session",
            "slack",
            [("user", "must also be excluded", start + 2)],
            session_key="pgl-verify-slack",
        )
        stats = self._collect()
        self.assertEqual(self._texts(), [])
        self.assertEqual(stats["prefix_warn_sessions"], 2)
        self.assertTrue(any(line.startswith("[WARN]") for line in stats["digest_lines"]))

    def test_config_requires_ledger_for_either_voice_gate(self) -> None:
        cases = ((True, ["telegram", "slack"]), (False, ["telegram", "api_server"]))
        for voice_enabled, sources in cases:
            with self.subTest(voice_enabled=voice_enabled, sources=sources):
                payload = self._config()
                payload["source"]["voice_enabled"] = voice_enabled  # type: ignore[index]
                payload["source"]["sources"] = sources  # type: ignore[index]
                del payload["source"]["exclude_session_ledger"]  # type: ignore[index]
                self._write_config(payload)
                with self.assertRaises(ConfigError):
                    load_config(self.config_path)

    def test_voice_outputs_are_private_and_observation_bytes_are_deterministic(self) -> None:
        start, _ = jst_epoch_window(RUN_DATE)
        self._add_session("voice-ok", "api_server", [("user", "deterministic voice", start + 1)])
        self._collect()
        records_path = self.obs_root / f"obslog/luca/{RUN_DATE}.jsonl"
        first = records_path.read_bytes()
        self._collect()
        self.assertEqual(records_path.read_bytes(), first)
        self.assertEqual(stat.S_IMODE(records_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(records_path.parent.stat().st_mode), 0o700)
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.obs_root.rglob("*")))

    def test_voice_disabled_does_not_touch_safety_files(self) -> None:
        payload = self._config()
        payload["source"]["voice_enabled"] = False  # type: ignore[index]
        payload["source"]["sources"] = ["telegram", "slack"]  # type: ignore[index]
        self._write_config(payload)
        self.journal_path.unlink()
        self.acceptance_ledger_path.unlink()
        self.baseline_path.unlink()
        start, _ = jst_epoch_window(RUN_DATE)
        self._add_session("tg-ok", "telegram", [("user", "telegram survives", start + 1)])
        stats = self._collect()
        self.assertEqual(self._texts(), ["telegram survives"])
        self.assertIsNone(stats["voice_dropped_reason"])
        self.assertFalse(self.journal_path.exists())
        self.assertFalse(self.acceptance_ledger_path.exists())
        self.assertFalse(self.baseline_path.exists())

    def test_baseline_schema_version_must_be_integer(self) -> None:
        self._add_three_sources()
        self.baseline_path.write_text(
            '{"schema_version":1.0,"line_count":1,"recorded_at":"2026-08-01T00:00:00+09:00"}\n',
            encoding="utf-8",
        )
        os.chmod(self.baseline_path, 0o600)
        self._assert_voice_fail_closed_with_other_sources(self._collect())

    def test_main_voice_fail_closed_emits_digest_and_stderr_but_exits_zero(self) -> None:
        self._add_three_sources()
        self.journal_path.unlink()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(
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
        self.assertEqual(result, 0)
        self.assertIn("[RED] luca: collector: voice fail-closed", stderr.getvalue())
        digest = (self.obs_root / f"digest/{RUN_DATE}.md").read_text(encoding="utf-8")
        self.assertIn("[RED] luca: collector: voice fail-closed", digest)
        marker = json.loads(
            (self.obs_root / "state/collector/luca.last-run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["errors"], 0)

    def test_baseline_update_failure_is_voice_fail_closed_not_global_failure(self) -> None:
        self._add_three_sources()
        with mock.patch(
            "collectors.hermes_luca.collector.ledger.write_line_count",
            side_effect=OSError("synthetic baseline write failure"),
        ):
            stats = self._collect()
        self._assert_voice_fail_closed_with_other_sources(stats)
        self.assertIn("baseline update", str(stats["voice_dropped_reason"]))


if __name__ == "__main__":
    unittest.main()
