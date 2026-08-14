from __future__ import annotations

import json
import os
import stat
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from collectors.claude_code.collector import collect
from collectors.claude_code.usage_log import (
    _matching_views_cached,
    load_phrases,
    scan_usage,
    write_usage_records,
)
from growthlane.guard import matching_views
from growthlane.ledger import dump_ledger, empty_ledger, new_phrase


def write_test_ledger(path: Path, state: str = "adopted") -> None:
    ledger = empty_ledger("alpha")
    phrase = new_phrase(
        "p-0001",
        "いいね",
        {"first_seen": "2026-07-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
    )
    phrase["state"] = state
    phrase["staged_at"] = "2026-07-02"
    if state == "adopted":
        phrase["history"].append(
            {"at": "2026-07-20", "from": "staged", "to": "adopted", "by": "applier", "proposal_id": "fixture"}
        )
    ledger["phrases"].append(phrase)
    path.write_bytes(dump_ledger(ledger))


class UsageLogTests(unittest.TestCase):
    def test_matching_view_cache_is_exact_for_the_full_codepoint_space(self) -> None:
        def unexpected(_text: str) -> tuple[str, ...]:
            raise AssertionError("cache miss")

        for codepoint in range(0x110000):
            character = chr(codepoint)
            expected = matching_views(character)
            cache: dict[str, tuple[str, ...]] = {}
            self.assertEqual(
                _matching_views_cached(character, cache, matching_views),
                expected,
            )
            self.assertEqual(
                _matching_views_cached(character, cache, unexpected),
                expected,
            )

    def test_usage_caches_matching_views_per_unique_assistant_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "session.jsonl"
            entry = {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "👨‍👩 いいね"}]},
                "timestamp": "2026-08-02T14:00:00.000Z",
                "sessionId": "cache-proof",
            }
            transcript.write_text(
                "\n".join(json.dumps(entry, ensure_ascii=False) for _ in range(3)) + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "growthlane.guard.matching_views", wraps=matching_views
            ) as canonicalize:
                records, _ = scan_usage(
                    [transcript],
                    "2026-08-02",
                    "alpha",
                    [{"id": "p-0001", "text": "いいね", "state": "adopted"}],
                )
            self.assertEqual(len(records), 3)
            self.assertEqual(canonicalize.call_count, 2)

    def test_raw_fullwidth_voiced_mark_is_not_a_false_fast_accept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "\uff76\uff9e"}]},
                        "timestamp": "2026-08-02T14:00:00.000Z",
                        "sessionId": "ff9e-counterexample",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            records, _ = scan_usage(
                [transcript],
                "2026-08-02",
                "alpha",
                [{"id": "p-0001", "text": "\uff9e", "state": "adopted"}],
            )
            self.assertEqual(records, [])

    def test_usage_temp_is_pid_unique_exclusive_and_never_follows_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "usage-2026-08-02.jsonl"
            outside = root / "outside.txt"
            outside.write_text("do not overwrite\n", encoding="utf-8")
            planted = output.with_name(f".{output.name}.tmp-{os.getpid()}")
            planted.symlink_to(outside)
            with self.assertRaises(FileExistsError):
                write_usage_records(output, [{"ts": "2026-08-02T10:00:00+09:00"}])
            self.assertEqual(outside.read_text(encoding="utf-8"), "do not overwrite\n")
            self.assertFalse(output.exists())

    def test_usage_temp_is_removed_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "usage-2026-08-02.jsonl"
            temporary_path = output.with_name(f".{output.name}.tmp-{os.getpid()}")
            with mock.patch("collectors.claude_code.usage_log.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_usage_records(output, [])
            self.assertFalse(temporary_path.exists())
            self.assertFalse(output.exists())

    def test_collector_removes_only_current_stale_usage_when_lists_disappear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcripts = root / "transcripts" / "-Users-dummy-project"
            transcripts.mkdir(parents=True)
            (transcripts / "session.jsonl").write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "いいね"}]},
                        "timestamp": "2026-08-02T14:00:00.000Z",
                        "sessionId": "00000000-0000-4000-8000-000000000020",
                        "cwd": "/workspace/project",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            obs_root = root / "pgl-home"
            phrase_dir = obs_root / "faces" / "alpha"
            phrase_dir.mkdir(parents=True)
            adopted = phrase_dir / "overlay-ledger.yml"
            write_test_ledger(adopted)
            denylist = root / "denylist.txt"
            denylist.write_text("# none\n", encoding="utf-8")
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "host": "test-host",
                        "face": "alpha",
                        "speaker": "test-speaker",
                        "obs_root": str(obs_root),
                        "transcripts_root": str(transcripts.parent),
                        "denylist_path": str(denylist),
                    }
                ),
                encoding="utf-8",
            )

            collect("2026-08-02", config_path=config)
            current_usage = obs_root / "obslog" / "alpha" / "usage-2026-08-02.jsonl"
            self.assertTrue(current_usage.is_file())
            self.assertEqual(len(current_usage.read_text(encoding="utf-8").splitlines()), 1)
            other_date = current_usage.with_name("usage-2026-08-01.jsonl")
            other_date.write_text("historical\n", encoding="utf-8")

            adopted.unlink()
            collect("2026-08-02", config_path=config)
            self.assertFalse(current_usage.exists())
            self.assertTrue(other_date.is_file())

            fresh_obs_root = root / "fresh-pgl-home"
            collect("2026-08-02", config_path=config, obs_root=fresh_obs_root)
            self.assertFalse(
                (fresh_obs_root / "obslog" / "alpha" / "usage-2026-08-02.jsonl").exists()
            )

    def test_one_adopted_phrase_match_stores_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "前後の文脈 いいね 保存しない文脈"},
                                {"type": "tool_result", "content": "いいね"},
                            ]
                        },
                        "timestamp": "2026-08-02T14:00:00.000Z",
                        "sessionId": "00000000-0000-4000-8000-000000000010",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            face_dir = root / "faces" / "alpha"
            face_dir.mkdir(parents=True)
            write_test_ledger(face_dir / "overlay-ledger.yml")
            enabled, phrases, invalid = load_phrases(root / "faces", "alpha")
            self.assertTrue(enabled)
            self.assertEqual(invalid, 0)
            records, stats = scan_usage([transcript], "2026-08-02", "alpha", phrases)
            self.assertEqual(stats["usage_invalid_lines"], 0)
            self.assertEqual(len(records), 1)
            self.assertEqual(
                set(records[0]),
                {"ts", "session", "face", "phrase_id", "state", "ucd"},
            )
            self.assertEqual(records[0]["phrase_id"], "p-0001")
            self.assertEqual(records[0]["ucd"], unicodedata.unidata_version)
            self.assertNotIn("text", records[0])
            self.assertNotIn("前後の文脈", json.dumps(records, ensure_ascii=False))

            output_dir = root / "obslog" / "alpha"
            output_dir.mkdir(parents=True)
            output = output_dir / "usage-2026-08-02.jsonl"
            write_usage_records(output, records)
            first = output.read_bytes()
            write_usage_records(output, records)
            self.assertEqual(first, output.read_bytes())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_repeated_phrase_in_one_text_block_counts_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "いいね、いいね、いいね"}]},
                        "timestamp": "2026-08-02T14:00:00.000Z",
                        "sessionId": "once-per-block",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            records, _ = scan_usage(
                [transcript],
                "2026-08-02",
                "alpha",
                [{"id": "p-0001", "text": "いいね", "state": "adopted"}],
            )
            self.assertEqual(len(records), 1)

    def test_sidechain_assistant_turns_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {"content": [{"type": "text", "text": "いいね"}]},
                                "timestamp": "2026-08-02T14:00:00.000Z",
                                "sessionId": "00000000-0000-4000-8000-000000000011",
                                "isSidechain": True,
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {"content": [{"type": "text", "text": "いいね"}]},
                                "timestamp": "2026-08-02T14:05:00.000Z",
                                "sessionId": "00000000-0000-4000-8000-000000000011",
                                "isSidechain": False,
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            phrases = [{"id": "phrase-1", "text": "いいね", "state": "adopted"}]
            records, stats = scan_usage([transcript], "2026-08-02", "alpha", phrases)
            self.assertEqual(stats["files_read_errors"], 0)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["phrase_id"], "phrase-1")

    def test_missing_usage_file_is_counted_and_skipped(self) -> None:
        records, stats = scan_usage(
            [Path("/definitely/missing-usage-file.jsonl")],
            "2026-08-02",
            "alpha",
            [{"id": "phrase-1", "text": "いいね", "state": "adopted"}],
        )
        self.assertEqual(records, [])
        self.assertEqual(stats["files_read_errors"], 1)
        self.assertEqual(stats["usage_invalid_lines"], 0)

    def test_symlinked_usage_transcript_is_reported_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.jsonl"
            target.write_text("", encoding="utf-8")
            linked = root / "linked.jsonl"
            linked.symlink_to(target)
            reports: list[str] = []
            records, stats = scan_usage(
                [linked],
                "2026-08-02",
                "alpha",
                [{"id": "phrase-1", "text": "いいね", "state": "adopted"}],
                report=reports.append,
            )
        self.assertEqual(records, [])
        self.assertEqual(stats["files_read_errors"], 1)
        self.assertEqual(
            reports,
            [f"transcript skipped: symlinked file: {linked}"],
        )

    def test_usage_discards_partial_file_records_on_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "いいね"}]},
                        "timestamp": "2026-08-02T14:00:00.000Z",
                        "sessionId": "00000000-0000-4000-8000-000000000012",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            class FailingStream:
                def __init__(self, wrapped: object) -> None:
                    self._wrapped = wrapped
                    self._yielded = False

                def __enter__(self) -> "FailingStream":
                    return self

                def __exit__(self, exc_type, exc, tb) -> None:
                    self._wrapped.close()
                    return None

                def __iter__(self) -> "FailingStream":
                    return self

                def __next__(self) -> bytes:
                    if not self._yielded:
                        self._yielded = True
                        return next(self._wrapped)
                    raise OSError("simulated read failure")

            original_open = (
                "collectors.claude_code.usage_log._open_transcript_nofollow"
            )

            def failing_open(path: Path):
                stream = path.open("rb")
                if path == transcript:
                    return FailingStream(stream)
                return stream

            with mock.patch(original_open, side_effect=failing_open):
                records, stats = scan_usage(
                    [transcript],
                    "2026-08-02",
                    "alpha",
                    [{"id": "phrase-1", "text": "いいね", "state": "adopted"}],
                )
            self.assertEqual(records, [])
            self.assertEqual(stats["files_read_errors"], 1)

    def test_absent_lists_are_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            enabled, phrases, invalid = load_phrases(Path(temporary) / "faces", "alpha")
            self.assertFalse(enabled)
            self.assertEqual(phrases, [])
            self.assertEqual(invalid, 0)

    def test_unknown_ledger_schema_disables_all_phrases_with_stat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            face_dir = Path(temporary) / "faces" / "alpha"
            face_dir.mkdir(parents=True)
            (face_dir / "overlay-ledger.yml").write_text(
                "schema_version: 2\nface: alpha\nphrases: []\n", encoding="utf-8"
            )
            enabled, phrases, invalid = load_phrases(Path(temporary) / "faces", "alpha")
            self.assertFalse(enabled)
            self.assertEqual(phrases, [])
            self.assertEqual(invalid, 1)


if __name__ == "__main__":
    unittest.main()
