from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from applier import apply as apply_module
from growthlane.faces import get_profile
from growthlane.persona_cli import PersonaCliError, persona_argv
from support import canonical_temporary_directory


HASH = "a" * 64


class PersonaCliTests(unittest.TestCase):
    def _write_cli(self, clone: Path) -> Path:
        cli = clone / "packages" / "core" / "bin" / "persona"
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text("fixture\n", encoding="utf-8")
        return cli

    def _build_fixture(self, root: Path) -> tuple[object, Path, Path, dict[str, object]]:
        profile = get_profile("luca")
        clone = root / "luca-repo"
        staging = root / "luca-staging"
        pack = clone / "persona-engine"
        (pack / "catalogs" / "overlay").mkdir(parents=True)
        (pack / "catalogs" / "overlay" / "adopted.txt").write_bytes(b"")
        (pack / "catalogs" / "overlay" / "candidates.txt").write_bytes(b"")
        (pack / "modes").mkdir()
        (pack / "modes" / "public.yml").write_text(
            "schema_version: 2\nid: public\n", encoding="utf-8"
        )
        (pack / "manifest.yml").write_text(
            "schema_version: 2\nname: fixture\n", encoding="utf-8"
        )
        (pack / "aliases.yml").write_text(
            "schema_version: 2\naliases: []\n", encoding="utf-8"
        )
        template = clone / "tests" / "luca-pack" / "install.yml"
        template.parent.mkdir(parents=True)
        template.write_text(
            "schema_version: 2\n"
            "pack: ../../persona-engine\n"
            "placeholders:\n"
            "  agent-name: ルカ\n"
            "  user: オーナー\n"
            "  owner-name: オーナー\n"
            "runtime: generic\n"
            "routes:\n"
            "  - id: fixture-public\n"
            "    match: {}\n"
            "    allowed_modes: [public]\n"
            "    switching: deny\n"
            "    state_domain: quarantine\n"
            "default_route:\n"
            "  state_domain: quarantine\n",
            encoding="utf-8",
        )
        self._write_cli(clone)
        staging.mkdir()
        (staging / "install.yml").write_text(
            "schema_version: 2\npack: pack\n", encoding="utf-8"
        )
        config = {
            "display_name": "オーナー",
            "speaker": "owner",
            "transcripts_root": "",
            "overlay_home_root": str(clone),
            "staging_root": str(staging),
            "writer_argv": [],
            "reviewer_argv": [],
            "classifier_argv": [],
        }
        return profile, clone, staging, config

    def _install_fake_node(
        self,
        root: Path,
        *,
        build_hash: str = HASH,
        lexical_dir: bool = False,
    ) -> dict[str, str]:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        node = fake_bin / "node"
        root_expr = (
            "Path(os.path.abspath(sys.argv[sys.argv.index('--dir') + 1]))"
            if lexical_dir
            else "Path(sys.argv[sys.argv.index('--dir') + 1])"
        )
        node.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"root = {root_expr}\n"
            "command = sys.argv[2]\n"
            "if command == 'build':\n"
            "    (root / 'build').mkdir(parents=True, exist_ok=True)\n"
            "    (root / 'build' / 'manifest.json').write_text("
            f"json.dumps({{'content_hash': '{build_hash}'}}) + '\\n', encoding='utf-8')\n"
            "    print(json.dumps({'ok': True, 'manifest': {'content_hash': '"
            + build_hash
            + "'}}))\n"
            "elif command == 'doctor':\n"
            "    print(json.dumps({'ok': True, 'issues': []}))\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        node.chmod(node.stat().st_mode | stat.S_IXUSR)
        return {
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        }

    def _install_decoy_persona(
        self,
        root: Path,
        *,
        content_hash: str,
    ) -> None:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        persona = fake_bin / "persona"
        persona.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "root = Path(sys.argv[sys.argv.index('--dir') + 1]) if '--dir' in sys.argv else Path.cwd()\n"
            "command = sys.argv[1]\n"
            "if command == 'build':\n"
            "    (root / 'build').mkdir(parents=True, exist_ok=True)\n"
            "    (root / 'build' / 'manifest.json').write_text("
            f"json.dumps({{'content_hash': '{content_hash}'}}) + '\\n', encoding='utf-8')\n"
            "    print(json.dumps({'ok': True, 'manifest': {'content_hash': '"
            + content_hash
            + "'}}))\n"
            "elif command == 'doctor':\n"
            "    print(json.dumps({'ok': True, 'issues': []}))\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        persona.chmod(persona.stat().st_mode | stat.S_IXUSR)

    def _argv_dir(self, argv: tuple[str, ...]) -> str:
        return argv[argv.index("--dir") + 1]

    def test_build_is_independent_of_persona_on_path(self) -> None:
        decoy_hash = "b" * 64
        with canonical_temporary_directory() as temporary:
            root = Path(temporary).resolve()
            profile, clone, _staging, config = self._build_fixture(root)
            env = self._install_fake_node(root)
            self._install_decoy_persona(root, content_hash=decoy_hash)
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertEqual(apply_module._build(profile, clone, {}, config), HASH)

    def test_run_persona_uses_expected_launch_argv(self) -> None:
        with canonical_temporary_directory() as temporary:
            root = Path(temporary).resolve()
            clone = root / "clone"
            install_root = root / "install"
            install_root.mkdir()
            cli = self._write_cli(clone)
            completed = subprocess.CompletedProcess(("node", str(cli), "build", "--dir", str(install_root)), 0, b"", b"")
            with mock.patch("applier.apply.subprocess.run", return_value=completed) as run_mock:
                apply_module._run_persona("build", clone, install_root)
            self.assertEqual(
                run_mock.call_args.args[0],
                ("node", str(cli), "build", "--dir", str(install_root)),
            )
            self.assertEqual(run_mock.call_args.kwargs["cwd"], install_root)

    def test_persona_argv_resolves_dir_to_physical_root(self) -> None:
        with canonical_temporary_directory() as temporary:
            root = Path(temporary).resolve()
            clone = root / "clone"
            self._write_cli(clone)
            base = root / "base"
            alt_dir = root / "alt" / "dir"
            physical_staging = root / "alt" / "staging"
            base.mkdir()
            alt_dir.mkdir(parents=True)
            (base / "staging").mkdir()
            physical_staging.mkdir(parents=True)
            (base / "link").symlink_to(alt_dir, target_is_directory=True)
            install_root = base / "link" / ".." / "staging"

            argv = persona_argv(clone, "build", install_root)

            self.assertEqual(self._argv_dir(argv), str(physical_staging.resolve()))
            self.assertNotEqual(self._argv_dir(argv), str(install_root))

    def test_build_uses_physically_resolved_dir_for_manifest_round_trip(self) -> None:
        with canonical_temporary_directory() as temporary:
            root = Path(temporary).resolve()
            profile, clone, _staging, config = self._build_fixture(root)
            base = root / "base"
            alt_dir = root / "alt" / "dir"
            physical_staging = root / "alt" / "staging"
            base.mkdir()
            alt_dir.mkdir(parents=True)
            lexical_staging = base / "link" / ".." / "staging"
            (base / "staging").mkdir(parents=True)
            physical_staging.mkdir(parents=True)
            (base / "link").symlink_to(alt_dir, target_is_directory=True)
            (physical_staging / "install.yml").write_text(
                "schema_version: 2\npack: pack\n", encoding="utf-8"
            )
            config["staging_root"] = str(lexical_staging)

            self.assertNotEqual(str(lexical_staging), str(physical_staging))
            self.assertEqual(lexical_staging.resolve(), physical_staging.resolve())

            with mock.patch.dict(
                os.environ,
                self._install_fake_node(root, lexical_dir=True),
                clear=False,
            ):
                self.assertEqual(apply_module._build(profile, clone, {}, config), HASH)

    def test_persona_argv_rejects_relative_install_root(self) -> None:
        with canonical_temporary_directory() as temporary:
            root = Path(temporary).resolve()
            clone = root / "clone"
            self._write_cli(clone)
            with self.assertRaisesRegex(PersonaCliError, "install_root must be absolute"):
                persona_argv(clone, "build", Path("relative-root"))

    def test_persona_argv_rejects_relative_clone(self) -> None:
        with canonical_temporary_directory() as temporary:
            install_root = Path(temporary).resolve()
            with self.assertRaisesRegex(PersonaCliError, "clone must be absolute"):
                persona_argv(Path("relative-clone"), "build", install_root)

    def test_persona_argv_rejects_missing_cli(self) -> None:
        with canonical_temporary_directory() as temporary:
            root = Path(temporary).resolve()
            clone = root / "clone"
            install_root = root / "install"
            install_root.mkdir()
            cli = clone / "packages" / "core" / "bin" / "persona"
            with self.assertRaisesRegex(
                PersonaCliError,
                rf"persona CLI missing: {re.escape(str(cli))}",
            ):
                persona_argv(clone, "build", install_root)

    def test_persona_argv_rejects_unavailable_cli(self) -> None:
        with canonical_temporary_directory() as temporary:
            root = Path(temporary).resolve()
            clone = root / "clone"
            install_root = root / "install"
            install_root.mkdir()
            cli = self._write_cli(clone)
            with mock.patch("growthlane.persona_cli.os.lstat", side_effect=OSError("boom")):
                with self.assertRaisesRegex(
                    PersonaCliError,
                    rf"persona CLI unavailable: {re.escape(str(cli))}:",
                ):
                    persona_argv(clone, "build", install_root)

    def test_persona_argv_rejects_symlinked_cli(self) -> None:
        with canonical_temporary_directory() as temporary:
            root = Path(temporary).resolve()
            clone = root / "clone"
            install_root = root / "install"
            install_root.mkdir()
            cli = clone / "packages" / "core" / "bin" / "persona"
            cli.parent.mkdir(parents=True, exist_ok=True)
            target = root / "target"
            target.write_text("fixture\n", encoding="utf-8")
            cli.symlink_to(target)
            with self.assertRaisesRegex(
                PersonaCliError,
                rf"persona CLI has unsafe shape: {re.escape(str(cli))}",
            ):
                persona_argv(clone, "build", install_root)

    def test_persona_argv_rejects_non_regular_cli(self) -> None:
        with canonical_temporary_directory() as temporary:
            root = Path(temporary).resolve()
            clone = root / "clone"
            install_root = root / "install"
            install_root.mkdir()
            cli = clone / "packages" / "core" / "bin" / "persona"
            cli.mkdir(parents=True)
            with self.assertRaisesRegex(
                PersonaCliError,
                rf"persona CLI has unsafe shape: {re.escape(str(cli))}",
            ):
                persona_argv(clone, "build", install_root)


if __name__ == "__main__":
    unittest.main()
