from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from collectors.hermes_luca import journal, ledger


TIMESTAMP = "2026-08-10T04:00:00+09:00"
WINDOW_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_IDS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
)


class LucaNightlyPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.obs_root = self.root / "obs"
        self.journal_path = (
            self.obs_root / journal.INTENT_JOURNAL_RELATIVE_PATH
        )
        self.ledger_path = self.obs_root / "state/luca-verify-sessions.jsonl"
        self.baseline_path = (
            self.obs_root / ledger.LEDGER_LINE_COUNT_RELATIVE_PATH
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed_ledger(self, entries: list[dict[str, str]] | None = None) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        records = entries if entries is not None else [
            {
                "session_id": "historical-opaque-seed-id",
                "recorded_at": "2026-08-01T00:00:00+09:00",
                "origin": "seed",
            }
        ]
        self.ledger_path.write_text(
            "".join(
                json.dumps(record, separators=(",", ":")) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        os.chmod(self.ledger_path, 0o600)

    def _write_baseline(self, line_count: int) -> None:
        self.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        self.baseline_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "line_count": line_count,
                    "recorded_at": TIMESTAMP,
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(self.baseline_path, 0o600)

    def test_journal_helpers_write_exact_vocabulary_and_loadable_window(self) -> None:
        calls = (
            lambda: journal.append_deploy_started(self.obs_root, ts=TIMESTAMP),
            lambda: journal.append_acceptance_window_open(
                self.obs_root, ts=TIMESTAMP, window_id=WINDOW_ID
            ),
            lambda: journal.append_acceptance_window_close(
                self.obs_root,
                ts="2026-08-10T04:01:00+09:00",
                window_id=WINDOW_ID,
            ),
            lambda: journal.append_acceptance_succeeded(
                self.obs_root, ts="2026-08-10T04:01:01+09:00"
            ),
            lambda: journal.append_commit_completed(
                self.obs_root, ts="2026-08-10T04:01:02+09:00"
            ),
        )
        for append in calls:
            append()

        records = [
            json.loads(line)
            for line in self.journal_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [record["event"] for record in records],
            [
                "deploy-started",
                "acceptance-window-open",
                "acceptance-window-close",
                "acceptance-succeeded",
                "commit-completed",
            ],
        )
        self.assertEqual(
            set(records[1]), {"ts", "event", "window_id"}
        )
        self.assertEqual(records[1]["window_id"], WINDOW_ID)
        self.assertEqual(set(records[0]), {"ts", "event"})
        state = journal.load_journal(self.obs_root)
        self.assertFalse(state.has_open_window)
        self.assertEqual(state.open_window_ids, ())
        self.assertTrue(
            state.excludes(datetime.fromisoformat("2026-08-10T04:00:30+09:00").timestamp())
        )

    def test_journal_unknown_base_event_is_forward_compatible(self) -> None:
        journal.append_event(self.obs_root, "future-event", ts=TIMESTAMP)
        record = json.loads(self.journal_path.read_text(encoding="utf-8"))
        self.assertEqual(record, {"ts": TIMESTAMP, "event": "future-event"})
        self.assertEqual(journal.load_journal(self.obs_root).windows, ())

    def test_recovery_abort_and_operator_window_resolution_writers_are_loadable(self) -> None:
        journal.append_deploy_started(self.obs_root, ts=TIMESTAMP)
        journal.append_acceptance_window_open(
            self.obs_root,
            ts=TIMESTAMP,
            window_id=WINDOW_ID,
        )
        self.assertEqual(
            journal.load_journal(self.obs_root).open_window_ids,
            (WINDOW_ID,),
        )
        journal.append_acceptance_window_resolved(
            self.obs_root,
            ts="2026-08-10T04:01:00+09:00",
            window_id=WINDOW_ID,
        )
        journal.append_recovery_started(
            self.obs_root,
            ts="2026-08-10T04:01:00.500000+09:00",
        )
        journal.append_recovery_completed(
            self.obs_root,
            ts="2026-08-10T04:01:01+09:00",
        )
        journal.append_deploy_started(
            self.obs_root,
            ts="2026-08-10T04:02:00+09:00",
        )
        journal.append_deploy_aborted(
            self.obs_root,
            ts="2026-08-10T04:02:01+09:00",
        )
        self.assertEqual(
            journal.load_event_names(self.obs_root),
            (
                "deploy-started",
                "acceptance-window-open",
                "acceptance-window-resolved",
                "recovery-started",
                "recovery-completed",
                "deploy-started",
                "deploy-aborted",
            ),
        )
        self.assertFalse(journal.load_journal(self.obs_root).has_open_window)

    def test_journal_window_id_semantics_are_enforced(self) -> None:
        for event in journal.ACCEPTANCE_WINDOW_EVENTS:
            with self.subTest(event=event):
                with self.assertRaisesRegex(journal.JournalError, "window_id"):
                    journal.append_event(self.obs_root, event, ts=TIMESTAMP)
        with self.assertRaisesRegex(journal.JournalError, "only valid"):
            journal.append_event(
                self.obs_root,
                "acceptance-window-opened",
                ts=TIMESTAMP,
                window_id=WINDOW_ID,
            )

    def test_journal_create_is_0600_and_fsyncs_file_then_directory(self) -> None:
        original_fsync = os.fsync
        synced_modes: list[int] = []

        def record_fsync(descriptor: int) -> None:
            synced_modes.append(os.fstat(descriptor).st_mode)
            original_fsync(descriptor)

        with mock.patch(
            "collectors.hermes_luca.journal.os.fsync", side_effect=record_fsync
        ):
            journal.append_event(self.obs_root, "created", ts=TIMESTAMP)

        self.assertEqual(stat.S_IMODE(self.journal_path.stat().st_mode), 0o600)
        self.assertEqual(len(synced_modes), 2)
        self.assertTrue(stat.S_ISREG(synced_modes[0]))
        self.assertTrue(stat.S_ISDIR(synced_modes[1]))

    def test_journal_existing_file_fsync_failure_is_an_error(self) -> None:
        journal.append_event(self.obs_root, "first", ts=TIMESTAMP)
        with mock.patch(
            "collectors.hermes_luca.journal.os.fsync",
            side_effect=OSError("synthetic fsync failure"),
        ):
            with self.assertRaisesRegex(journal.JournalError, "fsync failure"):
                journal.append_event(self.obs_root, "second", ts=TIMESTAMP)

    def test_journal_torn_tail_refuses_append_without_merging_event(self) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        torn_record = (
            b'{"ts":"2026-08-10T04:00:00+09:00","event":"first"}'
        )
        self.journal_path.write_bytes(torn_record)
        os.chmod(self.journal_path, 0o600)

        with self.assertRaisesRegex(journal.JournalError, "ends with an incomplete event"):
            journal.append_event(self.obs_root, "second", ts=TIMESTAMP)

        self.assertEqual(self.journal_path.read_bytes(), torn_record)
        self.assertEqual(journal.load_event_names(self.obs_root), ("first",))

    def test_journal_rejects_wrong_permissions_and_symlink(self) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.write_text("", encoding="utf-8")
        os.chmod(self.journal_path, 0o644)
        with self.assertRaisesRegex(journal.JournalError, "regular 0600"):
            journal.append_event(self.obs_root, "event", ts=TIMESTAMP)

        self.journal_path.unlink()
        target = self.root / "journal-target"
        target.write_text("", encoding="utf-8")
        os.chmod(target, 0o600)
        self.journal_path.symlink_to(target)
        with self.assertRaisesRegex(journal.JournalError, "cannot append"):
            journal.append_event(self.obs_root, "event", ts=TIMESTAMP)
        self.assertEqual(target.read_bytes(), b"")

    def test_ledger_appends_every_uuid_as_one_exact_durable_payload(self) -> None:
        self._seed_ledger()
        original_write = os.write
        writes: list[bytes] = []

        def record_write(descriptor: int, payload: bytes) -> int:
            writes.append(bytes(payload))
            return original_write(descriptor, payload)

        with mock.patch(
            "collectors.hermes_luca.ledger.os.write", side_effect=record_write
        ) as write_mock, mock.patch(
            "collectors.hermes_luca.ledger.os.fsync", wraps=os.fsync
        ) as fsync_mock:
            ledger.append_acceptance_sessions(
                self.ledger_path, SESSION_IDS, recorded_at=TIMESTAMP
            )

        self.assertEqual(write_mock.call_count, 1)
        self.assertEqual(fsync_mock.call_count, 1)
        appended = [json.loads(line) for line in writes[0].splitlines()]
        self.assertEqual(
            appended,
            [
                {
                    "session_id": session_id,
                    "recorded_at": TIMESTAMP,
                    "origin": "acceptance",
                }
                for session_id in SESSION_IDS
            ],
        )

    def test_ledger_append_integrates_with_loader_and_p2_session_inputs(self) -> None:
        self._seed_ledger()
        ledger.append_acceptance_sessions(
            self.ledger_path, SESSION_IDS, recorded_at=TIMESTAMP
        )
        self._write_baseline(1)
        state = ledger.load_ledger(self.ledger_path, self.obs_root)
        self.assertEqual(state.line_count, 4)
        self.assertTrue(set(SESSION_IDS).issubset(state.session_ids))
        candidate_sessions = set(SESSION_IDS) | {"ordinary-voice-session"}
        self.assertEqual(
            candidate_sessions - state.session_ids, {"ordinary-voice-session"}
        )

    def test_ledger_rejects_noncanonical_uuid_and_timestamp_before_write(self) -> None:
        self._seed_ledger()
        before = self.ledger_path.read_bytes()
        invalid_ids = (
            "11111111111141118111111111111111",
            "11111111-1111-4111-8111-11111111111A",
            "not-a-uuid",
        )
        for session_id in invalid_ids:
            with self.subTest(session_id=session_id):
                with self.assertRaisesRegex(ledger.LedgerError, "canonical UUID"):
                    ledger.append_acceptance_sessions(
                        self.ledger_path, [session_id], recorded_at=TIMESTAMP
                    )
                self.assertEqual(self.ledger_path.read_bytes(), before)
        with self.assertRaisesRegex(ledger.LedgerError, "include an offset"):
            ledger.append_acceptance_sessions(
                self.ledger_path,
                [SESSION_IDS[0]],
                recorded_at="2026-08-10T04:00:00",
            )
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_ledger_validates_existing_schema_and_seed_before_append(self) -> None:
        invalid_ledgers = (
            [],
            [
                {
                    "session_id": "accept-only",
                    "recorded_at": TIMESTAMP,
                    "origin": "acceptance",
                }
            ],
            [
                {
                    "session_id": "seed",
                    "recorded_at": TIMESTAMP,
                    "origin": "operator",
                }
            ],
            [
                {
                    "session_id": "seed",
                    "recorded_at": TIMESTAMP,
                    "origin": "seed",
                    "extra": "forbidden",
                }
            ],
        )
        for entries in invalid_ledgers:
            with self.subTest(entries=entries):
                self._seed_ledger(entries)
                before = self.ledger_path.read_bytes()
                with self.assertRaises(ledger.LedgerError):
                    ledger.append_acceptance_sessions(
                        self.ledger_path, [SESSION_IDS[0]], recorded_at=TIMESTAMP
                    )
                self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_ledger_rejects_wrong_permissions_and_symlink_without_touching_target(self) -> None:
        self._seed_ledger()
        os.chmod(self.ledger_path, 0o644)
        with self.assertRaisesRegex(ledger.LedgerError, "regular 0600"):
            ledger.append_acceptance_sessions(
                self.ledger_path, [SESSION_IDS[0]], recorded_at=TIMESTAMP
            )

        self.ledger_path.unlink()
        target = self.root / "ledger-target"
        target.write_text(
            '{"session_id":"seed","recorded_at":"2026-08-01T00:00:00+09:00","origin":"seed"}\n',
            encoding="utf-8",
        )
        os.chmod(target, 0o600)
        before = target.read_bytes()
        self.ledger_path.symlink_to(target)
        with self.assertRaisesRegex(ledger.LedgerError, "cannot open"):
            ledger.append_acceptance_sessions(
                self.ledger_path, [SESSION_IDS[0]], recorded_at=TIMESTAMP
            )
        self.assertEqual(target.read_bytes(), before)

    def test_ledger_append_or_fsync_failure_is_an_error(self) -> None:
        self._seed_ledger()
        with mock.patch(
            "collectors.hermes_luca.ledger.os.write",
            side_effect=OSError("synthetic append failure"),
        ):
            with self.assertRaisesRegex(ledger.LedgerError, "append failure"):
                ledger.append_acceptance_sessions(
                    self.ledger_path, [SESSION_IDS[0]], recorded_at=TIMESTAMP
                )

        with mock.patch(
            "collectors.hermes_luca.ledger.os.fsync",
            side_effect=OSError("synthetic fsync failure"),
        ):
            with self.assertRaisesRegex(ledger.LedgerError, "fsync failure"):
                ledger.append_acceptance_sessions(
                    self.ledger_path, [SESSION_IDS[1]], recorded_at=TIMESTAMP
                )


if __name__ == "__main__":
    unittest.main()
