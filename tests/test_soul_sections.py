from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from applier.apply import _verify_manifest_or_red
from growthlane import nightly
from growthlane.config import Thresholds
from growthlane.faces import get_profile
from growthlane.notify import Digest, send_soul_alert
from growthlane.operations import baseline_main
from growthlane.soul import SoulError, verify_manifest, write_manifest


def soul_text(order: tuple[str, ...] = ("identity", "warmth", "memory")) -> str:
    sections = {
        "identity": "### Identity (アルファ)\nidentity body\n",
        "warmth": "### Warmth Persona Core v1\nwarmth body  \n",
        "memory": "### F. 関係の記憶\nmemory body\n",
    }
    return (
        "# Unrelated\n日常編集 A\n"
        + "## Another\n日常編集 B\n"
        + "".join(sections[key] + "## separator\nignored\n" for key in order)
        + "~/.persona-growth-loop/faces/alpha/overlay.md を読む\n"
    )


class SoulSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home_patch = mock.patch.dict(os.environ, {"HOME": str(self.root)})
        self.home_patch.start()
        self.pgl_home = self.root / "pgl"
        self.overlay = self.pgl_home / "faces" / "alpha"
        (self.overlay / ".git").mkdir(parents=True)
        self.soul = self.root / ".claude" / "CLAUDE.md"
        self.soul.parent.mkdir()
        self.soul.write_text(soul_text(), encoding="utf-8")
        self.profile = get_profile("alpha")
        self.config: dict[str, object] = {}

    def tearDown(self) -> None:
        self.home_patch.stop()
        self.temporary.cleanup()

    def baseline(self) -> Path:
        return write_manifest(
            self.profile, self.pgl_home, self.config, [self.soul]
        )

    def test_unrelated_edits_and_section_moves_preserve_hash(self) -> None:
        manifest = self.baseline()
        expected = json.loads(manifest.read_text(encoding="utf-8"))["files"][0]["sha256"]
        moved = soul_text(("memory", "identity", "warmth")).replace(
            "日常編集 A", "まったく別の日常編集"
        )
        self.soul.write_text(moved, encoding="utf-8")
        self.assertEqual(
            verify_manifest(self.profile, self.pgl_home, self.config)[0]["sha256"],
            expected,
        )

    def test_core_edit_causes_mismatch(self) -> None:
        self.baseline()
        self.soul.write_text(soul_text().replace("warmth body", "warmth bodX"), encoding="utf-8")
        with self.assertRaisesRegex(SoulError, "soul hash mismatch"):
            verify_manifest(self.profile, self.pgl_home, self.config)

    def test_hash_reference_does_not_end_monitored_section(self) -> None:
        text = soul_text().replace(
            "identity body\n",
            "identity before\n#42 issue ref\nidentity after\n",
        )
        self.soul.write_text(text, encoding="utf-8")
        self.baseline()
        self.soul.write_text(
            text.replace("identity after", "identity mutated"), encoding="utf-8"
        )
        with self.assertRaisesRegex(SoulError, "soul hash mismatch"):
            verify_manifest(self.profile, self.pgl_home, self.config)

    def test_only_level_one_to_three_atx_headings_end_section(self) -> None:
        cases = (
            ("level-three", "### nested\n", False),
            ("bare-level-two", "##\n", False),
            ("level-four", "#### h4\n", True),
        )
        for label, boundary, mutation_is_monitored in cases:
            with self.subTest(label=label):
                text = soul_text().replace(
                    "identity body\n",
                    f"identity before\n{boundary}identity after\n",
                )
                self.soul.write_text(text, encoding="utf-8")
                self.baseline()
                self.soul.write_text(
                    text.replace("identity after", "identity mutated"),
                    encoding="utf-8",
                )
                if mutation_is_monitored:
                    with self.assertRaisesRegex(SoulError, "soul hash mismatch"):
                        verify_manifest(self.profile, self.pgl_home, self.config)
                else:
                    verify_manifest(self.profile, self.pgl_home, self.config)

    def test_unicode_line_separator_cannot_create_phantom_heading_boundary(self) -> None:
        text = soul_text().replace(
            "identity body\n",
            "identity before\u2028#x phantom boundary\nidentity after\n",
        )
        self.soul.write_text(text, encoding="utf-8")
        self.baseline()
        self.soul.write_text(
            text.replace("identity after", "identity mutated"), encoding="utf-8"
        )
        with self.assertRaisesRegex(SoulError, "soul hash mismatch"):
            verify_manifest(self.profile, self.pgl_home, self.config)

    def test_missing_and_duplicate_headings_fail_closed(self) -> None:
        for label, altered in (
            ("missing", soul_text().replace("### Identity (アルファ)", "### Renamed")),
            ("duplicate", soul_text() + "### Identity (アルファ) duplicate\n"),
        ):
            with self.subTest(label=label):
                self.soul.write_text(altered, encoding="utf-8")
                with self.assertRaisesRegex(
                    SoulError, "heading match count.*R12a"
                ):
                    self.baseline()

    def test_empty_core_section_fails_closed(self) -> None:
        self.soul.write_text(
            soul_text().replace("### Identity (アルファ)\nidentity body\n", "### Identity (アルファ)\n\n"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SoulError, "section body is empty"):
            self.baseline()

    def test_missing_and_duplicate_overlay_markers_fail_closed(self) -> None:
        marker_line = "~/.persona-growth-loop/faces/alpha/overlay.md を読む\n"
        for label, altered in (
            ("missing", soul_text().replace(marker_line, "")),
            ("duplicate", soul_text() + marker_line),
        ):
            with self.subTest(label=label):
                self.soul.write_text(altered, encoding="utf-8")
                with self.assertRaisesRegex(
                    SoulError, "line marker match count.*R12a"
                ):
                    self.baseline()

    def test_crlf_and_lf_hash_identically(self) -> None:
        self.baseline()
        self.soul.write_bytes(soul_text().replace("\n", "\r\n").encode("utf-8"))
        verify_manifest(self.profile, self.pgl_home, self.config)
        self.baseline()
        self.soul.write_text(soul_text(), encoding="utf-8")
        verify_manifest(self.profile, self.pgl_home, self.config)

    def test_target_section_may_end_at_eof(self) -> None:
        text = (
            "~/.persona-growth-loop/faces/alpha/overlay.md\n"
            "### Identity (アルファ)\nidentity\n"
            "### Warmth Persona Core v1\nwarmth\n"
            "### F. 関係の記憶\nmemory without terminal newline"
        )
        self.soul.write_text(text, encoding="utf-8")
        self.baseline()
        verify_manifest(self.profile, self.pgl_home, self.config)

    def test_extracted_manifest_round_trip_unknown_and_legacy(self) -> None:
        manifest = self.baseline()
        value = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(value["files"][0]["extract"], "alpha-soul-v1")
        verify_manifest(self.profile, self.pgl_home, self.config)

        value["files"][0]["extract"] = "unknown-v9"
        manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(SoulError, "unknown soul extraction method"):
            verify_manifest(self.profile, self.pgl_home, self.config)

        legacy_soul = self.soul.parent / "settings.json"
        legacy_soul.write_text('{"legacy":true}\n', encoding="utf-8")
        whole = hashlib.sha256(legacy_soul.read_bytes()).hexdigest()
        value["files"] = [{"path": str(legacy_soul.resolve()), "sha256": whole}]
        manifest.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(
            verify_manifest(self.profile, self.pgl_home, self.config)[0]["sha256"],
            whole,
        )

    def test_extracted_manifest_records_section_coverage(self) -> None:
        manifest = self.baseline()
        record = json.loads(manifest.read_text(encoding="utf-8"))["files"][0]
        coverage = record["coverage"]
        self.assertEqual(coverage["markers"], 1)
        self.assertEqual(
            [section["prefix"] for section in coverage["sections"]],
            [
                "### Identity (アルファ)",
                "### Warmth Persona Core v1",
                "### F. 関係の記憶",
            ],
        )
        for section in coverage["sections"]:
            self.assertGreater(section["lines"], 0)
            self.assertGreater(section["bytes"], 0)

    def test_invalid_coverage_schema_fails_closed(self) -> None:
        manifest = self.baseline()
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["files"][0]["coverage"]["sections"][0]["lines"] += 100
        manifest.write_text(json.dumps(value), encoding="utf-8")
        verify_manifest(self.profile, self.pgl_home, self.config)
        for invalid in (None, "not-an-object"):
            with self.subTest(invalid=invalid):
                value["files"][0]["coverage"] = invalid
                manifest.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(SoulError, "invalid soul baseline entry"):
                    verify_manifest(self.profile, self.pgl_home, self.config)

    def test_baseline_digest_includes_section_sizes(self) -> None:
        digest = Digest(self.pgl_home, "2026-08-13")
        with (
            mock.patch(
                "growthlane.operations._context",
                return_value=(
                    self.pgl_home,
                    "2026-08-13",
                    self.config,
                    self.profile,
                    digest,
                ),
            ),
            mock.patch("growthlane.operations.check_cp"),
            mock.patch("growthlane.operations.check_killswitch"),
            mock.patch(
                "growthlane.operations._locked",
                side_effect=lambda _home, _face, _digest, action: action(),
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(baseline_main(["alpha", str(self.soul)]), 0)
        digest_text = digest.path.read_text(encoding="utf-8")
        self.assertIn("coverage=", digest_text)
        self.assertIn("'### Identity (アルファ)'=2 lines/", digest_text)
        self.assertIn(" bytes", digest_text)


class SoulAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home_patch = mock.patch.dict(os.environ, {"HOME": str(self.root)})
        self.home_patch.start()
        self.pgl_home = self.root / "pgl"
        self.overlay = self.pgl_home / "faces" / "alpha"
        (self.overlay / ".git").mkdir(parents=True)
        self.soul = self.root / ".claude" / "CLAUDE.md"
        self.soul.parent.mkdir()
        self.soul.write_text(soul_text(), encoding="utf-8")
        self.profile = get_profile("alpha")
        write_manifest(self.profile, self.pgl_home, {}, [self.soul])

    def tearDown(self) -> None:
        self.home_patch.stop()
        self.temporary.cleanup()

    def fake_alert(self, exit_code: int = 0) -> tuple[Path, Path]:
        output = self.root / "alert.txt"
        script = self.root / "alert.sh"
        script.write_text(
            "#!/bin/sh\ncat >> \"$PGL_ALERT_OUTPUT\"\nexit " + str(exit_code) + "\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script, output

    def mismatch(self) -> None:
        self.soul.write_text(
            soul_text().replace("identity body", "identity changed"),
            encoding="utf-8",
        )

    def test_manifest_red_sends_only_once_per_run(self) -> None:
        script, output = self.fake_alert()
        self.mismatch()
        digest = Digest(self.pgl_home, "2026-08-12")
        config = {"soul_alert_argv": [str(script)]}
        with mock.patch.dict(os.environ, {"PGL_ALERT_OUTPUT": str(output)}):
            for _ in range(2):
                with self.assertRaises(SoulError):
                    _verify_manifest_or_red(self.profile, self.pgl_home, config, digest)
        delivered = output.read_text(encoding="utf-8")
        self.assertEqual(delivered.count("soul baseline check failed"), 1)
        self.assertIn("date: 2026-08-12", delivered)
        self.assertIn("再基線はオーナーの明示指示", delivered)
        self.assertIn("bin/pgl-baseline alpha <path>", delivered)

    def test_digest_exposes_soul_alert_dedup_api(self) -> None:
        digest = Digest(self.pgl_home, "2026-08-16")
        self.assertTrue(digest.mark_soul_alerted("alpha"))
        self.assertFalse(digest.mark_soul_alerted("alpha"))

    def test_alert_expands_only_executable_path(self) -> None:
        digest = Digest(self.pgl_home, "2026-08-17")
        completed = mock.Mock(returncode=0)
        with mock.patch("growthlane.notify.subprocess.run", return_value=completed) as run:
            send_soul_alert(
                {"soul_alert_argv": ["~/bin/alert", "~/literal-argument"]},
                "alpha",
                SoulError("bad soul"),
                digest,
            )
        self.assertEqual(
            run.call_args.args[0],
            [str(self.root / "bin" / "alert"), "~/literal-argument"],
        )

    def test_unexpected_alert_exception_is_best_effort(self) -> None:
        digest = Digest(self.pgl_home, "2026-08-18")
        with mock.patch(
            "growthlane.notify.subprocess.run", side_effect=TypeError("unexpected")
        ):
            send_soul_alert(
                {"soul_alert_argv": ["unused"]},
                "alpha",
                SoulError("bad soul"),
                digest,
            )
        self.assertIn(
            "[WARN] alpha: soul alert delivery failed: unexpected",
            digest.path.read_text(encoding="utf-8"),
        )

    def test_alert_failure_does_not_replace_soul_failure(self) -> None:
        script, _ = self.fake_alert(9)
        self.mismatch()
        digest = Digest(self.pgl_home, "2026-08-12")
        with self.assertRaises(SoulError):
            _verify_manifest_or_red(
                self.profile,
                self.pgl_home,
                {"soul_alert_argv": [str(script)]},
                digest,
            )
        text = digest.path.read_text(encoding="utf-8")
        self.assertIn("[RED] alpha: soul/root baseline check failed", text)
        self.assertIn("[WARN] alpha: soul alert delivery failed: exit 9", text)

    def test_unconfigured_and_invalid_alerts_warn(self) -> None:
        for label, config, warning in (
            ("unset", {}, "soul alert not configured"),
            ("invalid", {"soul_alert_argv": "bad"}, "invalid soul_alert_argv ignored"),
            ("blank", {"soul_alert_argv": [" "]}, "invalid soul_alert_argv ignored"),
        ):
            with self.subTest(label=label):
                run_day = {"unset": 12, "invalid": 13, "blank": 14}[label]
                digest = Digest(self.pgl_home, f"2026-08-{run_day}")
                send_soul_alert(config, "alpha", SoulError("bad soul"), digest)
                self.assertIn(warning, digest.path.read_text(encoding="utf-8"))

    def test_non_soul_lane_stop_does_not_alert(self) -> None:
        lock = object()
        config = {"classifier_argv": [], "soul_alert_argv": ["unused"]}
        with (
            mock.patch("growthlane.nightly.load_json_object", return_value=config),
            mock.patch("growthlane.nightly.emit_admission_refusal", return_value=None),
            mock.patch("growthlane.nightly.check_all"),
            mock.patch("growthlane.nightly.parse_scalar_yaml", return_value=Thresholds({}, [])),
            mock.patch("growthlane.nightly.acquire_lock", return_value=lock),
            mock.patch("growthlane.nightly.release_lock", return_value=True),
            mock.patch("growthlane.nightly.run_face", side_effect=RuntimeError("ordinary stop")),
            mock.patch("growthlane.nightly.inspect"),
            mock.patch("growthlane.nightly.send_soul_alert") as alert,
            mock.patch.dict(os.environ, {"PGL_HOME": str(self.pgl_home)}),
        ):
            self.assertEqual(nightly.main(["--face", "alpha", "--date", "2026-08-14"]), 1)
        alert.assert_not_called()
        self.assertIn(
            "alpha: lane stopped for night: ordinary stop",
            (self.pgl_home / "digest" / "2026-08-14.md").read_text(encoding="utf-8"),
        )

    def test_soul_lane_stop_alerts_and_preserves_nonzero_exit(self) -> None:
        lock = object()
        config = {"classifier_argv": [], "soul_alert_argv": ["unused"]}
        failure = SoulError("late soul mismatch")
        with (
            mock.patch("growthlane.nightly.load_json_object", return_value=config),
            mock.patch("growthlane.nightly.emit_admission_refusal", return_value=None),
            mock.patch("growthlane.nightly.check_all"),
            mock.patch("growthlane.nightly.parse_scalar_yaml", return_value=Thresholds({}, [])),
            mock.patch("growthlane.nightly.acquire_lock", return_value=lock),
            mock.patch("growthlane.nightly.release_lock", return_value=True),
            mock.patch("growthlane.nightly.run_face", side_effect=failure),
            mock.patch("growthlane.nightly.inspect"),
            mock.patch("growthlane.nightly.send_soul_alert") as alert,
            mock.patch.dict(os.environ, {"PGL_HOME": str(self.pgl_home)}),
        ):
            self.assertEqual(nightly.main(["--face", "alpha", "--date", "2026-08-15"]), 1)
        alert.assert_called_once_with(config, "alpha", failure, mock.ANY)

    def test_unexpected_alert_exception_preserves_red_lane_stop_and_exit(self) -> None:
        lock = object()
        config = {"classifier_argv": [], "soul_alert_argv": ["unused"]}
        failure = SoulError("late soul mismatch")

        def fail_after_red(*args: object) -> None:
            run_digest = args[-1]
            assert isinstance(run_digest, Digest)
            run_digest.emit("[RED] alpha: soul/root baseline check failed: late soul mismatch")
            raise failure

        with (
            mock.patch("growthlane.nightly.load_json_object", return_value=config),
            mock.patch("growthlane.nightly.emit_admission_refusal", return_value=None),
            mock.patch("growthlane.nightly.check_all"),
            mock.patch("growthlane.nightly.parse_scalar_yaml", return_value=Thresholds({}, [])),
            mock.patch("growthlane.nightly.acquire_lock", return_value=lock),
            mock.patch("growthlane.nightly.release_lock", return_value=True),
            mock.patch("growthlane.nightly.run_face", side_effect=fail_after_red),
            mock.patch("growthlane.nightly.inspect"),
            mock.patch(
                "growthlane.notify.subprocess.run", side_effect=TypeError("unexpected")
            ),
            mock.patch.dict(os.environ, {"PGL_HOME": str(self.pgl_home)}),
        ):
            self.assertEqual(nightly.main(["--face", "alpha", "--date", "2026-08-19"]), 1)
        text = (self.pgl_home / "digest" / "2026-08-19.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("[RED] alpha: soul/root baseline check failed", text)
        lane_stop = "alpha: lane stopped for night: late soul mismatch"
        warning = "[WARN] alpha: soul alert delivery failed: unexpected"
        self.assertIn(lane_stop, text)
        self.assertIn(warning, text)
        self.assertLess(text.index(lane_stop), text.index(warning))
