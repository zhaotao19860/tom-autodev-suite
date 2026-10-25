"""B-03: `adopt-code-only-repair` validates what it carries into the new run.

(a) the source run's spent repair rounds travel with the adoption and an exhausted
budget refuses it; (b) only the task's latest diagnosis and its accepted Plan qualify;
(c) an adopted target only replays the same adoption; (d) a submission receipt must
name the task; (e) the entry is a controlled transition recorded with its evidence,
without a new workflow-spec edge (the spec hash is unchanged).
"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import workflow_spec
from orchestrator import Orchestrator
from test_ipipe_runtime import approved
from test_phase_protocol_repair import final_envelope
from test_schema_validation import specialized_examples

SOURCE, TARGET = "run-source", "run-target"
INTAKE = {"requirement_id": "BGW-1", "project": "bgw", "profile_hash": "a" * 64}
PROFILE = {
    "business_repos": [{"module": "resolver", "path": "/repo/bgw", "branch": "main"}],
    "test_repo": {"module": "resolver-tests", "path": "/repo/bgw-tests", "branch": "main"},
}
SPEC_HASH_BEFORE_B03 = "76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410"


class AdoptCodeOnlyRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.orch = Orchestrator(Path(self.temp.name))
        self.state, self.artifacts = self.orch.state, self.orch.artifacts
        patcher = patch.object(self.orch, "_runtime_profile",
                               return_value={"ok": True, "profile": copy.deepcopy(PROFILE)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state.transition(SOURCE, "INTAKE", dict(INTAKE))
        self.tasks = self.artifacts.put_envelope(
            final_envelope(SOURCE, "TASKS", None, specialized_examples()["task-dag"]))
        self.plan = self.artifacts.put_envelope(final_envelope(
            SOURCE, "PLAN", "T-1", specialized_examples()["task-plan"],
            parent_hash=self.tasks["envelope"]["content_hash"],
            revisions={"business": "r1", "tests": "t1"}))
        self.state.transition(SOURCE, "IMPLEMENT", {
            "previous_state": "PLAN", "task_id": "T-1", "artifact_id": self.plan["artifact_id"]})
        self.receipt()

    def receipt(self, task_id="T-1", run_id=SOURCE, key="icode.submit:source:T-1"):
        payload = {"change_set_id": "cs"} if task_id is None else {"task_id": task_id}
        claim = self.state.claim_intent(run_id, "icode.submit", key, payload)
        self.state.receipt(claim["intent"]["intent_id"], {
            "ok": True, "reason_code": "OK", "module": "resolver",
            "change_number": "41", "change_id": "I" + "a" * 40,
        }, [])

    def diagnose(self, signature, *, event=None):
        event = event or self.state.transition(SOURCE, "DIAGNOSE", {"task_id": "T-1"})
        content = specialized_examples()["diagnosis"]
        content.update({"repair_scope": "CODE_ONLY", "failure_signature": signature,
                        "repair_diff_hash": None})
        envelope = final_envelope(SOURCE, "DIAGNOSE", "T-1", content)
        envelope["source_event_id"] = event["event_id"]
        envelope["approval_input_hash"] = canonical(signature)
        stored = self.artifacts.put_envelope(envelope)
        approved(self.orch.approvals, "G6", envelope["approval_input_hash"], SOURCE)
        return stored

    def stop_source(self):
        self.state.transition(SOURCE, "STOPPED", {})

    def new_target(self):
        self.state.transition(TARGET, "INTAKE", dict(INTAKE))

    def adopt(self, diagnosis, plan=None):
        return self.orch.adopt_code_only_repair(
            TARGET, SOURCE, TARGET, "T-1", diagnosis["artifact_id"],
            (plan or self.plan)["artifact_id"])

    # (e) + happy path ----------------------------------------------------------------

    def test_adoption_enters_implement_through_a_recorded_controlled_decision(self):
        diagnosis = self.diagnose("SIG-1")
        self.stop_source()
        self.new_target()

        result = self.adopt(diagnosis)

        self.assertTrue(result["ok"], result)
        event = self.state.events(TARGET)[-1]
        self.assertEqual(event["state"], "IMPLEMENT")
        decision = event["payload"]["policy_decision"]
        self.assertEqual((decision["controlled"], decision["current_state"], decision["next_state"]),
                         ("CODE_ONLY_ADOPTION", "INTAKE", "IMPLEMENT"))
        self.assertEqual(event["payload"]["inherited_repair_rounds"], 1)
        self.assertEqual(event["payload"]["source_revisions"], {"business": "r1", "tests": "t1"})
        self.assertEqual(event["payload"]["plan_artifact_id"], result["plan_artifact_id"])
        self.assertEqual(result["event_id"], event["event_id"])
        # No new workflow-spec edge: pinned runs see no drift.
        self.assertNotIn("IMPLEMENT", workflow_spec.allowed_transitions()["INTAKE"])
        self.assertEqual(workflow_spec.canonical_hash(), SPEC_HASH_BEFORE_B03)

    # (a) ---------------------------------------------------------------------------

    def test_inherited_rounds_count_toward_the_target_repair_limit(self):
        for number in range(1, 4):
            latest = self.diagnose(f"SIG-{number}")
            if number == 3:
                break
        self.state.transition(SOURCE, "ARCHITECTURE_REVIEW", {})
        self.stop_source()
        self.new_target()
        self.assertTrue(self.adopt(latest)["ok"])

        protocol = self.orch.phase_protocol()
        history = protocol._repair_history(TARGET, {"content": specialized_examples()["diagnosis"],
                                                    "content_hash": "f" * 64})

        self.assertEqual(sum(1 for item in history if item.get("diagnosis_confirmed")), 4)

    def test_a_source_that_spent_its_five_rounds_cannot_be_adopted(self):
        for number in range(1, 6):
            latest = self.diagnose(f"SIG-{number}")
            if number == 3:
                self.state.transition(SOURCE, "ARCHITECTURE_REVIEW", {})
        self.stop_source()
        self.new_target()

        result = self.adopt(latest)

        self.assertEqual(result["reason_code"], "ADOPTION_BUDGET_EXHAUSTED", result)
        self.assertEqual((result["repair_rounds"], result["budget_reason"]), (5, "REPAIR_LIMIT"))
        self.assertEqual(self.state.events(TARGET)[-1]["state"], "INTAKE")

    def test_three_failed_fixes_needs_an_architecture_review_after_the_diagnosis(self):
        for number in range(1, 4):
            latest = self.diagnose(f"SIG-{number}")
        self.stop_source()
        self.new_target()
        self.assertEqual(self.adopt(latest)["reason_code"], "ADOPTION_BUDGET_EXHAUSTED")

    # (b) ---------------------------------------------------------------------------

    def test_only_the_latest_diagnosis_of_the_task_qualifies(self):
        older = self.diagnose("SIG-1")
        self.diagnose("SIG-2")
        self.stop_source()
        self.new_target()
        self.assertEqual(self.adopt(older)["reason_code"], "ADOPTION_DIAGNOSIS_NOT_LATEST")

    def test_the_plan_must_be_the_accepted_plan_of_the_current_dag(self):
        diagnosis = self.diagnose("SIG-1")
        draft = self.artifacts.put_envelope(final_envelope(
            SOURCE, "PLAN", "T-1", {**specialized_examples()["task-plan"], "plan_id": "P-unaccepted"},
            parent_hash=self.tasks["envelope"]["content_hash"],
            revisions={"business": "r1", "tests": "t1"}))
        self.stop_source()
        self.new_target()

        self.assertEqual(self.adopt(diagnosis, draft)["reason_code"], "ADOPTION_PLAN_NOT_CURRENT")
        # The latest *accepted* Plan is still the approved one.
        self.assertTrue(self.adopt(diagnosis, self.plan)["ok"])

    def test_a_plan_whose_dag_was_superseded_is_refused(self):
        diagnosis = self.diagnose("SIG-1")
        amended = specialized_examples()["task-dag"]
        amended["nodes"][0]["capability_slice"] += " amended"
        self.artifacts.put_envelope(final_envelope(SOURCE, "TASKS", None, amended))
        self.stop_source()
        self.new_target()
        self.assertEqual(self.adopt(diagnosis)["reason_code"], "ADOPTION_PLAN_NOT_CURRENT")

    # (c) ---------------------------------------------------------------------------

    def test_replay_is_idempotent_and_a_different_pair_conflicts(self):
        diagnosis = self.diagnose("SIG-1")
        self.stop_source()
        self.new_target()
        first = self.adopt(diagnosis)
        events = len(self.state.events(TARGET))

        again = self.adopt(diagnosis)
        other = self.orch.adopt_code_only_repair(
            TARGET, SOURCE, TARGET, "T-1", diagnosis["artifact_id"], "plan-other")

        self.assertTrue(again["ok"], again)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["event_id"], first["event_id"])
        self.assertEqual(len(self.state.events(TARGET)), events)
        self.assertEqual(other["reason_code"], "ADOPTION_CONFLICT", other)

    # (d) ---------------------------------------------------------------------------

    def test_a_receipt_without_a_task_id_is_not_trusted(self):
        self.state.transition("run-bare", "INTAKE", dict(INTAKE))
        # Re-seed the source with only a task-less receipt.
        source_temp = tempfile.TemporaryDirectory()
        self.addCleanup(source_temp.cleanup)
        self.orch = Orchestrator(Path(source_temp.name))
        self.state, self.artifacts = self.orch.state, self.orch.artifacts
        patcher = patch.object(self.orch, "_runtime_profile",
                               return_value={"ok": True, "profile": copy.deepcopy(PROFILE)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state.transition(SOURCE, "INTAKE", dict(INTAKE))
        self.tasks = self.artifacts.put_envelope(
            final_envelope(SOURCE, "TASKS", None, specialized_examples()["task-dag"]))
        self.plan = self.artifacts.put_envelope(final_envelope(
            SOURCE, "PLAN", "T-1", specialized_examples()["task-plan"],
            parent_hash=self.tasks["envelope"]["content_hash"],
            revisions={"business": "r1", "tests": "t1"}))
        self.state.transition(SOURCE, "IMPLEMENT", {
            "previous_state": "PLAN", "task_id": "T-1", "artifact_id": self.plan["artifact_id"]})
        self.receipt(task_id=None)
        diagnosis = self.diagnose("SIG-1")
        self.stop_source()
        self.new_target()

        self.assertEqual(self.adopt(diagnosis)["reason_code"], "SUBMISSION_RECEIPT_REQUIRED")


def canonical(signature):
    import hashlib
    return hashlib.sha256(f"g6:{signature}".encode()).hexdigest()


if __name__ == "__main__":
    unittest.main()
