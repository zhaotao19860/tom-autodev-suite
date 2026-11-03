import copy
import importlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_bridge import AgentBridge
from collaboration import intake_input_hash, intake_prerequisites, intake_prerequisites_valid
from lock_manager import LockManager
from orchestrator import Orchestrator
from profile_wizard import (
    build_profile, confirmed_inputs, load_template, unfilled_placeholders, write_requirement_profile,
)
from project_registry import validate_profile
from test_orchestrator import _requirement_snapshot
from test_phase_protocol import _seed_envelope
from test_phase_protocol_repair import canonical_hash, production_profile
from test_profile_wizard import ANSWERS
from test_schema_validation import specialized_examples
from worker_driver import classify_next


FILES = {
    "docs/index.md": "All project knowledge\n",
    "docs/environment/topology.md": "Pinned environment\n",
    "docs/development/style.md": "Pinned development rules\n",
    "docs/development/含 '引号'.md": "Quoted path\n",
    "docs/testing/policy.md": "Pinned testing rules\n",
    "docs/architecture/design.md": "Architecture\n",
    "docs/template.md": "Template\n",
    "docs/testing/template.md": "Template\n",
    "docs/testing/data.txt": "Not Markdown\n",
    "repos/service/overview.md": "Service overview\n",
    "repos/service/template.md": "Template\n",
    "artifacts/outcome.md": "Not project knowledge\n",
    "README.md": "Workspace overview\n",
}
PHASES = {
    "GRILL": ("INTAKE", "requirement-snapshot"),
    "SPEC": ("GRILL", "decision-log"),
    "TASKS": ("SPEC", "spec"),
    "PLAN": ("TASKS", "task-dag"),
    "IMPLEMENT": ("PLAN", "task-plan"),
    "REVIEW": ("IMPLEMENT", "change-set"),
    "DIAGNOSE": ("REVIEW", "review"),
}


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "team's workspace"
        self.workspace.mkdir()
        self.git(self.workspace, "init", "-q")
        for name, content in FILES.items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.commit = self.commit_workspace()
        self.pin = {
            "repository": "team/project-workspace",
            "path": str(self.workspace),
            "commit": self.commit,
        }
        self.profile = production_profile()
        for name, repo in (("business", self.profile["business_repos"][0]),
                           ("tests", self.profile["test_repo"])):
            path = self.root / name
            path.mkdir()
            self.git(path, "init", "-q")
            repo["path"] = str(path)
        skill = self.root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("Fixture skill\n", encoding="utf-8")
        self.profile["language_skill"] = str(skill)
        self.profile["project_skill"] = str(skill)
        self.profile["knowledge_sources"].append({
            "provider": "workspace",
            "repository": self.pin["repository"],
            "path": self.pin["path"],
            "revision": "HEAD",
            "search_scope": "Team project knowledge",
            "priority": 0,
        })
        self.config = self.root / "config-root"
        self.orchestrator = Orchestrator(self.config)
        self.profile_path = self.config / "config/projects/bgw.yaml"
        self.write_profile()

    @staticmethod
    def git(path, *arguments):
        return subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()

    def commit_workspace(self):
        self.git(self.workspace, "add", ".")
        self.git(self.workspace, "-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.test", "-c", "core.hooksPath=/dev/null",
                 "commit", "--no-gpg-sign", "-qm", "Workspace knowledge")
        return self.git(self.workspace, "rev-parse", "HEAD")

    def write_profile(self):
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.profile_path.write_text(yaml.safe_dump(self.profile), encoding="utf-8")

    def start(self, **options):
        return self.orchestrator.start(
            "BGW-1", "bgw", requirement_snapshot=_requirement_snapshot(), **options,
        )

    def prepared_payload(self, **options):
        with patch("orchestrator.uuid.uuid4", return_value=SimpleNamespace(hex="fixed-run")):
            result = self.start(intake_extra={}, **options)
        self.assertIn("payload", result)
        return result["payload"]

    def phase_run(self, phase):
        started = self.start(fresh_token=phase)
        self.assertIn("run_id", started)
        run_id = started["run_id"]
        predecessor, schema = PHASES[phase]
        task_id = "T-1" if predecessor in {"PLAN", "IMPLEMENT", "REVIEW"} else None
        self.orchestrator.artifacts.put_envelope(
            _seed_envelope(run_id, predecessor, task_id, specialized_examples()[schema])
        )
        self.orchestrator.state.transition(run_id, phase, {
            "requirement_id": "BGW-1", "task_id": "T-1",
            "source_revisions": {"business": "r1", "tests": "t1"},
        })
        return run_id

    def replace_workspace_with_empty_repository(self):
        shutil.rmtree(self.workspace)
        self.workspace.mkdir()
        self.git(self.workspace, "init", "-q")


class WorkspaceProfileTests(WorkspaceFixture):
    def test_workspace_root_is_valid(self):
        result = validate_profile(self.profile)
        self.assertTrue(result["ready"], result)

    def test_workspace_requires_path_even_without_path_checks(self):
        del self.profile["knowledge_sources"][-1]["path"]
        result = validate_profile(self.profile, check_paths=False)
        self.assertFalse(result["ready"])
        self.assertIn("knowledge_sources[1].path", result["missing"])

    def test_workspace_requires_repository(self):
        del self.profile["knowledge_sources"][-1]["repository"]
        result = validate_profile(self.profile, check_paths=False)
        self.assertFalse(result["ready"])
        self.assertIn("knowledge_sources[1].repository", result["missing"])

    def test_workspace_rejects_non_git_path(self):
        self.profile["knowledge_sources"][-1]["path"] = str(self.root)
        result = validate_profile(self.profile)
        self.assertFalse(result["ready"])
        self.assertIn("knowledge_sources[1].path", result["invalid"])

    def test_workspace_requires_repository_root_not_subdirectory(self):
        self.profile["knowledge_sources"][-1]["path"] = str(self.workspace / "docs")
        result = validate_profile(self.profile)
        self.assertFalse(result["ready"])
        self.assertIn("knowledge_sources[1].path", result["invalid"])

    def test_workspace_root_compares_resolved_paths(self):
        link = self.root / "workspace-link"
        link.symlink_to(self.workspace, target_is_directory=True)
        self.profile["knowledge_sources"][-1]["path"] = str(link)
        result = validate_profile(self.profile)
        self.assertTrue(result["ready"], result)

    def test_two_workspace_sources_are_rejected(self):
        self.profile["knowledge_sources"].append(copy.deepcopy(self.profile["knowledge_sources"][-1]))
        result = validate_profile(self.profile, check_paths=False)
        self.assertFalse(result["ready"])
        self.assertIn("knowledge_sources.workspace", result["invalid"])

    def test_workspace_does_not_replace_required_ku_source(self):
        self.profile["knowledge_sources"].pop(0)
        result = validate_profile(self.profile, check_paths=False)
        self.assertFalse(result["ready"])
        self.assertIn("knowledge_sources.ku", result["missing"])

    def test_workspace_path_check_can_be_disabled(self):
        self.profile["knowledge_sources"][-1]["path"] = str(self.root / "not-present")
        result = validate_profile(self.profile, check_paths=False)
        self.assertTrue(result["ready"], result)

    def test_knowledge_source_still_rejects_unknown_fields(self):
        self.profile["knowledge_sources"][-1]["unexpected"] = True
        result = validate_profile(self.profile, check_paths=False)
        self.assertFalse(result["ready"])
        self.assertIn("knowledge_sources[1].unexpected", result["invalid"])

    def test_invalid_provider_is_rejected_without_raising(self):
        self.profile["knowledge_sources"][-1]["provider"] = []
        result = validate_profile(self.profile, check_paths=False)
        self.assertFalse(result["ready"])
        self.assertIn("knowledge_sources[1].provider", result["invalid"])

    def test_wizard_fills_and_confirms_workspace_path(self):
        answers = {**ANSWERS, "workspace_knowledge_path": str(self.workspace)}
        profile = build_profile(load_template("bgw"), answers)
        sources = [source for source in profile["knowledge_sources"] if source["provider"] == "workspace"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["path"], str(self.workspace))
        self.assertEqual(confirmed_inputs(profile)["workspace_knowledge_path"], str(self.workspace))
        self.assertEqual(unfilled_placeholders(profile), set())
        self.assertTrue(validate_profile(profile, check_paths=False)["ready"])

    def test_wizard_can_omit_optional_workspace_source(self):
        profile = build_profile(load_template("bgw"), ANSWERS)
        self.assertFalse(any(source["provider"] == "workspace" for source in profile["knowledge_sources"]))
        self.assertNotIn("workspace_knowledge_path", confirmed_inputs(profile))
        self.assertEqual(unfilled_placeholders(profile), set())

    def test_wizard_writes_confirmed_workspace_source(self):
        answers = {**ANSWERS, "workspace_knowledge_path": str(self.workspace)}
        profile = build_profile(load_template("bgw"), answers)
        answers["confirmed_inputs"] = confirmed_inputs(profile)
        answers["confirmed_by"] = "fixture-owner"
        result = write_requirement_profile(
            "bgw", "BGW-1", answers, config_root=self.config, check_paths=False,
        )
        self.assertTrue(result["ready"], result)
        saved = yaml.safe_load(Path(result["profile_path"]).read_text(encoding="utf-8"))
        self.assertEqual(confirmed_inputs(saved)["workspace_knowledge_path"], str(self.workspace))

    def test_wizard_preserves_an_already_configured_workspace_source(self):
        template = load_template("bgw")
        template["knowledge_sources"].append(copy.deepcopy(self.profile["knowledge_sources"][-1]))
        profile = build_profile(template, ANSWERS)
        sources = [source for source in profile["knowledge_sources"] if source["provider"] == "workspace"]
        self.assertEqual(sources, [self.profile["knowledge_sources"][-1]])


class WorkspaceIntakeTests(WorkspaceFixture):
    def test_intake_pins_head_and_uses_v6(self):
        started = self.start()
        self.assertIn("run_id", started)
        payload = self.orchestrator.state.events(started["run_id"])[0]["payload"]
        self.assertEqual(payload.get("knowledge_workspace"), self.pin)
        self.assertEqual(payload["intake_hash_version"], "v6")
        self.assertEqual(intake_prerequisites(payload)["knowledge_workspace"], self.pin)
        self.assertTrue(intake_prerequisites_valid(payload, started["run_id"]))
        self.assertEqual(self.orchestrator.next(started["run_id"])["input_hash"], payload["g0_input_hash"])

    def test_workspace_commit_changes_g0_hash(self):
        before = self.prepared_payload()
        (self.workspace / "docs/index.md").write_text("New knowledge\n", encoding="utf-8")
        updated = self.commit_workspace()
        after = self.prepared_payload()
        self.assertNotEqual(before["g0_input_hash"], after["g0_input_hash"])
        self.assertEqual(after["knowledge_workspace"]["commit"], updated)
        tampered = copy.deepcopy(before)
        tampered["knowledge_workspace"]["commit"] = updated
        self.assertFalse(intake_prerequisites_valid(tampered, "fixed-run"))

    def test_requirement_diff_and_scope_are_still_bound_by_v6(self):
        payload = self.prepared_payload(approval_mode="scope", requirement_diff={"before": "old", "after": "new"})
        self.assertEqual(payload["intake_hash_version"], "v6")
        self.assertIn("knowledge_workspace", intake_prerequisites(payload))
        for field, value in (("requirement_diff", {"after": "changed"}), ("approval_mode", "gate")):
            with self.subTest(field=field):
                changed = {**payload, field: value}
                self.assertNotEqual(intake_input_hash(payload), intake_input_hash(changed))

    def test_no_workspace_keeps_v3_v4_v5_payloads_and_hashes(self):
        self.profile["knowledge_sources"].pop()
        self.write_profile()
        versions = [
            ("v3", {}), ("v4", {"approval_mode": "scope"}),
            ("v5", {"requirement_diff": {"before": "old", "after": "new"}}),
        ]
        keys = (
            "requirement_id", "project", "profile_hash", "requirement_snapshot", "collaboration_binding",
            "change_class", "workflow_spec_hash", "intake_hash_version", "evidence_contract",
        )
        for version, options in versions:
            with self.subTest(version=version):
                before = self.prepared_payload(**options)
                self.assertNotIn("knowledge_workspace", before)
                self.assertEqual(before["intake_hash_version"], version)
                legacy_keys = keys + (("approval_mode",) if version in {"v4", "v5"} else ())
                if version == "v5":
                    legacy_keys += ("requirement_diff",)
                self.assertEqual(before["g0_input_hash"], canonical_hash({key: before.get(key) for key in legacy_keys}))
                (self.workspace / "docs/index.md").write_text(version, encoding="utf-8")
                self.commit_workspace()
                self.assertEqual(self.prepared_payload(**options), before)

    def test_empty_workspace_repository_does_not_create_run(self):
        self.replace_workspace_with_empty_repository()
        self.assertEqual(self.start(), {
            "ok": False, "reason_code": "PROJECT_NOT_READY", "missing": ["knowledge_sources.workspace"],
        })
        self.assertEqual(self.orchestrator.state.latest_states(), [])

    def test_intake_git_timeout_does_not_create_run(self):
        profile_result = {"profile": self.profile}
        with patch("orchestrator.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)):
            result = self.orchestrator._intake_event(
                "BGW-1", "bgw", self.profile_path, "a" * 64,
                profile_result, _requirement_snapshot(), None, "intake-key",
            )
        self.assertEqual(result, {
            "ok": False, "reason_code": "PROJECT_NOT_READY", "missing": ["knowledge_sources.workspace"],
        })
        self.assertEqual(self.orchestrator.state.latest_states(), [])

    def test_intake_missing_or_non_git_workspace_is_not_ready(self):
        for path in (self.root / "missing-workspace", self.root):
            with self.subTest(path=path):
                self.profile["knowledge_sources"][-1]["path"] = str(path)
                result = self.orchestrator._intake_event(
                    "BGW-1", "bgw", self.profile_path, "a" * 64,
                    {"profile": self.profile}, _requirement_snapshot(), None, "intake-key",
                )
                self.assertEqual(result, {
                    "ok": False, "reason_code": "PROJECT_NOT_READY",
                    "missing": ["knowledge_sources.workspace"],
                })
        self.assertEqual(self.orchestrator.state.latest_states(), [])


class WorkspaceKnowledgeTests(WorkspaceFixture):
    def knowledge(self):
        return importlib.import_module("workspace_knowledge")

    def test_pinned_reads_only_intake(self):
        knowledge = self.knowledge()
        events = [{"state": "INTAKE", "payload": {"knowledge_workspace": self.pin}},
                  {"state": "SPEC", "payload": {"knowledge_workspace": {"commit": "wrong"}}}]
        self.assertEqual(knowledge.pinned(events), self.pin)
        self.assertIsNone(knowledge.pinned([]))
        self.assertIsNone(knowledge.pinned(events[1:]))
        self.assertIsNone(knowledge.pinned([{"state": "INTAKE", "payload": {}}]))

    def test_verify_accepts_pinned_commit(self):
        self.assertIsNone(self.knowledge().verify(self.pin))

    def test_verify_rejects_missing_commit_in_replacement_repository(self):
        self.replace_workspace_with_empty_repository()
        self.assertEqual(self.knowledge().verify(self.pin), "WORKSPACE_COMMIT_UNAVAILABLE")

    def test_documents_filter_all_phases_and_exclude_templates_and_non_markdown(self):
        knowledge = self.knowledge()
        for phase in PHASES:
            with self.subTest(phase=phase):
                prefixes = ("docs/", "repos/") if phase in {"GRILL", "SPEC", "TASKS"} else (
                    ("docs/environment/", "docs/testing/", "repos/") if phase == "DIAGNOSE" else
                    ("docs/development/", "docs/testing/", "repos/")
                )
                expected = sorted(name for name in FILES if name.startswith(prefixes)
                                  and name.endswith(".md") and Path(name).name != "template.md")
                documents = knowledge.documents(self.pin, phase)
                self.assertEqual([document["path"] for document in documents], expected)
                for document in documents:
                    self.assertEqual(set(document), {"path", "commit", "show"})
                    self.assertEqual(document["commit"], self.commit)
                    self.assertEqual(shlex.split(document["show"]), [
                        "git", "-C", str(self.workspace), "show", f"{self.commit}:{document['path']}",
                    ])

    def test_git_failures_and_timeouts_are_unavailable(self):
        knowledge = self.knowledge()
        for error in (OSError("git missing"), subprocess.TimeoutExpired("git", 30),
                      subprocess.CalledProcessError(1, "git")):
            with self.subTest(error=type(error).__name__), patch("workspace_knowledge.subprocess.run", side_effect=error):
                self.assertEqual(knowledge.verify(self.pin), "WORKSPACE_COMMIT_UNAVAILABLE")
                with self.assertRaisesRegex(ValueError, "^WORKSPACE_COMMIT_UNAVAILABLE$"):
                    knowledge.documents(self.pin, "SPEC")

    def test_git_invocations_are_bounded_and_do_not_use_shell(self):
        knowledge = self.knowledge()
        with patch("workspace_knowledge.subprocess.run", wraps=subprocess.run) as run:
            self.assertIsNone(knowledge.verify(self.pin))
            knowledge.documents(self.pin, "SPEC")
        self.assertEqual(len(run.call_args_list), 2)
        for invocation in run.call_args_list:
            self.assertIsInstance(invocation.args[0], list)
            self.assertEqual(invocation.kwargs["timeout"], 30)
            self.assertFalse(invocation.kwargs.get("shell", False))


class WorkspaceActionTests(WorkspaceFixture):
    def test_producer_actions_filter_documents_for_every_phase(self):
        knowledge = importlib.import_module("workspace_knowledge")
        for phase in PHASES:
            with self.subTest(phase=phase):
                run_id = self.phase_run(phase)
                action = self.orchestrator.next(run_id)
                self.assertTrue(action["ok"], action)
                self.assertEqual(action["child_skill"], f"tom-{phase.lower()}")
                self.assertEqual(action["knowledge_workspace"], {
                    "repository": self.pin["repository"], "commit": self.commit,
                    "documents": knowledge.documents(self.pin, phase),
                })

    def test_workspace_display_does_not_change_action_identity_or_input_hash(self):
        for phase in PHASES:
            with self.subTest(phase=phase):
                run_id = self.phase_run(phase)
                events = self.orchestrator.state.events(run_id)
                without_pin = copy.deepcopy(events)
                del without_pin[0]["payload"]["knowledge_workspace"]
                with patch.object(self.orchestrator.state, "events", return_value=without_pin):
                    baseline = self.orchestrator.next(run_id, read_only=True)
                action = self.orchestrator.next(run_id, read_only=True)
                self.assertTrue(baseline["ok"], baseline)
                self.assertIn("knowledge_workspace", action)
                del action["knowledge_workspace"]
                self.assertEqual(action, baseline)

    def test_no_pin_action_matches_existing_full_grill_shape(self):
        self.profile["knowledge_sources"].pop()
        self.write_profile()
        run_id = self.phase_run("GRILL")
        action = self.orchestrator.next(run_id)
        current = self.orchestrator.state.events(run_id)[-1]
        identity = {
            key: action[key] for key in (
                "run_id", "source_event_id", "state", "phase", "task_id", "input_artifacts",
                "parent_artifact_hash", "source_revisions",
            )
        }
        identity["profile_hash"] = self.orchestrator.state.events(run_id)[0]["payload"]["profile_hash"]
        expected = {
            "ok": True, "reason_code": "OK", "run_id": run_id,
            "action_id": canonical_hash({"identity": identity, "input_hash": canonical_hash(identity)}),
            "source_event_id": current["event_id"], "state": "GRILL", "phase": "GRILL",
            "host": "comate", "child_skill": "tom-grill", "controller": None, "task_id": "T-1",
            "input_artifacts": action["input_artifacts"], "input_hash": canonical_hash(identity),
            "parent_artifact_hash": action["parent_artifact_hash"],
            "source_revisions": {"business": "r1", "tests": "t1"},
            "source_evidence_refs": ["ku:doc-seed/v1", "icafe:BGW-1/comment-seed"],
            "required_human_gate": "G1", "allowed_side_effects": action["allowed_side_effects"],
            "completion_predicate": action["completion_predicate"],
            "result_schema": "decision-log", "target_state": "SPEC",
        }
        self.assertEqual(action, expected)
        self.assertEqual(self.orchestrator.next(run_id), action)

    def test_missing_workspace_commit_blocks_even_a_cached_action_without_job(self):
        run_id = self.phase_run("GRILL")
        self.assertTrue(self.orchestrator.next(run_id)["ok"])
        self.replace_workspace_with_empty_repository()
        action = self.orchestrator.next(run_id)
        self.assertFalse(action["ok"])
        self.assertEqual(action["reason_code"], "WORKSPACE_COMMIT_UNAVAILABLE")
        self.assertEqual(action["run_id"], run_id)
        self.assertEqual(classify_next(self.orchestrator, run_id)["kind"], "BLOCKED")
        self.assertEqual(self.orchestrator.state.pending_producer_jobs(run_id), [])

    def test_failed_document_listing_blocks_action(self):
        run_id = self.phase_run("GRILL")
        with patch("workspace_knowledge.documents", side_effect=ValueError("WORKSPACE_COMMIT_UNAVAILABLE")):
            action = self.orchestrator.next(run_id)
        self.assertFalse(action["ok"])
        self.assertEqual(action["reason_code"], "WORKSPACE_COMMIT_UNAVAILABLE")

    def test_new_workspace_commit_does_not_change_pinned_run_documents(self):
        run_id = self.phase_run("GRILL")
        action = self.orchestrator.next(run_id)
        (self.workspace / "docs/environment/topology.md").write_text("Changed environment\n", encoding="utf-8")
        (self.workspace / "docs/new.md").write_text("New knowledge\n", encoding="utf-8")
        (self.workspace / "docs/testing/policy.md").unlink()
        self.assertNotEqual(self.commit_workspace(), self.commit)
        self.assertEqual(self.orchestrator.next(run_id), action)
        documents = action["knowledge_workspace"]["documents"]
        self.assertNotIn("docs/new.md", [document["path"] for document in documents])
        for document in documents:
            result = subprocess.run(shlex.split(document["show"]), check=True,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.stdout, FILES[document["path"]])
            self.assertEqual(document["commit"], self.commit)

    def test_agent_context_exposes_workspace_without_creating_job(self):
        run_id = self.phase_run("GRILL")
        locks = LockManager(self.config / "locks.sqlite")
        context = AgentBridge(self.orchestrator, locks=locks).context(run_id)
        self.assertTrue(context["ok"], context)
        action = self.orchestrator.next(run_id, read_only=True)
        self.assertEqual(context["action"]["knowledge_workspace"], action["knowledge_workspace"])
        self.assertEqual(self.orchestrator.state.pending_producer_jobs(run_id), [])
        json.dumps(context)


if __name__ == "__main__":
    unittest.main()
