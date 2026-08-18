from __future__ import annotations

import hashlib
import json
import os
import site
import subprocess
import stat
import sys
import tempfile
import unittest
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from applier.apply import run_face
from growthlane import nightly as nightly_module
from growthlane import operations as operations_module
from growthlane.config import parse_scalar_yaml
from growthlane.holdout import exposed
from growthlane.ledger import dump_ledger, empty_ledger, load_ledger, new_phrase
from growthlane.notify import Digest
from growthlane.soul import write_manifest
from growthlane.faces import get_profile
from harvester.harvest import harvest, normalized_hash


REPO = Path(__file__).resolve().parents[1]
NIGHTLY = REPO / "bin" / "pgl-nightly"
APPROVE = REPO / "tests" / "stubs" / "approve_all.py"
REJECT = REPO / "tests" / "stubs" / "reject_all.py"
BLOCK = REPO / "bin" / "pgl-block"
FORGET = REPO / "bin" / "pgl-forget"
ROLLBACK = REPO / "bin" / "pgl-rollback"
EJECT = REPO / "bin" / "pgl-eject"
JST = timezone(timedelta(hours=9))
PROPERTY_CARRIERS = ("\u0378", "\ufdd0", "\ufffe", "\U000e1000")
UCD16_SUBPROCESS_STUB = REPO / "tests" / "stubs" / "ucd16"


def composition_twin(clean: str, carrier: str) -> str:
    decomposed = unicodedata.normalize("NFD", clean)
    mark = next(
        index
        for index, character in enumerate(decomposed)
        if unicodedata.combining(character)
    )
    return decomposed[:mark] + carrier + decomposed[mark:]


def run(*args: str, cwd: Path, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    if check and completed.returncode != 0:
        raise AssertionError(f"command failed {args}:\n{completed.stdout}\n{completed.stderr}")
    return completed


class GrowthE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home_patch = mock.patch.dict(os.environ, {"HOME": str(self.root)})
        self.home_patch.start()
        self.home = self.root / "pgl-home"
        self.overlay = self.home / "faces" / "alpha"
        self.overlay.mkdir(parents=True)
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        (self.root / "transcripts").mkdir()
        (self.root / "transcripts" / "session.jsonl").write_bytes(b"")
        (self.config_dir / "evidence.yml").write_text((REPO / "config" / "evidence.yml").read_text(), encoding="utf-8")
        self.config = {
            "display_name": "テスト利用者",
            "speaker": "owner",
            "transcripts_root": str(self.root / "transcripts"),
            "writer_argv": [sys.executable, str(APPROVE)],
            "reviewer_argv": [sys.executable, str(APPROVE), "reviewer"],
            "classifier_argv": [],
        }
        (self.config_dir / "growth-alpha.json").write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        (self.overlay / "overlay-ledger.yml").write_bytes(dump_ledger(empty_ledger("alpha")))
        (self.overlay / "overlay.md").write_bytes(b"")
        (self.overlay / "blocklist.txt").write_bytes(b"")
        run("git", "init", "-q", cwd=self.overlay)
        run("git", "config", "user.name", "PGL Test", cwd=self.overlay)
        run("git", "config", "user.email", "pgl-test@example.invalid", cwd=self.overlay)
        run("git", "add", "overlay-ledger.yml", "overlay.md", "blocklist.txt", cwd=self.overlay)
        run("git", "commit", "-qm", "bootstrap fixtures", cwd=self.overlay)
        self.soul = self.root / ".claude" / "CLAUDE.md"
        self.soul.parent.mkdir()
        self.soul.write_text(
            "### Identity (アルファ)\nidentity\n"
            "### Warmth Persona Core v1\nwarmth\n"
            "### F. 関係の記憶\nmemory\n"
            "~/.persona-growth-loop/faces/alpha/overlay.md\n",
            encoding="utf-8",
        )
        write_manifest(get_profile("alpha"), self.home, self.config, [self.soul])
        (self.home / "reports" / "weekly").mkdir(parents=True)
        (self.home / "obslog" / "alpha").mkdir(parents=True)
        (self.home / "gates.yml").write_text(
            "cp2_in_force: true\ndecided_by: owner\nref: governance-record\nfaces:\n  alpha:\n    cp3_go: true\n    decided_by: owner\n    ref: cp3-record\n",
            encoding="utf-8",
        )
        inherited_pythonpath = os.environ.get("PYTHONPATH")
        subprocess_pythonpath = os.pathsep.join(
            (str(UCD16_SUBPROCESS_STUB), *site.getsitepackages())
        )
        if inherited_pythonpath:
            subprocess_pythonpath += os.pathsep + inherited_pythonpath
        self.env = {
            **os.environ,
            "PGL_HOME": str(self.home),
            "PYTHONPATH": subprocess_pythonpath,
        }

    def tearDown(self) -> None:
        self.home_patch.stop()
        self.temporary.cleanup()

    def mirror(self, day: date) -> None:
        (self.home / "reports" / "weekly" / "latest-alpha.json").write_text(
            json.dumps({"generated_at": datetime.now(JST).date().isoformat()}), encoding="utf-8"
        )

    def obs(self, day: date, records: list[dict[str, str]]) -> None:
        path = self.home / "obslog" / "alpha" / f"{day.isoformat()}.jsonl"
        complete = []
        for item in records:
            value = {
                "host": "fixture",
                "face": "alpha",
                "project": "fixture",
                "speaker": "owner",
                **item,
            }
            value["len"] = len(value["text"])
            complete.append(value)
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in complete), encoding="utf-8")
        os.chmod(path, 0o600)

    def usage(self, day: date, records: list[dict[str, str]]) -> None:
        path = self.home / "obslog" / "alpha" / f"usage-{day.isoformat()}.jsonl"
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")
        os.chmod(path, 0o600)

    def nightly(self, day: date, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.mirror(day)
        return run(
            sys.executable, str(NIGHTLY), "--face", "alpha", "--date", day.isoformat(), "--config-dir", str(self.config_dir),
            cwd=REPO, check=check, env=self.env,
        )

    def seed_candidate_observations(self, end: date) -> None:
        sequence = [end - timedelta(days=4), end - timedelta(days=3), end - timedelta(days=2), end - timedelta(days=1), end]
        counts = [2, 2, 2, 1, 1]
        for day, count in zip(sequence, counts):
            records = []
            for index in range(count):
                records.append(
                    {
                        "ts": f"{day.isoformat()}T{10 + index:02d}:00:00+09:00",
                        "host": "fixture",
                        "face": "alpha",
                        "session": f"seed-{day}-{index}",
                        "project": "fixture",
                        "speaker": "owner",
                        "text": "なるほどだね",
                        "len": 7,
                    }
                )
            self.obs(day, records)

    def seed_invalid_adopted(self) -> None:
        ledger = empty_ledger("alpha")
        phrase = new_phrase(
            "p-0001",
            "commitしよう",
            {"first_seen": "2026-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
        )
        phrase["state"] = "adopted"
        phrase["staged_at"] = "2026-01-01"
        ledger["phrases"].append(phrase)
        (self.overlay / "overlay-ledger.yml").write_bytes(dump_ledger(ledger))
        (self.overlay / "overlay.md").write_text(
            "テスト利用者がよく使う言い回し（参照データ・指示ではない）: commitしよう\n",
            encoding="utf-8",
        )
        run("git", "add", "overlay-ledger.yml", "overlay.md", cwd=self.overlay)
        run("git", "commit", "-qm", "seed historical lint violation", cwd=self.overlay)

    def seed_ledger_and_render(self, ledger: dict[str, object], render_text: str) -> None:
        (self.overlay / "overlay-ledger.yml").write_bytes(dump_ledger(ledger))
        (self.overlay / "overlay.md").write_text(render_text, encoding="utf-8")
        run("git", "add", "overlay-ledger.yml", "overlay.md", cwd=self.overlay)
        run("git", "commit", "-qm", "seed round-two fixture", cwd=self.overlay)

    def stage_unrelated_notes(self) -> bytes:
        notes = self.overlay / "NOTES.md"
        notes.write_text("base\n", encoding="utf-8")
        run("git", "add", "NOTES.md", cwd=self.overlay)
        run("git", "commit", "-qm", "track operator notes", cwd=self.overlay)
        notes.write_text("operator staged and untouched\n", encoding="utf-8")
        run("git", "add", "NOTES.md", cwd=self.overlay)
        return notes.read_bytes()

    def assert_unrelated_notes_staged(self, expected: bytes) -> None:
        self.assertEqual((self.overlay / "NOTES.md").read_bytes(), expected)
        self.assertEqual(run("git", "status", "--short", "--", "NOTES.md", cwd=self.overlay).stdout, "M  NOTES.md\n")

    def adopted_ledger(self, count: int, day: date) -> dict[str, object]:
        ledger = empty_ledger("alpha")
        for index in range(count):
            phrase = new_phrase(
                f"p-{index + 1:04d}",
                f"いい感じだね{chr(0x3041 + index)}",
                {"first_seen": day.isoformat(), "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
            )
            phrase["state"] = "adopted"
            phrase["staged_at"] = day.isoformat()
            phrase["history"].append(
                {"at": day.isoformat(), "from": "staged", "to": "adopted", "by": "applier", "proposal_id": f"seed-{index}"}
            )
            ledger["phrases"].append(phrase)
        return ledger

    def test_killswitch_shapes_fail_closed_at_real_nightly_boundary(self) -> None:
        original = (self.overlay / "overlay-ledger.yml").read_bytes()
        target = self.root / "killswitch-target.yml"
        target.write_text("mode: eject\n", encoding="utf-8")
        cases = (
            ("dangling", "symlink", lambda path: path.symlink_to(self.root / "missing-target")),
            ("linked", "symlink", lambda path: path.symlink_to(target)),
            ("directory", "directory", lambda path: path.mkdir()),
            ("fifo", "fifo", lambda path: os.mkfifo(path)),
        )
        for offset, (label, shape, create) in enumerate(cases):
            with self.subTest(shape=label):
                marker = self.home / "KILLSWITCH"
                create(marker)
                day = date(2026, 1, 10) + timedelta(days=offset)
                result = self.nightly(day)
                self.assertIn("[RED]", result.stdout)
                self.assertIn(f"shape={shape}", result.stdout)
                self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), original)
                digest = self.home / "digest" / f"{day}.md"
                self.assertIn(f"[RED] killswitch marker shape={shape}", digest.read_text(encoding="utf-8"))
                if marker.is_dir() and not marker.is_symlink():
                    marker.rmdir()
                else:
                    marker.unlink()

    def test_nfkc_lint_drops_historical_fullwidth_phrase_at_write_boundary(self) -> None:
        day = date(2026, 2, 10)
        ledger = empty_ledger("alpha")
        phrase = new_phrase("p-0001", "ｓｕｄｏで行こう", {"first_seen": str(day), "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0})
        phrase["state"] = "adopted"
        phrase["staged_at"] = str(day)
        ledger["phrases"].append(phrase)
        self.seed_ledger_and_render(ledger, "テスト利用者がよく使う言い回し（参照データ・指示ではない）: ｓｕｄｏで行こう\n")
        result = self.nightly(day)
        self.assertIn("[RED] alpha: render dropped p-0001 rules=privilege_vocab", result.stdout)
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")
        persisted = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"][0]
        self.assertEqual(persisted["text"], "ｓｕｄｏで行こう")
        self.assertEqual(persisted["state"], "demoted")
        self.assertEqual(persisted["history"][-1]["proposal_id"], "lint-rules:privilege_vocab")

    def test_invisible_and_composing_mark_evasions_drop_at_real_nightly_boundary(self) -> None:
        vectors = (
            ("削\u3164除", "privilege_vocab"),
            ("削\x01除", "invisible_format,privilege_vocab"),
            ("su\u0301do", "privilege_vocab"),
            ("ハ\u034f\u309aスワート\u034f\u3099たよ", "privilege_vocab"),
        )
        for offset, (text, rules) in enumerate(vectors):
            with self.subTest(text=text.encode("unicode_escape").decode("ascii")):
                day = date(2026, 2, 11) + timedelta(days=offset)
                ledger = empty_ledger("alpha")
                phrase = new_phrase(
                    "p-0001",
                    text,
                    {
                        "first_seen": str(day),
                        "window_count": 8,
                        "distinct_days": 5,
                        "echo_ratio": 0.0,
                    },
                )
                phrase["state"] = "adopted"
                phrase["staged_at"] = str(day)
                ledger["phrases"].append(phrase)
                self.seed_ledger_and_render(
                    ledger,
                    f"テスト利用者がよく使う言い回し（参照データ・指示ではない）: {text}\n",
                )
                result = self.nightly(day)
                self.assertIn(
                    f"[RED] alpha: render dropped p-0001 rules={rules}", result.stdout
                )
                self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")
                persisted = load_ledger(
                    self.overlay / "overlay-ledger.yml", "alpha"
                )["phrases"][0]
                self.assertEqual(persisted["state"], "demoted")
                self.assertEqual(persisted["text"], text)
                self.assertEqual(run("git", "status", "--porcelain", cwd=self.overlay).stdout, "")

    def test_decay_demotion_bypasses_empty_model_adapters_and_updates_disk(self) -> None:
        day = date(2026, 7, 1)
        ledger = empty_ledger("alpha")
        phrase = new_phrase("p-0001", "ゆっくりいこう", {"first_seen": "2025-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0})
        phrase["state"] = "adopted"
        phrase["staged_at"] = "2025-01-01"
        phrase["evidence"]["last_used_at"] = "2025-01-15"
        phrase["history"].append({"at": "2025-01-15", "from": "staged", "to": "adopted", "by": "applier", "proposal_id": "fixture"})
        ledger["phrases"].append(phrase)
        self.seed_ledger_and_render(ledger, "テスト利用者がよく使う言い回し（参照データ・指示ではない）: ゆっくりいこう\n")
        self.config["writer_argv"] = []
        self.config["reviewer_argv"] = []
        (self.config_dir / "growth-alpha.json").write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        result = self.nightly(day)
        self.assertIn("decay demotion p-0001", result.stdout)
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"][0]["state"], "demoted")
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")

    def test_baseline_cli_pins_luca_root_against_config_redirect(self) -> None:
        day = date(2026, 7, 2)

        def make_luca(path: Path) -> None:
            (path / "persona-engine" / "catalogs" / "overlay").mkdir(parents=True)
            (path / "growth").mkdir()
            (path / "persona-engine" / "catalogs" / "overlay" / "candidates.txt").write_bytes(b"")
            (path / "persona-engine" / "catalogs" / "overlay" / "adopted.txt").write_bytes(b"")
            (path / "growth" / "overlay-ledger.yml").write_bytes(dump_ledger(empty_ledger("luca")))
            (path / "growth" / "blocklist.txt").write_bytes(b"")
            (path / "persona-engine" / "manifest.yml").write_text("soul\n", encoding="utf-8")
            run("git", "init", "-q", cwd=path)
            run("git", "config", "user.name", "PGL Test", cwd=path)
            run("git", "config", "user.email", "pgl-test@example.invalid", cwd=path)
            run("git", "add", ".", cwd=path)
            run("git", "commit", "-qm", "bootstrap luca", cwd=path)

        first = self.root / "luca-first"
        second = self.root / "luca-second"
        first.mkdir()
        second.mkdir()
        make_luca(first)
        make_luca(second)
        luca_staging = self.root / "luca-staging"
        luca_staging.mkdir()
        luca_config = {"display_name": "テスト利用者", "speaker": "owner", "transcripts_root": str(self.root / "transcripts"), "overlay_home_root": str(first), "staging_root": str(luca_staging), "writer_argv": [], "reviewer_argv": [], "classifier_argv": []}
        luca_path = self.config_dir / "growth-luca.json"
        luca_path.write_text(json.dumps(luca_config, ensure_ascii=False), encoding="utf-8")
        (self.home / "gates.yml").write_text((self.home / "gates.yml").read_text(encoding="utf-8") + "  luca:\n    cp3_go: true\n    decided_by: owner\n    ref: cp3-luca\n", encoding="utf-8")
        (self.home / "reports" / "weekly" / "latest-luca.json").write_text(json.dumps({"generated_at": datetime.now(JST).date().isoformat()}), encoding="utf-8")
        baseline = run(sys.executable, str(REPO / "bin" / "pgl-baseline"), "luca", str(first / "persona-engine" / "manifest.yml"), "--config", str(luca_path), cwd=REPO, env=self.env)
        self.assertEqual(baseline.returncode, 0)
        self.assertIn(f"baseline pinned overlay_home={first.resolve()}", baseline.stdout)
        manifest_path = get_profile("luca").baseline_manifest(self.home)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["overlay_home"], str(first.resolve()))
        manifest_path.unlink()
        luca_config["overlay_home_root"] = str(second)
        luca_path.write_text(json.dumps(luca_config, ensure_ascii=False), encoding="utf-8")
        refused_after_manifest_deletion = run(
            sys.executable,
            str(REPO / "bin" / "pgl-baseline"),
            "luca",
            str(second / "persona-engine" / "manifest.yml"),
            "--config",
            str(luca_path),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(refused_after_manifest_deletion.returncode, 0)
        self.assertIn("requires --confirm-root-change", refused_after_manifest_deletion.stdout)
        self.assertFalse(manifest_path.exists())
        luca_config["overlay_home_root"] = str(first)
        luca_path.write_text(json.dumps(luca_config, ensure_ascii=False), encoding="utf-8")
        run(sys.executable, str(REPO / "bin" / "pgl-baseline"), "luca", str(first / "persona-engine" / "manifest.yml"), "--config", str(luca_path), cwd=REPO, env=self.env)
        luca_config["overlay_home_root"] = str(second)
        luca_path.write_text(json.dumps(luca_config, ensure_ascii=False), encoding="utf-8")
        refused = run(
            sys.executable,
            str(REPO / "bin" / "pgl-baseline"),
            "luca",
            str(second / "persona-engine" / "manifest.yml"),
            "--config",
            str(luca_path),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("requires --confirm-root-change", refused.stdout)
        confirmed = run(
            sys.executable,
            str(REPO / "bin" / "pgl-baseline"),
            "luca",
            str(second / "persona-engine" / "manifest.yml"),
            "--config",
            str(luca_path),
            "--confirm-root-change",
            cwd=REPO,
            env=self.env,
        )
        self.assertIn(f"baseline pinned overlay_home={second.resolve()}", confirmed.stdout)
        luca_config["overlay_home_root"] = str(first)
        luca_path.write_text(json.dumps(luca_config, ensure_ascii=False), encoding="utf-8")
        before = {path: (first / path).read_bytes() for path in get_profile("luca").allowlist}
        result = run(sys.executable, str(NIGHTLY), "--face", "luca", "--date", str(day), "--config-dir", str(self.config_dir), cwd=REPO, env=self.env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[RED]", result.stdout)
        self.assertIn("[RED] luca: soul/root baseline check failed", result.stdout)
        self.assertIn("overlay home mismatch", result.stdout)
        self.assertIn("re-run bin/pgl-baseline", result.stdout)
        digest = (self.home / "digest" / f"{day}.md").read_text(encoding="utf-8")
        self.assertIn("[RED] luca: soul/root baseline check failed", digest)
        self.assertIn("overlay home mismatch", digest)
        self.assertEqual(before, {path: (first / path).read_bytes() for path in get_profile("luca").allowlist})
        self.assertEqual(run("git", "status", "--porcelain", cwd=first).stdout, "")

    def test_baseline_requires_confirmation_for_established_face_without_baseline_state(self) -> None:
        self.seed_invalid_adopted()
        baseline_dir = self.home / "soul-baseline"
        for path in baseline_dir.iterdir():
            path.unlink()
        baseline_dir.rmdir()
        refused = run(
            sys.executable,
            str(REPO / "bin" / "pgl-baseline"),
            "alpha",
            str(self.soul),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("requires --confirm-root-change", refused.stdout)
        self.assertFalse(get_profile("alpha").baseline_manifest(self.home).exists())
        confirmed = run(
            sys.executable,
            str(REPO / "bin" / "pgl-baseline"),
            "alpha",
            str(self.soul),
            "--confirm-root-change",
            cwd=REPO,
            env=self.env,
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stdout + confirmed.stderr)

    def test_baseline_refuses_while_killswitch_is_on_and_records_failure(self) -> None:
        manifest_path = get_profile("alpha").baseline_manifest(self.home)
        before = manifest_path.read_bytes()
        (self.home / "KILLSWITCH").write_text("mode: freeze\n", encoding="utf-8")
        result = run(
            sys.executable,
            str(REPO / "bin" / "pgl-baseline"),
            "alpha",
            str(self.soul),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[RED] alpha: baseline failed: killswitch detected", result.stdout)
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_baseline_requires_cp_gates_and_single_writer_lock(self) -> None:
        manifest_path = get_profile("alpha").baseline_manifest(self.home)
        before = manifest_path.read_bytes()
        (self.home / "gates.yml").write_text(
            "cp2_in_force: false\ndecided_by: owner\nref: governance-record\nfaces:\n  alpha:\n    cp3_go: true\n    decided_by: owner\n    ref: cp3-record\n",
            encoding="utf-8",
        )
        cp_refused = run(
            sys.executable,
            str(REPO / "bin" / "pgl-baseline"),
            "alpha",
            str(self.soul),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(cp_refused.returncode, 0)
        self.assertIn("CP-2 is not in force", cp_refused.stdout)
        self.assertEqual(manifest_path.read_bytes(), before)
        (self.home / "gates.yml").write_text(
            "cp2_in_force: true\ndecided_by: owner\nref: governance-record\nfaces:\n  alpha:\n    cp3_go: true\n    decided_by: owner\n    ref: cp3-record\n",
            encoding="utf-8",
        )
        (self.home / "lock-alpha.d").mkdir()
        lock_refused = run(
            sys.executable,
            str(REPO / "bin" / "pgl-baseline"),
            "alpha",
            str(self.soul),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(lock_refused.returncode, 0)
        self.assertIn("lock contention", lock_refused.stdout)
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_forget_empty_and_large_unconfirmed_matches_leave_disk_untouched(self) -> None:
        day = date(2026, 7, 3)
        self.seed_candidate_observations(day)
        self.nightly(day)
        paths = sorted((self.home / "obslog" / "alpha").glob("*.jsonl"))
        ledger_before = (self.overlay / "overlay-ledger.yml").read_bytes()
        obs_before = {path: path.read_bytes() for path in paths}
        empty = run(sys.executable, str(FORGET), "alpha", "", "--date", str(day), "--config", str(self.config_dir / "growth-alpha.json"), cwd=REPO, env=self.env, check=False)
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn("must not be empty", empty.stdout)
        self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), ledger_before)
        self.assertEqual({path: path.read_bytes() for path in paths}, obs_before)
        extra = [
            {"ts": f"{day}T12:{index:02d}:00+09:00", "session": f"bulk-{index}", "text": "なるほどだね"}
            for index in range(21)
        ]
        self.obs(day, extra)
        paths = sorted((self.home / "obslog" / "alpha").glob("*.jsonl"))
        ledger_before = (self.overlay / "overlay-ledger.yml").read_bytes()
        obs_before = {path: path.read_bytes() for path in paths}
        refused = run(sys.executable, str(FORGET), "alpha", "なるほど", "--date", str(day), "--config", str(self.config_dir / "growth-alpha.json"), cwd=REPO, env=self.env, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("forget dry-run phrases=1 tier_l=28", refused.stdout)
        self.assertIn("confirmation required", refused.stdout)
        self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), ledger_before)
        self.assertEqual({path: path.read_bytes() for path in paths}, obs_before)
        confirmed = run(sys.executable, str(FORGET), "alpha", "なるほど", "--yes", "--date", str(day), "--config", str(self.config_dir / "growth-alpha.json"), cwd=REPO, env=self.env)
        self.assertIn("forget completed", confirmed.stdout)
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"], [])

    def test_demoted_phrase_reharvest_persists_new_id_and_history_reference(self) -> None:
        day = date(2026, 7, 4)
        ledger = empty_ledger("alpha")
        for phrase_id, text, state in (("p-0001", "またやろうね", "demoted"), ("p-0002", "二度と言わない", "blocked")):
            phrase = new_phrase(phrase_id, text, {"first_seen": "2026-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0})
            phrase["state"] = state
            ledger["phrases"].append(phrase)
        self.seed_ledger_and_render(ledger, "")
        for offset in range(5):
            current = day - timedelta(days=offset)
            rows = [{"ts": f"{current}T1{i}:00:00+09:00", "session": f"{text}-{i}", "text": text} for text in ("またやろうね", "二度と言わない") for i in range(2)]
            self.obs(current, rows)
        self.nightly(day)
        persisted = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual([(item["id"], item["state"]) for item in persisted["phrases"]], [("p-0001", "demoted"), ("p-0002", "blocked"), ("p-0003", "candidate")])
        self.assertIn({"at": str(day), "action": "reharvest", "predecessor_id": "p-0001", "new_id": "p-0003"}, persisted["history"])
        second = day + timedelta(days=1)
        self.obs(second, [{"ts": f"{second}T10:00:00+09:00", "session": "next", "text": "別の話だね"}])
        self.nightly(second)
        persisted = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertIn("p-0003", {item["id"] for item in persisted["phrases"]})
        self.assertNotIn("p-0003", persisted["retired_ids"])
        self.assertEqual(persisted["retired_text_hashes"], [])

    def test_raw_echo_ratio_persists_and_blocks_second_night_staging(self) -> None:
        first = date(2026, 7, 5)
        self.seed_candidate_observations(first)
        entries = []
        for current in (first - timedelta(days=4), first - timedelta(days=3), first - timedelta(days=2)):
            entries.append({"type": "assistant", "timestamp": f"{current}T01:00:00Z", "sessionId": f"echo-{current}", "message": {"content": [{"type": "text", "text": "前後 なるほどだね 文脈"}]}})
        (self.root / "transcripts" / "session.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in entries), encoding="utf-8")
        self.nightly(first)
        persisted = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual(persisted["phrases"][0]["source"]["echo_ratio"], 0.75)
        second = first + timedelta(days=1)
        self.obs(second, [{"ts": f"{second}T10:00:00+09:00", "session": "other", "text": "別の話だね"}])
        self.nightly(second)
        persisted = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual(persisted["phrases"][0]["source"]["echo_ratio"], 0.75)
        self.assertEqual(persisted["phrases"][0]["state"], "candidate")
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")

    def test_stale_lock_digest_names_owner_and_manual_clear_command(self) -> None:
        lock = self.home / "lock-alpha.d"
        lock.mkdir()
        (lock / "owner.json").write_text(json.dumps({"pid": 4242, "host": "dead-host", "started_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
        result = self.nightly(date(2026, 7, 6), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[RED] alpha: nightly: stale lock contention", result.stdout)
        self.assertIn("pid=4242 host=dead-host", result.stdout)
        self.assertIn("clear manually: rm --", result.stdout)
        self.assertTrue(lock.is_dir())
        (lock / "owner.json").unlink()
        lock.rmdir()

    def test_block_buckets_matching_entries_and_prefers_clean_storage_form(self) -> None:
        self.seed_invalid_adopted()
        (self.overlay / "blocklist.txt").write_text(
            "ｃｏｍｍｉｔしよう\nco\u3164mmitしよう\ncommitしよう\n",
            encoding="utf-8",
        )
        run("git", "add", "blocklist.txt", cwd=self.overlay)
        run("git", "commit", "-qm", "seed duplicate normalized blocklist", cwd=self.overlay)
        day = date(2026, 7, 7)
        self.mirror(day)
        result = run(sys.executable, str(BLOCK), "alpha", "p-0001", "--date", str(day), "--config", str(self.config_dir / "growth-alpha.json"), cwd=REPO, env=self.env)
        self.assertIn("immediate block", result.stdout)
        self.assertEqual(
            (self.overlay / "blocklist.txt")
            .read_text(encoding="utf-8")
            .splitlines(),
            ["commitしよう"],
        )

    def test_block_invalid_utf8_blocklist_preserves_bytes_and_appends_normalized_entry(self) -> None:
        self.seed_invalid_adopted()
        original = b"blocked\n\xff\xfeinvalid blocklist\n"
        (self.overlay / "blocklist.txt").write_bytes(original)
        run("git", "add", "blocklist.txt", cwd=self.overlay)
        run("git", "commit", "-qm", "seed invalid utf8 blocklist", cwd=self.overlay)
        day = date(2026, 7, 7)
        self.mirror(day)
        result = run(
            sys.executable,
            str(BLOCK),
            "alpha",
            "p-0001",
            "--date",
            str(day),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
        )
        self.assertIn("immediate block", result.stdout)
        self.assertEqual(
            (self.overlay / "blocklist.txt").read_bytes(),
            original + "commitしよう\n".encode("utf-8"),
        )

    def test_forget_blocklist_survives_simulated_ucd_hash_change(self) -> None:
        day = date(2026, 7, 8)
        carrier = "\U000e1000"
        text = f"そう{carrier}だね"
        ledger = empty_ledger("alpha")
        phrase = new_phrase(
            "p-0001",
            text,
            {"first_seen": str(day), "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
        )
        phrase["state"] = "adopted"
        phrase["staged_at"] = str(day)
        ledger["phrases"].append(phrase)
        self.seed_ledger_and_render(ledger, f"phrase: {text}\n")
        forgotten = run(
            sys.executable,
            str(FORGET),
            "alpha",
            "そう",
            "--date",
            str(day),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
        )
        self.assertIn("forget completed", forgotten.stdout)
        self.assertEqual(
            (self.overlay / "blocklist.txt").read_text(encoding="utf-8").splitlines(),
            [text],
        )
        old_hash = normalized_hash(text)
        original_category = unicodedata.category

        def future_category(character: str) -> str:
            return "Lo" if character == carrier else original_category(character)

        end = day + timedelta(days=4)
        for offset, count in enumerate((2, 2, 2, 1, 1)):
            current = end - timedelta(days=4 - offset)
            self.obs(
                current,
                [
                    {
                        "ts": f"{current}T{10 + index:02d}:00:00+09:00",
                        "session": f"ucd-{offset}-{index}",
                        "text": text,
                    }
                    for index in range(count)
                ],
            )
        with mock.patch("growthlane.guard.unicodedata.category", side_effect=future_category):
            self.assertNotEqual(normalized_hash(text), old_hash)
            proposals = harvest(
                self.home,
                "alpha",
                end.isoformat(),
                self.config,
                empty_ledger("alpha"),
                parse_scalar_yaml(self.config_dir / "evidence.yml"),
                self.overlay / "blocklist.txt",
                [self.root / "transcripts" / "session.jsonl"],
            )
        self.assertEqual(proposals, [])

    def test_forget_blocklist_fault_degrades_and_preserves_bytes(self) -> None:
        day = date(2026, 7, 9)
        self.seed_invalid_adopted()
        blocklist_bytes = b"blocked\n\xff\xfeinvalid blocklist\n"
        (self.overlay / "blocklist.txt").write_bytes(blocklist_bytes)
        run("git", "add", "blocklist.txt", cwd=self.overlay)
        run("git", "commit", "-qm", "seed invalid utf8 blocklist for forget", cwd=self.overlay)
        result = run(
            sys.executable,
            str(FORGET),
            "alpha",
            "commit",
            "--date",
            str(day),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
        )
        self.assertIn("forget completed", result.stdout)
        self.assertIn("forget blocklist fault; preserving blocklist bytes", result.stdout)
        self.assertEqual((self.overlay / "blocklist.txt").read_bytes(), blocklist_bytes)
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"], [])

    def test_nightly_refuses_admission_on_ucd_drift_in_both_directions(self) -> None:
        day = date(2026, 8, 8)
        self.seed_candidate_observations(day)
        self.mirror(day)
        baseline = {
            path.name: path.read_bytes()
            for path in self.overlay.iterdir()
            if path.is_file()
        }
        cases = (
            ("17.0.0", "16.0.0", "runtime>corpus"),
            ("15.0.0", "16.0.0", "runtime<corpus"),
        )
        for runtime_version, corpus_version, direction in cases:
            with self.subTest(direction=direction):
                digest_path = self.home / "digest" / f"{day.isoformat()}.md"
                if digest_path.exists():
                    digest_path.unlink()
                with mock.patch.dict(os.environ, self.env, clear=False):
                    with mock.patch(
                        "growthlane.ucd_runtime.unicode_admission_drift",
                        return_value=(corpus_version, runtime_version),
                    ):
                        result = nightly_module.main(
                            [
                                "--face",
                                "alpha",
                                "--date",
                                day.isoformat(),
                                "--config-dir",
                                str(self.config_dir),
                            ]
                        )
                self.assertEqual(result, 1)
                digest_text = digest_path.read_text(encoding="utf-8")
                self.assertIn(
                    f"UCD drift runtime={runtime_version} corpus={corpus_version} direction={direction}",
                    digest_text,
                )
                self.assertIn("nightly refused admission-bearing work", digest_text)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in self.overlay.iterdir()
                        if path.is_file()
                    },
                    baseline,
                )

    def test_newer_runtime_ucd_skips_admission_only_and_refreshes_render(self) -> None:
        day = date(2026, 8, 8)
        candidate = new_phrase(
            "p-0001",
            "候補だね",
            {"first_seen": "2026-08-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
        )
        adopted = new_phrase(
            "p-0002",
            "古いね",
            {"first_seen": "2025-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
        )
        adopted["state"] = "adopted"
        adopted["staged_at"] = "2025-01-01"
        adopted["history"].append(
            {"at": "2025-01-15", "from": "staged", "to": "adopted", "by": "applier", "proposal_id": "seed"}
        )
        ledger = empty_ledger("alpha")
        ledger["phrases"] = [candidate, adopted]
        self.seed_ledger_and_render(ledger, "stale render\n")
        for offset in range(5):
            current = day - timedelta(days=offset)
            self.obs(
                current,
                [
                    {"ts": f"{current}T10:0{index}:00+09:00", "session": f"candidate-{offset}-{index}", "text": "候補だね"}
                    for index in range(2)
                ]
                + [
                    {"ts": f"{current}T11:0{index}:00+09:00", "session": f"new-{offset}-{index}", "text": "新顔だね"}
                    for index in range(2)
                ],
            )
        cases = (
            ("17.0.0", "16.0.0", "runtime>corpus"),
            ("15.0.0", "16.0.0", "runtime<corpus"),
        )
        for runtime_version, corpus_version, direction in cases:
            with self.subTest(direction=direction):
                self.seed_ledger_and_render(ledger, f"stale render {direction}\n")
                self.mirror(day)
                digest = Digest(self.home, day.isoformat())
                with mock.patch(
                    "growthlane.ucd_runtime.unicode_admission_drift",
                    return_value=(corpus_version, runtime_version),
                ):
                    run_face(
                        get_profile("alpha"),
                        self.home,
                        self.config,
                        parse_scalar_yaml(self.config_dir / "evidence.yml"),
                        day.isoformat(),
                        digest,
                    )
                digest_text = digest.path.read_text(encoding="utf-8")
                self.assertIn(
                    f"UCD drift runtime={runtime_version} corpus={corpus_version} direction={direction}",
                    digest_text,
                )
                persisted = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
                self.assertEqual(
                    [(item["id"], item["state"]) for item in persisted["phrases"]],
                    [("p-0001", "candidate"), ("p-0002", "demoted")],
                )
                self.assertNotIn("古いね", (self.overlay / "overlay.md").read_text(encoding="utf-8"))

    def test_real_nightly_rejects_punctuated_imperative(self) -> None:
        day = date(2026, 1, 5)
        for offset, count in enumerate((2, 2, 2, 1, 1)):
            current = day - timedelta(days=4 - offset)
            self.obs(
                current,
                [
                    {
                        "ts": f"{current}T{10 + index}:00:00+09:00",
                        "session": f"imperative-{offset}-{index}",
                        "text": "とりあえずマージして。",
                    }
                    for index in range(count)
                ],
            )
        result = self.nightly(day)
        self.assertIn("no-op", result.stdout)
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"], [])
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")

    def test_lint_violating_adopted_phrase_can_be_blocked(self) -> None:
        self.seed_invalid_adopted()
        day = date(2026, 5, 5)
        self.mirror(day)
        result = run(
            sys.executable,
            str(BLOCK),
            "alpha",
            "p-0001",
            "--date",
            day.isoformat(),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"][0]["state"], "blocked")
        self.assertNotIn("commitしよう", (self.overlay / "overlay.md").read_text())

    def test_lint_violating_adopted_phrase_can_be_forgotten(self) -> None:
        self.seed_invalid_adopted()
        day = date(2026, 5, 5)
        result = run(
            sys.executable,
            str(FORGET),
            "alpha",
            "commit",
            "--date",
            day.isoformat(),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"], [])
        self.assertNotIn("commitしよう", (self.overlay / "overlay.md").read_text())

    def test_lint_violating_adopted_phrase_can_be_ejected(self) -> None:
        self.seed_invalid_adopted()
        day = date(2026, 5, 5)
        self.mirror(day)
        (self.home / "KILLSWITCH").write_text("mode: eject\n", encoding="utf-8")
        result = run(
            sys.executable,
            str(EJECT),
            "alpha",
            "--date",
            day.isoformat(),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"][0]["state"], "adopted")

    def test_eject_ignores_entry_cap_and_empties_forty_one_adopted_phrases(self) -> None:
        day = date(2026, 5, 6)
        self.seed_ledger_and_render(self.adopted_ledger(41, day), "injected\n")
        self.mirror(day)
        (self.home / "KILLSWITCH").write_text("mode: eject\n", encoding="utf-8")
        result = run(
            sys.executable,
            str(EJECT),
            "alpha",
            "--date",
            day.isoformat(),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")

    def test_eject_ignores_display_name_byte_cap_and_empties_render(self) -> None:
        day = date(2026, 5, 7)
        self.seed_ledger_and_render(self.adopted_ledger(38, day), "injected\n")
        self.config["display_name"] = "長" * 700
        config_path = self.config_dir / "growth-alpha.json"
        config_path.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        refused = self.nightly(day, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("adopted byte cap exceeded", refused.stdout)
        (self.home / "KILLSWITCH").write_text("mode: eject\n", encoding="utf-8")
        ejected = run(
            sys.executable,
            str(EJECT),
            "alpha",
            "--date",
            day.isoformat(),
            "--config",
            str(config_path),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertEqual(ejected.returncode, 0, ejected.stdout + ejected.stderr)
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")

    def test_missing_transcripts_skip_admission_but_decay_and_render_continue(self) -> None:
        day = date(2026, 8, 1)
        ledger = empty_ledger("alpha")
        candidate = new_phrase(
            "p-0001", "候補だね", {"first_seen": "2026-07-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0}
        )
        staged = new_phrase(
            "p-0002", "試用だね", {"first_seen": "2026-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0}
        )
        staged["state"] = "staged"
        staged["staged_at"] = "2026-01-01"
        adopted = new_phrase(
            "p-0003", "古いね", {"first_seen": "2025-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0}
        )
        adopted["state"] = "adopted"
        adopted["staged_at"] = "2025-01-01"
        adopted["history"].append(
            {"at": "2025-01-15", "from": "staged", "to": "adopted", "by": "applier", "proposal_id": "seed"}
        )
        ledger["phrases"] = [candidate, staged, adopted]
        self.seed_ledger_and_render(ledger, "stale injected render\n")
        for offset in range(5):
            current = day - timedelta(days=offset)
            records = [
                {"ts": f"{current}T10:0{occurrence}:00+09:00", "session": f"candidate-{offset}-{occurrence}", "text": "候補だね"}
                for occurrence in range(2)
            ] + [
                {"ts": f"{current}T11:0{occurrence}:00+09:00", "session": f"new-{offset}-{occurrence}", "text": "新顔だね"}
                for occurrence in range(2)
            ]
            if offset == 0:
                records.append(
                    {"ts": f"{current}T10:30:00+09:00", "session": "stage-session", "text": "その言い方いいね"}
                )
            self.obs(
                current,
                records,
            )
        self.usage(
            day,
            [
                {"ts": f"{day}T09:0{index}:00+09:00", "session": "stage-session", "face": "alpha", "phrase_id": "p-0002", "state": "staged"}
                for index in range(3)
            ],
        )
        self.config["transcripts_root"] = str(self.root / "missing-transcripts")
        (self.config_dir / "growth-alpha.json").write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        result = self.nightly(day)
        self.assertIn("admission skipped; transcript echo indeterminate", result.stdout)
        persisted = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual(
            [(item["id"], item["state"]) for item in persisted["phrases"]],
            [("p-0001", "candidate"), ("p-0002", "staged"), ("p-0003", "demoted")],
        )
        self.assertNotIn("古いね", (self.overlay / "overlay.md").read_text(encoding="utf-8"))

    def test_empty_transcript_directory_skips_new_candidates(self) -> None:
        day = date(2026, 8, 2)
        (self.root / "transcripts" / "session.jsonl").unlink()
        self.seed_candidate_observations(day)
        result = self.nightly(day)
        self.assertIn("zero readable transcript files", result.stdout)
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"], [])

    def test_symlinked_transcript_is_reported_and_readable_transcripts_continue(self) -> None:
        day = date(2026, 8, 3)
        transcripts = self.root / "transcripts"
        linked = transcripts / "linked.jsonl"
        linked.symlink_to(transcripts / "session.jsonl")
        result = self.nightly(day)
        expected = f"alpha: transcript skipped: symlinked file: {linked}"
        self.assertIn(expected, result.stdout)
        self.assertNotIn("admission skipped", result.stdout)
        digest = (self.home / "digest" / f"{day}.md").read_text(encoding="utf-8")
        self.assertIn(expected, digest)

    def test_block_succeeds_with_unrelated_file_staged(self) -> None:
        self.seed_invalid_adopted()
        expected = self.stage_unrelated_notes()
        day = date(2026, 8, 3)
        result = run(
            sys.executable, str(BLOCK), "alpha", "p-0001", "--date", day.isoformat(), "--config", str(self.config_dir / "growth-alpha.json"),
            cwd=REPO, env=self.env, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_unrelated_notes_staged(expected)

    def test_forget_succeeds_with_unrelated_file_staged(self) -> None:
        self.seed_invalid_adopted()
        expected = self.stage_unrelated_notes()
        day = date(2026, 8, 3)
        result = run(
            sys.executable, str(FORGET), "alpha", "commit", "--date", day.isoformat(), "--config", str(self.config_dir / "growth-alpha.json"),
            cwd=REPO, env=self.env, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_unrelated_notes_staged(expected)

    def test_eject_succeeds_with_unrelated_file_staged(self) -> None:
        day = date(2026, 8, 3)
        self.seed_ledger_and_render(self.adopted_ledger(41, day), "injected\n")
        expected = self.stage_unrelated_notes()
        self.mirror(day)
        (self.home / "KILLSWITCH").write_text("mode: eject\n", encoding="utf-8")
        result = run(
            sys.executable, str(EJECT), "alpha", "--date", day.isoformat(), "--config", str(self.config_dir / "growth-alpha.json"),
            cwd=REPO, env=self.env, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")
        self.assert_unrelated_notes_staged(expected)

    def test_nightly_succeeds_with_unrelated_file_staged(self) -> None:
        day = date(2026, 8, 3)
        expected = self.stage_unrelated_notes()
        self.seed_candidate_observations(day)
        result = self.nightly(day)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"]), 1)
        self.assert_unrelated_notes_staged(expected)

    def test_identical_writer_reviewer_config_is_rejected_by_real_entrypoint(self) -> None:
        self.config["reviewer_argv"] = list(self.config["writer_argv"])
        (self.config_dir / "growth-alpha.json").write_text(json.dumps(self.config), encoding="utf-8")
        before = (self.overlay / "overlay-ledger.yml").read_bytes()
        result = self.nightly(date(2026, 6, 1))
        self.assertIn("must be distinct", result.stdout)
        self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), before)

    def test_stale_mirror_does_not_block_safety_but_stops_nightly(self) -> None:
        self.seed_invalid_adopted()
        today = datetime.now(JST).date()
        marker = self.home / "reports" / "weekly" / "latest-alpha.json"
        marker.write_text(json.dumps({"generated_at": (today - timedelta(days=15)).isoformat()}), encoding="utf-8")
        blocked = run(
            sys.executable,
            str(BLOCK),
            "alpha",
            "p-0001",
            "--date",
            "2026-05-05",
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
        )
        self.assertEqual(blocked.returncode, 0)
        nightly = run(
            sys.executable,
            str(NIGHTLY),
            "--face",
            "alpha",
            "--date",
            "2026-05-05",
            "--config-dir",
            str(self.config_dir),
            cwd=REPO,
            env=self.env,
        )
        self.assertIn("mirror marker stale", nightly.stdout)

    def test_snapshot_records_parent_and_created_tag(self) -> None:
        day = date(2026, 6, 5)
        self.seed_candidate_observations(day)
        self.nightly(day)
        snapshot = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["snapshots"][-1]
        self.assertEqual(set(snapshot), {"at", "parent_sha", "tag", "content_hash"})
        self.assertIn(snapshot["tag"], run("git", "tag", "--list", cwd=self.overlay).stdout.splitlines())

    def test_unconfigured_classifier_warning_and_shipped_defaults_fail_closed(self) -> None:
        result = self.nightly(date(2026, 6, 6))
        self.assertIn("two-stage signal classification requirement is not fully met", result.stdout)
        self.assertFalse((REPO / "gates.yml").exists())
        self.assertFalse((REPO / "reports" / "weekly" / "latest-alpha.json").exists())
        shipped = json.loads((REPO / "config" / "growth-alpha.json").read_text())
        self.assertEqual(shipped["writer_argv"], [])
        self.assertEqual(shipped["reviewer_argv"], [])
        self.assertEqual(shipped["classifier_argv"], ["python3", "adapters/signal_classifier.py"])

    def test_threshold_deviation_is_written_to_nightly_digest(self) -> None:
        evidence = (self.config_dir / "evidence.yml").read_text(encoding="utf-8")
        (self.config_dir / "evidence.yml").write_text(
            evidence.replace("min_count: 8", "min_count: 9"), encoding="utf-8"
        )
        result = self.nightly(date(2026, 6, 6))
        self.assertIn("threshold min_count tightened 8->9", result.stdout)
        self.assertIn("threshold min_count tightened 8->9", (self.home / "digest" / "2026-06-06.md").read_text())

    def test_rollback_rejects_bad_tag_and_snapshot_without_hash_record(self) -> None:
        day = date(2026, 6, 7)
        self.mirror(day)
        bad = run(
            sys.executable,
            str(ROLLBACK),
            "alpha",
            "HEAD",
            "--date",
            day.isoformat(),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(bad.returncode, 0)
        tag = "overlay-snap-alpha-20260607-1"
        run("git", "tag", tag, cwd=self.overlay)
        missing = run(
            sys.executable,
            str(ROLLBACK),
            "alpha",
            tag,
            "--date",
            day.isoformat(),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("content_hash verification failed", missing.stdout)

    def test_forget_rejects_symlinked_face_observation_directory(self) -> None:
        self.seed_invalid_adopted()
        obs_dir = self.home / "obslog" / "alpha"
        obs_dir.rmdir()
        outside = self.root / "outside-observations"
        outside.mkdir()
        obs_dir.symlink_to(outside, target_is_directory=True)
        result = run(
            sys.executable,
            str(FORGET),
            "alpha",
            "commit",
            "--date",
            "2026-05-05",
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked observation directory", result.stdout)
        self.assertEqual(list(outside.iterdir()), [])

    def test_forget_preserves_unparseable_obslog_lines_and_reports_locations(self) -> None:
        self.seed_invalid_adopted()
        day = date(2026, 5, 7)
        path = self.home / "obslog" / "alpha" / f"{day}.jsonl"
        path.write_bytes(
            (
                json.dumps(
                    {
                        "ts": f"{day}T10:00:00+09:00",
                        "host": "fixture",
                        "face": "alpha",
                        "session": "match",
                        "project": "fixture",
                        "speaker": "owner",
                        "text": "commitしよう",
                        "len": len("commitしよう"),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                + b"\n"
                + b"{ broken json\n"
            )
        )
        result = run(
            sys.executable,
            str(FORGET),
            "alpha",
            "commit",
            "--date",
            day.isoformat(),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
        )
        self.assertIn("forget completed", result.stdout)
        self.assertIn(
            f"{path}:2: unparseable obslog line preserved",
            result.stdout,
        )
        self.assertIn("forget pattern match NOT verified for 1 line(s)", result.stdout)
        self.assertEqual(path.read_bytes(), b"{ broken json\n")

    def test_forget_rollback_replaces_swapped_symlink_without_touching_target(self) -> None:
        self.seed_invalid_adopted()
        day = date(2026, 5, 7)
        self.obs(
            day,
            [
                {
                    "ts": f"{day}T10:00:00+09:00",
                    "session": "match",
                    "text": "commitしよう",
                }
            ],
        )
        path = self.home / "obslog" / "alpha" / f"{day}.jsonl"
        original = path.read_bytes()
        outside = self.root / "outside-observation"
        outside.write_bytes(b"must remain untouched\n")

        def fail_after_rewrite(*_args: object, **_kwargs: object) -> None:
            self.assertEqual(path.read_bytes(), b"")
            path.unlink()
            path.symlink_to(outside)
            raise RuntimeError("forced post-rewrite failure")

        with mock.patch.dict(os.environ, self.env, clear=False):
            with mock.patch.object(
                operations_module,
                "commit_state",
                side_effect=fail_after_rewrite,
            ):
                result = operations_module.forget_main(
                    [
                        "alpha",
                        "commit",
                        "--date",
                        day.isoformat(),
                        "--config",
                        str(self.config_dir / "growth-alpha.json"),
                    ]
                )

        self.assertEqual(result, 1)
        self.assertFalse(path.is_symlink())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(outside.read_bytes(), b"must remain untouched\n")

    def test_forget_shape_errors_are_line_numbered(self) -> None:
        self.seed_invalid_adopted()
        day = date(2026, 5, 8)
        path = self.home / "obslog" / "alpha" / f"{day}.jsonl"
        path.write_text(
            json.dumps(
                {
                    "ts": f"{day}T10:00:00+09:00",
                    "host": "fixture",
                    "face": "alpha",
                    "session": "bad-shape",
                    "project": "fixture",
                    "speaker": "owner",
                    "text": "commitしよう",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        result = run(
            sys.executable,
            str(FORGET),
            "alpha",
            "commit",
            "--date",
            day.isoformat(),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"invalid Tier L record {path.resolve()}:1", result.stdout)

    def test_nightly_missing_blocklist_fails_closed_with_path(self) -> None:
        day = date(2026, 5, 9)
        self.seed_candidate_observations(day)
        self.mirror(day)
        target = self.overlay / "blocklist.txt"
        run("git", "rm", "-q", "--", "blocklist.txt", cwd=self.overlay)
        run("git", "commit", "-qm", "remove blocklist fixture", cwd=self.overlay)
        result = self.nightly(day, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"missing or invalid blocklist: {target.resolve()}", result.stdout)

    def test_preexisting_nightly_lock_is_hardened_to_0700(self) -> None:
        lock = self.home / "lock-alpha.d"
        lock.mkdir()
        lock.chmod(0o777)
        result = self.nightly(date(2026, 6, 8), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lock contention", result.stdout)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o700)

    def test_full_candidate_staged_holdout_adopted_journey_and_idempotency(self) -> None:
        first = date(2026, 1, 5)
        self.seed_candidate_observations(first)
        first_result = self.nightly(first)
        self.assertIn("committed", first_result.stdout)
        ledger = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual(ledger["phrases"][0]["state"], "candidate")

        staged_day = first + timedelta(days=1)
        self.obs(staged_day, [{"ts": f"{staged_day}T09:00:00+09:00", "face": "alpha", "session": "stage", "speaker": "owner", "text": "別の話題だね"}])
        self.nightly(staged_day)
        ledger = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual(ledger["phrases"][0]["state"], "staged")
        expected_visible = exposed("alpha", "p-0001", staged_day.isoformat())
        self.assertEqual(bool((self.overlay / "overlay.md").read_bytes()), expected_visible)

        mention_day: date | None = None
        for offset in range(1, 15):
            day = staged_day + timedelta(days=offset)
            session = f"session-{day}"
            use_ts = f"{day.isoformat()}T10:00:00+09:00"
            user_text = f"別の話題{offset}だね"
            if mention_day is None and exposed("alpha", "p-0001", day.isoformat()):
                mention_day = day
                user_text = "その言い方いいね"
            self.usage(day, [{"ts": use_ts, "session": session, "face": "alpha", "phrase_id": "p-0001", "state": "staged"}])
            self.obs(day, [{"ts": f"{day.isoformat()}T11:00:00+09:00", "face": "alpha", "session": session, "speaker": "owner", "text": user_text}])
            self.nightly(day)
            state = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"][0]["state"]
            if offset < 14:
                self.assertEqual(state, "staged")
                payload = (self.overlay / "overlay.md").read_text(encoding="utf-8")
                self.assertEqual("試用中" in payload, exposed("alpha", "p-0001", day.isoformat()))
        self.assertIsNotNone(mention_day)
        ledger = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual(ledger["phrases"][0]["state"], "adopted")
        self.assertIn("テスト利用者がよく使う言い回し", (self.overlay / "overlay.md").read_text(encoding="utf-8"))
        commits_before = run("git", "rev-list", "--count", "HEAD", cwd=self.overlay).stdout.strip()
        bytes_before = (self.overlay / "overlay-ledger.yml").read_bytes(), (self.overlay / "overlay.md").read_bytes()
        self.nightly(staged_day + timedelta(days=14))
        self.assertEqual(commits_before, run("git", "rev-list", "--count", "HEAD", cwd=self.overlay).stdout.strip())
        self.assertEqual(bytes_before, ((self.overlay / "overlay-ledger.yml").read_bytes(), (self.overlay / "overlay.md").read_bytes()))
        tags = run("git", "tag", "--list", "overlay-snap-alpha-*", cwd=self.overlay).stdout.splitlines()
        self.assertGreaterEqual(len(tags), 3)
        for offset in range(15):
            digest = self.home / "digest" / f"{(first + timedelta(days=offset)).isoformat()}.md"
            self.assertTrue(digest.is_file())
            self.assertTrue(digest.read_text(encoding="utf-8").strip())

    def test_fail_closed_gate_matrix_and_unknown_ledger(self) -> None:
        day = date(2026, 2, 1)
        self.seed_candidate_observations(day)
        original = (self.overlay / "overlay-ledger.yml").read_bytes()
        cases = ("missing-gates", "cp3-false", "killswitch", "missing-mirror", "stale-mirror")
        base_gates = (self.home / "gates.yml").read_text()
        for case in cases:
            with self.subTest(case=case):
                (self.home / "gates.yml").write_text(base_gates, encoding="utf-8")
                (self.home / "KILLSWITCH").unlink(missing_ok=True)
                self.mirror(day)
                if case == "missing-gates":
                    (self.home / "gates.yml").unlink()
                elif case == "cp3-false":
                    (self.home / "gates.yml").write_text(base_gates.replace("cp3_go: true", "cp3_go: false"), encoding="utf-8")
                elif case == "killswitch":
                    (self.home / "KILLSWITCH").write_text("mode: freeze\n")
                elif case == "missing-mirror":
                    (self.home / "reports" / "weekly" / "latest-alpha.json").unlink()
                else:
                    (self.home / "reports" / "weekly" / "latest-alpha.json").write_text(
                        json.dumps({"generated_at": (datetime.now(JST).date() - timedelta(days=15)).isoformat()}),
                        encoding="utf-8",
                    )
                result = run(sys.executable, str(NIGHTLY), "--face", "alpha", "--date", day.isoformat(), "--config-dir", str(self.config_dir), cwd=REPO, check=True, env=self.env)
                self.assertIn("skipped", result.stdout)
                self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), original)
                self.assertEqual(run("git", "status", "--porcelain", cwd=self.overlay).stdout, "")
        (self.home / "KILLSWITCH").unlink(missing_ok=True)
        (self.home / "gates.yml").write_text(base_gates)
        ledger = (self.overlay / "overlay-ledger.yml").read_text().replace("schema_version: 1", "schema_version: 2")
        (self.overlay / "overlay-ledger.yml").write_text(ledger)
        run("git", "add", "overlay-ledger.yml", cwd=self.overlay)
        run("git", "commit", "-qm", "seed unknown schema", cwd=self.overlay)
        before = (self.overlay / "overlay-ledger.yml").read_bytes()
        result = self.nightly(day, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), before)

    def test_review_reject_blocks_staging(self) -> None:
        day = date(2026, 3, 5)
        self.seed_candidate_observations(day)
        self.nightly(day)
        self.config["reviewer_argv"] = [sys.executable, str(REJECT)]
        (self.config_dir / "growth-alpha.json").write_text(json.dumps(self.config), encoding="utf-8")
        next_day = day + timedelta(days=1)
        self.obs(next_day, [{"ts": f"{next_day}T12:00:00+09:00", "face": "alpha", "session": "r", "speaker": "owner", "text": "別件だね"}])
        result = self.nightly(next_day)
        self.assertIn("review blocked", result.stdout)
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"][0]["state"], "candidate")
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")

    def test_missing_soul_lock_dirty_and_guard_rejection_are_fail_closed(self) -> None:
        day = date(2026, 4, 5)
        self.seed_candidate_observations(day)
        ledger_before = (self.overlay / "overlay-ledger.yml").read_bytes()
        get_profile("alpha").baseline_manifest(self.home).unlink()
        result = self.nightly(day, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("soul baseline", result.stdout)
        self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), ledger_before)
        self.assertEqual(run("git", "status", "--porcelain", cwd=self.overlay).stdout, "")
        write_manifest(get_profile("alpha"), self.home, self.config, [self.soul])
        (self.home / "lock-alpha.d").mkdir()
        result = self.nightly(day, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lock contention", result.stdout)
        self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), ledger_before)
        (self.home / "lock-alpha.d").rmdir()
        (self.overlay / "blocklist.txt").write_text("local dirty edit\n", encoding="utf-8")
        result = self.nightly(day, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty overlay", result.stdout)
        self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), ledger_before)
        run("git", "checkout", "--", "blocklist.txt", cwd=self.overlay)
        # Replace all eligible fragments with a deny-grammar violation. The
        # harvester must leave the ledger and repository untouched.
        for path in (self.home / "obslog" / "alpha").glob("*.jsonl"):
            records = [json.loads(line) for line in path.read_text().splitlines()]
            for item in records:
                item["text"] = "commitしてみよう"
                item["len"] = len(item["text"])
            path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records))
        result = self.nightly(day)
        self.assertIn("no-op", result.stdout)
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"], [])

    def test_manual_block_and_forget_keep_deletion_direction_live(self) -> None:
        day = date(2026, 5, 5)
        self.seed_candidate_observations(day)
        self.nightly(day)
        self.mirror(day)
        (self.home / "KILLSWITCH").write_text("mode: freeze\n", encoding="utf-8")
        blocked = run(
            sys.executable, str(BLOCK), "alpha", "p-0001", "--date", day.isoformat(), "--config", str(self.config_dir / "growth-alpha.json"),
            cwd=REPO, env=self.env,
        )
        self.assertIn("immediate block", blocked.stdout)
        ledger = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual(ledger["phrases"][0]["state"], "blocked")
        self.assertIn("なるほどだね", (self.overlay / "blocklist.txt").read_text())
        forgotten = run(
            sys.executable, str(FORGET), "alpha", "なるほど", "--date", day.isoformat(), "--config", str(self.config_dir / "growth-alpha.json"),
            cwd=REPO, env=self.env,
        )
        self.assertIn("forget completed", forgotten.stdout)
        ledger = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual(ledger["phrases"], [])
        self.assertIn("p-0001", ledger["retired_ids"])
        history_text = json.dumps(ledger["history"], ensure_ascii=False)
        self.assertNotIn("なるほど", history_text)
        self.assertTrue(all("なるほど" not in path.read_text() for path in (self.home / "obslog" / "alpha").glob("*.jsonl")))
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")

    def test_deletion_operations_continue_with_ucd_drift_note(self) -> None:
        day = date(2026, 5, 6)
        self.seed_candidate_observations(day)
        self.nightly(day)
        self.mirror(day)
        drift_runtime = "17.0.0"
        drift_corpus = "16.0.0"
        drift_note = (
            f"UCD drift runtime={drift_runtime} corpus={drift_corpus} "
            "— matching may miss variants; direction=runtime>corpus"
        )
        with mock.patch.dict(os.environ, self.env, clear=False):
            with mock.patch(
                "growthlane.ucd_runtime.unicode_admission_drift",
                return_value=(drift_corpus, drift_runtime),
            ):
                blocked = operations_module.block_main(
                    [
                        "alpha",
                        "p-0001",
                        "--date",
                        day.isoformat(),
                        "--config",
                        str(self.config_dir / "growth-alpha.json"),
                    ]
                )
                forgotten = operations_module.forget_main(
                    [
                        "alpha",
                        "なるほど",
                        "--date",
                        day.isoformat(),
                        "--config",
                        str(self.config_dir / "growth-alpha.json"),
                    ]
                )
                (self.home / "KILLSWITCH").write_text("mode: eject\n", encoding="utf-8")
                ejected = operations_module.eject_main(
                    [
                        "alpha",
                        "--date",
                        day.isoformat(),
                        "--config",
                        str(self.config_dir / "growth-alpha.json"),
                    ]
                )
        self.assertEqual(blocked, 0)
        self.assertEqual(forgotten, 0)
        self.assertEqual(ejected, 0)
        digest = (self.home / "digest" / f"{day.isoformat()}.md").read_text(encoding="utf-8")
        self.assertIn(drift_note, digest)
        self.assertIn("alpha: immediate block p-0001", digest)
        self.assertIn("alpha: forget completed counts=", digest)
        self.assertIn("alpha: eject completed", digest)

    def test_blocked_clean_phrase_prevents_property_twin_reharvest(self) -> None:
        clean = "そうだね"
        for carrier_index, carrier in enumerate(PROPERTY_CARRIERS):
            with self.subTest(carrier=f"U+{ord(carrier):04X}"):
                blocked_day = date(2026, 5, 6) + timedelta(days=carrier_index * 20)
                twin = composition_twin(clean, carrier)
                ledger = empty_ledger("alpha")
                phrase = new_phrase(
                    "p-0001",
                    clean,
                    {
                        "first_seen": blocked_day.isoformat(),
                        "window_count": 8,
                        "distinct_days": 5,
                        "echo_ratio": 0.0,
                    },
                )
                phrase["state"] = "adopted"
                phrase["staged_at"] = blocked_day.isoformat()
                ledger["phrases"].append(phrase)
                self.seed_ledger_and_render(ledger, f"phrase: {clean}\n")
                self.mirror(blocked_day)
                blocked = run(
                    sys.executable,
                    str(BLOCK),
                    "alpha",
                    "p-0001",
                    "--date",
                    blocked_day.isoformat(),
                    "--config",
                    str(self.config_dir / "growth-alpha.json"),
                    cwd=REPO,
                    env=self.env,
                )
                self.assertIn("immediate block", blocked.stdout)
                self.assertEqual(
                    (self.overlay / "blocklist.txt")
                    .read_text(encoding="utf-8")
                    .splitlines(),
                    [clean],
                )

                end = blocked_day + timedelta(days=4)
                for offset, count in enumerate((2, 2, 2, 1, 1)):
                    current = end - timedelta(days=4 - offset)
                    self.obs(
                        current,
                        [
                            {
                                "ts": f"{current}T{10 + index:02d}:00:00+09:00",
                                "session": f"property-{carrier_index}-{offset}-{index}",
                                "text": twin,
                            }
                            for index in range(count)
                        ],
                    )
                result = self.nightly(end)
                self.assertIn("no-op", result.stdout)
                persisted = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
                self.assertEqual([item["id"] for item in persisted["phrases"]], ["p-0001"])
                self.assertEqual(persisted["phrases"][0]["state"], "blocked")

    def test_forget_clean_phrase_removes_property_twin_from_ledger_and_tier_l(self) -> None:
        clean = "そうだね"
        for carrier_index, carrier in enumerate(PROPERTY_CARRIERS):
            with self.subTest(carrier=f"U+{ord(carrier):04X}"):
                day = date(2026, 8, 7) + timedelta(days=carrier_index * 20)
                twin = composition_twin(clean, carrier)
                ledger = empty_ledger("alpha")
                phrase = new_phrase(
                    "p-0001",
                    twin,
                    {
                        "first_seen": day.isoformat(),
                        "window_count": 8,
                        "distinct_days": 5,
                        "echo_ratio": 0.0,
                    },
                )
                phrase["state"] = "adopted"
                phrase["staged_at"] = day.isoformat()
                ledger["phrases"].append(phrase)
                self.seed_ledger_and_render(ledger, f"phrase: {twin}\n")
                self.obs(
                    day,
                    [
                        {
                            "ts": f"{day}T10:00:00+09:00",
                            "session": f"property-{carrier_index}",
                            "text": twin,
                        }
                    ],
                )
                result = run(
                    sys.executable,
                    str(FORGET),
                    "alpha",
                    clean,
                    "--date",
                    day.isoformat(),
                    "--config",
                    str(self.config_dir / "growth-alpha.json"),
                    cwd=REPO,
                    env=self.env,
                )
                self.assertIn("forget completed", result.stdout)
                persisted = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
                self.assertEqual(persisted["phrases"], [])
                self.assertIn("p-0001", persisted["retired_ids"])
                self.assertEqual(
                    (self.home / "obslog" / "alpha" / f"{day}.jsonl").read_bytes(),
                    b"",
                )
                self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")

                return_day = day + timedelta(days=5)
                for offset, count in enumerate((2, 2, 2, 1, 1)):
                    current = return_day - timedelta(days=4 - offset)
                    self.obs(
                        current,
                        [
                            {
                                "ts": f"{current}T{10 + index:02d}:00:00+09:00",
                                "session": f"return-{carrier_index}-{offset}-{index}",
                                "text": twin,
                            }
                            for index in range(count)
                        ],
                    )
                returned = self.nightly(return_day)
                self.assertIn("no-op", returned.stdout)
                self.assertEqual(
                    load_ledger(self.overlay / "overlay-ledger.yml", "alpha")[
                        "phrases"
                    ],
                    [],
                )

    def test_negative_signal_blocks_on_next_nightly_cycle(self) -> None:
        day = date(2026, 8, 5)
        self.seed_candidate_observations(day)
        self.nightly(day)
        staged_day = day + timedelta(days=1)
        self.obs(staged_day, [{"ts": f"{staged_day}T10:00:00+09:00", "session": "stage", "text": "別の話だね"}])
        self.nightly(staged_day)
        negative_day = staged_day + timedelta(days=1)
        self.usage(negative_day, [{"ts": f"{negative_day}T10:00:00+09:00", "session": "negative", "face": "alpha", "phrase_id": "p-0001", "state": "staged"}])
        self.obs(negative_day, [{"ts": f"{negative_day}T10:01:00+09:00", "session": "negative", "text": "その言い方やめて"}])
        result = self.nightly(negative_day)
        self.assertIn("demotion/block", result.stdout)
        ledger = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual(ledger["phrases"][0]["state"], "blocked")
        self.assertIn("なるほどだね", (self.overlay / "blocklist.txt").read_text())
        self.assertNotIn("なるほどだね", (self.overlay / "overlay.md").read_text())

    def test_eject_and_one_command_rollback_restore_verified_snapshots(self) -> None:
        day = date(2026, 9, 5)
        self.seed_candidate_observations(day)
        self.nightly(day)
        first_tag = run("git", "tag", "--list", "overlay-snap-alpha-*", cwd=self.overlay).stdout.splitlines()[-1]
        staged_day = day + timedelta(days=1)
        self.obs(staged_day, [{"ts": f"{staged_day}T10:00:00+09:00", "session": "stage", "text": "別件だね"}])
        self.nightly(staged_day)
        self.mirror(staged_day)
        rollback = run(
            sys.executable, str(ROLLBACK), "alpha", first_tag, "--date", staged_day.isoformat(), "--config", str(self.config_dir / "growth-alpha.json"),
            cwd=REPO, env=self.env,
        )
        self.assertIn("before=", rollback.stdout)
        self.assertIn("after=", rollback.stdout)
        self.assertEqual(load_ledger(self.overlay / "overlay-ledger.yml", "alpha")["phrases"][0]["state"], "candidate")
        rollback_status = run("git", "status", "--porcelain", cwd=self.overlay).stdout
        self.assertEqual(rollback_status, "", rollback_status)
        # Stage again, then exercise the one sanctioned growth-direction halt.
        eject_day = staged_day + timedelta(days=1)
        self.obs(eject_day, [{"ts": f"{eject_day}T10:00:00+09:00", "session": "restage", "text": "別件その二だね"}])
        self.nightly(eject_day)
        self.mirror(eject_day)
        (self.home / "KILLSWITCH").write_text("mode: eject\n", encoding="utf-8")
        ejected = run(
            sys.executable, str(EJECT), "alpha", "--date", eject_day.isoformat(), "--config", str(self.config_dir / "growth-alpha.json"),
            cwd=REPO, env=self.env,
        )
        self.assertIn("eject completed", ejected.stdout)
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")
        ledger = load_ledger(self.overlay / "overlay-ledger.yml", "alpha")
        self.assertEqual(ledger["phrases"][0]["state"], "staged")
        self.assertEqual(ledger["history"][-2]["action"], "eject")

    def test_luca_rollback_restores_tag_through_staging_build(self) -> None:
        day = date(2026, 9, 8)
        profile = get_profile("luca")
        overlay = self.root / "luca-pack"
        staging = self.root / "luca-staging"
        (overlay / "persona-engine" / "catalogs" / "overlay").mkdir(parents=True)
        persona = overlay / "packages" / "core" / "bin" / "persona"
        persona.parent.mkdir(parents=True)
        persona.write_text("fixture\n", encoding="utf-8")
        (overlay / "growth").mkdir()
        (overlay / "tests" / "luca-pack").mkdir(parents=True)
        staging.mkdir()
        install_text = (
            "schema_version: 2\n"
            "pack: pack\n"
            "runtime: generic\n"
            "placeholders:\n"
            "  agent-name: ルカ\n"
            "  user: オーナー\n"
            "  owner-name: オーナー\n"
        )
        (staging / "install.yml").write_text(install_text, encoding="utf-8")
        (overlay / "tests" / "luca-pack" / "install.yml").write_text(
            install_text, encoding="utf-8"
        )
        (overlay / "persona-engine" / "manifest.yml").write_text(
            "name: luca-fixture\n", encoding="utf-8"
        )
        # The killswitch-relaxed rollback fixture must be a §5.5 reduction.
        # Injection-increasing tags are covered by the full-gate lane tests.
        restored_candidates = b""
        restored_hash = hashlib.sha256(restored_candidates).hexdigest()
        current_candidates = b"current candidate\n"
        current_hash = hashlib.sha256(current_candidates).hexdigest()
        tag = "overlay-snap-luca-20260908-1"
        restored_ledger = empty_ledger("luca")
        restored_ledger["snapshots"].append(
            {
                "at": day.isoformat(),
                "parent_sha": "0" * 40,
                "tag": tag,
                "content_hash": restored_hash,
            }
        )
        (overlay / profile.ledger_path).write_bytes(dump_ledger(restored_ledger))
        (overlay / profile.blocklist_path).write_bytes(b"")
        (overlay / profile.render_files["candidates"]).write_bytes(restored_candidates)
        (overlay / profile.render_files["adopted"]).write_bytes(b"")
        run("git", "init", "-q", cwd=overlay)
        run("git", "config", "user.name", "PGL Test", cwd=overlay)
        run("git", "config", "user.email", "pgl-test@example.invalid", cwd=overlay)
        run("git", "add", "-A", cwd=overlay)
        run("git", "commit", "-qm", "seed luca rollback tag", cwd=overlay)
        run("git", "tag", tag, cwd=overlay)

        current_ledger = empty_ledger("luca")
        (overlay / profile.ledger_path).write_bytes(dump_ledger(current_ledger))
        (overlay / profile.render_files["candidates"]).write_bytes(current_candidates)
        run("git", "add", "--", *profile.allowlist, cwd=overlay)
        run("git", "commit", "-qm", "advance luca overlay", cwd=overlay)

        luca_config = {
            "display_name": "オーナー",
            "speaker": "owner",
            "transcripts_root": str(self.root / "transcripts"),
            "overlay_home_root": str(overlay),
            "staging_root": str(staging),
            "writer_argv": [],
            "reviewer_argv": [],
            "classifier_argv": [],
        }
        luca_config_path = self.config_dir / "growth-luca.json"
        luca_config_path.write_text(
            json.dumps(luca_config, ensure_ascii=False), encoding="utf-8"
        )
        acceptance_ledger = self.home / "state" / "luca-verify-sessions.jsonl"
        acceptance_ledger.parent.mkdir(parents=True, exist_ok=True)
        acceptance_ledger.write_text(
            '{"session_id":"historical-seed","recorded_at":"2026-08-01T00:00:00+09:00","origin":"seed"}\n',
            encoding="utf-8",
        )
        acceptance_ledger.chmod(0o600)
        (self.config_dir / "obs-collector-luca.json").write_text(
            json.dumps(
                {
                    "face": "luca",
                    "host": "vps-hermes",
                    "speaker": "owner",
                    "obs_root": str(self.home),
                    "source": {
                        "ssh_host": "example-vps",
                        "kind": "hermes-state-db",
                        "db_path": "~/.hermes/profiles/luca/state.db",
                        "owner_uids": {
                            "telegram": ["owner"],
                            "slack": ["owner"],
                        },
                        "expected_dm_entries": {
                            "telegram": ["owner"],
                            "slack": ["dm"],
                        },
                        "sources": ["telegram", "slack", "api_server"],
                        "dm_only": True,
                        "exclude_session_prefixes": ["pgl-verify-"],
                        "exclude_session_ledger": str(acceptance_ledger),
                        "voice_enabled": True,
                    },
                    "denylist_path": "denylist.txt",
                }
            ),
            encoding="utf-8",
        )
        (self.home / "gates.yml").write_text(
            (self.home / "gates.yml").read_text(encoding="utf-8")
            + "  luca:\n    cp3_go: true\n    decided_by: owner\n    ref: cp3-luca\n",
            encoding="utf-8",
        )
        (self.home / "reports" / "weekly" / "latest-luca.json").write_text(
            json.dumps({"generated_at": datetime.now(JST).date().isoformat()}),
            encoding="utf-8",
        )
        write_manifest(
            profile,
            self.home,
            luca_config,
            [overlay / "persona-engine" / "manifest.yml"],
        )

        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        persona_calls = self.root / "persona-calls.txt"
        node = fake_bin / "node"
        node.write_text(
            "#!/usr/bin/env python3\n"
            "import hashlib, json, os, sys\n"
            "from pathlib import Path\n"
            "root = Path(sys.argv[sys.argv.index('--dir') + 1])\n"
            "with Path(os.environ['PGL_TEST_CALLS']).open('a', encoding='utf-8') as stream:\n"
            "    stream.write(sys.argv[2] + '\\n')\n"
            "if sys.argv[2] == 'build':\n"
            "    value = hashlib.sha256((root / 'pack/catalogs/overlay/candidates.txt').read_bytes()).hexdigest()\n"
            "    (root / 'build').mkdir()\n"
            "    (root / 'build/manifest.json').write_text(json.dumps({'content_hash': value}) + '\\n', encoding='utf-8')\n"
            "    print(json.dumps({'ok': True, 'manifest': {'content_hash': value}}))\n"
            "elif sys.argv[2] == 'doctor':\n"
            "    print(json.dumps({'ok': True, 'issues': []}))\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        node.chmod(0o755)
        transport_calls = self.root / "transport-calls.txt"
        ssh = fake_bin / "ssh"
        ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "if args[:1] == ['-i']:\n"
            "    args = args[2:]\n"
            "command = args[1:]\n"
            "with Path(os.environ['PGL_TEST_TRANSPORT_CALLS']).open('a', encoding='utf-8') as stream:\n"
            "    stream.write('ssh ' + ' '.join(command) + '\\n')\n"
            "if command == ['hash']:\n"
            "    print(os.environ['PGL_TEST_OLD_HASH'])\n"
            "elif command == ['accept']:\n"
            "    print(json.dumps(['11111111-1111-4111-8111-111111111111']))\n"
            "elif command in (['deploy', 'backup'], ['deploy', 'restart']):\n"
            "    pass\n"
            "elif command[:2] == ['deploy', 'promote'] and len(command) == 3:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        rsync = fake_bin / "rsync"
        rsync.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "with Path(os.environ['PGL_TEST_TRANSPORT_CALLS']).open('a', encoding='utf-8') as stream:\n"
            "    stream.write('rsync ' + ' '.join(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        rsync.chmod(0o755)
        (self.home / "KILLSWITCH").write_text("mode: freeze\n", encoding="utf-8")
        environment = {
            **self.env,
            "PATH": f"{fake_bin}:{self.env['PATH']}",
            "PGL_TEST_CALLS": str(persona_calls),
            "PGL_TEST_TRANSPORT_CALLS": str(transport_calls),
            "PGL_TEST_OLD_HASH": current_hash,
        }
        rollback = run(
            sys.executable,
            str(ROLLBACK),
            "luca",
            tag,
            "--date",
            day.isoformat(),
            "--config",
            str(luca_config_path),
            cwd=REPO,
            env=environment,
        )

        self.assertIn(f"before={current_hash} after={restored_hash}", rollback.stdout)
        self.assertEqual(
            (overlay / profile.render_files["candidates"]).read_bytes(),
            restored_candidates,
        )
        self.assertEqual(
            json.loads((staging / "build" / "manifest.json").read_text(encoding="utf-8"))[
                "content_hash"
            ],
            restored_hash,
        )
        self.assertEqual(
            persona_calls.read_text(encoding="utf-8").splitlines(),
            ["build", "doctor", "build", "doctor"],
        )
        self.assertIn(
            "R13-Luca production-stop-seconds=", rollback.stdout
        )
        calls = transport_calls.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("ssh accept deletion", calls)
        self.assertLess(calls.index("ssh deploy backup"), calls.index("ssh deploy restart"))
        self.assertLess(calls.index("ssh deploy restart"), calls.index("ssh accept"))

    def test_eject_preserves_corrupt_ledger_and_empties_render_for_every_fault_class(self) -> None:
        schema_two = dump_ledger(empty_ledger("alpha")).replace(
            b"schema_version: 1", b"schema_version: 2"
        )
        cases = (
            ("conflict", b"<<<<<<< ours\nschema_version: 1\n=======\nface: alpha\n>>>>>>> theirs\n"),
            ("unparseable", b"[\n"),
            ("schema-version", schema_two),
            ("face-mismatch", dump_ledger(empty_ledger("luca"))),
        )
        for offset, (label, corrupt) in enumerate(cases):
            with self.subTest(fault=label):
                day = date(2026, 9, 20) + timedelta(days=offset)
                render = b"injected bytes must be removable\n"
                (self.overlay / "overlay-ledger.yml").write_bytes(corrupt)
                (self.overlay / "overlay.md").write_bytes(render)
                run("git", "add", "overlay-ledger.yml", "overlay.md", cwd=self.overlay)
                run("git", "commit", "-qm", f"seed {label} ledger fault", cwd=self.overlay)
                ledger_before = (self.overlay / "overlay-ledger.yml").read_bytes()
                if offset == 0:
                    blocked = run(
                        sys.executable,
                        str(BLOCK),
                        "alpha",
                        "p-0001",
                        "--date",
                        day.isoformat(),
                        "--config",
                        str(self.config_dir / "growth-alpha.json"),
                        cwd=REPO,
                        env=self.env,
                        check=False,
                    )
                    forgotten = run(
                        sys.executable,
                        str(FORGET),
                        "alpha",
                        "injected",
                        "--date",
                        day.isoformat(),
                        "--config",
                        str(self.config_dir / "growth-alpha.json"),
                        cwd=REPO,
                        env=self.env,
                        check=False,
                    )
                    self.assertNotEqual(blocked.returncode, 0)
                    self.assertNotEqual(forgotten.returncode, 0)
                    self.assertIn(
                        "pgl-eject is the ledger-independent path to zero the injection",
                        blocked.stdout,
                    )
                    self.assertIn(
                        "pgl-eject is the ledger-independent path to zero the injection",
                        forgotten.stdout,
                    )
                self.mirror(day)
                (self.home / "KILLSWITCH").write_text("mode: eject\n", encoding="utf-8")
                ejected = run(
                    sys.executable,
                    str(EJECT),
                    "alpha",
                    "--date",
                    day.isoformat(),
                    "--config",
                    str(self.config_dir / "growth-alpha.json"),
                    cwd=REPO,
                    env=self.env,
                )
                self.assertEqual(ejected.returncode, 0)
                self.assertIn("[RED] alpha: eject ledger fault", ejected.stdout)
                self.assertIn("alpha: eject completed ledger-independent", ejected.stdout)
                self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")
                self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), ledger_before)
                self.assertEqual(run("git", "status", "--porcelain", cwd=self.overlay).stdout, "")
                committed = run(
                    "git", "show", "--name-only", "--pretty=format:", "HEAD", cwd=self.overlay
                ).stdout.splitlines()
                self.assertEqual(committed, ["overlay.md"])
                digest = (self.home / "digest" / f"{day}.md").read_text(encoding="utf-8")
                self.assertIn("[RED] alpha: eject ledger fault", digest)
                self.assertIn("alpha: eject completed ledger-independent", digest)

    def test_eject_preserves_invalid_utf8_ledger_and_blocklist_together(self) -> None:
        day = date(2026, 10, 1)
        ledger_bytes = b"\xff\xfeinvalid ledger\x00"
        blocklist_bytes = b"blocked\n\xff\xfeinvalid blocklist\n"
        (self.overlay / "overlay-ledger.yml").write_bytes(ledger_bytes)
        (self.overlay / "blocklist.txt").write_bytes(blocklist_bytes)
        (self.overlay / "overlay.md").write_bytes(b"injected bytes must be removable\n")
        run(
            "git",
            "add",
            "overlay-ledger.yml",
            "blocklist.txt",
            "overlay.md",
            cwd=self.overlay,
        )
        run("git", "commit", "-qm", "seed combined invalid utf8 state", cwd=self.overlay)
        self.mirror(day)
        (self.home / "KILLSWITCH").write_text("mode: eject\n", encoding="utf-8")
        ejected = run(
            sys.executable,
            str(EJECT),
            "alpha",
            "--date",
            day.isoformat(),
            "--config",
            str(self.config_dir / "growth-alpha.json"),
            cwd=REPO,
            env=self.env,
            check=False,
        )
        self.assertEqual(ejected.returncode, 0, ejected.stdout + ejected.stderr)
        self.assertIn("[RED] alpha: eject ledger fault", ejected.stdout)
        self.assertIn("[RED] alpha: eject blocklist fault", ejected.stdout)
        self.assertIn(
            "eject completed ledger-independent blocklist-independent",
            ejected.stdout,
        )
        self.assertEqual((self.overlay / "overlay.md").read_bytes(), b"")
        self.assertEqual((self.overlay / "overlay-ledger.yml").read_bytes(), ledger_bytes)
        self.assertEqual((self.overlay / "blocklist.txt").read_bytes(), blocklist_bytes)
        self.assertEqual(run("git", "status", "--porcelain", cwd=self.overlay).stdout, "")
        committed = run(
            "git", "show", "--name-only", "--pretty=format:", "HEAD", cwd=self.overlay
        ).stdout.splitlines()
        self.assertEqual(committed, ["overlay.md"])
