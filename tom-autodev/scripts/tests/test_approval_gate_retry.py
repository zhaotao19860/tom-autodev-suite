"""A re-issued (suffixed) approval, once approved, must satisfy its bare gate."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval_ledger import ApprovalLedger, gate_of, MAX_APPROVAL_RETRIES
from approval_delivery import InfoflowApprovalTransport
from approval_watch import ApprovalWatcher
from clients.infoflow_approval_client import InfoflowApprovalClient
from clients.icode_runtime import _approved_record as icode_approved_record
from clients.ipipe_runtime import _approved_record as ipipe_approved_record
from collaboration import CollaborationSession
from evidence_gate import EvidenceGate
from orchestrator import Orchestrator
from run_brief import build as build_brief
from run_summary import RunSummary
from test_approval_watch import FakeNotifyClient
from test_collaboration import FakeGroupClient, g0_binding, members
from test_phase_protocol import FakeKnowledgeSync, _seed_envelope
from test_phase_protocol_repair import strict_intake_payload
from test_run_summary import KnowledgeFake
from test_schema_validation import specialized_examples
import worker_driver
import workflow_spec


_CHANNELS = ["comate", "infoflow"]
_POLICY = {channel: ["owner@example.test"] for channel in _CHANNELS}


class GateNormalizationTests(unittest.TestCase):
    def test_gate_of_drops_retry_suffix(self):
        self.assertEqual(gate_of("G7"), "G7")
        self.assertEqual(gate_of("G7#retry-1"), "G7")
        self.assertEqual(gate_of("G9#retry-12"), "G9")

    def _approved_reissued(self, ledger):
        # A timed-out G7 is re-issued as "G7#retry-1"; the operator approves that card.
        record = ledger.request("G7#retry-1", "h" * 64, ["comate", "infoflow"], run_id="run-1",
                                member_policy={c: ["owner@example.test"] for c in ("comate", "infoflow")})
        for channel in ("comate", "infoflow"):
            ledger.record_delivery(record["approval_id"], channel, {"request_id": f"r-{channel}"},
                                   payload_hash="h" * 64)
        ledger.resolve(record["approval_id"], "APPROVE", "h" * 64, "comate",
                       run_id="run-1", responder="owner@example.test")
        return record["approval_id"]

    def test_reissued_approval_satisfies_the_bare_gate_consumers(self):
        for approved_record in (icode_approved_record, ipipe_approved_record):
            with tempfile.TemporaryDirectory() as directory:
                ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
                approval_id = self._approved_reissued(ledger)
                # The consumer asks for the bare gate "G7"; the record's action is "G7#retry-1".
                result = approved_record(
                    ledger, {"approval_id": approval_id, "input_hash": "h" * 64},
                    run_id="run-1", action="G7", input_hash="h" * 64)
                self.assertIsNone(result, (approved_record.__module__, result))

    def test_a_different_gate_still_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            approval_id = self._approved_reissued(ledger)
            result = icode_approved_record(
                ledger, {"approval_id": approval_id, "input_hash": "h" * 64},
                run_id="run-1", action="G5", input_hash="h" * 64)
            self.assertEqual(result["reason_code"], "APPROVAL_GATE_MISMATCH")

    def test_automatic_reissue_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            orch = Orchestrator(Path(directory))
            # MAX_APPROVAL_RETRIES suffixed attempts already timed out; the next reissue
            # must stop rather than mint "#retry-(MAX+1)".
            timed_out = [{"approval_id": f"a-{i}", "action": action, "input_hash": "h" * 64,
                          "effective_decision": "TIMEOUT", "status": "TIMEOUT"}
                         for i, action in enumerate(
                             ["G0"] + [f"G0#retry-{n}" for n in range(1, MAX_APPROVAL_RETRIES + 1)])]
            orch.approvals.for_run = lambda run_id: list(timed_out)
            result = orch.reissue_infoflow_approval(
                "run-1", "G0", "h" * 64,
                member_policy={c: ["owner@example.test"] for c in ("comate", "infoflow")},
                infoflow_client=object())
            self.assertEqual(result["reason_code"], "APPROVAL_RETRY_EXHAUSTED")


class RetryConsumerTests(unittest.TestCase):
    """Exercise retry action identity through the durable consumer boundaries."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.orch = Orchestrator(self.root)
        self.run_id = "retry-consumers"
        self.sync = FakeKnowledgeSync()
        self.notify = FakeNotifyClient()
        self.client = InfoflowApprovalClient(
            InfoflowApprovalTransport(self.orch.state, self.notify))

    def _record(self, action, input_hash, *, run_id=None, decision="APPROVE"):
        run_id = run_id or self.run_id
        record = self.orch.approvals.request(
            action, input_hash, _CHANNELS, run_id=run_id, member_policy=_POLICY)
        for channel in _CHANNELS:
            self.orch.approvals.record_delivery(
                record["approval_id"], channel, {"request_id": f"local-{channel}"},
                payload_hash=input_hash)
        if decision is not None:
            self.orch.approvals.resolve(
                record["approval_id"], decision, input_hash, "comate", run_id=run_id,
                responder="owner@example.test", state_store=self.orch.state)
        return self.orch.approvals.get(record["approval_id"])

    def _timeout(self, record):
        result = self.orch.approvals.timeout(
            record["approval_id"], record["input_hash"], run_id=record["run_id"],
            state_store=self.orch.state,
            now=datetime.fromisoformat(record["deadline_at"]) + timedelta(seconds=1))
        self.assertEqual(result["effective_decision"], "TIMEOUT", result)
        return self.orch.approvals.get(record["approval_id"])

    def _reissued(self, gate, input_hash, *, decision="APPROVE", delivery_failed=False):
        original = self._record(gate, input_hash, decision=None)
        if delivery_failed:
            self.orch.approvals.record_delivery_failure(
                original["approval_id"], {"reason_code": "APPROVAL_REQUEST_RESPONSE_INVALID",
                                          "retry_allowed": False})
        else:
            self._timeout(original)
        retried = self.orch.reissue_infoflow_approval(
            self.run_id, gate, input_hash, member_policy=_POLICY,
            infoflow_client=self.client)
        self.assertEqual(retried.get("action"), f"{gate}#retry-1", retried)
        self.assertEqual(retried["input_hash"], input_hash)
        if decision is not None:
            self.orch.approvals.resolve(
                retried["approval_id"], decision, input_hash, "comate", run_id=self.run_id,
                responder="owner@example.test", state_store=self.orch.state)
        return self.orch.approvals.get(retried["approval_id"])

    def _intake(self):
        payload = strict_intake_payload(
            self.run_id, specialized_examples()["requirement-snapshot"])
        self.orch.state.transition(self.run_id, "INTAKE", payload)
        return payload

    def _plan_draft(self):
        examples = specialized_examples()
        self.orch.artifacts.put_envelope(_seed_envelope(
            self.run_id, "TASKS", None, examples["task-dag"]))
        self.orch.state.transition(self.run_id, "PLAN", {
            "requirement_id": "BGW-1", "profile_hash": "a" * 64,
            "change_class": "full", "workflow_spec_hash": workflow_spec.canonical_hash(),
            "workflow_modes": workflow_spec.run_workflow_modes("full"),
            "source_revisions": {"business": "r1", "tests": "t1"}, "task_id": "T-1",
        })
        action = self.orch.next(self.run_id)
        self.assertTrue(action["ok"], action)
        parked = worker_driver.advance(self.orch, self.run_id, knowledge_sync=self.sync)
        job_id = parked["producer_job"]["job_id"]
        draft = {**examples["task-plan"], "g4_input_hash": action["input_hash"]}
        waiting = worker_driver.submit_draft(
            self.orch, self.run_id, job_id, draft, knowledge_sync=self.sync)
        self.assertEqual(waiting["reason_code"], "APPROVAL_REQUIRED", waiting)
        self.assertEqual(waiting["gate"], "G4")
        return job_id, draft, waiting["approval_input_hash"]

    def _proposal(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self.orch.state.transition(self.run_id, "DIAGNOSE", {
            "reason_code": "REVIEW_FAILURE", "failure_signature": "review:rule-1",
            "optimization_candidate": {
                "schema_version": "1", "root_cause": "missing retry gate normalization",
                "expected_benefit": "resume from an approved retry", "risk": "bounded edit",
                "rollback": "restore the prior bytes",
                "target_files": [{"path": str(target), "content": "after"}],
                "verification_commands": ["python3 -m unittest"],
            },
        })
        summary = RunSummary(
            self.orch.state, self.orch.artifacts, self.orch.approvals,
            knowledge_sync=KnowledgeFake(), control_root=self.root,
            validation_runner=lambda _commands, _root: {"ok": True, "reason_code": "OK"})
        proposal = summary.propose(summary.build(self.run_id), [self.root])
        self.assertTrue(proposal["ok"], proposal)
        return summary, proposal, target

    def test_worker_consumes_reissued_approval_and_persisted_gated_draft_after_restart(self):
        job_id, draft, input_hash = self._plan_draft()
        record = self._reissued("G4", input_hash)

        restarted = Orchestrator(self.root)
        result = worker_driver.advance(restarted, self.run_id, knowledge_sync=self.sync)

        self.assertEqual(restarted.status(self.run_id)["state"], "IMPLEMENT", result)
        self.assertEqual([step["phase"] for step in result["auto_completed"]], ["PLAN"])
        self.assertEqual(result["auto_completed"][0]["source"], "cached-draft")
        self.assertEqual(restarted.state.producer_job(job_id)["draft"], draft)
        artifact = restarted.artifacts.latest_phase(self.run_id, "PLAN", "T-1")
        self.assertEqual(artifact["envelope"]["approval_id"], record["approval_id"])
        self.assertEqual(artifact["envelope"]["approval_input_hash"], input_hash)

    def test_worker_controller_retry_keeps_gate_hash_run_and_decision_binding(self):
        self._intake()
        action = self.orch.next(self.run_id)
        input_hash = action["input_hash"]
        for gate, approved_hash, run_id, decision in (
            ("G1#retry-1", input_hash, self.run_id, "APPROVE"),
            ("G0#retry-1", "changed-input", self.run_id, "APPROVE"),
            ("G0#retry-1", input_hash, "other-run", "APPROVE"),
            ("G0#retry-2", input_hash, self.run_id, "REJECT"),
        ):
            with self.subTest(gate=gate, input_hash=approved_hash, run_id=run_id, decision=decision):
                self._record(gate, approved_hash, run_id=run_id, decision=decision)
                self.assertEqual(worker_driver.classify_next(self.orch, self.run_id)["kind"],
                                 worker_driver.APPROVAL_WAIT)
        self._record("G0#retry-3", input_hash)
        self.assertEqual(worker_driver.classify_next(self.orch, self.run_id)["kind"],
                         worker_driver.CONTROLLER_STEP)

    def test_worker_stored_draft_rejects_retry_for_other_gate_hash_run_or_rejection(self):
        job_id, draft, input_hash = self._plan_draft()
        for gate, approved_hash, run_id, decision in (
            ("G5#retry-1", input_hash, self.run_id, "APPROVE"),
            ("G4#retry-1", "changed-input", self.run_id, "APPROVE"),
            ("G4#retry-1", input_hash, "other-run", "APPROVE"),
            ("G4#retry-2", input_hash, self.run_id, "REJECT"),
        ):
            with self.subTest(gate=gate, input_hash=approved_hash, run_id=run_id, decision=decision):
                self._record(gate, approved_hash, run_id=run_id, decision=decision)
                result = worker_driver.advance(self.orch, self.run_id, knowledge_sync=self.sync)
                self.assertEqual(result["parked"], worker_driver.APPROVAL_WAIT, result)
                self.assertEqual(self.orch.status(self.run_id)["state"], "PLAN")
                self.assertEqual(self.orch.state.producer_job(job_id)["draft"], draft)
        self.assertFalse(self.orch.artifacts.latest_phase(self.run_id, "PLAN", "T-1").get("valid"))

    def test_reissued_approval_cannot_authorize_changed_producer_content(self):
        job_id, draft, input_hash = self._plan_draft()
        record = self._reissued("G4", input_hash)
        action = self.orch.next(self.run_id)
        changed = worker_driver.build_envelope(
            action, {**draft, "risks": ["a new risk needing approval"]}, record["approval_id"])

        checked = self.orch.phase_protocol(self.sync).validate_result(action, changed)

        self.assertEqual(checked["reason_code"], "APPROVAL_INPUT_MISMATCH", checked)
        self.assertEqual(self.orch.status(self.run_id)["state"], "PLAN")
        self.assertEqual(self.orch.state.producer_job(job_id)["draft"], draft)

    def test_evidence_gate_accepts_each_reissued_gate_from_sqlite(self):
        for number in range(11):
            gate = f"G{number}"
            with self.subTest(gate=gate):
                record = self._record(f"{gate}#retry-1", "bound-hash")
                result = EvidenceGate().check(gate, {
                    "run_id": self.run_id, "input_hash": "bound-hash",
                    "approved_input_hash": "bound-hash", "approval_id": record["approval_id"],
                    "approval_record": self.orch.approvals.get(record["approval_id"]),
                })
                self.assertTrue(result["passed"], result)

    def test_evidence_gate_retry_does_not_authorize_other_gate_hash_run_or_rejection(self):
        for gate, approved_hash, run_id, decision in (
            ("G8#retry-1", "bound-hash", self.run_id, "APPROVE"),
            ("G9#retry-1", "changed-input", self.run_id, "APPROVE"),
            ("G9#retry-1", "bound-hash", "other-run", "APPROVE"),
            ("G9#retry-2", "bound-hash", self.run_id, "REJECT"),
            ("G9#retry-3", "bound-hash", self.run_id, None),
        ):
            with self.subTest(gate=gate, input_hash=approved_hash, run_id=run_id, decision=decision):
                record = self._record(gate, approved_hash, run_id=run_id, decision=decision)
                result = EvidenceGate().check("G9", {
                    "run_id": self.run_id, "input_hash": "bound-hash",
                    "approved_input_hash": "bound-hash", "approval_id": record["approval_id"],
                    "approval_record": record,
                })
                self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED", result)

    def test_reissued_card_still_requires_an_authorized_member(self):
        self._intake()
        input_hash = self.orch.next(self.run_id)["input_hash"]
        record = self._reissued("G0", input_hash, decision=None)
        result = self.orch.approvals.receive(
            record["approval_id"], "APPROVE", input_hash, "comate", "stranger@example.test",
            run_id=self.run_id, state_store=self.orch.state)
        self.assertEqual(result["reason_code"], "APPROVAL_RESPONDER_UNAUTHORIZED")
        self.assertEqual(worker_driver.classify_next(self.orch, self.run_id)["kind"],
                         worker_driver.APPROVAL_WAIT)

    def test_g0_reissued_approval_creates_only_the_bound_collaboration_group(self):
        payload = self._intake()
        client = FakeGroupClient()
        session = CollaborationSession(self.orch.state, client, approvals=self.orch.approvals)
        record = self._reissued("G0", payload["g0_input_hash"])
        result = session.create(
            self.run_id, "bgw", {"id": "BGW-1"}, {},
            approval_id=record["approval_id"], input_hash=payload["g0_input_hash"])
        self.assertEqual(result.get("group_id"), "123", result)
        receipt = self.orch.state.result_by_idempotency_key(f"infoflow.group.create:{self.run_id}")
        self.assertEqual(receipt["receipt"]["response"]["g0_approval_id"], record["approval_id"])
        self.assertEqual(result["member_snapshot"], payload["collaboration_binding"]["member_snapshot"])
        self.assertEqual(result["owner"], payload["collaboration_binding"]["owner"])

    def test_g0_reissued_approval_rejects_wrong_gate_hash_run_and_decision_before_group_write(self):
        payload = self._intake()
        input_hash = payload["g0_input_hash"]
        client = FakeGroupClient()
        session = CollaborationSession(self.orch.state, client, approvals=self.orch.approvals)
        for gate, approved_hash, run_id, decision in (
            ("G1#retry-1", input_hash, self.run_id, "APPROVE"),
            ("G0#retry-1", "changed-input", self.run_id, "APPROVE"),
            ("G0#retry-1", input_hash, "other-run", "APPROVE"),
            ("G0#retry-2", input_hash, self.run_id, "REJECT"),
        ):
            with self.subTest(gate=gate, input_hash=approved_hash, run_id=run_id, decision=decision):
                record = self._record(gate, approved_hash, run_id=run_id, decision=decision)
                result = session.create(
                    self.run_id, "bgw", {"id": "BGW-1"}, {},
                    approval_id=record["approval_id"], input_hash=input_hash)
                self.assertEqual(result["reason_code"], "G0_BINDING_MISMATCH", result)
        self.assertEqual(client.create_calls, [])
        self.assertEqual(self.orch.state.pending_intents(self.run_id), [])

    def test_legacy_g0_retry_binding_still_requires_the_exact_members(self):
        client = FakeGroupClient()
        session = CollaborationSession(self.orch.state, client)
        roster = members()
        binding = {**g0_binding(session, roster), "action": "G0#retry-1"}
        roster["g0_approval"] = {**binding, "member_snapshot": ["stranger@example.test"]}
        wrong = session.create(self.run_id, "bgw", {"id": "BGW-19", "title": "Login flow"}, roster)
        self.assertEqual(wrong["reason_code"], "G0_BINDING_REQUIRED", wrong)
        self.assertEqual(client.create_calls, [])
        roster["g0_approval"] = binding
        created = session.create(self.run_id, "bgw", {"id": "BGW-19", "title": "Login flow"}, roster)
        self.assertEqual(created.get("group_id"), "123", created)

    def test_run_brief_shows_pending_retry_and_approved_retry_supersedes_failed_attempt(self):
        _, _, input_hash = self._plan_draft()
        record = self._reissued("G4", input_hash, decision=None, delivery_failed=True)
        waiting = build_brief(self.orch, self.run_id)
        self.assertIn(record["approval_id"], [row["approval_id"] for row in waiting["waiting_on"]])
        self.orch.approvals.resolve(
            record["approval_id"], "APPROVE", input_hash, "comate", run_id=self.run_id,
            responder="owner@example.test", state_store=self.orch.state)
        approved = build_brief(self.orch, self.run_id)
        self.assertEqual(approved["waiting_on"], [])
        self.assertIn(record["approval_id"], approved["next"]["text"])
        self.assertIn("已批准待提交", approved["next"]["text"])

    def test_watcher_creates_retry_handoff_that_worker_can_resume(self):
        _, _, input_hash = self._plan_draft()
        record = self._reissued("G4", input_hash)
        watcher = ApprovalWatcher(self.orch, lambda: self.client, self.notify)
        notices = watcher._handoff()
        self.assertEqual([notice["reason_code"] for notice in notices], ["RESUME_NOTICE_SENT"])
        handoffs = [item for item in self.orch.state.incomplete_handoffs(self.run_id)
                    if item["payload"].get("kind") == "APPROVAL_RESUME"]
        self.assertEqual([item["payload"]["approval_id"] for item in handoffs], [record["approval_id"]])
        resumed = worker_driver.resume(self.orch, self.run_id, knowledge_sync=self.sync)
        self.assertEqual(self.orch.status(self.run_id)["state"], "IMPLEMENT", resumed)
        self.assertEqual(watcher._handoff(), [])

    def test_watcher_reissues_each_timed_out_retry_with_same_hash_and_stops_at_bound(self):
        self._intake()
        record = self._record("G0", "bound-hash", decision=None)
        watcher = ApprovalWatcher(self.orch, lambda: self.client, self.notify)
        for number in range(1, MAX_APPROVAL_RETRIES + 1):
            record = self._timeout(record)
            result = watcher._reissue(record, self.client)
            self.assertEqual(result["reason_code"], "REISSUED", result)
            record = self.orch.approvals.get(result["approval_id"])
            self.assertEqual(record["action"], f"G0#retry-{number}")
            self.assertEqual(record["input_hash"], "bound-hash")
        exhausted = watcher._reissue(self._timeout(record), self.client)
        self.assertEqual(exhausted["reason_code"], "APPROVAL_RETRY_EXHAUSTED", exhausted)
        self.assertEqual(len(self.orch.approvals.for_run(self.run_id)), MAX_APPROVAL_RETRIES + 1)

    def test_g10_reissued_approval_applies_and_replays_exact_candidate(self):
        summary, proposal, target = self._proposal()
        record = self._reissued("G10", proposal["candidate_hash"])
        applied = summary.apply(proposal["proposal_id"], record["approval_id"])
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(target.read_text(encoding="utf-8"), "after")
        self.assertEqual(summary.apply(proposal["proposal_id"], record["approval_id"]), applied)

    def test_g10_retry_keeps_gate_hash_run_and_pending_approval_binding(self):
        summary, proposal, target = self._proposal()
        input_hash = proposal["candidate_hash"]
        for gate, approved_hash, run_id, decision, reason in (
            ("G9#retry-1", input_hash, self.run_id, "APPROVE", "G10_APPROVAL_REQUIRED"),
            ("G10#retry-1", "changed-input", self.run_id, "APPROVE", "G10_APPROVAL_HASH_MISMATCH"),
            ("G10#retry-1", input_hash, "other-run", "APPROVE", "G10_APPROVAL_REQUIRED"),
            ("G10#retry-2", input_hash, self.run_id, None, "G10_APPROVAL_REQUIRED"),
        ):
            with self.subTest(gate=gate, input_hash=approved_hash, run_id=run_id, decision=decision):
                record = self._record(gate, approved_hash, run_id=run_id, decision=decision)
                result = summary.apply(proposal["proposal_id"], record["approval_id"])
                self.assertEqual(result["reason_code"], reason, result)
                self.assertEqual(target.read_text(encoding="utf-8"), "before")

    def test_g10_rejected_retry_remains_rejected_on_terminal_replay(self):
        summary, proposal, target = self._proposal()
        record = self._reissued("G10", proposal["candidate_hash"], decision="REJECT")
        rejected = summary.apply(proposal["proposal_id"], record["approval_id"])
        self.assertEqual(rejected["reason_code"], "G10_REJECTED", rejected)
        replay = summary.apply(proposal["proposal_id"], record["approval_id"])
        self.assertEqual(replay["reason_code"], "G10_REJECTED", replay)
        self.assertEqual(replay["proposal_id"], proposal["proposal_id"])
        self.assertEqual(target.read_text(encoding="utf-8"), "before")


if __name__ == "__main__":
    unittest.main()
