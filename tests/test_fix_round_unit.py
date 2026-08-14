from __future__ import annotations

import json
import inspect
import os
import stat
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aggregator.aggregate import AggregateError, aggregate, negative_pattern
from applier.apply import ApplyError, _require_overlay_home
from classifierd.classify import classify
from collectors.claude_code.usage_log import assistant_echo_days
from growthlane.config import ConfigError, DEFAULT_THRESHOLDS, load_json_object, parse_scalar_yaml
from growthlane.faces import get_profile
from growthlane.gates import GateError, check_killswitch, check_mirror, killswitch_mode
from growthlane.guard import canonicalize_for_storage, lint_phrase, matching_views
from growthlane.holdout import exposed
from growthlane.ledger import dump_ledger, empty_ledger, load_ledger, new_phrase
from growthlane.soul import SoulError, verify_manifest, write_manifest
from growthlane.render import render_files
from harvester.harvest import HarvestError, harvest, transcript_inputs
from writerd.propose import AdapterError


JST = timezone(timedelta(hours=9))


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
        encoding="utf-8",
    )


class FixRoundUnitTests(unittest.TestCase):
    def test_killswitch_non_regular_shapes_are_strict_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "KILLSWITCH"
            target = root / "target.yml"
            target.write_text("mode: eject\n", encoding="utf-8")
            cases: list[tuple[str, object]] = [
                ("dangling-symlink", lambda: marker.symlink_to(root / "missing")),
                ("symlink", lambda: marker.symlink_to(target)),
                ("directory", marker.mkdir),
                ("fifo", lambda: os.mkfifo(marker)),
            ]
            for expected_shape, create in cases:
                with self.subTest(shape=expected_shape):
                    create()
                    self.assertEqual(killswitch_mode(root), "freeze")
                    with self.assertRaisesRegex(GateError, rf"\[RED\].*shape={expected_shape.split('-', 1)[-1]}"):
                        check_killswitch(root)
                    if marker.is_dir() and not marker.is_symlink():
                        marker.rmdir()
                    else:
                        marker.unlink()
            socket_stat = os.stat_result((stat.S_IFSOCK | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            with mock.patch("growthlane.gates.os.lstat", return_value=socket_stat):
                self.assertEqual(killswitch_mode(root), "freeze")
                with self.assertRaisesRegex(GateError, r"shape=socket"):
                    check_killswitch(root)
            marker.write_text("mode: eject\n", encoding="utf-8")
            self.assertEqual(killswitch_mode(root), "eject")
            marker.write_text("mode: unknown\n", encoding="utf-8")
            self.assertEqual(killswitch_mode(root), "freeze")

    def test_killswitch_regular_to_symlink_swap_never_reads_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "KILLSWITCH"
            target = root / "target.yml"
            marker.write_text("mode: freeze\n", encoding="utf-8")
            target.write_text("mode: eject\n", encoding="utf-8")
            original_open = os.open

            def swap_then_open(path: object, flags: int, *args: object) -> int:
                if Path(path) == marker and not marker.is_symlink():
                    marker.unlink()
                    marker.symlink_to(target)
                return original_open(path, flags, *args)

            with mock.patch("growthlane.gates.os.open", side_effect=swap_then_open):
                self.assertEqual(killswitch_mode(root), "freeze")

    def test_killswitch_symlink_unlinked_during_check_stays_on(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "KILLSWITCH"
            marker.symlink_to(root / "missing-target")

            def unlink_then_report_absent(_home: Path) -> None:
                marker.unlink()
                return None

            with mock.patch(
                "growthlane.gates.killswitch_mode",
                side_effect=unlink_then_report_absent,
            ):
                with self.assertRaisesRegex(
                    GateError,
                    r"\[RED\].*shape=symlink.*disappeared.*mode=freeze",
                ):
                    check_killswitch(root)

    def test_killswitch_initial_shape_survives_absent_final_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "KILLSWITCH"
            marker.symlink_to(root / "missing-target")

            def freeze_then_unlink(_home: Path) -> str:
                marker.unlink()
                return "freeze"

            with mock.patch(
                "growthlane.gates.killswitch_mode",
                side_effect=freeze_then_unlink,
            ):
                with self.assertRaisesRegex(
                    GateError,
                    r"\[RED\].*shape=symlink.*mode=freeze",
                ):
                    check_killswitch(root)

    def test_negative_word_precision_vectors(self) -> None:
        for text in ("それ変だよ", "なんか変", "変な言い方", "変じゃない?"):
            with self.subTest(text=text):
                self.assertTrue(negative_pattern(text))
        for text in ("変数について話そう", "変更しておいて", "大変だね", "変換して"):
            with self.subTest(text=text):
                self.assertFalse(negative_pattern(text))

    def test_threshold_ratchet_and_relaxation_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.yml"

            def write(values: dict[str, object], approval: bool = False) -> None:
                lines = [f"{key}: {values[key]}" for key in DEFAULT_THRESHOLDS]
                if approval:
                    lines += ["relaxation_approval:", "  decided_by: council", "  ref: R12a-42"]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            tightened = dict(DEFAULT_THRESHOLDS, min_count=9, echo_ratio=0.4)
            write(tightened)
            loaded = parse_scalar_yaml(path)
            self.assertEqual(loaded["min_count"], 9)
            self.assertEqual(len(loaded.deviations), 2)
            relaxed = dict(DEFAULT_THRESHOLDS, min_count=7)
            write(relaxed)
            with self.assertRaises(ConfigError):
                parse_scalar_yaml(path)
            write(relaxed, approval=True)
            self.assertIn("approval_ref=R12a-42", parse_scalar_yaml(path).deviations[0])
            for key, value in (("staged_min_days", 13), ("min_uses", 2)):
                with self.subTest(key=key):
                    write(dict(DEFAULT_THRESHOLDS, **{key: value}), approval=True)
                    with self.assertRaises(ConfigError):
                        parse_scalar_yaml(path)

    def test_boolean_integer_thresholds_are_rejected_with_or_without_approval(self) -> None:
        integer_keys = tuple(
            key for key, value in DEFAULT_THRESHOLDS.items() if type(value) is int
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.yml"
            for key in integer_keys:
                for boolean in ("true", "false"):
                    for approval in (False, True):
                        with self.subTest(key=key, boolean=boolean, approval=approval):
                            lines = [f"{name}: {value}" for name, value in DEFAULT_THRESHOLDS.items()]
                            lines[list(DEFAULT_THRESHOLDS).index(key)] = f"{key}: {boolean}"
                            if approval:
                                lines += [
                                    "relaxation_approval:",
                                    "  decided_by: council",
                                    "  ref: R12a-42",
                                ]
                            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                            with self.assertRaisesRegex(
                                ConfigError, f"invalid positive integer threshold: {key}"
                            ):
                                parse_scalar_yaml(path)

    def test_writer_and_reviewer_argv_must_differ(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "growth.json"
            path.write_text(
                json.dumps({"writer_argv": ["model", "same"], "reviewer_argv": ["model", "same"]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "must be distinct"):
                load_json_object(path)

    def test_mirror_liveness_uses_wall_clock_not_run_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            marker = home / "reports" / "weekly" / "latest-alpha.json"
            marker.parent.mkdir(parents=True)
            today = datetime.now(JST).date()
            marker.write_text(json.dumps({"generated_at": today.isoformat()}), encoding="utf-8")
            check_mirror(home, "alpha", "2000-01-01")
            marker.write_text(
                json.dumps({"generated_at": (today + timedelta(days=1)).isoformat()}), encoding="utf-8"
            )
            with self.assertRaises(GateError):
                check_mirror(home, "alpha", "2099-01-01")

    def test_staged_echo_ratio_rechecked_and_null_usage_decays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            run_day = date(2026, 7, 31)
            while not exposed("alpha", "p-0001", run_day.isoformat()):
                run_day -= timedelta(days=1)
            phrase = new_phrase(
                "p-0001",
                "いい感じだね",
                {"first_seen": "2026-06-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.8},
            )
            phrase["state"] = "staged"
            phrase["staged_at"] = (run_day - timedelta(days=20)).isoformat()
            ledger = empty_ledger("alpha")
            ledger["phrases"].append(phrase)
            usage = [
                {
                    "ts": f"{run_day}T09:0{i}:00+09:00",
                    "session": "s",
                    "face": "alpha",
                    "phrase_id": "p-0001",
                    "state": "staged",
                }
                for i in range(3)
            ]
            mention = {
                "ts": f"{run_day}T10:00:00+09:00",
                "host": "h",
                "face": "alpha",
                "session": "s",
                "project": "p",
                "speaker": "owner",
                "text": "その言い方いいね",
                "len": len("その言い方いいね"),
            }
            write_jsonl(obs / f"usage-{run_day}.jsonl", usage)
            write_jsonl(obs / f"{run_day}.jsonl", [mention])
            _, eligible, _ = aggregate(root, "alpha", run_day.isoformat(), ledger, DEFAULT_THRESHOLDS)
            self.assertNotIn("staged->adopted", {item["transition"] for item in eligible})

            adopted = new_phrase(
                "p-0002",
                "ゆっくりいこう",
                {"first_seen": "2025-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
            )
            adopted["state"] = "adopted"
            adopted["staged_at"] = "2025-01-01"
            adopted["history"].append(
                {"at": "2025-01-15", "from": "staged", "to": "adopted", "by": "applier", "proposal_id": "x"}
            )
            decay_ledger = empty_ledger("alpha")
            decay_ledger["phrases"].append(adopted)
            _, decay, _ = aggregate(root, "alpha", "2026-07-31", decay_ledger, DEFAULT_THRESHOLDS)
            self.assertIn("adopted->demoted", {item["transition"] for item in decay})

    def test_soul_roots_and_luca_structure_are_code_constrained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pgl_home = root / "home"
            outside = root / "unrelated.txt"
            outside.write_text("stable", encoding="utf-8")
            (pgl_home / "faces" / "alpha" / ".git").mkdir(parents=True)
            with self.assertRaises(SoulError):
                write_manifest(get_profile("alpha"), pgl_home, {}, [outside])

            repo = root / "luca"
            (repo / ".git").mkdir(parents=True)
            config = {"overlay_home_root": str(repo)}
            with self.assertRaises(SoulError):
                write_manifest(get_profile("luca"), pgl_home, config, [outside])
            with self.assertRaises(ApplyError):
                _require_overlay_home(get_profile("luca"), repo)
            (repo / "persona-engine" / "catalogs" / "overlay").mkdir(parents=True)
            (repo / "growth").mkdir()
            soul = repo / "persona-engine" / "catalogs" / "voice.txt"
            soul.write_text("voice", encoding="utf-8")
            write_manifest(get_profile("luca"), pgl_home, config, [soul])
            mutable = repo / "persona-engine" / "catalogs" / "overlay" / "adopted.txt"
            mutable.write_text("mutable", encoding="utf-8")
            with self.assertRaises(SoulError):
                write_manifest(get_profile("luca"), pgl_home, config, [mutable])

    def test_soul_manifest_pins_overlay_home_and_old_shape_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pgl_home = root / "home"
            repo = root / "luca-a"
            (repo / ".git").mkdir(parents=True)
            (repo / "persona-engine" / "catalogs" / "overlay").mkdir(parents=True)
            (repo / "growth").mkdir()
            soul = repo / "persona-engine" / "manifest.yml"
            soul.write_text("soul\n", encoding="utf-8")
            config = {"overlay_home_root": str(repo)}
            manifest = write_manifest(get_profile("luca"), pgl_home, config, [soul])
            value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(value["overlay_home"], str(repo.resolve()))
            other = root / "luca-b"
            with self.assertRaisesRegex(SoulError, "overlay home mismatch"):
                verify_manifest(get_profile("luca"), pgl_home, {"overlay_home_root": str(other)})
            manifest.write_text(json.dumps(value["files"]), encoding="utf-8")
            with self.assertRaisesRegex(SoulError, "re-run bin/pgl-baseline"):
                verify_manifest(get_profile("luca"), pgl_home, config)

    def test_demoted_text_reharvests_with_fresh_id_but_blocked_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            transcripts = root / "transcripts"
            transcripts.mkdir()
            (transcripts / "session.jsonl").write_bytes(b"")
            blocklist = root / "blocklist.txt"
            blocklist.write_text("", encoding="utf-8")
            ledger = empty_ledger("alpha")
            for phrase_id, text, state in (
                ("p-0001", "またやろうね", "demoted"),
                ("p-0002", "二度と言わない", "blocked"),
            ):
                phrase = new_phrase(
                    phrase_id,
                    text,
                    {"first_seen": "2026-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
                )
                phrase["state"] = state
                ledger["phrases"].append(phrase)
            end = date(2026, 8, 2)
            for offset in range(5):
                day = end - timedelta(days=offset)
                rows = []
                for text in ("またやろうね", "二度と言わない"):
                    for occurrence in range(2):
                        rows.append({"ts": f"{day}T1{occurrence}:00:00+09:00", "host": "h", "face": "alpha", "session": f"s-{offset}-{occurrence}", "project": "p", "speaker": "owner", "text": text, "len": len(text)})
                write_jsonl(obs / f"{day}.jsonl", rows)
            proposals = harvest(
                root, "alpha", end.isoformat(),
                {"speaker": "owner", "transcripts_root": str(transcripts)},
                ledger, DEFAULT_THRESHOLDS, blocklist, [transcripts / "session.jsonl"],
            )
            self.assertEqual([item["text"] for item in proposals], ["またやろうね"])
            self.assertEqual(proposals[0]["phrase_id"], "p-0003")
            self.assertEqual(proposals[0]["predecessor_id"], "p-0001")

    def test_candidacy_echo_uses_raw_assistant_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            transcripts = root / "transcripts"
            transcripts.mkdir()
            blocklist = root / "blocklist.txt"
            blocklist.write_text("", encoding="utf-8")
            end = date(2026, 8, 2)
            transcript_rows = []
            for offset in range(5):
                day = end - timedelta(days=offset)
                text = "たしかにそうだね"
                rows = [
                    {"ts": f"{day}T10:0{i}:00+09:00", "host": "h", "face": "alpha", "session": f"s-{offset}-{i}", "project": "p", "speaker": "owner", "text": text, "len": len(text)}
                    for i in range(2)
                ]
                write_jsonl(obs / f"{day}.jsonl", rows)
                if offset < 3:
                    transcript_rows.append({"type": "assistant", "timestamp": f"{day}T01:00:00Z", "sessionId": f"a-{offset}", "message": {"content": [{"type": "text", "text": "前後 たしかにそうだね 文脈"}]}})
            write_jsonl(transcripts / "session.jsonl", transcript_rows)
            config = {"speaker": "owner", "transcripts_root": str(transcripts)}
            proposals = harvest(
                root,
                "alpha",
                end.isoformat(),
                config,
                empty_ledger("alpha"),
                DEFAULT_THRESHOLDS,
                blocklist,
                [transcripts / "session.jsonl"],
            )
            self.assertEqual(proposals[0]["source"]["echo_ratio"], 0.6)

    def test_storage_round_trips_accepted_sequences_through_ledger_and_render(self) -> None:
        vectors = ("👨‍👩‍👧‍👦", "🏳️‍🌈", "❤️", "1️⃣", "👍🏽", "nice work")
        for text in vectors:
            with self.subTest(text=text):
                stored = canonicalize_for_storage(text)
                ledger = empty_ledger("alpha")
                phrase = new_phrase(
                    "p-0001",
                    stored,
                    {
                        "first_seen": "2026-08-02",
                        "window_count": 8,
                        "distinct_days": 5,
                        "echo_ratio": 0.0,
                    },
                )
                phrase["state"] = "adopted"
                phrase["staged_at"] = "2026-08-02"
                ledger["phrases"].append(phrase)
                with tempfile.TemporaryDirectory() as temporary:
                    ledger_path = Path(temporary) / "overlay-ledger.yml"
                    ledger_path.write_bytes(dump_ledger(ledger))
                    loaded = load_ledger(ledger_path, "alpha")
                    rendered = render_files(
                        get_profile("alpha"), loaded, "2026-08-02", "Tester"
                    )["overlay.md"]
                self.assertEqual(loaded["phrases"][0]["text"].encode(), text.encode())
                self.assertIn(text.encode(), rendered)

    def test_harvest_groups_storage_variants_by_matching_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            transcripts = root / "transcripts"
            transcripts.mkdir()
            transcript = transcripts / "session.jsonl"
            transcript.write_bytes(b"")
            blocklist = root / "blocklist.txt"
            blocklist.write_bytes(b"")
            end = date(2026, 8, 2)
            clean = "そうだね"
            twin = "そうた\u0378\u3099ね"
            for offset, count in enumerate((2, 2, 2, 1, 1)):
                day = end - timedelta(days=4 - offset)
                write_jsonl(
                    obs / f"{day}.jsonl",
                    [
                        {
                            "ts": f"{day}T1{index}:00:00+09:00",
                            "host": "h",
                            "face": "alpha",
                            "session": f"s-{offset}-{index}",
                            "project": "p",
                            "speaker": "owner",
                            "text": clean if offset == 0 and index == 0 else twin,
                            "len": len(clean if offset == 0 and index == 0 else twin),
                        }
                        for index in range(count)
                    ],
                )
            proposals = harvest(
                root,
                "alpha",
                end.isoformat(),
                {"speaker": "owner", "transcripts_root": str(transcripts)},
                empty_ledger("alpha"),
                DEFAULT_THRESHOLDS,
                blocklist,
                [transcript],
            )
            representative = canonicalize_for_storage(twin)
            self.assertEqual([item["text"] for item in proposals], [representative])
            self.assertEqual(proposals[0]["source"]["window_count"], 8)
            self.assertEqual(proposals[0]["source"]["distinct_days"], 5)
            ledger = empty_ledger("alpha")
            phrase = new_phrase("p-0001", proposals[0]["text"], proposals[0]["source"])
            phrase["state"] = "adopted"
            phrase["staged_at"] = end.isoformat()
            ledger["phrases"].append(phrase)
            rendered = render_files(
                get_profile("alpha"), ledger, end.isoformat(), "Tester"
            )["overlay.md"].decode("utf-8")
            self.assertIn(representative, rendered)
            self.assertIn("privilege_vocab", lint_phrase("su do"))

    def test_harvest_representative_ties_break_lexicographically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            transcripts = root / "transcripts"
            transcripts.mkdir()
            transcript = transcripts / "session.jsonl"
            transcript.write_bytes(b"")
            blocklist = root / "blocklist.txt"
            blocklist.write_bytes(b"")
            day = date(2026, 8, 2)
            variants = ("ｎｉｃｅ work", "nice work")
            for offset in range(5):
                current = day - timedelta(days=offset)
                rows = []
                for index, text in enumerate(variants):
                    rows.append({
                        "ts": f"{current}T1{index}:00:00+09:00",
                        "host": "h",
                        "face": "alpha",
                        "session": f"s-{offset}-{index}",
                        "project": "p",
                        "speaker": "owner",
                        "text": text,
                        "len": len(text),
                    })
                write_jsonl(obs / f"{current}.jsonl", rows)
            proposals = harvest(
                root,
                "alpha",
                day.isoformat(),
                {"speaker": "owner", "transcripts_root": str(transcripts)},
                empty_ledger("alpha"),
                DEFAULT_THRESHOLDS,
                blocklist,
                [transcript],
            )
            self.assertEqual(proposals[0]["text"], min(variants))

    def test_harvest_applies_length_to_storage_in_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            transcripts = root / "transcripts"
            transcripts.mkdir()
            transcript = transcripts / "session.jsonl"
            transcript.write_bytes(b"")
            blocklist = root / "blocklist.txt"
            blocklist.write_bytes(b"")
            expands_to_48 = "\u0344" * 24
            shrinks_to_24 = "A\u030a" * 4 + "a" * 20
            day = date(2026, 8, 2)
            for offset, count in enumerate((2, 2, 2, 1, 1)):
                current = day - timedelta(days=4 - offset)
                rows = []
                for text in (expands_to_48, shrinks_to_24):
                    for index in range(count):
                        rows.append({
                            "ts": f"{current}T{10 + index}:00:00+09:00",
                            "host": "h",
                            "face": "alpha",
                            "session": f"{len(text)}-{offset}-{index}",
                            "project": "p",
                            "speaker": "owner",
                            "text": text,
                            "len": len(text),
                        })
                write_jsonl(obs / f"{current}.jsonl", rows)
            proposals = harvest(
                root,
                "alpha",
                day.isoformat(),
                {"speaker": "owner", "transcripts_root": str(transcripts)},
                empty_ledger("alpha"),
                DEFAULT_THRESHOLDS,
                blocklist,
                [transcript],
            )
            self.assertEqual(
                [item["text"] for item in proposals],
                [canonicalize_for_storage(shrinks_to_24)],
            )

    def test_distill_identical_adds_durable_blocklist_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "obslog" / "alpha").mkdir(parents=True)
            ledger = empty_ledger("alpha")
            for phrase_id, text in (("p-0001", "そうだね"), ("p-0002", "そ\u034fうだね")):
                phrase = new_phrase(
                    phrase_id,
                    text,
                    {"first_seen": "2026-08-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
                )
                ledger["phrases"].append(phrase)
            blocklist: list[str] = []
            aggregated, _, _ = aggregate(
                root,
                "alpha",
                "2026-08-02",
                ledger,
                DEFAULT_THRESHOLDS,
                blocklist=blocklist,
            )
            self.assertEqual([item["id"] for item in aggregated["phrases"]], ["p-0001"])
            self.assertEqual(blocklist, [canonicalize_for_storage("そ\u034fうだね")])

    def test_matching_bmp_fast_path_does_not_call_unicode_category_per_character(self) -> None:
        text = ("ordinary assistant text なるほどですね。" * 80)[:1720]
        with mock.patch(
            "growthlane.guard.unicodedata.category",
            side_effect=AssertionError("per-character category lookup entered"),
        ):
            views = matching_views(text)
        self.assertTrue(views)

    def test_harvest_none_transcript_paths_raise(self) -> None:
        with self.assertRaisesRegex(HarvestError, "transcript_paths must not be None"):
            harvest(
                Path("."),
                "alpha",
                "2026-08-02",
                {"speaker": "owner"},
                empty_ledger("alpha"),
                DEFAULT_THRESHOLDS,
                Path("missing-blocklist"),
                None,
            )

    def test_transcript_input_outages_are_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing"
            self.assertIn("missing", transcript_inputs({"transcripts_root": str(missing)})[1])
            empty = root / "empty"
            empty.mkdir()
            self.assertIn("zero readable transcript files", transcript_inputs({"transcripts_root": str(empty)})[1])
            with mock.patch("harvester.harvest.os.scandir", side_effect=PermissionError("denied")):
                self.assertIn("unreadable", transcript_inputs({"transcripts_root": str(empty)})[1])

    def test_transcript_inputs_skip_unreadable_and_symlinked_files_with_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "good.jsonl"
            unreadable = root / "unreadable.jsonl"
            linked = root / "linked.jsonl"
            good.write_bytes(b"")
            unreadable.write_bytes(b"")
            linked.symlink_to(good)
            reports: list[str] = []
            paths, reason = transcript_inputs(
                {"transcripts_root": str(root)}, reports.append
            )
            self.assertIsNone(reason)
            self.assertEqual(paths, sorted((good, linked, unreadable)))
            self.assertEqual(reports, [])

            original_open = os.open
            opens: list[Path] = []

            def selective_open(path: object, flags: int, *args: object) -> int:
                resolved = Path(path)
                opens.append(resolved)
                if resolved == unreadable:
                    raise PermissionError("denied")
                return original_open(path, flags, *args)

            with mock.patch(
                "collectors.claude_code.usage_log.os.open", side_effect=selective_open
            ):
                assistant_echo_days(
                    paths,
                    date(2026, 8, 1),
                    date(2026, 8, 2),
                    ["なるほどだね"],
                    reports.append,
                )
            self.assertEqual(opens.count(good), 1)
            self.assertEqual(opens.count(linked), 1)
            self.assertEqual(opens.count(unreadable), 1)
            self.assertTrue(any(str(unreadable) in report for report in reports), reports)
            self.assertTrue(any("symlinked file" in report and str(linked) in report for report in reports), reports)

    def test_echo_scan_skips_one_unreadable_file_but_requires_one_readable_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            readable = root / "readable.jsonl"
            unreadable = root / "unreadable.jsonl"
            readable.write_bytes(b"")
            unreadable.write_bytes(b"")
            reports: list[str] = []
            original_open = os.open

            def selective_open(path: object, flags: int, *args: object) -> int:
                if Path(path) == unreadable:
                    raise PermissionError("denied")
                return original_open(path, flags, *args)

            with mock.patch(
                "collectors.claude_code.usage_log.os.open", side_effect=selective_open
            ):
                matches = assistant_echo_days(
                    [unreadable, readable],
                    date(2026, 8, 1),
                    date(2026, 8, 2),
                    ["なるほどだね"],
                    reports.append,
                )
                with self.assertRaisesRegex(OSError, "zero transcript files were readable"):
                    assistant_echo_days(
                        [unreadable],
                        date(2026, 8, 1),
                        date(2026, 8, 2),
                        ["なるほどだね"],
                        reports.append,
                    )
            self.assertEqual(matches, {"なるほどだね": set()})
            self.assertTrue(any(str(unreadable) in report for report in reports), reports)

    def test_holdout_deviation_reclassifies_all_six_counters_by_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            day = date(2026, 8, 2)
            while exposed("alpha", "p-0001", day.isoformat()):
                day -= timedelta(days=1)
            phrase = new_phrase("p-0001", "いい感じだね", {"first_seen": str(day), "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0})
            phrase["state"] = "staged"
            phrase["staged_at"] = str(day)
            ledger = empty_ledger("alpha")
            ledger["phrases"].append(phrase)
            write_jsonl(obs / f"usage-{day}.jsonl", [{"ts": f"{day}T09:00:00+09:00", "session": "deviation", "face": "alpha", "phrase_id": "p-0001", "state": "staged"}])
            text = "いい感じだねはやめて"
            write_jsonl(obs / f"{day}.jsonl", [{"ts": f"{day}T10:00:00+09:00", "host": "h", "face": "alpha", "session": "other", "project": "p", "speaker": "owner", "text": text, "len": len(text)}])
            aggregated, _, deviations = aggregate(root, "alpha", day.isoformat(), ledger, DEFAULT_THRESHOLDS)
            self.assertEqual(deviations, 1)
            self.assertEqual(aggregated["phrases"][0]["holdout"], {"exposed_days": 1, "holdout_days": 0, "exposed_neg": 1, "holdout_neg": 0, "exposed_mentions": 0, "holdout_mentions": 0})

    def test_classifier_seam_validates_json_and_failure_is_closed(self) -> None:
        valid_code = (
            "import json,sys; p=json.load(sys.stdin); "
            "json.dump({'results':[{'index':i,'negative':False,'mention':None} "
            "for i,_ in enumerate(p['observations'])]},sys.stdout)"
        )
        decisions = classify(
            [sys.executable, "-c", valid_code],
            {"observations": [{"text": "x"}]},
            1,
        )
        self.assertIsNone(decisions[0]["mention"])
        with self.assertRaises(AdapterError):
            classify([sys.executable, "-c", "print('{}')"], {"observations": []}, 0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obs = root / "obslog" / "alpha"
            obs.mkdir(parents=True)
            phrase = new_phrase(
                "p-0001",
                "いい感じだね",
                {"first_seen": "2026-08-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
            )
            phrase["state"] = "staged"
            phrase["staged_at"] = "2026-08-01"
            ledger = empty_ledger("alpha")
            ledger["phrases"].append(phrase)
            text = "分類が必要な反応"
            write_jsonl(
                obs / "2026-08-01.jsonl",
                [{"ts": "2026-08-01T10:00:00+09:00", "host": "h", "face": "alpha", "session": "s", "project": "p", "speaker": "owner", "text": text, "len": len(text)}],
            )
            with self.assertRaisesRegex(AggregateError, "classifier failed closed"):
                aggregate(
                    root,
                    "alpha",
                    "2026-08-01",
                    ledger,
                    DEFAULT_THRESHOLDS,
                    [sys.executable, "-c", "raise SystemExit(2)"],
                )

    def test_old_snapshot_shape_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.yml"
            ledger = empty_ledger("alpha")
            ledger["snapshots"].append(
                {"at": "2026-01-01", "source_sha": "a" * 40, "content_hash": "b" * 64}
            )
            path.write_bytes(dump_ledger(ledger))
            self.assertEqual(load_ledger(path, "alpha")["snapshots"][0]["source_sha"], "a" * 40)

    def test_safety_killswitch_exception_is_narrow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "KILLSWITCH").write_text("mode: freeze\n", encoding="utf-8")
            check_killswitch(home, exception="safety")
            with self.assertRaises(GateError):
                check_killswitch(home)

    def test_safety_exception_records_contract_v11_section_10_rationale(self) -> None:
        source = inspect.getsource(check_killswitch)
        self.assertIn("Contract v1.1 §10 already records the manual deletion exception", source)
        self.assertIn("immediate block/forget despite the normal killswitch freeze", source)


if __name__ == "__main__":
    unittest.main()
