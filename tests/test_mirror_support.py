from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from growthlane.ledger import dump_ledger, empty_ledger
from mirror.common import write_snapshot


REPO = Path(__file__).resolve().parents[1]
MIRROR_RESPONDER = REPO / "tests" / "stubs" / "mirror_responder.py"
MIRROR_SCORER = REPO / "tests" / "stubs" / "mirror_scorer.py"


class MirrorHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.fake_home = self.root / "fake-home"
        self.soul_source = self.fake_home / ".claude" / "settings.json"
        self.soul_source.parent.mkdir(parents=True)
        self.soul_source.write_text('{"fixture":"soul"}\n', encoding="utf-8")
        self.home = self.root / "pgl-home"
        self.overlay = self.home / "faces" / "alpha"
        self.overlay.mkdir(parents=True)
        (self.overlay / "overlay.md").write_bytes(b"")
        (self.overlay / "overlay-ledger.yml").write_bytes(dump_ledger(empty_ledger("alpha")))
        (self.overlay / "blocklist.txt").write_bytes(b"")
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        (self.config_dir / "growth-alpha.json").write_text(
            json.dumps(
                {
                    "display_name": "owner",
                    "speaker": "owner",
                    "transcripts_root": "",
                    "writer_argv": [],
                    "reviewer_argv": [],
                    "classifier_argv": [],
                }
            ),
            encoding="utf-8",
        )
        self.write_mirror_config()
        self.env = {**os.environ, "HOME": str(self.fake_home), "PGL_HOME": str(self.home)}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_mirror_config(
        self,
        responder: Optional[Sequence[str]] = None,
        scorer: Optional[Sequence[str]] = None,
    ) -> None:
        (self.config_dir / "mirror-alpha.json").write_text(
            json.dumps(
                {
                    "responder_argv": list(responder or []),
                    "scorer_argv": list(scorer or []),
                    "vault_dir": "",
                }
            ),
            encoding="utf-8",
        )

    def run_module(
        self,
        module: str,
        run_day: date,
        *extra: str,
        env: Optional[Mapping[str, str]] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                module,
                "alpha",
                "--date",
                run_day.isoformat(),
                "--config-dir",
                str(self.config_dir),
                *extra,
            ],
            cwd=REPO,
            env=dict(env or self.env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=check,
        )

    def write_soul_manifest(self, sha256: Optional[str] = None) -> Path:
        payload = {
            "overlay_home": str(self.overlay.resolve()),
            "files": [
                {
                    "path": str(self.soul_source.resolve()),
                    "sha256": sha256 or hashlib.sha256(self.soul_source.read_bytes()).hexdigest(),
                }
            ],
        }
        path = self.home / "soul-baseline" / "alpha.manifest"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
        return path

    def symlink_soul_manifest(self) -> Path:
        target = self.home / "soul-baseline" / "alpha-target.manifest"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
        path = self.home / "soul-baseline" / "alpha.manifest"
        if path.exists() or path.is_symlink():
            path.unlink()
        path.symlink_to(target)
        return path

    def write_ledger(self, ledger: Mapping[str, object]) -> None:
        (self.overlay / "overlay-ledger.yml").write_bytes(dump_ledger(dict(ledger)))

    def write_overlay(self, text: str) -> None:
        (self.overlay / "overlay.md").write_text(text, encoding="utf-8")

    def write_usage(self, run_day: date, records: Iterable[Mapping[str, object]]) -> None:
        path = self.home / "obslog" / "alpha" / ("usage-{day}.jsonl".format(day=run_day.isoformat()))
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for record in records:
            payload = dict(record)
            payload.setdefault("face", "alpha")
            lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        os.chmod(path, 0o600)

    def write_killswitch(self, text: str) -> Path:
        path = self.home / "KILLSWITCH"
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def digest_text(self, run_day: date) -> str:
        return (self.home / "digest" / ("{day}.md".format(day=run_day.isoformat()))).read_text(encoding="utf-8")

    def weekly_report_text(self, run_day: date) -> str:
        return (self.home / "reports" / "weekly" / "alpha" / ("{day}.md".format(day=run_day.isoformat()))).read_text(
            encoding="utf-8"
        )

    def monthly_report_text(self, run_day: date) -> str:
        return (self.home / "reports" / "monthly" / "alpha" / ("{month}.md".format(month=run_day.strftime("%Y-%m")))).read_text(
            encoding="utf-8"
        )

    def tier_s_payload(self, run_day: date) -> Dict[str, object]:
        return json.loads(
            (self.home / "reports" / "tier-s" / "alpha" / ("{day}.json".format(day=run_day.isoformat()))).read_text(
                encoding="utf-8"
            )
        )

    def seed_snapshot(self, run_day: date, files: Mapping[str, bytes]) -> None:
        write_snapshot(self.home, "alpha", run_day.isoformat(), files, always=True)

    def responder_argv(self) -> List[str]:
        return [sys.executable, str(MIRROR_RESPONDER)]

    def scorer_argv(self) -> List[str]:
        return [sys.executable, str(MIRROR_SCORER)]


def day_with_exposure(face: str, phrase_id: str, start: date, expected: bool) -> date:
    from growthlane.holdout import exposed

    for offset in range(14):
        candidate = start + timedelta(days=offset)
        if exposed(face, phrase_id, candidate.isoformat()) == expected:
            return candidate
    raise AssertionError("could not find holdout exposure day for fixture")
