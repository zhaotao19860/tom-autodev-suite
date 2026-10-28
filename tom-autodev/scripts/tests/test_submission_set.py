"""Contract 3.2: the aggregate of a multi-repository submission attempt."""

import tempfile
import unittest
from pathlib import Path

import intent_executor as ie
import submission_set as ss
from state_store import StateStore

RUN, REVSET = "run-1", "revset-1"
PRIMARY, COMPANION = "change-1", "change-1:companion:baidu/team/tests"


class SubmissionSetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temp.name) / "s.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def claim(self, change_set_id, module):
        key = f"icode.submit:{RUN}:{change_set_id}:{REVSET}"
        return self.state.claim_intent(RUN, "icode.submit", key, {"module": module})["intent"]

    def confirm(self, intent, number):
        self.state.receipt(intent["intent_id"], {"ok": True, "reason_code": "OK",
                                                 "change_number": number}, [f"icode-cr-{number}"])

    def evaluate(self):
        return ss.evaluate(self.state, RUN, [PRIMARY, COMPANION], REVSET, PRIMARY)

    def test_nothing_attempted_is_pending(self):
        record = self.evaluate()
        self.assertEqual(record["status"], ss.PENDING)
        self.assertEqual({r["intent_state"] for r in record["repositories"]}, {"NOT_ATTEMPTED"})

    def test_one_confirmed_and_one_unknown_is_pending_not_submitted(self):
        self.confirm(self.claim(COMPANION, "tests"), "1")
        unknown = self.claim(PRIMARY, "app")
        ie.settle(self.state, unknown, ie.Verdict.unknown())
        self.assertEqual(self.evaluate()["status"], ss.PENDING)
        self.assertIsNone(ss.recorded(self.state, RUN, PRIMARY, REVSET))

    def test_one_confirmed_and_one_ambiguous_is_partial(self):
        self.confirm(self.claim(COMPANION, "tests"), "1")
        ie.settle(self.state, self.claim(PRIMARY, "app"), ie.Verdict.ambiguous(candidates=["7", "8"]))
        self.assertEqual(self.evaluate()["status"], ss.PARTIAL)

    def test_one_confirmed_and_one_refused_is_partial(self):
        self.confirm(self.claim(COMPANION, "tests"), "1")
        refused = self.claim(PRIMARY, "app")
        self.state.receipt(refused["intent_id"], {"ok": False, "reason_code": "SUBMIT_REJECTED"}, [])
        record = self.evaluate()
        self.assertEqual(record["status"], ss.PARTIAL)
        self.assertIn("REFUSED", {r["intent_state"] for r in record["repositories"]})

    def test_an_ambiguous_repository_is_partial_even_without_a_confirmed_sibling(self):
        ie.settle(self.state, self.claim(PRIMARY, "app"), ie.Verdict.ambiguous(candidates=["7", "8"]))
        self.assertEqual(self.evaluate()["status"], ss.PARTIAL)

    def test_a_confirmed_receipt_without_a_change_number_is_reported_not_pending(self):
        self.confirm(self.claim(COMPANION, "tests"), "1")
        intent = self.claim(PRIMARY, "app")
        self.state.receipt(intent["intent_id"], {"ok": True, "reason_code": "OK"}, [])
        record = self.evaluate()
        self.assertEqual(record["status"], ss.PARTIAL)
        self.assertEqual(record["reason_code"], "RECEIPT_MISSING_CHANGE_NUMBER")

    def test_a_different_record_for_a_submitted_attempt_is_a_conflict(self):
        self.confirm(self.claim(COMPANION, "tests"), "1")
        self.confirm(self.claim(PRIMARY, "app"), "2")
        self.state.save_idempotency_result(ss.attempt_key(RUN, PRIMARY, REVSET), {"status": "other"})
        # The stored record wins the read; evaluate never overwrites it.
        self.assertEqual(self.evaluate(), {"status": "other"})

    def test_all_confirmed_is_submitted_and_recorded_once(self):
        self.confirm(self.claim(COMPANION, "tests"), "1")
        self.confirm(self.claim(PRIMARY, "app"), "2")
        first = self.evaluate()
        self.assertEqual(first["status"], ss.SUBMITTED)
        self.assertEqual(ss.recorded(self.state, RUN, PRIMARY, REVSET), first)
        self.assertEqual(self.evaluate(), first)

    def test_a_retry_under_the_abandoned_lineage_is_the_one_that_counts(self):
        self.confirm(self.claim(COMPANION, "tests"), "1")
        first = self.claim(PRIMARY, "app")
        self.state.abandon_intent(first["intent_id"], "operator", "tester")
        key = f"icode.submit:{RUN}:{PRIMARY}:{REVSET}:after:{first['intent_id']}"
        retry = self.state.claim_intent(RUN, "icode.submit", key, {"module": "app"})["intent"]
        self.confirm(retry, "3")
        self.assertEqual(self.evaluate()["status"], ss.SUBMITTED)


class RoutePartialTests(unittest.TestCase):
    """A PARTIAL attempt goes to F (DIAGNOSE); PENDING and successes are left alone."""

    class Orchestrator:
        def __init__(self):
            self.routed = []

        def route_failure(self, run_id, reason_code, evidence):
            self.routed.append((run_id, reason_code, evidence))
            return {"ok": True, "state": "DIAGNOSE"}

    def test_partial_routes_once_with_the_repository_states(self):
        from orchestrator import _route_partial
        orch = self.Orchestrator()
        result = {"ok": False, "reason_code": "CR_IDENTITY_AMBIGUOUS", "submission_set": {
            "status": ss.PARTIAL, "change_set_id": PRIMARY, "revision_set_id": REVSET,
            "repositories": [{"change_set_id": PRIMARY, "intent_id": "i1", "intent_state": "AMBIGUOUS"}]}}
        routed = _route_partial(result, orch, RUN)
        self.assertEqual(routed["routed"]["state"], "DIAGNOSE")
        self.assertEqual([r[1] for r in orch.routed], ["SUBMIT_PARTIAL"])

    def test_pending_and_success_are_not_routed(self):
        from orchestrator import _route_partial
        orch = self.Orchestrator()
        _route_partial({"ok": False, "submission_set": {"status": ss.PENDING}}, orch, RUN)
        _route_partial({"ok": True, "submission_set": {"status": ss.SUBMITTED}}, orch, RUN)
        self.assertEqual(orch.routed, [])

class PartialHeldTests(unittest.TestCase):
    """Codex round 4: the PARTIAL exemption lasts while the run is out of SUBMIT."""

    def events(self, last_state):
        route = {"state": "DIAGNOSE", "payload": {"reason_code": "SUBMIT_PARTIAL", "evidence": {
            "submission_set": {"repositories": [{"intent_id": "i-amb", "intent_state": "AMBIGUOUS"}]}}}}
        return [{"state": "SUBMIT", "payload": {}}, route] + (
            [{"state": last_state, "payload": {}}] if last_state else [])

    def test_held_in_diagnosis_and_later_phases(self):
        from recovery import partial_held
        self.assertEqual(partial_held(self.events(None)), frozenset({"i-amb"}))
        self.assertEqual(partial_held(self.events("IMPLEMENT")), frozenset({"i-amb"}))

    def test_back_in_submit_the_intent_holds_the_run_again(self):
        from recovery import partial_held
        self.assertEqual(partial_held(self.events("SUBMIT")), frozenset())

if __name__ == "__main__":
    unittest.main()
