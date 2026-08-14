from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict
from unittest import mock

from growthlane.gates import check_mirror
from growthlane.ledger import empty_ledger, new_phrase
from growthlane.notify import Digest
from growthlane.ucd_runtime import UcdRuntimeStatus, runtime_status
from mirror.common import MirrorError
from mirror.weekly import TIER_S_KEYS, _collector_liveness, _write_tier_s, run_weekly

from test_mirror_support import MirrorHarness, day_with_exposure


JST = timezone(timedelta(hours=9))


def _jst_today() -> date:
    return datetime.now(JST).date()


def _phrase(phrase_id: str, text: str, first_seen: date) -> Dict[str, object]:
    return new_phrase(
        phrase_id,
        text,
        {"first_seen": first_seen.isoformat(), "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
    )


def _write_collector_last_run(
    home: Path,
    *,
    face: str = "alpha",
    run_at: datetime | None = None,
    bucket_date: str | None = None,
    usage_enabled: bool = False,
    errors: int = 0,
    sources_scanned: int = 0,
    records_written: int = 0,
    ucd: str | None = None,
) -> Path:
    run_at = run_at or datetime.now(JST).replace(microsecond=0)
    payload = {
        "schema_version": 1,
        "face": face,
        "run_at": run_at.isoformat(),
        "date": bucket_date or run_at.astimezone(JST).date().isoformat(),
        "sources_scanned": sources_scanned,
        "records_written": records_written,
        "usage_enabled": usage_enabled,
        "errors": errors,
        "ucd": ucd or runtime_status().runtime_version,
    }
    path = home / "state" / "collector" / f"{face}.last-run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def _write_healthy_collector_last_run(home: Path, run_day: date, *, usage_enabled: bool = False) -> Path:
    return _write_collector_last_run(
        home,
        run_at=datetime.combine(run_day, time(12, 0), tzinfo=JST),
        bucket_date=run_day.isoformat(),
        usage_enabled=usage_enabled,
    )


class WeeklyMirrorTests(MirrorHarness):
    def test_weekly_rejects_marker_backfill_but_repairs_corrupt_marker(self) -> None:
        today = _jst_today()
        newer = today - timedelta(days=1)
        older = today - timedelta(days=2)
        self.write_soul_manifest()
        _write_collector_last_run(self.home, usage_enabled=False)
        self.write_usage(newer, [])
        self.run_module("mirror.weekly", newer)
        marker_path = self.home / "reports" / "weekly" / "latest-alpha.json"
        original = marker_path.read_bytes()

        self.write_usage(older, [])
        rejected = self.run_module("mirror.weekly", older, check=False)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("marker generated_at regression", rejected.stdout)
        self.assertEqual(marker_path.read_bytes(), original)

        marker_path.write_text("{broken", encoding="utf-8")
        repaired = self.run_module("mirror.weekly", older)
        self.assertEqual(repaired.returncode, 0)
        self.assertEqual(
            json.loads(marker_path.read_text(encoding="utf-8"))["generated_at"],
            older.isoformat(),
        )

        same_date = self.run_module("mirror.weekly", older)
        self.assertEqual(same_date.returncode, 0)
        self.assertEqual(
            json.loads(marker_path.read_text(encoding="utf-8"))["generated_at"],
            older.isoformat(),
        )

    def test_weekly_replaces_future_marker_and_reports_red(self) -> None:
        run_day = _jst_today()
        future_day = run_day + timedelta(days=30)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        marker_path = self.home / "reports" / "weekly" / "latest-alpha.json"
        marker_path.parent.mkdir(parents=True)
        marker_path.write_text(
            json.dumps({"generated_at": future_day.isoformat(), "report": "future"}),
            encoding="utf-8",
        )

        completed = self.run_module("mirror.weekly", run_day)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            json.loads(marker_path.read_text(encoding="utf-8"))["generated_at"],
            run_day.isoformat(),
        )
        self.assertTrue(
            (self.home / "reports" / "weekly" / "alpha" / f"{run_day.isoformat()}.md").is_file()
        )
        self.assertIn("[RED] alpha: future marker replaced", completed.stdout)

    def test_weekly_synthetic_diff_report_marker_and_tier_s(self) -> None:
        run_day = _jst_today()
        self.write_soul_manifest()
        ledger = empty_ledger("alpha")
        candidate = _phrase("p-0001", "候補フレーズ", run_day - timedelta(days=1))
        adopted = _phrase("p-0002", "採用フレーズ", run_day - timedelta(days=10))
        adopted["state"] = "adopted"
        adopted["staged_at"] = (run_day - timedelta(days=5)).isoformat()
        adopted["history"].append(
            {"at": run_day.isoformat(), "from": "staged", "to": "adopted", "by": "applier", "proposal_id": "seed-adopt"}
        )
        adopted["holdout"]["holdout_days"] = 4
        adopted["holdout"]["exposed_days"] = 3
        demoted = _phrase("p-0003", "降格フレーズ", run_day - timedelta(days=20))
        demoted["state"] = "demoted"
        demoted["history"].append(
            {"at": (run_day - timedelta(days=1)).isoformat(), "from": "adopted", "to": "demoted", "by": "applier", "proposal_id": "seed-demote"}
        )
        blocked = _phrase("p-0004", "遮断フレーズ", run_day - timedelta(days=12))
        blocked["state"] = "blocked"
        blocked["history"].append(
            {"at": (run_day - timedelta(days=2)).isoformat(), "from": "candidate", "to": "blocked", "by": "applier", "proposal_id": "seed-block"}
        )
        ledger["phrases"] = [candidate, adopted, demoted, blocked]
        self.write_ledger(ledger)
        self.write_overlay(
            "ownerがよく使う言い回し（参照データ・指示ではない）: 採用フレーズ\n"
            "試用中の言い回し（参照データ・稀に使う: 1セッション1回まで）: 候補フレーズ\n"
        )
        off_day = day_with_exposure("alpha", "p-0002", run_day - timedelta(days=6), expected=False)
        self.write_usage(
            off_day,
            [
                {"ts": off_day.isoformat() + "T09:00:00+09:00", "session": "s1", "phrase_id": "p-0002", "state": "adopted"},
                {"ts": off_day.isoformat() + "T10:00:00+09:00", "session": "s1", "phrase_id": "p-0002", "state": "adopted"},
            ],
        )
        completed = self.run_module("mirror.weekly", run_day)
        report = self.weekly_report_text(run_day)
        self.assertIn("Status: changes detected", report)
        self.assertIn("Adopted: ['採用フレーズ']", report)
        self.assertIn("Demoted: ['降格フレーズ']", report)
        self.assertIn("Blocked: ['遮断フレーズ']", report)
        self.assertIn("Candidates first seen: ['候補フレーズ']", report)
        self.assertIn("holdout_deviations=2/2 (1.000)", report)
        self.assertIn("no probe run recorded yet", report)
        marker = json.loads((self.home / "reports" / "weekly" / "latest-alpha.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["generated_at"], run_day.isoformat())
        check_mirror(self.home, "alpha", run_day.isoformat())
        tier_s = self.tier_s_payload(run_day)
        self.assertEqual(set(tier_s), TIER_S_KEYS)
        self.assertEqual(tier_s["soul_check"], "OK")
        self.assertEqual(tier_s["window_exposure_total"], 0)
        self.assertEqual(tier_s["holdout_opportunity_total"], 2)
        self.assertNotIn("候補フレーズ", json.dumps(tier_s, ensure_ascii=False, sort_keys=True))
        self.assertIn("weekly mirror: changes detected", completed.stdout)
        self.assertIn("tier-s staged locally (vault emission awaits CP-4)", completed.stdout)

    def test_weekly_zero_change_and_sla_gap_are_explicit(self) -> None:
        first_day = date(2026, 7, 25)
        second_day = date(2026, 8, 3)
        self.write_soul_manifest()
        _write_healthy_collector_last_run(self.home, first_day, usage_enabled=False)
        self.run_module("mirror.weekly", first_day)
        first_report = self.weekly_report_text(first_day)
        self.assertIn("\nno changes\n", first_report)
        _write_healthy_collector_last_run(self.home, second_day, usage_enabled=False)
        completed = self.run_module("mirror.weekly", second_day)
        second_report = self.weekly_report_text(second_day)
        self.assertIn("\nno changes\n", second_report)
        self.assertIn("[WARN] alpha: weekly SLA gap 9d", completed.stdout)
        self.assertIn("weekly mirror: no changes", completed.stdout)

    def test_weekly_soul_mismatch_and_symlink_manifest_still_publish_marker(self) -> None:
        run_day = _jst_today() - timedelta(days=1)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        self.soul_source.write_text('{"fixture":"mutated"}\n', encoding="utf-8")
        mismatch = self.run_module("mirror.weekly", run_day)
        self.assertIn("mirror soul check failed", mismatch.stdout)
        self.assertIn("Soul check: MISMATCH", self.weekly_report_text(run_day))
        self.assertTrue((self.home / "reports" / "weekly" / "latest-alpha.json").is_file())
        check_mirror(self.home, "alpha", run_day.isoformat())
        self.assertEqual(self.tier_s_payload(run_day)["soul_check"], "MISMATCH")

        later_day = run_day + timedelta(days=1)
        self.symlink_soul_manifest()
        linked = self.run_module("mirror.weekly", later_day)
        self.assertIn("symlinked baseline manifest rejected", linked.stdout)
        self.assertIn("Soul check: MISMATCH", self.weekly_report_text(later_day))
        check_mirror(self.home, "alpha", later_day.isoformat())

    def test_weekly_tier_s_preserves_no_baseline(self) -> None:
        run_day = _jst_today()
        self.write_usage(run_day, [])

        completed = self.run_module("mirror.weekly", run_day)

        self.assertIn("mirror soul check has no baseline", completed.stdout)
        self.assertEqual(self.tier_s_payload(run_day)["soul_check"], "NO-BASELINE")

    def test_weekly_killswitch_tracking_and_detection_only_posture(self) -> None:
        first_day = date(2026, 8, 1)
        second_day = date(2026, 8, 2)
        third_day = date(2026, 8, 3)
        self.write_soul_manifest()
        marker = self.write_killswitch("set_by: owner\nset_at: 2026-08-01\nreason: fixture\nmode: freeze\n")
        self.run_module("mirror.weekly", first_day)
        marker.write_text("set_by: owner\nset_at: 2026-08-02\nreason: changed\nmode: freeze\n", encoding="utf-8")
        self.run_module("mirror.weekly", second_day)
        second_report = self.weekly_report_text(second_day)
        self.assertIn("Change tracking: changed ON->ON mode=freeze", second_report)
        marker.unlink()
        self.run_module("mirror.weekly", third_day)
        third_report = self.weekly_report_text(third_day)
        self.assertIn("Change tracking: changed ON->OFF mode=None", third_report)
        self.assertTrue((self.home / "reports" / "weekly" / "latest-alpha.json").is_file())

    def test_weekly_killswitch_tracker_ignores_mtime_only_changes(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        marker = self.write_killswitch(
            "set_by: owner\nset_at: 2026-08-03\nreason: fixture\nmode: freeze\n"
        )
        self.run_module("mirror.weekly", run_day)

        original = marker.read_bytes()
        os.utime(marker, ns=(marker.stat().st_atime_ns, marker.stat().st_mtime_ns + 1))
        self.assertEqual(marker.read_bytes(), original)
        self.run_module("mirror.weekly", run_day)

        self.assertIn(
            "Change tracking: unchanged", self.weekly_report_text(run_day)
        )

    def test_weekly_killswitch_symlink_retarget_is_red_and_not_unchanged(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        first_target = self.root / "killswitch-first.yml"
        second_target = self.root / "killswitch-second.yml"
        first_target.write_text("mode: freeze\n", encoding="utf-8")
        second_target.write_text("mode: eject\n", encoding="utf-8")
        marker = self.home / "KILLSWITCH"
        marker.symlink_to(first_target)
        initial = self.run_module("mirror.weekly", run_day)
        self.assertIn("[RED] alpha: killswitch transition not verified:", initial.stdout)
        self.assertIn("Status: transition not verified", self.weekly_report_text(run_day))

        marker.unlink()
        marker.symlink_to(second_target)
        completed = self.run_module("mirror.weekly", run_day)

        report = self.weekly_report_text(run_day)
        self.assertIn("Status: transition not verified", report)
        self.assertNotIn("Change tracking: unchanged", report)
        self.assertIn("transition not verified", report)
        self.assertIn("[RED] alpha: killswitch transition not verified:", completed.stdout)

    def test_weekly_current_unreadable_killswitch_identity_is_red_and_not_unchanged(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        marker = self.write_killswitch(
            "set_by: owner\nset_at: 2026-08-03\nreason: fixture\nmode: freeze\n"
        )
        self.run_module("mirror.weekly", run_day)
        current = {
            "exists": True,
            "mtime": marker.stat().st_mtime_ns,
            "sha256": None,
            "state": "ON",
            "mode": "freeze",
        }

        with mock.patch("mirror.weekly._killswitch", return_value=current):
            run_weekly(
                "alpha",
                run_day.isoformat(),
                self.config_dir,
                self.home,
                Digest(self.home, run_day.isoformat()),
            )

        report = self.weekly_report_text(run_day)
        self.assertIn("Status: transition not verified", report)
        self.assertNotIn("Change tracking: unchanged", report)
        self.assertIn("transition not verified", report)
        self.assertIn(
            "[RED] alpha: killswitch transition not verified:",
            self.digest_text(run_day),
        )

    def test_weekly_previous_unverifiable_killswitch_identity_is_red(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        tracker = self.home / "mirror" / "state" / "killswitch-track.json"
        tracker.parent.mkdir(parents=True)
        tracker.write_text(
            json.dumps({"exists": True, "mtime": 1, "sha256": None}) + "\n",
            encoding="utf-8",
        )

        completed = self.run_module("mirror.weekly", run_day)

        report = self.weekly_report_text(run_day)
        self.assertIn("Status: transition not verified", report)
        self.assertIn("previous killswitch identity unavailable", report)
        self.assertNotIn("Change tracking: unchanged", report)
        self.assertIn("[RED] alpha: killswitch transition not verified:", completed.stdout)

    def test_weekly_corrupt_killswitch_tracker_is_red_and_self_heals(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        tracker = self.home / "mirror" / "state" / "killswitch-track.json"
        tracker.parent.mkdir(parents=True)
        tracker.write_text("{broken", encoding="utf-8")

        completed = self.run_module("mirror.weekly", run_day)

        report = self.weekly_report_text(run_day)
        self.assertIn("Status: transition not verified", report)
        self.assertIn(
            "Change tracking: UNAVAILABLE: previous tracker unreadable; "
            "current state OFF mode=None; transition not verified",
            report,
        )
        self.assertIn("[RED] alpha: killswitch tracker unavailable:", completed.stdout)
        self.assertEqual(
            json.loads(tracker.read_text(encoding="utf-8")),
            {"exists": False, "mtime": None, "sha256": None},
        )

    def test_weekly_killswitch_tracker_commits_only_after_marker_publication(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        self.write_killswitch(
            "set_by: owner\nset_at: 2026-08-03\nreason: fixture\nmode: freeze\n"
        )
        tracker = self.home / "mirror" / "state" / "killswitch-track.json"
        tracker.parent.mkdir(parents=True)
        prior = {"exists": False, "mtime": None, "sha256": None}
        tracker.write_text(json.dumps(prior) + "\n", encoding="utf-8")
        digest = Digest(self.home, run_day.isoformat())

        with mock.patch.dict(os.environ, self.env, clear=False):
            with mock.patch(
                "mirror.weekly.atomic_monotonic_date_json",
                side_effect=OSError("forced marker publication failure"),
            ):
                with self.assertRaisesRegex(
                    OSError, "forced marker publication failure"
                ):
                    run_weekly(
                        "alpha",
                        run_day.isoformat(),
                        self.config_dir,
                        self.home,
                        digest,
                    )

        self.assertEqual(json.loads(tracker.read_text(encoding="utf-8")), prior)
        self.run_module("mirror.weekly", run_day)
        self.assertIn(
            "Change tracking: changed OFF->ON mode=freeze",
            self.weekly_report_text(run_day),
        )

    def test_weekly_data_failures_are_loud_but_publish(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        (self.overlay / "overlay-ledger.yml").write_text("schema_version: [broken\n", encoding="utf-8")
        self.write_usage(
            run_day,
            [
                {"ts": run_day.isoformat() + "T09:00:00+09:00", "session": "s1", "state": "adopted"},
            ],
        )
        completed = self.run_module("mirror.weekly", run_day, check=False)
        self.assertEqual(completed.returncode, 0)
        report = self.weekly_report_text(run_day)
        self.assertIn("Ledger: UNAVAILABLE:", report)
        self.assertIn("## Degraded inputs", report)
        self.assertIn("UNAVAILABLE: usage unavailable:", report)
        self.assertTrue((self.home / "reports" / "weekly" / "latest-alpha.json").is_file())
        self.assertIn("[RED] alpha: usage discipline unavailable", completed.stdout)
        self.assertFalse(
            (self.home / "reports" / "tier-s" / "alpha" / f"{run_day.isoformat()}.json").exists()
        )

    def test_weekly_usage_counts_only_current_window_and_missing_is_unavailable(self) -> None:
        run_day = date(2026, 8, 3)
        start = run_day - timedelta(days=6)
        self.write_soul_manifest()
        exposed_day = day_with_exposure("alpha", "p-0001", start, expected=True)
        holdout_day = day_with_exposure(
            "alpha", "p-0002", start + timedelta(days=1), expected=False
        )
        outside = start - timedelta(days=1)
        self.write_usage(
            exposed_day,
            [{"ts": exposed_day.isoformat() + "T09:00:00+09:00", "session": "inside-exposed", "phrase_id": "p-0001", "state": "staged"}],
        )
        self.write_usage(
            holdout_day,
            [{"ts": holdout_day.isoformat() + "T09:00:00+09:00", "session": "inside-holdout", "phrase_id": "p-0002", "state": "adopted"}],
        )
        self.write_usage(
            outside,
            [{"ts": outside.isoformat() + "T09:00:00+09:00", "session": "outside", "phrase_id": "p-0003", "state": "adopted"}],
        )

        self.run_module("mirror.weekly", run_day)
        tier_s = self.tier_s_payload(run_day)
        self.assertEqual(tier_s["window_exposure_total"], 1)
        self.assertEqual(tier_s["holdout_opportunity_total"], 1)

        missing_day = run_day + timedelta(days=1)
        obslog = self.home / "obslog" / "alpha"
        for path in obslog.glob("usage-*.jsonl"):
            path.unlink()
        _write_collector_last_run(
            self.home,
            run_at=datetime.combine(missing_day, time(23, 45), tzinfo=JST),
            bucket_date=missing_day.isoformat(),
            usage_enabled=True,
        )
        completed = self.run_module("mirror.weekly", missing_day)
        report = self.weekly_report_text(missing_day)
        self.assertIn(
            "BROKEN: collector last-run marker healthy but usage files missing while collector marker says usage collection was enabled",
            report,
        )
        self.assertIn("[RED] alpha: usage discipline unavailable", completed.stdout)
        self.assertFalse(
            (self.home / "reports" / "tier-s" / "alpha" / f"{missing_day.isoformat()}.json").exists()
        )

    def test_weekly_failed_injected_read_does_not_anchor_or_fake_no_change(self) -> None:
        run_day = date(2026, 8, 3)
        previous_day = run_day - timedelta(days=7)
        self.seed_snapshot(previous_day, {"overlay.md": b"last known good\n"})
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        digest = Digest(self.home, run_day.isoformat())

        with mock.patch("mirror.weekly.injected_bytes", side_effect=OSError("read failed")):
            self.assertEqual(
                run_weekly("alpha", run_day.isoformat(), self.config_dir, self.home, digest),
                0,
            )
        digest.ensure_line()
        report = self.weekly_report_text(run_day)
        self.assertIn("Status: UNAVAILABLE", report)
        self.assertIn("Injected-byte drift: UNAVAILABLE", report)
        self.assertNotIn("\nno changes\n", report)
        snapshot_days = sorted(
            path.name for path in (self.home / "mirror" / "snapshots" / "alpha").iterdir()
        )
        self.assertEqual(snapshot_days, [previous_day.isoformat()])

        self.write_overlay("changed after recovery\n")
        next_day = run_day + timedelta(days=1)
        self.write_usage(next_day, [])
        self.run_module("mirror.weekly", next_day)
        next_report = self.weekly_report_text(next_day)
        self.assertIn("Injected-byte drift: changed since previous snapshot", next_report)

    def test_weekly_missing_render_is_unavailable_and_not_anchored(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        (self.overlay / "overlay.md").unlink()

        completed = self.run_module("mirror.weekly", run_day)

        report = self.weekly_report_text(run_day)
        self.assertIn("Status: UNAVAILABLE", report)
        self.assertIn("missing injected artifact(s): overlay.md", report)
        self.assertNotIn("\nno changes\n", report)
        self.assertFalse(
            (self.home / "mirror" / "snapshots" / "alpha" / run_day.isoformat()).exists()
        )
        self.assertIn("[RED] alpha: missing injected artifact(s): overlay.md", completed.stdout)

    def test_weekly_lock_recovery_and_contention_are_red(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        self.write_usage(run_day, [])
        lock = self.home / "mirror" / "lock.d"
        lock.mkdir(parents=True)
        stale = datetime.now(timezone.utc) - timedelta(hours=25)
        (lock / "owner.json").write_text(
            json.dumps({"pid": 12345, "host": "dead", "started_at": stale.isoformat()}),
            encoding="utf-8",
        )
        recovered = self.run_module("mirror.weekly", run_day)
        self.assertIn("[RED] alpha: stale mirror lock recovered", recovered.stdout)
        self.assertTrue((self.home / "reports" / "weekly" / "latest-alpha.json").exists())

        lock.mkdir(parents=True)
        (lock / "owner.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "host": "live",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        contended = self.run_module("mirror.weekly", run_day, check=False)
        self.assertEqual(contended.returncode, 1)
        self.assertIn("[RED] alpha: mirror lock contention", contended.stdout)

    def test_tier_s_secret_scan_rejects_without_losing_owner_report(self) -> None:
        aggregate = {
            "candidate_count": 0,
            "staged_count": 0,
            "adopted_count": 0,
            "window_exposure_total": 0,
            "holdout_opportunity_total": 0,
            "negative_signal_count": 0,
            "explicit_mention_count": 0,
            "promotion_count": 0,
            "demotion_count": 0,
            "block_count": 0,
            "cap_usage": {"adopted": {"bytes": 0, "cap": 1800}, "candidates": {"bytes": 0, "cap": 720}},
            "soul_check": "OK",
            "killswitch": {"state": "ON", "mode": "sk-live-fixture"},
        }
        with self.assertRaisesRegex(MirrorError, "secret scan rejected"):
            _write_tier_s(self.home / "reports" / "tier-s" / "alpha" / "2026-08-03.json", aggregate)

        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        _write_healthy_collector_last_run(self.home, run_day, usage_enabled=False)
        digest = Digest(self.home, run_day.isoformat())
        with mock.patch.dict(os.environ, self.env, clear=False):
            with mock.patch("mirror.weekly._write_tier_s", side_effect=MirrorError("Tier S secret scan rejected aggregate")):
                self.assertEqual(run_weekly("alpha", run_day.isoformat(), self.config_dir, self.home, digest), 0)
        digest.ensure_line()
        self.assertTrue((self.home / "reports" / "weekly" / "alpha" / "2026-08-03.md").is_file())
        self.assertTrue((self.home / "reports" / "weekly" / "latest-alpha.json").is_file())
        self.assertIn("Tier S write aborted", self.digest_text(run_day))

    def test_weekly_zero_usage_currentish_run_day_accepts_prior_night_marker(self) -> None:
        run_day = date(2026, 8, 4)
        self.write_soul_manifest()
        _write_collector_last_run(
            self.home,
            run_at=datetime(2026, 8, 3, 23, 55, tzinfo=JST),
            bucket_date=(run_day - timedelta(days=1)).isoformat(),
            usage_enabled=False,
        )

        completed = self.run_module("mirror.weekly", run_day)

        report = self.weekly_report_text(run_day)
        self.assertIn(
            "[OK] quiet week (bootstrap: 0 staged/adopted — usage collection structurally disabled)",
            report,
        )
        self.assertIn("\nno changes\n", report)
        tier_s = self.tier_s_payload(run_day)
        self.assertEqual(tier_s["window_exposure_total"], 0)
        self.assertEqual(tier_s["holdout_opportunity_total"], 0)
        self.assertIn("weekly mirror: no changes", completed.stdout)

    def test_weekly_zero_usage_currentish_run_day_accepts_same_day_evening_marker(self) -> None:
        run_day = date(2026, 8, 4)
        self.write_soul_manifest()
        _write_collector_last_run(
            self.home,
            run_at=datetime(2026, 8, 4, 23, 45, tzinfo=JST),
            bucket_date=run_day.isoformat(),
            usage_enabled=False,
        )

        completed = self.run_module("mirror.weekly", run_day)

        report = self.weekly_report_text(run_day)
        self.assertIn(
            "[OK] quiet week (bootstrap: 0 staged/adopted — usage collection structurally disabled)",
            report,
        )
        self.assertIn("\nno changes\n", report)
        tier_s = self.tier_s_payload(run_day)
        self.assertEqual(tier_s["window_exposure_total"], 0)
        self.assertEqual(tier_s["holdout_opportunity_total"], 0)
        self.assertIn("weekly mirror: no changes", completed.stdout)

    def test_weekly_zero_usage_currentish_run_day_accepts_post_midnight_previous_day_bucket(self) -> None:
        run_day = date(2026, 8, 4)
        self.write_soul_manifest()
        _write_collector_last_run(
            self.home,
            run_at=datetime(2026, 8, 4, 0, 30, tzinfo=JST),
            bucket_date=(run_day - timedelta(days=1)).isoformat(),
            usage_enabled=False,
        )

        completed = self.run_module("mirror.weekly", run_day)

        report = self.weekly_report_text(run_day)
        self.assertIn(
            "[OK] quiet week (bootstrap: 0 staged/adopted — usage collection structurally disabled)",
            report,
        )
        self.assertIn("\nno changes\n", report)
        tier_s = self.tier_s_payload(run_day)
        self.assertEqual(tier_s["window_exposure_total"], 0)
        self.assertEqual(tier_s["holdout_opportunity_total"], 0)
        self.assertIn("weekly mirror: no changes", completed.stdout)

    def test_weekly_zero_usage_past_run_day_rejects_fresh_current_marker(self) -> None:
        run_day = date(2026, 7, 20)
        self.write_soul_manifest()
        _write_collector_last_run(
            self.home,
            run_at=datetime(2026, 8, 4, 12, 0, tzinfo=JST),
            bucket_date="2026-08-04",
            usage_enabled=False,
        )

        completed = self.run_module("mirror.weekly", run_day)

        report = self.weekly_report_text(run_day)
        self.assertIn(
            "collector last-run marker date is not the weekly run_day or previous JST bucket",
            report,
        )
        self.assertIn("[RED] alpha: usage discipline unavailable", completed.stdout)
        self.assertFalse(
            (self.home / "reports" / "tier-s" / "alpha" / f"{run_day.isoformat()}.json").exists()
        )

    def test_weekly_zero_usage_rejects_future_bucket_relative_to_run_at_day(self) -> None:
        run_day = date(2026, 8, 4)
        self.write_soul_manifest()
        _write_collector_last_run(
            self.home,
            run_at=datetime(2026, 8, 3, 23, 45, tzinfo=JST),
            bucket_date=run_day.isoformat(),
            usage_enabled=False,
        )

        completed = self.run_module("mirror.weekly", run_day)

        report = self.weekly_report_text(run_day)
        self.assertIn(
            "collector last-run marker date is newer than the collector run_at JST day",
            report,
        )
        self.assertIn("[RED] alpha: usage discipline unavailable", completed.stdout)
        self.assertFalse(
            (self.home / "reports" / "tier-s" / "alpha" / f"{run_day.isoformat()}.json").exists()
        )

    def test_weekly_collector_run_at_rejects_after_run_day_grace_window(self) -> None:
        run_day = _jst_today() - timedelta(days=2)
        _write_collector_last_run(
            self.home,
            run_at=datetime.combine(
                run_day + timedelta(days=1), time(1, 1), tzinfo=JST
            ),
            bucket_date=run_day.isoformat(),
            usage_enabled=False,
        )

        healthy, detail, usage_enabled = _collector_liveness(
            self.home, "alpha", run_day
        )

        self.assertFalse(healthy)
        self.assertEqual(
            detail,
            "collector last-run marker run_at later than weekly run_day grace window",
        )
        self.assertIsNone(usage_enabled)

    def test_weekly_zero_usage_with_adopted_phrase_is_broken(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        ledger = empty_ledger("alpha")
        adopted = _phrase("p-0001", "採用フレーズ", run_day - timedelta(days=5))
        adopted["state"] = "adopted"
        adopted["staged_at"] = (run_day - timedelta(days=4)).isoformat()
        ledger["phrases"] = [adopted]
        self.write_ledger(ledger)
        _write_healthy_collector_last_run(self.home, run_day, usage_enabled=False)

        completed = self.run_module("mirror.weekly", run_day)

        report = self.weekly_report_text(run_day)
        self.assertIn(
            "BROKEN: collector last-run marker healthy but usage files missing while staged/adopted phrases exist",
            report,
        )
        self.assertIn("[RED] alpha: usage discipline unavailable", completed.stdout)
        self.assertFalse(
            (self.home / "reports" / "tier-s" / "alpha" / f"{run_day.isoformat()}.json").exists()
        )

    def test_weekly_zero_usage_with_unreadable_ledger_is_broken(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        (self.overlay / "overlay-ledger.yml").write_text("schema_version: [broken\n", encoding="utf-8")
        _write_healthy_collector_last_run(self.home, run_day, usage_enabled=False)

        completed = self.run_module("mirror.weekly", run_day, check=False)

        report = self.weekly_report_text(run_day)
        self.assertIn("Ledger: UNAVAILABLE:", report)
        self.assertIn(
            "BROKEN: collector last-run marker healthy but ledger unavailable for quiet zero-usage certification",
            report,
        )
        self.assertIn("[RED] alpha: usage discipline unavailable", completed.stdout)
        self.assertFalse(
            (self.home / "reports" / "tier-s" / "alpha" / f"{run_day.isoformat()}.json").exists()
        )

    def test_weekly_zero_usage_collector_marker_forgery_matrix(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        marker = self.home / "state" / "collector" / "alpha.last-run.json"
        tier_s_path = self.home / "reports" / "tier-s" / "alpha" / f"{run_day.isoformat()}.json"

        def clean_marker() -> None:
            if marker.exists() or marker.is_symlink():
                marker.unlink()
            marker.parent.mkdir(parents=True, exist_ok=True)
            if tier_s_path.exists():
                tier_s_path.unlink()

        cases = (
            ("missing", lambda: None, "collector last-run marker missing"),
            (
                "symlink",
                lambda: marker.symlink_to(self.home / "missing-collector-marker.json"),
                "symlinked collector last-run marker rejected",
            ),
            ("malformed", lambda: marker.write_text("{broken", encoding="utf-8"), "collector last-run marker invalid"),
            (
                "schema",
                lambda: marker.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "face": "alpha",
                            "run_at": "2026-08-04T12:00:00+09:00",
                            "date": "2026-08-03",
                            "sources_scanned": 0,
                            "records_written": 0,
                            "usage_enabled": False,
                            "errors": 0,
                        }
                    ),
                    encoding="utf-8",
                ),
                "collector last-run marker schema mismatch",
            ),
            (
                "unknown-schema-version",
                lambda: marker.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "face": "alpha",
                            "run_at": datetime(2026, 8, 3, 12, 0, tzinfo=JST).isoformat(),
                            "date": run_day.isoformat(),
                            "sources_scanned": 0,
                            "records_written": 0,
                            "usage_enabled": False,
                            "errors": 0,
                            "ucd": runtime_status().runtime_version,
                        }
                    ),
                    encoding="utf-8",
                ),
                "collector last-run marker schema_version mismatch",
            ),
            (
                "future-run-at",
                lambda: _write_collector_last_run(
                    self.home,
                    run_at=datetime(2099, 1, 1, 0, 0, tzinfo=JST),
                    bucket_date="2026-08-03",
                    usage_enabled=False,
                ),
                "collector last-run marker run_at too far in the future",
            ),
            (
                "stale-run-at",
                lambda: _write_collector_last_run(
                    self.home,
                    run_at=datetime(2026, 7, 31, 23, 59, tzinfo=JST),
                    bucket_date="2026-08-02",
                    usage_enabled=False,
                ),
                "collector last-run marker run_at older than 48h",
            ),
            (
                "collector-errors",
                lambda: _write_collector_last_run(
                    self.home,
                    run_at=datetime.combine(run_day, time(12, 0), tzinfo=JST),
                    bucket_date="2026-08-03",
                    usage_enabled=False,
                    errors=1,
                ),
                "collector last-run marker recorded collector errors",
            ),
            (
                "stale-processed-date",
                lambda: _write_collector_last_run(
                    self.home,
                    run_at=datetime(2026, 8, 3, 12, 0, tzinfo=JST),
                    bucket_date="2026-06-01",
                    usage_enabled=False,
                ),
                "collector last-run marker date is not the weekly run_day or previous JST bucket",
            ),
            (
                "future-processed-date",
                lambda: _write_collector_last_run(
                    self.home,
                    run_at=datetime(2026, 8, 3, 12, 0, tzinfo=JST),
                    bucket_date="2099-01-01",
                    usage_enabled=False,
                ),
                "collector last-run marker date is not the weekly run_day or previous JST bucket",
            ),
        )
        for label, setup, expected in cases:
            with self.subTest(label=label):
                clean_marker()
                setup()
                completed = self.run_module("mirror.weekly", run_day)
                report = self.weekly_report_text(run_day)
                self.assertIn(expected, report)
                self.assertIn("[RED] alpha: usage discipline unavailable", completed.stdout)
                self.assertFalse(tier_s_path.exists())

    def test_weekly_reports_ucd_drift_without_changing_tier_s_shape(self) -> None:
        run_day = date(2026, 8, 3)
        self.write_soul_manifest()
        _write_healthy_collector_last_run(self.home, run_day, usage_enabled=False)
        digest = Digest(self.home, run_day.isoformat())
        with mock.patch.dict(os.environ, self.env, clear=False):
            with mock.patch(
                "mirror.weekly.runtime_status",
                return_value=UcdRuntimeStatus(
                    corpus_version="16.0.0",
                    runtime_version="15.0.0",
                    drifted=True,
                ),
            ):
                self.assertEqual(run_weekly("alpha", run_day.isoformat(), self.config_dir, self.home, digest), 0)
        digest.ensure_line()
        report = self.weekly_report_text(run_day)
        self.assertIn("## Degraded inputs", report)
        self.assertIn(
            "DRIFT: UCD drift runtime=15.0.0 corpus=16.0.0 direction=runtime<corpus; weekly mirror continued",
            report,
        )
        self.assertIn(
            "[RED] alpha: UCD drift runtime=15.0.0 corpus=16.0.0 direction=runtime<corpus; weekly mirror continued",
            self.digest_text(run_day),
        )
