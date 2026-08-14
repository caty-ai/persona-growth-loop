from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from mirror.common import MirrorError, load_configs


CONFIG_DIR = Path("config")


class ShippedConfigTests(unittest.TestCase):
    def test_growth_configs_load_for_every_face(self) -> None:
        expected_soul_alert_argv = {"alpha": [], "luca": None}

        for face, expected in expected_soul_alert_argv.items():
            with self.subTest(face=face):
                growth, _, profile = load_configs(face, CONFIG_DIR)

                self.assertEqual(profile.name, face)
                if expected is None:
                    self.assertNotIn("soul_alert_argv", growth)
                else:
                    self.assertEqual(growth["soul_alert_argv"], expected)

    def test_unknown_growth_config_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "config"
            shutil.copytree(CONFIG_DIR, config_dir)
            growth_path = config_dir / "growth-alpha.json"
            growth = json.loads(growth_path.read_text(encoding="utf-8"))
            growth["bogus_key"] = True
            growth_path.write_text(
                json.dumps(growth, ensure_ascii=False) + "\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(
                MirrorError, "alpha growth config keys must be exactly"
            ):
                load_configs("alpha", config_dir)


if __name__ == "__main__":
    unittest.main()
