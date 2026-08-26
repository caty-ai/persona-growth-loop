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

    def test_divergent_installed_unit_paths_warn_and_keep_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            unit_root = home / ".config/systemd/user"
            unit_root.mkdir(parents=True)
            first = unit_root / "ai.caty.pgl.obs-collector.service"
            second = unit_root / "ai.caty.pgl.obs-collector-luca.service"
            first.write_text(
                "[Service]\nEnvironment=PATH=/first/bin:/usr/bin\n",
                encoding="utf-8",
            )
            second.write_text(
                "[Service]\nEnvironment=PATH=/second/bin:/usr/bin\n",
                encoding="utf-8",
            )
            result = preflight.resolve_unit_environment(
                python_bin=None,
                environ={},
                home=home,
                host_platform="linux",
            )
        self.assertEqual(result.path, "/first/bin:/usr/bin")
        self.assertEqual(result.source, str(first))
        self.assertEqual(len(result.warnings), 1)
        self.assertIn(first.name, result.warnings[0])
        self.assertIn(second.name, result.warnings[0])
        self.assertIn("diverge", result.warnings[0])

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
        self.assertIn(str(home.resolve()), result.detail)
        self.assertIn(str((home / "obslog").resolve()), result.detail)

    def test_pgl_home_missing_path_never_probes_above_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home" / "operator"
            home.mkdir(parents=True)
            missing = root / "missing" / "pgl"
            probe = mock.Mock(return_value=None)
            result = preflight.check_pgl_home(
                missing,
                mounts_text="",
                permission_probe=probe,
                home=home,
            )
        self.assertEqual(result.status, "UNDETERMINED")
        self.assertIn(str(missing), result.detail)
        self.assertIn("above HOME", result.detail)
        self.assertEqual(preflight.exit_code_for((result,)), 2)
        probe.assert_not_called()

    def test_pgl_home_missing_child_probes_home_and_names_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            target = home / "missing" / "pgl"
            probed: list[Path] = []
            result = preflight.check_pgl_home(
                target,
                mounts_text="",
                permission_probe=lambda path: probed.append(path),
                home=home,
            )
        self.assertEqual(result.status, "GREEN")
        self.assertEqual(probed, [home.resolve(), home.resolve()])
        self.assertIn(str(home.resolve()), result.detail)

    def test_pgl_home_existing_file_remains_red(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            pgl_home = home / "pgl-home"
            pgl_home.write_text("not a directory\n", encoding="utf-8")
            probe = mock.Mock(return_value=None)
            result = preflight.check_pgl_home(
                pgl_home,
                mounts_text="",
                permission_probe=probe,
                home=home,
            )
        self.assertEqual(result.status, "RED")
        self.assertIn("nearest existing ancestor is not a directory", result.detail)
        probe.assert_not_called()

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
    def test_host_labels_alpha_default_is_info_on_macos_and_warn_on_linux(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alpha = root / "alpha.json"
            _write_json(alpha, {"face": "alpha", "host": "mbp"})
            mac_alpha = preflight.check_host_labels(
                (alpha,),
                actual_hostname="machine",
                host_platform="darwin",
            )
            linux_alpha = preflight.check_host_labels(
                (alpha,),
                actual_hostname="linux-box",
                host_platform="linux",
            )
        self.assertEqual(mac_alpha.status, "INFO")
        self.assertIn("shipped default accepted", mac_alpha.detail)
        self.assertEqual(linux_alpha.status, "WARN")
        self.assertIn("choose a deployment label", linux_alpha.detail)

    def test_host_labels_alpha_operator_configured_is_info_on_both_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alpha = root / "alpha.json"
            _write_json(alpha, {"face": "alpha", "host": "chosen-label"})
            linux = preflight.check_host_labels(
                (alpha,),
                actual_hostname="linux-box",
                host_platform="linux",
            )
            darwin = preflight.check_host_labels(
                (alpha,),
                actual_hostname="mac-box",
                host_platform="darwin",
            )
        self.assertEqual(linux.status, "INFO")
        self.assertEqual(darwin.status, "INFO")
        self.assertIn("operator-configured", linux.detail)
        self.assertIn("operator-configured", darwin.detail)
        self.assertIn("chosen-label", linux.detail)
        self.assertIn("chosen-label", darwin.detail)
        self.assertIn("linux-box", linux.detail)
        self.assertIn("mac-box", darwin.detail)

    def test_host_labels_luca_is_remote_source_info_without_hostname(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            luca = root / "luca.json"
            _write_json(luca, {"face": "luca", "host": "custom-remote"})
            remote_luca = preflight.check_host_labels(
                (luca,),
                actual_hostname="unrelated-operator-host",
                host_platform="linux",
            )
        self.assertEqual(remote_luca.status, "INFO")
        self.assertIn("comparison skipped", remote_luca.detail)
        self.assertNotIn("unrelated-operator-host", remote_luca.detail)

    def test_host_labels_invalid_configs_are_red_and_exit_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = root / "malformed.json"
            malformed.write_text("{not json}\n", encoding="utf-8")
            missing_host = root / "missing-host.json"
            _write_json(missing_host, {"face": "alpha"})
            non_string_host = root / "non-string-host.json"
            _write_json(non_string_host, {"face": "alpha", "host": 42})

            malformed_result = preflight.check_host_labels(
                (malformed,),
                actual_hostname="machine",
                host_platform="linux",
            )
            missing_host_result = preflight.check_host_labels(
                (missing_host,),
                actual_hostname="machine",
                host_platform="linux",
            )
            non_string_host_result = preflight.check_host_labels(
                (non_string_host,),
                actual_hostname="machine",
                host_platform="linux",
            )
        self.assertEqual(malformed_result.status, "RED")
        self.assertEqual(preflight.exit_code_for((malformed_result,)), 1)
        self.assertEqual(missing_host_result.status, "RED")
        self.assertEqual(preflight.exit_code_for((missing_host_result,)), 1)
        self.assertEqual(non_string_host_result.status, "RED")
        self.assertEqual(preflight.exit_code_for((non_string_host_result,)), 1)

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

    def test_transcripts_root_green_empty_red_and_drvfs_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcripts = root / "transcripts"
            transcripts.mkdir()
            config = root / "growth.json"
            _write_json(config, {"transcripts_root": str(transcripts)})
            empty = preflight.check_transcripts_roots({"alpha": config}, mounts_text="")
            (transcripts / "session.jsonl").write_text("{}\n", encoding="utf-8")
            green = preflight.check_transcripts_roots({"alpha": config}, mounts_text="")
            _write_json(config, {"transcripts_root": str(root / "missing")})
            red = preflight.check_transcripts_roots({"alpha": config}, mounts_text="")
            _write_json(config, {"transcripts_root": "/mnt/c/transcripts"})
            PathMock = preflight.Path
            with mock.patch.object(PathMock, "is_dir", return_value=True), mock.patch.object(
                PathMock,
                "iterdir",
                return_value=iter((Path("session.jsonl"),)),
            ):
                warning = preflight.check_transcripts_roots(
                    {"alpha": config},
                    mounts_text="C: /mnt/c drvfs rw 0 0\n",
                )
        self.assertEqual(empty.status, "WARN")
        self.assertIn("no transcripts found", empty.detail)
        self.assertIn("Windows side", empty.detail)
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
        colon = preflight.check_no_spaces(
            {"PGL_REPO": "/repo", "PGL_HOME": "/home/pgl", "PYTHON_BIN": "/venv:bad/bin"}
        )
        self.assertEqual(green.status, "GREEN")
        self.assertEqual(red.status, "RED")
        self.assertEqual(colon.status, "RED")
        self.assertIn("colons", colon.detail)


class ExitCodeContractTests(unittest.TestCase):
    def test_exit_zero_when_all_results_are_determined_and_none_are_red(self) -> None:
        results = (
            preflight.CheckResult("green", "GREEN", "ok"),
            preflight.CheckResult("warn", "WARN", "advisory"),
            preflight.CheckResult("info", "INFO", "context"),
            preflight.CheckResult("skip", "SKIP", "not applicable"),
        )
        self.assertEqual(preflight.exit_code_for(results), 0)

    def test_exit_one_when_any_result_is_red(self) -> None:
        results = (
            preflight.CheckResult("unknown", "UNDETERMINED", "unknown"),
            preflight.CheckResult("failure", "RED", "failed"),
        )
        self.assertEqual(preflight.exit_code_for(results), 1)

    def test_exit_two_when_no_result_is_red_and_one_is_undetermined(self) -> None:
        results = (
            preflight.CheckResult("warning", "WARN", "advisory"),
            preflight.CheckResult("unknown", "UNDETERMINED", "unknown"),
        )
        self.assertEqual(preflight.exit_code_for(results), 2)


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
