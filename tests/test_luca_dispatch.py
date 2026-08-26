import contextlib
import hashlib
import http.client
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import select
import shutil
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

from tests.support import MACOS_RSYNC_CLIENT_SKIP_REASON


REPO = Path(__file__).resolve().parents[1]
DISPATCH = REPO / "vps" / "pgl-luca-dispatch"
PERSONA_BUILD_FIXTURE = REPO / "tests" / "fixtures" / "luca_dispatch" / "persona_build"
ERROR_LINE = "pgl-luca-dispatch: request rejected\n"
HAS_REAL_RSYNC = Path("/usr/bin/rsync").is_file()


def reason_line(code):
    return f"pgl-luca-dispatch: request rejected: {code}\n"


def load_dispatch_module(name):
    loader = importlib.machinery.SourceFileLoader(name, str(DISPATCH))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise AssertionError("dispatcher import spec unavailable")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class HeldAcceptResponse:
    def __init__(self):
        self.closed = False
        self.close_calls = 0
        self.read_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed = True

    def read(self, *_arguments, **_kwargs):
        self.read_calls += 1
        raise AssertionError("accept response body was read")


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


def _peer_disconnected(connection):
    readable, _, _ = select.select([connection], [], [], 0)
    if not readable:
        return False
    try:
        return connection.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


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
            f"""#!{python}
import os
from pathlib import Path
import sys
import time

ROOT = Path({str(self.root)!r})
arguments = sys.argv[1:]
line = " ".join(arguments)
log = Path.cwd() / "systemctl.argv"
with log.open("a", encoding="utf-8") as stream:
    stream.write(line + "\\n")
with (Path.cwd() / "systemctl.env").open("a", encoding="utf-8") as stream:
    stream.write(
        os.environ.get("XDG_RUNTIME_DIR", "")
        + "|"
        + os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        + "\\n"
    )

if len(arguments) >= 2 and arguments[1] == "restart":
    stem = "systemctl.restart"
elif len(arguments) == 4 and arguments[1:3] == ["is-active", "--quiet"]:
    unit = arguments[3]
    matching = [entry for entry in log.read_text(encoding="utf-8").splitlines() if entry == line]
    stem = f"systemctl.is-active.{{unit}}.{{len(matching)}}"
    fallback = ROOT / f"systemctl.is-active.{{unit}}.exit"
else:
    raise SystemExit(99)

sleep_path = ROOT / (stem + ".sleep")
if sleep_path.is_file():
    time.sleep(float(sleep_path.read_text(encoding="ascii")))
exit_path = ROOT / (stem + ".exit")
if not exit_path.is_file() and "fallback" in globals():
    exit_path = fallback
if exit_path.is_file():
    raise SystemExit(int(exit_path.read_text(encoding="ascii")))
""",
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
        MACOS_RSYNC_CLIENT_SKIP_REASON,
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

    def test_transfer_argv_rejects_foreign_destination_before_exec(self):
        module = load_dispatch_module("luca_dispatch_destination_gate")
        cases = [
            ("--server", "-rpt", ".", "/tmp/not-pack"),
            (
                "--server",
                "-rpt",
                ".",
                "/home/admin/.persona-engine/luca/pack/../escape",
            ),
            ("--server", "-rpt", "not-dot", "/home/admin/.persona-engine/luca/pack"),
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
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  printf 'rsync  version 3.4.0  protocol version 31\\n'\n"
            "  exit 0\n"
            "fi\n"
            "mkdir -p pack/catalogs\n"
            "printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' > pack/catalogs/oversized.bin\n",
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
            [
                "--user restart hermes-gateway-luca hermes-api-luca",
                "--user is-active --quiet hermes-gateway-luca",
                "--user is-active --quiet hermes-api-luca",
                "--user is-active --quiet hermes-gateway-luca",
                "--user is-active --quiet hermes-api-luca",
                "--user is-active --quiet hermes-gateway-luca",
                "--user is-active --quiet hermes-api-luca",
            ],
        )
        runtime_dir = f"/run/user/{os.getuid()}"
        self.assertEqual(
            (self.install / "systemctl.env").read_text(encoding="utf-8").splitlines(),
            [f"{runtime_dir}|unix:path={runtime_dir}/bus"] * 7,
        )

    def test_restart_rejects_mixed_unit_state_on_first_or_later_sample(self):
        for sample in (1, 2):
            with self.subTest(sample=sample):
                for path in self.root.glob("systemctl.*.exit"):
                    path.unlink()
                for path in (self.install / "systemctl.argv", self.install / "systemctl.env"):
                    path.unlink(missing_ok=True)
                (self.root / f"systemctl.is-active.hermes-api-luca.{sample}.exit").write_text(
                    "3\n", encoding="ascii"
                )
                result = self.run_dispatch("deploy restart", role="deploy")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, reason_line("restart-units-not-active"))

    def test_restart_nonzero_reports_command_failed(self):
        (self.root / "systemctl.restart.exit").write_text("1\n", encoding="ascii")
        result = self.run_dispatch("deploy restart", role="deploy")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, reason_line("restart-command-failed"))

    def test_restart_timeout_constant_is_plumbed_to_fixed_command(self):
        (self.root / "systemctl.restart.sleep").write_text("2\n", encoding="ascii")
        module = load_dispatch_module("luca_dispatch_restart_timeout")
        with mock.patch.object(module, "_RESTART_TIMEOUT_SECONDS", 1):
            with self.assertRaises(module.DispatchError) as caught:
                module._restart(self.systemctl_path, self.install)
        self.assertEqual(caught.exception.reason_code, "restart-command-timeout")

    def test_restart_verify_timeout_reports_verification_failed(self):
        (self.root / "systemctl.is-active.hermes-gateway-luca.1.sleep").write_text(
            "0.2\n", encoding="ascii"
        )
        module = load_dispatch_module("luca_dispatch_restart_verify_timeout")
        with mock.patch.object(module, "_RESTART_VERIFY_TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(module.DispatchError) as caught:
                module._restart(self.systemctl_path, self.install)
        self.assertEqual(caught.exception.reason_code, "restart-verification-failed")

    def test_restart_verify_oserror_reports_verification_failed(self):
        module = load_dispatch_module("luca_dispatch_restart_verify_oserror")
        calls = 0

        def run_fixed(*_arguments, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            raise module._FixedCommandExecutionError("fixed command failed")

        with mock.patch.object(module, "_run_fixed", side_effect=run_fixed):
            with self.assertRaises(module.DispatchError) as caught:
                module._restart(self.systemctl_path, self.install)
        self.assertEqual(caught.exception.reason_code, "restart-verification-failed")

    def test_restart_budget_dominates_stop_timeouts_and_stays_below_client_cap(self):
        module = load_dispatch_module("luca_dispatch_restart_budget")
        self.assertGreaterEqual(module._RESTART_TIMEOUT_SECONDS, 2 * 90 + 30)
        total = (
            module._RESTART_TIMEOUT_SECONDS
            + module._RESTART_VERIFY_SAMPLES
            * len(module._SYSTEMD_UNITS)
            * module._RESTART_VERIFY_TIMEOUT_SECONDS
            + (module._RESTART_VERIFY_SAMPLES - 1)
            * module._RESTART_VERIFY_DWELL_SECONDS
        )
        self.assertLess(total, 300)

    def _run_accept_server(self):
        records = []
        session_ids = (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
            "44444444-4444-4444-8444-444444444444",
        )
        record_lock = threading.Lock()
        install = self.install
        manifest = json.loads((self.build / "manifest.json").read_text(encoding="utf-8"))

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(handler):
                length = int(handler.headers["Content-Length"])
                body = json.loads(handler.rfile.read(length))
                with record_lock:
                    if len(records) >= len(session_ids):
                        handler.send_error(
                            500, "unexpected extra request beyond fixture session_ids"
                        )
                        return
                    session_id = session_ids[len(records)]
                    record = {
                        "path": handler.path,
                        "authorization": handler.headers.get("Authorization"),
                        "body": body,
                        "session_id": session_id,
                        "body_writes": 0,
                        "status_writes": 0,
                        "status_write_while_open": False,
                        "disconnect_exception": None,
                        "disconnect_observed": threading.Event(),
                        "stream_completed": False,
                        "connection": handler.connection,
                    }
                    records.append(record)
                if (
                    handler.path != "/v1/responses"
                    or handler.headers.get("Authorization") != "Bearer fixture-secret"
                    or body.get("stream") is not True
                    or not isinstance(body.get("conversation"), str)
                ):
                    handler.send_error(403)
                    return
                utterance = body["input"]
                mode = "public" if utterance == "deployment warm-up" else utterance.split(" ", 1)[1]
                if mode == "public":
                    block_bytes = 0
                    block_sha256 = hashlib.sha256(b"").hexdigest()
                else:
                    metadata = manifest["modes"][mode]
                    block_bytes = metadata["bytes"]
                    block_sha256 = metadata["sha256"]
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream")
                handler.send_header("X-Hermes-Session-Id", session_id)
                handler.send_header("Connection", "close")
                handler.end_headers()
                try:
                    handler.wfile.write(b"data: unread-body-bytes\n\n")
                    handler.wfile.flush()
                    record["body_writes"] += 1
                    status_write_while_open = not _peer_disconnected(
                        handler.connection
                    )
                    (install / "state/status.json").write_text(
                        json.dumps(
                            {
                                "ts": "2026-08-17T12:00:00.000Z",
                                "mode": mode,
                                "block_bytes": block_bytes,
                                "block_sha256": block_sha256,
                                "turn_key": session_id + ":fixture:abcdef12",
                            }
                        ),
                        encoding="utf-8",
                    )
                    record["status_writes"] += 1
                    record["status_write_while_open"] = status_write_while_open
                    while True:
                        handler.wfile.write(b"data: unread-body-bytes\n\n")
                        handler.wfile.flush()
                        record["body_writes"] += 1
                except (BrokenPipeError, ConnectionResetError) as exc:
                    record["disconnect_exception"] = exc
                    record["disconnect_observed"].set()
                    return
                record["stream_completed"] = True

            def log_message(self, _format, *_arguments):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._write_accept_status("public")
        config = self.root / "home/admin/.config/caty-gateway/luca-hermes-api.env"
        config.parent.mkdir(parents=True)
        config.write_text(
            f"API_SERVER_KEY=fixture-secret\nAPI_SERVER_PORT={server.server_port}\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        return server, thread, records, session_ids

    def _stop_accept_server(self, server, thread, records):
        server.shutdown()
        for record in records:
            connection = record["connection"]
            try:
                connection.shutdown(2)
            except OSError:
                pass
            connection.close()
        server.server_close()
        if thread is not None:
            thread.join(timeout=1)

    def _assert_accept_streams_held(self, records):
        for item in records:
            self.assertTrue(item["held_at_writes"])
            self.assertTrue(all(item["held_at_writes"]))

    def _accept_with_restore_retry_oracle(
        self, module, config, backups, kind="standard"
    ):
        try:
            return module._accept(self.install, config, backups, kind)
        except module.DispatchError as exc:
            self.assertEqual(exc.reason_code, "accept-restore-retried")
            return None

    def test_accept_real_streams_persist_owned_status_before_client_disconnect(self):
        module = load_dispatch_module("luca_dispatch_accept_real_streams")
        server, thread, records, session_ids = self._run_accept_server()
        client_body_reads = []

        def reject_body_read(response, *_arguments, **_kwargs):
            client_body_reads.append(response)
            raise AssertionError("accept response body was read")

        try:
            config = self.root / "home/admin/.config/caty-gateway/luca-hermes-api.env"
            with mock.patch.object(http.client.HTTPResponse, "read", reject_body_read):
                output = self._accept_with_restore_retry_oracle(
                    module, config, self.root / "backups"
                )
            for record in records:
                self.assertTrue(record["disconnect_observed"].wait(timeout=1))
        finally:
            self._stop_accept_server(server, thread, records)

        if output is not None:
            self.assertEqual(json.loads(output), sorted(session_ids[:3]))
            self.assertEqual(
                [record["body"]["input"] for record in records],
                ["deployment warm-up", "/persona mode-b", "/persona public"],
            )
            self.assertEqual([record["path"] for record in records], ["/v1/responses"] * 3)
            self.assertEqual(
                [record["authorization"] for record in records],
                ["Bearer fixture-secret"] * 3,
            )
            self.assertEqual(len({record["body"]["conversation"] for record in records}), 3)
        else:
            self.assertEqual(len(records), 4)
            self.assertEqual(records[-1]["body"]["input"], "/persona public")
        self.assertTrue(all(record["body"]["stream"] is True for record in records))
        self.assertEqual(client_body_reads, [])
        self.assertEqual(
            json.loads((self.install / "state/status.json").read_text(encoding="utf-8"))[
                "mode"
            ],
            "public",
        )
        for record in records:
            self.assertGreater(record["body_writes"], 0)
            self.assertEqual(record["status_writes"], 1)
            self.assertTrue(record["status_write_while_open"])
            self.assertIsInstance(
                record["disconnect_exception"],
                (BrokenPipeError, ConnectionResetError),
            )
            self.assertFalse(record["stream_completed"])

    def test_peer_disconnected_probe_discriminates(self):
        left, right = socket.socketpair()
        try:
            self.assertFalse(_peer_disconnected(left))
            right.sendall(b"x")
            self.assertFalse(_peer_disconnected(left))
            self.assertEqual(left.recv(1), b"x")
            right.close()
            self.assertTrue(_peer_disconnected(left))
        finally:
            left.close()

    def test_accept_uses_loopback_token_restores_pre_mode_and_returns_uuid_array(self):
        module = load_dispatch_module("luca_dispatch_accept_success")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            output = self._accept_with_restore_retry_oracle(
                module, config, self.root / "backups"
            )
        if output is not None:
            self.assertEqual(len(json.loads(output)), 3)
            self.assertEqual([item["utterance"] for item in records], [
                "deployment warm-up", "/persona mode-b", "/persona public"
            ])
            self.assertNotIn(b"fixture-secret", output)
        else:
            self.assertEqual(len(records), 4)
            self.assertEqual(records[-1]["utterance"], "/persona public")

    def test_accept_settles_when_server_responds_before_status_write(self):
        module = load_dispatch_module("luca_dispatch_headers_before_status")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            output = self._accept_with_restore_retry_oracle(
                module, config, self.root / "backups"
            )
        if output is None:
            self.assertEqual(len(records), 4)
        self.assertTrue(all(item["held_at_writes"] == [True] for item in records))

    def test_accept_settles_across_torn_intermediate_switch_reread(self):
        module = load_dispatch_module("luca_dispatch_accept_torn_reread")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"unreadable": True}, {"mode": "mode-b"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            output = self._accept_with_restore_retry_oracle(
                module, config, self.root / "backups"
            )
        if output is not None:
            self.assertEqual(len(json.loads(output)), 3)
        else:
            self.assertEqual(len(records), 4)
            self.assertEqual(records[-1]["utterance"], "/persona public")

    def test_accept_torn_reread_restore_timeout_consumes_fourth_stream(self):
        module = load_dispatch_module("luca_dispatch_accept_torn_reread_retry")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"unreadable": True}, {"mode": "mode-b"}]},
            {"writes": [{"mode": "public", "owner": "foreign"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-restore-retried")
        self.assertEqual(len(records), 4)
        self.assertEqual(records[-1]["utterance"], "/persona public")

    def test_accept_transport_disables_proxies_and_redirects(self):
        module = load_dispatch_module("luca_dispatch_transport")
        response = mock.MagicMock()
        response.headers.get_all.return_value = [
            "12345678-1234-1234-1234-123456789abc"
        ]
        response.read.side_effect = AssertionError("accept response body was read")
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch.object(
            module.urllib.request, "build_opener", return_value=opener
        ) as build_opener:
            held, session_id = module._open_accept_stream(
                "fixture-secret",
                4321,
                "fixed-conversation",
                "deployment warm-up",
                module._time.monotonic() + 30,
            )
        self.assertIs(held, response)
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
        self.assertEqual(
            json.loads(request.data),
            {
                "input": "deployment warm-up",
                "conversation": "fixed-conversation",
                "stream": True,
            },
        )
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        response.read.assert_not_called()

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
                module._open_accept_stream(
                    "fixture-secret",
                    4321,
                    "fixed-conversation",
                    "deployment warm-up",
                    module._time.monotonic() + 30,
                )

    def test_accept_transport_slow_server_raises_timeout_subclass(self):
        module = load_dispatch_module("luca_dispatch_accept_slow_server")
        opener = mock.MagicMock()
        opener.open.side_effect = TimeoutError()
        with mock.patch.object(module.urllib.request, "build_opener", return_value=opener):
            with mock.patch.object(module, "_ACCEPT_OPEN_TIMEOUT_SECONDS", 0.05):
                with self.assertRaises(module._AcceptRequestTimeout):
                    module._open_accept_stream(
                        "fixture-secret",
                        4321,
                        "fixed-conversation",
                        "/persona mode-b",
                        module._time.monotonic() + 1,
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

    def _write_accept_status(
        self,
        mode,
        *,
        block_bytes=None,
        block_sha256=None,
        turn_key="00000000-0000-0000-0000-000000000000:fixture:abcdef12",
        ts="2026-08-17T12:00:00.000Z",
    ):
        if mode == "public":
            expected_bytes = 0
            expected_sha256 = hashlib.sha256(b"").hexdigest()
        else:
            manifest = json.loads(
                (self.build / "manifest.json").read_text(encoding="utf-8")
            )
            metadata = manifest["modes"][mode]
            expected_bytes = metadata["bytes"]
            expected_sha256 = metadata["sha256"]
        state = self.install / "state"
        state.mkdir(exist_ok=True)
        (state / "status.json").write_text(
            json.dumps(
                {
                    "ts": ts,
                    "mode": mode,
                    "block_bytes": expected_bytes if block_bytes is None else block_bytes,
                    "block_sha256": (
                        expected_sha256 if block_sha256 is None else block_sha256
                    ),
                    "turn_key": turn_key,
                }
            ),
            encoding="utf-8",
        )

    def _write_accept_config(self):
        config = self.root / "home/admin/.config/caty-gateway/luca-hermes-api.env"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "API_SERVER_KEY=fixture-secret\nAPI_SERVER_PORT=1\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        return config

    def _patch_accept_open(self, module, send_turn, responses=None, conversations=None):
        responses = [] if responses is None else responses
        conversations = [] if conversations is None else conversations
        module._ACCEPT_OPEN_TIMEOUT_SECONDS = 0.5
        module._ACCEPT_WARMUP_WINDOW_SECONDS = 0.5
        module._ACCEPT_SWITCH_WINDOW_SECONDS = 0.5
        module._ACCEPT_RESTORE_WINDOW_SECONDS = 0.5
        module._ACCEPT_POLL_INTERVAL_SECONDS = 0.001
        module._ACCEPT_DISCONNECT_HORIZON_SECONDS = 0.002
        module._ACCEPT_RESTORE_RESERVED_SECONDS = 1.5
        module._ACCEPT_DEADLINE_SECONDS = 10.0

        def open_stream(token, port, conversation, utterance, _deadline):
            conversations.append(conversation)
            session_id = send_turn(token, port, conversation, utterance)
            status_path = self.install / "state/status.json"
            if status_path.is_file():
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    status = None
                if isinstance(status, dict):
                    status["ts"] = "2026-08-17T12:00:00.000Z"
                    status["turn_key"] = session_id + ":fixture:abcdef12"
                    status_path.write_text(json.dumps(status), encoding="utf-8")
            response = mock.MagicMock()
            response.read.side_effect = AssertionError("accept response body was read")
            responses.append(response)
            return response, session_id

        return mock.patch.object(module, "_open_accept_stream", side_effect=open_stream)

    @contextlib.contextmanager
    def _scripted_accept(self, module, steps, *, deadline=10.0, horizon=0.002):
        original_status = module._status
        records = []
        pending = []
        module._ACCEPT_OPEN_TIMEOUT_SECONDS = 0.5
        module._ACCEPT_WARMUP_WINDOW_SECONDS = 0.5
        module._ACCEPT_SWITCH_WINDOW_SECONDS = 0.5
        module._ACCEPT_RESTORE_WINDOW_SECONDS = 0.5
        module._ACCEPT_POLL_INTERVAL_SECONDS = 0.001
        module._ACCEPT_DISCONNECT_HORIZON_SECONDS = horizon
        module._ACCEPT_RESTORE_RESERVED_SECONDS = 1.5
        module._ACCEPT_DEADLINE_SECONDS = deadline

        opener = mock.MagicMock()

        def open_response(request, *, timeout):
            index = len(records)
            step = dict(steps[index])
            error = step.get("error")
            body = json.loads(request.data)
            self.assertEqual(request.full_url, "http://127.0.0.1:1/v1/responses")
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-secret")
            self.assertIs(body.get("stream"), True)
            self.assertIsInstance(body.get("conversation"), str)
            self.assertTrue(body["conversation"])
            records.append(
                {
                    "conversation": body["conversation"],
                    "utterance": body["input"],
                    "response": None,
                    "session_id": None,
                    "held_at_writes": [],
                    "turn_keys_at_writes": [],
                    "open_timeout": timeout,
                }
            )
            if error is not None:
                raise error
            session_id = f"{index + 1:08d}-1111-4111-8111-{index + 1:012d}"
            response = HeldAcceptResponse()
            response.headers = mock.MagicMock()
            response.headers.get_all.return_value = [session_id]
            records[-1]["response"] = response
            records[-1]["session_id"] = session_id
            if step.get("close_at_open"):
                response.close()
            writes = list(step.get("writes", []))
            pending.append(
                {
                    "response": response,
                    "session_id": session_id,
                    "writes": writes,
                    "record": records[-1],
                }
            )
            return response

        opener.open.side_effect = open_response

        def read_status(path):
            while pending and not pending[0]["writes"]:
                pending.pop(0)
            if pending and pending[0]["writes"]:
                current = pending[0]
                write = dict(current["writes"].pop(0))
                current["record"]["held_at_writes"].append(
                    not current["response"].closed
                )
                if write.pop("unreadable", False):
                    path.write_bytes(b"{")
                else:
                    owner = write.pop("owner", "self")
                    if owner == "self":
                        turn_key = current["session_id"] + ":fixture:abcdef12"
                    elif owner == "fallback":
                        turn_key = current["session_id"]
                    elif owner == "warmup":
                        turn_key = records[0]["session_id"] + ":fixture:abcdef12"
                    else:
                        turn_key = "99999999-9999-4999-8999-999999999999:foreign:abcdef12"
                    current["record"]["turn_keys_at_writes"].append(turn_key)
                    self._write_accept_status(turn_key=turn_key, **write)
            return original_status(path)

        with mock.patch.object(
            module.urllib.request, "build_opener", return_value=opener
        ):
            with mock.patch.object(module, "_status", side_effect=read_status):
                yield records

    def test_accept_owned_streams_hold_until_each_oracle_and_close_without_reads(self):
        module = load_dispatch_module("luca_dispatch_owned_streams")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            output = self._accept_with_restore_retry_oracle(
                module, config, self.root / "backups"
            )
        if output is not None:
            self.assertEqual(json.loads(output), sorted(item["session_id"] for item in records))
            self.assertEqual(len({item["conversation"] for item in records}), 3)
            self.assertEqual(
                [item["utterance"] for item in records],
                ["deployment warm-up", "/persona mode-b", "/persona public"],
            )
        else:
            self.assertEqual(len(records), 4)
            self.assertEqual(records[-1]["utterance"], "/persona public")
        self._assert_accept_streams_held(records)
        for item in records:
            self.assertTrue(item["response"].closed)
            self.assertEqual(item["response"].close_calls, 1)
            self.assertEqual(item["response"].read_calls, 0)

    def test_accept_hold_open_assertion_rejects_close_immediately_control(self):
        module = load_dispatch_module("luca_dispatch_closed_stream_control")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"close_at_open": True, "writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            output = self._accept_with_restore_retry_oracle(
                module, config, self.root / "backups"
            )
        if output is None:
            self.assertEqual(len(records), 4)
        self.assertEqual(records[0]["held_at_writes"], [False])
        with self.assertRaises(AssertionError):
            self._assert_accept_streams_held(records)

    def test_accept_stale_pre_mode_is_replaced_by_owned_warmup_mode(self):
        module = load_dispatch_module("luca_dispatch_stale_pre_mode")
        self._write_accept_status("mode-b")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            output = self._accept_with_restore_retry_oracle(
                module, config, self.root / "backups"
            )
        if output is not None:
            self.assertEqual(len(json.loads(output)), 3)
        else:
            self.assertEqual(len(records), 4)
            self.assertEqual(records[-1]["utterance"], "/persona public")
        self.assertEqual(json.loads((self.install / "state/status.json").read_text())["mode"], "public")

    def test_accept_owned_warmup_at_verify_mode_refuses_degenerate_switch(self):
        module = load_dispatch_module("luca_dispatch_degenerate_owned_warmup")
        self._write_accept_status("public")
        config = self._write_accept_config()
        with self._scripted_accept(
            module, [{"writes": [{"mode": "mode-b"}]}]
        ) as records:
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-switch-failed")
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["response"].closed)

    def test_accept_foreign_warmup_persist_cannot_satisfy_oracle(self):
        module = load_dispatch_module("luca_dispatch_foreign_warmup")
        self._write_accept_status("public")
        config = self._write_accept_config()
        with self._scripted_accept(
            module, [{"writes": [{"mode": "public", "owner": "foreign"}]}]
        ) as records:
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-warmup-timeout")
        self.assertEqual(len(records), 1)

    def test_accept_preexisting_target_and_unowned_switch_persist_fail(self):
        module = load_dispatch_module("luca_dispatch_preexisting_target")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b", "owner": "foreign"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-switch-not-applied")
        self.assertEqual(len(records), 4)

    def test_accept_switch_stale_writer_waits_for_owned_persist(self):
        module = load_dispatch_module("luca_dispatch_stale_writer")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {
                "writes": [
                    {"mode": "mode-b", "owner": "foreign"},
                    {"mode": "mode-b"},
                ]
            },
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            output = self._accept_with_restore_retry_oracle(
                module, config, self.root / "backups"
            )
        if output is None:
            self.assertEqual(len(records), 4)
        self.assertEqual(records[1]["held_at_writes"], [True, True])

    def test_accept_restore_rejects_stale_warmup_owner_on_both_attempts(self):
        module = load_dispatch_module("luca_dispatch_restore_stale_owner")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b"}]},
            {"writes": [{"mode": "public", "owner": "foreign"}]},
            {"writes": [{"mode": "public", "owner": "foreign"}]},
        ]
        with self._scripted_accept(module, steps):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-restore-failed")

    def test_accept_restore_rejects_literal_warmup_turn_key_on_both_attempts(self):
        module = load_dispatch_module("luca_dispatch_restore_literal_warmup_owner")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b"}]},
            {"writes": [{"mode": "public", "owner": "warmup"}]},
            {"writes": [{"mode": "public", "owner": "warmup"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-restore-failed")
        stale_warmup_turn_key = records[0]["session_id"] + ":fixture:abcdef12"
        self.assertTrue(
            module._owned({"turn_key": stale_warmup_turn_key}, records[0]["session_id"])
        )
        self.assertEqual(
            [records[2]["turn_keys_at_writes"], records[3]["turn_keys_at_writes"]],
            [[stale_warmup_turn_key], [stale_warmup_turn_key]],
        )
        self.assertFalse(
            module._owned({"turn_key": stale_warmup_turn_key}, records[2]["session_id"])
        )
        self.assertFalse(
            module._owned({"turn_key": stale_warmup_turn_key}, records[3]["session_id"])
        )

    def test_accept_zombie_flip_cannot_be_certified_by_pre_horizon_restore(self):
        module = load_dispatch_module("luca_dispatch_zombie_flip")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public", "owner": "foreign"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b", "owner": "foreign"}]},
        ]
        with self._scripted_accept(module, steps, horizon=0.002):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-restore-failed")

    def test_accept_deadline_truncated_horizon_never_certifies_restore(self):
        module = load_dispatch_module("luca_dispatch_truncated_horizon")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public", "owner": "foreign"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps, deadline=0.04, horizon=0.05):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-restore-failed")

    def test_accept_horizon_clock_granularity_cannot_shrink_certification_window(self):
        module = load_dispatch_module("luca_dispatch_horizon_granularity")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public", "owner": "foreign"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        clock = {"now": 0.0}

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += seconds + (0.001 if seconds > 0.0015 else 0.0)

        with self._scripted_accept(module, steps, deadline=0.0405, horizon=0.005):
            module._ACCEPT_OPEN_TIMEOUT_SECONDS = 0.0125
            module._ACCEPT_RESTORE_WINDOW_SECONDS = 0.0125
            module._ACCEPT_WARMUP_WINDOW_SECONDS = 0.01
            module._ACCEPT_SWITCH_WINDOW_SECONDS = 0.01
            module._ACCEPT_RESTORE_RESERVED_SECONDS = 0.03
            with mock.patch.object(module._time, "monotonic", side_effect=monotonic):
                with mock.patch.object(module._time, "sleep", side_effect=sleep):
                    with self.assertRaises(module.DispatchError) as caught:
                        module._accept(
                            self.install, config, self.root / "backups", "standard"
                        )
        self.assertEqual(caught.exception.reason_code, "accept-restore-failed")

    def test_accept_happy_path_never_waits_for_disconnect_horizon(self):
        module = load_dispatch_module("luca_dispatch_no_horizon_happy")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            with mock.patch.object(module._time, "sleep", wraps=time.sleep) as slept:
                output = self._accept_with_restore_retry_oracle(
                    module, config, self.root / "backups"
                )
        if output is None:
            self.assertEqual(len(records), 4)
            self.assertEqual(records[-1]["utterance"], "/persona public")
            return
        slept.assert_not_called()

    def test_accept_restore_open_failure_consumes_attempt_and_rejects_retried(self):
        module = load_dispatch_module("luca_dispatch_restore_open_retry")
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b"}]},
            {"error": module.DispatchError("accept request failed")},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-restore-retried")
        self.assertEqual(len(records), 4)
        self.assertEqual(len({item["conversation"] for item in records}), 4)

    def test_status_turn_key_shape_and_owned_fallback_are_fail_closed(self):
        module = load_dispatch_module("luca_dispatch_turn_key_shape")
        session_id = "12345678-1234-4234-8234-123456789abc"
        for value in (3, "hostile\nbytes", "x" * 257):
            with self.subTest(value=value):
                self._write_accept_status("public", turn_key=value)
                with self.assertRaises(module.DispatchError):
                    module._status(self.install / "state/status.json")
        self._write_accept_status("public", turn_key=session_id)
        status = module._status(self.install / "state/status.json")
        self.assertTrue(module._owned(status, session_id))
        self.assertFalse(module._owned(status, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
        self._write_accept_status("public", turn_key=None)
        self.assertFalse(module._owned(module._status(self.install / "state/status.json"), session_id))

    def test_status_read_is_bounded(self):
        module = load_dispatch_module("luca_dispatch_status_bounded")
        state = self.install / "state"
        state.mkdir(exist_ok=True)
        (state / "status.json").write_bytes(b"{" + b" " * module._STATUS_OUTPUT_LIMIT + b"}")
        with self.assertRaises(module.DispatchError):
            module._status(state / "status.json")

    def test_accept_http_error_closes_without_reading_hostile_body(self):
        module = load_dispatch_module("luca_dispatch_http_error_body")

        class HostileBody:
            def __init__(self):
                self.read_calls = 0
                self.closed = False

            def read(self, *_arguments):
                self.read_calls += 1
                raise AssertionError("HTTP error body was read")

            def close(self):
                self.closed = True

        body = HostileBody()
        error = urllib.error.HTTPError(
            "http://127.0.0.1:4321/v1/responses", 503, "hostile status", {}, body
        )
        opener = mock.MagicMock()
        opener.open.side_effect = error
        with mock.patch.object(module.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(module.DispatchError):
                module._open_accept_stream(
                    "fixture-secret",
                    4321,
                    "fixed-conversation",
                    "deployment warm-up",
                    module._time.monotonic() + 1,
                )
        self.assertTrue(body.closed)
        self.assertEqual(body.read_calls, 0)

    def test_accept_invalid_session_header_closes_response_without_body_read(self):
        module = load_dispatch_module("luca_dispatch_bad_session_close")
        response = HeldAcceptResponse()
        response.headers = mock.MagicMock()
        response.headers.get_all.return_value = ["hostile-header-value"]
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch.object(module.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(module.DispatchError):
                module._open_accept_stream(
                    "fixture-secret",
                    4321,
                    "fixed-conversation",
                    "deployment warm-up",
                    module._time.monotonic() + 1,
                )
        self.assertTrue(response.closed)
        self.assertEqual(response.close_calls, 1)
        self.assertEqual(response.read_calls, 0)

    def test_accept_http_error_emits_only_literal_reason_line(self):
        module = load_dispatch_module("luca_dispatch_http_error_literal")
        self._write_accept_status("public")
        self._write_accept_config()
        body = HeldAcceptResponse()
        error = urllib.error.HTTPError(
            "http://127.0.0.1:1/v1/responses",
            503,
            "private hostile status",
            {"X-Private": "private hostile header"},
            body,
        )
        opener = mock.MagicMock()
        opener.open.side_effect = error
        stderr = io.StringIO()
        stdout_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="utf-8")
        environment = {
            "SSH_ORIGINAL_COMMAND": "accept",
            "PGL_LUCA_DISPATCH_TESTING": "1",
            "PGL_LUCA_DISPATCH_TEST_ROOT": str(self.root),
        }
        with mock.patch.object(module.urllib.request, "build_opener", return_value=opener):
            with mock.patch.object(module.sys, "argv", [str(DISPATCH), "deploy"]):
                with mock.patch.object(module.os, "environ", environment):
                    with mock.patch.object(module.sys, "stderr", stderr):
                        with mock.patch.object(module.sys, "stdout", stdout):
                            self.assertEqual(module.main(), 1)
                            stdout.flush()
        self.assertEqual(stderr.getvalue(), reason_line("accept-warmup-failed"))
        self.assertEqual(stdout_bytes.getvalue(), b"")
        self.assertTrue(body.closed)
        self.assertEqual(body.read_calls, 0)

    def test_accept_wrong_mode_and_metadata_reports_switch_not_applied(self):
        module = load_dispatch_module("luca_dispatch_accept_not_applied")
        self._write_accept_status("public")
        config = self._write_accept_config()

        def send_turn(_token, _port, _conversation, utterance):
            if utterance == "/persona public":
                self._write_accept_status("public")
            return "11111111-1111-1111-1111-111111111111"

        with mock.patch.object(module, "_ACCEPT_POLL_INTERVAL_SECONDS", 0):
            with mock.patch.object(module, "_ACCEPT_SWITCH_WINDOW_SECONDS", 0):
                with self._patch_accept_open(module, send_turn):
                    with self.assertRaises(module.DispatchError) as caught:
                        module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-switch-not-applied")

    def test_accept_right_mode_wrong_metadata_reports_mismatch(self):
        module = load_dispatch_module("luca_dispatch_accept_metadata_mismatch")
        self._write_accept_status("public")
        config = self._write_accept_config()

        def send_turn(_token, _port, _conversation, utterance):
            if utterance == "/persona mode-b":
                self._write_accept_status(
                    "mode-b",
                    block_bytes=1,
                    block_sha256="0" * 64,
                )
            elif utterance == "/persona public":
                self._write_accept_status("public")
            return "11111111-1111-1111-1111-111111111111"

        with mock.patch.object(module, "_ACCEPT_POLL_INTERVAL_SECONDS", 0):
            with mock.patch.object(module, "_ACCEPT_SWITCH_WINDOW_SECONDS", 0):
                with self._patch_accept_open(module, send_turn):
                    with self.assertRaises(module.DispatchError) as caught:
                        module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(
            caught.exception.reason_code, "accept-switch-metadata-mismatch"
        )

    def test_accept_switch_non_timeout_failure_reports_request_failed(self):
        module = load_dispatch_module("luca_dispatch_accept_request_failed")
        self._write_accept_status("public")
        config = self._write_accept_config()

        def send_turn(_token, _port, _conversation, utterance):
            if utterance == "/persona mode-b":
                raise module.DispatchError("accept request failed")
            if utterance == "/persona public":
                self._write_accept_status("public")
            return "11111111-1111-1111-1111-111111111111"

        with self._patch_accept_open(module, send_turn):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-switch-request-failed")

    def test_accept_switch_timeout_reports_request_timeout(self):
        module = load_dispatch_module("luca_dispatch_accept_request_timeout")
        self._write_accept_status("public")
        config = self._write_accept_config()

        def send_turn(_token, _port, _conversation, utterance):
            if utterance == "/persona mode-b":
                raise module._AcceptRequestTimeout("accept request failed")
            if utterance == "/persona public":
                self._write_accept_status("public")
            return "11111111-1111-1111-1111-111111111111"

        with self._patch_accept_open(module, send_turn):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-switch-request-timeout")

    def test_accept_warmup_timeout_rejects_before_switch_turn(self):
        module = load_dispatch_module("luca_dispatch_accept_warmup_timeout")
        self._write_accept_status("public")
        self._write_accept_config()
        stderr = io.StringIO()
        stdout_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="utf-8")
        environment = {
            "SSH_ORIGINAL_COMMAND": "accept",
            "PGL_LUCA_DISPATCH_TESTING": "1",
            "PGL_LUCA_DISPATCH_TEST_ROOT": str(self.root),
        }
        with self._scripted_accept(
            module, [{"writes": [{"mode": "public", "owner": "foreign"}]}]
        ) as records:
            with mock.patch.object(module.sys, "argv", [str(DISPATCH), "deploy"]):
                with mock.patch.object(module.os, "environ", environment):
                    with mock.patch.object(module.sys, "stderr", stderr):
                        with mock.patch.object(module.sys, "stdout", stdout):
                            returncode = module.main()
                            stdout.flush()
        self.assertEqual(returncode, 1)
        self.assertEqual(stderr.getvalue(), reason_line("accept-warmup-timeout"))
        self.assertEqual(stdout_bytes.getvalue(), b"")
        self.assertEqual([item["utterance"] for item in records], ["deployment warm-up"])

    def test_accept_warmup_failure_rejects_before_switch_turn(self):
        module = load_dispatch_module("luca_dispatch_accept_warmup_failed")
        self._write_accept_status("public")
        self._write_accept_config()
        calls = []

        def send_turn(_token, _port, _conversation, utterance):
            calls.append(utterance)
            raise module.DispatchError("accept request failed")

        stderr = io.StringIO()
        stdout_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="utf-8")
        environment = {
            "SSH_ORIGINAL_COMMAND": "accept",
            "PGL_LUCA_DISPATCH_TESTING": "1",
            "PGL_LUCA_DISPATCH_TEST_ROOT": str(self.root),
        }
        with self._patch_accept_open(module, send_turn):
            with mock.patch.object(module.sys, "argv", [str(DISPATCH), "deploy"]):
                with mock.patch.object(module.os, "environ", environment):
                    with mock.patch.object(module.sys, "stderr", stderr):
                        with mock.patch.object(module.sys, "stdout", stdout):
                            returncode = module.main()
                            stdout.flush()
        self.assertEqual(returncode, 1)
        self.assertEqual(stderr.getvalue(), reason_line("accept-warmup-failed"))
        self.assertEqual(stdout_bytes.getvalue(), b"")
        self.assertEqual(calls, ["deployment warm-up"])

    def test_accept_timeout_budget_stays_below_ssh_cap(self):
        module = load_dispatch_module("luca_dispatch_accept_timeout_budget")
        from growthlane import deploy

        attempts = module._ACCEPT_RESTORE_RETRIES
        happy = (
            (2 + attempts) * module._ACCEPT_OPEN_TIMEOUT_SECONDS
            + module._ACCEPT_WARMUP_WINDOW_SECONDS
            + module._ACCEPT_SWITCH_WINDOW_SECONDS
            + attempts * module._ACCEPT_RESTORE_WINDOW_SECONDS
        )
        self.assertLessEqual(happy, module._ACCEPT_DEADLINE_SECONDS)
        self.assertLessEqual(
            happy + module._ACCEPT_DISCONNECT_HORIZON_SECONDS,
            module._ACCEPT_DEADLINE_SECONDS,
        )
        self.assertLessEqual(
            module._ACCEPT_DEADLINE_SECONDS + 25,
            deploy._ACCEPT_COMMAND_TIMEOUT_SECONDS,
        )
        self.assertGreaterEqual(
            module._ACCEPT_RESTORE_RESERVED_SECONDS,
            max(
                attempts
                * (
                    module._ACCEPT_OPEN_TIMEOUT_SECONDS
                    + module._ACCEPT_RESTORE_WINDOW_SECONDS
                ),
                module._ACCEPT_OPEN_TIMEOUT_SECONDS
                + module._ACCEPT_RESTORE_WINDOW_SECONDS
                + module._ACCEPT_DISCONNECT_HORIZON_SECONDS,
            ),
        )
        self.assertGreaterEqual(
            module._ACCEPT_DISCONNECT_HORIZON_SECONDS, 2 * 30 + 10
        )
        self.assertLessEqual(
            2 * module._ACCEPT_OPEN_TIMEOUT_SECONDS
            + module._ACCEPT_WARMUP_WINDOW_SECONDS
            + module._ACCEPT_SWITCH_WINDOW_SECONDS
            + module._ACCEPT_DISCONNECT_HORIZON_SECONDS,
            module._ACCEPT_DEADLINE_SECONDS,
        )

    def test_client_raises_only_accept_invocation_cap(self):
        from growthlane import deploy

        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(deploy.subprocess, "run", return_value=completed) as run:
            deploy._run_command(
                ("ssh", "-i", "/fixed/key", deploy.DEPLOY_TARGET, "accept"), self.root
            )
            deploy._run_command(
                (
                    "ssh",
                    "-i",
                    "/fixed/key",
                    deploy.DEPLOY_TARGET,
                    "accept",
                    "deletion",
                ),
                self.root,
            )
            deploy._run_command(
                (
                    "ssh",
                    "-i",
                    "/fixed/key",
                    deploy.DEPLOY_TARGET,
                    "deploy",
                    "restart",
                ),
                self.root,
            )
        self.assertEqual(
            [call.kwargs["timeout"] for call in run.call_args_list],
            [
                deploy._ACCEPT_COMMAND_TIMEOUT_SECONDS,
                deploy._ACCEPT_COMMAND_TIMEOUT_SECONDS,
                deploy._COMMAND_TIMEOUT_SECONDS,
            ],
        )
        self.assertEqual(deploy._ACCEPT_COMMAND_TIMEOUT_SECONDS, 380)
        self.assertEqual(deploy._COMMAND_TIMEOUT_SECONDS, 300)

    def test_open_accept_stream_clamps_timeout_and_closes_post_deadline_response(self):
        module = load_dispatch_module("luca_dispatch_open_deadline")
        response = HeldAcceptResponse()
        response.headers = mock.MagicMock()
        response.headers.get_all.return_value = [
            "12345678-1234-4234-8234-123456789abc"
        ]
        opener = mock.MagicMock()
        opener.open.return_value = response
        ticks = iter((10.0, 15.0))
        with mock.patch.object(module._time, "monotonic", side_effect=lambda: next(ticks)):
            with mock.patch.object(module.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(module._AcceptDeadlineExpired):
                    module._open_accept_stream(
                        "fixture-secret",
                        4321,
                        "fixed-conversation",
                        "deployment warm-up",
                        14.0,
                    )
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 4.0)
        self.assertTrue(response.closed)
        self.assertEqual(response.read_calls, 0)

    def test_accept_zero_deadline_skips_first_open_and_all_later_opens(self):
        module = load_dispatch_module("luca_dispatch_skip_open_deadline")
        self._write_accept_status("public")
        config = self._write_accept_config()
        with mock.patch.object(module, "_ACCEPT_DEADLINE_SECONDS", 0):
            with mock.patch.object(module.urllib.request, "build_opener") as build:
                with self.assertRaises(module.DispatchError) as caught:
                    module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-warmup-timeout")
        build.assert_not_called()

    def test_oracle_poll_sleep_is_clamped_exactly_to_deadline_boundary(self):
        module = load_dispatch_module("luca_dispatch_poll_boundary")
        self._write_accept_status("public", turn_key=None)
        clock = {"now": 0.0}
        sleeps = []

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds

        with mock.patch.object(module._time, "monotonic", side_effect=monotonic):
            with mock.patch.object(module._time, "sleep", side_effect=sleep):
                with mock.patch.object(module, "_ACCEPT_POLL_INTERVAL_SECONDS", 3):
                    with self.assertRaises(module._AcceptOracleTimeout):
                        module._await_owned_status(
                            self.install / "state/status.json",
                            "12345678-1234-4234-8234-123456789abc",
                            5.0,
                        )
        self.assertEqual(sleeps, [3, 2.0])
        self.assertEqual(clock["now"], 5.0)

    def test_accept_switch_failure_after_runtime_write_still_restores(self):
        module = load_dispatch_module("luca_dispatch_accept_presend_latch")
        self._write_accept_status("public")
        config = self._write_accept_config()
        calls = []

        def send_turn(_token, _port, _conversation, utterance):
            calls.append(utterance)
            if utterance == "/persona mode-b":
                self._write_accept_status("mode-b")
                raise module.DispatchError("accept request failed")
            if utterance == "/persona public":
                self._write_accept_status("public")
            return "11111111-1111-1111-1111-111111111111"

        with self._patch_accept_open(module, send_turn):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-switch-request-failed")
        self.assertEqual(
            calls,
            ["deployment warm-up", "/persona mode-b", "/persona public"],
        )
        self.assertEqual(
            json.loads((self.install / "state/status.json").read_text(encoding="utf-8"))[
                "mode"
            ],
            "public",
        )

    def test_accept_nonconvergent_restore_overrides_primary_switch_code(self):
        module = load_dispatch_module("luca_dispatch_accept_restore_precedence")
        self._write_accept_status("public")
        self._write_accept_config()
        calls = []

        def send_turn(_token, _port, _conversation, utterance):
            calls.append(utterance)
            if utterance == "/persona mode-b":
                self._write_accept_status("mode-b")
                raise module.DispatchError("accept request failed")
            if utterance == "/persona public":
                raise module.DispatchError("accept request failed")
            return "11111111-1111-1111-1111-111111111111"

        stderr = io.StringIO()
        environment = {
            "SSH_ORIGINAL_COMMAND": "accept",
            "PGL_LUCA_DISPATCH_TESTING": "1",
            "PGL_LUCA_DISPATCH_TEST_ROOT": str(self.root),
        }
        with self._patch_accept_open(module, send_turn):
            with mock.patch.object(module.sys, "argv", [str(DISPATCH), "deploy"]):
                with mock.patch.object(module.os, "environ", environment):
                    with mock.patch.object(module.sys, "stderr", stderr):
                        self.assertEqual(module.main(), 1)
        self.assertEqual(stderr.getvalue(), reason_line("accept-restore-failed"))
        self.assertEqual(calls.count("/persona public"), 2)

    def test_accept_primary_switch_code_survives_restore_retry(self):
        module = load_dispatch_module("luca_dispatch_accept_primary_survives")
        self._write_accept_status("public")
        config = self._write_accept_config()
        restore_attempts = 0

        def send_turn(_token, _port, _conversation, utterance):
            nonlocal restore_attempts
            if utterance == "/persona mode-b":
                raise module.DispatchError("accept request failed")
            if utterance == "/persona public":
                restore_attempts += 1
                if restore_attempts == 1:
                    raise module.DispatchError("accept request failed")
                self._write_accept_status("public")
            return "11111111-1111-1111-1111-111111111111"

        with self._patch_accept_open(module, send_turn):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-switch-request-failed")
        self.assertEqual(restore_attempts, 2)

    def test_accept_unreadable_switch_status_reports_switch_failed(self):
        module = load_dispatch_module("luca_dispatch_accept_status_unreadable")
        self._write_accept_status("public")
        config = self._write_accept_config()

        def send_turn(_token, _port, _conversation, utterance):
            if utterance == "/persona mode-b":
                (self.install / "state/status.json").unlink()
            elif utterance == "/persona public":
                self._write_accept_status("public")
            return "11111111-1111-1111-1111-111111111111"

        with self._patch_accept_open(module, send_turn):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(self.install, config, self.root / "backups", "standard")
        self.assertEqual(caught.exception.reason_code, "accept-switch-failed")

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
                "44444444-4444-4444-8444-444444444444",
            )
        )
        issued_session_ids = []

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
            session_id = next(session_ids)
            issued_session_ids.append(session_id)
            return session_id

        with self._patch_accept_open(module, send_turn):
            output = self._accept_with_restore_retry_oracle(
                module, config, self.root / "home/admin/.hermes/backups"
            )
        if output is not None:
            self.assertEqual(
                json.loads(output),
                [
                    "11111111-1111-1111-1111-111111111111",
                    "22222222-2222-2222-2222-222222222222",
                    "33333333-3333-3333-3333-333333333333",
                ],
            )
        else:
            self.assertEqual(len(issued_session_ids), 4)
            self.assertEqual(
                issued_session_ids[-1], "44444444-4444-4444-8444-444444444444"
            )

    def test_accept_deletion_rejects_render_file_set_increase(self):
        module = load_dispatch_module("luca_dispatch_deletion_increase")
        overlay = self.pack / "catalogs/overlay"
        overlay.mkdir(parents=True)
        (overlay / "adopted.txt").write_text("old\n", encoding="utf-8")
        expected = content_hash(self.pack)
        rebuilt = self.run_dispatch(f"deploy promote {expected}", role="deploy")
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        self.assertEqual(self.run_dispatch("deploy backup", role="deploy").returncode, 0)
        (overlay / "candidates.txt").write_text("new\n", encoding="utf-8")
        with self._scripted_accept(module, []) as records:
            with self.assertRaises(module.DispatchError):
                module._accept(
                    self.install,
                    self.root / "missing-config",
                    self.root / "home/admin/.hermes/backups",
                    "deletion",
                )
        self.assertEqual(records, [])

    def test_accept_deletion_allows_shrunk_render_file_set(self):
        module = load_dispatch_module("luca_dispatch_deletion_shrink")
        overlay = self.pack / "catalogs/overlay"
        overlay.mkdir(parents=True)
        (overlay / "old.txt").write_text("old\n", encoding="utf-8")
        (overlay / "drop.txt").write_text("drop\n", encoding="utf-8")
        expected = content_hash(self.pack)
        rebuilt = self.run_dispatch(f"deploy promote {expected}", role="deploy")
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        self.assertEqual(self.run_dispatch("deploy backup", role="deploy").returncode, 0)
        (overlay / "drop.txt").unlink()
        self._write_accept_status("public")
        config = self._write_accept_config()
        steps = [
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "mode-b"}]},
            {"writes": [{"mode": "public"}]},
            {"writes": [{"mode": "public"}]},
        ]
        with self._scripted_accept(module, steps) as records:
            output = self._accept_with_restore_retry_oracle(
                module,
                config,
                self.root / "home/admin/.hermes/backups",
                "deletion",
            )
        if output is not None:
            self.assertEqual(len(json.loads(output)), 3)
            self.assertEqual(len(records), 3)
        else:
            self.assertEqual(len(records), 4)
            self.assertEqual(records[-1]["utterance"], "/persona public")

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

        with self._patch_accept_open(module, send_turn):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(
                    self.install,
                    config,
                    self.root / "home/admin/.hermes/backups",
                    "standard",
                )
        self.assertIn("runtime mode unknown", str(caught.exception))
        self.assertEqual(caught.exception.reason_code, "accept-restore-failed")
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

        with self._patch_accept_open(module, send_turn):
            with self.assertRaises(module.DispatchError) as caught:
                module._accept(
                    self.install,
                    config,
                    self.root / "home/admin/.hermes/backups",
                    "standard",
                )
        self.assertEqual(str(caught.exception), "acceptance failed")
        self.assertEqual(caught.exception.reason_code, "accept-restore-retried")
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

    def test_dispatch_error_rejects_unknown_reason_code_at_construction(self):
        module = load_dispatch_module("luca_dispatch_reason_validation")
        with self.assertRaises(ValueError):
            module.DispatchError(reason_code="accept-typo-not-in-vocabulary")

    def test_main_ignores_reason_attributes_on_foreign_exceptions(self):
        module = load_dispatch_module("luca_dispatch_foreign_reason")

        class ForeignError(Exception):
            reason = "accept-switch-not-applied"
            reason_code = "accept-switch-not-applied"

        stderr = io.StringIO()
        with mock.patch.object(module, "_parse_request", side_effect=ForeignError()):
            with mock.patch.object(module.sys, "stderr", stderr):
                self.assertEqual(module.main(), 1)
        self.assertEqual(stderr.getvalue(), ERROR_LINE)

    def test_main_rejects_str_subclass_reason_code_at_emission(self):
        module = load_dispatch_module("luca_dispatch_str_subclass_reason")

        class ReasonCode(str):
            pass

        exception = module.DispatchError(
            reason_code=ReasonCode("accept-switch-not-applied")
        )
        stderr = io.StringIO()
        with mock.patch.object(module, "_parse_request", side_effect=exception):
            with mock.patch.object(module.sys, "stderr", stderr):
                self.assertEqual(module.main(), 1)
        self.assertEqual(stderr.getvalue(), ERROR_LINE)

    def test_every_emitted_failure_is_one_literal_line_and_stdout_is_empty(self):
        module = load_dispatch_module("luca_dispatch_literal_emission")
        allowed_lines = {ERROR_LINE, *module._REASON_LINES.values()}

        class ForeignError(Exception):
            reason_code = "restart-command-failed"

        failures = [ForeignError(), module.DispatchError()]
        failures.extend(
            module.DispatchError(reason_code=code) for code in module._REASON_CODES
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__, reason=getattr(failure, "reason_code", None)):
                stderr = io.StringIO()
                stdout_bytes = io.BytesIO()
                stdout = io.TextIOWrapper(stdout_bytes, encoding="utf-8")
                with mock.patch.object(module, "_parse_request", side_effect=failure):
                    with mock.patch.object(module.sys, "stderr", stderr):
                        with mock.patch.object(module.sys, "stdout", stdout):
                            self.assertEqual(module.main(), 1)
                            stdout.flush()
                self.assertIn(stderr.getvalue(), allowed_lines)
                self.assertEqual(stderr.getvalue().count("\n"), 1)
                self.assertEqual(stdout_bytes.getvalue(), b"")

    def test_reason_vocabulary_has_closed_safe_shape(self):
        module = load_dispatch_module("luca_dispatch_reason_vocabulary")
        self.assertEqual(len(module._REASON_CODES), 13)
        self.assertEqual(len(module._REASON_LINES), 13)
        self.assertEqual(
            module._REASON_CODES,
            {
                "accept-warmup-timeout",
                "accept-warmup-failed",
                "accept-switch-request-failed",
                "accept-switch-request-timeout",
                "accept-switch-not-applied",
                "accept-switch-metadata-mismatch",
                "accept-switch-failed",
                "accept-restore-failed",
                "accept-restore-retried",
                "restart-command-failed",
                "restart-command-timeout",
                "restart-units-not-active",
                "restart-verification-failed",
            },
        )
        self.assertEqual(module._REASON_LINES.keys(), module._REASON_CODES)
        for code in module._REASON_CODES:
            self.assertIsNotNone(re.fullmatch(r"[a-z][a-z0-9-]{1,40}", code))
            self.assertTrue(code.isascii())
        for line in module._REASON_LINES.values():
            self.assertTrue(line.endswith("\n"))
            self.assertEqual(line.count("\n"), 1)
            self.assertNotIn("\r", line)

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
