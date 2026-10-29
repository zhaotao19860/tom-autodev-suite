"""Resume handoffs left open by explicit approval-consuming commands (MULTIPLE_RESUME_HANDOFFS)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ipipe_rebuild
import orchestrator as orchestrator_module
import worker_driver
import workflow_spec_migration
from approval_watch import auto_resume_from_hook
from orchestrator import Orchestrator

_CHANNELS = ["comate", "infoflow"]
_POLICY = {channel: ["owner@example.test"] for channel in _CHANNELS}
RUN = "run-stale"


class StaleResumeHandoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.orch = Orchestrator(Path(directory.name))
        self.state = self.orch.state
        # DIAGNOSE entry event, then the IPIPE event a rebuild appends.
        self.diagnose = self.state.transition(RUN, "DIAGNOSE", {"project": "bgw"})
        self.state.save_idempotency_result(f"run.driver:{RUN}:20261029T000000Z:sess-1",
                                           {"session_id": "sess-1"})
        self.counter = 0

    def _events(self):
        return self.state.events(RUN)

    def _approval(self, action, decision="APPROVE", input_hash=None):
        self.counter += 1
        input_hash = input_hash or f"hash-{self.counter}"
        record = self.orch.approvals.request(
            action, input_hash, _CHANNELS, run_id=RUN, member_policy=_POLICY)
        for channel in _CHANNELS:
            self.orch.approvals.record_delivery(
                record["approval_id"], channel, {"request_id": f"local-{channel}"},
                payload_hash=input_hash)
        self.orch.approvals.resolve(
            record["approval_id"], decision, input_hash, "comate", run_id=RUN,
            responder="owner@example.test", state_store=self.state)
        return self.orch.approvals.get(record["approval_id"])

    def _handoff(self, approval, event_id):
        self.state.record_handoff(
            RUN, f"approval-resume-{approval['approval_id']}-{event_id}",
            {"kind": "APPROVAL_RESUME", "run_id": RUN, "event_id": event_id,
             "approval_id": approval["approval_id"], "input_hash": approval["input_hash"],
             "action": approval["action"]})

    def _open(self):
        return [item["handoff_id"] for item in self.state.incomplete_handoffs(RUN)]

    def _rebuild_scenario(self):
        """Reproduce the real run: two handoffs on the DIAGNOSE event, then an IPIPE event."""
        old_event = self._events()[-1]["event_id"]
        migration = self._approval("WORKFLOW_SPEC_MIGRATION")
        g8 = self._approval("G8")
        self._handoff(migration, old_event)
        self._handoff(g8, old_event)
        self.state.transition(RUN, "IPIPE", {"rebuild": {"approval_id": g8["approval_id"]}})
        return migration, g8

    # -- source: explicit commands close the handoff -------------------------------------

    def test_complete_approval_handoffs_is_exact_audited_and_idempotent(self):
        old_event = self._events()[-1]["event_id"]
        mine, other = self._approval("G8"), self._approval("G4")
        self._handoff(mine, old_event)
        self._handoff(other, old_event)
        done = self.state.complete_approval_handoffs(
            RUN, mine["approval_id"], actor="ipipe-rebuild", reason="APPROVAL_CONSUMED")
        self.assertEqual(done, [f"approval-resume-{mine['approval_id']}-{old_event}"])
        self.assertEqual(self._open(), [f"approval-resume-{other['approval_id']}-{old_event}"])
        audit = self.state.idempotency_result(f"handoff-completion:{done[0]}")
        self.assertEqual((audit["actor"], audit["reason"]), ("ipipe-rebuild", "APPROVAL_CONSUMED"))
        self.assertEqual(
            self.state.complete_approval_handoffs(
                RUN, mine["approval_id"], actor="again", reason="APPROVAL_CONSUMED"), [])
        self.assertEqual(self.state.idempotency_result(f"handoff-completion:{done[0]}")["actor"],
                         "ipipe-rebuild")

    def test_migrate_workflow_spec_success_closes_its_handoff_and_failure_does_not(self):
        migration, g8 = self._rebuild_scenario()
        failure = {"ok": False, "reason_code": "APPROVAL_REQUIRED", "run_id": RUN}
        with mock.patch.object(workflow_spec_migration, "_apply", return_value=failure):
            workflow_spec_migration.apply(self.orch, RUN, migration["approval_id"])
        self.assertEqual(len(self._open()), 2)
        success = {"ok": True, "reason_code": "OK", "approval_id": migration["approval_id"]}
        with mock.patch.object(workflow_spec_migration, "_apply", return_value=success):
            workflow_spec_migration.apply(self.orch, RUN, migration["approval_id"])
            workflow_spec_migration.apply(self.orch, RUN, migration["approval_id"])
        self.assertEqual([h for h in self._open() if migration["approval_id"] in h], [])
        self.assertEqual(len(self._open()), 1)

    def test_ipipe_rebuild_trigger_and_reconcile_close_the_g8_handoff(self):
        for reconcile in (False, True):
            with self.subTest(reconcile_only=reconcile):
                self.setUp()
                _migration, g8 = self._rebuild_scenario()
                receipt = {"ok": True, "reason_code": "OK", "approval_id": g8["approval_id"]}
                with mock.patch.object(ipipe_rebuild, "_execute", return_value=receipt):
                    ipipe_rebuild.execute(
                        self.orch, object(), RUN, g8["approval_id"], g8["input_hash"],
                        reconcile_only=reconcile)
                self.assertEqual([h for h in self._open() if g8["approval_id"] in h], [])
                self.assertEqual(len(self._open()), 1)

    def test_rebuild_failure_keeps_the_handoff(self):
        _migration, g8 = self._rebuild_scenario()
        with mock.patch.object(ipipe_rebuild, "_execute",
                               return_value={"ok": False, "reason_code": "RUN_MOVED"}):
            ipipe_rebuild.execute(self.orch, object(), RUN, g8["approval_id"], g8["input_hash"])
        self.assertEqual(len(self._open()), 2)

    def test_cli_consumers_close_handoff_only_on_success(self):
        old_event = self._events()[0]["event_id"]
        approval = self._approval("G8")
        self._handoff(approval, old_event)
        settle = orchestrator_module._settle_consumed_approval
        for command, bad in (("ipipe-rerun", {"ok": False, "reason_code": "X"}),
                             ("advance", {"reason_code": "STALE_ACTION"}),
                             ("submit", {"ok": False, "reason_code": "APPROVAL_REQUIRED"})):
            settle(self.orch, RUN, approval["approval_id"], command, bad)
            self.assertEqual(len(self._open()), 1, command)
        settle(self.orch, RUN, approval["approval_id"], "ipipe-rerun", {"ok": True, "reason_code": "RERUN_TRIGGERED"})
        self.assertEqual(self._open(), [])
        # advance reports success as reason_code OK without an `ok` key.
        again = self._approval("G7")
        self._handoff(again, old_event)
        settle(self.orch, RUN, again["approval_id"], "advance", {"reason_code": "OK", "state": "IPIPE"})
        self.assertEqual(self._open(), [])

    # -- fallback: resume / hook reap only expired handoffs ------------------------------

    def test_resume_reaps_real_run_shape_then_reports_no_handoff(self):
        migration, g8 = self._rebuild_scenario()
        self.assertEqual(len(self._open()), 2)
        result = worker_driver.resume(self.orch, RUN)
        self.assertEqual(result["reason_code"], "NO_RESUME_HANDOFF")
        self.assertEqual(self._open(), [])
        for approval in (migration, g8):
            audit = self.state.idempotency_result(
                f"handoff-completion:approval-resume-{approval['approval_id']}-{self.diagnose['event_id']}")
            self.assertEqual(audit["reason"], "STALE_EVENT")
            self.assertEqual(audit["actor"], "worker_driver.resume")
        # Idempotent: a second resume changes nothing.
        self.assertEqual(worker_driver.resume(self.orch, RUN)["reason_code"], "NO_RESUME_HANDOFF")

    def test_resume_keeps_the_unique_handoff_that_matches_the_latest_event(self):
        _migration, _g8 = self._rebuild_scenario()
        latest = self._events()[-1]["event_id"]
        current = self._approval("G9")
        self._handoff(current, latest)
        with mock.patch.object(worker_driver, "advance", return_value={"ok": True, "reason_code": "PARKED"}) as advance:
            result = worker_driver.resume(self.orch, RUN)
        self.assertEqual(result["reason_code"], "PARKED")
        advance.assert_called_once()
        self.assertEqual(self._open(), [])

    def test_resume_still_refuses_when_several_match_the_latest_event(self):
        latest = self._events()[-1]["event_id"]
        first, second = self._approval("G4"), self._approval("G5")
        self._handoff(first, latest)
        self._handoff(second, latest)
        self.assertEqual(worker_driver.resume(self.orch, RUN)["reason_code"], "MULTIPLE_RESUME_HANDOFFS")
        self.assertEqual(len(self._open()), 2)

    def test_resume_refuses_when_stale_one_is_unconsumed_and_gate_matches(self):
        # Gate matches the current state and nothing consumed it: not provably expired.
        first = self._approval("G6")
        self._handoff(first, self._events()[-1]["event_id"])
        self.state.transition(RUN, "DIAGNOSE", {"note": "later event"})
        second = self._approval("G6")
        self._handoff(second, self._events()[-1]["event_id"])
        self.assertEqual(worker_driver.resume(self.orch, RUN)["reason_code"], "MULTIPLE_RESUME_HANDOFFS")
        self.assertEqual(len(self._open()), 2)

    def test_rejected_approval_handoff_is_never_reaped_or_released(self):
        old_event = self._events()[-1]["event_id"]
        rejected = self._approval("G8", decision="REJECT")
        self._handoff(rejected, old_event)
        self.state.transition(RUN, "IPIPE", {"rebuild": {"approval_id": rejected["approval_id"]}})
        current = self._approval("G9")
        self._handoff(current, self._events()[-1]["event_id"])
        with mock.patch.object(worker_driver, "advance") as advance:
            result = worker_driver.resume(self.orch, RUN)
        self.assertEqual(result["reason_code"], "MULTIPLE_RESUME_HANDOFFS")
        advance.assert_not_called()
        self.assertEqual(len(self._open()), 2)

    def test_single_rejected_handoff_is_not_released(self):
        rejected = self._approval("G4", decision="REJECT")
        self._handoff(rejected, self._events()[-1]["event_id"])
        with mock.patch.object(worker_driver, "advance") as advance:
            result = worker_driver.resume(self.orch, RUN)
        self.assertEqual(result["reason_code"], "RESUME_HANDOFF_STALE")
        advance.assert_not_called()

    def test_stop_hook_reaps_stale_and_resumes_the_matching_one(self):
        self._rebuild_scenario()
        current = self._approval("G9")
        self._handoff(current, self._events()[-1]["event_id"])
        result = auto_resume_from_hook(self.orch, {"hook_event_name": "Stop", "session_id": "sess-1"})
        self.assertEqual(result["reason_code"], "AUTO_RESUME")
        self.assertEqual(result["approval_id"], current["approval_id"])
        self.assertEqual(self._open(), [])

    def test_stop_hook_with_only_stale_handoffs_reports_none(self):
        self._rebuild_scenario()
        result = auto_resume_from_hook(self.orch, {"hook_event_name": "Stop", "session_id": "sess-1"})
        self.assertEqual(result["reason_code"], "NO_RESUME_HANDOFF")
        self.assertEqual(self._open(), [])


if __name__ == "__main__":
    unittest.main()
