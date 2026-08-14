from __future__ import annotations

import contextlib
import json
import os
import site
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from applier.apply import ApplyError, _content_hash, commit_state
from growthlane.faces import get_profile
from growthlane.gates import DeletionContext, deletion_context
from growthlane.holdout import exposed
from growthlane.ledger import dump_ledger, empty_ledger, load_ledger, new_phrase
from growthlane.notify import Digest
from growthlane.operations import block_main
from growthlane.render import render_files
from growthlane.soul import write_manifest


REPO = Path(__file__).resolve().parents[1]
UCD16_SUBPROCESS_STUB = REPO / "tests" / "stubs" / "ucd16"
JST = timezone(timedelta(hours=9))
BLOCK = REPO / "bin" / "pgl-block"
FORGET = REPO / "bin" / "pgl-forget"
EJECT = REPO / "bin" / "pgl-eject"
NIGHTLY = REPO / "bin" / "pgl-nightly"
ROLLBACK = REPO / "bin" / "pgl-rollback"
BASELINE = REPO / "bin" / "pgl-baseline"


class Fixture:
    def __enter__(self) -> Fixture:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "pgl-home"
        self.overlay = self.home / "faces" / "alpha"
        self.overlay.mkdir(parents=True)
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        transcripts = self.root / "transcripts"
        transcripts.mkdir()
        self.config = {
            "display_name": "test owner",
            "speaker": "owner",
            "transcripts_root": str(transcripts),
            "writer_argv": [],
            "reviewer_argv": [],
            "classifier_argv": [],
        }
        self.config_path = self.config_dir / "growth-alpha.json"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        (self.config_dir / "evidence.yml").write_text(
            (REPO / "config" / "evidence.yml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.profile = get_profile("alpha")
        (self.overlay / self.profile.ledger_path).write_bytes(dump_ledger(empty_ledger("alpha")))
        (self.overlay / self.profile.blocklist_path).write_bytes(b"")
        (self.overlay / "overlay.md").write_bytes(b"")
        self.git("init", "-q")
        self.git("config", "user.name", "PGL Test")
        self.git("config", "user.email", "pgl@example.invalid")
        self.git("add", "--", *self.profile.allowlist)
        self.git("commit", "-qm", "bootstrap")
        self.soul = Path.home() / ".claude" / "settings.json"
        if not self.soul.is_file():
            raise AssertionError("test environment must provide alpha's soul source")
        write_manifest(self.profile, self.home, self.config, [self.soul])
        self.write_gates()
        self.write_mirror()
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
        return self

    def __exit__(self, *_: object) -> None:
        self.temporary.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.overlay, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True,
        )

    def run(self, path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(path), *args], cwd=REPO, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )

    def write_gates(self, *, cp3_go: bool = True) -> None:
        (self.home / "gates.yml").write_text(
            "cp2_in_force: true\ndecided_by: owner\nref: cp2\nfaces:\n"
            f"  alpha:\n    cp3_go: {'true' if cp3_go else 'false'}\n    decided_by: owner\n    ref: cp3\n",
            encoding="utf-8",
        )

    def degrade_gates(self, state: str) -> str:
        if state == "missing":
            (self.home / "gates.yml").unlink()
            return "gates-missing"
        if state == "corrupt":
            (self.home / "gates.yml").write_text("not yaml\n", encoding="utf-8")
            return "gates-invalid"
        self.write_gates(cp3_go=False)
        return "cp3-not-go"

    def write_mirror(self, state: str = "fresh") -> str:
        marker = self.home / "reports" / "weekly" / "latest-alpha.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        if state == "missing":
            marker.unlink(missing_ok=True)
            return "stale(missing)"
        generated = datetime.now(JST).date() - timedelta(days=15 if state == "stale" else 0)
        marker.write_text(json.dumps({"generated_at": generated.isoformat()}), encoding="utf-8")
        return "stale(15d)" if state == "stale" else "fresh"

    def seed_phrase(
        self,
        *,
        text: str = "なるほどだね",
        state: str = "adopted",
        extra_lint: bool = False,
        render_date: str = "2026-08-05",
    ) -> None:
        ledger = empty_ledger("alpha")
        for phrase_id, phrase_text in (("p-0001", text), ("p-0002", "commitしてみよう")):
            if phrase_id == "p-0002" and not extra_lint:
                continue
            phrase = new_phrase(
                phrase_id, phrase_text,
                {"first_seen": "2026-08-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
            )
            phrase["state"] = state
            if state in {"staged", "adopted"}:
                phrase["staged_at"] = "2026-08-01"
            ledger["phrases"].append(phrase)
        (self.overlay / self.profile.ledger_path).write_bytes(dump_ledger(ledger))
        for rel_path, payload in render_files(self.profile, ledger, render_date, self.config["display_name"]).items():
            (self.overlay / rel_path).write_bytes(payload)
        self.git("add", "--", *self.profile.allowlist)
        self.git("commit", "-qm", "seed deletion phrase")

    def seed_custom_phrases(
        self,
        phrases: list[dict[str, str]],
        *,
        render_date: str,
        commit_message: str = "seed custom phrases",
        trusted_snapshot_date: str | None = None,
    ) -> None:
        ledger = empty_ledger("alpha")
        for item in phrases:
            phrase = new_phrase(
                item["id"],
                item["text"],
                {
                    "first_seen": "2026-08-01",
                    "window_count": 8,
                    "distinct_days": 5,
                    "echo_ratio": 0.0,
                },
            )
            phrase["state"] = item["state"]
            if phrase["state"] in {"staged", "adopted"}:
                phrase["staged_at"] = item.get("staged_at", "2026-08-01")
            ledger["phrases"].append(phrase)
        (self.overlay / self.profile.ledger_path).write_bytes(dump_ledger(ledger))
        for rel_path, payload in render_files(
            self.profile, ledger, render_date, self.config["display_name"]
        ).items():
            (self.overlay / rel_path).write_bytes(payload)
        self.git("add", "--", *self.profile.allowlist)
        self.git("commit", "-qm", commit_message)
        if trusted_snapshot_date is not None:
            parent_sha = self.git("rev-parse", "HEAD").stdout.strip()
            ledger = load_ledger(self.overlay / self.profile.ledger_path, "alpha")
            tag = f"overlay-snap-alpha-{trusted_snapshot_date.replace('-', '')}-1"
            rendered = render_files(
                self.profile, ledger, trusted_snapshot_date, self.config["display_name"]
            )
            ledger["snapshots"].append(
                {
                    "at": trusted_snapshot_date,
                    "parent_sha": parent_sha,
                    "tag": tag,
                    "content_hash": _content_hash(self.profile, rendered),
                }
            )
            (self.overlay / self.profile.ledger_path).write_bytes(dump_ledger(ledger))
            self.git("add", "--", self.profile.ledger_path)
            self.git("commit", "-qm", f"{commit_message} snapshot")
            self.git("tag", tag)

    def deletion(self, operation: str) -> subprocess.CompletedProcess[str]:
        common = ("--date", "2026-08-05", "--config", str(self.config_path))
        if operation == "block":
            return self.run(BLOCK, "alpha", "p-0001", *common)
        if operation == "forget":
            return self.run(FORGET, "alpha", "なるほど", *common)
        (self.home / "KILLSWITCH").write_text("mode: eject\n", encoding="utf-8")
        return self.run(EJECT, "alpha", *common)

    def digest(self) -> str:
        return (self.home / "digest" / "2026-08-05.md").read_text(encoding="utf-8")


class DeletionGatesE2ETests(unittest.TestCase):
    def test_block_missing_phrase_reports_explicit_diagnostic(self) -> None:
        with Fixture() as fixture:
            ledger_before = (fixture.overlay / fixture.profile.ledger_path).read_bytes()
            result = fixture.run(
                BLOCK,
                "alpha",
                "p-missing",
                "--date",
                "2026-08-05",
                "--config",
                str(fixture.config_path),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "[RED] alpha: block failed: phrase not found p-missing",
                result.stdout,
            )
            self.assertEqual(
                (fixture.overlay / fixture.profile.ledger_path).read_bytes(),
                ledger_before,
            )
            self.assertEqual(fixture.git("status", "--porcelain").stdout, "")

    def test_deletion_context_is_closed_even_when_constructed_directly(self) -> None:
        for operation in ("block", "forget", "eject"):
            with self.subTest(operation=operation):
                self.assertEqual(DeletionContext(operation=operation).operation, operation)
        with self.assertRaisesRegex(ValueError, "unknown deletion operation"):
            DeletionContext(operation="nightly")

    def test_deletion_matrix_audits_gate_and_mirror_exceptions(self) -> None:
        for operation in ("block", "forget", "eject"):
            for gate in ("missing", "corrupt", "cp3-off"):
                for mirror in ("stale", "missing"):
                    with self.subTest(operation=operation, gate=gate, mirror=mirror), Fixture() as fixture:
                        fixture.seed_phrase()
                        gate_audit = fixture.degrade_gates(gate)
                        mirror_audit = fixture.write_mirror(mirror)
                        result = fixture.deletion(operation)
                        self.assertEqual(result.returncode, 0, result.stdout)
                        self.assertIn("[RED] alpha: deletion audit", result.stdout)
                        self.assertIn(gate_audit, result.stdout)
                        self.assertIn(mirror_audit, result.stdout)
                        self.assertIn("[RED] alpha: deletion audit", fixture.digest())
                        self.assertIn(gate_audit, fixture.digest())
                        self.assertIn(mirror_audit, fixture.digest())
                        message = fixture.git("log", "-1", "--format=%B").stdout
                        self.assertIn(f"Gates-State: unverified({gate_audit})", message)
                        self.assertIn(f"Mirror-Liveness: {mirror_audit}", message)
                        history = json.dumps(
                            load_ledger(fixture.overlay / fixture.profile.ledger_path, "alpha")["history"],
                            ensure_ascii=False,
                        )
                        self.assertIn(gate_audit, history)
                        self.assertIn(mirror_audit, history)

    def test_corrupt_ledger_eject_emits_three_audits_and_block_forget_fail(self) -> None:
        for gate in ("missing", "corrupt"):
            for mirror in ("stale", "missing"):
                with self.subTest(gate=gate, mirror=mirror), Fixture() as fixture:
                    corrupt = b"not: [valid\n"
                    (fixture.overlay / fixture.profile.ledger_path).write_bytes(corrupt)
                    (fixture.overlay / "overlay.md").write_text("injected bytes\n", encoding="utf-8")
                    fixture.git("add", "--", fixture.profile.ledger_path, "overlay.md")
                    fixture.git("commit", "-qm", "seed corrupt ledger")
                    gate_audit = fixture.degrade_gates(gate)
                    mirror_audit = fixture.write_mirror(mirror)
                    for operation in ("block", "forget"):
                        denied = fixture.deletion(operation)
                        self.assertNotEqual(denied.returncode, 0)
                        self.assertIn("requires a valid ledger", denied.stdout)
                    ejected = fixture.deletion("eject")
                    self.assertEqual(ejected.returncode, 0, ejected.stdout)
                    self.assertEqual((fixture.overlay / fixture.profile.ledger_path).read_bytes(), corrupt)
                    self.assertEqual((fixture.overlay / "overlay.md").read_bytes(), b"")
                    message = fixture.git("log", "-1", "--format=%B").stdout
                    self.assertIn(f"Gates-State: unverified({gate_audit})", message)
                    self.assertIn(f"Mirror-Liveness: {mirror_audit}", message)
                    self.assertIn("Monotonicity: unverifiable-ledger", message)
                    digest = fixture.digest()
                    for expected in (gate_audit, mirror_audit, "monotonicity=unverifiable-ledger"):
                        self.assertIn(expected, digest)
                        self.assertIn("[RED] alpha: deletion audit", digest)

    def test_monotonicity_injections_abort_and_revert_every_plane(self) -> None:
        for attack in ("candidate", "blocklist", "render"):
            with self.subTest(attack=attack), Fixture() as fixture:
                fixture.seed_phrase()
                if attack == "blocklist":
                    (fixture.overlay / fixture.profile.blocklist_path).write_text("older-one\nolder-two\n", encoding="utf-8")
                    fixture.git("add", "--", fixture.profile.blocklist_path)
                    fixture.git("commit", "-qm", "seed durable blocklist")
                before = {name: (fixture.overlay / name).read_bytes() for name in fixture.profile.allowlist}
                head = fixture.git("rev-parse", "HEAD").stdout.strip()
                ledger = load_ledger(fixture.overlay / fixture.profile.ledger_path, "alpha")
                ledger["phrases"][0]["state"] = "blocked"
                context = deletion_context("block", target_phrase_ids=("p-0001",), target_phrase_texts=("なるほどだね",))
                blocklist = ["older-one", "older-two", "なるほどだね"]
                patcher: object = contextlib.nullcontext()
                if attack == "candidate":
                    ledger["phrases"].append(new_phrase(
                        "p-0002", "new candidate",
                        {"first_seen": "2026-08-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
                    ))
                    expected = "candidate expanded"
                elif attack == "blocklist":
                    blocklist = ["older-one"]
                    expected = "blocklist shrank"
                else:
                    patcher = mock.patch(
                        "applier.apply.render_files",
                        return_value={"overlay.md": b"unstructured attacker phrase\n"},
                    )
                    expected = "render expanded"
                with patcher:
                    with self.assertRaisesRegex(ApplyError, expected):
                        commit_state(
                            fixture.profile, fixture.home, fixture.config, "2026-08-05", ledger,
                            blocklist, Digest(fixture.home, "2026-08-05"), deletion=context,
                        )
                self.assertEqual({name: (fixture.overlay / name).read_bytes() for name in fixture.profile.allowlist}, before)
                self.assertEqual(fixture.git("rev-parse", "HEAD").stdout.strip(), head)
                self.assertEqual(fixture.git("status", "--porcelain").stdout, "")
                self.assertIn("[RED] alpha: abort/revert", fixture.digest())

    def test_pre_effectiveness_missing_or_corrupt_gates_deny_deletion(self) -> None:
        for gate in ("missing", "corrupt"):
            with self.subTest(gate=gate), Fixture() as fixture:
                fixture.seed_phrase()
                for path in (fixture.home / "soul-baseline").iterdir():
                    path.unlink()
                (fixture.home / "soul-baseline").rmdir()
                fixture.degrade_gates(gate)
                denied = fixture.deletion("block")
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("gates", denied.stdout)
                self.assertEqual(
                    load_ledger(fixture.overlay / fixture.profile.ledger_path, "alpha")["phrases"][0]["state"],
                    "adopted",
                )

    def test_cp2_invalid_is_a_witnessed_audit_but_explicit_false_always_denies(self) -> None:
        with Fixture() as fixture:
            fixture.seed_phrase()
            (fixture.home / "gates.yml").write_text(
                "decided_by: owner\nref: cp2\nfaces:\n  alpha:\n    cp3_go: true\n    decided_by: owner\n    ref: cp3\n",
                encoding="utf-8",
            )
            fixture.write_mirror("stale")
            audited = fixture.deletion("block")
            self.assertEqual(audited.returncode, 0, audited.stdout)
            self.assertIn("gates-invalid", fixture.git("log", "-1", "--format=%B").stdout)
        for operation in ("block", "forget", "eject"):
            with self.subTest(operation=operation), Fixture() as fixture:
                fixture.seed_phrase()
                (fixture.home / "gates.yml").write_text(
                    "cp2_in_force: false\ndecided_by: owner\nref: cp2\nfaces:\n"
                    "  alpha:\n    cp3_go: true\n    decided_by: owner\n    ref: cp3\n",
                    encoding="utf-8",
                )
                before = {name: (fixture.overlay / name).read_bytes() for name in fixture.profile.allowlist}
                denied = fixture.deletion(operation)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("CP-2 is not in force", denied.stdout)
                self.assertEqual({name: (fixture.overlay / name).read_bytes() for name in fixture.profile.allowlist}, before)

    def test_soul_mismatch_stops_deletion_until_mirror_independent_baseline_repair(self) -> None:
        for operation in ("block", "forget", "eject"):
            with self.subTest(operation=operation), Fixture() as fixture:
                fixture.seed_phrase(extra_lint=True)
                fixture.write_mirror("stale")
                manifest_path = fixture.profile.baseline_manifest(fixture.home)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["files"][0]["sha256"] = "0" * 64
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                before = {name: (fixture.overlay / name).read_bytes() for name in fixture.profile.allowlist}
                denied = fixture.deletion(operation)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("soul hash mismatch", denied.stdout)
                self.assertEqual({name: (fixture.overlay / name).read_bytes() for name in fixture.profile.allowlist}, before)
                (fixture.home / "KILLSWITCH").unlink(missing_ok=True)
                repaired = fixture.run(
                    BASELINE, "alpha", str(fixture.soul), "--config", str(fixture.config_path), "--confirm-root-change"
                )
                self.assertEqual(repaired.returncode, 0, repaired.stdout)
                self.assertEqual(fixture.deletion(operation).returncode, 0)

    def test_deletion_skips_lint_for_target_and_leaves_other_invalid_phrase_untouched(self) -> None:
        with Fixture() as fixture:
            fixture.seed_phrase(text="commitしよう", extra_lint=True)
            blocked = fixture.deletion("block")
            self.assertEqual(blocked.returncode, 0, blocked.stdout)
            phrases = load_ledger(fixture.overlay / fixture.profile.ledger_path, "alpha")["phrases"]
            self.assertEqual([(item["id"], item["state"]) for item in phrases], [("p-0001", "blocked"), ("p-0002", "adopted")])

    def test_deletion_rerender_keeps_previous_day_staged_holdout_hidden_with_or_without_anchor(self) -> None:
        for operation in ("block", "forget"):
            for anchored in (False, True):
                with self.subTest(operation=operation, anchored=anchored), Fixture() as fixture:
                    self.assertFalse(exposed("alpha", "p-0001", "2026-08-04"))
                    self.assertTrue(exposed("alpha", "p-0001", "2026-08-05"))
                    fixture.seed_custom_phrases(
                        [
                            {"id": "p-0001", "text": "日替わり候補", "state": "staged"},
                            {"id": "p-0002", "text": "削除対象", "state": "adopted"},
                        ],
                        render_date="2026-08-04",
                        commit_message="seed holdout drift fixture",
                        trusted_snapshot_date="2026-08-04" if anchored else None,
                    )
                    (fixture.home / "KILLSWITCH").write_text("mode: freeze\n", encoding="utf-8")
                    gate_audit = fixture.degrade_gates("missing")
                    mirror_audit = fixture.write_mirror("stale")
                    result = (
                        fixture.run(
                            BLOCK,
                            "alpha",
                            "p-0002",
                            "--date",
                            "2026-08-05",
                            "--config",
                            str(fixture.config_path),
                        )
                        if operation == "block"
                        else fixture.run(
                            FORGET,
                            "alpha",
                            "削除対象",
                            "--date",
                            "2026-08-05",
                            "--config",
                            str(fixture.config_path),
                        )
                    )
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertIn(gate_audit, result.stdout)
                    self.assertIn(mirror_audit, result.stdout)
                    overlay = (fixture.overlay / "overlay.md").read_text(encoding="utf-8")
                    self.assertNotIn("日替わり候補", overlay)

    def test_deletion_after_verified_rollback_clamps_next_day_holdout_drift(self) -> None:
        for operation in ("block", "forget"):
            with self.subTest(operation=operation), Fixture() as fixture:
                fixture.seed_custom_phrases(
                    [
                        {"id": "p-0001", "text": "日替わり候補", "state": "staged"},
                        {"id": "p-0002", "text": "最初の削除対象", "state": "adopted"},
                        {"id": "p-0003", "text": "次の削除対象", "state": "adopted"},
                    ],
                    render_date="2026-08-04",
                    commit_message="seed rollback drift fixture",
                    trusted_snapshot_date="2026-08-04",
                )
                first_block = fixture.run(
                    BLOCK,
                    "alpha",
                    "p-0002",
                    "--date",
                    "2026-08-04",
                    "--config",
                    str(fixture.config_path),
                )
                self.assertEqual(first_block.returncode, 0, first_block.stdout)
                rollback = fixture.run(
                    ROLLBACK,
                    "alpha",
                    "overlay-snap-alpha-20260804-1",
                    "--date",
                    "2026-08-04",
                    "--config",
                    str(fixture.config_path),
                )
                self.assertEqual(rollback.returncode, 0, rollback.stdout)
                (fixture.home / "KILLSWITCH").write_text("mode: freeze\n", encoding="utf-8")
                gate_audit = fixture.degrade_gates("missing")
                mirror_audit = fixture.write_mirror("stale")
                result = (
                    fixture.run(
                        BLOCK,
                        "alpha",
                        "p-0003",
                        "--date",
                        "2026-08-05",
                        "--config",
                        str(fixture.config_path),
                    )
                    if operation == "block"
                    else fixture.run(
                        FORGET,
                        "alpha",
                        "次の削除対象",
                        "--date",
                        "2026-08-05",
                        "--config",
                        str(fixture.config_path),
                    )
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn(gate_audit, result.stdout)
                self.assertIn(mirror_audit, result.stdout)
                overlay = (fixture.overlay / "overlay.md").read_text(encoding="utf-8")
                self.assertNotIn("日替わり候補", overlay)
                self.assertNotIn("次の削除対象", overlay)

    def test_block_does_not_abort_on_remaining_phrase_substring_match(self) -> None:
        with Fixture() as fixture:
            fixture.seed_custom_phrases(
                [
                    {"id": "p-0001", "text": "なるほど", "state": "adopted"},
                    {"id": "p-0002", "text": "なるほどね", "state": "adopted"},
                ],
                render_date="2026-08-05",
                commit_message="seed substring sibling fixture",
            )
            result = fixture.deletion("block")
            self.assertEqual(result.returncode, 0, result.stdout)
            overlay = (fixture.overlay / "overlay.md").read_text(encoding="utf-8")
            self.assertIn("なるほどね", overlay)
            self.assertNotIn("なるほど、", overlay)
            self.assertEqual(
                load_ledger(fixture.overlay / fixture.profile.ledger_path, "alpha")["phrases"][0]["state"],
                "blocked",
            )

    def test_input_boundary_staged_injection_aborts_as_staged_expanded(self) -> None:
        with Fixture() as fixture:
            fixture.seed_custom_phrases(
                [{"id": "p-0002", "text": "正規の対象", "state": "adopted"}],
                render_date="2026-08-04",
                commit_message="seed injection guard fixture",
            )
            overlay_before = (fixture.overlay / "overlay.md").read_bytes()
            ledger = load_ledger(fixture.overlay / fixture.profile.ledger_path, "alpha")
            injected = new_phrase(
                "p-0001",
                "注入された候補",
                {
                    "first_seen": "2026-08-01",
                    "window_count": 8,
                    "distinct_days": 5,
                    "echo_ratio": 0.0,
                },
            )
            injected["state"] = "staged"
            injected["staged_at"] = "2026-08-01"
            ledger["phrases"].append(injected)
            injected_head = fixture.git("rev-parse", "HEAD").stdout.strip()
            with (
                mock.patch.dict(os.environ, {"PGL_HOME": str(fixture.home)}),
                mock.patch("growthlane.operations._load_required_ledger", return_value=ledger),
            ):
                result = block_main(
                    [
                        "alpha",
                        "p-0002",
                        "--date",
                        "2026-08-05",
                        "--config",
                        str(fixture.config_path),
                    ]
                )
            self.assertEqual(result, 1)
            self.assertEqual((fixture.overlay / "overlay.md").read_bytes(), overlay_before)
            self.assertEqual(fixture.git("rev-parse", "HEAD").stdout.strip(), injected_head)
            self.assertIn("staged expanded", fixture.digest())
            self.assertIn("[RED] alpha: abort/revert", fixture.digest())

    def test_disk_adopted_injection_aborts_on_render_expansion(self) -> None:
        with Fixture() as fixture:
            fixture.seed_custom_phrases(
                [{"id": "p-0002", "text": "正規の対象", "state": "adopted"}],
                render_date="2026-08-04",
                commit_message="seed adopted injection fixture",
            )
            render_before = {
                path: (fixture.overlay / path).read_bytes()
                for path in fixture.profile.render_files.values()
            }
            ledger = load_ledger(fixture.overlay / fixture.profile.ledger_path, "alpha")
            injected = new_phrase(
                "p-0001",
                "注入された adopted phrase",
                {
                    "first_seen": "2026-08-01",
                    "window_count": 8,
                    "distinct_days": 5,
                    "echo_ratio": 0.0,
                },
            )
            injected["state"] = "adopted"
            injected["staged_at"] = "2026-08-01"
            ledger["phrases"].append(injected)
            (fixture.overlay / fixture.profile.ledger_path).write_bytes(dump_ledger(ledger))
            fixture.git("add", "--", fixture.profile.ledger_path)
            fixture.git("commit", "-qm", "inject adopted phrase on disk")
            injected_head = fixture.git("rev-parse", "HEAD").stdout.strip()

            result = fixture.run(
                BLOCK,
                "alpha",
                "p-0002",
                "--date",
                "2026-08-05",
                "--config",
                str(fixture.config_path),
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("render expanded", result.stdout)
            self.assertEqual(
                {
                    path: (fixture.overlay / path).read_bytes()
                    for path in fixture.profile.render_files.values()
                },
                render_before,
            )
            self.assertEqual(fixture.git("rev-parse", "HEAD").stdout.strip(), injected_head)
            self.assertIn("[RED] alpha: abort/revert", fixture.digest())

    def test_disk_staged_injection_is_clamped_out_and_deletion_succeeds(self) -> None:
        with Fixture() as fixture:
            fixture.seed_custom_phrases(
                [{"id": "p-0002", "text": "正規の対象", "state": "adopted"}],
                render_date="2026-08-04",
                commit_message="seed staged injection fixture",
            )
            self.assertTrue(exposed("alpha", "p-0001", "2026-08-05"))
            injected_text = "注入された staged phrase"
            ledger = load_ledger(fixture.overlay / fixture.profile.ledger_path, "alpha")
            injected = new_phrase(
                "p-0001",
                injected_text,
                {
                    "first_seen": "2026-08-01",
                    "window_count": 8,
                    "distinct_days": 5,
                    "echo_ratio": 0.0,
                },
            )
            injected["state"] = "staged"
            injected["staged_at"] = "2026-08-01"
            ledger["phrases"].append(injected)
            (fixture.overlay / fixture.profile.ledger_path).write_bytes(dump_ledger(ledger))
            fixture.git("add", "--", fixture.profile.ledger_path)
            fixture.git("commit", "-qm", "inject staged phrase on disk")
            injected_head = fixture.git("rev-parse", "HEAD").stdout.strip()

            result = fixture.run(
                BLOCK,
                "alpha",
                "p-0002",
                "--date",
                "2026-08-05",
                "--config",
                str(fixture.config_path),
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            for path in fixture.profile.render_files.values():
                self.assertNotIn(
                    injected_text,
                    (fixture.overlay / path).read_text(encoding="utf-8"),
                )
            phrases = load_ledger(
                fixture.overlay / fixture.profile.ledger_path,
                "alpha",
            )["phrases"]
            self.assertEqual(
                next(item for item in phrases if item["id"] == "p-0001")["state"],
                "staged",
            )
            self.assertEqual(
                next(item for item in phrases if item["id"] == "p-0002")["state"],
                "blocked",
            )
            deletion_head = fixture.git("rev-parse", "HEAD").stdout.strip()
            self.assertNotEqual(deletion_head, injected_head)
            self.assertEqual(fixture.git("rev-parse", "HEAD^").stdout.strip(), injected_head)

    def test_deletion_keeps_full_staged_entry_cap_even_when_prior_render_clamps_visibility(self) -> None:
        with Fixture() as fixture:
            phrases = [
                {"id": f"p-{index:04d}", "text": f"候補{index}", "state": "staged"}
                for index in range(1, 14)
            ]
            phrases.append({"id": "p-0200", "text": "削除対象", "state": "adopted"})
            fixture.seed_custom_phrases(
                phrases,
                render_date="2026-08-04",
                commit_message="seed staged cap fixture",
                trusted_snapshot_date="2026-08-04",
            )
            result = fixture.run(
                BLOCK,
                "alpha",
                "p-0200",
                "--date",
                "2026-08-05",
                "--config",
                str(fixture.config_path),
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("candidate entry cap exceeded", result.stdout)

    def test_nightly_and_rollback_remain_fail_closed_on_missing_gates(self) -> None:
        with Fixture() as fixture:
            fixture.seed_phrase()
            (fixture.home / "gates.yml").unlink()
            ledger_before = (fixture.overlay / fixture.profile.ledger_path).read_bytes()
            head_before = fixture.git("rev-parse", "HEAD").stdout.strip()
            nightly = fixture.run(NIGHTLY, "--face", "alpha", "--date", "2026-08-05", "--config-dir", str(fixture.config_dir))
            rollback = fixture.run(
                ROLLBACK, "alpha", "overlay-snap-alpha-20260805-1", "--date", "2026-08-05", "--config", str(fixture.config_path)
            )
            self.assertIn("missing or unreadable gates file", nightly.stdout)
            self.assertEqual((fixture.overlay / fixture.profile.ledger_path).read_bytes(), ledger_before)
            self.assertEqual(fixture.git("rev-parse", "HEAD").stdout.strip(), head_before)
            self.assertNotEqual(rollback.returncode, 0)
            self.assertIn("missing or unreadable gates file", rollback.stdout)

    def test_alpha_rollback_still_blocks_explicit_cp3_false(self) -> None:
        with Fixture() as fixture:
            fixture.seed_custom_phrases(
                [{"id": "p-0001", "text": "rollback target", "state": "adopted"}],
                render_date="2026-08-05",
                trusted_snapshot_date="2026-08-05",
            )
            fixture.write_gates(cp3_go=False)
            before = {
                name: (fixture.overlay / name).read_bytes()
                for name in fixture.profile.allowlist
            }
            rollback = fixture.run(
                ROLLBACK,
                "alpha",
                "overlay-snap-alpha-20260805-1",
                "--date",
                "2026-08-05",
                "--config",
                str(fixture.config_path),
            )
            self.assertNotEqual(rollback.returncode, 0)
            self.assertIn("CP-3 is not GO for alpha", rollback.stdout)
            self.assertEqual(
                {
                    name: (fixture.overlay / name).read_bytes()
                    for name in fixture.profile.allowlist
                },
                before,
            )

    def test_alpha_rollback_still_blocks_explicit_cp2_false(self) -> None:
        with Fixture() as fixture:
            fixture.seed_custom_phrases(
                [{"id": "p-0001", "text": "rollback target", "state": "adopted"}],
                render_date="2026-08-05",
                trusted_snapshot_date="2026-08-05",
            )
            (fixture.home / "gates.yml").write_text(
                "cp2_in_force: false\n"
                "decided_by: owner\n"
                "ref: cp2\n"
                "faces:\n"
                "  alpha:\n"
                "    cp3_go: true\n"
                "    decided_by: owner\n"
                "    ref: cp3\n",
                encoding="utf-8",
            )
            before = {
                name: (fixture.overlay / name).read_bytes()
                for name in fixture.profile.allowlist
            }
            source_head = fixture.git("rev-parse", "HEAD").stdout.strip()
            rollback = fixture.run(
                ROLLBACK,
                "alpha",
                "overlay-snap-alpha-20260805-1",
                "--date",
                "2026-08-05",
                "--config",
                str(fixture.config_path),
            )
            self.assertNotEqual(rollback.returncode, 0)
            self.assertIn("CP-2 is not in force", rollback.stdout)
            self.assertEqual(
                {
                    name: (fixture.overlay / name).read_bytes()
                    for name in fixture.profile.allowlist
                },
                before,
            )
            self.assertEqual(
                fixture.git("rev-parse", "HEAD").stdout.strip(), source_head
            )


if __name__ == "__main__":
    unittest.main()
