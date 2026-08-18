from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from applier import apply as apply_module
from applier.apply import FaceResult
from growthlane import nightly
from growthlane.faces import get_profile
from growthlane.ledger import dump_ledger, empty_ledger


HASH = "a" * 64


class _Profile:
    name = "luca"
    ledger_path = "growth/overlay-ledger.yml"
    allowlist = ("growth/overlay-ledger.yml",)

    def __init__(self, home: Path, staging: Path) -> None:
        self.home = home
        self.staging = staging

    def resolve_home(self, _pgl_home: Path, _config: object) -> Path:
        return self.home

    def resolve_staging_root(self, _config: object) -> Path:
        return self.staging


class LucaNightlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "luca-repo"
        self.staging = self.root / "luca-staging"
        self.pgl_home = self.root / "pgl-home"
        self.profile = _Profile(self.home, self.staging)
        self.config = {"staging_root": str(self.staging)}
        self.previous_home = os.environ.get("PGL_HOME")
        os.environ["PGL_HOME"] = str(self.pgl_home)

    def tearDown(self) -> None:
        if self.previous_home is None:
            os.environ.pop("PGL_HOME", None)
        else:
            os.environ["PGL_HOME"] = self.previous_home
        self.temporary.cleanup()

    def _git(self, events: list[str]):
        def fake(home: Path, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if args == ("pull", "--ff-only"):
                events.append("pull")
                output = b""
            elif args == ("rev-parse", "--abbrev-ref", "@{upstream}"):
                output = b"origin/main\n"
            elif args == ("rev-list", "--count", "origin/main..HEAD"):
                output = b"0\n"
            elif args == ("show", "HEAD:growth/overlay-ledger.yml"):
                output = b"face: luca\nsnapshots: []\n"
            elif args == ("rev-parse", "HEAD"):
                output = b"1111111111111111111111111111111111111111\n"
            elif args[:2] == ("commit", "--only"):
                events.append("commit")
                output = b""
            elif args == ("branch", "--show-current"):
                output = b"main\n"
            else:
                output = b""
            return subprocess.CompletedProcess(["git", *args], 0, output, b"")

        return fake

    def _run(
        self,
        events: list[str],
        run_face: object,
        *,
        reconciliation: object | None = None,
        lifecycle: object | None = None,
        parity: object | None = None,
        weekly_parity: str = "GREEN",
        git_side_effect: object | None = None,
        tripwire_side_effect: object | None = None,
        real_lifecycle: bool = False,
        invoke_before_commit: bool = True,
        path_side_effect: object | None = None,
        real_weekly_marker: bool = False,
    ) -> int:
        green = SimpleNamespace(
            status="GREEN",
            committed_hash=HASH,
            detail="green",
            expected_source="ledger",
        )
        lifecycle = lifecycle or SimpleNamespace(
            deploy_started=False,
            acceptance_succeeded=False,
            commit_completed=False,
            resume_required=False,
        )
        parity = parity or SimpleNamespace(status="GREEN", detail="green")
        selected_reconciliation = reconciliation or green

        def reconcile(*_args: object, **kwargs: object) -> object:
            self.assertEqual(kwargs, {"pgl_home": self.pgl_home})
            events.append("g0")
            return selected_reconciliation

        if callable(run_face):
            run_face_effect = run_face
        else:
            def run_face_effect(*_args: object, **kwargs: object) -> object:
                if run_face.changed and invoke_before_commit:
                    kwargs["before_commit"](run_face.content_hash)
                return run_face
        run_face_patch = mock.patch.object(
            nightly.applier_apply,
            "run_face",
            side_effect=run_face_effect,
        )
        lifecycle_patch = (
            nullcontext()
            if real_lifecycle
            else mock.patch.object(
                nightly.luca_deploy,
                "load_lifecycle_state",
                return_value=lifecycle,
            )
        )
        weekly_patch = (
            nullcontext()
            if real_weekly_marker
            else mock.patch.object(
                nightly,
                "_weekly_luca_parity",
                side_effect=lambda *_args: events.append("weekly") or weekly_parity,
            )
        )
        with mock.patch.object(nightly, "get_profile", return_value=self.profile), mock.patch.object(
            nightly, "load_json_object", return_value=self.config
        ), mock.patch.object(
            nightly, "parse_scalar_yaml", return_value=SimpleNamespace(deviations=())
        ), mock.patch.object(
            nightly, "emit_admission_refusal", return_value=None
        ), mock.patch.object(
            nightly, "inspect", side_effect=tripwire_side_effect
        ), mock.patch.object(
            nightly.applier_apply, "_git", side_effect=git_side_effect or self._git(events)
        ), mock.patch.object(
            nightly.luca_deploy, "reconcile_production", side_effect=reconcile
        ) as reconcile_mock, lifecycle_patch, mock.patch.object(
            nightly.luca_deploy,
            "require_collector_path_agreement",
            side_effect=(
                path_side_effect
                if path_side_effect is not None
                else lambda *_args: (
                    self.pgl_home,
                    self.pgl_home / "state/luca-verify-sessions.jsonl",
                )
            ),
        ), mock.patch.object(
            nightly, "check_all", side_effect=lambda *_args: events.append("a-a2")
        ), weekly_patch, mock.patch.object(
            nightly, "luca_parity", side_effect=lambda *_args: events.append("a3") or parity
        ), mock.patch.object(nightly, "acquire_lock", return_value=self.root / "lock"), mock.patch.object(
            nightly, "release_lock", return_value=True
        ), run_face_patch:
            result = nightly.luca_main(["--date", "2026-08-10"])
        return result

    def test_tripwire_failure_is_additional_to_the_primary_stop(self) -> None:
        events: list[str] = []
        stop = SimpleNamespace(status="RED", committed_hash=None, detail="reconciliation mismatch")
        self.assertEqual(
            self._run(
                events,
                FaceResult(False, None, HASH),
                reconciliation=stop,
                tripwire_side_effect=RuntimeError("tripwire unavailable"),
            ),
            1,
        )
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(encoding="utf-8")
        self.assertIn("g0 reconciliation RED", digest)
        self.assertIn("additionally tripwire failed", digest)

    def test_pull_g0_shared_gates_and_a3_precede_exact_e2_commit(self) -> None:
        events: list[str] = []

        def run_face(*_args: object, before_commit: object) -> FaceResult:
            before_commit(HASH)
            nightly.applier_apply._git(
                self.home,
                "commit",
                "--only",
                "-m",
                f"Content-Hash: {HASH}\n",
                "--",
                "growth/overlay-ledger.yml",
            )
            return FaceResult(True, "overlay-snap-luca-1", HASH)

        def deploy(**_kwargs: object) -> object:
            self.assertEqual(
                _kwargs["known_production_install"],
                self.home / "tests/luca-pack/install.yml",
            )
            events.append("e2")
            return object()

        with mock.patch.object(nightly.luca_deploy, "deploy_and_accept", side_effect=deploy), mock.patch.object(
            nightly.luca_deploy, "record_commit_completed", side_effect=lambda *_args, **_kwargs: events.append("journal")
        ), mock.patch.object(
            nightly.luca_deploy, "write_production_anchor", side_effect=lambda *_args: events.append("anchor")
        ), mock.patch.object(nightly, "_push_luca_commit", side_effect=lambda *_args: events.append("push")):
            self.assertEqual(self._run(events, run_face), 0)
        self.assertEqual(events, ["pull", "g0", "a-a2", "weekly", "a3", "e2", "commit", "journal", "anchor", "push"])

    def test_changed_without_e2_rolls_back_mutant_commit_and_tag(self) -> None:
        events: list[str] = []
        old_head = "1" * 40
        new_head = "2" * 40
        state = {"head": old_head, "tags": {"existing-tag"}}

        def git(_home: Path, *args: str, **_kwargs: object):
            output = b""
            if args == ("pull", "--ff-only"):
                events.append("pull")
            elif args == ("rev-parse", "--abbrev-ref", "@{upstream}"):
                output = b"origin/main\n"
            elif args == ("rev-list", "--count", "origin/main..HEAD"):
                output = b"0\n"
            elif args == ("show", "HEAD:growth/overlay-ledger.yml"):
                output = b"face: luca\nsnapshots: []\n"
            elif args == ("rev-parse", "HEAD"):
                output = f"{state['head']}\n".encode()
            elif args == ("tag", "--list"):
                output = ("\n".join(sorted(state["tags"])) + "\n").encode()
            elif args[:2] == ("tag", "-d"):
                state["tags"].discard(args[2])
            elif args[:2] == ("reset", "--soft"):
                state["head"] = args[2]
            elif args[:2] == ("checkout", old_head):
                events.append("allowlist-restored")
            return subprocess.CompletedProcess(["git", *args], 0, output, b"")

        def mutant_run_face(*_args: object, **_kwargs: object) -> FaceResult:
            state["head"] = new_head
            state["tags"].add("hidden-mutant-tag")
            return FaceResult(True, None, HASH)

        with mock.patch.object(
            nightly.luca_deploy, "record_commit_completed"
        ) as completed, mock.patch.object(
            nightly.luca_deploy, "write_production_anchor"
        ) as anchor, mock.patch.object(nightly, "_push_luca_commit") as push:
            self.assertEqual(
                self._run(events, mutant_run_face, git_side_effect=git),
                1,
            )
        self.assertEqual(state, {"head": old_head, "tags": {"existing-tag"}})
        self.assertIn("allowlist-restored", events)
        digest = (self.pgl_home / "digest/2026-08-10.md").read_text(encoding="utf-8")
        self.assertIn("[RED]", digest)
        self.assertIn("changed commit returned without a performed e2", digest)
        completed.assert_not_called()
        anchor.assert_not_called()
        push.assert_not_called()

    def test_changed_without_e2_status_verification_failure_is_not_clean(self) -> None:
        old_head = "1" * 40
        new_head = "2" * 40
        state = {"head": old_head, "tags": set()}

        def git(_home: Path, *args: str, **kwargs: object):
            outputs = {
                ("pull", "--ff-only"): b"",
                ("rev-parse", "--abbrev-ref", "@{upstream}"): b"origin/main\n",
                ("rev-list", "--count", "origin/main..HEAD"): b"0\n",
                ("show", "HEAD:growth/overlay-ledger.yml"): b"face: luca\nsnapshots: []\n",
            }
            output = outputs.get(args, b"")
            if args == ("rev-parse", "HEAD"):
                output = f"{state['head']}\n".encode()
            elif args == ("tag", "--list"):
                output = ("\n".join(sorted(state["tags"])) + "\n").encode()
            elif args[:2] == ("tag", "-d"):
                state["tags"].discard(args[2])
            elif args[:2] == ("reset", "--soft"):
                state["head"] = args[2]
            elif args[:2] == ("status", "--porcelain"):
                if kwargs.get("check", True):
                    raise RuntimeError("checked status verification failed")
                return subprocess.CompletedProcess(["git", *args], 1, b"", b"failed")
            return subprocess.CompletedProcess(["git", *args], 0, output, b"")

        def mutant_run_face(*_args: object, **_kwargs: object) -> FaceResult:
            state["head"] = new_head
            state["tags"].add("hidden-mutant-tag")
            return FaceResult(True, "wrong-reported-tag", HASH)

        self.assertEqual(self._run([], mutant_run_face, git_side_effect=git), 1)
        digest = (self.pgl_home / "digest/2026-08-10.md").read_text(encoding="utf-8")
        self.assertIn("checked status verification failed", digest)

    def test_changed_without_e2_reports_each_post_rollback_residue(self) -> None:
        old_head = "1" * 40
        new_head = "2" * 40
        source_tags = {"existing-tag"}

        for residue in ("head", "dirty", "tags"):
            with self.subTest(residue=residue):
                digest_path = self.pgl_home / "digest/2026-08-10.md"
                digest_path.unlink(missing_ok=True)
                state = {"head": old_head, "tags": set(source_tags)}

                def git(_home: Path, *args: str, **_kwargs: object):
                    output = b""
                    if args == ("pull", "--ff-only"):
                        pass
                    elif args == ("rev-parse", "--abbrev-ref", "@{upstream}"):
                        output = b"origin/main\n"
                    elif args == ("rev-list", "--count", "origin/main..HEAD"):
                        output = b"0\n"
                    elif args == ("show", "HEAD:growth/overlay-ledger.yml"):
                        output = b"face: luca\nsnapshots: []\n"
                    elif args == ("rev-parse", "HEAD"):
                        output = f"{state['head']}\n".encode()
                    elif args == ("tag", "--list"):
                        output = ("\n".join(sorted(state["tags"])) + "\n").encode()
                    elif args[:2] == ("tag", "-d"):
                        if residue != "tags":
                            state["tags"].discard(args[2])
                    elif args[:2] == ("reset", "--soft"):
                        if residue != "head":
                            state["head"] = args[2]
                    elif args[:2] == ("status", "--porcelain"):
                        output = b" M growth/overlay-ledger.yml\n" if residue == "dirty" else b""
                    return subprocess.CompletedProcess(["git", *args], 0, output, b"")

                def mutant_run_face(*_args: object, **_kwargs: object) -> FaceResult:
                    state["head"] = new_head
                    state["tags"].add("mutant-tag")
                    return FaceResult(True, "mutant-tag", HASH)

                self.assertEqual(
                    self._run([], mutant_run_face, git_side_effect=git),
                    1,
                )
                digest = digest_path.read_text(encoding="utf-8")
                self.assertIn(
                    "rollback could not prove a clean source HEAD",
                    digest,
                )

    def test_luca_deploy_arguments_keep_collector_paths_in_their_destinations(self) -> None:
        collector_config = self.root / "obs-collector-luca.json"
        custom_obs_root = self.root / "collector-observations"
        custom_ledger_path = self.root / "collector-ledger.jsonl"

        arguments = nightly._luca_deploy_arguments(
            self.profile,
            self.config,
            self.pgl_home,
            self.home,
            collector_config,
            custom_obs_root,
            custom_ledger_path,
            HASH,
        )

        self.assertEqual(arguments["obs_root"], custom_obs_root)
        self.assertEqual(arguments["ledger_path"], custom_ledger_path)
        self.assertNotEqual(arguments["obs_root"], arguments["ledger_path"])

    def test_luca_main_real_run_face_calls_e2_before_stub_git_commit(self) -> None:
        profile = get_profile("luca")
        overlay = self.root / "real-luca-overlay"
        staging = self.root / "real-luca-staging"
        for relative in ("persona-engine/catalogs/overlay", "growth"):
            (overlay / relative).mkdir(parents=True, exist_ok=True)
        (overlay / ".git").mkdir()
        (overlay / profile.ledger_path).write_bytes(dump_ledger(empty_ledger("luca")))
        (overlay / profile.blocklist_path).write_bytes(b"")
        for relative in profile.render_files.values():
            (overlay / relative).write_bytes(b"")
        (overlay / "persona-engine/manifest.yml").write_text("name: luca\n", encoding="utf-8")
        staging.mkdir()
        (staging / "install.yml").write_text("schema_version: 2\n", encoding="utf-8")
        config = {
            "overlay_home_root": str(overlay),
            "staging_root": str(staging),
            "display_name": "オーナー",
            "classifier_argv": [],
            "writer_argv": [],
            "reviewer_argv": [],
        }
        timeline: list[str] = []
        baseline_head = "1" * 40
        committed_head = "2" * 40
        git_state = {"head": baseline_head, "tags": set()}

        def git(_home: Path, *args: str, **_kwargs: object):
            returncode = 0
            output = b""
            if args == ("pull", "--ff-only"):
                timeline.append("pull")
            elif args == ("rev-parse", "--abbrev-ref", "@{upstream}"):
                output = b"origin/main\n"
            elif args == ("rev-list", "--count", "origin/main..HEAD"):
                output = b"0\n"
            elif args == ("show", f"HEAD:{profile.ledger_path}"):
                output = (overlay / profile.ledger_path).read_bytes()
            elif args == ("rev-parse", "HEAD"):
                output = f"{git_state['head']}\n".encode()
            elif args[:2] == ("status", "--porcelain"):
                output = b""
            elif args[:2] == ("diff", "--quiet"):
                returncode = 1
            elif args[:2] == ("tag", "--list"):
                output = b""
            elif args[:2] == ("diff", "--name-only"):
                output = f"{profile.ledger_path}\n".encode()
            elif args[:2] == ("add", "--"):
                timeline.append("git-add")
            elif args and args[0] == "commit":
                self.assertEqual(git_state["head"], baseline_head)
                self.assertIn("e2", timeline)
                git_state["head"] = committed_head
                timeline.append("git-commit")
            elif args[:3] == ("show", "--name-only", "--pretty=format:"):
                output = f"{profile.ledger_path}\n".encode()
            elif args[:2] == ("checkout", "HEAD"):
                timeline.append("git-checkout")
            elif args and args[0] == "tag":
                git_state["tags"].add(args[1])
                timeline.append("git-tag")
            return subprocess.CompletedProcess(["git", *args], returncode, output, b"")

        def persona(command: str, clone: Path, install_root: Path):
            self.assertEqual(install_root, staging)
            timeline.append(f"persona-{command}")
            if command == "build":
                (staging / "build").mkdir()
                (staging / "build/manifest.json").write_text(
                    json.dumps({"content_hash": HASH}),
                    encoding="utf-8",
                )
                stdout = b""
            else:
                stdout = b'{"ok":true,"issues":[]}\n'
            return subprocess.CompletedProcess(
                ["node", str(clone / "packages/core/bin/persona"), command],
                0,
                stdout,
                b"",
            )

        def deploy(*, content_hash: str, attempt: object, **_kwargs: object) -> object:
            self.assertEqual(content_hash, HASH)
            self.assertEqual(git_state["head"], baseline_head)
            self.assertEqual(timeline[-1], "git-add")
            self.assertNotIn("git-commit", timeline)
            attempt.deploy_started = True
            attempt.backup_taken = True
            timeline.append("e2")
            return object()

        candidate = {
            "phrase_id": "p-0001",
            "text": "なるほどだね",
            "source": {
                "first_seen": "2026-08-10",
                "window_count": 1,
                "distinct_days": 1,
                "echo_ratio": 0.0,
            },
            "proposal_id": "candidate-p-0001",
        }
        clean_lifecycle = SimpleNamespace(
            deploy_started=False,
            acceptance_succeeded=False,
            commit_completed=False,
            resume_required=False,
        )
        green_reconciliation = SimpleNamespace(
            status="GREEN",
            committed_hash=HASH,
            detail="green",
            expected_source="ledger",
        )
        green_parity = SimpleNamespace(status="GREEN", detail="green")
        patches = (
            mock.patch.object(apply_module, "_git", side_effect=git),
            mock.patch.object(apply_module, "_run_persona", side_effect=persona),
            mock.patch.object(apply_module, "check_all"),
            mock.patch.object(apply_module, "verify_manifest"),
            mock.patch.object(
                apply_module,
                "runtime_status",
                return_value=SimpleNamespace(drifted=False),
            ),
            mock.patch.object(apply_module, "transcript_inputs", return_value=([], None)),
            mock.patch.object(apply_module, "harvest", return_value=[candidate]),
            mock.patch.object(
                apply_module,
                "aggregate",
                side_effect=lambda *_args, **_kwargs: (_args[3], [], 0),
            ),
            mock.patch.object(nightly, "get_profile", return_value=profile),
            mock.patch.object(nightly, "load_json_object", return_value=config),
            mock.patch.object(
                nightly,
                "parse_scalar_yaml",
                return_value=SimpleNamespace(deviations=()),
            ),
            mock.patch.object(nightly, "emit_admission_refusal", return_value=None),
            mock.patch.object(nightly, "check_all"),
            mock.patch.object(
                nightly.luca_deploy,
                "require_collector_path_agreement",
                return_value=(
                    self.pgl_home,
                    self.pgl_home / "state/luca-verify-sessions.jsonl",
                ),
            ),
            mock.patch.object(
                nightly.luca_deploy,
                "load_lifecycle_state",
                return_value=clean_lifecycle,
            ),
            mock.patch.object(
                nightly.luca_deploy,
                "reconcile_production",
                return_value=green_reconciliation,
            ),
            mock.patch.object(nightly, "_weekly_luca_parity", return_value="GREEN"),
            mock.patch.object(nightly, "luca_parity", return_value=green_parity),
            mock.patch.object(nightly.luca_deploy, "deploy_and_accept", side_effect=deploy),
            mock.patch.object(
                nightly.luca_deploy,
                "record_commit_completed",
                side_effect=lambda *_args, **_kwargs: timeline.append("journal"),
            ),
            mock.patch.object(
                nightly.luca_deploy,
                "write_production_anchor",
                side_effect=lambda *_args: timeline.append("anchor"),
            ),
            mock.patch.object(
                nightly,
                "_push_luca_commit",
                side_effect=lambda *_args: timeline.append("push"),
            ),
            mock.patch.object(nightly, "inspect"),
            mock.patch.object(nightly, "acquire_lock", return_value=self.root / "lock"),
            mock.patch.object(nightly, "release_lock", return_value=True),
        )
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            self.assertEqual(nightly.luca_main(["--date", "2026-08-10"]), 0)
        self.assertEqual(git_state["head"], committed_head)
        self.assertEqual(len(git_state["tags"]), 1)
        self.assertEqual(
            timeline,
            [
                "pull",
                "persona-build",
                "persona-doctor",
                "git-add",
                "e2",
                "git-commit",
                "git-checkout",
                "git-tag",
                "journal",
                "anchor",
                "push",
            ],
        )

    def test_first_run_without_intent_journal_reaches_no_diff_gates(self) -> None:
        events: list[str] = []
        self.assertFalse(
            (self.pgl_home / "state" / "luca-intent-journal.jsonl").exists()
        )
        self.assertEqual(
            self._run(
                events,
                FaceResult(False, None, HASH),
                real_lifecycle=True,
            ),
            0,
        )
        self.assertEqual(events, ["pull", "g0", "a-a2", "weekly", "a3"])

    def test_no_diff_runs_g0_and_a3_but_never_deploys_or_pushes(self) -> None:
        events: list[str] = []
        result = FaceResult(False, None, HASH)
        with mock.patch.object(nightly.luca_deploy, "deploy_and_accept") as deploy, mock.patch.object(
            nightly, "_push_luca_commit"
        ) as push:
            self.assertEqual(self._run(events, result), 0)
        self.assertEqual(events, ["pull", "g0", "a-a2", "weekly", "a3"])
        deploy.assert_not_called()
        push.assert_not_called()

    def test_anchor_based_green_emits_reconciliation_detail_once(self) -> None:
        events: list[str] = []
        detail = "production digest matches expected from production anchor (no committed snapshot)"
        reconciliation = SimpleNamespace(
            status="GREEN",
            committed_hash=HASH,
            detail=detail,
            expected_source="anchor",
        )

        self.assertEqual(
            self._run(events, FaceResult(False, None, HASH), reconciliation=reconciliation),
            0,
        )

        self.assertEqual(events, ["pull", "g0", "a-a2", "weekly", "a3"])
        digest_lines = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(digest_lines, [f"- luca: g0 {detail}"])

    def test_ledger_based_green_emits_no_reconciliation_digest_line(self) -> None:
        events: list[str] = []
        reconciliation = SimpleNamespace(
            status="GREEN",
            committed_hash=HASH,
            detail="production digest matches committed Luca snapshot",
            expected_source="ledger",
        )

        self.assertEqual(
            self._run(events, FaceResult(False, None, HASH), reconciliation=reconciliation),
            0,
        )

        self.assertEqual(events, ["pull", "g0", "a-a2", "weekly", "a3"])
        digest_lines = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(digest_lines, ["- nightly: no changes"])

    def test_g0_mismatch_and_unavailable_are_red_stops(self) -> None:
        for status in ("RED", "UNAVAILABLE"):
            with self.subTest(status=status):
                events: list[str] = []
                stop = SimpleNamespace(status=status, committed_hash=None, detail="bad")
                with mock.patch.object(nightly.applier_apply, "run_face") as run_face:
                    self.assertEqual(self._run(events, FaceResult(False, None, HASH), reconciliation=stop), 1)
                self.assertEqual(events, ["pull", "g0"])
                run_face.assert_not_called()

    def test_a3_requires_real_weekly_marker_and_live_parity_green(self) -> None:
        marker_path = self.pgl_home / "reports/weekly/latest-luca.json"
        marker_path.parent.mkdir(parents=True)
        for marker, live_status, expected, expected_tail in (
            ({"parity": "GREEN"}, "GREEN", 0, ["a3"]),
            (None, "GREEN", 1, []),
            ({"generated_at": "2026-08-10"}, "GREEN", 1, []),
            ({"parity": "RED"}, "GREEN", 1, ["a3"]),
            ({"parity": "GREEN"}, "UNAVAILABLE", 1, ["a3"]),
        ):
            with self.subTest(marker=marker, live=live_status):
                marker_path.unlink(missing_ok=True)
                if marker is not None:
                    marker_path.write_text(json.dumps(marker), encoding="utf-8")
                events: list[str] = []
                parity = SimpleNamespace(status=live_status, detail="live parity")
                self.assertEqual(
                    self._run(
                        events,
                        FaceResult(False, None, HASH),
                        parity=parity,
                        real_weekly_marker=True,
                    ),
                    expected,
                )
                self.assertEqual(events, ["pull", "g0", "a-a2", *expected_tail])

    def test_collector_path_disagreement_stops_before_g0_and_e2(self) -> None:
        events: list[str] = []
        with mock.patch.object(nightly.applier_apply, "run_face") as run_face:
            self.assertEqual(
                self._run(
                    events,
                    FaceResult(False, None, HASH),
                    path_side_effect=RuntimeError("collector journal paths disagree"),
                ),
                1,
            )
        self.assertEqual(events, ["pull"])
        run_face.assert_not_called()
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("collector journal paths disagree", digest)

    def test_pull_failure_stops_before_g0_and_upstream_ahead_requires_manual_push(self) -> None:
        def pull_failure(_home: Path, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if args == ("pull", "--ff-only"):
                raise RuntimeError("pull failed")
            raise AssertionError(f"unexpected git call: {args}")

        events: list[str] = []
        self.assertEqual(
            self._run(events, FaceResult(False, None, HASH), git_side_effect=pull_failure), 1
        )
        self.assertEqual(events, [])

        def ahead(_home: Path, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            outputs = {
                ("pull", "--ff-only"): b"",
                ("rev-parse", "--abbrev-ref", "@{upstream}"): b"origin/main\n",
                ("rev-list", "--count", "origin/main..HEAD"): b"1\n",
            }
            return subprocess.CompletedProcess(["git", *args], 0, outputs[args], b"")

        self.assertEqual(
            self._run([], FaceResult(False, None, HASH), git_side_effect=ahead), 1
        )
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(encoding="utf-8")
        self.assertIn("manual push recovery required", digest)

    def test_incomplete_deploy_resume_stops_without_taking_another_backup(self) -> None:
        lifecycle = SimpleNamespace(
            deploy_started=True,
            acceptance_succeeded=False,
            commit_completed=False,
            resume_required=True,
        )
        with mock.patch.object(nightly.applier_apply, "run_face") as run_face:
            self.assertEqual(
                self._run([], FaceResult(False, None, HASH), lifecycle=lifecycle), 1
            )
        run_face.assert_not_called()
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("incomplete deploy resume detected", digest)
        self.assertIn("do not take another backup", digest)
        self.assertNotIn("acceptance-succeeded has no later commit-completed", digest)

    def test_acceptance_precommit_crash_is_self_identified_as_row_6(self) -> None:
        lifecycle = SimpleNamespace(
            deploy_started=True,
            acceptance_succeeded=True,
            commit_completed=False,
            resume_required=True,
        )
        with mock.patch.object(nightly.applier_apply, "run_face") as run_face:
            self.assertEqual(
                self._run([], FaceResult(False, None, HASH), lifecycle=lifecycle), 1
            )
        run_face.assert_not_called()
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Luca self-crash", digest)
        self.assertIn("acceptance-succeeded has no later commit-completed", digest)
        self.assertNotIn("incomplete deploy resume detected", digest)

    def test_row_6_lifecycle_precedes_ahead_and_g0_red(self) -> None:
        lifecycle = SimpleNamespace(
            deploy_started=True,
            acceptance_succeeded=True,
            commit_completed=False,
            resume_required=True,
        )
        stop = SimpleNamespace(status="RED", committed_hash=None, detail="g0 mismatch")
        events: list[str] = []
        self.assertEqual(
            self._run(
                events,
                FaceResult(False, None, HASH),
                lifecycle=lifecycle,
                reconciliation=stop,
            ),
            1,
        )
        self.assertEqual(events, ["pull"])
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Luca self-crash", digest)
        self.assertNotIn("g0 reconciliation", digest)

        def ahead(_home: Path, *args: str, **_kwargs: object):
            outputs = {
                ("pull", "--ff-only"): b"",
                ("rev-parse", "--abbrev-ref", "@{upstream}"): b"origin/main\n",
                ("rev-list", "--count", "origin/main..HEAD"): b"1\n",
            }
            return subprocess.CompletedProcess(["git", *args], 0, outputs[args], b"")

        events.clear()
        self.assertEqual(
            self._run(
                events,
                FaceResult(False, None, HASH),
                lifecycle=lifecycle,
                git_side_effect=ahead,
            ),
            1,
        )
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Luca self-crash", digest)
        self.assertNotIn("manual push recovery required", digest)

    def test_completed_new_lifecycle_defers_to_g0_message(self) -> None:
        lifecycle = SimpleNamespace(
            deploy_started=True,
            acceptance_succeeded=True,
            commit_completed=True,
            resume_required=False,
        )
        stop = SimpleNamespace(status="RED", committed_hash=None, detail="g0 mismatch")
        events: list[str] = []
        self.assertEqual(
            self._run(
                events,
                FaceResult(False, None, HASH),
                lifecycle=lifecycle,
                reconciliation=stop,
            ),
            1,
        )
        self.assertEqual(events, ["pull", "g0"])
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("g0 reconciliation RED", digest)
        self.assertNotIn("Luca self-crash", digest)

    def test_e2_failure_reverts_then_rebuilds_and_recovers_even_if_rebuild_fails(self) -> None:
        events: list[str] = []

        def run_face(*_args: object, before_commit: object) -> FaceResult:
            try:
                before_commit(HASH)
            except Exception:
                events.append("outer-revert")
                raise
            raise AssertionError("e2 failure must prevent the real commit")

        def deploy(*, attempt: object, **_kwargs: object) -> object:
            events.append("e2")
            attempt.backup_taken = True
            raise RuntimeError("acceptance failed")

        with mock.patch.object(nightly.luca_deploy, "deploy_and_accept", side_effect=deploy), mock.patch(
            "mirror.staging.regenerate_staging", side_effect=lambda *_args: events.append("rebuild")
        ), mock.patch.object(
            nightly.luca_deploy, "recover_production", side_effect=lambda **_kwargs: events.append("recover")
        ):
            self.assertEqual(self._run(events, run_face), 1)
        self.assertEqual(events[-4:], ["e2", "outer-revert", "rebuild", "recover"])

        events.clear()

        def failed_rebuild(*_args: object) -> None:
            events.append("rebuild")
            raise RuntimeError("staging rebuild failed")

        with mock.patch.object(nightly.luca_deploy, "deploy_and_accept", side_effect=deploy), mock.patch(
            "mirror.staging.regenerate_staging", side_effect=failed_rebuild
        ), mock.patch.object(
            nightly.luca_deploy, "recover_production", side_effect=lambda **_kwargs: events.append("recover")
        ):
            self.assertEqual(self._run(events, run_face), 1)
        self.assertEqual(events[-4:], ["e2", "outer-revert", "rebuild", "recover"])

    def test_prebackup_failure_never_recovers_and_push_failure_never_reverts(self) -> None:
        events: list[str] = []

        def prebackup(*, attempt: object, **_kwargs: object) -> object:
            self.assertFalse(attempt.backup_taken)
            attempt.deploy_started = True
            nightly.luca_deploy.journal.append_deploy_started(
                self.pgl_home,
                ts="2026-08-10T04:00:00+09:00",
            )
            raise RuntimeError("backup failed")

        def e2_failure(*_args: object, before_commit: object) -> FaceResult:
            before_commit(HASH)
            raise AssertionError("unreachable")

        with mock.patch.object(nightly.luca_deploy, "deploy_and_accept", side_effect=prebackup), mock.patch.object(
            nightly.luca_deploy, "recover_production"
        ) as recovery:
            self.assertEqual(self._run(events, e2_failure), 1)
        recovery.assert_not_called()
        state = nightly.luca_deploy.load_lifecycle_state(self.pgl_home)
        self.assertTrue(state.deploy_aborted)
        self.assertFalse(state.resume_required)
        self.assertEqual(state.production_state, "OLD")

    def test_post_e2_pre_result_exception_recovers_but_post_result_failure_does_not(self) -> None:
        events: list[str] = []

        def deploy(*, attempt: object, **_kwargs: object) -> object:
            attempt.backup_taken = True
            events.append("e2")
            return object()

        def run_face_before_result(*_args: object, before_commit: object) -> FaceResult:
            before_commit(HASH)
            nightly.applier_apply._git(
                self.home, "commit", "--only", "-m", f"Content-Hash: {HASH}\n", "--", "x"
            )
            events.append("outer-revert")
            raise RuntimeError("tag verification failed")

        with mock.patch.object(nightly.luca_deploy, "deploy_and_accept", side_effect=deploy), mock.patch(
            "mirror.staging.regenerate_staging", side_effect=lambda *_args: events.append("rebuild")
        ), mock.patch.object(
            nightly.luca_deploy, "recover_production", side_effect=lambda **_kwargs: events.append("recover")
        ):
            self.assertEqual(self._run(events, run_face_before_result), 1)
        self.assertEqual(events[-5:], ["e2", "commit", "outer-revert", "rebuild", "recover"])

        events.clear()
        def successful_deploy(*, attempt: object, **_kwargs: object) -> object:
            attempt.deploy_started = True
            attempt.backup_taken = True
            events.append("e2")
            return object()

        with mock.patch.object(
            nightly.luca_deploy, "deploy_and_accept", side_effect=successful_deploy
        ), mock.patch.object(nightly.luca_deploy, "recover_production") as recovery, mock.patch.object(
            nightly.luca_deploy, "record_commit_completed", side_effect=RuntimeError("journal failure")
        ):
            self.assertEqual(self._run(events, FaceResult(True, "tag", HASH)), 1)
        recovery.assert_not_called()
        self.assertIn("e2", events)

    def test_transfer_phase_failure_restores_unconditionally_after_backup(self) -> None:
        events: list[str] = []

        def transfer_failure(*, attempt: object, **_kwargs: object) -> object:
            attempt.deploy_started = True
            attempt.backup_taken = True
            events.append("transfer-failed")
            raise nightly.luca_deploy.DeployError(
                "transfer failed",
                phase="transfer",
                backup_completed=True,
            )

        def run_face(*_args: object, before_commit: object) -> FaceResult:
            before_commit(HASH)
            raise AssertionError("transfer failure must prevent commit")

        with mock.patch.object(
            nightly.luca_deploy, "deploy_and_accept", side_effect=transfer_failure
        ), mock.patch(
            "mirror.staging.regenerate_staging",
            side_effect=lambda *_args: events.append("rebuild"),
        ), mock.patch.object(
            nightly.luca_deploy,
            "recover_production",
            side_effect=lambda **_kwargs: events.append("restore"),
        ):
            self.assertEqual(self._run(events, run_face), 1)
        self.assertEqual(events[-3:], ["transfer-failed", "rebuild", "restore"])

    def test_post_restart_private_activity_hold_recovers_with_skip_digest(self) -> None:
        events: list[str] = []

        def private_activity_hold(*, attempt: object, **_kwargs: object) -> object:
            attempt.deploy_started = True
            attempt.backup_taken = True
            events.append("private-activity")
            raise nightly.luca_deploy.PrivateActivityHold(
                "private activity observed within 15 minutes",
                phase=nightly.luca_deploy.PRIVATE_ACTIVITY_PHASE,
                backup_completed=True,
            )

        def run_face(*args: object, before_commit: object) -> FaceResult:
            try:
                before_commit(HASH)
            except Exception as exc:
                args[5].emit(f"[RED] luca: abort/revert: {exc}")
                raise
            raise AssertionError("private-activity hold must prevent commit")

        with mock.patch.object(
            nightly.luca_deploy,
            "deploy_and_accept",
            side_effect=private_activity_hold,
        ), mock.patch(
            "mirror.staging.regenerate_staging",
            side_effect=lambda *_args: events.append("rebuild"),
        ), mock.patch.object(
            nightly.luca_deploy,
            "recover_production",
            side_effect=lambda **_kwargs: events.append("restore"),
        ) as recovery, mock.patch.object(
            nightly.luca_deploy, "record_commit_completed"
        ) as journal, mock.patch.object(
            nightly.luca_deploy, "write_production_anchor"
        ) as anchor, mock.patch.object(
            nightly, "_push_luca_commit"
        ) as push:
            self.assertEqual(self._run(events, run_face), 1)

        recovery.assert_called_once()
        journal.assert_not_called()
        anchor.assert_not_called()
        push.assert_not_called()
        self.assertEqual(events[-3:], ["private-activity", "rebuild", "restore"])
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "luca: nightly acceptance skipped: private activity within 15 minutes; "
            "production restored",
            digest,
        )
        self.assertNotIn("[RED]", digest)

    def test_preflight_private_activity_hold_skips_without_backup_or_recovery(self) -> None:
        events: list[str] = []

        def private_activity_hold(*, attempt: object, **_kwargs: object) -> object:
            self.assertFalse(attempt.deploy_started)
            self.assertFalse(attempt.backup_taken)
            events.append("private-activity-preflight")
            raise nightly.luca_deploy.PrivateActivityHold(
                "private activity observed within 15 minutes",
                phase=nightly.luca_deploy.PRIVATE_ACTIVITY_PREFLIGHT_PHASE,
                backup_completed=False,
            )

        def run_face(*args: object, before_commit: object) -> FaceResult:
            try:
                before_commit(HASH)
            except Exception as exc:
                args[5].emit(f"[RED] luca: abort/revert: {exc}")
                raise
            raise AssertionError("private-activity hold must prevent commit")

        with mock.patch.object(
            nightly.luca_deploy,
            "deploy_and_accept",
            side_effect=private_activity_hold,
        ), mock.patch.object(
            nightly.luca_deploy, "recover_production"
        ) as recovery, mock.patch.object(
            nightly.luca_deploy, "record_commit_completed"
        ) as journal, mock.patch.object(
            nightly.luca_deploy, "write_production_anchor"
        ) as anchor, mock.patch.object(
            nightly, "_push_luca_commit"
        ) as push:
            self.assertEqual(self._run(events, run_face), 1)

        recovery.assert_not_called()
        journal.assert_not_called()
        anchor.assert_not_called()
        push.assert_not_called()
        self.assertEqual(events[-1], "private-activity-preflight")
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "luca: nightly acceptance skipped: private activity within 15 minutes; "
            "production unchanged",
            digest,
        )
        self.assertNotIn("[RED]", digest)

    def test_private_activity_hold_keeps_red_abort_when_recovery_fails(self) -> None:
        def private_activity_hold(*, attempt: object, **_kwargs: object) -> object:
            attempt.deploy_started = True
            attempt.backup_taken = True
            raise nightly.luca_deploy.PrivateActivityHold(
                "private activity observed within 15 minutes",
                phase=nightly.luca_deploy.PRIVATE_ACTIVITY_PHASE,
                backup_completed=True,
            )

        def run_face(*args: object, before_commit: object) -> FaceResult:
            try:
                before_commit(HASH)
            except Exception as exc:
                args[5].emit(f"[RED] luca: abort/revert: {exc}")
                raise
            raise AssertionError("private-activity hold must prevent commit")

        with mock.patch.object(
            nightly.luca_deploy,
            "deploy_and_accept",
            side_effect=private_activity_hold,
        ), mock.patch(
            "mirror.staging.regenerate_staging",
        ), mock.patch.object(
            nightly.luca_deploy,
            "recover_production",
            side_effect=RuntimeError("restore unavailable"),
        ):
            self.assertEqual(self._run([], run_face), 1)

        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("[RED] luca: abort/revert: private activity observed", digest)
        self.assertIn("[RED] luca: nightly stopped: Luca recovery failed", digest)
        self.assertNotIn("nightly acceptance skipped", digest)

    def test_private_activity_checker_fault_remains_red_after_recovery(self) -> None:
        events: list[str] = []

        def checker_fault(*, attempt: object, **_kwargs: object) -> object:
            attempt.deploy_started = True
            attempt.backup_taken = True
            raise nightly.luca_deploy.DeployError(
                "Luca deploy failed during private-activity: acceptance ledger missing",
                phase=nightly.luca_deploy.PRIVATE_ACTIVITY_PHASE,
                backup_completed=True,
            )

        def run_face(*args: object, before_commit: object) -> FaceResult:
            try:
                before_commit(HASH)
            except Exception as exc:
                args[5].emit(f"[RED] luca: abort/revert: {exc}")
                raise
            raise AssertionError("private-activity checker fault must prevent commit")

        with mock.patch.object(
            nightly.luca_deploy,
            "deploy_and_accept",
            side_effect=checker_fault,
        ), mock.patch(
            "mirror.staging.regenerate_staging",
        ), mock.patch.object(
            nightly.luca_deploy,
            "recover_production",
            side_effect=lambda **_kwargs: events.append("restore"),
        ) as recovery:
            self.assertEqual(self._run(events, run_face), 1)

        recovery.assert_called_once()
        self.assertEqual(events[-1], "restore")
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("[RED] luca: abort/revert:", digest)
        self.assertIn("[RED] luca: nightly stopped:", digest)
        self.assertIn("acceptance ledger missing", digest)
        self.assertNotIn("nightly acceptance skipped", digest)

    def test_restart_and_recovery_failures_are_loud_without_later_production_actions(self) -> None:
        events: list[str] = []

        def failed_restart(*, attempt: object, **_kwargs: object) -> object:
            attempt.backup_taken = True
            raise nightly.luca_deploy.DeployError("restart failed", phase="restart", backup_completed=True)

        def commit_then_fail(*_args: object, before_commit: object) -> FaceResult:
            before_commit(HASH)
            raise AssertionError("unreachable")

        with mock.patch.object(nightly.luca_deploy, "deploy_and_accept", side_effect=failed_restart), mock.patch(
            "mirror.staging.regenerate_staging"
        ), mock.patch.object(nightly.luca_deploy, "recover_production"):
            self.assertEqual(self._run(events, commit_then_fail), 1)
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(encoding="utf-8")
        self.assertIn("Luca may be stopped", digest)

        def failed_recovery(**_kwargs: object) -> None:
            raise RuntimeError("restore unavailable")

        with mock.patch.object(nightly.luca_deploy, "deploy_and_accept", side_effect=failed_restart), mock.patch(
            "mirror.staging.regenerate_staging"
        ), mock.patch.object(nightly.luca_deploy, "recover_production", side_effect=failed_recovery), mock.patch.object(
            nightly.luca_deploy, "record_commit_completed"
        ) as journal, mock.patch.object(nightly.luca_deploy, "write_production_anchor") as anchor, mock.patch.object(
            nightly, "_push_luca_commit"
        ) as push:
            self.assertEqual(self._run([], commit_then_fail), 1)
        journal.assert_not_called()
        anchor.assert_not_called()
        push.assert_not_called()
        digest = (self.pgl_home / "digest" / "2026-08-10.md").read_text(encoding="utf-8")
        self.assertIn("human escalation", digest)

    def test_push_uses_one_atomic_nonforce_command(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(nightly, "_overlay_git", return_value="main\n"), mock.patch.object(
            nightly.subprocess, "run", return_value=completed
        ) as run:
            nightly._push_luca_commit(self.home, "overlay-snap-luca-1")
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            (
                "git",
                "push",
                "--atomic",
                "origin",
                "HEAD:refs/heads/main",
                "refs/tags/overlay-snap-luca-1:refs/tags/overlay-snap-luca-1",
            ),
        )
        self.assertFalse(any(item.startswith("+") or item in {"--force", "-f"} for item in command))

        events: list[str] = []
        def deployed(*, attempt: object, **_kwargs: object) -> object:
            attempt.deploy_started = True
            attempt.backup_taken = True
            events.append("e2-performed")
            return object()

        with mock.patch.object(
            nightly.luca_deploy, "deploy_and_accept", side_effect=deployed
        ), mock.patch.object(nightly, "_push_luca_commit", side_effect=RuntimeError("push failed")), mock.patch.object(
            nightly.luca_deploy, "recover_production"
        ) as recovery, mock.patch.object(nightly.luca_deploy, "record_commit_completed"), mock.patch.object(
            nightly.luca_deploy, "write_production_anchor"
        ):
            self.assertEqual(self._run(events, FaceResult(True, "tag", HASH)), 1)
        recovery.assert_not_called()
        self.assertIn("e2-performed", events)

    def test_generic_main_refuses_luca_without_running_face(self) -> None:
        with mock.patch.object(
            nightly, "load_json_object", return_value=self.config
        ), mock.patch.object(
            nightly, "emit_admission_refusal", return_value=None
        ), mock.patch.object(nightly, "check_all"), mock.patch.object(
            nightly, "parse_scalar_yaml", return_value=SimpleNamespace(deviations=())
        ), mock.patch.object(nightly, "get_profile", return_value=self.profile), mock.patch.object(
            nightly, "inspect"
        ) as inspect_mock, mock.patch.object(
            nightly.applier_apply, "run_face"
        ) as applier_run_face:
            self.assertEqual(
                nightly.main(["--face", "luca", "--date", "2026-08-10"]),
                1,
            )
        applier_run_face.assert_not_called()
        inspect_mock.assert_called_once()
        digest = (self.pgl_home / "digest/2026-08-10.md").read_text(encoding="utf-8")
        self.assertIn("generic pgl-nightly refuses deployment", digest)
        self.assertIn("bin/pgl-nightly-luca", digest)

    def test_generic_main_luca_config_failure_still_emits_dedicated_refusal(self) -> None:
        with mock.patch.object(
            nightly,
            "load_json_object",
            side_effect=FileNotFoundError("growth-luca.json missing"),
        ), mock.patch.object(
            nightly, "emit_admission_refusal", return_value=None
        ), mock.patch.object(
            nightly.applier_apply, "run_face"
        ) as applier_run_face:
            self.assertEqual(
                nightly.main(["--face", "luca", "--date", "2026-08-10"]),
                1,
            )
        applier_run_face.assert_not_called()
        digest = (self.pgl_home / "digest/2026-08-10.md").read_text(encoding="utf-8")
        self.assertIn(
            "[RED] luca: generic pgl-nightly refuses deployment; "
            "use bin/pgl-nightly-luca",
            digest,
        )

    def test_generic_main_default_alpha_call_shape_is_unchanged(self) -> None:
        alpha_profile = SimpleNamespace(name="alpha")
        alpha_config = {"classifier_argv": ["classifier"]}
        with mock.patch.object(
            nightly, "load_json_object", return_value=alpha_config
        ), mock.patch.object(
            nightly, "emit_admission_refusal", return_value=None
        ), mock.patch.object(nightly, "check_all"), mock.patch.object(
            nightly, "parse_scalar_yaml", return_value=SimpleNamespace(deviations=())
        ), mock.patch.object(
            nightly, "get_profile", return_value=alpha_profile
        ), mock.patch.object(
            nightly, "acquire_lock", return_value=self.root / "alpha-lock"
        ), mock.patch.object(nightly, "release_lock", return_value=True), mock.patch.object(
            nightly, "inspect"
        ), mock.patch.object(
            nightly.applier_apply, "run_face", return_value=FaceResult(False, None, HASH)
        ) as applier_run_face:
            self.assertEqual(nightly.main(["--date", "2026-08-10"]), 0)
        self.assertEqual(applier_run_face.call_count, 1)
        self.assertEqual(applier_run_face.call_args.args[0].name, "alpha")
        self.assertEqual(len(applier_run_face.call_args.args), 6)
        self.assertNotIn("before_commit", applier_run_face.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
