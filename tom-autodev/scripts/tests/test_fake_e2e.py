import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import Orchestrator
from orchestrator import main as cli_main
import worker_driver
from clients.icafe_client import CafeClient
from knowledge_sync import KnowledgeSync

from test_collaboration import FakeGroupClient
from test_ipipe_runtime import FakeApi, trigger_binding
from test_live_preflight import PROFILE
from test_schema_validation import specialized_examples


def snapshot(card_id):
    value = {
        "canonical_card_id": card_id, "title": f"Requirement {card_id}",
        "body": "behavior", "html": "<p>behavior</p>", "acceptance": ["AC-1"],
        "fields": {"priority": "P1"}, "attachments": [], "links": [], "status": "OPEN",
        "type": "REQUIREMENT", "responsible_people": [{"email": "owner@example.test"}],
        "created": {"user": {"email": "owner@example.test"}, "time": "2026-08-11T00:00:00+00:00"},
        "modified": {"user": {"email": "owner@example.test"}, "time": "2026-08-11T00:00:00+00:00"},
    }
    value["content_hash"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


def canonical_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class FakeKuBoundary:
    def __init__(self, project):
        self.project = project
        self.calls = []
        self.sequence = 0

    def ensure_run_root(self, parent_doc_id, title, markdown):
        self.calls.append(("ensure_run_root", parent_doc_id, title, markdown))
        doc_id = f"{self.project}-run-root"
        return {
            "ok": True, "reason_code": "OK", "doc_id": doc_id,
            "url": f"https://ku.baidu-int.com/knowledge/{doc_id}", "version": "v1",
            "evidence_refs": [f"ku:{doc_id}/v1"],
        }

    def create_artifact(self, parent_doc_id, title, markdown):
        self.sequence += 1
        self.calls.append(("create_artifact", parent_doc_id, title, markdown))
        doc_id = f"{self.project}-artifact-{self.sequence}"
        return {
            "ok": True, "reason_code": "OK", "doc_id": doc_id,
            "url": f"https://ku.baidu-int.com/knowledge/{doc_id}",
            "version": f"v{self.sequence}",
            "content_hash": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "evidence_refs": [f"ku:{doc_id}/v{self.sequence}"],
        }

    def update_index(self, doc_id, entry):
        self.calls.append(("update_index", doc_id, copy.deepcopy(entry)))
        version = f"index-{self.sequence}"
        return {
            "ok": True, "reason_code": "OK", "doc_id": doc_id, "version": version,
            "evidence_refs": [f"ku:{doc_id}/{version}"],
        }


class FakeCafeBoundary:
    def __init__(self):
        self.calls = []

    def comment(self, card_id, content, idempotency_key):
        self.calls.append((card_id, content, idempotency_key))
        comment_id = str(len(self.calls))
        return {
            "ok": True, "reason_code": "OK", "comment_id": comment_id,
            "evidence_refs": [f"icafe:{card_id}/{comment_id}"],
        }


class FakeIcodeRuntime:
    def __init__(self, state, run_id):
        self.state = state
        self.run_id = run_id

    def submit(self, change_set, approval):
        repo_path = str(Path(change_set["repo_path"]).expanduser().resolve())
        payload = {
            "run_id": self.run_id,
            "change_set_id": change_set["change_set_id"],
            "revision_set_id": change_set["revision_set_id"],
            "input_hash": change_set["input_hash"],
            "approval_id": approval["approval_id"],
            "repo_path": repo_path,
            "module": change_set["module"],
            "target_branch": change_set["target_branch"],
            "commit_revision": change_set["commit_revision"],
            "card_id": change_set["card_id"],
            "owner": change_set["owner"],
            "revision_set": copy.deepcopy(change_set["revision_set"]),
        }
        key = (
            f"icode.submit:{self.run_id}:{change_set['change_set_id']}:"
            f"{change_set['revision_set_id']}"
        )
        claim = self.state.claim_intent(self.run_id, "icode.submit", key, payload)
        response = {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "change_set_id": change_set["change_set_id"],
            "revision_set_id": change_set["revision_set_id"],
            "repo_path": repo_path,
            "module": change_set["module"],
            "target_branch": change_set["target_branch"],
            "commit_revision": change_set["commit_revision"],
            "revision_set": copy.deepcopy(change_set["revision_set"]),
            "change_number": "42",
            "patchset": change_set["commit_revision"],
            "cr_url": "https://icode.example/cr/42",
            "evidence_refs": ["icode-cr-42", "revision-r2", "revision-t2"],
        }
        self.state.receipt(claim["intent"]["intent_id"], response, response["evidence_refs"])
        return response


class StatefulKuTransport:
    skip_preflight = True

    def __init__(self, repo_id="sX0BTOBWJX"):
        self.repo_id = repo_id
        self.calls = []
        self.docs = {}
        self.sequence = 0

    def run(self, argv, **options):
        command = list(argv)
        self.calls.append((command, dict(options)))
        operation = command[1]
        argument = lambda name: command[command.index(name) + 1]
        if operation == "query-repo":
            parent = argument("--parent-doc-id")
            data = [
                self._info(doc_id, name_field="name")
                for doc_id, document in self.docs.items()
                if document["parent"] == parent
            ]
            return self._result({"count": len(data), "total": len(data), "data": data})
        if operation == "create-doc":
            self.sequence += 1
            doc_id = f"fake-doc-{self.sequence}"
            # The body arrives as a file rather than an argument: a change-set document
            # carries a unified diff, and putting that on the command line cost a real
            # document its entire body on 2026-09-09.
            self.docs[doc_id] = {
                "parent": argument("--parent-doc-id"),
                "title": argument("--title"),
                "text": Path(argument("--md-file")).read_text(encoding="utf-8"),
                "version": 1,
                "published": False,
            }
            return self._result(self._info(doc_id))
        if operation == "query-content":
            doc_id = argument("--doc-id")
            return self._result({**self._info(doc_id), "text": self.docs[doc_id]["text"]})
        if operation == "query-version":
            doc_id = argument("--doc-id")
            document = self.docs[doc_id]
            return self._result({
                "data": [{
                    "docGuid": doc_id,
                    "versionId": document["version"],
                    "initType": 0 if document["published"] else 4,
                }]
            })
        if operation == "publish-doc":
            doc_id = argument("--doc-id")
            self.docs[doc_id]["published"] = True
            self.docs[doc_id]["version"] += 1
            return self._result({"docGuid": doc_id})
        if operation == "edit-content":
            doc_id = argument("--doc-id")
            edit = json.loads(argument("--operation"))
            self.docs[doc_id]["text"] = (
                self.docs[doc_id]["text"].rstrip() + "\n\n" + edit["markdown"]
            )
            self.docs[doc_id]["published"] = False
            self.docs[doc_id]["version"] += 1
            return self._result({"docGuid": doc_id})
        raise AssertionError(f"unexpected KU operation: {command!r}")

    def _info(self, doc_id, name_field="title"):
        document = self.docs[doc_id]
        return {
            "docGuid": doc_id,
            name_field: document["title"],
            "repositoryGuid": self.repo_id,
            "url": f"https://ku.baidu-int.com/knowledge/space/category/{self.repo_id}/{doc_id}",
        }

    @staticmethod
    def _result(result):
        return {"returnCode": 200, "success": True, "result": result}


class StatefulCafeTransport:
    skip_preflight = True

    def __init__(self):
        self.calls = []
        self.comments = []

    def run(self, argv, **options):
        command = list(argv)
        self.calls.append((command, dict(options)))
        if command[1:3] == ["comment", "get"]:
            return {"status": 200, "success": True, "result": copy.deepcopy(self.comments)}
        if command[1:3] == ["comment", "create"]:
            content = command[command.index("--content") + 1]
            self.comments.append({"id": str(len(self.comments) + 1), "content": content})
            return {"status": 200, "success": True, "result": {"id": self.comments[-1]["id"]}}
        raise AssertionError(f"unexpected iCafe operation: {command!r}")


class FakeE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._write_profile("bgw", "I15ClP2KW4ZGAK", "tom-lang-c-cpp", "tom-project-bgw")
        self._write_profile("xflow", "meQ-Acjg0K09Xr", "tom-lang-npl", "tom-project-xflow")
        self.orchestrator = Orchestrator(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_bgw_and_xflow_enter_one_comate_controller_with_exact_project_binding(self):
        for project, card, parent, skill in (
            ("bgw", "BGW-101", "I15ClP2KW4ZGAK", None),
            ("xflow", "XFLOW-202", "meQ-Acjg0K09Xr", None),
        ):
            with self.subTest(project=project):
                started = self.orchestrator.start(card, project, requirement_snapshot=snapshot(card))
                action = self.orchestrator.next(started["run_id"])
                trace = self.orchestrator.trace(started["run_id"])

                self.assertEqual(action["host"], "comate")
                self.assertEqual(action["phase"], "INTAKE")
                self.assertEqual(trace["events"][0]["state"], "INTAKE")
                self.assertEqual(trace["project"]["project_id"], project)
                self.assertEqual(trace["project"]["ku_parent_doc_id"], parent)
                self.assertEqual(trace["artifacts"], [])
                self.assertEqual(trace["collaboration"], [])

    def test_bgw_and_xflow_traverse_integrated_controller_boundaries(self):
        results = {}
        for project, card, parent, decision in (
            ("bgw", "BGW-701", "I15ClP2KW4ZGAK", "REJECT"),
            ("xflow", "XFLOW-702", "meQ-Acjg0K09Xr", "APPROVE"),
        ):
            with self.subTest(project=project):
                results[project] = self._integrated_run(project, card, parent, decision)

        self.assertEqual(results["bgw"]["optimization"]["reason_code"], "G10_REJECTED")
        self.assertEqual(results["bgw"]["target"].read_text(encoding="utf-8"), "before\n")
        self.assertEqual(results["xflow"]["optimization"]["reason_code"], "OK")
        self.assertEqual(results["xflow"]["target"].read_text(encoding="utf-8"), "after\n")

    def test_trace_collects_artifacts_group_receipts_and_role_routing(self):
        started = self.orchestrator.start("BGW-303", "bgw", requirement_snapshot=snapshot("BGW-303"))
        run_id = started["run_id"]
        self.orchestrator.artifacts.put(run_id, "fixture-evidence", b"{}", {"source": "fake-e2e"})
        intent = self.orchestrator.state.intent(run_id, "collaboration.group.create", "fake-group", {"run_id": run_id})
        self.orchestrator.state.receipt(intent["intent_id"], {"group_id": "10001"}, ["group-10001"])
        self.orchestrator.state.transition(run_id, "REVIEW", {"fake_remote_evidence": True})

        code = self.orchestrator.route_failure(run_id, "CODE_FAILURE", {"classification": "CODE", "signature": "c1"})
        duplicate = self.orchestrator.route_failure(run_id, "CODE_FAILURE", {"classification": "CODE", "signature": "c1"})
        trace = self.orchestrator.trace(run_id)

        self.assertEqual(code, duplicate)
        self.assertEqual(code["collaboration_category"], "code")
        self.assertEqual([event["state"] for event in trace["events"]], ["INTAKE", "REVIEW", "DIAGNOSE"])
        self.assertEqual(trace["artifacts"][0]["kind"], "fixture-evidence")
        self.assertEqual(trace["collaboration"][0]["receipt"]["response"]["group_id"], "10001")
        self.assertEqual(trace["role_routing"][-1]["roles"], ["development"])

    def test_test_environment_and_mixed_failures_route_to_expected_people(self):
        cases = (
            ("TEST_FAILURE", "TEST", "test-case", ["test"]),
            ("ENV_UNSATISFIED", "ENVIRONMENT", "environment", ["test"]),
            ("PIPELINE_FAILURE", "MIXED", "mixed", ["development", "test"]),
        )
        for index, (reason, classification, expected, expected_roles) in enumerate(cases):
            with self.subTest(reason=reason):
                card = f"BGW-{400 + index}"
                result = self._integrated_run(
                    "bgw", card, "I15ClP2KW4ZGAK", "REJECT",
                    failure_reason=reason, failure_classification=classification,
                    stop_after_routing=True,
                )
                self.assertEqual(result["routed"]["collaboration_category"], expected)
                self.assertEqual(result["trace"]["role_routing"][-1]["roles"], expected_roles)
                self.assertEqual(result["at_users"], {
                    "development": ["dev@example.test"],
                    "test": ["tester@example.test"],
                    "development+test": ["dev@example.test", "tester@example.test"],
                }["+".join(expected_roles)])

    def test_pending_external_intent_blocks_next_until_recovery(self):
        result = self._integrated_run(
            "bgw", "BGW-500", "I15ClP2KW4ZGAK", "REJECT", stop_after_routing=True
        )

        self.assertEqual(result["blocked_after_restart"]["reason_code"], "RECOVERY_REQUIRED")
        self.assertFalse(result["blocked_after_restart"]["retry_allowed"])
        self.assertEqual(result["recovered_trigger"], result["replayed_trigger"])
        self.assertEqual(result["pending_after_recovery"], [])

    def _run_to_grill_action(self, card, parent, change_class):
        profile = self._integrated_profile("bgw")
        raw_snapshot = snapshot(card)
        raw_snapshot.pop("content_hash")
        cafe = CafeClient(fetcher=lambda requested: copy.deepcopy(raw_snapshot))
        requirement = cafe.snapshot(card)
        started = self.orchestrator.start(
            card, "bgw", requirement_snapshot=requirement, change_class=change_class
        )
        run_id = started["run_id"]
        collaboration = self.orchestrator.collaboration_session(
            FakeGroupClient(create_result={"group_id": f"group-{card}"})
        )
        prepared = collaboration.prepare_g0(
            run_id, "bgw", {"id": card, "title": requirement["title"]},
            profile["approval_channels"]["role_members"],
        )
        g0 = self._approval(run_id, "G0", prepared["input_hash"])
        collaboration.create(
            run_id, "bgw", {"id": card, "title": requirement["title"]},
            profile["approval_channels"]["role_members"],
            approval_id=g0["approval_id"], input_hash=prepared["input_hash"],
        )
        knowledge = KnowledgeSync(
            state_store=self.orchestrator.state, ku_client=FakeKuBoundary("bgw"),
            cafe_client=FakeCafeBoundary(), parent_doc_id=None, project_parent_doc_id=parent,
            run_root_title=f"{card}-{run_id[:12]}-fixture", card_id=card, run_id=run_id,
        )
        intake = self.orchestrator.next(run_id)
        self.assertEqual(intake["phase"], "INTAKE", intake)
        envelope = self._phase_envelope(intake, self._phase_content(intake, requirement), run_id)
        self.assertTrue(self.orchestrator.complete_phase(run_id, envelope, knowledge_sync=knowledge)["ok"])
        return run_id, knowledge, requirement

    def test_express_auto_derives_grill_while_standard_uses_the_model(self):
        # standard: GRILL is a full skill phase gated by G1.
        run_id, _knowledge, _req = self._run_to_grill_action("BGW-902", "I15ClP2KW4ZGAK", "standard")
        grill = self.orchestrator.next(run_id)
        self.assertEqual(
            (grill["phase"], grill["child_skill"], grill["required_human_gate"]),
            ("GRILL", "tom-grill", "G1"))
        self.assertNotIn("content", grill)

        # express (acceptance already present): GRILL is auto — no model, no G1, and the
        # controller supplies a NO_OPEN_DECISIONS decision-log that completes into SPEC.
        run_id, knowledge, _req = self._run_to_grill_action("BGW-901", "I15ClP2KW4ZGAK", "express")
        grill = self.orchestrator.next(run_id)
        self.assertEqual(grill["phase"], "GRILL")
        self.assertIsNone(grill["child_skill"])
        self.assertIsNone(grill["required_human_gate"])
        self.assertEqual(grill["content"]["decision_result"], "NO_OPEN_DECISIONS")

        envelope = self._phase_envelope(grill, self._phase_content(grill, _req), run_id)
        self.assertIsNone(envelope["approval_id"])
        completed = self.orchestrator.complete_phase(run_id, envelope, knowledge_sync=knowledge)
        self.assertTrue(completed["ok"], completed)
        self.assertEqual(self.orchestrator.next(run_id)["phase"], "SPEC")

        # SPEC stays a full gated phase for express; TASKS still runs the model but its
        # G3 gate is waived (ungated), so the DAG needs no separate human approval.
        spec_action = self.orchestrator.next(run_id)
        spec_env = self._phase_envelope(spec_action, self._phase_content(spec_action, _req), run_id)
        self.assertEqual(spec_action["required_human_gate"], "G2")
        self.assertTrue(self.orchestrator.complete_phase(run_id, spec_env, knowledge_sync=knowledge)["ok"])
        tasks = self.orchestrator.next(run_id)
        self.assertEqual((tasks["phase"], tasks["child_skill"]), ("TASKS", "tom-tasks"))
        self.assertIsNone(tasks["required_human_gate"])

    def test_express_falls_back_to_full_grill_when_acceptance_absent(self):
        # express is only auto when the card already carries acceptance; with none, GRILL
        # must fall back to the full skill path so the model can clarify.
        profile = self._integrated_profile("bgw")
        raw_snapshot = snapshot("BGW-903")
        raw_snapshot["acceptance"] = []
        raw_snapshot.pop("content_hash")
        cafe = CafeClient(fetcher=lambda requested: copy.deepcopy(raw_snapshot))
        requirement = cafe.snapshot("BGW-903")
        started = self.orchestrator.start(
            "BGW-903", "bgw", requirement_snapshot=requirement, change_class="express"
        )
        run_id = started["run_id"]
        collaboration = self.orchestrator.collaboration_session(
            FakeGroupClient(create_result={"group_id": "group-BGW-903"})
        )
        prepared = collaboration.prepare_g0(
            run_id, "bgw", {"id": "BGW-903", "title": requirement["title"]},
            profile["approval_channels"]["role_members"],
        )
        g0 = self._approval(run_id, "G0", prepared["input_hash"])
        collaboration.create(
            run_id, "bgw", {"id": "BGW-903", "title": requirement["title"]},
            profile["approval_channels"]["role_members"],
            approval_id=g0["approval_id"], input_hash=prepared["input_hash"],
        )
        knowledge = KnowledgeSync(
            state_store=self.orchestrator.state, ku_client=FakeKuBoundary("bgw"),
            cafe_client=FakeCafeBoundary(), parent_doc_id=None, project_parent_doc_id="I15ClP2KW4ZGAK",
            run_root_title=f"BGW-903-{run_id[:12]}-fixture", card_id="BGW-903", run_id=run_id,
        )
        intake = self.orchestrator.next(run_id)
        envelope = self._phase_envelope(intake, self._phase_content(intake, requirement), run_id)
        self.assertTrue(self.orchestrator.complete_phase(run_id, envelope, knowledge_sync=knowledge)["ok"])

        grill = self.orchestrator.next(run_id)
        self.assertEqual((grill["child_skill"], grill["required_human_gate"]), ("tom-grill", "G1"))
        self.assertNotIn("content", grill)

    def test_worker_classify_next_reads_the_frontier(self):
        # standard GRILL needs the model -> PRODUCER_WAIT.
        run_id, _knowledge, _req = self._run_to_grill_action("BGW-911", "I15ClP2KW4ZGAK", "standard")
        decision = worker_driver.classify_next(self.orchestrator, run_id)
        self.assertEqual(decision["kind"], worker_driver.PRODUCER_WAIT)
        self.assertEqual(decision["skill"], "tom-grill")

        # express GRILL is auto-derived -> AUTO_COMPLETE; after completing it the SPEC
        # frontier again needs the model -> PRODUCER_WAIT.
        run_id, knowledge, requirement = self._run_to_grill_action("BGW-912", "I15ClP2KW4ZGAK", "express")
        decision = worker_driver.classify_next(self.orchestrator, run_id)
        self.assertEqual(decision["kind"], worker_driver.AUTO_COMPLETE)

        grill = self.orchestrator.next(run_id)
        envelope = self._phase_envelope(grill, self._phase_content(grill, requirement), run_id)
        self.assertTrue(self.orchestrator.complete_phase(run_id, envelope, knowledge_sync=knowledge)["ok"])
        spec_decision = worker_driver.classify_next(self.orchestrator, run_id)
        self.assertEqual(spec_decision["kind"], worker_driver.PRODUCER_WAIT)
        self.assertEqual(spec_decision["skill"], "tom-spec")

    def test_worker_executes_auto_grill_without_an_agent_turn(self):
        # The worker builds the envelope itself and completes express auto-GRILL — no
        # agent turn, no approval — then the run sits at the SPEC frontier.
        run_id, knowledge, _req = self._run_to_grill_action("BGW-913", "I15ClP2KW4ZGAK", "express")
        result = worker_driver.execute_auto(self.orchestrator, run_id, knowledge_sync=knowledge)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.orchestrator.next(run_id)["phase"], "SPEC")

        # Safety: a standard run's GRILL needs the model, so execute_auto refuses it —
        # the worker can never use this path for a gated or model-authored phase.
        std_run, _k, _r = self._run_to_grill_action("BGW-914", "I15ClP2KW4ZGAK", "standard")
        refused = worker_driver.execute_auto(self.orchestrator, std_run)
        self.assertEqual(refused["reason_code"], "NOT_AUTO")

    def test_worker_advance_auto_completes_then_parks_at_the_model(self):
        # express: advance() auto-completes GRILL and parks at the SPEC model frontier;
        # it does not autonomously run controller side effects.
        run_id, knowledge, _req = self._run_to_grill_action("BGW-915", "I15ClP2KW4ZGAK", "express")
        result = worker_driver.advance(self.orchestrator, run_id, knowledge_sync=knowledge)
        self.assertEqual((result["ok"], result["reason_code"]), (True, "PARKED"))
        self.assertEqual(result["parked"], worker_driver.PRODUCER_WAIT)
        self.assertEqual(result["decision"]["skill"], "tom-spec")
        self.assertEqual([step["phase"] for step in result["auto_completed"]], ["GRILL"])
        self.assertEqual(self.orchestrator.next(run_id)["phase"], "SPEC")
        # Parking enqueued a durable, idempotent ProducerJob for the SPEC frontier.
        job = result["producer_job"]
        self.assertEqual((job["status"], job["payload"]["phase"]), ("PENDING", "SPEC"))
        self.assertEqual([j["job_id"] for j in self.orchestrator.state.pending_producer_jobs(run_id)], [job["job_id"]])
        again = worker_driver.advance(self.orchestrator, run_id, knowledge_sync=knowledge)
        self.assertEqual(again["producer_job"]["job_id"], job["job_id"])

        # standard: nothing is auto — advance() parks immediately at GRILL with no
        # auto-completed steps.
        std_run, _k, _r = self._run_to_grill_action("BGW-916", "I15ClP2KW4ZGAK", "standard")
        parked = worker_driver.advance(self.orchestrator, std_run)
        self.assertEqual(parked["parked"], worker_driver.PRODUCER_WAIT)
        self.assertEqual(parked["auto_completed"], [])

    def test_submit_draft_merges_spec_and_dag_for_express(self):
        # express run auto-advances to the merged SPEC producer frontier; its job says merged.
        run_id, knowledge, _req = self._run_to_grill_action("BGW-917", "I15ClP2KW4ZGAK", "express")
        parked = worker_driver.advance(self.orchestrator, run_id, knowledge_sync=knowledge)
        job_id = parked["producer_job"]["job_id"]
        self.assertEqual(parked["producer_job"]["payload"]["mode"], "merged")
        draft = {
            "spec": copy.deepcopy(specialized_examples()["spec"]),
            "dag": copy.deepcopy(specialized_examples()["task-dag"]),
        }

        # Without the G2 approval, the merged submit records the draft but refuses to
        # complete, returning the spec's hash to approve.
        pending = worker_driver.submit_draft(self.orchestrator, run_id, job_id, draft, knowledge_sync=knowledge)
        self.assertEqual((pending["reason_code"], pending["gate"]), ("APPROVAL_REQUIRED", "G2"))

        # After approving that one hash, the single call writes BOTH the spec and the
        # task-dag (TASKS ungated) and lands the frontier at WORKSPACE.
        self._approval(run_id, "G2", pending["approval_input_hash"])
        done = worker_driver.submit_draft(self.orchestrator, run_id, job_id, draft, knowledge_sync=knowledge)
        self.assertEqual((done["ok"], done["reason_code"]), (True, "MERGED_COMPLETE"))
        self.assertTrue(self.orchestrator.artifacts.latest_phase(run_id, "SPEC", None)["valid"])
        self.assertTrue(self.orchestrator.artifacts.latest_phase(run_id, "TASKS", None)["valid"])
        self.assertEqual(self.orchestrator.next(run_id)["state"], "WORKSPACE")

        # A malformed merged draft (missing dag) is refused before any write.
        run2, k2, _ = self._run_to_grill_action("BGW-918", "I15ClP2KW4ZGAK", "express")
        p2 = worker_driver.advance(self.orchestrator, run2, knowledge_sync=k2)
        bad = worker_driver.submit_draft(self.orchestrator, run2, p2["producer_job"]["job_id"],
                                         {"spec": copy.deepcopy(specialized_examples()["spec"])}, knowledge_sync=k2)
        self.assertEqual(bad["reason_code"], "MERGED_DRAFT_INVALID")

    def test_worker_executes_workspace_binding(self):
        # A run parked at WORKSPACE: the worker cuts the owned worktrees, derives the G4
        # binding hash, and — since G4 is not yet approved for it — returns the hash to
        # approve (worktree creation is local and reversible, so the prep runs first).
        run_id, knowledge = self._run_to_workspace("BGW-520", "bgw", "I15ClP2KW4ZGAK")
        need = worker_driver.execute_controller(self.orchestrator, run_id, knowledge_sync=knowledge)
        self.assertEqual((need["reason_code"], need["gate"]), ("APPROVAL_REQUIRED", "G4"))
        self.assertEqual(set(need["workspace_receipts"]), {"business", "tests"})

        # After approving that binding hash, the worker advances WORKSPACE -> PLAN itself,
        # and the frontier is the PLAN model phase.
        self._approval(run_id, "G4", need["approval_input_hash"])
        done = worker_driver.execute_controller(self.orchestrator, run_id, knowledge_sync=knowledge)
        self.assertTrue(done["ok"], done)
        self.assertEqual(self.orchestrator.next(run_id)["phase"], "PLAN")

    def test_worker_advance_chains_through_a_settled_workspace(self):
        # Once G4 is settled for the binding, advance() executes the WORKSPACE controller
        # itself and keeps going, parking at the next model frontier (PLAN).
        run_id, knowledge = self._run_to_workspace("BGW-540", "bgw", "I15ClP2KW4ZGAK")
        need = worker_driver.execute_controller(self.orchestrator, run_id, knowledge_sync=knowledge)
        self._approval(run_id, "G4", need["approval_input_hash"])
        result = worker_driver.advance(self.orchestrator, run_id, knowledge_sync=knowledge)
        self.assertEqual((result["ok"], result["reason_code"], result["parked"]),
                         (True, "PARKED", worker_driver.PRODUCER_WAIT))
        self.assertEqual(result["decision"]["skill"], "tom-plan")
        self.assertIn("workspace", [step.get("controller") for step in result["auto_completed"]])
        self.assertEqual(self.orchestrator.next(run_id)["phase"], "PLAN")

    def test_worker_resume_is_run_scoped_and_drives_forward(self):
        # No handoff -> nothing to resume.
        run_id, knowledge, _req = self._run_to_grill_action("BGW-560", "I15ClP2KW4ZGAK", "express")
        self.assertEqual(worker_driver.resume(self.orchestrator, run_id)["reason_code"], "NO_RESUME_HANDOFF")

        # A settled-approval resume handoff for THIS run: resume completes it and drives
        # the run forward (auto-GRILL) itself, parking at the SPEC model frontier. Scoping
        # to run_id is the fix for the Stop-hook's global scan (review #3).
        g0 = next(
            record for record in self.orchestrator.approvals.for_run(run_id)
            if record.get("action") == "G0" and record.get("effective_decision") == "APPROVE"
        )
        self.orchestrator.state.record_handoff(
            run_id, f"approval-resume-{g0['approval_id']}",
            {"kind": "APPROVAL_RESUME", "run_id": run_id,
             "approval_id": g0["approval_id"], "input_hash": g0["input_hash"]},
        )
        result = worker_driver.resume(self.orchestrator, run_id, knowledge_sync=knowledge)
        self.assertEqual((result["ok"], result["parked"]), (True, worker_driver.PRODUCER_WAIT))
        self.assertEqual(result["decision"]["skill"], "tom-spec")
        self.assertEqual(self.orchestrator.state.incomplete_handoffs(run_id), [])

    def _drive_to_submit(self, card):
        run_id, knowledge = self._run_to_workspace(card, "bgw", "I15ClP2KW4ZGAK")
        requirement = snapshot(card)
        receipts = self._owned_workspace_receipts(run_id, "bgw")
        binding = self.orchestrator.workspace_binding(run_id, receipts)
        approval = self._approval(run_id, "G4", binding["input_hash"])
        self.orchestrator.advance(run_id, "PLAN", {
            "input_hash": binding["input_hash"], "approval_id": approval["approval_id"],
            "artifacts": ["workspace", "task-plan"], "workspace_receipts": receipts,
        })
        reviewed_revisions = None
        for phase in ("PLAN", "IMPLEMENT", "REVIEW"):
            action = self.orchestrator.next(run_id)
            self.assertEqual(action["phase"], phase, action)
            if phase == "IMPLEMENT":
                reviewed_revisions = self._commit_reviewed_worktrees(receipts)
            content = self._phase_content(action, requirement, reviewed_revisions)
            envelope = self._phase_envelope(action, content, run_id)
            self.assertTrue(self.orchestrator.complete_phase(run_id, envelope, knowledge_sync=knowledge)["ok"])
        self.assertEqual(self.orchestrator.next(run_id)["controller"], "submit")
        return run_id, knowledge

    def test_worker_submits_to_icode_under_g7(self):
        # A run parked at SUBMIT: the worker derives the reviewed descriptor and, since G7
        # is not yet APPROVE for that descriptor, returns the exact hash to approve — it
        # never opens a CR without the gate.
        run_id, knowledge = self._drive_to_submit("BGW-530")
        need = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge,
            icode_runtime=FakeIcodeRuntime(self.orchestrator.state, run_id),
        )
        self.assertEqual(need["reason_code"], "APPROVAL_REQUIRED")

        # After the operator approves that descriptor, the worker submits to iCode itself
        # (fake runtime here) and the single-task frontier advances to IPIPE.
        self._approval(run_id, "G7", need["approval_input_hash"])
        done = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge,
            icode_runtime=FakeIcodeRuntime(self.orchestrator.state, run_id),
        )
        self.assertTrue(done["ok"], done)
        self.assertEqual(self.orchestrator.next(run_id)["controller"], "ipipe")


    def _drive_to_ipipe(self, card):
        """A run submitted to iCode under G7 and parked at the IPIPE controller."""
        run_id, knowledge = self._drive_to_submit(card)
        icode = FakeIcodeRuntime(self.orchestrator.state, run_id)
        need = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, icode_runtime=icode)
        self._approval(run_id, "G7", need["approval_input_hash"])
        submitted = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, icode_runtime=icode)
        self.assertTrue(submitted["ok"], submitted)
        self.assertEqual(self.orchestrator.next(run_id)["controller"], "ipipe")
        return run_id, knowledge

    def _ipipe_api(self, run_id, *, build_status="SUCCESS", stage_status="SUCCESS"):
        """A FakeApi whose one build matches the revision set the run actually submitted.

        Deriving the build from `_ipipe_revision_set` (rather than hard-coding revisions)
        keeps the fake honest: the worker's trigger only matches when the build carries the
        exact module/revision/pipeline the run pinned at SUBMIT.
        """
        from orchestrator import _ipipe_revision_set
        from phase_protocol import _registered_pipeline, _registered_release_rule

        profile = self.orchestrator._runtime_profile(run_id)["profile"]
        derived = _ipipe_revision_set(self.orchestrator, run_id, profile)
        self.assertTrue(derived["ok"], derived)
        repositories = derived["revisions"]["repositories"]
        primary = repositories[0]
        pipeline_id = _registered_pipeline(profile["pipeline_profile"], primary["module"])
        release_rule = _registered_release_rule(profile["pipeline_profile"], primary["module"])
        revision_map = {item["module"]: item["revision"] for item in repositories}
        api = FakeApi()
        api.pipeline = {"id": pipeline_id, "module": primary["module"]}
        api.trigger_result = {"id": "build-1"}
        api.builds["build-1"] = {
            "id": "build-1", "pipelineConfId": pipeline_id,
            "module": primary["module"], "revision": primary["revision"],
            "revisions": revision_map, "params": {}, "status": build_status,
            "stageBuilds": [{"id": "stage-1", "stageName": "compile", "status": stage_status}],
        }
        if build_status == "SUCCESS" and stage_status == "SUCCESS":
            # The published release the run's RELEASE step verifies (read-only). Seeding it
            # on the same api keeps the whole trigger -> monitor -> verify chain honest.
            api.releases = [{
                "id": "release-1", "module": primary["module"], "branch": primary["branch"],
                "pipelineBuildId": "build-1", "revisions": revision_map,
                "releaseRule": release_rule, "status": "SUCCESS",
            }]
        return api

    def test_worker_triggers_ipipe_under_g7_and_lands_release(self):
        # A run parked at IPIPE: the worker derives the trigger's OWN binding hash and,
        # since G7 is not yet APPROVE for that hash, returns it to approve — it never
        # triggers a pipeline without the gate. G7 authorizes submit AND trigger; G8 is
        # only the failed-stage re-run, so the trigger gate here is G7.
        run_id, knowledge = self._drive_to_ipipe("BGW-531")
        api = self._ipipe_api(run_id)
        need = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self.assertEqual((need["reason_code"], need["gate"]), ("APPROVAL_REQUIRED", "G7"))
        self.assertEqual(sum(call[0] == "trigger_by_revision" for call in api.calls), 0)

        # After approving that exact trigger hash, the worker triggers the pipeline itself,
        # monitors it to SUCCESS, ingests the evidence, and the run lands at RELEASE.
        self._approval(run_id, "G7", need["approval_input_hash"])
        done = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self.assertTrue(done["ok"], done)
        self.assertEqual((done["state"], done["monitor_status"]), ("RELEASE", "SUCCESS"))
        self.assertEqual(self.orchestrator.status(run_id)["state"], "RELEASE")
        self.assertEqual(sum(call[0] == "trigger_by_revision" for call in api.calls), 1)

    def test_worker_ipipe_failure_routes_to_diagnose(self):
        # A failed pipeline: the terminal evidence is ingested just the same, but a FAILURE
        # routes the run to DIAGNOSE rather than RELEASE.
        run_id, knowledge = self._drive_to_ipipe("BGW-532")
        api = self._ipipe_api(run_id, build_status="FAILING", stage_status="FAILED")
        need = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self._approval(run_id, "G7", need["approval_input_hash"])
        done = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self.assertTrue(done["ok"], done)
        self.assertEqual((done["state"], done["monitor_status"]), ("DIAGNOSE", "FAILURE"))
        self.assertEqual(self.orchestrator.status(run_id)["state"], "DIAGNOSE")

    def test_worker_ipipe_manual_wait_parks_for_a_human(self):
        # A manual pipeline gate is not the worker's call: the trigger runs, but a
        # non-terminal monitor result parks the run at IPIPE without fabricating evidence.
        run_id, knowledge = self._drive_to_ipipe("BGW-533")
        api = self._ipipe_api(run_id, build_status="RUNNING", stage_status="MANUAL")
        need = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self._approval(run_id, "G7", need["approval_input_hash"])
        parked = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self.assertEqual((parked["ok"], parked["reason_code"]), (True, "PARKED"))
        self.assertEqual((parked["parked"], parked["monitor_status"]), ("IPIPE_MONITOR", "MANUAL_WAIT"))
        self.assertEqual(self.orchestrator.status(run_id)["state"], "IPIPE")

    def test_worker_advance_chains_through_a_settled_ipipe(self):
        # With G7 settled for the trigger and an ipipe_api injected, advance() runs the
        # IPIPE controller itself (trigger + monitor + ingest) and keeps going, parking at
        # the RELEASE controller, which still needs its own runtime.
        run_id, knowledge = self._drive_to_ipipe("BGW-534")
        api = self._ipipe_api(run_id)
        need = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self._approval(run_id, "G7", need["approval_input_hash"])
        result = worker_driver.advance(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self.assertEqual((result["ok"], result["reason_code"]), (True, "PARKED"))
        self.assertEqual(result["controller"], "release")
        self.assertIn("ipipe", [step.get("controller") for step in result["auto_completed"]])
        self.assertEqual(self.orchestrator.status(run_id)["state"], "RELEASE")


    def _drive_to_release(self, card):
        """A run whose pipeline passed under G7, parked at the RELEASE controller."""
        run_id, knowledge = self._drive_to_ipipe(card)
        api = self._ipipe_api(run_id)
        need = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self._approval(run_id, "G7", need["approval_input_hash"])
        done = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self.assertEqual(done["state"], "RELEASE", done)
        return run_id, knowledge, api

    def test_worker_verifies_release_under_g9_and_reaches_terminal(self):
        # A run parked at RELEASE: G9 is the release gate. Until G9 is APPROVE for the
        # release action, the worker returns its hash to approve; once approved it verifies
        # the platform published the pinned build and records the evidence, reaching the
        # terminal RELEASE_SUCCESS.
        run_id, knowledge, api = self._drive_to_release("BGW-535")
        need = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self.assertEqual((need["reason_code"], need["gate"]), ("APPROVAL_REQUIRED", "G9"))

        self._approval(run_id, "G9", need["approval_input_hash"])
        done = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["state"], "RELEASE_SUCCESS")
        self.assertEqual(done["release_id"], "release-1")
        self.assertEqual(self.orchestrator.status(run_id)["state"], "RELEASE_SUCCESS")

    def test_worker_release_waiting_parks_until_platform_publishes(self):
        # G9 approved, but the platform has not published the release yet: verify_release is
        # read-only, so the worker parks (RELEASE_WAITING) rather than forcing a release.
        run_id, knowledge, api = self._drive_to_release("BGW-536")
        need = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self._approval(run_id, "G9", need["approval_input_hash"])
        api.releases = []  # nothing published yet
        parked = worker_driver.execute_controller(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self.assertEqual((parked["ok"], parked["reason_code"]), (True, "PARKED"))
        self.assertEqual(parked["parked"], "RELEASE_WAITING")
        self.assertEqual(self.orchestrator.status(run_id)["state"], "RELEASE")

    def test_worker_advance_chains_through_a_settled_release_to_terminal(self):
        # With G9 settled and an ipipe_api injected, advance() runs the RELEASE controller
        # itself and drives the run to the terminal RELEASE_SUCCESS.
        run_id, knowledge, api = self._drive_to_release("BGW-537")
        action = self.orchestrator.next(run_id)
        self._approval(run_id, "G9", action["input_hash"])
        result = worker_driver.advance(
            self.orchestrator, run_id, knowledge_sync=knowledge, ipipe_api=api)
        self.assertEqual((result["ok"], result["reason_code"]), (True, "PARKED"))
        self.assertIn("release", [step.get("controller") for step in result["auto_completed"]])
        self.assertEqual(self.orchestrator.status(run_id)["state"], "RELEASE_SUCCESS")

    def _worker_drive_to_terminal(self, card):
        """Drive one standard run GRILL->RELEASE_SUCCESS with the WorkerDriver as the only
        sequencer: it auto-runs every deterministic transition and parks exactly at each
        gate and ProducerJob. Returns the counts the plan's acceptance asserts."""
        run_id, knowledge, requirement = self._run_to_grill_action(card, "I15ClP2KW4ZGAK", "standard")
        icode = FakeIcodeRuntime(self.orchestrator.state, run_id)
        producers = 0
        gate_approvals = 1  # G0 was granted in _run_to_grill_action before the loop
        controllers_run: list[str] = []
        receipts = None
        reviewed_revisions = None
        api = None
        for _ in range(80):
            state = self.orchestrator.status(run_id)["state"]
            if state == "RELEASE_SUCCESS":
                break
            if api is None and state == "IPIPE":
                api = self._ipipe_api(run_id)
            result = worker_driver.advance(
                self.orchestrator, run_id, knowledge_sync=knowledge, icode_runtime=icode, ipipe_api=api)
            controllers_run += [
                step["controller"] for step in result.get("auto_completed", [])
                if step.get("controller")]
            if self.orchestrator.status(run_id)["state"] == "RELEASE_SUCCESS":
                break  # the RELEASE controller ran within this advance and landed terminal
            self.assertEqual(result["reason_code"], "PARKED", result)
            parked = result.get("parked")
            if parked == worker_driver.PRODUCER_WAIT:
                action = self.orchestrator.next(run_id)
                if action["phase"] == "IMPLEMENT" and reviewed_revisions is None:
                    reviewed_revisions = self._commit_reviewed_worktrees(receipts)
                if result["producer_job"]["payload"].get("mode") == "merged":
                    # standard's merged design front: one producer fill yields {spec, dag}.
                    draft = {
                        "spec": self._phase_content(action, requirement, reviewed_revisions),
                        "dag": copy.deepcopy(specialized_examples()["task-dag"]),
                    }
                else:
                    draft = self._phase_content(action, requirement, reviewed_revisions)
                job_id = result["producer_job"]["job_id"]
                done = worker_driver.submit_draft(self.orchestrator, run_id, job_id, draft, knowledge_sync=knowledge)
                if done.get("reason_code") == "APPROVAL_REQUIRED":
                    self._approval(run_id, done["gate"], done["approval_input_hash"])
                    gate_approvals += 1
                    done = worker_driver.submit_draft(
                        self.orchestrator, run_id, job_id, draft, knowledge_sync=knowledge)
                self.assertTrue(done["ok"], (action.get("phase"), done))
                producers += 1
            elif parked == worker_driver.APPROVAL_WAIT:
                gate = result["gate"]
                input_hash = result["approval_input_hash"]
                if result.get("controller") == "workspace" and receipts is None:
                    # Capture the worktrees the worker cut, so IMPLEMENT can commit into them.
                    need = worker_driver.execute_controller(
                        self.orchestrator, run_id, knowledge_sync=knowledge, icode_runtime=icode)
                    receipts = need["workspace_receipts"]
                    gate, input_hash = need["gate"], need["approval_input_hash"]
                self._approval(run_id, gate, input_hash)
                gate_approvals += 1
            elif parked == worker_driver.CONTROLLER_STEP and result.get("controller") in ("ipipe", "release"):
                # SUBMIT/IPIPE landed the next controller in the same advance, before the
                # transport was built; the next iteration builds it and runs the controller.
                self.assertIsNone(api)
            else:
                self.fail(f"unexpected park {parked!r}: {result}")
        else:
            self.fail("run did not reach RELEASE_SUCCESS within the step bound")
        events = len(self.orchestrator.trace(run_id)["events"])
        return {"run_id": run_id, "producers": producers, "gate_approvals": gate_approvals,
                "controllers_run": controllers_run, "events": events}

    def test_worker_drives_a_standard_run_to_release_success_without_agent_turns(self):
        # The plan's acceptance: one standard requirement driven end to end by the worker.
        summary = self._worker_drive_to_terminal("BGW-800")
        self.assertEqual(self.orchestrator.status(summary["run_id"])["state"], "RELEASE_SUCCESS")
        # ProducerJob fills == the five model producer steps: GRILL, the merged SPEC+dag
        # step, PLAN, IMPLEMENT, REVIEW (TASKS is auto-filled from the merged draft, not a
        # separate producer). Deterministic steps take no agent turn.
        self.assertEqual(summary["producers"], 5)
        # Every side-effect controller ran inside the worker loop, never as a ProducerJob.
        self.assertEqual(set(summary["controllers_run"]), {"workspace", "submit", "ipipe", "release"})
        # One human APPROVE per gate the run actually crosses: G0 at intake, G1 (grill),
        # the merged design front G2 (TASKS's G3 is waived), WORKSPACE G4, PLAN G4, REVIEW
        # G5, SUBMIT G7, the IPIPE trigger G7 and RELEASE G9.
        self.assertEqual(summary["gate_approvals"], 9)
        # Far below the 102-event thrash of the one real un-worker-driven run.
        self.assertLess(summary["events"], 40)

    def test_slimmed_knowledge_writes_follow_the_phase_scope(self):
        # Slimming Stage B: a phase's stored artifact carries a KU doc / iCafe comment only
        # when its scope says so — GRILL/TASKS/PLAN/IMPLEMENT write neither, REVIEW writes
        # KU but no comment, IPIPE comments but writes no KU doc, SPEC/RELEASE do both. The
        # artifact itself is stored regardless (correctness never reads KU).
        import workflow_spec as ws

        summary = self._worker_drive_to_terminal("BGW-804")
        scoped = {"INTAKE", "GRILL", "SPEC", "TASKS", "PLAN", "IMPLEMENT", "REVIEW", "IPIPE", "RELEASE"}
        seen = set()
        for artifact in self.orchestrator.artifacts.phase_artifacts(summary["run_id"]):
            envelope = artifact["envelope"]
            phase = envelope["phase"]
            if phase not in scoped:
                continue
            seen.add(phase)
            scope = ws.knowledge_scope(phase)
            self.assertEqual(bool(envelope.get("knowledge_doc_id")), scope in ("both", "ku_only"), (phase, "ku"))
            self.assertEqual(bool(envelope.get("icafe_comment_id")), scope in ("both", "icafe_only"), (phase, "icafe"))
        # The run actually exercised the trimmed (skip) and both split-scope phases.
        self.assertTrue({"GRILL", "PLAN", "IMPLEMENT", "REVIEW", "SPEC", "IPIPE", "RELEASE"} <= seen, seen)

    def test_worker_records_a_model_execution_receipt_per_producer_fill(self):
        # Phase 2: every ProducerJob fill leaves a ModelExecutionReceipt (provider/model
        # null for the agent-turn backend, but input_hash/output_hash/prompt_version pinned)
        # — the ledger a later golden replay and cross-model diff gate read from.
        summary = self._worker_drive_to_terminal("BGW-801")
        receipts = self.orchestrator.state.model_execution_receipts(summary["run_id"])
        self.assertEqual(len(receipts), summary["producers"])
        self.assertEqual(
            {receipt["phase"] for receipt in receipts},
            {"GRILL", "SPEC", "PLAN", "IMPLEMENT", "REVIEW"},
        )
        for receipt in receipts:
            self.assertEqual(receipt["backend"], "agent-turn")
            self.assertIsNone(receipt["model"])
            self.assertEqual(receipt["prompt_version"], "workflow-spec-v1")
            self.assertTrue(receipt["input_hash"] and receipt["output_hash"])
            self.assertTrue(receipt["validators_passed"])
        # Idempotent: re-recording the same fill returns the same row, never a duplicate.
        first = receipts[0]
        again = self.orchestrator.state.record_model_execution_receipt(summary["run_id"], {
            key: value for key, value in first.items()
            if key not in ("receipt_id", "run_id", "created_at")
        })
        self.assertEqual(again["receipt_id"], first["receipt_id"])
        self.assertEqual(
            len(self.orchestrator.state.model_execution_receipts(summary["run_id"])),
            summary["producers"],
        )

    def test_golden_replay_and_cross_model_diff_gate(self):
        # Phase 3c: golden replay confirms every recorded producer fill has a cached draft
        # that still hashes to its recorded output (determinism/integrity); the cross-model
        # diff gate flags when two models produce different drafts for the same input.
        import replay_gate

        summary = self._worker_drive_to_terminal("BGW-803")
        report = replay_gate.golden_replay(self.orchestrator.state, summary["run_id"])
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["receipts"], summary["producers"])
        self.assertGreaterEqual(report["checked"], summary["producers"])
        self.assertEqual(report["missing_cache"], [])

        input_hash = "input-xyz"
        self.orchestrator.state.cache_draft(input_hash, "workflow-spec-v1", "model-a", {"x": 1})
        self.orchestrator.state.cache_draft(input_hash, "workflow-spec-v1", "model-b", {"x": 1})
        agree = replay_gate.cross_model_diff(self.orchestrator.state.draft_cache_entries(input_hash))
        self.assertEqual(agree["status"], "CONSISTENT")

        self.orchestrator.state.cache_draft(input_hash, "workflow-spec-v1", "model-c", {"x": 2})
        diverge = replay_gate.cross_model_diff(self.orchestrator.state.draft_cache_entries(input_hash))
        self.assertEqual((diverge["status"], diverge["distinct_outputs"]), ("DIVERGENT", 2))

    def test_cross_run_failure_case_library_escalates_recurring_signatures(self):
        # Phase 3b: the same failure signature routed across two runs accumulates into one
        # cross-run FailureCase; signature-first matching then escalates a recurring,
        # unresolved failure to architecture review instead of repairing it yet again.
        import repair_policy

        signature = "SIG-recurring-xyz"
        for card in ("BGW-820", "BGW-821"):
            started = self.orchestrator.start(card, "bgw", requirement_snapshot=snapshot(card))
            rid = started["run_id"]
            self.orchestrator.state.transition(rid, "REVIEW", {"fake_remote_evidence": True})
            self.orchestrator.route_failure(
                rid, "CODE_FAILURE", {"classification": "CODE", "failure_signature": signature})
        case = self.orchestrator.state.failure_case(signature)
        self.assertEqual((case["distinct_runs"], case["occurrences"], case["resolved"]), (2, 2, False))

        confirmed = [{"diagnosis_confirmed": True, "failure_signature": signature}]
        self.assertEqual(
            repair_policy.next_action(confirmed),
            {"action": "REPAIR", "reason_code": "DIAGNOSIS_CONFIRMED"},
        )
        self.assertEqual(
            repair_policy.next_action(confirmed, known_case=case),
            {"action": "ARCHITECTURE_REVIEW", "reason_code": "KNOWN_CROSS_RUN_FAILURE"},
        )
        # A first-time-cross-run or already-resolved signature does not escalate.
        self.assertIsNone(repair_policy.known_failure_verdict({"distinct_runs": 1, "resolved": False}))
        self.assertIsNone(repair_policy.known_failure_verdict({"distinct_runs": 3, "resolved": True}))

    def test_worker_caches_producer_drafts_for_reuse(self):
        # Phase 2: every producer fill also caches its DraftContent under
        # (input_hash, prompt_version, model), so a worker re-driving the same frontier can
        # reuse it instead of re-invoking the producer.
        summary = self._worker_drive_to_terminal("BGW-802")
        receipts = self.orchestrator.state.model_execution_receipts(summary["run_id"])
        for receipt in receipts:
            cached = self.orchestrator.state.cached_draft(
                receipt["input_hash"], "workflow-spec-v1", None)
            self.assertIsNotNone(cached, receipt["phase"])
            self.assertEqual(cached["prompt_version"], "workflow-spec-v1")
            self.assertIsNone(cached["model"])
        # The cache is authoritative and immutable per key: a conflicting re-cache is a hit
        # that returns the original draft, never an overwrite.
        first = receipts[0]
        original = self.orchestrator.state.cached_draft(first["input_hash"], "workflow-spec-v1", None)
        recache = self.orchestrator.state.cache_draft(
            first["input_hash"], "workflow-spec-v1", None, {"tampered": True})
        self.assertTrue(recache["cached"])
        self.assertEqual(recache["output_hash"], original["output_hash"])

    def test_worker_lease_makes_a_run_single_writer(self):
        # Phase 2: with a LockManager, the worker takes a per-run lease. A run already held
        # by a live worker is refused (WORKER_LEASE_HELD), never raced; once free the worker
        # drives and releases the lease on return.
        run_id, knowledge, _req = self._run_to_grill_action("BGW-810", "I15ClP2KW4ZGAK", "express")
        locks = self.orchestrator.recovery.locks
        key = f"worker-run:{run_id}"
        held = locks.acquire(key, "foreign-worker", 300)
        self.assertTrue(held["acquired"], held)
        refused = worker_driver.advance(
            self.orchestrator, run_id, knowledge_sync=knowledge, locks=locks, owner_token="worker-b")
        self.assertEqual(refused["reason_code"], "WORKER_LEASE_HELD")

        locks.release(key, "foreign-worker")
        drove = worker_driver.advance(
            self.orchestrator, run_id, knowledge_sync=knowledge, locks=locks, owner_token="worker-b")
        self.assertEqual((drove["ok"], drove["reason_code"]), (True, "PARKED"))
        self.assertEqual(locks.records()["active"], [])  # lease released after the drive

    def test_worker_lease_is_taken_over_from_a_crashed_holder(self):
        # A crashed worker's lease goes stale (TTL elapsed + dead pid); the next worker takes
        # it over and resumes — the event log + idempotency keys make the takeover safe.
        from datetime import datetime, timedelta, timezone
        from lock_manager import LockManager

        run_id, knowledge, _req = self._run_to_grill_action("BGW-811", "I15ClP2KW4ZGAK", "express")
        key = f"worker-run:{run_id}"
        stale_at = datetime.now(timezone.utc) - timedelta(hours=1)
        crashed = LockManager(self.orchestrator.state.database_path, pid=2147483000,
                              clock=lambda: stale_at)
        self.assertTrue(crashed.acquire(key, "crashed-worker", 1)["acquired"])

        live = LockManager(self.orchestrator.state.database_path)
        drove = worker_driver.advance(
            self.orchestrator, run_id, knowledge_sync=knowledge, locks=live, owner_token="worker-c")
        self.assertEqual((drove["ok"], drove["reason_code"]), (True, "PARKED"))

    def test_producer_draft_validated_before_the_job_is_locked(self):
        # HIGH-002: a schema-invalid draft is rejected as retryable WITHOUT locking the
        # ProducerJob, so a corrected draft can retry the same frontier (no unrecoverable
        # PRODUCER_JOB_CONFLICT). `full` keeps GRILL a normal gated model phase.
        run_id, knowledge, req = self._run_to_grill_action("BGW-807", "I15ClP2KW4ZGAK", "full")
        parked = worker_driver.advance(self.orchestrator, run_id, knowledge_sync=knowledge)
        job_id = parked["producer_job"]["job_id"]
        bad = worker_driver.submit_draft(
            self.orchestrator, run_id, job_id, {"garbage": True}, knowledge_sync=knowledge)
        self.assertEqual(bad["reason_code"], "DRAFT_SCHEMA_INVALID")
        self.assertTrue(bad["retry_allowed"])
        self.assertEqual(self.orchestrator.state.producer_job(job_id)["status"], "PENDING")
        # A corrected draft on the same job/frontier proceeds to its gate, not a conflict.
        action = self.orchestrator.next(run_id)
        good = worker_driver.submit_draft(
            self.orchestrator, run_id, job_id, self._phase_content(action, req),
            knowledge_sync=knowledge)
        self.assertEqual(good["reason_code"], "APPROVAL_REQUIRED")

    def test_producer_job_concurrent_fulfill_is_idempotent_or_conflicts(self):
        # Two workers filling the same ProducerJob: an identical draft merges to one row;
        # a different draft for an already-fulfilled job conflicts rather than overwriting.
        run_id = self.orchestrator.start(
            "BGW-805", "bgw", requirement_snapshot=snapshot("BGW-805"))["run_id"]
        st = self.orchestrator.state
        st.record_producer_job(run_id, "producer:x", {"phase": "SPEC"})
        first = st.fulfill_producer_job("producer:x", {"spec": 1})
        self.assertEqual(first["status"], "FULFILLED")
        again = st.fulfill_producer_job("producer:x", {"spec": 1})
        self.assertEqual(again["draft"], {"spec": 1})
        with self.assertRaises(ValueError):
            st.fulfill_producer_job("producer:x", {"spec": 2})

    def test_golden_replay_flags_a_receipt_with_no_cached_draft(self):
        # A recorded producer fill whose cached draft is missing must fail the replay gate
        # (never a false green), naming the offending input.
        import replay_gate

        run_id = self.orchestrator.start(
            "BGW-806", "bgw", requirement_snapshot=snapshot("BGW-806"))["run_id"]
        self.orchestrator.state.record_model_execution_receipt(run_id, {
            "job_id": "producer:y", "phase": "SPEC", "input_hash": "ih-y", "output_hash": "oh-y",
        })
        report = replay_gate.golden_replay(self.orchestrator.state, run_id)
        self.assertFalse(report["ok"])
        self.assertIn("ih-y", report["missing_cache"])

    def test_plan_rejects_caller_forged_task_and_revisions_without_owned_workspaces(self):
        run_id, _knowledge = self._run_to_workspace("BGW-510", "bgw", "I15ClP2KW4ZGAK")
        approval = self._approval(run_id, "G4", "approved-hash")

        result = self.orchestrator.advance(run_id, "PLAN", {
            "input_hash": "approved-hash",
            "approval_id": approval["approval_id"],
            "artifacts": ["workspace", "task-plan"],
            "task_id": "FORGED-TASK",
            "source_revisions": {"business": "forged-b", "tests": "forged-t"},
            "repo_revisions": {"business": "real-b", "tests": "real-t"},
            "evidence_revisions": {"business": "real-b", "tests": "real-t"},
        })

        self.assertEqual(result["reason_code"], "WORKSPACE_BINDING_REQUIRED")
        self.assertEqual(self.orchestrator.status(run_id)["state"], "WORKSPACE")

    def test_plan_derives_task_and_revisions_from_owned_business_and_test_worktrees(self):
        run_id, _knowledge = self._run_to_workspace("BGW-511", "bgw", "I15ClP2KW4ZGAK")
        receipts = self._owned_workspace_receipts(run_id, "bgw")
        binding = self.orchestrator.workspace_binding(run_id, receipts)
        self.assertTrue(binding["ok"], binding)
        approval = self._approval(run_id, "G4", binding["input_hash"])

        result = self.orchestrator.advance(run_id, "PLAN", {
            "input_hash": binding["input_hash"],
            "approval_id": approval["approval_id"],
            "artifacts": ["workspace", "task-plan"],
            "workspace_receipts": receipts,
            "workspace_binding": binding["workspace_binding"],
            "task_id": binding["workspace_binding"]["task_id"],
            "source_revisions": binding["workspace_binding"]["source_revisions"],
            "repo_revisions": binding["workspace_binding"]["source_revisions"],
            "evidence_revisions": binding["workspace_binding"]["source_revisions"],
        })

        self.assertEqual(result["state"], "PLAN", result)
        payload = self.orchestrator.status(run_id)["events"][-1]["payload"]
        self.assertEqual(payload["task_id"], "T-1")
        self.assertEqual(payload["source_revisions"], binding["workspace_binding"]["source_revisions"])
        self.assertEqual(payload["workspace_binding"], binding["workspace_binding"])
        self.assertNotIn(receipts["business"]["owner_token"], json.dumps(payload))

    def test_plan_rejects_caller_aliases_that_disagree_with_owned_workspace_binding(self):
        run_id, _knowledge = self._run_to_workspace("BGW-512", "bgw", "I15ClP2KW4ZGAK")
        receipts = self._owned_workspace_receipts(run_id, "bgw")
        binding = self.orchestrator.workspace_binding(run_id, receipts)
        approval = self._approval(run_id, "G4", binding["input_hash"])

        result = self.orchestrator.advance(run_id, "PLAN", {
            "input_hash": binding["input_hash"],
            "approval_id": approval["approval_id"],
            "artifacts": ["workspace", "task-plan"],
            "workspace_receipts": receipts,
            "task_id": "FORGED-TASK",
            "source_revisions": binding["workspace_binding"]["source_revisions"],
            "repo_revisions": binding["workspace_binding"]["source_revisions"],
            "evidence_revisions": binding["workspace_binding"]["source_revisions"],
        })

        self.assertEqual(result["reason_code"], "WORKSPACE_TASK_MISMATCH")
        self.assertEqual(self.orchestrator.status(run_id)["state"], "WORKSPACE")

    def test_unbound_legacy_plan_checkpoint_cannot_issue_an_executable_plan_action(self):
        run_id, _knowledge = self._run_to_workspace("BGW-513", "bgw", "I15ClP2KW4ZGAK")
        approval = self._approval(run_id, "G4", "legacy-policy-hash")

        result = self.orchestrator.advance(run_id, "PLAN", {
            "input_hash": "legacy-policy-hash",
            "approval_id": approval["approval_id"],
            "artifacts": ["workspace", "task-plan"],
        })

        self.assertEqual(result["state"], "PLAN", result)
        payload = self.orchestrator.status(run_id)["events"][-1]["payload"]
        self.assertNotIn("task_id", payload)
        self.assertNotIn("source_revisions", payload)
        self.assertNotIn("workspace_binding", payload)
        self.assertEqual(self.orchestrator.next(run_id)["reason_code"], "SOURCE_REVISION_REQUIRED")

    def test_workspace_binding_rejects_business_and_test_profile_role_swap(self):
        run_id, _knowledge = self._run_to_workspace("BGW-514", "bgw", "I15ClP2KW4ZGAK")
        receipts = self._owned_workspace_receipts(run_id, "bgw")
        swapped = copy.deepcopy(receipts)
        for role, other in (("business", "tests"), ("tests", "business")):
            swapped[role].update({
                key: receipts[other][key]
                for key in ("module", "repo_path", "worktree_path", "baseline_revision", "owner_token")
            })

        result = self.orchestrator.workspace_binding(run_id, swapped)

        self.assertEqual(result["reason_code"], "WORKSPACE_PROFILE_MISMATCH")

    def test_plan_projects_canonical_evidence_and_discards_real_owner_token_aliases(self):
        run_id, _knowledge = self._run_to_workspace("BGW-515", "bgw", "I15ClP2KW4ZGAK")
        receipts = self._owned_workspace_receipts(run_id, "bgw")
        binding = self.orchestrator.workspace_binding(run_id, receipts)
        approval = self._approval(run_id, "G4", binding["input_hash"])
        owner_token = receipts["business"]["owner_token"]

        result = self.orchestrator.advance(run_id, "PLAN", {
            "input_hash": binding["input_hash"],
            "approval_id": approval["approval_id"],
            "artifacts": ["workspace", "task-plan", owner_token],
            "required_artifacts": [owner_token],
            "workspace_receipts": receipts,
            "task_id": binding["workspace_binding"]["task_id"],
            "source_revisions": binding["workspace_binding"]["source_revisions"],
            "repo_revisions": binding["workspace_binding"]["source_revisions"],
            "evidence_revisions": binding["workspace_binding"]["source_revisions"],
            "opaque_proof": owner_token,
            "nested_alias": {"proof": owner_token, "values": [owner_token]},
        })

        self.assertEqual(result["state"], "PLAN", result)
        evidence = self.orchestrator.status(run_id)["events"][-1]["payload"]["evidence"]
        self.assertNotIn(owner_token, json.dumps(evidence, sort_keys=True))
        self.assertEqual(evidence["artifacts"], ["workspace", "task-plan"])
        self.assertEqual(set(evidence), {
            "run_id", "input_hash", "approved_input_hash", "approval_id", "approval_record",
            "artifacts", "task_id", "source_revisions", "repo_revisions",
            "evidence_revisions", "workspace_binding",
        })

    def test_plan_discards_duplicated_workspace_receipts_under_an_unknown_key(self):
        run_id, _knowledge = self._run_to_workspace("BGW-516", "bgw", "I15ClP2KW4ZGAK")
        receipts = self._owned_workspace_receipts(run_id, "bgw")
        binding = self.orchestrator.workspace_binding(run_id, receipts)
        approval = self._approval(run_id, "G4", binding["input_hash"])

        result = self.orchestrator.advance(run_id, "PLAN", {
            "input_hash": binding["input_hash"],
            "approval_id": approval["approval_id"],
            "artifacts": ["workspace", "task-plan"],
            "workspace_receipts": receipts,
            "workspace_receipts_copy": copy.deepcopy(receipts),
        })

        self.assertEqual(result["state"], "PLAN", result)
        evidence = self.orchestrator.status(run_id)["events"][-1]["payload"]["evidence"]
        self.assertNotIn("workspace_receipts_copy", evidence)
        self.assertNotIn(receipts["business"]["owner_token"], json.dumps(evidence, sort_keys=True))

    def test_plan_rejects_a_forged_workspace_binding_alias(self):
        run_id, _knowledge = self._run_to_workspace("BGW-517", "bgw", "I15ClP2KW4ZGAK")
        receipts = self._owned_workspace_receipts(run_id, "bgw")
        binding = self.orchestrator.workspace_binding(run_id, receipts)
        approval = self._approval(run_id, "G4", binding["input_hash"])

        result = self.orchestrator.advance(run_id, "PLAN", {
            "input_hash": binding["input_hash"],
            "approval_id": approval["approval_id"],
            "artifacts": ["workspace", "task-plan"],
            "workspace_receipts": receipts,
            "workspace_binding": {"forged": True},
        })

        self.assertEqual(result["reason_code"], "WORKSPACE_BINDING_MISMATCH")
        self.assertEqual(self.orchestrator.status(run_id)["state"], "WORKSPACE")

    def test_plan_rejects_near_match_workspace_binding_aliases(self):
        mutations = (
            ("worktree", lambda value: value["repositories"]["business"].update(
                {"worktree_path": value["repositories"]["tests"]["worktree_path"]}
            )),
            ("owner-proof", lambda value: value["repositories"]["business"].update(
                {"owner_proof": "0" * 64}
            )),
        )
        for index, (label, mutate) in enumerate(mutations):
            with self.subTest(label=label):
                card = f"BGW-{518 + index}"
                run_id, _knowledge = self._run_to_workspace(card, "bgw", "I15ClP2KW4ZGAK")
                receipts = self._owned_workspace_receipts(run_id, "bgw")
                binding = self.orchestrator.workspace_binding(run_id, receipts)
                approval = self._approval(run_id, "G4", binding["input_hash"])
                supplied = copy.deepcopy(binding["workspace_binding"])
                mutate(supplied)

                result = self.orchestrator.advance(run_id, "PLAN", {
                    "input_hash": binding["input_hash"],
                    "approval_id": approval["approval_id"],
                    "artifacts": ["workspace", "task-plan"],
                    "workspace_receipts": receipts,
                    "workspace_binding": supplied,
                })

                self.assertEqual(result["reason_code"], "WORKSPACE_BINDING_MISMATCH")
                self.assertEqual(self.orchestrator.status(run_id)["state"], "WORKSPACE")

    def test_optimize_rejects_missing_run_and_runtime_replacement(self):
        missing = self.orchestrator.optimize("missing-run", "build")

        self.assertEqual(missing["reason_code"], "RUN_NOT_FOUND")
        with self.assertRaises(TypeError):
            self.orchestrator.optimize("missing-run", "build", summary_runtime=object())
        with self.assertRaises(TypeError):
            self.orchestrator.optimize("missing-run", "build", knowledge_sync=object())

    def test_optimize_rejects_a_summary_owned_by_another_run(self):
        run_a = self.orchestrator.start(
            "BGW-801", "bgw", requirement_snapshot=snapshot("BGW-801")
        )["run_id"]
        run_b = self.orchestrator.start(
            "BGW-802", "bgw", requirement_snapshot=snapshot("BGW-802")
        )["run_id"]
        control_root, _target = self._optimization_candidate(run_a, "summary-cross-run")
        summary_a = self.orchestrator.optimize(run_a, "build")
        ku, cafe = self._install_knowledge_transports()

        result = self.orchestrator.optimize(
            run_b, "propose", summary=summary_a, allowed_roots=[control_root]
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "OPTIMIZATION_RUN_MISMATCH")
        self.assertEqual(ku.calls, [])
        self.assertEqual(cafe.calls, [])

    def test_optimize_rejects_another_runs_proposal_and_approval(self):
        run_a = self.orchestrator.start(
            "BGW-803", "bgw", requirement_snapshot=snapshot("BGW-803")
        )["run_id"]
        run_b = self.orchestrator.start(
            "BGW-804", "bgw", requirement_snapshot=snapshot("BGW-804")
        )["run_id"]
        control_root, target = self._optimization_candidate(run_a, "apply-cross-run")
        summary_a = self.orchestrator.optimize(run_a, "build")
        ku, cafe = self._install_knowledge_transports()
        proposal_a = self.orchestrator.optimize(
            run_a, "propose", summary=summary_a, allowed_roots=[control_root]
        )
        approval_a = self._approval(run_a, "G10", proposal_a["candidate_hash"])

        calls_before = (len(ku.calls), len(cafe.calls))
        result = self.orchestrator.optimize(
            run_b, "apply", proposal_id=proposal_a["proposal_id"],
            approval_id=approval_a["approval_id"],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "OPTIMIZATION_RUN_MISMATCH")
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
        self.assertEqual((len(ku.calls), len(cafe.calls)), calls_before)

    def test_trace_fails_closed_when_the_pinned_profile_drifts(self):
        run_id = self.orchestrator.start(
            "BGW-601", "bgw", requirement_snapshot=snapshot("BGW-601")
        )["run_id"]
        profile_path = self.root / "config" / "projects" / "bgw.yaml"
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        profile["knowledge_sources"][0]["parent_doc_id"] = "meQ-Acjg0K09Xr"
        profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")

        result = self.orchestrator.trace(run_id)

        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT")
        self.assertNotIn("project", result)

    def test_cli_exposes_next_complete_phase_optimize_and_preflight(self):
        class Controller:
            def __init__(self): self.calls = []
            def next(self, run_id): self.calls.append(("next", run_id)); return {"ok": True, "reason_code": "OK"}
            def complete_phase(self, run_id, envelope): self.calls.append(("complete", run_id, envelope)); return {"ok": True, "reason_code": "OK"}
            def optimize(self, run_id, operation, **options): self.calls.append(("optimize", run_id, operation, options)); return {"ok": True, "reason_code": "OK"}
            def preflight(self, project): self.calls.append(("preflight", project)); return {"ready": True, "status": "READY", "reason_code": "READY"}

        controller = Controller()
        envelope = self.root / "envelope.json"
        envelope.write_text('{"run_id":"run-1"}', encoding="utf-8")
        summary = self.root / "summary.json"
        summary.write_text('{"run_id":"run-1"}', encoding="utf-8")
        commands = (
            ["next", "run-1"],
            ["complete-phase", "run-1", str(envelope)],
            ["optimize", "run-1", "build"],
            ["optimize", "run-1", "propose", "--summary", str(summary), "--allowed-root", str(self.root)],
            ["optimize", "run-1", "apply", "--proposal-id", "p1", "--approval-id", "g10"],
            ["preflight", "bgw"],
        )
        with patch("orchestrator.Orchestrator", return_value=controller):
            for argv in commands:
                with self.subTest(command=argv[0]), redirect_stdout(StringIO()):
                    self.assertEqual(cli_main(argv), 0)

        self.assertEqual([call[0] for call in controller.calls], [
            "next", "complete", "optimize", "optimize", "optimize", "preflight"
        ])

    def _integrated_run(
        self, project, card, parent, optimization_decision,
        *, failure_reason="CODE_FAILURE", failure_classification="CODE",
        stop_after_routing=False,
    ):
        profile = self._integrated_profile(project)
        raw_snapshot = snapshot(card)
        raw_snapshot.pop("content_hash")
        cafe = CafeClient(fetcher=lambda requested: copy.deepcopy(raw_snapshot))
        requirement = cafe.snapshot(card)
        started = self.orchestrator.start(card, project, requirement_snapshot=requirement)
        self.assertEqual(started["state"], "INTAKE", started)
        run_id = started["run_id"]

        group_client = FakeGroupClient(create_result={"group_id": f"group-{project}"})
        collaboration = self.orchestrator.collaboration_session(group_client)
        prepared = collaboration.prepare_g0(
            run_id, project, {"id": card, "title": requirement["title"]},
            profile["approval_channels"]["role_members"],
        )
        self.assertEqual(prepared["input_hash"], self.orchestrator.next(run_id)["input_hash"])
        g0 = self._approval(run_id, "G0", prepared["input_hash"])
        group = collaboration.create(
            run_id, project, {"id": card, "title": requirement["title"]},
            profile["approval_channels"]["role_members"],
            approval_id=g0["approval_id"], input_hash=prepared["input_hash"],
        )
        self.assertEqual(group["group_id"], f"group-{project}")

        ku = FakeKuBoundary(project)
        cafe_comments = FakeCafeBoundary()
        knowledge = KnowledgeSync(
            state_store=self.orchestrator.state,
            ku_client=ku,
            cafe_client=cafe_comments,
            parent_doc_id=None,
            project_parent_doc_id=parent,
            run_root_title=f"{card}-{run_id[:12]}-fixture",
            card_id=card,
            run_id=run_id,
        )

        expected_children = {
            "INTAKE": None, "GRILL": "tom-grill", "SPEC": "tom-spec",
            "TASKS": "tom-tasks", "PLAN": "tom-plan",
            "IMPLEMENT": "tom-implement", "REVIEW": "tom-review",
        }
        for phase in ("INTAKE", "GRILL", "SPEC", "TASKS"):
            action = self.orchestrator.next(run_id)
            self.assertEqual((action["phase"], action["child_skill"]), (phase, expected_children[phase]))
            content = self._phase_content(action, requirement)
            envelope = self._phase_envelope(action, content, run_id)
            result = self.orchestrator.complete_phase(run_id, envelope, knowledge_sync=knowledge)
            self.assertTrue(result["ok"], result)

        receipts = self._owned_workspace_receipts(run_id, project)
        binding = self.orchestrator.workspace_binding(run_id, receipts)
        self.assertTrue(binding["ok"], binding)
        workspace_hash = binding["input_hash"]
        workspace_approval = self._approval(run_id, "G4", workspace_hash)
        workspace = self.orchestrator.advance(run_id, "PLAN", {
            "input_hash": workspace_hash,
            "approval_id": workspace_approval["approval_id"],
            "artifacts": ["workspace", "task-plan"],
            "workspace_receipts": receipts,
            "task_id": binding["workspace_binding"]["task_id"],
            "source_revisions": binding["workspace_binding"]["source_revisions"],
            "repo_revisions": binding["workspace_binding"]["source_revisions"],
            "evidence_revisions": binding["workspace_binding"]["source_revisions"],
        })
        self.assertEqual(workspace["state"], "PLAN", workspace)

        reviewed_revisions = None
        for phase in ("PLAN", "IMPLEMENT", "REVIEW"):
            action = self.orchestrator.next(run_id)
            self.assertTrue(action["ok"], action)
            self.assertEqual((action["phase"], action["child_skill"]), (phase, expected_children[phase]))
            if phase == "IMPLEMENT":
                reviewed_revisions = self._commit_reviewed_worktrees(receipts)
            content = self._phase_content(action, requirement, reviewed_revisions)
            envelope = self._phase_envelope(action, content, run_id)
            result = self.orchestrator.complete_phase(run_id, envelope, knowledge_sync=knowledge)
            self.assertTrue(result["ok"], result)

        self.assertEqual(self.orchestrator.next(run_id)["controller"], "submit")
        revision_set = {
            "business": {"module": "baidu/team/app", "branch": "main", "revision": "r2"},
            "test": {"module": "baidu/team/app-tests", "branch": "main", "revision": "t2"},
        }
        submit_hash = canonical_hash({"run_id": run_id, "revision_set": revision_set})
        g7 = self._approval(run_id, "G7", submit_hash)
        change_set = {
            "run_id": run_id, "change_set_id": "CS-1",
            "revision_set_id": f"RS-{project}", "repo_path": str(self.root / f"{project}-business"),
            "module": "baidu/team/app", "target_branch": "main", "commit_revision": "r2",
            "card_id": card, "owner": "owner@example.test", "revision_set": revision_set,
            "input_hash": submit_hash,
        }
        submitted = self.orchestrator.submit_to_ipipe(
            run_id, change_set, g7,
            icode_runtime=FakeIcodeRuntime(self.orchestrator.state, run_id),
        )
        self.assertTrue(submitted["ok"], submitted)
        # A single-task DAG has no open nodes once its only change set reaches iCode,
        # so the last submission advances the frontier straight to IPIPE.
        self.assertEqual(self.orchestrator.next(run_id)["controller"], "ipipe")

        revisions = {
            "run_id": run_id, "revision_set_id": f"RS-{project}",
            "repositories": [
                {"kind": "business", "module": "baidu/team/app", "revision": "r2", "branch": "main"},
                {"kind": "test", "module": "baidu/team/app-tests", "revision": "t2", "branch": "main"},
            ],
            "parameters": {"mode": "remote"},
        }
        api = FakeApi()
        candidate = {
            "id": "build-1", "pipelineConfId": "pipe-1", "module": "baidu/team/app",
            "revision": "r2", "revisions": {"baidu/team/app": "r2", "baidu/team/app-tests": "t2"},
            "params": {"mode": "remote"}, "status": "SUCCESS",
            "stageBuilds": [{"id": "stage-1", "stageName": "compile", "status": "SUCCESS"}],
        }
        api.candidates = []
        def crash_after_remote_trigger():
            api.candidates = [copy.deepcopy(candidate)]
            api.builds["build-1"] = copy.deepcopy(candidate)
            raise SystemExit("simulated process crash after remote trigger")

        api.on_trigger = crash_after_remote_trigger
        runtime = self.orchestrator.ipipe_runtime(run_id, api, sleeper=lambda _seconds: None)
        g8_hash = canonical_hash(trigger_binding(profile, revisions))
        g8 = self._approval(run_id, "G7", g8_hash)
        with self.assertRaises(SystemExit):
            runtime.trigger(profile, revisions, g8)
        restarted = Orchestrator(self.root)
        blocked_after_restart = restarted.next(run_id)
        self.assertEqual(blocked_after_restart["reason_code"], "RECOVERY_REQUIRED")
        api.on_trigger = None
        recovered_runtime = restarted.ipipe_runtime(run_id, api, sleeper=lambda _seconds: None)
        recovered_trigger = recovered_runtime.trigger(profile, revisions, g8)
        replayed_trigger = recovered_runtime.trigger(profile, revisions, g8)
        self.assertEqual(recovered_trigger, replayed_trigger)
        self.assertEqual(recovered_trigger["build_id"], "build-1")
        self.assertEqual(sum(call[0] == "trigger_by_revision" for call in api.calls), 1)
        self.orchestrator = restarted

        api.stages["build-1"] = [{"id": "stage-1", "stageName": "compile", "status": "SUCCESS"}]
        monitored = runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )
        self.assertTrue(monitored["ok"], monitored)
        ipipe = copy.deepcopy(specialized_examples()["ipipe-evidence"])
        ipipe.update({
            "pipeline_id": "pipe-1", "build_id": "build-1", "module": "baidu/team/app",
            "revisions": {"business": "r2", "tests": "t2"},
            "stages": [{"stage_id": "stage-1", "status": "SUCCESS", "job_ids": ["job-1"]}],
            "jobs": [{"job_id": "job-1", "status": "SUCCESS", "evidence_refs": ["ipipe:build-1/job-1"]}],
            "environment_fingerprint": canonical_hash(profile["environment_profile"]),
            "release_rule": "manual-approval",
            "remote_evidence_refs": monitored["evidence_refs"],
            "release_evidence": monitored["evidence_refs"],
        })
        ingested = self.orchestrator.phase_protocol(knowledge).ingest_ipipe_evidence(run_id, ipipe)
        self.assertTrue(ingested["ok"], ingested)
        self.assertEqual(self.orchestrator.status(run_id)["state"], "RELEASE")

        control_root = self.root / f"control-{hashlib.sha256(card.encode()).hexdigest()[:12]}"
        target = control_root / "scripts" / "guard.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("before\n", encoding="utf-8")
        candidate_fix = {
            "schema_version": "1", "root_cause": "missing durable retry guard",
            "expected_benefit": "deterministic retry", "risk": "low",
            "rollback": "restore previous bytes",
            "target_files": [{"path": str(target), "content": "after\n"}],
            "verification_commands": ["python3 -m unittest scripts.tests.test_fake_e2e"],
        }
        routed = self.orchestrator.route_failure(run_id, failure_reason, {
            "classification": failure_classification, "failure_signature": f"SIG-{project}",
            "optimization_candidate": candidate_fix,
        })
        duplicate_route = self.orchestrator.route_failure(run_id, failure_reason, {
            "classification": failure_classification, "failure_signature": f"SIG-{project}",
            "optimization_candidate": candidate_fix,
        })
        self.assertEqual(routed, duplicate_route)
        failure_message = collaboration.route_failure({
            "run_id": run_id, "category": routed["collaboration_category"],
            "summary": "remote pipeline failure",
            "evidence": {"build": "build-1"}, "next_action": "repair source",
        })
        duplicate_message = collaboration.route_failure({
            "run_id": run_id, "category": routed["collaboration_category"],
            "summary": "remote pipeline failure",
            "evidence": {"build": "build-1"}, "next_action": "repair source",
        })
        self.assertEqual(failure_message, duplicate_message)
        self.assertEqual(len(group_client.send_calls), 1)
        if stop_after_routing:
            return {
                "trace": self.orchestrator.trace(run_id),
                "routed": routed,
                "at_users": failure_message["at_users"],
                "blocked_after_restart": blocked_after_restart,
                "recovered_trigger": recovered_trigger,
                "replayed_trigger": replayed_trigger,
                "pending_after_recovery": self.orchestrator.state.pending_intents(run_id),
            }

        self.orchestrator._run_summary_options = {
            "control_root": control_root,
            "validation_runner": lambda _commands, _root: {"ok": True, "reason_code": "OK"},
        }
        summary = self.orchestrator.optimize(run_id, "build")
        self.assertEqual(summary["collaboration_receipt_count"], 2)
        self.assertEqual(summary["pipeline_evidence_count"], 1)
        self._install_knowledge_transports()
        proposal = self.orchestrator.optimize(
            run_id, "propose", summary=summary, allowed_roots=[control_root]
        )
        self.assertTrue(proposal["ok"], proposal)
        g10 = self._approval(run_id, "G10", proposal["candidate_hash"], optimization_decision)
        optimization = self.orchestrator.optimize(
            run_id, "apply", proposal_id=proposal["proposal_id"],
            approval_id=g10["approval_id"],
        )

        trace = self.orchestrator.trace(run_id)
        self.assertEqual(trace["project"], {
            "project_id": project,
            "profile_hash": trace["project"]["profile_hash"],
            "ku_repo_id": "sX0BTOBWJX", "ku_parent_doc_id": parent,
        })
        self.assertEqual([event["state"] for event in trace["events"]], [
            "INTAKE", "GRILL", "SPEC", "TASKS", "WORKSPACE", "PLAN",
            "IMPLEMENT", "REVIEW", "SUBMIT", "IPIPE", "RELEASE", "DIAGNOSE",
        ])
        self.assertEqual([item["receipt"]["response"].get("group_id") for item in trace["collaboration"]], [
            f"group-{project}", f"group-{project}",
        ])
        self.assertEqual(trace["role_routing"][-1], {
            "category": routed["collaboration_category"],
            "roles": ["development"] if routed["collaboration_category"] == "code" else trace["role_routing"][-1]["roles"],
        })
        self.assertEqual(
            {"intake", "grill", "spec", "tasks", "plan", "implement", "review", "change-set", "submission", "ipipe", "run-summary"},
            {artifact["kind"] for artifact in trace["artifacts"]},
        )
        self.assertEqual(ku.calls[0][1], parent)
        return {
            "trace": trace, "optimization": optimization, "target": target,
            "blocked_after_restart": blocked_after_restart,
            "recovered_trigger": recovered_trigger,
            "replayed_trigger": replayed_trigger,
            "pending_after_recovery": self.orchestrator.state.pending_intents(run_id),
        }

    def _phase_content(self, action, requirement, revisions=None):
        content = copy.deepcopy(action.get("content") or specialized_examples()[action["result_schema"]])
        if action["phase"] == "INTAKE":
            return copy.deepcopy(requirement)
        # An auto/controller-authored phase (child_skill None with content in the action)
        # is submitted verbatim — the controller already produced the exact bytes.
        if action.get("content") is not None and action.get("child_skill") is None:
            return content
        if action["phase"] == "GRILL":
            content["source_evidence"] = [f"icafe:{requirement['canonical_card_id']}/snapshot-1"]
        elif action["phase"] == "PLAN":
            content["g4_input_hash"] = action["input_hash"]
            for repository in content["repositories"]:
                repository["revision"] = action["source_revisions"][repository["role"]]
        elif action["phase"] == "IMPLEMENT":
            content["baseline_revisions"] = copy.deepcopy(action["source_revisions"])
            content["revisions"] = copy.deepcopy(revisions) if revisions else {"business": "r2", "tests": "t2"}
            content["full_diff_hash"] = canonical_hash({
                "business_patch": content["business_patch"], "test_patch": content["test_patch"],
            })
            content["candidate_hash"] = canonical_hash({
                key: value for key, value in content.items() if key != "candidate_hash"
            })
        elif action["phase"] == "REVIEW":
            predecessor = self.orchestrator.artifacts.latest_phase(action["run_id"], "IMPLEMENT", "T-1")
            content["change_set_hash"] = predecessor["envelope"]["content"]["candidate_hash"]
            content["baseline_revisions"] = predecessor["envelope"]["content"]["baseline_revisions"]
        return content

    def _commit_reviewed_worktrees(self, receipts):
        # The real IMPLEMENT step writes the change into each owned worktree and commits
        # it; the reviewed change set then pins those commits. build_and_archive (run at
        # REVIEW completion) re-commits the same clean worktree and asserts the revision
        # matches content["revisions"], so the fixture has to use the real commit SHAs
        # rather than synthetic placeholders.
        from submit_descriptor import _commit_if_dirty

        revisions = {}
        for role in ("business", "tests"):
            worktree = Path(receipts[role]["worktree_path"])
            (worktree / f"{role}-reviewed-change.txt").write_text(
                f"reviewed change for {role}\n", encoding="utf-8"
            )
            revisions[role] = _commit_if_dirty(str(worktree), f"reviewed {role} change set")
        return revisions

    def _optimization_candidate(self, run_id, label):
        control_root = self.root / f"control-{label}"
        target = control_root / "scripts" / "guard.py"
        target.parent.mkdir(parents=True)
        target.write_text("before\n", encoding="utf-8")
        candidate = {
            "schema_version": "1", "root_cause": "missing run binding",
            "expected_benefit": "reject cross-run dispatch", "risk": "low",
            "rollback": "restore previous bytes",
            "target_files": [{"path": str(target), "content": "after\n"}],
            "verification_commands": ["python3 -m unittest scripts.tests.test_fake_e2e"],
        }
        self.orchestrator.state.transition(run_id, "DIAGNOSE", {
            "classification": "CODE", "failure_signature": f"SIG-{label}",
            "optimization_candidate": candidate,
        })
        self.orchestrator._run_summary_options = {
            "control_root": control_root,
            "validation_runner": lambda _commands, _root: {"ok": True, "reason_code": "OK"},
        }
        return control_root, target

    def _install_knowledge_transports(self):
        ku = StatefulKuTransport()
        cafe = StatefulCafeTransport()
        self.orchestrator._knowledge_sync_transport_options = {
            "ku_transport": ku,
            "cafe_transport": cafe,
            "username": "fixture-user",
            "cafe_preflight": False,
        }
        return ku, cafe

    def _run_to_workspace(self, card, project, parent):
        requirement = snapshot(card)
        started = self.orchestrator.start(card, project, requirement_snapshot=requirement)
        run_id = started["run_id"]
        profile = yaml.safe_load(
            (self.root / "config" / "projects" / f"{project}.yaml").read_text(encoding="utf-8")
        )
        collaboration = self.orchestrator.collaboration_session(
            FakeGroupClient(create_result={"group_id": f"group-{project}-{card}"})
        )
        prepared = collaboration.prepare_g0(
            run_id, project, {"id": card, "title": requirement["title"]},
            profile["approval_channels"]["role_members"],
        )
        approval = self._approval(run_id, "G0", prepared["input_hash"])
        created = collaboration.create(
            run_id, project, {"id": card, "title": requirement["title"]},
            profile["approval_channels"]["role_members"],
            approval_id=approval["approval_id"], input_hash=prepared["input_hash"],
        )
        self.assertEqual(created["run_id"], run_id, created)
        knowledge = KnowledgeSync(
            state_store=self.orchestrator.state,
            ku_client=FakeKuBoundary(project),
            cafe_client=FakeCafeBoundary(),
            parent_doc_id=None,
            project_parent_doc_id=parent,
            run_root_title=f"{card}-{run_id[:12]}-fixture",
            card_id=card,
            run_id=run_id,
        )
        for phase in ("INTAKE", "GRILL", "SPEC", "TASKS"):
            action = self.orchestrator.next(run_id)
            self.assertEqual(action["phase"], phase)
            content = self._phase_content(action, requirement)
            result = self.orchestrator.complete_phase(
                run_id, self._phase_envelope(action, content, run_id), knowledge_sync=knowledge
            )
            self.assertTrue(result["ok"], result)
        self.assertEqual(self.orchestrator.status(run_id)["state"], "WORKSPACE")
        return run_id, knowledge

    def _owned_workspace_receipts(self, run_id, project):
        action = self.orchestrator.next(run_id)
        self.assertEqual(action["state"], "WORKSPACE", action)
        task_id = action["task_id"]
        profile = yaml.safe_load(
            (self.root / "config" / "projects" / f"{project}.yaml").read_text(encoding="utf-8")
        )
        selected = {
            "business": profile["business_repos"][0],
            "tests": profile["test_repo"],
        }
        receipts = {}
        for role, repository in selected.items():
            repo = Path(repository["path"])
            revision = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            baseline = self.orchestrator.workspaces.inspect(
                repo, run_id, task_id, baseline_evidence={"revision": revision}
            )
            created = self.orchestrator.workspaces.create(repo, run_id, task_id, baseline)
            self.assertEqual(created["status"], "CREATED", created)
            receipts[role] = {
                "role": role,
                "module": repository["module"],
                "repo_path": str(repo.resolve()),
                "worktree_path": created["worktree_path"],
                "baseline_revision": created["baseline_revision"],
                "owner_token": created["owner_token"],
            }
        return receipts

    def _phase_envelope(self, action, content, run_id):
        content_hash = canonical_hash(content)
        source_revisions = (
            copy.deepcopy(content["revisions"])
            if action["phase"] == "IMPLEMENT"
            else copy.deepcopy(action["source_revisions"])
        )
        approval_hash = (
            action["input_hash"] if action["phase"] == "INTAKE" else canonical_hash({
                "action_id": action["action_id"], "task_id": action["task_id"],
                "parent_artifact_hash": action["parent_artifact_hash"],
                "source_revisions": source_revisions, "content_hash": content_hash,
            })
        )
        approval = None
        if action["required_human_gate"] is not None:
            approval = self._approval(run_id, action["required_human_gate"], approval_hash)
        return {
            "action_id": action["action_id"], "source_event_id": action["source_event_id"],
            "host": "comate", "run_id": run_id, "phase": action["phase"],
            "task_id": action["task_id"], "schema_version": "1", "input_hash": action["input_hash"],
            "content_hash": content_hash, "source_revisions": source_revisions,
            "parent_artifact_hash": action["parent_artifact_hash"],
            "knowledge_doc_id": None, "knowledge_url": None, "knowledge_version": None,
            "icafe_comment_id": None, "evidence_refs": copy.deepcopy(action["source_evidence_refs"]),
            "approval_id": approval["approval_id"] if approval else None,
            "approval_input_hash": approval_hash if approval else approval_hash,
            "content": content,
        }

    def _approval(self, run_id, action, input_hash, decision="APPROVE"):
        policy = {
            "comate": ["owner@example.test"],
            "infoflow": ["owner@example.test"],
        }
        row = self.orchestrator.approvals.request(
            action, input_hash, ["comate", "infoflow"], run_id=run_id,
            member_policy=policy,
        )
        for channel in ("comate", "infoflow"):
            self.orchestrator.approvals.record_delivery(
                row["approval_id"], channel, {"request_id": f"{channel}-{action}"},
                payload_hash=input_hash,
            )
        self.orchestrator.approvals.resolve(
            row["approval_id"], decision, input_hash, "comate", run_id=run_id,
            responder="owner@example.test", state_store=self.orchestrator.state,
        )
        return {"approval_id": row["approval_id"], "input_hash": input_hash}

    def _integrated_profile(self, project):
        path = self.root / "config" / "projects" / f"{project}.yaml"
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
        profile["business_repos"][0]["module"] = "baidu/team/app"
        profile["test_repo"]["module"] = "baidu/team/app-tests"
        profile["pipeline_profile"].update({
            "pipeline_id": "pipe-1", "allowed_parameters": ["mode"],
            "stage_classes": ["compile", "unit", "release"],
            "release_rule": "manual-approval",
        })
        path.write_text(yaml.safe_dump(profile), encoding="utf-8")
        return profile

    def _write_profile(self, project, parent, language_name, project_name):
        profile = copy.deepcopy(PROFILE)
        profile["project_id"] = project
        business = self.root / f"{project}-business"
        tests = self.root / f"{project}-tests"
        subprocess.run(["git", "init", "-q", str(business)], check=True)
        subprocess.run(["git", "init", "-q", str(tests)], check=True)
        for repo, label in ((business, "business"), (tests, "tests")):
            # Persist the identity (rather than passing it per commit) so git worktrees
            # created off these repos inherit it: build_and_archive commits reviewed
            # change sets from the worktree and reads user.name/user.email there.
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "tom-autodev-fixture"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.test"], check=True)
            (repo / "README.md").write_text(f"{project} {label}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run([
                "git", "-C", str(repo), "commit", "-q", "-m", "fixture baseline",
            ], check=True)
        language = self.root / f"{project}-{language_name}"
        project_skill = self.root / f"{project}-{project_name}"
        for path in (language, project_skill):
            path.mkdir()
            (path / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")
        profile["business_repos"][0].update(path=str(business), module=project, lock=f"{project}-main")
        profile["test_repo"].update(path=str(tests), module=f"{project}-tests", lock=f"{project}-tests-main")
        profile["language_skill"] = str(language)
        profile["project_skill"] = str(project_skill)
        profile["knowledge_sources"][0]["parent_doc_id"] = parent
        profile["pipeline_profile"]["pipeline_id"] = f"{project}-pipeline"
        target = self.root / "config" / "projects" / f"{project}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(profile), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
