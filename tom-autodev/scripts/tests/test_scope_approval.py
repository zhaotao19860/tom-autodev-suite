"""MVP §2: one scope card standing for several gates, expanded into per-gate approvals."""

import tempfile
import unittest
from pathlib import Path

from approval_ledger import ApprovalLedger, gate_of, scope_members

ALICE, BOB = "alice@example.test", "bob@example.test"
WIDE = {"comate": [ALICE, BOB], "infoflow": [ALICE, BOB]}
NARROW = {"comate": [ALICE], "infoflow": [ALICE]}
GATES = [("G4", "a" * 64), ("G5", "b" * 64), ("G7", "c" * 64)]


class ScopeApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = ApprovalLedger(Path(self.temp.name) / "a.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def card(self, gates=GATES, policies=(WIDE, NARROW, WIDE)):
        row = self.ledger.request_scope("C", gates, ["comate", "infoflow"], run_id="run-1",
                                        member_policies=list(policies), policy_hash="p1")
        for channel in ("comate", "infoflow"):
            self.ledger.record_delivery(row["approval_id"], channel, {"request_id": channel},
                                        payload_hash=row["input_hash"])
        return row

    def approve(self, row, responder=ALICE):
        self.ledger.resolve(row["approval_id"], "APPROVE", row["input_hash"], "comate",
                            run_id="run-1", responder=responder)

    def test_members_are_the_intersection_of_every_gate(self):
        self.assertEqual(scope_members([WIDE, NARROW])["comate"], [ALICE])
        with self.assertRaises(ValueError):
            self.ledger.request_scope("C", GATES, ["comate", "infoflow"], run_id="run-1",
                                      member_policies=[{"comate": [ALICE], "infoflow": [ALICE]},
                                                       {"comate": [BOB], "infoflow": [BOB]},
                                                       WIDE],
                                      policy_hash="p1")

    def test_policies_must_align_one_per_gate(self):
        with self.assertRaisesRegex(ValueError, "SCOPE_POLICIES_MISALIGNED"):
            self.ledger.request_scope("C", GATES, ["comate", "infoflow"], run_id="run-1",
                                      member_policies=[WIDE], policy_hash="p1")

    def test_gate_order_does_not_change_the_scope_hash(self):
        row = self.card()
        self.approve(row)
        derived = self.ledger.expand_scope(row["approval_id"], "C", list(reversed(GATES)),
                                           run_id="run-1", policy_hash="p1")
        self.assertEqual(len(derived), 3)

    def test_a_pending_gate_with_narrower_members_is_not_satisfied(self):
        self.ledger.request("G5", "b" * 64, ["comate", "infoflow"], run_id="run-1",
                            member_policy={"comate": [BOB], "infoflow": [BOB]})
        row = self.ledger.request_scope("C", GATES, ["comate", "infoflow"], run_id="run-1",
                                        member_policies=[WIDE, WIDE, WIDE], policy_hash="p1")
        for channel in ("comate", "infoflow"):
            self.ledger.record_delivery(row["approval_id"], channel, {"request_id": channel},
                                        payload_hash=row["input_hash"])
        self.approve(row)
        with self.assertRaisesRegex(ValueError, "SCOPE_GATE_POLICY_NARROWER"):
            self.ledger.expand_scope(row["approval_id"], "C", GATES, run_id="run-1", policy_hash="p1")

    def test_only_an_intersection_member_may_answer(self):
        row = self.card()
        with self.assertRaises(ValueError):
            self.approve(row, responder=BOB)

    def test_an_approved_card_expands_into_each_gate_with_its_own_hash(self):
        row = self.card()
        self.approve(row)
        derived = self.ledger.expand_scope(row["approval_id"], "C", GATES, run_id="run-1", policy_hash="p1")
        self.assertEqual([(gate_of(d["action"]), d["input_hash"]) for d in derived], GATES)
        self.assertTrue(all(d["effective_decision"] == "APPROVE" for d in derived))
        self.assertTrue(all(d["effective_channel"] == f"scope:{row['approval_id']}" for d in derived))
        again = self.ledger.expand_scope(row["approval_id"], "C", GATES, run_id="run-1", policy_hash="p1")
        self.assertEqual([d["approval_id"] for d in again], [d["approval_id"] for d in derived])

    def test_a_changed_artifact_or_rule_does_not_expand(self):
        row = self.card()
        self.approve(row)
        changed = [("G4", "a" * 64), ("G5", "d" * 64), ("G7", "c" * 64)]
        with self.assertRaises(ValueError):
            self.ledger.expand_scope(row["approval_id"], "C", changed, run_id="run-1", policy_hash="p1")
        with self.assertRaises(ValueError):
            self.ledger.expand_scope(row["approval_id"], "C", GATES, run_id="run-1", policy_hash="p2")
        self.assertEqual([r for r in self.ledger.for_run("run-1") if gate_of(r["action"]) == "G5"], [])

    def test_an_unapproved_card_does_not_expand(self):
        row = self.card()
        with self.assertRaises(ValueError):
            self.ledger.expand_scope(row["approval_id"], "C", GATES, run_id="run-1", policy_hash="p1")

    def test_a_gate_that_was_stopped_is_not_overridden(self):
        stopped = self.ledger.request("G5", "b" * 64, ["comate", "infoflow"], run_id="run-1",
                                      member_policy=WIDE)
        for channel in ("comate", "infoflow"):
            self.ledger.record_delivery(stopped["approval_id"], channel, {"request_id": channel},
                                        payload_hash="b" * 64)
        self.ledger.resolve(stopped["approval_id"], "REJECT", "b" * 64, "comate", run_id="run-1", responder=ALICE)
        row = self.card()
        self.approve(row)
        with self.assertRaises(ValueError):
            self.ledger.expand_scope(row["approval_id"], "C", GATES, run_id="run-1", policy_hash="p1")


if __name__ == "__main__":
    unittest.main()
