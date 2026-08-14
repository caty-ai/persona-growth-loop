from __future__ import annotations

import json
import tempfile
import unicodedata
import unittest
from datetime import date, timedelta
from pathlib import Path

from aggregator.aggregate import AggregateError, aggregate
from growthlane.ledger import empty_ledger, new_phrase
from harvester.harvest import harvest


THRESHOLDS = {
    "window": 30,
    "min_count": 8,
    "min_days": 5,
    "staged_min_days": 14,
    "min_uses": 3,
    "decay_days": 90,
    "echo_ratio": 0.5,
}


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")


class GrowthLimitTests(unittest.TestCase):
    def test_fourth_new_candidate_is_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            phrases = ("なるほどだね", "たしかにそうだね", "いい感じだね", "ゆっくりいこう")
            end = date(2026, 6, 5)
            for offset in range(5):
                day = end - timedelta(days=offset)
                records = []
                for text in phrases:
                    for occurrence in range(2):
                        records.append({"ts": f"{day}T1{occurrence}:00:00+09:00", "host": "fixture", "face": "alpha", "session": f"{text}-{day}-{occurrence}", "project": "fixture", "speaker": "owner", "text": text, "len": len(text)})
                write_jsonl(obs / f"{day}.jsonl", records)
            blocklist = root / "blocklist.txt"
            blocklist.write_text("", encoding="utf-8")
            transcripts = root / "transcripts"
            transcripts.mkdir()
            (transcripts / "session.jsonl").write_bytes(b"")
            proposals = harvest(
                root,
                "alpha",
                end.isoformat(),
                {"speaker": "owner", "transcripts_root": str(transcripts)},
                empty_ledger("alpha"),
                THRESHOLDS,
                blocklist,
                [transcripts / "session.jsonl"],
            )
            self.assertEqual(len(proposals), 3)
            self.assertNotIn("ゆっくりいこう", {item["text"] for item in proposals})

    def test_third_adoption_promotion_is_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            run_day = date(2026, 7, 20)
            ledger = empty_ledger("alpha")
            observations: list[dict[str, object]] = []
            usage: list[dict[str, object]] = []
            for index in range(3):
                phrase = new_phrase(
                    f"p-{index + 1:04d}", f"口ぐせ{chr(65 + index)}",
                    {"first_seen": "2026-07-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
                )
                phrase["state"] = "staged"
                phrase["staged_at"] = "2026-07-01"
                ledger["phrases"].append(phrase)
                session = f"session-{index}"
                for use in range(3):
                    usage.append({"ts": f"{run_day}T09:0{use}:00+09:00", "session": session, "face": "alpha", "phrase_id": phrase["id"], "state": "staged"})
                text = "その言い方いいね"
                observations.append({"ts": f"{run_day}T10:00:00+09:00", "host": "fixture", "session": session, "face": "alpha", "project": "fixture", "speaker": "owner", "text": text, "len": len(text)})
            write_jsonl(obs / f"{run_day}.jsonl", observations)
            write_jsonl(obs / f"usage-{run_day}.jsonl", usage)
            _, eligible, _ = aggregate(root, "alpha", run_day.isoformat(), ledger, THRESHOLDS)
            promoted = [item["phrase_id"] for item in eligible if item["transition"] == "staged->adopted"]
            self.assertEqual(promoted, ["p-0001", "p-0002"])

    def test_aggregate_accepts_legacy_and_ucd_usage_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            run_day = date(2026, 7, 20)
            ledger = empty_ledger("alpha")
            phrase = new_phrase(
                "p-0001",
                "口ぐせA",
                {"first_seen": "2026-07-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
            )
            phrase["state"] = "staged"
            phrase["staged_at"] = "2026-07-01"
            ledger["phrases"].append(phrase)
            observations = [
                {
                    "ts": f"{run_day}T10:00:00+09:00",
                    "host": "fixture",
                    "session": "legacy",
                    "face": "alpha",
                    "project": "fixture",
                    "speaker": "owner",
                    "text": "その言い方いいね",
                    "len": len("その言い方いいね"),
                    "ucd": unicodedata.unidata_version,
                }
            ]
            usage = [
                {
                    "ts": f"{run_day}T09:00:00+09:00",
                    "session": "legacy",
                    "face": "alpha",
                    "phrase_id": "p-0001",
                    "state": "staged",
                },
                {
                    "ts": f"{run_day}T09:30:00+09:00",
                    "session": "new",
                    "face": "alpha",
                    "phrase_id": "p-0001",
                    "state": "staged",
                    "ucd": unicodedata.unidata_version,
                },
            ]
            write_jsonl(obs / f"{run_day}.jsonl", observations)
            write_jsonl(obs / f"usage-{run_day}.jsonl", usage)
            aggregated, _eligible, _ = aggregate(root, "alpha", run_day.isoformat(), ledger, THRESHOLDS)
            persisted = aggregated["phrases"][0]
            self.assertEqual(persisted["evidence"]["uses"], 2)
            self.assertEqual(persisted["evidence"]["last_used_at"], run_day.isoformat())

    def test_aggregate_rejects_unexpected_usage_record_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            run_day = date(2026, 7, 20)
            write_jsonl(
                obs / f"usage-{run_day}.jsonl",
                [
                    {
                        "ts": f"{run_day}T09:00:00+09:00",
                        "session": "legacy",
                        "face": "alpha",
                        "phrase_id": "p-0001",
                        "state": "staged",
                        "legacy_state": "staged",
                    }
                ],
            )
            with self.assertRaisesRegex(AggregateError, r"unexpected usage record keys .*usage-2026-07-20\.jsonl:1"):
                aggregate(root, "alpha", run_day.isoformat(), empty_ledger("alpha"), THRESHOLDS)

    def test_harvest_accepts_optional_ucd_field_and_rejects_mask_literals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            end = date(2026, 6, 5)
            transcripts = root / "transcripts"
            transcripts.mkdir()
            session_path = transcripts / "session.jsonl"
            session_path.write_bytes(b"")
            records: list[dict[str, object]] = []
            for offset in range(5):
                day = end - timedelta(days=offset)
                records.extend(
                    [
                        {
                            "ts": f"{day}T10:00:00+09:00",
                            "host": "fixture",
                            "face": "alpha",
                            "session": f"safe-{offset}-0",
                            "project": "fixture",
                            "speaker": "owner",
                            "text": "なるほどだね",
                            "len": len("なるほどだね"),
                            "ucd": "16.0.0",
                        },
                        {
                            "ts": f"{day}T10:30:00+09:00",
                            "host": "fixture",
                            "face": "alpha",
                            "session": f"safe-{offset}-1",
                            "project": "fixture",
                            "speaker": "owner",
                            "text": "なるほどだね",
                            "len": len("なるほどだね"),
                            "ucd": "16.0.0",
                        },
                        {
                            "ts": f"{day}T11:00:00+09:00",
                            "host": "fixture",
                            "face": "alpha",
                            "session": f"mask-{offset}-0",
                            "project": "fixture",
                            "speaker": "owner",
                            "text": "連絡は***@***です",
                            "len": len("連絡は***@***です"),
                            "ucd": "16.0.0",
                        },
                    ]
                )
            write_jsonl(obs / f"{end}.jsonl", [item for item in records if str(item["ts"]).startswith(str(end))])
            for offset in range(1, 5):
                day = end - timedelta(days=offset)
                day_records = [item for item in records if str(item["ts"]).startswith(str(day))]
                write_jsonl(obs / f"{day}.jsonl", day_records)
            blocklist = root / "blocklist.txt"
            blocklist.write_text("", encoding="utf-8")
            proposals = harvest(
                root,
                "alpha",
                end.isoformat(),
                {"speaker": "owner", "transcripts_root": str(transcripts)},
                empty_ledger("alpha"),
                THRESHOLDS,
                blocklist,
                [session_path],
            )
            self.assertEqual([item["text"] for item in proposals], ["なるほどだね"])
