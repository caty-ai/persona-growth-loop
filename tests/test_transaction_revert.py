from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from applier import apply as apply_module
from applier.apply import ApplyError, commit_state
from growthlane.faces import get_profile
from growthlane.ledger import dump_ledger, empty_ledger, load_ledger, new_phrase
from growthlane.notify import Digest
from growthlane.soul import SoulError, write_manifest


class TransactionRevertTests(unittest.TestCase):
    def _fixture(self, root: Path, face: str) -> tuple[Path, Path, object, dict[str, str]]:
        pgl_home = root / "home"
        profile = get_profile(face)
        overlay = pgl_home / "faces" / "alpha" if face == "alpha" else root / "luca-pack"
        if face == "luca":
            for rel in ("persona-engine/catalogs/overlay", "growth"):
                (overlay / rel).mkdir(parents=True, exist_ok=True)
            staging = root / "luca-staging"
            staging.mkdir()
            (staging / "install.yml").write_text("version: 1\n", encoding="utf-8")
            config: dict[str, str] = {
                "overlay_home_root": str(overlay),
                "staging_root": str(staging),
                "display_name": "利用者",
            }
            soul = overlay / "persona-engine" / "catalogs" / "soul.txt"
            soul.write_text("frozen\n", encoding="utf-8")
        else:
            home_patch = mock.patch.dict(os.environ, {"HOME": str(root)})
            home_patch.start()
            self.addCleanup(home_patch.stop)
            overlay.mkdir(parents=True)
            config = {"display_name": "利用者"}
            soul = root / ".claude" / "CLAUDE.md"
            soul.parent.mkdir()
            soul.write_text(
                "### Identity (アルファ)\nidentity\n"
                "### Warmth Persona Core v1\nwarmth\n"
                "### F. 関係の記憶\nmemory\n"
                "~/.persona-growth-loop/faces/alpha/overlay.md\n",
                encoding="utf-8",
            )
        (overlay / profile.ledger_path).write_bytes(dump_ledger(empty_ledger(face)))
        (overlay / profile.blocklist_path).write_bytes(b"")
        for rel in profile.render_files.values():
            (overlay / rel).write_bytes(b"")
        subprocess.run(["git", "init", "-q"], cwd=overlay, check=True)
        subprocess.run(["git", "config", "user.name", "PGL Test"], cwd=overlay, check=True)
        subprocess.run(["git", "config", "user.email", "pgl@example.invalid"], cwd=overlay, check=True)
        subprocess.run(["git", "add", "-A"], cwd=overlay, check=True)
        subprocess.run(["git", "commit", "-qm", "bootstrap"], cwd=overlay, check=True)
        write_manifest(profile, pgl_home, config, [soul])
        (pgl_home / "reports" / "weekly").mkdir(parents=True)
        today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        (pgl_home / "reports" / "weekly" / f"latest-{face}.json").write_text(
            json.dumps({"generated_at": today}), encoding="utf-8"
        )
        (pgl_home / "gates.yml").write_text(
            f"cp2_in_force: true\ndecided_by: owner\nref: cp2\nfaces:\n  {face}:\n    cp3_go: true\n    decided_by: owner\n    ref: cp3\n"
        )
        return pgl_home, overlay, profile, config

    def test_persona_failure_detail_is_truncated_to_500_bytes(self) -> None:
        completed = subprocess.CompletedProcess(
            ["persona", "build"], 7, b"", b"x" * 600
        )
        with mock.patch("applier.apply.subprocess.run", return_value=completed):
            with self.assertRaises(ApplyError) as raised:
                apply_module._run_persona("build", Path("."))
        message = str(raised.exception)
        self.assertIn("x" * 500, message)
        self.assertNotIn("x" * 501, message)
        self.assertTrue(message.endswith(" [truncated]"), message)

    def test_engine_build_soul_mismatch_restores_all_allowlist_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pgl_home = root / "home"
            overlay = root / "luca-pack"
            for rel in ("persona-engine/catalogs/overlay", "growth"):
                (overlay / rel).mkdir(parents=True, exist_ok=True)
            profile = get_profile("luca")
            baseline = empty_ledger("luca")
            (overlay / profile.ledger_path).write_bytes(dump_ledger(baseline))
            (overlay / profile.blocklist_path).write_bytes(b"")
            for rel in profile.render_files.values():
                (overlay / rel).write_bytes(b"")
            subprocess.run(["git", "init", "-q"], cwd=overlay, check=True)
            subprocess.run(["git", "config", "user.name", "PGL Test"], cwd=overlay, check=True)
            subprocess.run(["git", "config", "user.email", "pgl@example.invalid"], cwd=overlay, check=True)
            subprocess.run(["git", "add", "--", *profile.allowlist], cwd=overlay, check=True)
            subprocess.run(["git", "commit", "-qm", "bootstrap"], cwd=overlay, check=True)
            soul = overlay / "persona-engine" / "catalogs" / "soul.txt"
            soul.write_text("frozen\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "persona-engine/catalogs/soul.txt"], cwd=overlay, check=True)
            subprocess.run(["git", "commit", "-qm", "seed soul"], cwd=overlay, check=True)
            staging = root / "luca-staging"
            staging.mkdir()
            (staging / "install.yml").write_text("version: 1\n", encoding="utf-8")
            config = {
                "overlay_home_root": str(overlay),
                "staging_root": str(staging),
                "display_name": "利用者",
            }
            write_manifest(profile, pgl_home, config, [soul])
            (pgl_home / "reports" / "weekly").mkdir(parents=True)
            (pgl_home / "reports" / "weekly" / "latest-luca.json").write_text(
                json.dumps({"generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()})
            )
            (pgl_home / "gates.yml").write_text(
                "cp2_in_force: true\ndecided_by: owner\nref: cp2\nfaces:\n  luca:\n    cp3_go: true\n    decided_by: owner\n    ref: cp3\n"
            )
            desired = empty_ledger("luca")
            desired["phrases"].append(new_phrase("p-0001", "なるほどだね", {"first_seen": "2026-08-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0}))
            original = {rel: (overlay / rel).read_bytes() for rel in profile.allowlist}
            fake_bin = root / "bin"
            fake_bin.mkdir()
            persona = fake_bin / "persona"
            persona.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = build ]; then\n"
                "  mkdir -p build\n"
                "  printf 'changed\\n' > \"$PGL_TEST_SOUL\"\n"
                "  printf '{\"content_hash\":\"%064d\"}\\n' 0 > build/manifest.json\n"
                "  exit 0\n"
                "fi\n"
                "printf '{\"ok\":true,\"issues\":[]}\\n'\n",
                encoding="utf-8",
            )
            persona.chmod(0o755)
            digest = Digest(pgl_home, "2026-08-01")
            with mock.patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}", "PGL_TEST_SOUL": str(soul)}):
                with self.assertRaises(SoulError):
                    commit_state(profile, pgl_home, config, "2026-08-01", desired, [], digest)
            self.assertEqual({rel: (overlay / rel).read_bytes() for rel in profile.allowlist}, original)
            status = subprocess.run(
                ["git", "status", "--porcelain", "--", *profile.allowlist],
                cwd=overlay,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout
            self.assertEqual(status, "")
            self.assertIn("[RED]", digest.path.read_text(encoding="utf-8"))

    def test_git_hooks_are_disabled_for_applier_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pgl_home, overlay, profile, config = self._fixture(root, "alpha")
            hook = overlay / ".git" / "hooks" / "pre-commit"
            hook.write_text("#!/bin/sh\nprintf fired > OUTSIDE_ALLOWLIST.txt\n", encoding="utf-8")
            hook.chmod(0o755)
            subprocess.run(["git", "config", "core.hooksPath", ".git/hooks"], cwd=overlay, check=True)
            desired = empty_ledger("alpha")
            desired["phrases"].append(
                new_phrase("p-0001", "なるほどだね", {"first_seen": "2026-08-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0})
            )
            result = commit_state(profile, pgl_home, config, "2026-08-01", desired, [], Digest(pgl_home, "2026-08-01"))
            self.assertTrue(result.changed)
            self.assertFalse((overlay / "OUTSIDE_ALLOWLIST.txt").exists())

    def test_pre_staged_non_allowlist_file_survives_commit_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pgl_home, overlay, profile, config = self._fixture(root, "alpha")
            outside = overlay / "operator.txt"
            outside.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "operator.txt"], cwd=overlay, check=True)
            subprocess.run(["git", "commit", "-qm", "track operator file"], cwd=overlay, check=True)
            outside.write_text("operator staged\n", encoding="utf-8")
            subprocess.run(["git", "add", "operator.txt"], cwd=overlay, check=True)
            staged_payload = outside.read_bytes()
            desired = empty_ledger("alpha")
            desired["phrases"].append(
                new_phrase("p-0001", "なるほどだね", {"first_seen": "2026-08-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0})
            )
            result = commit_state(
                profile, pgl_home, config, "2026-08-01", desired, [], Digest(pgl_home, "2026-08-01")
            )
            self.assertTrue(result.changed)
            self.assertEqual(outside.read_bytes(), staged_payload)
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"], cwd=overlay, text=True, stdout=subprocess.PIPE, check=True
            ).stdout.splitlines()
            self.assertEqual(staged, ["operator.txt"])

    def test_post_commit_file_list_mismatch_reverts_only_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pgl_home, overlay, profile, config = self._fixture(root, "alpha")
            original_head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=overlay, text=True, stdout=subprocess.PIPE, check=True
            ).stdout.strip()
            original = {rel: (overlay / rel).read_bytes() for rel in profile.allowlist}
            desired = empty_ledger("alpha")
            desired["phrases"].append(
                new_phrase(
                    "p-0001",
                    "なるほどだね",
                    {"first_seen": "2026-08-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
                )
            )
            real_git = apply_module._git

            def mismatched_show(home: Path, *args: str, **kwargs: object):
                completed = real_git(home, *args, **kwargs)
                if args[:3] == ("show", "--name-only", "--pretty=format:"):
                    return subprocess.CompletedProcess(completed.args, 0, b"unexpected.txt\n", b"")
                return completed

            with mock.patch("applier.apply._git", side_effect=mismatched_show):
                with self.assertRaisesRegex(ApplyError, "commit file list"):
                    commit_state(
                        profile,
                        pgl_home,
                        config,
                        "2026-08-01",
                        desired,
                        [],
                        Digest(pgl_home, "2026-08-01"),
                    )
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=overlay, text=True, stdout=subprocess.PIPE, check=True
                ).stdout.strip(),
                original_head,
            )
            self.assertEqual({rel: (overlay / rel).read_bytes() for rel in profile.allowlist}, original)

    def test_lint_violations_drop_from_render_and_demote_with_rule_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pgl_home, overlay, profile, config = self._fixture(root, "alpha")
            desired = empty_ledger("alpha")
            for index in range(41):
                phrase = new_phrase(
                    f"p-{index + 1:04d}",
                    "commitしよう",
                    {"first_seen": "2026-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
                )
                phrase["state"] = "adopted"
                phrase["staged_at"] = "2026-01-01"
                desired["phrases"].append(phrase)
            digest = Digest(pgl_home, "2026-08-01")
            result = commit_state(profile, pgl_home, config, "2026-08-01", desired, [], digest)
            self.assertTrue(result.changed)
            self.assertEqual((overlay / "overlay.md").read_bytes(), b"")
            persisted = load_ledger(overlay / profile.ledger_path, "alpha")
            self.assertEqual(len(persisted["phrases"]), 41)
            self.assertTrue(all(item["state"] == "demoted" for item in persisted["phrases"]))
            self.assertEqual(
                persisted["phrases"][0]["history"][-1]["proposal_id"],
                "lint-rules:privilege_vocab",
            )
            self.assertIn("[RED] alpha: render dropped p-0001 rules=privilege_vocab", digest.path.read_text())

    def test_engine_build_success_mutation_missing_hash_and_doctor_failure(self) -> None:
        for mode in (
            "success",
            "mutate",
            "missing",
            "doctor-fail",
            "doctor-not-ok",
            "doctor-non-json",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                pgl_home, overlay, profile, config = self._fixture(root, "luca")
                desired = empty_ledger("luca")
                phrase = new_phrase(
                    "p-0001", "なるほどだね", {"first_seen": "2026-08-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0}
                )
                phrase["state"] = "staged"
                phrase["staged_at"] = "2026-08-01"
                desired["phrases"].append(phrase)
                original = {rel: (overlay / rel).read_bytes() for rel in profile.allowlist}
                fake_bin = root / "bin"
                fake_bin.mkdir()
                persona = fake_bin / "persona"
                build_manifest = (
                    "printf '{}\\n' > build/manifest.json"
                    if mode == "missing"
                    else "printf '{\"content_hash\":\"%064d\"}\\n' 0 > build/manifest.json"
                )
                mutation = (
                    "printf smuggled > \"$PGL_TEST_OVERLAY/persona-engine/catalogs/overlay/candidates.txt\""
                    if mode == "mutate"
                    else ":"
                )
                if mode == "doctor-fail":
                    doctor = "printf '{\"ok\":true,\"issues\":[]}\\n'; exit 9"
                elif mode == "doctor-not-ok":
                    doctor = "printf '{\"ok\":false}\\n'"
                elif mode == "doctor-non-json":
                    doctor = "printf 'not-json\\n'"
                else:
                    doctor = "printf '{\"ok\":true,\"issues\":[]}\\n'"
                persona.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = build ]; then\n"
                    "  mkdir -p build\n"
                    f"  {mutation}\n"
                    f"  {build_manifest}\n"
                    "  printf 'build complete\\n'\n"
                    "  exit 0\n"
                    "fi\n"
                    f"{doctor}\n",
                    encoding="utf-8",
                )
                persona.chmod(0o755)
                digest = Digest(pgl_home, "2026-08-01")
                with mock.patch.dict(
                    os.environ,
                    {
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "PGL_TEST_OVERLAY": str(overlay),
                    },
                ):
                    if mode == "success":
                        result = commit_state(profile, pgl_home, config, "2026-08-01", desired, [], digest)
                        self.assertTrue(result.changed)
                        self.assertEqual(result.content_hash, "0" * 64)
                        self.assertIn(result.tag, subprocess.run(["git", "tag", "--list"], cwd=overlay, text=True, stdout=subprocess.PIPE, check=True).stdout.splitlines())
                    else:
                        with self.assertRaises(ApplyError):
                            commit_state(profile, pgl_home, config, "2026-08-01", desired, [], digest)
                        self.assertEqual({rel: (overlay / rel).read_bytes() for rel in profile.allowlist}, original)
                        status = subprocess.run(
                            ["git", "status", "--porcelain", "--", *profile.allowlist],
                            cwd=overlay,
                            text=True,
                            stdout=subprocess.PIPE,
                            check=True,
                        ).stdout
                        self.assertEqual(status, "")
                        self.assertIn("[RED]", digest.path.read_text(encoding="utf-8"))

    def test_engine_build_uses_manifest_hash_instead_of_stdout_decoy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _pgl_home, overlay, profile, config = self._fixture(root, "luca")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            persona = fake_bin / "persona"
            persona.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = build ]; then\n"
                "  mkdir -p build\n"
                "  printf '%064d\\n' 1\n"
                "  printf '{\"content_hash\":\"%064d\"}\\n' 0 > build/manifest.json\n"
                "  exit 0\n"
                "fi\n"
                "printf '{\"ok\":true,\"issues\":[]}\\n'\n",
                encoding="utf-8",
            )
            persona.chmod(0o755)
            with mock.patch.dict(
                os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"}
            ):
                content_hash = apply_module._build(profile, overlay, {}, config)
            self.assertEqual(content_hash, "0" * 64)

    def test_engine_build_mirrors_source_to_staging_and_keeps_outputs_out_of_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _pgl_home, overlay, profile, config = self._fixture(root, "luca")
            staging = Path(config["staging_root"])
            source_marker = overlay / "persona-engine" / "catalogs" / "source-marker.txt"
            source_marker.write_text("first\n", encoding="utf-8")
            stale = staging / "pack" / "stale.txt"
            stale.parent.mkdir()
            stale.write_text("delete me\n", encoding="utf-8")
            orphan_directory = staging / ".pack-sync-orphan"
            orphan_directory.mkdir()
            (orphan_directory / "partial.txt").write_text("partial\n", encoding="utf-8")
            orphan_file = staging / ".pack-sync-file"
            orphan_file.write_text("partial\n", encoding="utf-8")
            near_miss = staging / ".pack-sync"
            near_miss.write_text("keep\n", encoding="utf-8")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            calls = root / "persona-calls.txt"
            persona = fake_bin / "persona"
            persona.write_text(
                "#!/bin/sh\n"
                "printf '%s:%s\\n' \"$1\" \"$PWD\" >> \"$PGL_TEST_CALLS\"\n"
                "if [ \"$1\" = build ]; then\n"
                "  mkdir -p build\n"
                "  cp pack/catalogs/source-marker.txt build/source-marker.txt\n"
                "  if grep -q second pack/catalogs/source-marker.txt; then\n"
                "    printf '{\"content_hash\":\"%064d\"}\\n' 1 > build/manifest.json\n"
                "  else\n"
                "    printf '{\"content_hash\":\"%064d\"}\\n' 0 > build/manifest.json\n"
                "  fi\n"
                "  exit 0\n"
                "fi\n"
                "test -f build/source-marker.txt || exit 1\n"
                "printf '{\"ok\":true,\"issues\":[]}\\n'\n",
                encoding="utf-8",
            )
            persona.chmod(0o755)
            environment = {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "PGL_TEST_CALLS": str(calls),
            }
            with mock.patch.dict(os.environ, environment):
                first = apply_module._build(profile, overlay, {}, config)
                self.assertEqual(first, "0" * 64)
                self.assertEqual((staging / "pack/catalogs/source-marker.txt").read_text(), "first\n")
                self.assertEqual((staging / "build/source-marker.txt").read_text(), "first\n")
                self.assertFalse(stale.exists())
                self.assertFalse(orphan_directory.exists())
                self.assertFalse(orphan_file.exists())
                self.assertEqual(near_miss.read_text(encoding="utf-8"), "keep\n")
                self.assertFalse((overlay / "build").exists())

                source_marker.write_text("second\n", encoding="utf-8")
                source_before = {
                    path.relative_to(overlay / "persona-engine"): path.read_bytes()
                    for path in (overlay / "persona-engine").rglob("*")
                    if path.is_file()
                }
                second = apply_module._build(profile, overlay, {}, config)

            self.assertEqual(second, "0" * 63 + "1")
            self.assertEqual((staging / "pack/catalogs/source-marker.txt").read_text(), "second\n")
            self.assertEqual((staging / "build/source-marker.txt").read_text(), "second\n")
            self.assertEqual(
                {
                    path.relative_to(overlay / "persona-engine"): path.read_bytes()
                    for path in (overlay / "persona-engine").rglob("*")
                    if path.is_file()
                },
                source_before,
            )
            self.assertFalse((overlay / "build").exists())
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                [
                    f"build:{staging.resolve()}",
                    f"doctor:{staging.resolve()}",
                    f"build:{staging.resolve()}",
                    f"doctor:{staging.resolve()}",
                ],
            )

    def test_engine_staging_root_is_required_and_must_be_disjoint_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _pgl_home, overlay, profile, config = self._fixture(root, "luca")
            with self.assertRaisesRegex(ApplyError, "requires face config"):
                apply_module._build(profile, overlay, {})

            staging = Path(config["staging_root"])
            install = staging / "install.yml"
            install.unlink()
            with self.assertRaisesRegex(ApplyError, "staging install.yml missing"):
                apply_module._build(profile, overlay, {}, config)
            outside_install = root / "outside-install.yml"
            outside_install.write_text("version: 1\n", encoding="utf-8")
            install.symlink_to(outside_install)
            with self.assertRaisesRegex(ApplyError, "staging install.yml has unsafe shape"):
                apply_module._build(profile, overlay, {}, config)
            install.unlink()
            install.write_text("version: 1\n", encoding="utf-8")

            build = staging / "build"
            build.mkdir()

            def swap_build_for_file(_source: Path, _staging_root: Path) -> None:
                build.rmdir()
                build.write_text("unsafe\n", encoding="utf-8")

            with mock.patch(
                "applier.apply._replace_staging_pack", side_effect=swap_build_for_file
            ):
                with self.assertRaisesRegex(
                    ApplyError, "staging build directory has unsafe shape"
                ):
                    apply_module._build(profile, overlay, {}, config)
            build.unlink()

            missing = dict(config)
            missing.pop("staging_root")
            with self.assertRaisesRegex(ApplyError, "requires non-empty staging_root"):
                apply_module._build(profile, overlay, {}, missing)
            with mock.patch("applier.apply.write_guarded") as guarded_write:
                with self.assertRaisesRegex(ApplyError, "requires non-empty staging_root"):
                    commit_state(
                        profile,
                        _pgl_home,
                        missing,
                        "2026-08-01",
                        empty_ledger("luca"),
                        [],
                        Digest(_pgl_home, "2026-08-01"),
                    )
                guarded_write.assert_not_called()

            overlapping = dict(config, staging_root=str(overlay / "staging"))
            Path(overlapping["staging_root"]).mkdir()
            with self.assertRaisesRegex(ApplyError, "outside overlay_home_root"):
                apply_module._build(profile, overlay, {}, overlapping)

            (staging / "pack").write_text("unsafe\n", encoding="utf-8")
            with self.assertRaisesRegex(ApplyError, "staging pack has unsafe shape"):
                apply_module._build(profile, overlay, {}, config)

            alpha = get_profile("alpha")
            rendered = {"overlay.md": b"alpha\n"}
            self.assertEqual(
                apply_module._build(alpha, root, rendered, {}),
                apply_module._content_hash(alpha, rendered),
            )
