import hashlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
DISPATCH = REPO / "vps" / "pgl-luca-dispatch"
PERSONA_BUILD_FIXTURE = REPO / "tests" / "fixtures" / "luca_dispatch" / "persona_build"
ERROR_LINE = "pgl-luca-dispatch: request rejected\n"
HAS_REAL_RSYNC = Path("/usr/bin/rsync").is_file()


def load_dispatch_module(name):
    loader = importlib.machinery.SourceFileLoader(name, str(DISPATCH))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise AssertionError("dispatcher import spec unavailable")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def content_hash(pack: Path) -> str:
    files = []
    for fixed in ("manifest.yml", "aliases.yml"):
        path = pack / fixed
        if path.exists():
            files.append((fixed, path.read_bytes()))
    for subtree in ("modes", "catalogs"):
        root = pack / subtree
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file():
                    files.append((path.relative_to(pack).as_posix(), path.read_bytes()))
    digest = hashlib.sha256()
    for relative, data in sorted(files, key=lambda item: item[0].encode("utf-8")):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


class LucaDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".pgl-luca-dispatch-test-root").write_bytes(
            b"pgl-luca-dispatch test root\n"
        )
        self.profile = self.root / "home/admin/.hermes/profiles/luca"
        self.profile.mkdir(parents=True)
        self.install = self.root / "home/admin/.persona-engine/luca"
        self.install.parent.mkdir(parents=True)
        shutil.copytree(PERSONA_BUILD_FIXTURE, self.install)
        self.pack = self.install / "pack"
        self.build = self.install / "build"
        self._write_owners()
        self._write_database()
        self._write_doctor_harness()
        self.expected_hash = content_hash(self.pack)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_owners(self):
        value = {
            "updated_at": "2026-08-07T12:00:00+00:00",
            "platforms": {
                "telegram": [
                    {
                        "id": "100000001",
                        "name": "private owner name",
                        "type": "dm",
                        "metadata": {"must": "not leak"},
                    },
                    {"id": "-100123", "name": "group", "type": "group"},
                ],
                "slack": [
                    {"id": "D0OWNER", "name": "private slack name", "type": "dm"},
                    {"id": "C0TEAM", "name": "private channel", "type": "private"},
                ],
            },
        }
        (self.profile / "channel_directory.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def _write_database(self):
        database = self.profile / "state.db"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                chat_type TEXT,
                user_id TEXT,
                session_key TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            [
                ("tg-session", "telegram", "dm", "100000001", "telegram:dm:100000001"),
                ("slack-session", "slack", "dm", "UOWNER", "slack:dm:D0OWNER"),
                ("voice-session", "api_server", None, None, "voice:owner"),
                ("cli-session", "cli", None, None, "cli:self"),
            ],
        )
        connection.executemany(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)",
            [
                ("slack-session", "user", "second at same instant", 110.0),
                ("tg-session", "user", "first at same instant", 110.0),
                ("voice-session", "user", "voice turn", 120.5),
                ("tg-session", "assistant", "not a user turn", 130.0),
                ("tg-session", "tool", "tool call output", 135.0),
                ("tg-session", "session_meta", "session bookkeeping", 137.0),
                ("cli-session", "user", "self-generated", 140.0),
                ("tg-session", "user", "outside window", 210.0),
            ],
        )
        connection.commit()
        connection.close()

    def _write_doctor_harness(self):
        self.node_path = self.root / "usr/bin/node"
        self.cli_path = (
            self.root / "home/admin/.persona-engine/cli/packages/core/bin/persona"
        )
        self.node_path.parent.mkdir(parents=True)
        self.cli_path.parent.mkdir(parents=True)
        python = sys.executable
        self.node_path.write_text(
            f"""#!{python}
import runpy
import sys

if len(sys.argv) < 2:
    raise SystemExit(2)
source = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(source, run_name="__main__")
""",
            encoding="utf-8",
        )
        self.node_path.chmod(0o755)

        build_files = {
            relative: (PERSONA_BUILD_FIXTURE / "build" / relative).read_bytes()
            for relative in (
                "triggers.json",
                "policy.json",
                "modes/dummy.md",
                "modes/mode-b.md",
            )
        }
        manifest = json.loads(
            (PERSONA_BUILD_FIXTURE / "build/manifest.json").read_text(encoding="utf-8")
        )
        self.cli_path.write_text(
            f"""#!{python}
import hashlib
import json
from pathlib import Path
import sys

BUILD_FILES = {build_files!r}
BASE_MANIFEST = {manifest!r}

def pack_hash(pack):
    files = []
    for fixed in ("manifest.yml", "aliases.yml"):
        path = pack / fixed
        if path.exists():
            files.append((fixed, path.read_bytes()))
    for subtree in ("modes", "catalogs"):
        root = pack / subtree
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file():
                    files.append((path.relative_to(pack).as_posix(), path.read_bytes()))
    digest = hashlib.sha256()
    for relative, data in sorted(files, key=lambda item: item[0].encode("utf-8")):
        digest.update(relative.encode("utf-8") + b"\\0" + data + b"\\0")
    return digest.hexdigest()

if len(sys.argv) != 4 or sys.argv[2] != "--dir":
    raise SystemExit(2)
command = sys.argv[1]
root = Path(sys.argv[3])
expected_manifest = dict(BASE_MANIFEST)
expected_manifest["content_hash"] = pack_hash(root / "pack")
if command == "build":
    build = root / "build"
    (build / "modes").mkdir(parents=True)
    for relative, data in BUILD_FILES.items():
        (build / relative).write_bytes(data)
    (build / "manifest.json").write_text(
        json.dumps(expected_manifest, separators=(",", ":")), encoding="utf-8"
    )
    print(json.dumps({{"ok": True}}, separators=(",", ":")))
    raise SystemExit(0)
if command != "doctor":
    raise SystemExit(2)
issues = []
for relative, expected in BUILD_FILES.items():
    try:
        actual = (root / "build" / relative).read_bytes()
    except OSError:
        actual = None
    if actual != expected:
        issues.append(relative + " differs from real persona build")
try:
    actual_manifest = json.loads((root / "build/manifest.json").read_text(encoding="utf-8"))
except Exception:
    actual_manifest = None
if actual_manifest != expected_manifest:
    issues.append("manifest.json differs from real persona build")
print(json.dumps({{"ok": not issues, "issues": issues}}, separators=(",", ":")))
raise SystemExit(0 if not issues else 2)
""",
            encoding="utf-8",
        )
        self.cli_path.chmod(0o644)

        package = self.root / "home/admin/.persona-engine/cli/packages/core/package.json"
        package.write_text('{"version":"0.1.0"}\n', encoding="utf-8")
        adapter = (
            self.root
            / "home/admin/.hermes/profiles/luca/plugins/persona-engine/version.py"
        )
        adapter.parent.mkdir(parents=True)
        adapter.write_text('VERSION = "0.1.0"\n', encoding="utf-8")

        self.rsync_path = self.root / "usr/bin/rsync"
        self.rsync_path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  printf 'rsync  version 3.4.0  protocol version 31\\n'\n"
            "  exit 0\n"
            "fi\n"
            "printf '%s\\n' \"$@\" > \"$PWD/rsync.argv\"\n",
            encoding="utf-8",
        )
        self.rsync_path.chmod(0o755)
        self.systemctl_path = self.root / "usr/bin/systemctl"
        self.systemctl_path.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$PWD/systemctl.argv\"\n",
            encoding="utf-8",
        )
        self.systemctl_path.chmod(0o755)

    def run_dispatch(
        self,
        command=None,
        *,
        role="read",
        script=DISPATCH,
        extra_env=None,
        timeout=None,
    ):
        env = os.environ.copy()
        env.pop("SSH_ORIGINAL_COMMAND", None)
        env.update(
            {
                "PGL_LUCA_DISPATCH_TESTING": "1",
                "PGL_LUCA_DISPATCH_TEST_ROOT": str(self.root),
            }
        )
        if command is not None:
            env["SSH_ORIGINAL_COMMAND"] = command
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(script), role],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )

    def assert_rejected(self, command=None, *, role="read", script=DISPATCH):
        result = self.run_dispatch(command, role=role, script=script)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, ERROR_LINE)

    def _install_real_server_rsync(self):
        self.rsync_path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  printf 'rsync  version 3.4.0  protocol version 31\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exec /usr/bin/rsync \"$@\"\n",
            encoding="utf-8",
        )
        self.rsync_path.chmod(0o755)

    def _write_versioned_receiver(self, version: str, transfer_body: str) -> None:
        self.rsync_path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            f"  printf 'rsync  version {version}  protocol version 31\\n'\n"
            "  exit 0\n"
            "fi\n"
            f"{transfer_body}",
            encoding="utf-8",
        )
        self.rsync_path.chmod(0o755)

    def _rsync_backport_exception_payload(self, version: str) -> dict[str, object]:
        return {
            "version": version,
            "reviewed_at": "2026-08-10T12:00:00+00:00",
            "reviewed_by": "ops@example.invalid",
            "reviewed_fixes": ["CVE-2024-12087", "CVE-2024-12088"],
            "notes": "Debian receiver build reviewed for backported traversal fixes.",
        }

    def _write_rsync_backport_exception(
        self,
        path: Path,
        version: str,
        *,
        mode: int = 0o644,
        payload: dict[str, object] | None = None,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                self._rsync_backport_exception_payload(version)
                if payload is None
                else payload
            ),
            encoding="utf-8",
        )
        path.chmod(mode)

    def _ssh_helper_path(self):
        helper = self.root / "fake-ssh"
        helper.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    "host=\"$1\"",
                    "shift",
                    f"export PGL_LUCA_DISPATCH_TESTING=1",
                    f"export PGL_LUCA_DISPATCH_TEST_ROOT={str(self.root)!r}",
                    "export SSH_ORIGINAL_COMMAND=\"$*\"",
                    f"exec {str(DISPATCH)!r} deploy",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        helper.chmod(0o755)
        return helper

    def _run_real_rsync(self, *arguments, timeout=10):
        helper = self._ssh_helper_path()
        return subprocess.run(
            [
                "/usr/bin/rsync",
                *arguments,
                f"--rsh={helper}",
            ],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )

    def test_read_owners_is_deterministic_and_excludes_names_and_metadata(self):
        result = self.run_dispatch("read-owners")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            '[{"platform":"slack","id":"D0OWNER","type":"dm"},'
            '{"platform":"telegram","id":"100000001","type":"dm"}]\n',
        )
        self.assertNotIn("owner name", result.stdout)
        self.assertNotIn("metadata", result.stdout)

    def test_read_owners_rejects_malformed_or_unknown_platform_data(self):
        path = self.profile / "channel_directory.json"
        cases = [
            {"platforms": []},
            {"platforms": {"discord": []}},
            {"platforms": {"telegram": {}}},
            {"platforms": {"telegram": ["not-an-object"]}},
            {"platforms": {"telegram": [{"id": [], "type": "dm"}]}},
            {"platforms": {"telegram": [{"id": "123", "type": "new-kind"}]}},
            {"platforms": {"telegram": [{"id": "123", "type": "dm"}, {"id": "123", "type": "dm"}]}},
        ]
        for value in cases:
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assert_rejected("read-owners")

    def test_read_sessions_uses_closed_query_and_exact_json_lines(self):
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*") if path.is_file())
        result = self.run_dispatch("read-sessions 100 200")
        after = sorted(path.relative_to(self.root) for path in self.root.rglob("*") if path.is_file())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            records,
            [
                {
                    "session_id": "slack-session",
                    "source": "slack",
                    "chat_type": "dm",
                    "user_id": "UOWNER",
                    "session_key": "slack:dm:D0OWNER",
                    "content": "second at same instant",
                    "timestamp": 110.0,
                    "role": "user",
                },
                {
                    "session_id": "tg-session",
                    "source": "telegram",
                    "chat_type": "dm",
                    "user_id": "100000001",
                    "session_key": "telegram:dm:100000001",
                    "content": "first at same instant",
                    "timestamp": 110.0,
                    "role": "user",
                },
                {
                    "session_id": "voice-session",
                    "source": "api_server",
                    "chat_type": None,
                    "user_id": None,
                    "session_key": "voice:owner",
                    "content": "voice turn",
                    "timestamp": 120.5,
                    "role": "user",
                },
                {
                    "session_id": "tg-session",
                    "source": "telegram",
                    "chat_type": "dm",
                    "user_id": "100000001",
                    "session_key": "telegram:dm:100000001",
                    "content": "not a user turn",
                    "timestamp": 130.0,
                    "role": "assistant",
                },
            ],
        )
        self.assertEqual(set(records[0]), {
            "session_id", "source", "chat_type", "user_id", "session_key", "content", "timestamp", "role"
        })
        self.assertEqual(before, after, "read-sessions must not create a raw copy or temp file")

    def test_read_sessions_includes_assistant_turns_alongside_user_turns(self):
        result = self.run_dispatch("read-sessions 100 200")
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        roles = {record["role"] for record in records}
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        assistant_records = [record for record in records if record["role"] == "assistant"]
        self.assertEqual(len(assistant_records), 1)
        self.assertEqual(assistant_records[0]["content"], "not a user turn")

    def test_read_sessions_excludes_tool_and_session_meta_roles(self):
        result = self.run_dispatch("read-sessions 100 200")
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        roles = {record["role"] for record in records}
        self.assertEqual(roles, {"user", "assistant"})
        contents = {record["content"] for record in records}
        self.assertNotIn("tool call output", contents)
        self.assertNotIn("session bookkeeping", contents)

    def test_read_sessions_skips_only_null_content_row(self):
        database = self.profile / "state.db"
        connection = sqlite3.connect(database)
        connection.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)",
            ("tg-session", "user", None, 115.0),
        )
        connection.commit()
        connection.close()

        result = self.run_dispatch("read-sessions 100 200")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            [(record["content"], record["timestamp"]) for record in records],
            [
                ("second at same instant", 110.0),
                ("first at same instant", 110.0),
                ("voice turn", 120.5),
                ("not a user turn", 130.0),
            ],
        )
        self.assertNotIn(None, (record["content"] for record in records))

    def test_read_sessions_still_rejects_non_string_non_null_content(self):
        database = self.profile / "state.db"
        connection = sqlite3.connect(database)
        connection.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) "
            "VALUES (?,?,CAST(? AS BLOB),?)",
            ("tg-session", "user", "not text", 115.0),
        )
        connection.commit()
        connection.close()

        self.assert_rejected("read-sessions 100 200")

    def test_hash_matches_real_persona_build_manifest_and_independent_pack_hash(self):
        fixture_manifest = json.loads((self.build / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture_manifest["engine_version"], "0.1.0")
        self.assertEqual(fixture_manifest["content_hash"], content_hash(self.pack))
        result = self.run_dispatch("hash")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, self.expected_hash + "\n")

    def test_hash_rejects_forged_manifest_self_report(self):
        path = self.build / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["content_hash"] = "0" * 64
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertNotEqual(manifest["content_hash"], content_hash(self.pack))
        self.assert_rejected("hash")

    def test_hash_rejects_pack_mutation_after_build(self):
        source = self.pack / "modes/dummy.yml"
        source.write_bytes(source.read_bytes() + b"# changed after build\n")
        self.assertNotEqual(self.expected_hash, content_hash(self.pack))
        self.assert_rejected("hash")

    def test_hash_rejects_torn_or_unexpected_build_artifacts(self):
        mode = self.build / "modes/dummy.md"
        mode.write_bytes(mode.read_bytes() + b"torn")
        self.assert_rejected("hash")

    def test_hash_rejects_extra_build_artifact(self):
        (self.build / "unexpected.json").write_text("{}", encoding="utf-8")
        self.assert_rejected("hash")

    def test_hash_requires_duplicate_free_json_objects_for_policy_and_triggers(self):
        invalid_artifacts = {
            "triggers.json": b"[]",
            "policy.json": b'{"routes":[],"routes":[]}',
        }
        for name, invalid in invalid_artifacts.items():
            with self.subTest(name=name):
                path = self.build / name
                original = path.read_bytes()
                path.write_bytes(invalid)
                self.assert_rejected("hash")
                path.write_bytes(original)

    def test_hash_doctor_rejects_valid_policy_and_trigger_mutations(self):
        for name in ("policy.json", "triggers.json"):
            with self.subTest(name=name):
                path = self.build / name
                original = path.read_bytes()
                value = json.loads(original)
                value["tampered"] = True
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assert_rejected("hash")
                path.write_bytes(original)

    def test_hash_doctor_rejects_self_consistent_mode_and_manifest_tampering(self):
        mode = self.build / "modes/dummy.md"
        mode.write_bytes(mode.read_bytes() + b"\npost-build change")
        manifest_path = self.build / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        metadata = manifest["modes"]["dummy"]
        block = mode.read_bytes()
        metadata["bytes"] = len(block)
        metadata["tokens"] = (len(block) + 2) // 3
        metadata["sha256"] = hashlib.sha256(block).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_rejected("hash")

    def test_hash_rejects_invalid_doctor_executable_and_source_shapes(self):
        for path, mode in ((self.node_path, 0o755), (self.cli_path, 0o644)):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                target = self.root / (path.name + "-symlink-target")
                target.write_bytes(original)
                target.chmod(mode)
                path.unlink()
                path.symlink_to(target)
                self.assert_rejected("hash")
                path.unlink()
                path.write_bytes(original)
                path.chmod(mode)

        self.node_path.chmod(0o644)
        self.assert_rejected("hash")
        self.node_path.chmod(0o755)
        self.cli_path.chmod(0o200)
        self.assert_rejected("hash")

    def test_hash_rejects_pack_symlinks(self):
        target = self.root / "outside-pack-input"
        target.write_bytes(b"aliases: {}\n")
        aliases = self.pack / "aliases.yml"
        aliases.symlink_to(target)
        self.assert_rejected("hash")

    def test_closed_command_grammar_rejects_injection_and_bad_ranges(self):
        rejected = [
            "unknown",
            "../read-owners",
            "read-owners; rm -rf /",
            "$(read-owners)",
            "read-owners extra",
            'read-owners "extra argument"',
            "read-sessions 100",
            "read-sessions -1 2",
            "read-sessions 1 2x",
            "read-sessions 2 2",
            "read-sessions 3 2",
            "read-sessions 0000000000000 2",
            "read-sessions 1 2 extra",
            "read-sessions 1 2\nhash",
            "deploy",
            "restore",
            "accept",
            "restart",
        ]
        for command in rejected:
            with self.subTest(command=command):
                self.assert_rejected(command)

    def test_requires_exact_role_and_original_command(self):
        # Frozen v1.2 deliberately shares only `hash` across the two roles;
        # this replaces the pre-deploy parser assertion that rejected the role.
        deploy_hash = self.run_dispatch("hash", role="deploy")
        self.assertEqual(deploy_hash.returncode, 0, deploy_hash.stderr)
        self.assertEqual(deploy_hash.stdout, self.expected_hash + "\n")
        self.assert_rejected("hash", role="read-only")
        self.assert_rejected(None)
        self.assert_rejected("")

        env = os.environ.copy()
        env.update(
            {
                "PGL_LUCA_DISPATCH_TESTING": "1",
                "PGL_LUCA_DISPATCH_TEST_ROOT": str(self.root),
                "SSH_ORIGINAL_COMMAND": "hash",
            }
        )
        result = subprocess.run(
            [str(DISPATCH), "read", "extra-role-argument"],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, ERROR_LINE)

    def test_deploy_backup_rotates_to_fourteen_and_restore_recovers_all_three_objects(self):
        backups = self.root / "home/admin/.hermes/backups"
        backups.mkdir(parents=True)
        for index in range(15):
            generation = backups / f"luca-20260101T0000{index:02d}000000Z-{index:08x}"
            generation.mkdir()
            (generation / "old").write_bytes(b"x")

        original_install = (self.install / "install.yml").read_bytes()
        original_pack = (self.pack / "manifest.yml").read_bytes()
        original_build = (self.build / "manifest.json").read_bytes()
        result = self.run_dispatch("deploy backup", role="deploy")
        self.assertEqual(result.returncode, 0, result.stderr)
        generations = sorted(path for path in backups.iterdir() if path.name.startswith("luca-"))
        self.assertEqual(len(generations), 14)
        newest = generations[-1]
        self.assertTrue((newest / "install.yml").is_file())
        self.assertTrue((newest / "pack").is_dir())
        self.assertTrue((newest / "build").is_dir())

        (self.install / "install.yml").write_text("broken: true\n", encoding="utf-8")
        (self.pack / "manifest.yml").write_text("broken: true\n", encoding="utf-8")
        (self.build / "manifest.json").write_text("{}\n", encoding="utf-8")
        restored = self.run_dispatch("restore", role="deploy")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((self.install / "install.yml").read_bytes(), original_install)
        self.assertEqual((self.pack / "manifest.yml").read_bytes(), original_pack)
        self.assertEqual((self.build / "manifest.json").read_bytes(), original_build)

    def test_deploy_backup_enforces_total_capacity_cap(self):
        module = load_dispatch_module("luca_dispatch_capacity")
        backups = self.root / "home/admin/.hermes/backups"
        with mock.patch.object(module, "_BACKUP_CAP_BYTES", 1):
            with self.assertRaises(module.DispatchError):
                module._create_backup(
                    self.install,
                    backups,
                    self.node_path,
                    self.cli_path,
                )
        self.assertEqual(
            [path for path in backups.iterdir() if path.name.startswith("luca-")],
            [],
        )

    def test_deploy_backup_accounts_for_complete_and_sequence_marker_bytes(self):
        module = load_dispatch_module("luca_dispatch_marker_cap")
        backups = self.root / "home/admin/.hermes/backups"
        install_size = module._install_size(self.install)
        marker_bytes = module._BACKUP_MARKER_BYTES + len(b"1\n")
        with mock.patch.object(module, "_BACKUP_CAP_BYTES", install_size + marker_bytes - 1):
            with self.assertRaises(module.DispatchError):
                module._create_backup(
                    self.install,
                    backups,
                    self.node_path,
                    self.cli_path,
                )
        self.assertEqual(
            [path for path in backups.iterdir() if path.name.startswith("luca-")],
            [],
        )

    def test_backup_rotation_prefers_sequence_marker_over_name_order(self):
        module = load_dispatch_module("luca_dispatch_rotation")
        backups = self.root / "home/admin/.hermes/backups"
        backups.mkdir(parents=True)
        for index, stamp in ((1, "20990101T000000000000Z"), (2, "19990101T000000000000Z"), (3, "18990101T000000000000Z")):
            generation = backups / f"luca-{stamp}-{index:08x}"
            generation.mkdir()
            (generation / ".complete").write_bytes(module._BACKUP_COMPLETE)
            (generation / ".sequence").write_text(f"{index}\n", encoding="ascii")
            (generation / "blob").write_bytes(b"x")
        with mock.patch.object(module, "_BACKUP_KEEP", 2):
            module._rotate_backups(backups)
        remaining = sorted(
            path.name for path in backups.iterdir() if path.name.startswith("luca-")
        )
        self.assertEqual(
            remaining,
            [
                "luca-18990101T000000000000Z-00000003",
                "luca-19990101T000000000000Z-00000002",
            ],
        )

    def test_hash_rejects_fifo_promptly(self):
        manifest = self.pack / "manifest.yml"
        manifest.unlink()
        os.mkfifo(manifest)
        started = time.monotonic()
        result = self.run_dispatch("hash", timeout=2)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, ERROR_LINE)

    def test_deploy_promote_rejects_fifo_install_promptly(self):
        install_file = self.install / "install.yml"
        install_file.unlink()
        os.mkfifo(install_file)
        started = time.monotonic()
        result = self.run_dispatch(
            f"deploy promote {self.expected_hash}",
            role="deploy",
            timeout=2,
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, ERROR_LINE)

    def test_unexpected_pack_entries_are_rejected_by_hash_promote_and_backup(self):
        (self.pack / "evil.js").write_text("console.log('nope')\n", encoding="utf-8")
        (self.pack / "plugins").mkdir()
        (self.pack / "plugins/x.js").write_text("console.log('still nope')\n", encoding="utf-8")
        self.assertEqual(content_hash(self.pack), self.expected_hash)
        self.assert_rejected("hash")
        self.assert_rejected(f"deploy promote {self.expected_hash}", role="deploy")
        self.assert_rejected("deploy backup", role="deploy")

    @unittest.skipUnless(HAS_REAL_RSYNC, "/usr/bin/rsync unavailable")
    @unittest.skipUnless(
        sys.platform == "darwin",
        "production sender is the macOS orchestrator; Linux client rsync (samba) emits a --server capability token the dispatcher's fixed allowlist intentionally rejects",
    )
    def test_deploy_transfer_real_rsync_push_lands_under_pack_and_deletes_removed_entries(self):
        self._install_real_server_rsync()
        source = self.root / "transfer-source"
        shutil.copytree(self.pack, source)
        (source / "aliases.yml").write_text("aliases: {fresh: yes}\n", encoding="utf-8")
        (source / "catalogs/overlay").mkdir(parents=True)
        (source / "catalogs/overlay/banner.txt").write_text("banner\n", encoding="utf-8")
        (source / "modes/mode-b.yml").unlink()

        result = self._run_real_rsync(
            "-rpt",
            "--checksum",
            "--compress",
            "--itemize-changes",
            "--delete",
            "--partial",
            "--inplace",
            "--safe-links",
            "--rsync-path=deploy transfer",
            f"{source}/",
            "loopback:/home/admin/.persona-engine/luca/pack",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.pack / "aliases.yml").read_text(encoding="utf-8"),
            "aliases: {fresh: yes}\n",
        )
        self.assertEqual(
            (self.pack / "catalogs/overlay/banner.txt").read_text(encoding="utf-8"),
            "banner\n",
        )
        self.assertFalse((self.pack / "modes/mode-b.yml").exists())
        self.assertEqual(
            (self.root / "home/admin/.hermes/profiles/luca/rsync-receiver.log").read_text(
                encoding="utf-8"
            ),
            "",
        )

    @unittest.skipUnless(HAS_REAL_RSYNC, "/usr/bin/rsync unavailable")
    def test_deploy_transfer_real_rsync_rejects_sender_mode(self):
        self._install_real_server_rsync()
        destination = self.root / "sender-destination"
        destination.mkdir()
        result = self._run_real_rsync(
            "-rpt",
            "--checksum",
            "--compress",
            "--itemize-changes",
            "--safe-links",
            "--rsync-path=deploy transfer",
            "loopback:/home/admin/.persona-engine/luca/pack/",
            f"{destination}/",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(ERROR_LINE.strip(), result.stderr)
        self.assertNotIn("/home/admin/.persona-engine/luca/pack", result.stderr)

    def test_transfer_argv_rejects_sender_mode_before_exec(self):
        module = load_dispatch_module("luca_dispatch_sender_gate")
        with self.assertRaises(module.DispatchError):
            module._validated_transfer_argv(
                self.install,
                (
                    "--server",
                    "--sender",
                    "-rpt",
                    "--safe-links",
                    ".",
                    "/home/admin/.persona-engine/luca/pack",
                ),
            )

    def test_transfer_argv_forces_safe_links_when_client_omits_it(self):
        module = load_dispatch_module("luca_dispatch_safe_links")
        argv = module._validated_transfer_argv(
            self.install,
            (
                "--server",
                "-r",
                "-t",
                "-p",
                ".",
                "/home/admin/.persona-engine/luca/pack",
            ),
        )
        self.assertIn("--safe-links", argv)
        self.assertEqual(argv.count("--safe-links"), 1)
        self.assertEqual(
            argv,
            [
                "--server",
                "-r",
                "-t",
                "-p",
                "--safe-links",
                ".",
                str(self.install / "pack"),
            ],
        )

    def test_deploy_transfer_refuses_gnu_compat_bundle_by_design(self):
        """GNU rsync's protocol-capability bundle is intentionally not accepted."""
        module = load_dispatch_module("luca_dispatch_gnu_compat_bundle")
        # Do not widen this for GNU rsync's -e.<caps> compatibility bits without
        # a separate security review: they are protocol capability signals, so
        # this lane deliberately fails closed and pins the supported client
        # flavour to macOS stock openrsync.
        self.assertNotIn("e", module._TRANSFER_SHORT_FLAGS)
        self.assert_rejected(
            "deploy transfer --server -logDtpre.iLsfxCIvu . "
            "/home/admin/.persona-engine/luca/pack",
            role="deploy",
        )

    @unittest.skipUnless(HAS_REAL_RSYNC, "/usr/bin/rsync unavailable")
    def test_deploy_transfer_real_rsync_symlink_escape_does_not_write_outside_pack(self):
        self._install_real_server_rsync()
        source = self.root / "transfer-links-source"
        shutil.copytree(self.pack, source)
        target = self.root / "home/admin/.ssh"
        target.mkdir(parents=True)
        authorized = target / "authorized_keys"
        authorized.write_text("keep-me\n", encoding="utf-8")
        escape = source / "x"
        escape.symlink_to(target)
        files_from = self.root / "files-from.txt"
        files_from.write_text("x\nx/authorized_keys\n", encoding="utf-8")

        result = self._run_real_rsync(
            "-rptl",
            "--files-from",
            str(files_from),
            "--safe-links",
            "--rsync-path=deploy transfer",
            f"{source}/",
            "loopback:/home/admin/.persona-engine/luca/pack",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(ERROR_LINE.strip(), result.stderr)
        self.assertEqual(authorized.read_text(encoding="utf-8"), "keep-me\n")
        self.assertFalse((self.pack / "x").exists())
        self.assertNotIn("forbidden transfer option", result.stderr)
        self.assertNotIn("authorized_keys", result.stderr)

    def test_transfer_argv_rejects_link_creation_flags_before_exec(self):
        module = load_dispatch_module("luca_dispatch_links_gate")
        cases = [
            ("--server", "-rptl", ".", "/home/admin/.persona-engine/luca/pack"),
            ("--server", "--links", ".", "/home/admin/.persona-engine/luca/pack"),
        ]
        for trailing in cases:
            with self.subTest(trailing=trailing):
                with self.assertRaises(module.DispatchError):
                    module._validated_transfer_argv(self.install, trailing)

    @unittest.skipUnless(HAS_REAL_RSYNC, "/usr/bin/rsync unavailable")
    def test_deploy_transfer_real_rsync_rejects_foreign_destination(self):
        self._install_real_server_rsync()
        source = self.root / "transfer-destination-source"
        shutil.copytree(self.pack, source)
        result = self._run_real_rsync(
            "-rpt",
            "--checksum",
            "--compress",
            "--itemize-changes",
            "--safe-links",
            "--rsync-path=deploy transfer",
            f"{source}/",
            "loopback:/tmp/not-pack",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(ERROR_LINE.strip(), result.stderr)
        self.assertNotIn("/tmp/not-pack", result.stderr)

    @unittest.skipUnless(HAS_REAL_RSYNC, "/usr/bin/rsync unavailable")
    def test_deploy_transfer_real_rsync_rejects_special_files_option(self):
        self._install_real_server_rsync()
        source = self.root / "transfer-specials-source"
        shutil.copytree(self.pack, source)
        result = self._run_real_rsync(
            "-rptD",
            "--checksum",
            "--compress",
            "--itemize-changes",
            "--safe-links",
            "--rsync-path=deploy transfer",
            f"{source}/",
            "loopback:/home/admin/.persona-engine/luca/pack",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(ERROR_LINE.strip(), result.stderr)
        self.assertNotIn("forbidden transfer option", result.stderr)

    def test_transfer_argv_rejects_special_files_flags_before_exec(self):
        module = load_dispatch_module("luca_dispatch_specials_gate")
        cases = [
            ("--server", "-rptD", ".", "/home/admin/.persona-engine/luca/pack"),
            ("--server", "--specials", ".", "/home/admin/.persona-engine/luca/pack"),
        ]
        for trailing in cases:
            with self.subTest(trailing=trailing):
                with self.assertRaises(module.DispatchError):
                    module._validated_transfer_argv(self.install, trailing)

    def test_deploy_transfer_captures_receiver_stderr_in_bounded_server_log(self):
        payload = "x" * 70000
        self.rsync_path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  printf 'rsync  version 3.4.0  protocol version 31\\n'\n"
            "  exit 0\n"
            "fi\n"
            f"printf '%s' {payload!r} >&2\n"
            "exit 1\n",
            encoding="utf-8",
        )
        self.rsync_path.chmod(0o755)
        result = self.run_dispatch(
            "deploy transfer --server -r -t -p --safe-links . /home/admin/.persona-engine/luca/pack",
            role="deploy",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, ERROR_LINE)
        log_path = self.root / "home/admin/.hermes/profiles/luca/rsync-receiver.log"
        self.assertTrue(log_path.is_file())
        log = log_path.read_text(encoding="utf-8")
        self.assertTrue(log.endswith("[truncated]\n"))
        self.assertLessEqual(log_path.stat().st_size, 65536)

    def test_deploy_transfer_rejects_post_transfer_size_over_cap(self):
        self.rsync_path.write_text(
            "#!/usr/bin/python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "import os\n"
            "if len(sys.argv) > 1 and sys.argv[1] == '--version':\n"
            "    print('rsync  version 3.4.0  protocol version 31')\n"
            "    raise SystemExit(0)\n"
            "target = Path(os.getcwd()) / 'pack/catalogs/oversized.bin'\n"
            "target.parent.mkdir(parents=True, exist_ok=True)\n"
            "target.write_bytes(b'x' * 32)\n",
            encoding="utf-8",
        )
        self.rsync_path.chmod(0o755)
        module = load_dispatch_module("luca_dispatch_transfer_cap")
        with mock.patch.object(module, "_TRANSFER_CAP_BYTES", 16):
            with self.assertRaises(module.DispatchError):
                module._receive_pack(
                    self.install,
                    self.rsync_path,
                    (
                        "--server",
                        "-r",
                        "-t",
                        "-p",
                        "--safe-links",
                        ".",
                        "/home/admin/.persona-engine/luca/pack",
                    ),
                    self.root / "home/admin/.hermes/profiles/luca/rsync-receiver.log",
                    self.root / "etc/pgl-luca-rsync-backport.json",
                )
        module._validate_pack_tree(self.pack)
        self.assertEqual((self.pack / "catalogs/oversized.bin").stat().st_size, 32)

    def test_receive_pack_enforces_rsync_receiver_version_floor_and_backport_exception(self):
        module = load_dispatch_module("luca_dispatch_rsync_receiver_version")
        trailing = (
            "--server",
            "-r",
            "-t",
            "-p",
            "--safe-links",
            ".",
            "/home/admin/.persona-engine/luca/pack",
        )
        log_path = self.root / "home/admin/.hermes/profiles/luca/rsync-receiver.log"
        exception_path = self.root / "etc/pgl-luca-rsync-backport.json"
        marker = self.install / "receiver-ran"
        cases = [
            ("below-floor-rejects", "3.2.7", None, False),
            ("floor-accepts", "3.4.0", "none", True),
            ("above-floor-accepts", "3.5.0", "none", True),
            ("backport-exception-honored", "3.2.7", "valid", True),
            ("malformed-exception-ignored", "3.2.7", "malformed", False),
        ]
        with mock.patch.object(
            module, "_RSYNC_RECEIVER_BACKPORT_OWNER_UID", os.getuid()
        ):
            for name, version, exception_kind, accepted in cases:
                with self.subTest(case=name):
                    marker.unlink(missing_ok=True)
                    exception_path.unlink(missing_ok=True)
                    self._write_versioned_receiver(
                        version,
                        "printf 'ran\\n' > \"$PWD/receiver-ran\"\n",
                    )
                    if exception_kind == "valid":
                        self._write_rsync_backport_exception(exception_path, version)
                    elif exception_kind == "malformed":
                        exception_path.parent.mkdir(parents=True, exist_ok=True)
                        exception_path.write_text("{not json}\n", encoding="utf-8")
                        exception_path.chmod(0o644)
                    if accepted:
                        module._receive_pack(
                            self.install,
                            self.rsync_path,
                            trailing,
                            log_path,
                            exception_path,
                        )
                        self.assertEqual(marker.read_text(encoding="utf-8"), "ran\n")
                    else:
                        with self.assertRaises(module.DispatchError):
                            module._receive_pack(
                                self.install,
                                self.rsync_path,
                                trailing,
                                log_path,
                                exception_path,
                            )
                        self.assertFalse(marker.exists())

    def test_rsync_backport_exception_requires_exact_owned_regular_0644_json(self):
        module = load_dispatch_module("luca_dispatch_rsync_backport_exception")
        exception_path = self.root / "etc/pgl-luca-rsync-backport.json"
        target_path = self.root / "etc/pgl-luca-rsync-backport-target.json"
        detected_version = (3, 2, 7)
        valid_payload = self._rsync_backport_exception_payload("3.2.7")
        valid_payload_bytes = json.dumps(valid_payload).encode("utf-8")

        def cleanup(path: Path) -> None:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

        def write_payload(
            path: Path, payload: dict[str, object], *, mode: int = 0o644
        ) -> None:
            self._write_rsync_backport_exception(
                path, "3.2.7", mode=mode, payload=payload
            )

        cases = [
            ("valid", lambda: write_payload(exception_path, valid_payload), os.getuid(), True),
            (
                "owner-uid-mismatch",
                lambda: write_payload(exception_path, valid_payload),
                os.getuid() + 1,
                False,
            ),
            (
                "version-echo-mismatch",
                lambda: write_payload(
                    exception_path, {**valid_payload, "version": "3.2.8"}
                ),
                os.getuid(),
                False,
            ),
            (
                "reviewed-fixes-wrong",
                lambda: write_payload(
                    exception_path,
                    {
                        **valid_payload,
                        "reviewed_fixes": ["CVE-2024-12087", "CVE-2024-99999"],
                    },
                ),
                os.getuid(),
                False,
            ),
            (
                "reviewed-fixes-extra",
                lambda: write_payload(
                    exception_path,
                    {
                        **valid_payload,
                        "reviewed_fixes": [
                            "CVE-2024-12087",
                            "CVE-2024-12088",
                            "CVE-2024-13000",
                        ],
                    },
                ),
                os.getuid(),
                False,
            ),
            (
                "reviewed-fixes-missing",
                lambda: write_payload(
                    exception_path,
                    {"version": "3.2.7", "reviewed_at": valid_payload["reviewed_at"], "reviewed_by": valid_payload["reviewed_by"], "reviewed_fixes": ["CVE-2024-12087"], "notes": valid_payload["notes"]},
                ),
                os.getuid(),
                False,
            ),
            (
                "reviewed-fixes-duplicate-padded",
                lambda: write_payload(
                    exception_path,
                    {
                        **valid_payload,
                        "reviewed_fixes": [
                            "CVE-2024-12087",
                            "CVE-2024-12088",
                            "CVE-2024-12088",
                        ],
                    },
                ),
                os.getuid(),
                False,
            ),
            (
                "mode-too-open",
                lambda: write_payload(exception_path, valid_payload, mode=0o666),
                os.getuid(),
                False,
            ),
            (
                "mode-too-closed",
                lambda: write_payload(exception_path, valid_payload, mode=0o600),
                os.getuid(),
                False,
            ),
            (
                "nofollow-symlink",
                lambda: (
                    write_payload(target_path, valid_payload),
                    exception_path.parent.mkdir(parents=True, exist_ok=True),
                    exception_path.symlink_to(target_path),
                ),
                os.getuid(),
                False,
            ),
            (
                "directory-not-regular",
                lambda: (
                    exception_path.mkdir(parents=True),
                    exception_path.chmod(0o644),
                ),
                os.getuid(),
                False,
                True,
            ),
            (
                "fifo-not-regular",
                lambda: (
                    exception_path.parent.mkdir(parents=True, exist_ok=True),
                    os.mkfifo(exception_path),
                    exception_path.chmod(0o644),
                ),
                os.getuid(),
                False,
                True,
            ),
        ]

        for case in cases:
            if len(case) == 4:
                name, setup_case, expected_owner_uid, allowed = case
                patch_read = False
            else:
                name, setup_case, expected_owner_uid, allowed, patch_read = case
            with self.subTest(case=name):
                cleanup(exception_path)
                cleanup(target_path)
                setup_case()
                read_stream = [valid_payload_bytes, b""]

                def fake_read(_fd, _size):
                    if read_stream:
                        return read_stream.pop(0)
                    return b""

                owner_patch = mock.patch.object(
                    module,
                    "_RSYNC_RECEIVER_BACKPORT_OWNER_UID",
                    expected_owner_uid,
                )
                read_patch = (
                    mock.patch.object(module.os, "read", side_effect=fake_read)
                    if patch_read
                    else mock.patch.object(module.os, "read")
                )
                with owner_patch:
                    if patch_read:
                        with read_patch:
                            self.assertEqual(
                                module._rsync_backport_exception_allows(
                                    exception_path, detected_version
                                ),
                                allowed,
                            )
                    else:
                        self.assertEqual(
                            module._rsync_backport_exception_allows(
                                exception_path, detected_version
                            ),
                            allowed,
                        )
                cleanup(exception_path)
                cleanup(target_path)

    def test_receive_pack_rejects_fifo_backport_exception_promptly(self):
        self._write_versioned_receiver(
            "3.2.7",
            "printf 'ran\\n' > \"$PWD/receiver-ran\"\n",
        )
        exception_path = self.root / "etc/pgl-luca-rsync-backport.json"
        exception_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(exception_path)
        started = time.monotonic()
        result = self.run_dispatch(
            "deploy transfer --server -r -t -p --safe-links . "
            "/home/admin/.persona-engine/luca/pack",
            role="deploy",
            timeout=2,
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, ERROR_LINE)
        self.assertFalse((self.install / "receiver-ran").exists())

    def test_receive_pack_rejects_unexpected_post_transfer_pack_entry(self):
        self._write_versioned_receiver(
            "3.4.0",
            "printf 'unexpected\\n' > \"$PWD/pack/unexpected.txt\"\n",
        )
        module = load_dispatch_module("luca_dispatch_post_transfer_validate")
        trailing = (
            "--server",
            "-r",
            "-t",
            "-p",
            "--safe-links",
            ".",
            "/home/admin/.persona-engine/luca/pack",
        )
        with self.assertRaises(module.DispatchError):
            module._receive_pack(
                self.install,
                self.rsync_path,
                trailing,
                self.root / "home/admin/.hermes/profiles/luca/rsync-receiver.log",
                self.root / "etc/pgl-luca-rsync-backport.json",
            )
        self.assertEqual(
            (self.pack / "unexpected.txt").read_text(encoding="utf-8"), "unexpected\n"
        )
        self.assertLess(module._tree_size(self.pack), module._TRANSFER_CAP_BYTES)

    def test_deploy_promote_builds_out_of_place_and_preserves_old_build_on_gate_failure(self):
        promoted = self.run_dispatch(
            f"deploy promote {self.expected_hash}", role="deploy"
        )
        self.assertEqual(promoted.returncode, 0, promoted.stderr)
        self.assertEqual(self.run_dispatch("hash", role="deploy").stdout, self.expected_hash + "\n")
        self.assertFalse(any(path.name.startswith(".luca-build-") for path in self.install.iterdir()))

        adapter = self.root / "home/admin/.hermes/profiles/luca/plugins/persona-engine/version.py"
        adapter.write_text('VERSION = "0.2.0"\n', encoding="utf-8")
        before = (self.build / "manifest.json").read_bytes()
        self.assert_rejected(f"deploy promote {self.expected_hash}", role="deploy")
        self.assertEqual((self.build / "manifest.json").read_bytes(), before)

    def test_deploy_promote_rejects_wrong_hash_at_equality_gate(self):
        before = (self.build / "manifest.json").read_bytes()
        self.assert_rejected(f"deploy promote {'0' * 64}", role="deploy")
        self.assertEqual((self.build / "manifest.json").read_bytes(), before)

    def test_deploy_promote_self_heals_missing_build_from_previous_residue(self):
        old_build = self.install / ".luca-build-previous"
        os.replace(self.build, old_build)
        promoted = self.run_dispatch(
            f"deploy promote {self.expected_hash}", role="deploy"
        )
        self.assertEqual(promoted.returncode, 0, promoted.stderr)
        self.assertTrue(self.build.is_dir())
        self.assertFalse(old_build.exists())

    def test_deploy_promote_heals_missing_build_before_later_hash_rejection(self):
        old_build = self.install / ".luca-build-previous"
        os.replace(self.build, old_build)
        self.assert_rejected(f"deploy promote {'0' * 64}", role="deploy")
        self.assertTrue(self.build.is_dir())
        self.assertFalse(old_build.exists())

    def test_deploy_promote_rejects_ambiguous_previous_and_current_builds(self):
        old_build = self.install / ".luca-build-previous"
        shutil.copytree(self.build, old_build)
        module = load_dispatch_module("luca_dispatch_promote_ambiguous")
        with mock.patch.object(module, "_run_fixed", side_effect=AssertionError("should not build")):
            with self.assertRaises(module.DispatchError):
                module._promote_build(
                    self.install,
                    self.expected_hash,
                    self.node_path,
                    self.cli_path,
                    self.root / "home/admin/.persona-engine/cli/packages/core/package.json",
                    self.root / "home/admin/.hermes/profiles/luca/plugins/persona-engine/version.py",
                )
        self.assertTrue(self.build.is_dir())
        self.assertTrue(old_build.is_dir())

    def test_restore_tolerates_missing_production_build(self):
        backed_up = self.run_dispatch("deploy backup", role="deploy")
        self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
        shutil.rmtree(self.build)
        restored = self.run_dispatch("restore", role="deploy")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertTrue((self.build / "manifest.json").is_file())

    def test_restore_rejects_when_no_backup_is_available(self):
        backups = self.root / "home/admin/.hermes/backups"
        backups.mkdir(parents=True)
        module = load_dispatch_module("luca_dispatch_no_backup")
        with self.assertRaises(module.DispatchError) as caught:
            module._latest_backup(backups)
        self.assertEqual(str(caught.exception), "no backup available")

    def test_restore_rolls_back_when_post_swap_verification_fails(self):
        backed_up = self.run_dispatch("deploy backup", role="deploy")
        self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
        (self.install / "install.yml").write_text("runtime: mutated\n", encoding="utf-8")
        (self.pack / "aliases.yml").write_text("aliases: {mutated: true}\n", encoding="utf-8")
        mutated_install = (self.install / "install.yml").read_bytes()
        mutated_aliases = (self.pack / "aliases.yml").read_bytes()
        mutated_build = (self.build / "manifest.json").read_bytes()
        module = load_dispatch_module("luca_dispatch_restore_rollback")
        original_hash_install = module._hash_install

        def failing_hash(install_root, node_path, cli_path):
            if install_root == self.install:
                raise module.DispatchError("post-swap mismatch")
            return original_hash_install(install_root, node_path, cli_path)

        with mock.patch.object(module, "_hash_install", side_effect=failing_hash):
            with self.assertRaises(module.DispatchError):
                module._restore_backup(
                    self.install,
                    self.root / "home/admin/.hermes/backups",
                    self.node_path,
                    self.cli_path,
                )
        self.assertEqual((self.install / "install.yml").read_bytes(), mutated_install)
        self.assertEqual((self.pack / "aliases.yml").read_bytes(), mutated_aliases)
        self.assertEqual((self.build / "manifest.json").read_bytes(), mutated_build)

    def test_deploy_restart_uses_fixed_two_unit_vector(self):
        result = self.run_dispatch("deploy restart", role="deploy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.install / "systemctl.argv").read_text(encoding="utf-8").splitlines(),
            ["--user", "restart", "hermes-gateway-luca", "hermes-api-luca"],
        )

    def _run_accept_server(self):
        requests = []
        session_id = "12345678-1234-1234-1234-123456789abc"
        install = self.install
        manifest = json.loads((self.build / "manifest.json").read_text(encoding="utf-8"))

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(handler):
                length = int(handler.headers["Content-Length"])
                body = json.loads(handler.rfile.read(length))
                requests.append((handler.path, handler.headers.get("Authorization"), body))
                if handler.path != "/v1/responses" or handler.headers.get("Authorization") != "Bearer fixture-secret":
                    handler.send_error(403)
                    return
                utterance = body["input"]
                if utterance.startswith("/persona "):
                    mode = utterance.split(" ", 1)[1]
                    if mode == "public":
                        block_bytes = 0
                        block_sha256 = hashlib.sha256(b"").hexdigest()
                    else:
                        metadata = manifest["modes"][mode]
                        block_bytes = metadata["bytes"]
                        block_sha256 = metadata["sha256"]
                    status = {
                        "mode": mode,
                        "block_bytes": block_bytes,
                        "block_sha256": block_sha256,
                    }
                    (install / "state/status.json").write_text(
                        json.dumps(status), encoding="utf-8"
                    )
                handler.send_response(200)
                handler.send_header("X-Hermes-Session-Id", session_id)
                handler.send_header("Content-Length", "2")
                handler.end_headers()
                handler.wfile.write(b"{}")

            def log_message(self, _format, *_arguments):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state = self.install / "state"
        state.mkdir(exist_ok=True)
        (state / "status.json").write_text(
            json.dumps(
                {
                    "mode": "public",
                    "block_bytes": 0,
                    "block_sha256": hashlib.sha256(b"").hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        config = self.root / "home/admin/.config/caty-gateway/luca-hermes-api.env"
        config.parent.mkdir(parents=True)
        config.write_text(
            f"API_SERVER_KEY=fixture-secret\nAPI_SERVER_PORT={server.server_port}\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        return server, thread, requests, session_id

    def test_accept_uses_loopback_token_restores_pre_mode_and_returns_uuid_array(self):
        try:
            server, thread, requests, session_id = self._run_accept_server()
        except PermissionError:
            self.skipTest("loopback bind unavailable in this sandbox")
        else:
            try:
                result = self.run_dispatch("accept", role="deploy")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [session_id])
        status = json.loads((self.install / "state/status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["mode"], "public")
        self.assertEqual(
            [entry[2]["input"] for entry in requests],
            ["deployment warm-up", "/persona mode-b", "/persona public"],
        )
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)

    def test_accept_transport_disables_proxies_and_redirects(self):
        module = load_dispatch_module("luca_dispatch_transport")
        response = mock.MagicMock()
        response.headers.get_all.return_value = [
            "12345678-1234-1234-1234-123456789abc"
        ]
        response.read.return_value = b"{}"
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with mock.patch.object(
            module.urllib.request, "build_opener", return_value=opener
        ) as build_opener:
            session_id = module._send_accept_turn(
                "fixture-secret", 4321, "fixed-conversation", "deployment warm-up"
            )
        self.assertEqual(session_id, "12345678-1234-1234-1234-123456789abc")
        handlers = build_opener.call_args.args
        self.assertTrue(any(isinstance(item, module.urllib.request.ProxyHandler) for item in handlers))
        proxy_handler = next(
            item for item in handlers if isinstance(item, module.urllib.request.ProxyHandler)
        )
        self.assertEqual(proxy_handler.proxies, {})
        self.assertTrue(any(isinstance(item, module._NoRedirect) for item in handlers))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:4321/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-secret")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)

    def test_accept_transport_rejects_redirects_cleanly(self):
        module = load_dispatch_module("luca_dispatch_redirect")
        opener = mock.MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            "http://127.0.0.1:4321/v1/responses",
            302,
            "redirect",
            {},
            None,
        )
        with mock.patch.object(
            module.urllib.request, "build_opener", return_value=opener
        ):
            with self.assertRaises(module.DispatchError):
                module._send_accept_turn(
                    "fixture-secret", 4321, "fixed-conversation", "deployment warm-up"
                )

    def test_accept_config_enforces_0o077_permission_gate(self):
        config = self.root / "home/admin/.config/caty-gateway/luca-hermes-api.env"
        config.parent.mkdir(parents=True)
        config.write_text("API_SERVER_KEY=fixture-secret\nAPI_SERVER_PORT=1\n", encoding="utf-8")
        config.chmod(0o644)
        module = load_dispatch_module("luca_dispatch_accept_config")
        with self.assertRaises(module.DispatchError):
            module._accept_config(config)
        config.chmod(0o600)
        self.assertEqual(module._accept_config(config), ("fixture-secret", 1))

    def test_accept_returns_every_distinct_generated_session_uuid(self):
        module = load_dispatch_module("luca_dispatch_multi_session")
        state = self.install / "state"
        state.mkdir(exist_ok=True)
        (state / "status.json").write_text(
            json.dumps(
                {
                    "mode": "public",
                    "block_bytes": 0,
                    "block_sha256": hashlib.sha256(b"").hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        config = self.root / "home/admin/.config/caty-gateway/luca-hermes-api.env"
        config.parent.mkdir(parents=True)
        config.write_text(
            "API_SERVER_KEY=fixture-secret\nAPI_SERVER_PORT=1\n", encoding="utf-8"
        )
        config.chmod(0o600)
        manifest = json.loads((self.build / "manifest.json").read_text(encoding="utf-8"))
        session_ids = iter(
            (
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
            )
        )

        def send_turn(_token, _port, _conversation, utterance):
            if utterance.startswith("/persona "):
                mode = utterance.split(" ", 1)[1]
                if mode == "public":
                    block_bytes = 0
                    block_sha256 = hashlib.sha256(b"").hexdigest()
                else:
                    metadata = manifest["modes"][mode]
                    block_bytes = metadata["bytes"]
                    block_sha256 = metadata["sha256"]
                (state / "status.json").write_text(
                    json.dumps(
                        {
                            "mode": mode,
                            "block_bytes": block_bytes,
                            "block_sha256": block_sha256,
                        }
                    ),
                    encoding="utf-8",
                )
            return next(session_ids)

        with mock.patch.object(module, "_send_accept_turn", send_turn):
            output = module._accept(
                self.install,
                config,
                self.root / "home/admin/.hermes/backups",
                "standard",
            )
        self.assertEqual(
            json.loads(output),
            [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
            ],
        )

    def test_accept_deletion_rejects_render_file_set_increase(self):
        overlay = self.pack / "catalogs/overlay"
        overlay.mkdir(parents=True)
        (overlay / "adopted.txt").write_text("old\n", encoding="utf-8")
        expected = content_hash(self.pack)
        rebuilt = self.run_dispatch(f"deploy promote {expected}", role="deploy")
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        self.assertEqual(self.run_dispatch("deploy backup", role="deploy").returncode, 0)
        try:
            server, thread, requests, _session_id = self._run_accept_server()
        except PermissionError:
            self.skipTest("loopback bind unavailable in this sandbox")
        try:
            (overlay / "candidates.txt").write_text("new\n", encoding="utf-8")
            result = self.run_dispatch("accept deletion", role="deploy")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, ERROR_LINE)
        self.assertEqual(requests, [])

    def test_accept_deletion_allows_shrunk_render_file_set(self):
        overlay = self.pack / "catalogs/overlay"
        overlay.mkdir(parents=True)
        (overlay / "old.txt").write_text("old\n", encoding="utf-8")
        (overlay / "drop.txt").write_text("drop\n", encoding="utf-8")
        expected = content_hash(self.pack)
        rebuilt = self.run_dispatch(f"deploy promote {expected}", role="deploy")
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        self.assertEqual(self.run_dispatch("deploy backup", role="deploy").returncode, 0)
        (overlay / "drop.txt").unlink()
        try:
            server, thread, requests, session_id = self._run_accept_server()
        except PermissionError:
            self.skipTest("loopback bind unavailable in this sandbox")
        try:
            result = self.run_dispatch("accept deletion", role="deploy")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [session_id])
        self.assertEqual(
            [entry[2]["input"] for entry in requests],
            ["deployment warm-up", "/persona mode-b", "/persona public"],
        )

    def test_assert_deletion_subset_rejects_increase_and_allows_shrink_or_unchanged(self):
        module = load_dispatch_module("luca_dispatch_deletion_subset")
        overlay = self.pack / "catalogs/overlay"
        overlay.mkdir(parents=True)
        (overlay / "kept.txt").write_text("kept\n", encoding="utf-8")
        (overlay / "drop.txt").write_text("drop\n", encoding="utf-8")
        backups = self.root / "home/admin/.hermes/backups"
        generation = backups / "luca-20260101T000000000000Z-00000001"
        (generation / "pack").parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.pack, generation / "pack")
        (generation / ".complete").write_bytes(module._BACKUP_COMPLETE)
        (generation / ".sequence").write_text("1\n", encoding="ascii")

        module._assert_deletion_subset(self.install, backups)
        (overlay / "drop.txt").unlink()
        module._assert_deletion_subset(self.install, backups)
        (overlay / "new.txt").write_text("new\n", encoding="utf-8")
        with self.assertRaises(module.DispatchError):
            module._assert_deletion_subset(self.install, backups)

    def test_accept_retries_mode_restore_and_reports_unknown_runtime_mode_on_failure(self):
        module = load_dispatch_module("luca_dispatch_accept_retry")
        state = self.install / "state"
        state.mkdir(exist_ok=True)
        (state / "status.json").write_text(
            json.dumps(
                {
                    "mode": "public",
                    "block_bytes": 0,
                    "block_sha256": hashlib.sha256(b"").hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        config = self.root / "home/admin/.config/caty-gateway/luca-hermes-api.env"
        config.parent.mkdir(parents=True)
        config.write_text(
            "API_SERVER_KEY=fixture-secret\nAPI_SERVER_PORT=1\n", encoding="utf-8"
        )
        config.chmod(0o600)
        manifest = json.loads((self.build / "manifest.json").read_text(encoding="utf-8"))
        calls = []

        def send_turn(_token, _port, _conversation, utterance):
            calls.append(utterance)
            if utterance == "deployment warm-up":
                return "11111111-1111-1111-1111-111111111111"
            if utterance == "/persona mode-b":
                metadata = manifest["modes"]["mode-b"]
                (state / "status.json").write_text(
                    json.dumps(
                        {
                            "mode": "mode-b",
                            "block_bytes": metadata["bytes"],
                            "block_sha256": metadata["sha256"],
                        }
                    ),
                    encoding="utf-8",
                )
                return "22222222-2222-2222-2222-222222222222"
            raise urllib.error.URLError("loopback unavailable")

        with mock.patch.object(module, "_send_accept_turn", side_effect=send_turn):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(
                    self.install,
                    config,
                    self.root / "home/admin/.hermes/backups",
                    "standard",
                )
        self.assertIn("runtime mode unknown", str(caught.exception))
        self.assertEqual(
            calls,
            [
                "deployment warm-up",
                "/persona mode-b",
                "/persona public",
                "/persona public",
            ],
        )
        status = json.loads((state / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["mode"], "mode-b")

    def test_accept_first_restore_failure_then_retry_success_still_rejects(self):
        module = load_dispatch_module("luca_dispatch_accept_retry_success")
        state = self.install / "state"
        state.mkdir(exist_ok=True)
        (state / "status.json").write_text(
            json.dumps(
                {
                    "mode": "public",
                    "block_bytes": 0,
                    "block_sha256": hashlib.sha256(b"").hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        config = self.root / "home/admin/.config/caty-gateway/luca-hermes-api.env"
        config.parent.mkdir(parents=True)
        config.write_text(
            "API_SERVER_KEY=fixture-secret\nAPI_SERVER_PORT=1\n", encoding="utf-8"
        )
        config.chmod(0o600)
        manifest = json.loads((self.build / "manifest.json").read_text(encoding="utf-8"))
        calls = []
        restore_attempts = 0

        def send_turn(_token, _port, _conversation, utterance):
            nonlocal restore_attempts
            calls.append(utterance)
            if utterance == "deployment warm-up":
                return "11111111-1111-1111-1111-111111111111"
            if utterance == "/persona mode-b":
                metadata = manifest["modes"]["mode-b"]
                (state / "status.json").write_text(
                    json.dumps(
                        {
                            "mode": "mode-b",
                            "block_bytes": metadata["bytes"],
                            "block_sha256": metadata["sha256"],
                        }
                    ),
                    encoding="utf-8",
                )
                return "22222222-2222-2222-2222-222222222222"
            if utterance == "/persona public":
                restore_attempts += 1
                if restore_attempts == 1:
                    raise urllib.error.URLError("first restore turn failed")
                (state / "status.json").write_text(
                    json.dumps(
                        {
                            "mode": "public",
                            "block_bytes": 0,
                            "block_sha256": hashlib.sha256(b"").hexdigest(),
                        }
                    ),
                    encoding="utf-8",
                )
                return "33333333-3333-3333-3333-333333333333"
            raise AssertionError(f"unexpected utterance: {utterance}")

        with mock.patch.object(module, "_send_accept_turn", side_effect=send_turn):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(
                    self.install,
                    config,
                    self.root / "home/admin/.hermes/backups",
                    "standard",
                )
        self.assertEqual(str(caught.exception), "acceptance failed")
        self.assertEqual(
            calls,
            [
                "deployment warm-up",
                "/persona mode-b",
                "/persona public",
                "/persona public",
            ],
        )
        status = json.loads((state / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["mode"], "public")

    def test_run_transfer_receiver_times_out_after_stderr_closes(self):
        self.rsync_path.write_text(
            "#!/bin/sh\n"
            "exec 2>&-\n"
            "sleep 10\n",
            encoding="utf-8",
        )
        self.rsync_path.chmod(0o755)
        module = load_dispatch_module("luca_dispatch_transfer_timeout")
        log_path = self.root / "home/admin/.hermes/profiles/luca/rsync-receiver.log"
        with mock.patch.object(module, "_TRANSFER_TIMEOUT_SECONDS", 1):
            with self.assertRaises(module.DispatchError):
                module._run_transfer_receiver(
                    [
                        str(self.rsync_path),
                        "--server",
                        "-r",
                        "-t",
                        "-p",
                        "--safe-links",
                        ".",
                        str(self.pack),
                    ],
                    self.install,
                    log_path,
                )
        self.assertTrue(log_path.is_file())
        self.assertEqual(log_path.read_text(encoding="utf-8"), "")

    def test_drain_pipe_before_deadline_times_out_when_writer_stays_open(self):
        module = load_dispatch_module("luca_dispatch_transfer_grandchild_timeout")
        read_fd, write_fd = os.pipe()
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os, sys, time; "
                    "fd = int(sys.argv[1]); "
                    "os.write(fd, b'x'); "
                    "time.sleep(10)"
                ),
                str(write_fd),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(write_fd,),
            close_fds=True,
        )
        os.close(write_fd)
        capture = bytearray()
        elapsed = None
        try:
            with os.fdopen(read_fd, "rb", closefd=True) as pipe:
                started = time.monotonic()
                deadline = started + 1
                self.assertFalse(
                    module._drain_pipe_before_deadline(pipe, capture, deadline, 16)
                )
                elapsed = time.monotonic() - started
        finally:
            holder.kill()
            holder.wait(timeout=5)
        self.assertEqual(bytes(capture), b"x")
        assert elapsed is not None
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertLess(elapsed, 2.0)

    def test_run_transfer_receiver_times_out_when_grandchild_holds_stderr_after_clean_exit(self):
        self.rsync_path.write_text(
            f"""#!{sys.executable}
import subprocess
import sys

subprocess.Popen(
    [
        {sys.executable!r},
        "-c",
        "import sys, time; sys.stderr.write('held-open\\\\n'); sys.stderr.flush(); time.sleep(10)",
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=sys.stderr,
    close_fds=False,
)
raise SystemExit(0)
""",
            encoding="utf-8",
        )
        self.rsync_path.chmod(0o755)
        module = load_dispatch_module("luca_dispatch_transfer_grandchild_integration")
        log_path = self.root / "home/admin/.hermes/profiles/luca/rsync-receiver.log"
        started = time.monotonic()
        with mock.patch.object(module, "_TRANSFER_TIMEOUT_SECONDS", 1):
            with self.assertRaises(module.DispatchError):
                module._run_transfer_receiver(
                    [
                        str(self.rsync_path),
                        "--server",
                        "-r",
                        "-t",
                        "-p",
                        "--safe-links",
                        ".",
                        str(self.pack),
                    ],
                    self.install,
                    log_path,
                )
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(log_path.read_text(encoding="utf-8"), "held-open\n")

    def test_unknown_subcommand_is_rejected_for_both_roles(self):
        for role in ("read", "deploy"):
            with self.subTest(role=role):
                self.assert_rejected("unknown-subcommand", role=role)

    def test_path_escape_arguments_are_rejected(self):
        for command in (
            "deploy promote ../../etc/passwd",
            "deploy transfer ../../etc",
            "restore ../old",
            "accept deletion ../backup",
        ):
            with self.subTest(command=command):
                self.assert_rejected(command, role="deploy")

    def test_ssh_original_command_injection_is_rejected_as_data(self):
        for command in (
            "deploy restart; id",
            "deploy $(id)",
            "deploy restart\nhash",
            "accept && hash",
        ):
            with self.subTest(command=command):
                self.assert_rejected(command, role="deploy")

        module = load_dispatch_module("luca_dispatch_nul")
        with mock.patch.object(module.sys, "argv", [str(DISPATCH), "deploy"]), mock.patch.object(
            module.os, "environ", {"SSH_ORIGINAL_COMMAND": "accept\x00hash"}
        ):
            with self.assertRaises(module.DispatchError):
                module._parse_request()

    def test_read_key_cannot_restart_or_reach_any_deploy_phase(self):
        for command in ("deploy backup", "deploy transfer", f"deploy promote {self.expected_hash}", "deploy restart"):
            with self.subTest(command=command):
                self.assert_rejected(command, role="read")

    def test_deploy_key_cannot_read_sessions(self):
        self.assert_rejected("read-sessions 100 200", role="deploy")

    def test_deploy_key_cannot_read_owners(self):
        self.assert_rejected("read-owners", role="deploy")

    def test_test_root_seam_is_ignored_at_installed_entrypoint(self):
        installed = self.root / "installed-pgl-luca-dispatch"
        source = DISPATCH.read_text(encoding="utf-8")
        source = source.replace(
            'Path("/usr/local/libexec/pgl-luca-dispatch")',
            f'Path({str(installed)!r})',
            1,
        )
        installed.write_text(source, encoding="utf-8")
        installed.chmod(0o755)
        # If the environment seam were honored here, this would return the
        # fixture owner list.  Installed-path simulation must instead consult
        # the fixed production path and fail in the isolated test environment.
        self.assert_rejected("read-owners", script=installed)


if __name__ == "__main__":
    unittest.main()
