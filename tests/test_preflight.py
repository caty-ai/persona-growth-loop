from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]


def _load_preflight_module():
    path = REPO / "bin" / "pgl-preflight"
    loader = importlib.machinery.SourceFileLoader("pgl_preflight_tests", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("cannot load bin/pgl-preflight")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


preflight = _load_preflight_module()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class UnitEnvironmentResolutionTests(unittest.TestCase):
    def test_installed_systemd_unit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            unit_root = home / ".config/systemd/user"
            unit_root.mkdir(parents=True)
            service = unit_root / "ai.caty.pgl.obs-collector.service"
            service.write_text(
                "[Service]\nEnvironment=PGL_HOME=/tmp/pgl\nEnvironment=PATH=/venv/bin:/usr/bin\n",
                encoding="utf-8",
            )
            result = preflight.resolve_unit_environment(
                python_bin=None,
                environ={},
                home=home,
                host_platform="linux",
            )
        self.assertEqual(result.mode, "installed-unit")
        self.assertEqual(result.path, "/venv/bin:/usr/bin")
        self.assertEqual(result.source, str(service))

    def test_installed_launchd_unit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            unit_root = home / "Library/LaunchAgents"
            unit_root.mkdir(parents=True)
            plist_path = unit_root / "ai.caty.pgl.obs-collector.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump(
                    {"EnvironmentVariables": {"PATH": "/unit/bin:/usr/bin"}},
                    handle,
                )
            result = preflight.resolve_unit_environment(
                python_bin=None,
                environ={},
                home=home,
                host_platform="darwin",
            )
        self.assertEqual(result.mode, "installed-unit")
        self.assertEqual(result.path, "/unit/bin:/usr/bin")

    def test_prospective_mode_cli_overrides_installed_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            unit_root = home / ".config/systemd/user"
            unit_root.mkdir(parents=True)
            (unit_root / "ai.caty.pgl.obs-collector.service").write_text(
                "[Service]\nEnvironment=PATH=/installed/bin:/usr/bin\n",
                encoding="utf-8",
            )
            result = preflight.resolve_unit_environment(
                python_bin="/prospective/bin",
                environ={"PGL_PYTHON_BIN": "/environment/bin"},
                home=home,
                host_platform="linux",
            )
        self.assertEqual(result.mode, "prospective")
        self.assertEqual(result.source, "--python-bin")
        self.assertEqual(result.path, "/prospective/bin" + preflight.SYSTEM_PATH_SUFFIX)

    def test_prospective_mode_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = preflight.resolve_unit_environment(
                python_bin=None,
                environ={"PGL_PYTHON_BIN": "/environment/bin"},
                home=Path(temporary),
                host_platform="linux",
            )
        self.assertEqual(result.mode, "prospective")
        self.assertEqual(result.source, "PGL_PYTHON_BIN")

    def test_undetermined_mode_does_not_claim_a_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = preflight.resolve_unit_environment(
                python_bin=None,
                environ={},
                home=Path(temporary),
                host_platform="linux",
            )
        result = preflight.check_ucd(environment)
        self.assertEqual(environment.mode, "undetermined")
        self.assertEqual(result.status, "UNDETERMINED")
        self.assertIn("supply --python-bin", result.detail)


class UcdCheckTests(unittest.TestCase):
    def test_green_runtime_reports_resolved_executable_verbatim(self) -> None:
        environment = preflight.UnitEnvironment("prospective", "/unit/path", "test")
        result = preflight.check_ucd(
            environment,
            probe=lambda path: preflight.PythonRuntime(
                "/exact/venv/bin/python3",
                "3.14.9",
                "16.0.0",
            ),
        )
        self.assertEqual(result.status, "GREEN")
        self.assertIn("/exact/venv/bin/python3", result.detail)
        self.assertIn("observed unidata_version '16.0.0'", result.detail)

    def test_red_runtime_quotes_observed_and_required_ucd(self) -> None:
        environment = preflight.UnitEnvironment("installed-unit", "/unit/path", "test")
        result = preflight.check_ucd(
            environment,
            probe=lambda path: preflight.PythonRuntime("/wrong/python3", "3.12.0", "15.0.0"),
        )
        self.assertEqual(result.status, "RED")
        self.assertIn("CPython 3.14.x (UCD 16.0.0)", result.detail)
        self.assertIn("observed unidata_version '15.0.0', required '16.0.0'", result.detail)

    def test_false_green_regression_uses_unit_path_not_test_interpreter(self) -> None:
        self.assertEqual(
            unicodedata.unidata_version,
            preflight.IGNORABLE_CORPUS_UNICODE_VERSION,
            "the test process must itself be on the admitted UCD",
        )
        with tempfile.TemporaryDirectory() as temporary:
            fake_bin = Path(temporary)
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/bin/sh\nprintf '%s\\n' "
                "'{\"executable\":\"/fake/unit/python3\",\"python_version\":\"3.14.0\",\"ucd_version\":\"0.0.0\"}'\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            environment = preflight.UnitEnvironment(
                "installed-unit",
                str(fake_bin),
                "fake service",
            )
            result = preflight.check_ucd(
                environment,
            )
        self.assertEqual(result.status, "RED")
        self.assertIn("/fake/unit/python3", result.detail)
        self.assertIn("observed unidata_version '0.0.0'", result.detail)

    def test_missing_python_is_red_in_determined_mode(self) -> None:
        environment = preflight.UnitEnvironment("prospective", "/does/not/exist", "test")
        result = preflight.check_ucd(environment)
        self.assertEqual(result.status, "RED")

    def test_ambient_line_is_clearly_labelled(self) -> None:
        line = preflight.ambient_ucd_line(
            {"PATH": "/ambient"},
            probe=lambda path: preflight.PythonRuntime("/ambient/python3", "3.14.0", "16.0.0"),
        )
        self.assertIn("ucd ambient-shell", line)
        self.assertIn("/ambient/python3", line)


class PathAndFilesystemCheckTests(unittest.TestCase):
    def test_paths_green_imported_agreement_for_luca_and_alpha_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "pgl"
            alpha = root / "alpha.json"
            luca = root / "luca.json"
            _write_json(alpha, {"obs_root": str(home)})
            _write_json(luca, {})
            calls: list[tuple[Path, Path]] = []

            def agreement(pgl_home: Path, config: Path):
                calls.append((pgl_home, config))
                return pgl_home, pgl_home / "state/luca-verify-sessions.jsonl"

            result = preflight.check_paths(home, alpha, luca, agreement_check=agreement)
        self.assertEqual(result.status, "GREEN")
        self.assertEqual(calls, [(home.resolve(), luca)])

    def test_paths_red_when_alpha_obs_root_disagrees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alpha = root / "alpha.json"
            luca = root / "luca.json"
            _write_json(alpha, {"obs_root": str(root / "other")})
            _write_json(luca, {})
            result = preflight.check_paths(
                root / "pgl",
                alpha,
                luca,
                agreement_check=lambda home, config: (home, home / "ledger"),
            )
        self.assertEqual(result.status, "RED")

    def test_paths_red_when_imported_luca_agreement_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "pgl"
            alpha = root / "alpha.json"
            luca = root / "luca.json"
            _write_json(alpha, {"obs_root": str(home)})
            _write_json(luca, {})

            def reject(home: Path, config: Path):
                raise RuntimeError("luca disagreement")

            result = preflight.check_paths(home, alpha, luca, agreement_check=reject)
        self.assertEqual(result.status, "RED")
        self.assertIn("luca disagreement", result.detail)

    def test_pgl_home_green_probes_home_and_obslog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "pgl"
            home.mkdir()
            (home / "obslog").mkdir()
            probed: list[Path] = []

            def probe(path: Path):
                probed.append(path)
                return None

            result = preflight.check_pgl_home(
                home,
                mounts_text="",
                permission_probe=probe,
            )
        self.assertEqual(result.status, "GREEN")
        self.assertEqual(probed, [home.resolve(), (home / "obslog").resolve()])

    def test_pgl_home_red_on_drvfs(self) -> None:
        result = preflight.check_pgl_home(
            Path("/mnt/c/pgl"),
            mounts_text="C: /mnt/c drvfs rw 0 0\n",
            permission_probe=lambda path: None,
        )
        self.assertEqual(result.status, "RED")
        self.assertIn("drvfs", result.detail)

    def test_pgl_home_red_on_permission_lie(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = preflight.check_pgl_home(
                Path(temporary),
                mounts_text="",
                permission_probe=lambda path: "0600 read back as 0777",
            )
        self.assertEqual(result.status, "RED")

    def test_logs_green_and_red(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            logs = home / "logs"
            logs.mkdir(mode=0o700)
            green = preflight.check_logs(home, write_probe=lambda path: None)
            logs.chmod(0o755)
            red = preflight.check_logs(home, write_probe=lambda path: None)
        self.assertEqual(green.status, "GREEN")
        self.assertEqual(red.status, "RED")
        self.assertIn("required 0700", red.detail)

    def test_permission_probe_removes_its_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = set(root.iterdir())
            self.assertIsNone(preflight._probe_permissions(root))
            self.assertEqual(set(root.iterdir()), before)


class ConfigurationCheckTests(unittest.TestCase):
    def test_host_label_green_warn_and_red(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alpha = root / "alpha.json"
            luca = root / "luca.json"
            _write_json(alpha, {"face": "alpha", "host": "mbp"})
            _write_json(luca, {"face": "luca", "host": "machine"})
            green = preflight.check_host_labels(
                (alpha, luca),
                actual_hostname="machine",
                host_platform="darwin",
            )
            warning = preflight.check_host_labels(
                (alpha,),
                actual_hostname="linux-box",
                host_platform="linux",
            )
            _write_json(luca, {"face": "luca", "host": 42})
            red = preflight.check_host_labels(
                (luca,),
                actual_hostname="machine",
                host_platform="linux",
            )
        self.assertEqual(green.status, "GREEN")
        self.assertEqual(warning.status, "WARN")
        self.assertEqual(red.status, "RED")

    def test_soul_alert_green_red_skip_and_undetermined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = root / "alert"
            command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
            config = root / "growth.json"
            _write_json(config, {"soul_alert_argv": ["alert"]})
            prospective = preflight.UnitEnvironment("prospective", str(root), "test")
            green = preflight.check_soul_alert("alpha", config, prospective)
            _write_json(config, {"soul_alert_argv": ["missing"]})
            red = preflight.check_soul_alert("alpha", config, prospective)
            _write_json(config, {"soul_alert_argv": []})
            skipped = preflight.check_soul_alert("alpha", config, prospective)
            _write_json(config, {"soul_alert_argv": ["alert"]})
            undetermined = preflight.check_soul_alert(
                "alpha",
                config,
                preflight.UnitEnvironment("undetermined", "/usr/bin", "test"),
            )
        self.assertEqual(green.status, "GREEN")
        self.assertEqual(red.status, "RED")
        self.assertEqual(skipped.status, "SKIP")
        self.assertEqual(undetermined.status, "UNDETERMINED")

    def test_transcripts_root_green_red_and_drvfs_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcripts = root / "transcripts"
            transcripts.mkdir()
            config = root / "growth.json"
            _write_json(config, {"transcripts_root": str(transcripts)})
            green = preflight.check_transcripts_roots({"alpha": config}, mounts_text="")
            _write_json(config, {"transcripts_root": str(root / "missing")})
            red = preflight.check_transcripts_roots({"alpha": config}, mounts_text="")
            _write_json(config, {"transcripts_root": "/mnt/c/transcripts"})
            PathMock = preflight.Path
            with mock.patch.object(PathMock, "is_dir", return_value=True):
                warning = preflight.check_transcripts_roots(
                    {"alpha": config},
                    mounts_text="C: /mnt/c drvfs rw 0 0\n",
                )
        self.assertEqual(green.status, "GREEN")
        self.assertEqual(red.status, "RED")
        self.assertEqual(warning.status, "WARN")

    def test_timezone_green_and_warn(self) -> None:
        green = preflight.check_timezone(timezone_value=("Asia/Tokyo", 9 * 60 * 60))
        warning = preflight.check_timezone(timezone_value=("UTC", 0))
        self.assertEqual(green.status, "GREEN")
        self.assertEqual(warning.status, "WARN")
        self.assertIn("host-local time", warning.detail)

    def test_no_spaces_green_and_red(self) -> None:
        green = preflight.check_no_spaces(
            {"PGL_REPO": "/repo", "PGL_HOME": "/home/pgl", "PYTHON_BIN": "/venv/bin"}
        )
        red = preflight.check_no_spaces(
            {"PGL_REPO": "/repo with spaces", "PGL_HOME": "/home/pgl"}
        )
        self.assertEqual(green.status, "GREEN")
        self.assertEqual(red.status, "RED")


class SchedulerCheckTests(unittest.TestCase):
    def test_scheduler_green_when_manager_and_linger_are_ready(self) -> None:
        responses = iter((_completed(), _completed(stdout="yes\n")))
        result = preflight.check_scheduler(
            host_platform="linux",
            runner=lambda *args, **kwargs: next(responses),
            uid=1000,
        )
        self.assertEqual(result.status, "GREEN")

    def test_scheduler_warns_when_linger_is_disabled(self) -> None:
        responses = iter((_completed(), _completed(stdout="no\n")))
        result = preflight.check_scheduler(
            host_platform="linux",
            runner=lambda *args, **kwargs: next(responses),
            uid=1000,
        )
        self.assertEqual(result.status, "WARN")

    def test_scheduler_red_when_user_manager_is_unreachable(self) -> None:
        result = preflight.check_scheduler(
            host_platform="linux",
            runner=lambda *args, **kwargs: _completed(1, stderr="no bus"),
            uid=1000,
        )
        self.assertEqual(result.status, "RED")
        self.assertIn("systemd=true", result.detail)

    def test_scheduler_skips_on_macos(self) -> None:
        result = preflight.check_scheduler(host_platform="darwin")
        self.assertEqual(result.status, "SKIP")


if __name__ == "__main__":
    unittest.main()
