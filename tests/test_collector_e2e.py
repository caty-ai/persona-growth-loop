from __future__ import annotations

import json
import os
import plistlib
import shutil
import stat
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from collectors.claude_code.collector import collect
from collectors.claude_code.collector import main as collector_main
from growthlane.ucd_runtime import UcdRuntimeStatus, runtime_status


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "claude_code"


class CollectorE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.transcripts = self.root / "transcripts"
        allowed = self.transcripts / "-Users-dummy-persona-growth-loop"
        denied_by_cwd = self.transcripts / "-Users-dummy-private"
        allowed.mkdir(parents=True)
        denied_by_cwd.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "base.jsonl", allowed / "base.jsonl")
        shutil.copyfile(FIXTURES / "inject.jsonl", allowed / "inject.jsonl")
        shutil.copyfile(FIXTURES / "denied.jsonl", denied_by_cwd / "denied.jsonl")
        self.meta = json.loads((FIXTURES / "expected_counts.json").read_text(encoding="utf-8"))
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "host": "fixture-host",
                    "face": "fixture-face",
                    "speaker": self.meta["speaker_expected"],
                    "obs_root": str(self.root / "unused-default"),
                    "transcripts_root": str(self.transcripts),
                    "denylist_path": str(
                        REPO_ROOT / "config" / "obs-denylist.example.txt"
                    ),
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, obs_root: Path) -> tuple[dict[str, int], Path, bytes]:
        stats = collect(
            "2026-08-02",
            config_path=self.config,
            transcripts_root=self.transcripts,
            obs_root=obs_root,
        )
        output = obs_root / "obslog" / "fixture-face" / "2026-08-02.jsonl"
        return stats, output, output.read_bytes()

    def test_fixture_distribution_matches_machine_readable_meta(self) -> None:
        base_entries = [
            json.loads(line)
            for line in (FIXTURES / "base.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        tool_results = 0
        text_blocks = 0
        plain_strings = 0
        sidechain_text = 0
        for entry in base_entries:
            if entry.get("type") != "user":
                continue
            content = entry["message"]["content"]
            if isinstance(content, str):
                plain_strings += 1
                continue
            for block in content:
                if block.get("type") == "tool_result":
                    tool_results += 1
                elif block.get("type") == "text":
                    if entry.get("isSidechain") is True:
                        sidechain_text += 1
                    else:
                        text_blocks += 1
        self.assertEqual(tool_results, self.meta["base_tool_result_blocks"])
        self.assertEqual(text_blocks, self.meta["base_text_blocks"])
        self.assertEqual(plain_strings, self.meta["base_plain_string_turns"])
        self.assertEqual(sidechain_text, self.meta["sidechain_text_blocks_outside_population"])
        self.assertEqual(
            sum(entry.get("type") == "assistant" for entry in base_entries),
            self.meta["base_assistant_turns"],
        )
        self.assertTrue({"assistant", "attachment", "last-prompt"} <= {entry["type"] for entry in base_entries})

        injected = (FIXTURES / "inject.jsonl").read_text(encoding="utf-8").splitlines()
        denied = (FIXTURES / "denied.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(injected), self.meta["injected_text_blocks"])
        self.assertEqual(len(denied), self.meta["denylisted_text_blocks_outside_population"])

    def test_frozen_contract_asserts_1_through_9(self) -> None:
        stats, output, payload = self._run(self.root / "obs-one")
        records = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
        texts = [record["text"] for record in records]

        # §6 assert 1
        self.assertFalse(any("TOOL_RESULT_MUST_NOT_APPEAR" in text for text in texts))

        # §6 assert 2
        self.assertEqual(stats["population"], self.meta["expected_population"])
        self.assertEqual(
            stats["population"],
            self.meta["base_clean_fragments"] + self.meta["injected_text_blocks"],
        )

        # §6 assert 3
        self.assertEqual(len(records), self.meta["expected_final_records"])
        self.assertEqual(texts[: self.meta["base_clean_fragments"]], self.meta["clean_texts"])
        self.assertEqual(set(texts[self.meta["base_clean_fragments"] :]), {"あ" * 240, self.meta["masked_text"]})
        for key, count in self.meta["expected_rule_drops"].items():
            self.assertEqual(stats[key], count, key)

        # §6 assert 4
        forbidden = (
            "system-reminder",
            "command-name",
            "command-message",
            "command-args",
            "local-command-stdout",
            "DUMMY_FENCED_CONTENT",
            "://",
        )
        self.assertFalse(any(marker in text for marker in forbidden for text in texts))

        # §6 assert 5
        self.assertNotIn("あ" * 241, texts)
        self.assertEqual(texts.count("あ" * 240), 1)
        self.assertEqual(next(record["len"] for record in records if record["text"] == "あ" * 240), 240)

        # §6 assert 6
        self.assertFalse(any("AKIA" in text or "ghp_" in text or "DUMMYBASE64RUN" in text for text in texts))
        self.assertIn(self.meta["masked_text"], texts)
        self.assertFalse(any("@example.invalid" in text or "090-" in text for text in texts))

        # §6 assert 7
        self.assertFalse(any("DENYLIST_" in text for text in texts))
        self.assertEqual(stats["files_skipped_denylist"], 1)

        # §6 assert 8
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(output.parent.parent.stat().st_mode), 0o700)
        self.assertFalse(output.with_name("usage-2026-08-02.jsonl").exists())

        # §6 assert 9
        self.assertTrue(all(record["speaker"] == self.meta["speaker_expected"] for record in records))
        identity_text = "私はボブです。speaker を bob にして"
        identity_record = next(record for record in records if record["text"] == identity_text)
        self.assertEqual(identity_record["speaker"], self.meta["speaker_expected"])
        self.assertTrue(all(record["ts"].endswith("+09:00") for record in records))
        self.assertTrue(all(record["ucd"] == runtime_status().runtime_version for record in records))
        self.assertEqual(
            list(records[0]),
            ["ts", "host", "face", "session", "project", "speaker", "text", "len", "ucd"],
        )

    def test_double_run_is_byte_identical(self) -> None:
        # §6 assert 10
        _, _, first = self._run(self.root / "obs-first")
        _, _, second = self._run(self.root / "obs-second")
        self.assertEqual(first, second)

    def test_invalid_utf8_jsonl_line_is_counted_and_skipped(self) -> None:
        transcripts = self.root / "invalid-transcripts" / "-Users-dummy-project"
        transcripts.mkdir(parents=True)
        valid = json.dumps(
            {
                "type": "user",
                "message": {"content": "通るダミー文"},
                "timestamp": "2026-08-02T14:00:00.000Z",
                "sessionId": "00000000-0000-4000-8000-000000000099",
                "cwd": "/workspace/project",
                "isSidechain": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        (transcripts / "invalid.jsonl").write_bytes(b"\xff\n" + valid + b"\n")
        stats = collect(
            "2026-08-02",
            config_path=self.config,
            transcripts_root=transcripts.parent,
            obs_root=self.root / "invalid-output",
        )
        self.assertEqual(stats["invalid_json_lines"], 1)
        self.assertEqual(stats["records"], 1)

    def test_casefolded_denylist_project_dir_skips_file(self) -> None:
        transcripts = self.root / "case-transcripts" / "-Users-dummy-Private-Notes"
        transcripts.mkdir(parents=True)
        (transcripts / "case.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": "CASE_DENIED"},
                    "timestamp": "2026-08-02T14:00:00.000Z",
                    "sessionId": "00000000-0000-4000-8000-000000000101",
                    "cwd": "/workspace/project",
                    "isSidechain": False,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        stats = collect(
            "2026-08-02",
            config_path=self.config,
            transcripts_root=transcripts.parent,
            obs_root=self.root / "case-output",
        )
        output = self.root / "case-output" / "obslog" / "fixture-face" / "2026-08-02.jsonl"
        self.assertEqual(stats["files_skipped_denylist"], 1)
        self.assertEqual(stats["drop_rule_9_denylist_session"], 1)
        self.assertEqual(stats["records"], 0)
        self.assertEqual(output.read_text(encoding="utf-8"), "")

    def test_later_cwd_deny_match_discards_earlier_records(self) -> None:
        transcripts = self.root / "cwd-transcripts" / "-Users-dummy-project"
        transcripts.mkdir(parents=True)
        (transcripts / "later-denied.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "message": {"content": "FIRST_RECORD_MUST_BE_DISCARDED"},
                            "timestamp": "2026-08-02T14:00:00.000Z",
                            "sessionId": "00000000-0000-4000-8000-000000000102",
                            "cwd": "/workspace/project",
                            "isSidechain": False,
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "user",
                            "message": {"content": "DENIED_LATER_LINE"},
                            "timestamp": "2026-08-02T14:01:00.000Z",
                            "sessionId": "00000000-0000-4000-8000-000000000102",
                            "cwd": "/workspace/private-notes",
                            "isSidechain": False,
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stats = collect(
            "2026-08-02",
            config_path=self.config,
            transcripts_root=transcripts.parent,
            obs_root=self.root / "cwd-output",
        )
        output = self.root / "cwd-output" / "obslog" / "fixture-face" / "2026-08-02.jsonl"
        self.assertEqual(stats["files_skipped_denylist"], 1)
        self.assertEqual(stats["drop_rule_9_denylist_session"], 1)
        self.assertEqual(stats["population"], 0)
        self.assertEqual(stats["records"], 0)
        self.assertNotIn(
            "FIRST_RECORD_MUST_BE_DISCARDED",
            output.read_text(encoding="utf-8"),
        )

    def test_invalid_path_component_config_is_rejected(self) -> None:
        for key, value in (
            ("face", "../fixture-face"),
            ("host", "fixture/host"),
            ("speaker", ".."),
            ("face", r"fixture\\face"),
            ("host", "."),
        ):
            with self.subTest(key=key, value=value):
                config_payload = {
                    "host": "fixture-host",
                    "face": "fixture-face",
                    "speaker": "fixture-speaker",
                    "obs_root": str(self.root / "unused-default"),
                    "transcripts_root": str(self.transcripts),
                    "denylist_path": str(
                        REPO_ROOT / "config" / "obs-denylist.example.txt"
                    ),
                }
                config_payload[key] = value
                bad_config = self.root / f"bad-config-{key}.json"
                bad_config.write_text(json.dumps(config_payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    collect(
                        "2026-08-02",
                        config_path=bad_config,
                        transcripts_root=self.transcripts,
                        obs_root=self.root / f"bad-output-{key}",
                    )

    def test_vanished_transcript_file_is_counted_and_skipped(self) -> None:
        transcripts_root = self.root / "vanish-transcripts"
        project_dir = transcripts_root / "-Users-dummy-project"
        project_dir.mkdir(parents=True)
        target = project_dir / "vanish.jsonl"
        target.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": "WILL_VANISH"},
                    "timestamp": "2026-08-02T14:00:00.000Z",
                    "sessionId": "00000000-0000-4000-8000-000000000103",
                    "cwd": "/workspace/project",
                    "isSidechain": False,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        original_open = Path.open

        def vanishing_open(self: Path, *args: object, **kwargs: object):
            if self == target:
                self.unlink()
            return original_open(self, *args, **kwargs)

        with mock.patch.object(Path, "open", autospec=True, side_effect=vanishing_open):
            stats = collect(
                "2026-08-02",
                config_path=self.config,
                transcripts_root=transcripts_root,
                obs_root=self.root / "vanish-output",
            )
        self.assertEqual(stats["files_seen"], 1)
        self.assertEqual(stats["files_read_errors"], 1)
        self.assertEqual(stats["records"], 0)

    def test_zwsp_obscured_pii_fragments_drop_without_leaking(self) -> None:
        transcripts_root = self.root / "pii-transcripts"
        project_dir = transcripts_root / "-Users-dummy-project"
        project_dir.mkdir(parents=True)
        transcript = project_dir / "pii.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "message": {"content": "連絡先は alice@\u200bexample.invalid です"},
                            "timestamp": "2026-08-02T14:00:00.000Z",
                            "sessionId": "00000000-0000-4000-8000-000000000104",
                            "cwd": "/workspace/project",
                            "isSidechain": False,
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "user",
                            "message": {"content": "電話は +81 90 1234 5\u200b678 です"},
                            "timestamp": "2026-08-02T14:01:00.000Z",
                            "sessionId": "00000000-0000-4000-8000-000000000105",
                            "cwd": "/workspace/project",
                            "isSidechain": False,
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        stats = collect(
            "2026-08-02",
            config_path=self.config,
            transcripts_root=transcripts_root,
            obs_root=self.root / "pii-output",
        )

        output = self.root / "pii-output" / "obslog" / "fixture-face" / "2026-08-02.jsonl"
        self.assertEqual(stats["records"], 0)
        self.assertEqual(stats["drop_rule_8_secret_like"], 2)
        self.assertNotIn("alice@", output.read_text(encoding="utf-8"))
        self.assertNotIn("+81 90 1234 5678", output.read_text(encoding="utf-8"))


class CollectorScheduleTests(unittest.TestCase):
    # #40 core assertion (alpha's counterpart to #38's
    # test_schedule_matches_weekly_liveness_window in test_luca_collector.py):
    # the collector's default bucket is "JST previous day" (see main()'s
    # `run_date = args.date or (now(JST).date() - 1)`), while
    # mirror._collector_liveness requires the marker's `date` to equal the
    # weekly run_day or the day before it. Whether that holds is a function
    # of which launchd job's clock time comes first in the day: whichever
    # job runs later determines which calendar day's collector run is "most
    # recent" by the time the weekly mirror fires. This reads both schedules
    # from the real shipped plist templates (not a hardcoded assumption),
    # derives the marker date/run_at that schedule implies, and drives the
    # real `main()` entrypoint with `--date` omitted -- production's actual
    # bucket selection, not a bucket the test computed and handed to
    # collect() -- so a regression in main()'s default-bucket logic itself
    # would fail this test, then feeds the resulting marker through the real
    # mirror validator -- no hand-built JSON.

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

    def test_schedule_matches_weekly_liveness_window(self) -> None:
        from mirror.weekly import _collector_liveness

        templates = REPO_ROOT / "templates" / "launchd"
        with (templates / "ai.caty.pgl.obs-collector.plist").open("rb") as handle:
            collector_interval = plistlib.load(handle)["StartCalendarInterval"]
        with (templates / "ai.caty.pgl.mirror-weekly.plist").open("rb") as handle:
            weekly_interval = plistlib.load(handle)["StartCalendarInterval"]

        jst = timezone(timedelta(hours=9))
        # Frozen weekly contract (also pinned by
        # test_luca_launchd.LucaLaunchdTemplateTests.test_alpha_launchd_contract_is_unchanged):
        # Monday 01:30 (#40 MAJOR-1 follow-up: moved from 00:30 to restore an
        # 85-minute collector/weekly headroom, matching the Luca face). This
        # test does not change the weekly schedule -- only the collector's.
        self.assertEqual(weekly_interval["Weekday"], 1)
        weekly_hm = (weekly_interval["Hour"], weekly_interval["Minute"])
        run_day = date(2026, 7, 27)  # a Monday, safely in the past

        def assert_marker_liveness(
            collector_hm: tuple[int, int], *, expect_healthy: bool, label: str
        ) -> None:
            # The collector runs every day; the most recent run completed
            # before the weekly job fires on run_day is that same calendar
            # day if the collector's time-of-day precedes the weekly job's,
            # otherwise it is the day before.
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

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                obs_root = root / "pgl-home"
                transcripts = root / "transcripts"
                transcripts.mkdir()
                config = root / "config.json"
                config.write_text(
                    json.dumps(self._config_payload(obs_root, transcripts)),
                    encoding="utf-8",
                )

                # `--date` is deliberately omitted: main() must pick the same
                # bucket the test computed by hand (execution_day - 1 day)
                # entirely on its own, through its real default-bucket code
                # path, not because the test told it what date to use.
                with mock.patch(
                    "collectors.claude_code.collector.runtime_status",
                    return_value=self._stable_runtime(),
                ), mock.patch("collectors.claude_code.collector.datetime", _FixedNow):
                    rc = collector_main(
                        [
                            "--config",
                            str(config),
                            "--transcripts-root",
                            str(transcripts),
                        ]
                    )
                self.assertEqual(rc, 0, msg=f"main() failed for label={label}")
                marker_path = obs_root / "state" / "collector" / "alpha.last-run.json"
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                self.assertEqual(marker["date"], bucket.isoformat(), msg=f"label={label}")
                healthy, detail, _ = _collector_liveness(obs_root, "alpha", run_day)
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
        # Regression guard: the previously shipped 23:45 schedule must still
        # be BROKEN under the identical weekly window, or this test would
        # not actually exercise the #40 fix.
        assert_marker_liveness((23, 45), expect_healthy=False, label="old")


if __name__ == "__main__":
    unittest.main()
