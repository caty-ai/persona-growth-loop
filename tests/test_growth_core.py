from __future__ import annotations

import os
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

from applier.apply import ApplyError, _render_caps, write_guarded
from growthlane.faces import get_profile
from growthlane.holdout import exposed, proposal_id
from growthlane.ledger import LedgerVersionError, dump_ledger, empty_ledger, load_ledger, new_phrase
from growthlane.render import render_files
from growthlane.notify import Digest
from growthlane.tripwire import inspect
from reviewd.diff_review import review
from writerd.propose import AdapterError


class GrowthCoreTests(unittest.TestCase):
    def test_holdout_and_proposal_formulas_are_exact(self) -> None:
        import hashlib

        raw = "alphap-00012026-08-02".encode("utf-8")
        self.assertEqual(exposed("alpha", "p-0001", "2026-08-02"), hashlib.sha256(raw).digest()[-1] % 2 == 0)
        expected = hashlib.sha256("alphap-0001candidate->staged2026-08-02".encode()).hexdigest()
        self.assertEqual(proposal_id("alpha", "p-0001", "candidate->staged", "2026-08-02"), expected)

    def test_unknown_ledger_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.yml"
            path.write_text("schema_version: 2\nface: alpha\nphrases: []\n", encoding="utf-8")
            with self.assertRaises(LedgerVersionError):
                load_ledger(path, "alpha")

    def test_empty_render_is_zero_bytes(self) -> None:
        ledger = empty_ledger("alpha")
        self.assertEqual(render_files(get_profile("alpha"), ledger, "2026-08-02", "オーナー")["overlay.md"], b"")

    def test_guarded_writer_rejects_outside_traversal_symlink_and_soul(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "faces" / "alpha"
            home.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            profile = get_profile("alpha")
            write_guarded(profile, home, "overlay.md", b"safe\n")
            self.assertEqual((home / "overlay.md").read_bytes(), b"safe\n")
            for path in ("outside.txt", "../outside.txt", str(root / "soul-baseline" / "alpha.manifest")):
                with self.subTest(path=path), self.assertRaises(ApplyError):
                    write_guarded(profile, home, path, b"bad")
            (home / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ApplyError):
                write_guarded(profile, home, "linked/escape", b"bad")
            (home / "overlay.md").unlink()
            (home / "overlay.md").symlink_to(outside / "escaped.md")
            with self.assertRaises(ApplyError):
                write_guarded(profile, home, "overlay.md", b"bad")

    def test_guarded_writer_rejects_final_component_symlink_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "faces" / "alpha"
            home.mkdir(parents=True)
            target = home / "overlay.md"
            target.write_text("old\n", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            original_write = os.write

            def swap_after_temp_write(descriptor: int, payload: bytes) -> int:
                written = original_write(descriptor, payload)
                target.unlink()
                target.symlink_to(outside)
                return written

            with mock.patch("applier.apply.os.write", side_effect=swap_after_temp_write):
                with self.assertRaisesRegex(ApplyError, "symlinked allowlist target"):
                    write_guarded(get_profile("alpha"), home, "overlay.md", b"new\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
            self.assertTrue(target.is_symlink())
            self.assertFalse((home / f".overlay.md.pgl-tmp-{os.getpid()}").exists())

    def test_dump_is_deterministic(self) -> None:
        ledger = empty_ledger("alpha")
        self.assertEqual(dump_ledger(ledger), dump_ledger(ledger))

    def test_adopted_byte_and_entry_caps_reject_before_write(self) -> None:
        profile = get_profile("alpha")
        byte_overflow = empty_ledger("alpha")
        for index in range(25):
            phrase = new_phrase(
                f"p-{index + 1:04d}", "あ" * 24,
                {"first_seen": "2026-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
            )
            phrase["state"] = "adopted"
            byte_overflow["phrases"].append(phrase)
        with self.assertRaisesRegex(ApplyError, "byte cap"):
            _render_caps(profile, byte_overflow, "2026-08-02", "利用者")
        entry_overflow = empty_ledger("alpha")
        for index in range(41):
            phrase = new_phrase(
                f"p-{index + 1:04d}", "いいね",
                {"first_seen": "2026-01-01", "window_count": 8, "distinct_days": 5, "echo_ratio": 0.0},
            )
            phrase["state"] = "adopted"
            entry_overflow["phrases"].append(phrase)
        with self.assertRaisesRegex(ApplyError, "entry cap"):
            _render_caps(profile, entry_overflow, "2026-08-02", "利用者")

    def test_garbage_and_missing_reviewer_output_fail_closed(self) -> None:
        for code in ("print('garbage')", "print('{}')"):
            with self.subTest(code=code), self.assertRaises(AdapterError):
                review([sys.executable, "-c", code], {"proposal": {}, "diff": {}})

    def test_tripwire_proposes_but_never_enables_killswitch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            overlay = home / "faces" / "alpha"
            overlay.mkdir(parents=True)
            (overlay / "overlay.md").write_bytes(("利用者がよく使う言い回し: " + "あ" * 700).encode("utf-8"))
            digest = Digest(home, "2026-08-02")
            self.assertTrue(inspect(get_profile("alpha"), home, {}, digest))
            self.assertTrue((home / "KILLSWITCH.proposed").is_file())
            self.assertFalse((home / "KILLSWITCH").exists())
