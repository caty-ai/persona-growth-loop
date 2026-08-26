from __future__ import annotations

import plistlib
import stat
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from growthlane.nightly import (
    LUCA_NIGHTLY_HOUR,
    LUCA_NIGHTLY_LAUNCHD_LABEL,
    LUCA_NIGHTLY_MINUTE,
    LUCA_NIGHTLY_PROGRAM,
)


REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "templates" / "launchd"


class LucaLaunchdTemplateTests(unittest.TestCase):
    @staticmethod
    def _minutes_until_next_collector(hour: int, minute: int) -> int:
        nightly_minutes = hour * 60 + minute
        collector_minutes = 5
        return (collector_minutes - nightly_minutes) % (24 * 60)

    def test_luca_nightly_is_host_authored_and_precedes_next_collector(self) -> None:
        """The repo deliberately ships no deploy-capable Luca launchd plist."""

        self.assertEqual(LUCA_NIGHTLY_LAUNCHD_LABEL, "ai.caty.pgl.nightly-luca")
        self.assertEqual((LUCA_NIGHTLY_HOUR, LUCA_NIGHTLY_MINUTE), (4, 0))
        self.assertEqual(LUCA_NIGHTLY_PROGRAM, REPO / "bin" / "pgl-nightly-luca")
        self.assertFalse((TEMPLATES / "ai.caty.pgl.nightly-luca.plist").exists())
        self.assertTrue(
            (LUCA_NIGHTLY_PROGRAM.stat().st_mode & stat.S_IXUSR) != 0,
            "dedicated Luca nightly executable must be user-executable",
        )

        headroom = self._minutes_until_next_collector(
            LUCA_NIGHTLY_HOUR,
            LUCA_NIGHTLY_MINUTE,
        )
        self.assertEqual(headroom, 20 * 60 + 5)
        self.assertGreater(
            headroom,
            0,
            "nightly-luca must leave time before the next 00:05 collector",
        )
        self.assertEqual(
            self._minutes_until_next_collector(0, 5),
            0,
            "a nightly moved to 00:05 must fail the headroom contract",
        )

    def test_alpha_nightly_entrypoint_remains_generic_and_luca_is_dedicated(self) -> None:
        alpha_entrypoint = (REPO / "bin" / "pgl-nightly").read_text(encoding="utf-8")
        luca_entrypoint = (REPO / "bin" / "pgl-nightly-luca").read_text(encoding="utf-8")
        self.assertIn("from growthlane.nightly import main", alpha_entrypoint)
        self.assertIn("from growthlane.nightly import luca_main", luca_entrypoint)
        self.assertNotIn("luca_main", alpha_entrypoint)

    def test_host_authored_alpha_nightly_contract_when_installed(self) -> None:
        plist_path = Path.home() / "Library" / "LaunchAgents" / "ai.caty.pgl.nightly.plist"
        if not plist_path.is_file():
            self.skipTest("host-authored alpha nightly plist is not installed in this environment")
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["Label"], "ai.caty.pgl.nightly")
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 0, "Minute": 15})
        self.assertEqual(plist["ProgramArguments"][-2:], ["--face", "alpha"])

    def test_host_authored_luca_nightly_contract_when_installed(self) -> None:
        plist_path = (
            Path.home()
            / "Library"
            / "LaunchAgents"
            / "ai.caty.pgl.nightly-luca.plist"
        )
        if not plist_path.is_file():
            self.skipTest("host-authored Luca nightly plist is not installed in this environment")
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["Label"], "ai.caty.pgl.nightly-luca")
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 4, "Minute": 0})
        self.assertEqual(plist["ProgramArguments"][-1], str(LUCA_NIGHTLY_PROGRAM))
        self.assertNotIn("--face", plist["ProgramArguments"])

    def _render(self, name: str, destination: Path) -> dict[str, object]:
        temporary_repo = destination.parent / "persona-growth-loop"
        temporary_home = destination.parent / "pgl-home"
        rendered = (
            (TEMPLATES / name)
            .read_text(encoding="utf-8")
            .replace("__PGL_REPO__", str(temporary_repo))
            .replace("__PGL_HOME__", str(temporary_home))
        )
        self.assertNotIn("__PGL_REPO__", rendered)
        self.assertNotIn("__PGL_HOME__", rendered)
        destination.write_text(rendered, encoding="utf-8")
        with destination.open("rb") as handle:
            return plistlib.load(handle)

    def test_launchd_templates_lint_when_plutil_is_available(self) -> None:
        plutil = Path("/usr/bin/plutil")
        if not plutil.exists():
            discovered = shutil.which("plutil")
            if discovered is None:
                self.skipTest("plutil is not available")
            plutil = Path(discovered)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for template in sorted(TEMPLATES.glob("*.plist")):
                destination = root / template.name
                rendered = (
                    template.read_text(encoding="utf-8")
                    .replace("__PGL_REPO__", str(root / "persona-growth-loop"))
                    .replace("__PGL_HOME__", str(root / "pgl-home"))
                )
                destination.write_text(rendered, encoding="utf-8")
                result = subprocess.run(
                    [str(plutil), "-lint", str(destination)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{template.name}: {result.stdout}{result.stderr}",
                )

    def test_luca_collector_template_renders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rendered = self._render(
                "ai.caty.pgl.obs-collector-luca.plist",
                root / "collector.plist",
            )
            repo = root / "persona-growth-loop"
            home = root / "pgl-home"
            self.assertEqual(rendered["Label"], "ai.caty.pgl.obs-collector-luca")
            self.assertEqual(
                rendered["ProgramArguments"],
                [
                    "/usr/bin/env",
                    "python3",
                    str(repo / "bin" / "pgl-obs-collector-luca"),
                ],
            )
            self.assertEqual(rendered["WorkingDirectory"], str(repo))
            self.assertEqual(
                rendered["EnvironmentVariables"],
                {
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                    "PGL_HOME": str(home),
                },
            )
            self.assertEqual(
                rendered["StartCalendarInterval"],
                {"Hour": 0, "Minute": 5},
            )
            self.assertEqual(
                rendered["StandardOutPath"],
                str(home / "logs" / "collector-luca.out.log"),
            )
            self.assertEqual(
                rendered["StandardErrorPath"],
                str(home / "logs" / "collector-luca.err.log"),
            )

    def test_luca_weekly_template_renders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rendered = self._render(
                "ai.caty.pgl.mirror-weekly-luca.plist",
                root / "weekly.plist",
            )
            repo = root / "persona-growth-loop"
            home = root / "pgl-home"
            self.assertEqual(rendered["Label"], "ai.caty.pgl.mirror-weekly-luca")
            self.assertEqual(
                rendered["ProgramArguments"],
                [
                    "/usr/bin/env",
                    "python3",
                    str(repo / "bin" / "pgl-mirror-weekly"),
                    "luca",
                ],
            )
            self.assertEqual(rendered["WorkingDirectory"], str(repo))
            self.assertEqual(
                rendered["EnvironmentVariables"],
                {
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                    "PGL_HOME": str(home),
                },
            )
            self.assertEqual(
                rendered["StartCalendarInterval"],
                {"Weekday": 2, "Hour": 1, "Minute": 30},
            )
            self.assertEqual(
                rendered["StandardOutPath"],
                str(home / "logs" / "mirror-weekly-luca.out.log"),
            )
            self.assertEqual(
                rendered["StandardErrorPath"],
                str(home / "logs" / "mirror-weekly-luca.err.log"),
            )

    def test_alpha_launchd_contract_is_unchanged(self) -> None:
        with (TEMPLATES / "ai.caty.pgl.obs-collector.plist").open("rb") as handle:
            collector = plistlib.load(handle)
        with (TEMPLATES / "ai.caty.pgl.mirror-weekly.plist").open("rb") as handle:
            weekly = plistlib.load(handle)

        self.assertEqual(collector["Label"], "ai.caty.pgl.obs-collector")
        self.assertEqual(
            collector["StartCalendarInterval"],
            {"Hour": 0, "Minute": 5},
        )
        self.assertEqual(
            collector["ProgramArguments"],
            [
                "/usr/bin/env",
                "python3",
                "__PGL_REPO__/collectors/claude_code/collector.py",
                "--config",
                "__PGL_REPO__/config/obs-collector.json",
            ],
        )
        self.assertEqual(
            collector["EnvironmentVariables"],
            {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "PGL_HOME": "__PGL_HOME__",
            },
        )
        self.assertEqual(collector["WorkingDirectory"], "__PGL_REPO__")
        self.assertEqual(
            collector["StandardOutPath"],
            "__PGL_HOME__/logs/collector.out.log",
        )
        self.assertEqual(
            collector["StandardErrorPath"],
            "__PGL_HOME__/logs/collector.err.log",
        )
        self.assertEqual(weekly["Label"], "ai.caty.pgl.mirror-weekly")
        self.assertEqual(
            weekly["StartCalendarInterval"],
            {"Weekday": 1, "Hour": 1, "Minute": 30},
        )
        self.assertEqual(
            weekly["ProgramArguments"],
            [
                "/usr/bin/env",
                "python3",
                "__PGL_REPO__/bin/pgl-mirror-weekly",
                "alpha",
            ],
        )
        self.assertEqual(
            weekly["EnvironmentVariables"],
            {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "PGL_HOME": "__PGL_HOME__",
            },
        )
        self.assertEqual(weekly["WorkingDirectory"], "__PGL_REPO__")
        self.assertEqual(
            weekly["StandardOutPath"],
            "__PGL_HOME__/logs/mirror-weekly.out.log",
        )
        self.assertEqual(
            weekly["StandardErrorPath"],
            "__PGL_HOME__/logs/mirror-weekly.err.log",
        )


class MirrorScheduleHeadroomTests(unittest.TestCase):
    # #40 MAJOR-1 / MINOR-1 (independent GLM + Grok review convergence,
    # 2026-08-07/08): the collector's ~11s observed run time is a single
    # measurement (n=1), not a bound -- a cold cache, a growing sources list,
    # a sleep-wake-delayed execution, or contention with another job could
    # all push a real run well past a thin margin. The marker the weekly
    # mirror reads is written only after the collector's full scan
    # completes, so if the collector is still running when the weekly job
    # fires, the weekly job sees the *previous* day's marker and reports
    # BROKEN. Pin a floor of 60 minutes of headroom (a large safety factor
    # over the 11s measurement) between the daily collector and each face's
    # weekly mirror so a future schedule edit that erodes this margin fails
    # a test instead of silently landing on the BROKEN day-(N-2) case.
    _MIN_HEADROOM_MINUTES = 60

    def _interval(self, name: str) -> dict[str, int]:
        with (TEMPLATES / name).open("rb") as handle:
            return plistlib.load(handle)["StartCalendarInterval"]

    def _assert_headroom(self, *, collector_template: str, weekly_template: str) -> None:
        collector = self._interval(collector_template)
        weekly = self._interval(weekly_template)
        # Both collectors run daily (no "Weekday" key) and each weekly job's
        # "Weekday" is chosen so the most recent collector run lands on the
        # same calendar day as the weekly run (see docs/ops/collector-wiring.md
        # and docs/ops/luca-observe-wiring.md) -- so comparing time-of-day
        # alone is the correct headroom measure here.
        collector_minutes = collector["Hour"] * 60 + collector["Minute"]
        weekly_minutes = weekly["Hour"] * 60 + weekly["Minute"]
        headroom = weekly_minutes - collector_minutes
        self.assertGreaterEqual(
            headroom,
            self._MIN_HEADROOM_MINUTES,
            msg=(
                f"{weekly_template} runs only {headroom} minute(s) after "
                f"{collector_template}; need >= {self._MIN_HEADROOM_MINUTES}"
            ),
        )

    def test_alpha_weekly_has_sufficient_headroom_after_collector(self) -> None:
        self._assert_headroom(
            collector_template="ai.caty.pgl.obs-collector.plist",
            weekly_template="ai.caty.pgl.mirror-weekly.plist",
        )

    def test_luca_weekly_has_sufficient_headroom_after_collector(self) -> None:
        self._assert_headroom(
            collector_template="ai.caty.pgl.obs-collector-luca.plist",
            weekly_template="ai.caty.pgl.mirror-weekly-luca.plist",
        )


if __name__ == "__main__":
    unittest.main()
