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


if __name__ == "__main__":
    unittest.main()
