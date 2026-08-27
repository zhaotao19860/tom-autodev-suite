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
    def test_explicit_config_root_keeps_profiles_under_config_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "config" / "projects" / "bgw.yaml"
            _write_profile(root, PROFILE)

            result = _start(Orchestrator(root))
            recorded = Orchestrator(root).status(result["run_id"])["events"][0]["payload"]

        self.assertEqual(recorded["profile_path"], str(expected))
        self.assertEqual(result["state"], "INTAKE")

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
