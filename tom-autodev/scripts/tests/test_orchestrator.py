import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import Orchestrator


PROFILE = {
    "project_id": "bgw",
    "business_repos": [
        {"path": "/repo/business", "module": "bgw", "branch": "main", "lock": "bgw-main"}
    ],
    "test_repo": {
        "path": "/repo/tests",
        "module": "bgw-tests",
        "branch": "main",
        "lock": "bgw-tests-main",
    },
    "language_skill": "/skills/tom-lang-c-cpp",
    "project_skill": "/skills/tom-project-bgw",
    "knowledge_sources": [
        {
            "provider": "ku",
            "repository": "knowledge",
            "revision": "r1",
            "search_scope": "project",
            "priority": 1,
            "repo_id": "repo-1",
            "parent_doc_id": "parent-1",
        }
    ],
    "review_provider": {"kind": "source-only", "command": "review"},
    "pipeline_profile": {
        "pipeline_id": "bgw-pipeline",
        "allowed_parameters": [],
        "stage_classes": ["unit"],
        "release_rule": "manual-approval",
    },
    "environment_profile": {
        "runner": "linux",
        "os_arch": "linux/amd64",
        "image_digest": "sha256:env",
        "toolchain_digest": "sha256:tools",
        "hardware_or_simulator": "simulator",
        "data": "test-data",
        "services": "none",
        "capacity": "small",
    },
    "approval_channels": {
        "comate": {"channel": "comate-review"},
        "infoflow": {"channel": "group-1"},
        "role_members": {
            "development": ["developer@example.test"],
            "test": ["tester@example.test"],
            "project": ["manager@example.test"],
        },
    },
}


def _approve_for_run(orchestrator: Orchestrator, run_id: str, gate: str, input_hash: str) -> dict:
    policy = {"comate": ["manager@example.test"], "infoflow": ["manager@example.test"]}
    request = orchestrator.approvals.request(
        gate, input_hash, ["comate", "infoflow"], run_id=run_id, member_policy=policy
    )
    for channel in ("comate", "infoflow"):
        orchestrator.approvals.record_delivery(
            request["approval_id"], channel, {"request_id": f"{channel}-{request['approval_id']}"}, payload_hash=input_hash
        )
    orchestrator.approve(
        request["approval_id"], "APPROVE", input_hash, "comate", run_id, "manager@example.test"
    )
    return request


def _requirement_snapshot(card_id="BGW-1"):
    snapshot = {
        "canonical_card_id": card_id,
        "title": f"Requirement {card_id}",
        "body": "Behavior body",
        "html": "<p>Behavior body</p>",
        "acceptance": ["AC-1"],
        "fields": {"priority": "P1"},
        "attachments": [],
        "links": [],
        "status": "OPEN",
        "type": "REQUIREMENT",
        "responsible_people": [{"email": "manager@example.test"}],
        "created": {"user": {"email": "manager@example.test"}, "time": "2026-08-11T00:00:00+00:00"},
        "modified": {"user": {"email": "manager@example.test"}, "time": "2026-08-11T00:00:00+00:00"},
    }
    snapshot["content_hash"] = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return snapshot


def _start(orchestrator, card_id="BGW-1"):
    return orchestrator.start(
        card_id, "bgw", requirement_snapshot=_requirement_snapshot(card_id)
    )


class OrchestratorTests(unittest.TestCase):
    def test_child_brief_distinguishes_generation_and_approved_submission(self):
        from run_brief import _next_step

        action = {"child_skill": "tom-implement", "required_human_gate": "G5"}
        approved = {"action": "G5", "approval_id": "approval-g5"}
        draft = _next_step(action, "IMPLEMENT", [], [], None)
        self.assertIn("待生成", draft["text"])
        ready = _next_step(action, "IMPLEMENT", [], [], None, approved)
        self.assertIn("已批准待提交", ready["text"])
        self.assertNotIn("approval_input_hash", ready["text"])
        self.assertNotIn("complete-phase", ready["text"])
        self.assertIn("continue", ready["text"])
        ready_without_child = _next_step({"required_human_gate": "G5"}, "IMPLEMENT", [], [], None, approved)
        self.assertIn("已批准待提交", ready_without_child["text"])
        self.assertNotIn("complete-phase", ready_without_child["text"])
        self.assertIn("continue", ready_without_child["text"])
        g7 = {"action": "G7", "approval_id": "approval-g7"}
        submit_ready = _next_step({"controller": "submit", "required_human_gate": "G7"}, "SUBMIT", [], [], None, g7)
        self.assertIn("已批准待提交", submit_ready["text"])
        self.assertIn("continue", submit_ready["text"])
        self.assertNotIn("cli.py submit", submit_ready["text"])
        self.assertNotIn("complete-phase", submit_ready["text"])
        blocked = _next_step(action, "IMPLEMENT", [], [], "PREDECESSOR_REQUIRED", approved)
        self.assertNotIn("已批准待提交", blocked["text"])

    def test_child_brief_does_not_reuse_approval_from_previous_phase_entry(self):
        from run_brief import build
        from unittest.mock import Mock

        orchestrator = Mock()
        orchestrator.state.events.return_value = [{
            "state": "IMPLEMENT", "created_at": "2026-09-14T12:00:00",
        }]
        orchestrator.state.pending_intents.return_value = []
        orchestrator.next.return_value = {
            "ok": True, "child_skill": "tom-implement", "required_human_gate": "G5",
        }
        row = {
            "action": "G5", "approval_id": "g5", "effective_decision": "APPROVE",
            "created_at": "2026-09-14T11:00:00", "resolved_at": "2026-09-14T13:00:00",
        }
        orchestrator.approvals.for_run.return_value = [row]
        self.assertIn("待生成", build(orchestrator, "run")["next"]["text"])
        row["created_at"] = "2026-09-14T12:01:00"
        self.assertIn("已批准待提交", build(orchestrator, "run")["next"]["text"])

    def test_submission_recorded_is_a_successful_cli_exit(self):
        from orchestrator import _cli_exit_code

        self.assertEqual(_cli_exit_code({"ok": True, "reason_code": "SUBMISSION_RECORDED", "state": "WORKSPACE"}), 0)
        self.assertEqual(_cli_exit_code({"ok": False, "reason_code": "SUBMIT_REJECTED"}), 1)

    def test_explicit_config_root_keeps_profiles_under_config_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "config" / "projects" / "bgw.yaml"
            _write_profile(root, PROFILE)

            result = _start(Orchestrator(root))
            recorded = Orchestrator(root).status(result["run_id"])["events"][0]["payload"]

        self.assertEqual(recorded["profile_path"], str(expected))
        self.assertEqual(result["state"], "INTAKE")

    def test_status_does_not_ask_for_a_superseded_pending_gate(self):
        """A later APPROVE of the same action retires an unanswered earlier card.

        BGW-1956 Spec 1.1.4 already had G2 43cb8f46 APPROVE while e133d87e stayed
        PENDING; status then kept asking people to answer the dead card.
        """
        from run_brief import build

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            policy = {"comate": ["manager@example.test"], "infoflow": ["manager@example.test"]}
            stale = orchestrator.approvals.request(
                "G2", "stale-hash", ["comate", "infoflow"],
                run_id=run_id, member_policy=policy,
            )
            _approve_for_run(orchestrator, run_id, "G2", "current-hash")
            live = orchestrator.approvals.request(
                "G6", "live-hash", ["comate", "infoflow"],
                run_id=run_id, member_policy=policy,
            )
            for channel in ("comate", "infoflow"):
                orchestrator.approvals.record_delivery(
                    live["approval_id"], channel,
                    {"request_id": f"{channel}-{live['approval_id']}"},
                    payload_hash="live-hash",
                )
            orchestrator.state.transition(run_id, "DIAGNOSE", {"task_id": "T3"})
            original_next = orchestrator.next
            orchestrator.next = lambda _run_id: {
                "ok": True, "required_human_gate": "G6", "state": "DIAGNOSE",
            }
            try:
                brief = build(orchestrator, run_id)
            finally:
                orchestrator.next = original_next

        self.assertIsNone(stale.get("effective_decision"))
        self.assertEqual(
            [gate["approval_id"] for gate in brief["waiting_on"]],
            [live["approval_id"]],
        )
        self.assertIn(live["approval_id"], brief["next"]["text"])
        self.assertNotIn(stale["approval_id"], brief["next"]["text"])

    def test_start_missing_profile_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Orchestrator(Path(directory)).start("BGW-1", "bgw")

        self.assertEqual(result["reason_code"], "PROJECT_NOT_READY")
        self.assertNotIn("run_id", result)

    def test_knowledge_sync_factory_uses_the_started_runs_validated_project_profile(self):
        class NoCallTransport:
            skip_preflight = True

            def run(self, _argv, **_options):
                raise AssertionError("factory must not call a live transport")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = copy.deepcopy(PROFILE)
            profile["knowledge_sources"][0]["repo_id"] = "sX0BTOBWJX"
            profile["knowledge_sources"][0]["parent_doc_id"] = "I15ClP2KW4ZGAK"
            _write_profile(root, profile)
            orchestrator = Orchestrator(root)
            started = _start(orchestrator)

            sync = orchestrator.knowledge_sync(
                started["run_id"],
                ku_transport=NoCallTransport(),
                cafe_transport=NoCallTransport(),
                username="tester",
                cafe_preflight=False,
            )

        self.assertEqual(sync.card_id, "BGW-1")
        self.assertEqual(sync.project_parent_doc_id, "I15ClP2KW4ZGAK")

    def test_knowledge_sync_factory_rejects_profile_bytes_changed_after_start(self):
        class NoCallTransport:
            skip_preflight = True

            def __init__(self):
                self.calls = []

            def run(self, argv, **options):
                self.calls.append((list(argv), dict(options)))
                raise AssertionError("profile conflict must fail before adapter calls")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = copy.deepcopy(PROFILE)
            profile["knowledge_sources"][0]["repo_id"] = "sX0BTOBWJX"
            profile["knowledge_sources"][0]["parent_doc_id"] = "I15ClP2KW4ZGAK"
            _write_profile(root, profile)
            orchestrator = Orchestrator(root)
            started = _start(orchestrator)
            configured = root / "config" / "projects" / "bgw.yaml"
            changed = yaml.safe_load(configured.read_text(encoding="utf-8"))
            changed["approval_channels"]["comate"]["channel"] = "changed-after-start"
            configured.write_text(yaml.safe_dump(changed), encoding="utf-8")
            ku_transport = NoCallTransport()
            cafe_transport = NoCallTransport()

            result = orchestrator.knowledge_sync(
                started["run_id"],
                ku_transport=ku_transport,
                cafe_transport=cafe_transport,
                username="tester",
                cafe_preflight=False,
            )

        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT")
        self.assertEqual(ku_transport.calls, [])
        self.assertEqual(cafe_transport.calls, [])

    def test_knowledge_sync_factory_rejects_project_id_different_from_started_project(self):
        class NoCallTransport:
            skip_preflight = True

            def run(self, _argv, **_options):
                raise AssertionError("project mismatch must fail before adapter calls")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = copy.deepcopy(PROFILE)
            profile["project_id"] = "xflow"
            profile["knowledge_sources"][0]["repo_id"] = "sX0BTOBWJX"
            profile["knowledge_sources"][0]["parent_doc_id"] = "meQ-Acjg0K09Xr"
            _write_profile(root, profile)
            orchestrator = Orchestrator(root)
            started = _start(orchestrator)

            result = orchestrator.knowledge_sync(
                started["run_id"],
                ku_transport=NoCallTransport(),
                cafe_transport=NoCallTransport(),
                username="tester",
                cafe_preflight=False,
            )

        self.assertEqual(result["reason_code"], "PROJECT_PROFILE_MISMATCH")

    def test_knowledge_sync_factory_rejects_a_profile_path_not_recorded_for_the_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = copy.deepcopy(PROFILE)
            profile["knowledge_sources"][0]["repo_id"] = "sX0BTOBWJX"
            profile["knowledge_sources"][0]["parent_doc_id"] = "I15ClP2KW4ZGAK"
            _write_profile(root, profile)
            configured = root / "config" / "projects" / "bgw.yaml"
            orchestrator = Orchestrator(root)
            run_id = "run-profile-path"
            orchestrator.state.transition(
                run_id,
                "INTAKE",
                {
                    "requirement_id": "BGW-1",
                    "project": "bgw",
                    "profile_path": str(configured.parent / ".." / "projects" / "bgw.yaml"),
                    "profile_hash": hashlib.sha256(configured.read_bytes()).hexdigest(),
                },
            )

            result = orchestrator.knowledge_sync(run_id)

        self.assertEqual(result["reason_code"], "PROJECT_PROFILE_PATH_MISMATCH")

    def test_runtime_factories_use_started_profile_and_shared_durable_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            configured = root / "config" / "projects" / "bgw.yaml"
            expected_profile = yaml.safe_load(configured.read_text(encoding="utf-8"))
            expected_hash = hashlib.sha256(configured.read_bytes()).hexdigest()

            ipipe = orchestrator.ipipe_runtime(run_id, object())
            icode = orchestrator.icode_runtime(
                run_id,
                worktree_bindings={},
                system_skill_path=root / "language-skill",
                argv_transport=object(),
            )

        self.assertEqual(ipipe.validated_profile, expected_profile)
        self.assertEqual(ipipe.profile_hash, expected_hash)
        self.assertIs(icode.artifacts, orchestrator.artifacts)
        self.assertIs(icode.workspaces, orchestrator.workspaces)

    def test_runtime_factories_reject_profile_drift_and_pinned_dependency_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]

            ipipe_override = orchestrator.ipipe_runtime(run_id, object(), validated_profile={})
            icode_override = orchestrator.icode_runtime(
                run_id,
                artifact_store=object(),
                worktree_bindings={},
                system_skill_path=root / "language-skill",
                argv_transport=object(),
            )
            configured = root / "config" / "projects" / "bgw.yaml"
            changed = yaml.safe_load(configured.read_text(encoding="utf-8"))
            changed["pipeline_profile"]["pipeline_id"] = "changed-after-start"
            configured.write_text(yaml.safe_dump(changed), encoding="utf-8")
            drifted = orchestrator.ipipe_runtime(run_id, object())

        self.assertEqual(ipipe_override["reason_code"], "RUNTIME_OPTION_FORBIDDEN")
        self.assertEqual(icode_override["reason_code"], "RUNTIME_OPTION_FORBIDDEN")
        self.assertEqual(drifted["reason_code"], "PROFILE_CONFLICT")

    def test_start_status_resume_and_stop_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)

            started = _start(orchestrator)
            status = orchestrator.status(started["run_id"])
            resumed = orchestrator.resume(started["run_id"])
            stopped = orchestrator.stop(started["run_id"])
            stopped_again = orchestrator.stop(started["run_id"])

        self.assertEqual(started["state"], "INTAKE")
        self.assertEqual(status["state"], "INTAKE")
        self.assertEqual(resumed["state"], "INTAKE")
        self.assertEqual(stopped["state"], "STOPPED")
        self.assertEqual(stopped_again["state"], "STOPPED")

    def test_resume_and_progression_require_recovery_for_uncertain_intents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            intent = orchestrator.state.intent(
                run_id,
                "create-document",
                "document:BGW-1",
                {"document_key": "BGW-1"},
            )
            orchestrator.state.record_handoff(
                run_id, "handoff-1", {"phase": "INTAKE"}
            )
            event_count = len(orchestrator.status(run_id)["events"])

            resumed = orchestrator.resume(run_id)
            blocked_advance = orchestrator.advance(run_id, "GRILL", {})
            blocked_failure = orchestrator.route_failure(
                run_id, "REQUIREMENT_CHANGED", {"content_hash": "changed"}
            )
            event_count_after_blocks = len(orchestrator.status(run_id)["events"])
            stopped = orchestrator.stop(run_id)

        self.assertEqual(resumed["state"], "INTAKE")
        self.assertEqual(resumed["status"], "QUERY_REQUIRED")
        self.assertFalse(resumed["retry_allowed"])
        self.assertEqual(resumed["actions"][0]["intent_id"], intent["intent_id"])
        self.assertEqual(resumed["incomplete_handoffs"][0]["handoff_id"], "handoff-1")
        self.assertEqual(blocked_advance["reason_code"], "RECOVERY_REQUIRED")
        self.assertEqual(blocked_failure["reason_code"], "RECOVERY_REQUIRED")
        self.assertEqual(event_count_after_blocks, event_count)
        self.assertEqual(stopped["state"], "STOPPED")

    def test_approve_uses_shared_approval_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            request = orchestrator.approvals.request("G2", "hash-a", ["comate", "infoflow"])

            resolved = orchestrator.approve(request["approval_id"], "APPROVE", "hash-a", "comate")

        self.assertEqual(resolved["effective_decision"], "APPROVE")

    def test_advance_requires_evidence_gate_and_valid_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            started = _start(orchestrator)

            blocked = orchestrator.advance(
                started["run_id"],
                "GRILL",
                {"input_hash": "new", "approved_input_hash": "old"},
            )
            request = _approve_for_run(orchestrator, started["run_id"], "G0", "same")
            advanced = orchestrator.advance(
                started["run_id"],
                "GRILL",
                {
                    "input_hash": "same",
                    "approval_id": request["approval_id"],
                    "artifacts": ["requirement-snapshot", "collaboration-session"],
                },
            )

        self.assertEqual(blocked["reason_code"], "INPUT_HASH_MISMATCH")
        self.assertEqual(blocked["state"], "INTAKE")
        self.assertEqual(advanced["state"], "GRILL")

    def test_advance_rejects_caller_forged_approval_without_a_ledger_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            started = _start(orchestrator)
            event_count = len(orchestrator.status(started["run_id"])["events"])

            result = orchestrator.advance(
                started["run_id"],
                "GRILL",
                {
                    "input_hash": "same",
                    "approved_input_hash": "same",
                    "approval_id": "forged-id",
                    "artifacts": ["requirement-snapshot", "collaboration-session"],
                    "approvals": [{"gate": "G0", "decision": "APPROVE", "input_hash": "same"}],
                },
            )

            event_count_after = len(orchestrator.status(started["run_id"])["events"])

        self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED")
        self.assertEqual(result["state"], "INTAKE")
        self.assertEqual(event_count_after, event_count)

    def test_advance_records_its_policy_decision_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            started = _start(orchestrator)
            request = _approve_for_run(orchestrator, started["run_id"], "G0", "same")
            evidence = {
                "input_hash": "same",
                "approval_id": request["approval_id"],
                "artifacts": ["requirement-snapshot", "collaboration-session"],
            }

            advanced = orchestrator.advance(started["run_id"], "GRILL", evidence)
            repeated = orchestrator.advance(started["run_id"], "GRILL", evidence)
            event = orchestrator.status(started["run_id"])["events"][-1]

        self.assertEqual(repeated, advanced)
        self.assertEqual(event["payload"]["policy_decision"]["policy_id"], "transition-policy-v1")
        self.assertTrue(event["payload"]["policy_decision"]["allowed"])

    def test_advance_retry_uses_the_last_committed_operation_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            source_event = orchestrator.state.transition(run_id, "GRILL", {"test_setup": True})
            approval = _approve_for_run(orchestrator, run_id, "G1", "shared-hash")
            evidence = {
                "input_hash": "shared-hash",
                "approval_id": approval["approval_id"],
                "artifacts": ["grill"],
            }

            advanced = orchestrator.advance(run_id, "SPEC", evidence)
            retried = orchestrator.advance(run_id, "SPEC", evidence)
            events = orchestrator.status(run_id)["events"]

        self.assertEqual(retried, advanced)
        self.assertEqual(len(events), 3)
        self.assertEqual(
            events[-1]["payload"]["operation_identity"],
            {
                "approval_gate": "G1",
                "input_hash": "shared-hash",
                "source_event_id": source_event["event_id"],
                "source_state": "GRILL",
                "target_state": "SPEC",
            },
        )

    def test_reentered_source_checkpoint_does_not_reuse_prior_advance_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            first_source_event = orchestrator.state.transition(
                run_id, "GRILL", {"test_setup": True}
            )
            first_g1 = _approve_for_run(orchestrator, run_id, "G1", "shared-hash")
            first_result = orchestrator.advance(
                run_id,
                "SPEC",
                {
                    "input_hash": "shared-hash",
                    "approval_id": first_g1["approval_id"],
                    "artifacts": ["grill"],
                },
            )

            cycle = [
                ("TASKS", "G2", "cycle-tasks", ["spec"]),
                ("WORKSPACE", "G3", "cycle-workspace", ["task-dag"]),
                ("PLAN", "G4", "cycle-plan", ["workspace", "task-plan"]),
                ("IMPLEMENT", "G4", "cycle-implement", ["task-plan"]),
                ("REVIEW", "G5", "cycle-review", ["change-set"]),
            ]
            for target, gate, input_hash, artifacts in cycle:
                approval = _approve_for_run(orchestrator, run_id, gate, input_hash)
                advanced = orchestrator.advance(
                    run_id,
                    target,
                    {
                        "input_hash": input_hash,
                        "approval_id": approval["approval_id"],
                        "artifacts": artifacts,
                    },
                )
                self.assertEqual(advanced["state"], target)

            diagnosed = orchestrator.route_failure(
                run_id, "CODE_FAILURE", {"signature": "cycle-failure"}
            )
            self.assertEqual(diagnosed["state"], "DIAGNOSE")
            for target, gate, input_hash, artifacts in [
                ("ARCHITECTURE_REVIEW", "G6", "cycle-architecture", ["failure-bundle"]),
                (
                    "GRILL",
                    "G0",
                    "cycle-grill",
                    ["requirement-snapshot", "collaboration-session"],
                ),
            ]:
                approval = _approve_for_run(orchestrator, run_id, gate, input_hash)
                advanced = orchestrator.advance(
                    run_id,
                    target,
                    {
                        "input_hash": input_hash,
                        "approval_id": approval["approval_id"],
                        "artifacts": artifacts,
                    },
                )
                self.assertEqual(advanced["state"], target)

            reentered_source_event = orchestrator.status(run_id)["events"][-1]
            event_count = len(orchestrator.status(run_id)["events"])
            blocked = orchestrator.advance(
                run_id,
                "SPEC",
                {
                    "input_hash": "shared-hash",
                    "approved_input_hash": "shared-hash",
                    "source_event_id": first_source_event["event_id"],
                    "artifacts": ["grill"],
                },
            )
            event_count_after_block = len(orchestrator.status(run_id)["events"])

            current_g1 = _approve_for_run(orchestrator, run_id, "G1", "shared-hash")
            current_result = orchestrator.advance(
                run_id,
                "SPEC",
                {
                    "input_hash": "shared-hash",
                    "approval_id": current_g1["approval_id"],
                    "source_event_id": first_source_event["event_id"],
                    "artifacts": ["grill"],
                },
            )
            current_event = orchestrator.status(run_id)["events"][-1]

        self.assertEqual(blocked["reason_code"], "APPROVAL_REQUIRED")
        self.assertEqual(blocked["state"], "GRILL")
        self.assertEqual(event_count_after_block, event_count)
        self.assertNotEqual(current_result["event_id"], first_result["event_id"])
        self.assertEqual(current_result["event_id"], current_event["event_id"])
        self.assertEqual(
            current_event["payload"]["operation_identity"]["source_event_id"],
            reentered_source_event["event_id"],
        )
        self.assertNotEqual(
            current_event["payload"]["operation_identity"]["source_event_id"],
            first_source_event["event_id"],
        )

    def test_same_target_and_hash_from_diagnose_does_not_reuse_grill_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            orchestrator.state.transition(run_id, "GRILL", {"test_setup": True})
            g1 = _approve_for_run(orchestrator, run_id, "G1", "shared-hash")
            grill_result = orchestrator.advance(
                run_id,
                "SPEC",
                {
                    "input_hash": "shared-hash",
                    "approval_id": g1["approval_id"],
                    "artifacts": ["grill"],
                },
            )
            orchestrator.state.transition(run_id, "DIAGNOSE", {"test_setup": True})
            event_count = len(orchestrator.status(run_id)["events"])

            blocked = orchestrator.advance(
                run_id,
                "SPEC",
                {
                    "input_hash": "shared-hash",
                    "approved_input_hash": "shared-hash",
                    "artifacts": ["failure-bundle"],
                },
            )
            event_count_after_block = len(orchestrator.status(run_id)["events"])
            g6 = _approve_for_run(orchestrator, run_id, "G6", "shared-hash")
            diagnosis_result = orchestrator.advance(
                run_id,
                "SPEC",
                {
                    "input_hash": "shared-hash",
                    "approval_id": g6["approval_id"],
                    "artifacts": ["failure-bundle"],
                },
            )

        self.assertEqual(blocked["state"], "DIAGNOSE")
        self.assertEqual(blocked["reason_code"], "APPROVAL_REQUIRED")
        self.assertEqual(event_count_after_block, event_count)
        self.assertNotEqual(diagnosis_result["event_id"], grill_result["event_id"])
        self.assertEqual(diagnosis_result["state"], "SPEC")

    def test_terminal_operations_are_blocked_without_writing_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            for index, terminal_state in enumerate(["RELEASE_SUCCESS", "STOPPED"]):
                with self.subTest(terminal_state=terminal_state):
                    run_id = _start(orchestrator, f"BGW-{index + 1}")["run_id"]
                    orchestrator.state.transition(run_id, terminal_state, {"test_setup": True})
                    event_count = len(orchestrator.status(run_id)["events"])

                    advance = orchestrator.advance(run_id, "RELEASE_SUCCESS", {})
                    failure = orchestrator.route_failure(run_id, "RELEASE_FAILED", {"signature": "f1"})
                    stopped = orchestrator.stop(run_id)
                    event_count_after = len(orchestrator.status(run_id)["events"])

                    for result in [advance, failure, stopped]:
                        self.assertEqual(result["reason_code"], "TERMINAL_STATE")
                        self.assertEqual(result["state"], terminal_state)
                    self.assertEqual(event_count_after, event_count)

    def test_failure_route_records_policy_decision_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            orchestrator.state.transition(run_id, "REVIEW", {"test_setup": True})

            routed = orchestrator.route_failure(run_id, "CODE_FAILURE", {"signature": "c1"})
            repeated = orchestrator.route_failure(run_id, "CODE_FAILURE", {"signature": "c1"})
            event = orchestrator.status(run_id)["events"][-1]

        self.assertEqual(repeated, routed)
        self.assertEqual(event["payload"]["policy_decision"]["current_state"], "REVIEW")
        self.assertEqual(event["payload"]["policy_decision"]["next_state"], "DIAGNOSE")

    def test_diagnose_exit_requires_g6_and_keeps_failure_routing_automatic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            orchestrator.state.transition(run_id, "REVIEW", {"test_setup": True})

            routed = orchestrator.route_failure(run_id, "CODE_FAILURE", {"signature": "c1"})
            event_count = len(orchestrator.status(run_id)["events"])
            blocked = orchestrator.advance(
                run_id,
                "SPEC",
                {
                    "input_hash": "diagnosis-hash",
                    "approved_input_hash": "diagnosis-hash",
                    "artifacts": ["failure-bundle"],
                },
            )
            wrong_request = _approve_for_run(orchestrator, run_id, "G1", "diagnosis-hash")
            wrong_gate = orchestrator.advance(
                run_id,
                "SPEC",
                {
                    "input_hash": "diagnosis-hash",
                    "approval_id": wrong_request["approval_id"],
                    "artifacts": ["failure-bundle"],
                },
            )
            event_count_after_blocks = len(orchestrator.status(run_id)["events"])
            g6_request = _approve_for_run(orchestrator, run_id, "G6", "diagnosis-hash")
            advanced = orchestrator.advance(
                run_id,
                "SPEC",
                {
                    "input_hash": "diagnosis-hash",
                    "approval_id": g6_request["approval_id"],
                    "artifacts": ["failure-bundle"],
                },
            )

        self.assertEqual(routed["state"], "DIAGNOSE")
        self.assertEqual(blocked["reason_code"], "APPROVAL_REQUIRED")
        self.assertEqual(wrong_gate["reason_code"], "APPROVAL_REQUIRED")
        self.assertEqual(event_count_after_blocks, event_count)
        self.assertEqual(advanced["state"], "SPEC")

    def test_diagnose_to_architecture_review_requires_g6(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            orchestrator.state.transition(run_id, "DIAGNOSE", {"test_setup": True})
            event_count = len(orchestrator.status(run_id)["events"])

            blocked = orchestrator.advance(
                run_id,
                "ARCHITECTURE_REVIEW",
                {
                    "input_hash": "repair-hash",
                    "approved_input_hash": "repair-hash",
                    "artifacts": ["failure-bundle"],
                },
            )

            event_count_after = len(orchestrator.status(run_id)["events"])

        self.assertEqual(blocked["reason_code"], "APPROVAL_REQUIRED")
        self.assertEqual(event_count_after, event_count)

    def test_grill_to_spec_keeps_its_g1_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            orchestrator.state.transition(run_id, "GRILL", {"test_setup": True})
            g1_request = _approve_for_run(orchestrator, run_id, "G1", "grill-hash")

            advanced = orchestrator.advance(
                run_id,
                "SPEC",
                {
                    "input_hash": "grill-hash",
                    "approval_id": g1_request["approval_id"],
                    "artifacts": ["grill"],
                },
            )

        self.assertEqual(advanced["state"], "SPEC")

    def test_ipipe_gate_is_g7_from_submit_and_g8_from_environment_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            submit_run = _start(orchestrator)["run_id"]
            recovery_run = _start(orchestrator, "BGW-2")["run_id"]
            orchestrator.state.transition(submit_run, "SUBMIT", {"test_setup": True})
            orchestrator.state.transition(recovery_run, "ENVIRONMENT_BLOCKED", {"test_setup": True})
            g7 = _approve_for_run(orchestrator, submit_run, "G7", "submit-hash")
            wrong_recovery_gate = _approve_for_run(orchestrator, recovery_run, "G7", "recovery-hash")
            g8 = _approve_for_run(orchestrator, recovery_run, "G8", "recovery-hash")

            submit_advanced = orchestrator.advance(
                submit_run,
                "IPIPE",
                {
                    "input_hash": "submit-hash",
                    "approval_id": g7["approval_id"],
                    "artifacts": ["submission"],
                },
            )
            recovery_event_count = len(orchestrator.status(recovery_run)["events"])
            recovery_wrong_gate = orchestrator.advance(
                recovery_run,
                "IPIPE",
                {
                    "input_hash": "recovery-hash",
                    "approval_id": wrong_recovery_gate["approval_id"],
                    "artifacts": ["submission"],
                },
            )
            recovery_event_count_after_wrong_gate = len(orchestrator.status(recovery_run)["events"])
            recovery_advanced = orchestrator.advance(
                recovery_run,
                "IPIPE",
                {
                    "input_hash": "recovery-hash",
                    "approval_id": g8["approval_id"],
                    "artifacts": ["submission"],
                },
            )

        self.assertEqual(submit_advanced["state"], "IPIPE")
        self.assertEqual(recovery_wrong_gate["reason_code"], "APPROVAL_REQUIRED")
        self.assertEqual(recovery_event_count_after_wrong_gate, recovery_event_count)
        self.assertEqual(recovery_advanced["state"], "IPIPE")

    def test_a_mis_cut_dag_can_be_re_cut_from_workspace_but_not_for_free(self):
        """A DAG defect surfaces at binding time, so WORKSPACE must be able to go back.

        The edge exists only to avoid discarding a run over a re-cuttable DAG; it must
        not hand out a rewind, so re-entry still needs the G2-approved spec and the new
        DAG still has to win its own G3.
        """
        from evidence_policy import requirement_for
        from transition_policy import TransitionPolicy

        policy = TransitionPolicy()

        self.assertTrue(policy.validate("WORKSPACE", "TASKS")["allowed"])
        self.assertTrue(policy.validate("WORKSPACE", "PLAN")["allowed"])
        self.assertEqual(
            policy.validate("WORKSPACE", "IMPLEMENT")["reason_code"], "INVALID_TRANSITION"
        )
        requirement = requirement_for("TASKS", "WORKSPACE")
        self.assertEqual((requirement.artifacts, requirement.approval_gate), (("spec",), "G2"))

    def test_a_defect_found_on_the_cr_can_be_repaired_instead_of_built(self):
        """SUBMIT has to be able to reach DIAGNOSE, because the CR is where second opinions arrive.

        The platform's own review is taken *after* SUBMIT by design, and a human
        reviewer comments on the same CR. With `SUBMIT -> {IPIPE}` as the only edge, a
        confirmed defect at that point left two options: run the pipelines over code
        somebody had just said was wrong, or discard a run holding seven approved
        phases. Neither is a repair.

        The edge goes to DIAGNOSE and not to IMPLEMENT: a finding is root-caused before
        anything is changed, which is the same rule the REVIEW edge follows.
        """
        from transition_policy import TransitionPolicy

        policy = TransitionPolicy()

        self.assertTrue(policy.validate("SUBMIT", "DIAGNOSE")["allowed"])
        self.assertTrue(policy.validate("SUBMIT", "IPIPE")["allowed"])
        self.assertTrue(policy.validate("SUBMIT", "WORKSPACE")["allowed"])
        self.assertEqual(
            policy.validate("SUBMIT", "IMPLEMENT")["reason_code"], "INVALID_TRANSITION"
        )
        self.assertEqual(
            policy.failure_target("REVIEW_FAILED"), "DIAGNOSE"
        )

    def test_route_failure_rejects_an_illegal_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            started = _start(orchestrator)

            result = orchestrator.route_failure(
                started["run_id"], "CODE_FAILURE", {"signature": "c1"}
            )

        self.assertEqual(result["reason_code"], "INVALID_TRANSITION")
        self.assertEqual(result["state"], "INTAKE")

    def test_failure_routes_do_not_mix_environment_and_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            code_run = _start(orchestrator)["run_id"]
            env_run = _start(orchestrator, "BGW-2")["run_id"]
            changed_run = _start(orchestrator, "BGW-3")["run_id"]
            orchestrator.state.transition(code_run, "REVIEW", {"test_setup": True})
            orchestrator.state.transition(env_run, "IPIPE", {"test_setup": True})

            code = orchestrator.route_failure(code_run, "CODE_FAILURE", {"signature": "c1"})
            environment = orchestrator.route_failure(
                env_run,
                "ENV_UNSATISFIED",
                {"environment_fingerprint": "env-bad"},
            )
            changed = orchestrator.route_failure(
                changed_run,
                "REQUIREMENT_CHANGED",
                {"content_hash": "new"},
            )

        self.assertEqual(code["state"], "DIAGNOSE")
        self.assertEqual(environment["state"], "ENVIRONMENT_BLOCKED")
        self.assertEqual(changed["state"], "GRILL")


class SubmitDescriptorWiringTests(unittest.TestCase):
    """A passed Review must archive the descriptor the iCode boundary binds to."""

    def _orchestrator(self, root: Path, completion: dict):
        _write_profile(root, PROFILE)
        orchestrator = Orchestrator(root)

        class _Protocol:
            def complete(self, run_id, envelope):
                return completion

        orchestrator.phase_protocol = lambda *args, **kwargs: _Protocol()
        return orchestrator

    def test_passed_review_builds_the_descriptor(self):
        import submit_descriptor

        calls = []
        original = submit_descriptor.build_and_archive
        submit_descriptor.build_and_archive = lambda orchestrator, run_id, task_id: (
            calls.append((run_id, task_id)) or {"ok": True, "reason_code": "OK"}
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = self._orchestrator(
                    Path(directory),
                    {"ok": True, "phase_complete": True, "phase": "REVIEW", "task_id": "T1"},
                )
                result = orchestrator.complete_phase("run-1", {}, knowledge_sync=object())
        finally:
            submit_descriptor.build_and_archive = original

        self.assertEqual(calls, [("run-1", "T1")])
        self.assertEqual(result["submit_descriptor"]["reason_code"], "OK")

    def test_other_phases_and_failed_completions_build_nothing(self):
        import submit_descriptor

        calls = []
        original = submit_descriptor.build_and_archive
        submit_descriptor.build_and_archive = lambda orchestrator, run_id, task_id: (
            calls.append((run_id, task_id)) or {"ok": True, "reason_code": "OK"}
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                implement = self._orchestrator(
                    root / "a",
                    {"ok": True, "phase_complete": True, "phase": "IMPLEMENT", "task_id": "T1"},
                ).complete_phase("run-1", {}, knowledge_sync=object())
                rejected = self._orchestrator(
                    root / "b",
                    {"ok": False, "phase_complete": False, "reason_code": "SCHEMA_INVALID"},
                ).complete_phase("run-2", {}, knowledge_sync=object())
        finally:
            submit_descriptor.build_and_archive = original

        self.assertEqual(calls, [])
        self.assertNotIn("submit_descriptor", implement)
        self.assertNotIn("submit_descriptor", rejected)


class SkippedSubmitRecoveryTests(unittest.TestCase):
    """A WORKSPACE that skipped SUBMIT is moved onto the reviewed task."""

    def test_next_recovers_a_review_pass_that_jumped_to_workspace(self):
        from test_phase_protocol_repair import final_envelope, two_node_dag
        from test_schema_validation import specialized_examples

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            revisions = {"business": "r2", "tests": "t2"}
            orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "TASKS", None, two_node_dag(),
            ))
            review = specialized_examples()["review"]
            orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "REVIEW", "T-1", review, revisions=revisions,
            ))
            content = json.dumps({
                "run_id": run_id, "change_set_id": "CS-1", "revision_set_id": "RS-T-1",
                "repo_path": "/tmp/T-1", "module": "baidu/team/T-1", "target_branch": "main",
                "commit_revision": "rev-T-1", "card_id": "BGW-1", "owner": "dev",
                "revision_set": {
                    "business": {"module": "baidu/team/T-1", "revision": "rev-T-1", "branch": "main"},
                    "test": {"module": "baidu/team/T-1-tests", "revision": "test-rev", "branch": "main"},
                },
            }, sort_keys=True).encode()
            orchestrator.artifacts.put(run_id, "change-set", content, {
                "verdict": "PASS", "task_id": "T-1", "revision_set_id": "RS-T-1",
            })
            orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "IMPLEMENT", "T-1", specialized_examples()["change-set"],
                revisions=revisions,
            ))
            orchestrator.state.transition(run_id, "WORKSPACE", {
                "previous_state": "REVIEW", "task_id": "T-2", "profile_hash": "a" * 64,
            })

            action = orchestrator.next(run_id)
            status = orchestrator.status(run_id)

        self.assertTrue(action.get("ok"), action)
        self.assertEqual(status["state"], "SUBMIT")
        self.assertEqual(action["controller"], "submit")
        self.assertEqual(action.get("task_id"), "T-1")
        self.assertEqual(status["events"][-1]["payload"]["reason_code"], "SKIPPED_SUBMIT_RECOVERED")

    def test_next_does_not_recover_an_ordinary_workspace_binding(self):
        from test_phase_protocol_repair import final_envelope, two_node_dag

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "TASKS", None, two_node_dag(),
            ))
            orchestrator.state.transition(run_id, "WORKSPACE", {
                "previous_state": "TASKS", "profile_hash": "a" * 64,
            })

            action = orchestrator.next(run_id)
            status = orchestrator.status(run_id)

        self.assertTrue(action.get("ok"), action)
        self.assertEqual(status["state"], "WORKSPACE")
        self.assertEqual(action["controller"], "workspace")


class RebuiltChangeSetRecoveryTests(unittest.TestCase):
    """Recovery must be local, idempotent, and bind IMPLEMENT to one Plan."""

    def test_recovery_pins_the_selected_plan_not_a_newer_plan(self):
        from test_phase_protocol_repair import final_envelope
        from test_schema_validation import specialized_examples

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            first = orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "PLAN", "T-1", specialized_examples()["task-plan"],
                revisions={"business": "r1", "tests": "t1"},
            ))
            newer_plan = specialized_examples()["task-plan"]
            newer_plan["g4_input_hash"] = "b" * 64
            newer = orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "PLAN", "T-1", newer_plan,
                revisions={"business": "r2", "tests": "t2"},
            ))
            checkpoint = orchestrator.state.transition(run_id, "PLAN", {
                "previous_state": "DIAGNOSE", "task_id": "T-1",
            })

            recovered = orchestrator.recover_rebuilt_change_set(
                run_id, "T-1", first["artifact_id"]
            )
            action = orchestrator.next(run_id)
            replay = orchestrator.recover_rebuilt_change_set(
                run_id, "T-1", first["artifact_id"]
            )

        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["state"], "IMPLEMENT")
        self.assertEqual(replay["event_id"], recovered["event_id"])
        self.assertEqual(action["task_id"], "T-1")
        self.assertEqual(action["parent_artifact_hash"], first["envelope"]["content_hash"])
        self.assertNotEqual(action["parent_artifact_hash"], newer["envelope"]["content_hash"])
        self.assertEqual(checkpoint["state"], "PLAN")

    def test_recovery_rejects_foreign_plan_without_transition(self):
        from test_phase_protocol_repair import final_envelope
        from test_schema_validation import specialized_examples

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_profile(root, PROFILE)
            orchestrator = Orchestrator(root)
            run_id = _start(orchestrator)["run_id"]
            foreign = orchestrator.artifacts.put_envelope(final_envelope(
                "other-run", "PLAN", "T-1", specialized_examples()["task-plan"],
                revisions={"business": "r1", "tests": "t1"},
            ))
            orchestrator.state.transition(run_id, "PLAN", {"task_id": "T-1"})

            result = orchestrator.recover_rebuilt_change_set(run_id, "T-1", foreign["artifact_id"])
            state = orchestrator.status(run_id)["state"]

        self.assertEqual(result["reason_code"], "PLAN_PREDECESSOR_INVALID")
        self.assertEqual(state, "PLAN")


class StaleRebuiltPlanRecoveryTests(unittest.TestCase):
    """A rebuilt QA revision must restart at an unapproved, immutable PLAN."""

    def _fixture(self, state="SUBMIT"):
        from test_phase_protocol_repair import final_envelope
        from test_schema_validation import specialized_examples

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        _write_profile(root, PROFILE)
        orchestrator = Orchestrator(root)
        run_id = _start(orchestrator)["run_id"]
        source = orchestrator.artifacts.put_envelope(final_envelope(
            run_id, "PLAN", "T3", specialized_examples()["task-plan"],
            revisions={"business": "old-business", "tests": "old-tests"},
        ))
        orchestrator.state.transition(run_id, "PLAN", {"task_id": "T3"})
        orchestrator.state.transition(run_id, "IMPLEMENT", {
            "task_id": "T3", "plan_artifact_id": source["artifact_id"],
            "plan_content_hash": source["envelope"]["content_hash"],
        })
        if state == "SUBMIT":
            orchestrator.state.transition(run_id, "SUBMIT", {"task_id": "T3"})
        return orchestrator, run_id, source

    def test_recovery_creates_a_new_unapproved_plan_and_preserves_source(self):
        orchestrator, run_id, source = self._fixture()
        revisions = {
            "business": "008f38ba",
            "tests": "8696dccb13487a1920c4efd468bbdff9ae8df495",
        }

        recovered = orchestrator.recover_stale_rebuilt_plan(
            run_id, "T3", "SUBMIT", source["artifact_id"], revisions
        )
        replay = orchestrator.recover_stale_rebuilt_plan(
            run_id, "T3", "SUBMIT", source["artifact_id"], revisions
        )
        replacement = orchestrator.artifacts.phase_artifact(recovered["plan_artifact_id"])["envelope"]
        original = orchestrator.artifacts.phase_artifact(source["artifact_id"])["envelope"]

        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["state"], "PLAN")
        self.assertEqual(replay["event_id"], recovered["event_id"])
        self.assertNotEqual(recovered["plan_artifact_id"], source["artifact_id"])
        self.assertEqual(replacement["source_revisions"], revisions)
        self.assertIsNone(replacement["approval_id"])
        self.assertIsNone(replacement["approval_input_hash"])
        self.assertEqual(
            {entry["role"]: entry["revision"] for entry in replacement["content"]["repositories"]},
            revisions,
        )
        self.assertEqual(original["source_revisions"], {"business": "old-business", "tests": "old-tests"})
        self.assertEqual(orchestrator.status(run_id)["events"][-1]["payload"]["approval_required"], "G4")
        # The command itself never auto-approves; the recovery event explicitly leaves G4 pending.
        self.assertEqual(recovered["approval_required"], "G4")
        self.assertIsNone(replacement["approval_id"])

    def test_recovery_fails_closed_when_current_state_or_pinned_plan_does_not_match(self):
        orchestrator, run_id, source = self._fixture("IMPLEMENT")
        revisions = {"business": "b-new", "tests": "t-new"}

        wrong_state = orchestrator.recover_stale_rebuilt_plan(
            run_id, "T3", "SUBMIT", source["artifact_id"], revisions
        )
        wrong_plan = orchestrator.recover_stale_rebuilt_plan(
            run_id, "T3", "IMPLEMENT", "not-the-pinned-plan", revisions
        )

        self.assertEqual(wrong_state["reason_code"], "RECOVERY_STATE_MISMATCH")
        self.assertEqual(wrong_plan["reason_code"], "RECOVERY_PLAN_MISMATCH")
        self.assertEqual(orchestrator.status(run_id)["state"], "IMPLEMENT")



class MultiRepoSubmitFrontierTests(unittest.TestCase):
    """SUBMIT may only hand over to IPIPE once every reviewed change set is in."""

    def _orchestrator(self, root):
        _write_profile(root, PROFILE)
        return Orchestrator(root)

    @staticmethod
    def _descriptor(orchestrator, run_id, task_id, change_set_id, verdict="PASS"):
        # A whole descriptor, not the one key this test reads: the store validates
        # `change-set` content now, and a fixture that skips the other nine fields
        # would be asserting that an unsubmittable descriptor is archivable.
        content = json.dumps({
            "run_id": run_id, "change_set_id": change_set_id, "revision_set_id": f"RS-{task_id}",
            "repo_path": f"/tmp/{task_id}", "module": f"baidu/team/{task_id}", "target_branch": "main",
            "commit_revision": f"rev-{task_id}", "card_id": "BGW-1", "owner": "dev",
            "revision_set": {
                "business": {"module": f"baidu/team/{task_id}", "revision": f"rev-{task_id}", "branch": "main"},
                "test": {"module": f"baidu/team/{task_id}-tests", "revision": "test-rev", "branch": "main"},
            },
        }, sort_keys=True).encode()
        review = orchestrator.artifacts.latest_phase(run_id, "REVIEW", task_id)
        return orchestrator.artifacts.put(run_id, "change-set", content, {
            "verdict": verdict, "task_id": task_id, "revision_set_id": f"RS-{task_id}",
            **({"reviewed_artifact_id": review["artifact_id"]} if review.get("valid") else {}),
        })

    @staticmethod
    def _submission(orchestrator, run_id, change_set_id, module):
        revision_set_id = "RS"
        for artifact in orchestrator.artifacts.artifacts_for_run(run_id):
            if artifact.get("kind") == "change-set":
                descriptor = json.loads(artifact["content"])
                if descriptor["change_set_id"] == change_set_id:
                    revision_set_id = descriptor["revision_set_id"]
        return orchestrator.artifacts.put(run_id, "submission", b"{}", {
            "change_set_id": change_set_id, "revision_set_id": revision_set_id,
            "controller_binding": {"module": module, "pipeline_id": "p"},
        })

    def test_the_frontier_is_outstanding_until_every_task_is_submitted(self):
        from orchestrator import _outstanding_submissions, _reviewed_tasks

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = self._orchestrator(Path(directory))
            run_id = _start(orchestrator)["run_id"]
            self._descriptor(orchestrator, run_id, "T0", "CS-0")
            self._descriptor(orchestrator, run_id, "T1", "CS-1")

            reviewed = _reviewed_tasks(orchestrator, run_id)
            none_yet = _outstanding_submissions(orchestrator, run_id)
            self._submission(orchestrator, run_id, "CS-1", "bgw")
            half = _outstanding_submissions(orchestrator, run_id)
            self._submission(orchestrator, run_id, "CS-0", "bgw-second")
            done = _outstanding_submissions(orchestrator, run_id)

        self.assertEqual(reviewed, ["T0", "T1"])
        self.assertEqual(none_yet, ["T0", "T1"])
        self.assertEqual(half, ["T0"])
        self.assertEqual(done, [])

    def test_a_rejected_change_set_is_not_owed_to_icode(self):
        from orchestrator import _outstanding_submissions

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = self._orchestrator(Path(directory))
            run_id = _start(orchestrator)["run_id"]
            self._descriptor(orchestrator, run_id, "T0", "CS-0")
            self._descriptor(orchestrator, run_id, "T1", "CS-1", verdict="REJECT")
            self._submission(orchestrator, run_id, "CS-0", "bgw")

            outstanding = _outstanding_submissions(orchestrator, run_id)

        self.assertEqual(outstanding, [])

    def test_a_repaired_task_owes_icode_its_new_change_set_not_its_old_receipt(self):
        """The join is against the task's current change set, not any it ever passed with.

        A repair out of SUBMIT gives a task a second passing change set. Matching on
        "some change set of this task was submitted" would let the first one's receipt
        answer for the second: the run would transition to IPIPE on a sibling task's
        submission while the repaired code sat unsubmitted, and the pipelines would
        build the defect the repair existed to remove.
        """
        from orchestrator import _outstanding_submissions

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = self._orchestrator(Path(directory))
            run_id = _start(orchestrator)["run_id"]
            self._descriptor(orchestrator, run_id, "T0", "CS-0")
            self._submission(orchestrator, run_id, "CS-0", "bgw")
            settled = _outstanding_submissions(orchestrator, run_id)

            self._descriptor(orchestrator, run_id, "T0", "CS-0-repaired")
            after_repair = _outstanding_submissions(orchestrator, run_id)
            self._submission(orchestrator, run_id, "CS-0-repaired", "bgw")
            resubmitted = _outstanding_submissions(orchestrator, run_id)

        self.assertEqual(settled, [])
        self.assertEqual(after_repair, ["T0"])
        self.assertEqual(resubmitted, [])

    def test_a_run_without_descriptors_keeps_the_single_submission_behaviour(self):
        from orchestrator import _outstanding_submissions

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = self._orchestrator(Path(directory))
            run_id = _start(orchestrator)["run_id"]

            outstanding = _outstanding_submissions(orchestrator, run_id)

        self.assertEqual(outstanding, [])

    def test_an_amended_dag_keeps_unreviewed_nodes_outstanding_after_a_sibling_submit(self):
        """A Review older than the current DAG cannot answer IPIPE for that node.

        BGW-1956 T0's empty submit otherwise saw T1/T2 PASS descriptors from before
        the amended DAG, T3's latest Review as REJECT, and jumped to IPIPE.
        """
        from orchestrator import _outstanding_submissions
        from test_phase_protocol_repair import final_envelope, two_node_dag
        from test_schema_validation import specialized_examples

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = self._orchestrator(Path(directory))
            run_id = _start(orchestrator)["run_id"]
            revisions = {"business": "r2", "tests": "t2"}
            orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "TASKS", None, two_node_dag(),
            ))
            for task_id in ("T-1", "T-2"):
                orchestrator.artifacts.put_envelope(final_envelope(
                    run_id, "REVIEW", task_id, specialized_examples()["review"],
                    revisions=revisions,
                ))
            amended = two_node_dag()
            amended["nodes"][1]["capability_slice"] += " plus the amended scope"
            orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "TASKS", None, amended,
            ))
            current_t1 = copy.deepcopy(specialized_examples()["review"])
            current_t1["provider"] = {
                "kind": "source-only", "identity": "review-provider-after-amendment",
            }
            orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "REVIEW", "T-1", current_t1, revisions=revisions,
            ))
            self._descriptor(orchestrator, run_id, "T-1", "CS-1")
            self._descriptor(orchestrator, run_id, "T-2", "CS-2")
            self._submission(orchestrator, run_id, "CS-1", "bgw")

            outstanding = _outstanding_submissions(orchestrator, run_id)

        self.assertEqual(outstanding, ["T-2"])

    def test_submit_followup_stays_in_submit_until_reviewed_sets_are_in(self):
        from orchestrator import _submit_followup_state

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = self._orchestrator(Path(directory))
            run_id = _start(orchestrator)["run_id"]
            self._descriptor(orchestrator, run_id, "T0", "CS-0")
            self._descriptor(orchestrator, run_id, "T1", "CS-1")
            before = _submit_followup_state(orchestrator, run_id)
            self._submission(orchestrator, run_id, "CS-0", "bgw")
            half = _submit_followup_state(orchestrator, run_id)
            self._submission(orchestrator, run_id, "CS-1", "bgw-second")
            done = _submit_followup_state(orchestrator, run_id)

        self.assertEqual(before, "SUBMIT")
        self.assertEqual(half, "SUBMIT")
        self.assertEqual(done, "IPIPE")

    def test_submit_followup_returns_to_workspace_when_open_nodes_remain(self):
        from orchestrator import _submit_followup_state
        from test_phase_protocol_repair import final_envelope, two_node_dag
        from test_schema_validation import specialized_examples

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = self._orchestrator(Path(directory))
            run_id = _start(orchestrator)["run_id"]
            revisions = {"business": "r2", "tests": "t2"}
            orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "TASKS", None, two_node_dag(),
            ))
            orchestrator.artifacts.put_envelope(final_envelope(
                run_id, "REVIEW", "T-1", specialized_examples()["review"],
                revisions=revisions,
            ))
            self._descriptor(orchestrator, run_id, "T-1", "CS-1")
            self._submission(orchestrator, run_id, "CS-1", "bgw")

            followup = _submit_followup_state(orchestrator, run_id)

        self.assertEqual(followup, "WORKSPACE")

    def test_the_ipipe_payload_names_the_primary_repository_not_the_last_one(self):
        from orchestrator import _primary_submission, _recorded_submissions

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = self._orchestrator(Path(directory))
            run_id = _start(orchestrator)["run_id"]
            primary_module = PROFILE["business_repos"][0]["module"]
            self._submission(orchestrator, run_id, "CS-1", "baidu/other/second")
            last = self._submission(orchestrator, run_id, "CS-0", primary_module)
            submissions = _recorded_submissions(orchestrator, run_id)

            chosen = _primary_submission(
                {"business_repos": [{"module": primary_module}]}, submissions,
                {"module": "baidu/other/second"}, last,
            )

        self.assertEqual([item["change_set_id"] for item in submissions], ["CS-1", "CS-0"])
        self.assertEqual(chosen["controller_binding"]["module"], primary_module)


class PerModulePipelineIdentityTests(unittest.TestCase):
    """A cross-repository run has one pipeline per module, not one per run."""

    REGISTERED = {
        "pipeline_id": "348102",
        "pipelines": [
            {"module": "baidu/sysip/x86bgw", "pipeline_id": "348102",
             "required_for_release": True},
            {"module": "baidu/sysip/bgwagent", "pipeline_id": "348142",
             "required_for_release": True},
            {"module": "baidu/nsiqa/x86bgw", "pipeline_id": "504074",
             "required_for_release": False},
        ],
    }

    def test_each_module_resolves_to_its_own_pipeline(self):
        from phase_protocol import _registered_pipeline

        resolved = [
            _registered_pipeline(self.REGISTERED, "baidu/sysip/x86bgw"),
            _registered_pipeline(self.REGISTERED, "baidu/sysip/bgwagent"),
            _registered_pipeline(self.REGISTERED, "baidu/nsiqa/x86bgw"),
        ]

        self.assertEqual(resolved, ["348102", "348142", "504074"])

    def test_a_profile_registering_none_still_answers_with_its_single_pipeline(self):
        from phase_protocol import _registered_pipeline

        legacy = {"pipeline_id": "bgw-pipeline"}

        self.assertEqual(_registered_pipeline(legacy, "any/module"), "bgw-pipeline")
        self.assertEqual(_registered_pipeline(self.REGISTERED, "not/registered"), "348102")

    def test_only_the_release_gating_modules_are_required(self):
        from phase_protocol import _required_modules

        self.assertEqual(_required_modules(self.REGISTERED), [
            "baidu/sysip/bgwagent", "baidu/sysip/x86bgw",
        ])
        self.assertEqual(_required_modules({"pipeline_id": "p"}), [])


class TestOnlySubmissionTests(unittest.TestCase):
    """A task may legitimately touch only the test repository.

    A Spec amendment that retires stale product cases has no business commit, so the
    submission is about the test repository. Both the descriptor consumer and the
    submission binding used to read `module`/`commit_revision` off the business entry
    unconditionally, which refused exactly those submissions.
    """

    PROFILE = {
        "business_repos": [
            {"path": "/repo/business", "module": "baidu/sysip/x86bgw", "branch": "feature"}
        ],
        "test_repo": {"path": "/repo/tests", "module": "baidu/nsiqa/x86bgw", "branch": "pipline_case"},
        "pipeline_profile": {
            "pipeline_id": "348102",
            "release_rule": "manual-approval",
            "pipelines": [
                {"module": "baidu/sysip/x86bgw", "pipeline_id": "348102", "release_rule": "manual-approval"},
                {"module": "baidu/nsiqa/x86bgw", "pipeline_id": "504074", "release_rule": "p0-only"},
            ],
        },
        "environment_profile": {"kind": "sandbox"},
    }
    REVISION_SET = {
        "business": {"module": "baidu/sysip/x86bgw", "revision": "b" * 40, "branch": "feature"},
        "test": {"module": "baidu/nsiqa/x86bgw", "revision": "t" * 40, "branch": "pipline_case"},
    }

    def _change_set(self, primary_role):
        entry = self.REVISION_SET["business" if primary_role == "business" else "test"]
        return {
            "module": entry["module"],
            "commit_revision": entry["revision"],
            "revision_set": self.REVISION_SET,
        }

    def _receipt(self, change_set):
        return {
            "module": change_set["module"],
            "commit_revision": change_set["commit_revision"],
            "patchset": change_set["commit_revision"],
            "revision_set": self.REVISION_SET,
        }

    def test_a_test_repository_submission_binds_to_its_own_pipeline(self):
        from orchestrator import _submission_controller_binding

        change_set = self._change_set("test")
        error, binding = _submission_controller_binding(
            self.PROFILE, self._receipt(change_set), change_set
        )

        self.assertIsNone(error)
        self.assertEqual(binding["module"], "baidu/nsiqa/x86bgw")
        self.assertEqual(binding["pipeline_id"], "504074")
        self.assertEqual(binding["release_rule"], "p0-only")
        self.assertEqual(
            binding["source_revisions"],
            {"business": "b" * 40, "tests": "t" * 40},
        )

    def test_a_business_submission_still_binds_to_the_business_pipeline(self):
        from orchestrator import _submission_controller_binding

        change_set = self._change_set("business")

        error, binding = _submission_controller_binding(
            self.PROFILE, self._receipt(change_set), change_set
        )

        self.assertIsNone(error)
        self.assertEqual(binding["module"], "baidu/sysip/x86bgw")
        self.assertEqual(binding["pipeline_id"], "348102")

    def test_a_module_revision_pair_in_neither_entry_is_refused(self):
        from orchestrator import _submission_controller_binding

        # 描述符声明的模块与提交必须真的是 revision_set 里的某一条，
        # 否则提交对象无从确定。
        stray = {
            "module": "baidu/nsiqa/x86bgw",
            "commit_revision": "c" * 40,
            "revision_set": self.REVISION_SET,
        }

        error, binding = _submission_controller_binding(
            self.PROFILE, self._receipt(stray), stray
        )

        self.assertEqual(error, "SOURCE_REVISION_MISMATCH")
        self.assertEqual(binding, {})


def _write_profile(root: Path, profile: dict) -> None:
    profile = copy.deepcopy(profile)
    business = root / "business"
    tests = root / "tests"
    language_skill = root / "language-skill"
    project_skill = root / "project-skill"
    for repository in (business, tests):
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
    for skill in (language_skill, project_skill):
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")
    profile["business_repos"][0]["path"] = str(business)
    profile["test_repo"]["path"] = str(tests)
    profile["language_skill"] = str(language_skill)
    profile["project_skill"] = str(project_skill)
    profile_dir = root / "config" / "projects"
    profile_dir.mkdir(parents=True)
    (profile_dir / "bgw.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
