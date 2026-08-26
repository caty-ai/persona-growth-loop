from __future__ import annotations

import configparser
import importlib.machinery
import importlib.util
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LAUNCHD = REPO / "templates" / "launchd"
SYSTEMD = REPO / "templates" / "systemd"


def _load_preflight_module():
    path = REPO / "bin" / "pgl-preflight"
    loader = importlib.machinery.SourceFileLoader("pgl_preflight_contract", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("cannot load bin/pgl-preflight")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


SYSTEM_PATH_SUFFIX = _load_preflight_module().SYSTEM_PATH_SUFFIX

JOBS = {
    "obs-collector": {
        "calendar": "*-*-* 00:05:00",
        "log": "collector",
        "plist": {"Hour": 0, "Minute": 5},
    },
    "obs-collector-luca": {
        "calendar": "*-*-* 00:05:00",
        "log": "collector-luca",
        "plist": {"Hour": 0, "Minute": 5},
    },
    "mirror-weekly": {
        "calendar": "Mon *-*-* 01:30:00",
        "log": "mirror-weekly",
        "plist": {"Weekday": 1, "Hour": 1, "Minute": 30},
    },
    "mirror-weekly-luca": {
        "calendar": "Tue *-*-* 01:30:00",
        "log": "mirror-weekly-luca",
        "plist": {"Weekday": 2, "Hour": 1, "Minute": 30},
    },
}
EXPECTED_FILENAMES = {
    f"ai.caty.pgl.{job}.{suffix}" for job in JOBS for suffix in ("service", "timer")
}
ORDERING = {
    "mirror-weekly": "ai.caty.pgl.obs-collector.service",
    "mirror-weekly-luca": "ai.caty.pgl.obs-collector-luca.service",
}
MIN_HEADROOM_MINUTES = 60


def _parser(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    with path.open(encoding="utf-8") as handle:
        parser.read_file(handle)
    return parser


def _directives(path: Path, section: str, key: str) -> list[str]:
    current: str | None = None
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
        elif current == section and stripped.startswith(f"{key}="):
            values.append(stripped.split("=", 1)[1])
    return values


def _plist(job: str) -> dict[str, object]:
    with (LAUNCHD / f"ai.caty.pgl.{job}.plist").open("rb") as handle:
        return plistlib.load(handle)


def _calendar_from_plist(interval: dict) -> str:
    hour = int(interval["Hour"])
    minute = int(interval["Minute"])
    time_value = f"{hour:02d}:{minute:02d}:00"
    if "Weekday" not in interval:
        return f"*-*-* {time_value}"
    weekday = int(interval["Weekday"])
    names = {
        0: "Sun",
        1: "Mon",
        2: "Tue",
        3: "Wed",
        4: "Thu",
        5: "Fri",
        6: "Sat",
        7: "Sun",
    }
    if weekday not in names:
        raise ValueError(f"unsupported launchd Weekday: {weekday}")
    return f"{names[weekday]} *-*-* {time_value}"


def _calendar_minutes(calendar: str) -> int:
    hour, minute, _ = map(int, calendar.rsplit(" ", 1)[1].split(":"))
    return hour * 60 + minute


def _render(path: Path, root: Path) -> tuple[str, Path, Path, Path]:
    repo = REPO
    home = root / "pgl-home"
    python_bin = Path("/usr/bin")
    (home / "logs").mkdir(parents=True, exist_ok=True)
    rendered = (
        path.read_text(encoding="utf-8")
        .replace("__PGL_REPO__", str(repo))
        .replace("__PGL_HOME__", str(home))
        .replace("__PGL_PYTHON_BIN__", str(python_bin))
    )
    return rendered, repo, home, python_bin


class SystemdTemplateTests(unittest.TestCase):
    def test_positive_eight_filename_pin_and_nightly_absence(self) -> None:
        actual = {path.name for path in SYSTEMD.iterdir() if path.is_file()}
        self.assertEqual(actual, EXPECTED_FILENAMES)
        self.assertFalse(any("nightly" in name for name in actual))
        for face_suffix in ("", "-luca"):
            self.assertFalse((SYSTEMD / f"ai.caty.pgl.nightly{face_suffix}.service").exists())
            self.assertFalse((SYSTEMD / f"ai.caty.pgl.nightly{face_suffix}.timer").exists())

    def test_job_set_is_enumerated_from_disk_and_matches_launchd(self) -> None:
        launchd_jobs = {
            path.name.removeprefix("ai.caty.pgl.").removesuffix(".plist")
            for path in LAUNCHD.glob("*.plist")
            if "nightly" not in path.name
        }
        systemd_jobs = {
            path.name.removeprefix("ai.caty.pgl.").removesuffix(".timer")
            for path in SYSTEMD.glob("*.timer")
            if "nightly" not in path.name
        }
        self.assertEqual(launchd_jobs, systemd_jobs)
        self.assertEqual(systemd_jobs, set(JOBS))

    def test_service_and_timer_section_structure(self) -> None:
        for job in JOBS:
            with self.subTest(job=job):
                service_path = SYSTEMD / f"ai.caty.pgl.{job}.service"
                timer_path = SYSTEMD / f"ai.caty.pgl.{job}.timer"
                service = _parser(service_path)
                timer = _parser(timer_path)
                self.assertEqual(service.sections(), ["Unit", "Service"])
                self.assertNotIn("Install", service)
                self.assertEqual(timer.sections(), ["Unit", "Timer", "Install"])
                self.assertEqual(timer["Install"]["WantedBy"], "timers.target")
                self.assertEqual(_directives(timer_path, "Timer", "OnCalendar"), [JOBS[job]["calendar"]])
                self.assertEqual(_directives(timer_path, "Timer", "Persistent"), ["true"])
                self.assertEqual(service["Service"]["Type"], "oneshot")
                self.assertEqual(
                    _directives(service_path, "Service", "Environment").count(
                        "PATH=__PGL_PYTHON_BIN__" + SYSTEM_PATH_SUFFIX
                    ),
                    1,
                )
                for directive in ("ExecStart", "StandardOutput", "StandardError"):
                    self.assertEqual(
                        len(_directives(service_path, "Service", directive)),
                        1,
                        f"{service_path.name} must contain exactly one {directive}=",
                    )

    def test_weekly_services_pin_per_face_ordering(self) -> None:
        for job, collector in ORDERING.items():
            service = SYSTEMD / f"ai.caty.pgl.{job}.service"
            with self.subTest(job=job):
                self.assertEqual(_directives(service, "Unit", "After"), [collector])
                self.assertEqual(_directives(service, "Unit", "Wants"), [collector])
        for job in ("obs-collector", "obs-collector-luca"):
            service = SYSTEMD / f"ai.caty.pgl.{job}.service"
            self.assertEqual(_directives(service, "Unit", "After"), [])
            self.assertEqual(_directives(service, "Unit", "Wants"), [])

    def test_templates_render_without_placeholders_and_pin_path_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in sorted(SYSTEMD.iterdir()):
                with self.subTest(template=path.name):
                    rendered, repo, home, python_bin = _render(path, root)
                    self.assertNotIn("__PGL_", rendered)
                    if path.suffix == ".service":
                        rendered_path = root / path.name
                        rendered_path.write_text(rendered, encoding="utf-8")
                        values = _directives(rendered_path, "Service", "Environment")
                        paths = [value.removeprefix("PATH=") for value in values if value.startswith("PATH=")]
                        self.assertEqual(paths, [str(python_bin) + SYSTEM_PATH_SUFFIX])
                        prefix = paths[0][: -len(SYSTEM_PATH_SUFFIX)]
                        self.assertTrue(Path(prefix).is_absolute())
                        self.assertNotRegex(prefix, r"\s")
                        self.assertNotIn(":", prefix)
                        self.assertIn(f"PGL_HOME={home}", values)
                        self.assertIn(f"WorkingDirectory={repo}", rendered)

    def test_per_job_schedule_argv_and_log_equivalence(self) -> None:
        for job, expected in JOBS.items():
            with self.subTest(job=job):
                plist = _plist(job)
                service_path = SYSTEMD / f"ai.caty.pgl.{job}.service"
                timer_path = SYSTEMD / f"ai.caty.pgl.{job}.timer"
                service = _parser(service_path)
                timer = _parser(timer_path)
                self.assertEqual(plist["StartCalendarInterval"], expected["plist"])
                self.assertEqual(timer["Timer"]["OnCalendar"], expected["calendar"])
                self.assertEqual(
                    timer["Timer"]["OnCalendar"],
                    _calendar_from_plist(plist["StartCalendarInterval"]),
                )
                self.assertEqual(
                    shlex.split(service["Service"]["ExecStart"]),
                    plist["ProgramArguments"],
                )
                self.assertEqual(
                    Path(service["Service"]["StandardOutput"].removeprefix("append:")).name,
                    Path(plist["StandardOutPath"]).name,
                )
                self.assertEqual(
                    Path(service["Service"]["StandardError"].removeprefix("append:")).name,
                    Path(plist["StandardErrorPath"]).name,
                )
                self.assertEqual(
                    Path(plist["StandardOutPath"]).name,
                    f"{expected['log']}.out.log",
                )
                self.assertEqual(
                    Path(plist["StandardErrorPath"]).name,
                    f"{expected['log']}.err.log",
                )

    def test_systemd_headroom_is_at_least_sixty_minutes(self) -> None:
        for collector_job, weekly_job in (
            ("obs-collector", "mirror-weekly"),
            ("obs-collector-luca", "mirror-weekly-luca"),
        ):
            collector_calendar = _parser(
                SYSTEMD / f"ai.caty.pgl.{collector_job}.timer"
            )["Timer"]["OnCalendar"]
            weekly_calendar = _parser(
                SYSTEMD / f"ai.caty.pgl.{weekly_job}.timer"
            )["Timer"]["OnCalendar"]
            headroom = _calendar_minutes(weekly_calendar) - _calendar_minutes(
                collector_calendar
            )
            self.assertGreaterEqual(headroom, MIN_HEADROOM_MINUTES)

    def test_host_authored_nightly_timers_when_installed(self) -> None:
        installed: list[tuple[str, Path, str]] = []
        unit_root = Path.home() / ".config" / "systemd" / "user"
        for face_suffix, calendar in (("", "*-*-* 00:15:00"), ("-luca", "*-*-* 04:00:00")):
            path = unit_root / f"ai.caty.pgl.nightly{face_suffix}.timer"
            if path.is_file():
                installed.append((face_suffix, path, calendar))
        if not installed:
            self.skipTest("host-authored systemd nightly timers are not installed")
        for face_suffix, path, expected_calendar in installed:
            with self.subTest(timer=path.name):
                calendar = _parser(path)["Timer"]["OnCalendar"]
                self.assertEqual(calendar, expected_calendar)
                hour, minute = map(int, expected_calendar.rsplit(" ", 1)[1].split(":")[:2])
                headroom = (5 - (hour * 60 + minute)) % (24 * 60)
                self.assertGreaterEqual(headroom, MIN_HEADROOM_MINUTES)

    @unittest.skipIf(sys.platform == "darwin", "systemd-analyze is a Linux verification layer")
    def test_systemd_analyze_accepts_rendered_units_and_calendars(self) -> None:
        analyzer = shutil.which("systemd-analyze")
        self.assertIsNotNone(analyzer, "systemd-analyze is required on Linux")
        self.assertTrue(Path("/usr/bin/env").is_file(), "verify fixture needs a real ExecStart executable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rendered_paths: list[Path] = []
            calendars: list[str] = []
            for template in sorted(SYSTEMD.iterdir()):
                rendered, _, _, _ = _render(template, root)
                destination = root / template.name
                destination.write_text(rendered, encoding="utf-8")
                rendered_paths.append(destination)
                if template.suffix == ".timer":
                    calendars.append(_parser(destination)["Timer"]["OnCalendar"])
            verified = subprocess.run(
                [analyzer, "verify", *map(str, rendered_paths)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            for calendar in calendars:
                checked = subprocess.run(
                    [analyzer, "calendar", calendar],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)


if __name__ == "__main__":
    unittest.main()
