import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import workflow_spec
import workflow_spec_migration
from execution_guard import execution_guard
from orchestrator import Orchestrator
from test_orchestrator import PROFILE, _start, _write_profile


class WorkflowSpecMigrationTests(unittest.TestCase):
    def _run(self, directory):
        root = Path(directory)
        _write_profile(root, PROFILE)
        owner = Orchestrator(root)
        return owner, _start(owner)["run_id"]

    def test_approved_migration_unblocks_execution_without_rewriting_events(self):
        with tempfile.TemporaryDirectory() as directory:
            owner, run_id = self._run(directory)
            original = workflow_spec.canonical_hash
            workflow_spec.canonical_hash = lambda: "new-workflow-policy"
            try:
                prepared = workflow_spec_migration.plan(owner, run_id)
                request = owner.approvals.request(
                    workflow_spec_migration.ACTION,
                    prepared["input_hash"],
                    ["comate", "infoflow"],
                    run_id=run_id,
                    member_policy={
                        "comate": ["a@example.test"],
                        "infoflow": ["a@example.test"],
                    },
                )
                for channel in ("comate", "infoflow"):
                    owner.approvals.record_delivery(
                        request["approval_id"],
                        channel,
                        {"message_key": f"migration-{channel}"},
                        payload_hash=prepared["input_hash"],
                    )
                owner.approvals.resolve(
                    request["approval_id"],
                    "APPROVE",
                    prepared["input_hash"],
                    "comate",
                    run_id=run_id,
                    responder="a@example.test",
                )
                applied = workflow_spec_migration.apply(owner, run_id, request["approval_id"])
                events_before = owner.state.events(run_id)
                unblocked = execution_guard(owner.state, run_id)
                effective = workflow_spec_migration.effective_pinned_hash(owner.state, run_id)
            finally:
                workflow_spec.canonical_hash = original

        self.assertTrue(prepared["ok"])
        self.assertEqual(applied["reason_code"], "OK")
        self.assertEqual(len(events_before), 1)
        self.assertIsNone(unblocked)
        self.assertEqual(effective, "new-workflow-policy")

    def _open_migration_gate(self, owner, run_id, prepared):
        request = owner.approvals.request(
            workflow_spec_migration.ACTION,
            prepared["input_hash"],
            ["comate", "infoflow"],
            run_id=run_id,
            member_policy={
                "comate": ["a@example.test"],
                "infoflow": ["a@example.test"],
            },
        )
        for channel in ("comate", "infoflow"):
            owner.approvals.record_delivery(
                request["approval_id"],
                channel,
                {"message_key": f"migration-{channel}"},
                payload_hash=prepared["input_hash"],
            )
        return request

    def test_drifted_run_can_still_land_its_own_migration_decision(self):
        """The gate that authorizes the migration must be answerable under drift.

        `Orchestrator.approve` is the real landing path; guarding it unconditionally
        let the card be delivered while the decision never reached the ledger, so
        `apply()` stayed APPROVAL_REQUIRED and the run could never leave drift.
        """
        with tempfile.TemporaryDirectory() as directory:
            owner, run_id = self._run(directory)
            original = workflow_spec.canonical_hash
            workflow_spec.canonical_hash = lambda: "new-workflow-policy"
            try:
                prepared = workflow_spec_migration.plan(owner, run_id)
                request = self._open_migration_gate(owner, run_id, prepared)
                landed = owner.approve(
                    request["approval_id"],
                    "APPROVE",
                    prepared["input_hash"],
                    "comate",
                    run_id=run_id,
                    responder="a@example.test",
                )
                applied = workflow_spec_migration.apply(owner, run_id, request["approval_id"])
                unblocked = execution_guard(owner.state, run_id)
            finally:
                workflow_spec.canonical_hash = original

        self.assertEqual(landed.get("effective_decision"), "APPROVE")
        self.assertEqual(applied["reason_code"], "OK")
        self.assertIsNone(unblocked)

    def test_drift_still_blocks_a_business_gate_decision(self):
        """Only the migration gate is exempt; an ordinary gate must stay refused."""
        with tempfile.TemporaryDirectory() as directory:
            owner, run_id = self._run(directory)
            request = owner.approvals.request(
                "G5",
                "a" * 64,
                ["comate", "infoflow"],
                run_id=run_id,
                member_policy={
                    "comate": ["a@example.test"],
                    "infoflow": ["a@example.test"],
                },
            )
            for channel in ("comate", "infoflow"):
                owner.approvals.record_delivery(
                    request["approval_id"], channel, {"message_key": channel},
                    payload_hash="a" * 64,
                )
            original = workflow_spec.canonical_hash
            workflow_spec.canonical_hash = lambda: "new-workflow-policy"
            try:
                refused = owner.approve(
                    request["approval_id"], "APPROVE", "a" * 64, "comate",
                    run_id=run_id, responder="a@example.test",
                )
                unresolved = owner.approvals.get(request["approval_id"])["effective_decision"]
            finally:
                workflow_spec.canonical_hash = original

        self.assertEqual(refused["reason_code"], "WORKFLOW_SPEC_DRIFT")
        self.assertIsNone(unresolved)

    def test_wrong_approval_cannot_move_the_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            owner, run_id = self._run(directory)
            original = workflow_spec.canonical_hash
            workflow_spec.canonical_hash = lambda: "new-workflow-policy"
            try:
                prepared = workflow_spec_migration.plan(owner, run_id)
                request = owner.approvals.request(
                    workflow_spec_migration.ACTION,
                    "f" * 64,
                    ["comate", "infoflow"],
                    run_id=run_id,
                    member_policy={
                        "comate": ["a@example.test"],
                        "infoflow": ["a@example.test"],
                    },
                )
                result = workflow_spec_migration.apply(owner, run_id, request["approval_id"])
                record = workflow_spec_migration.record_for(owner.state, run_id)
            finally:
                workflow_spec.canonical_hash = original

        self.assertEqual(result["reason_code"], "APPROVAL_BINDING_MISMATCH")
        self.assertIsNone(record)

    def test_repeated_policy_changes_keep_migrating_the_same_run(self):
        """A run may outlive more than one control-plane policy change.

        The chain used to be read from its newest record, so the second approved
        migration was ignored: the run stayed drifted and a third migration collided
        with the second one's key as MIGRATION_CONFLICT.
        """
        with tempfile.TemporaryDirectory() as directory:
            owner, run_id = self._run(directory)
            original = workflow_spec.canonical_hash
            applied = []
            effective = []
            try:
                for policy in ("policy-y", "policy-z", "policy-w"):
                    workflow_spec.canonical_hash = lambda policy=policy: policy
                    prepared = workflow_spec_migration.plan(owner, run_id)
                    self.assertTrue(prepared["ok"], prepared)
                    request = self._open_migration_gate(owner, run_id, prepared)
                    owner.approve(
                        request["approval_id"], "APPROVE", prepared["input_hash"],
                        "comate", run_id=run_id, responder="a@example.test",
                    )
                    applied.append(workflow_spec_migration.apply(owner, run_id, request["approval_id"]))
                    effective.append(workflow_spec_migration.effective_pinned_hash(owner.state, run_id))
                    self.assertIsNone(execution_guard(owner.state, run_id), policy)
            finally:
                workflow_spec.canonical_hash = original

        self.assertEqual([item["reason_code"] for item in applied], ["OK", "OK", "OK"])
        self.assertEqual(effective, ["policy-y", "policy-z", "policy-w"])


if __name__ == "__main__":
    unittest.main()
