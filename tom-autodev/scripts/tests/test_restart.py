"""Contract 3.4: restart-from checks the source's leftovers and commits atomically."""

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

import restart
from orchestrator import Orchestrator
from test_orchestrator import PROFILE, _requirement_snapshot, _start, _write_profile


class RestartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _write_profile(self.root, PROFILE)
        self.orch = Orchestrator(self.root)
        self.source = _start(self.orch)["run_id"]
        self.snapshot = _requirement_snapshot("BGW-1")

    def _submitted_cr(self, number="1001"):
        claim = self.orch.state.claim_intent(self.source, "icode.submit", f"icode:{number}", {"n": number})
        self.orch.state.receipt(claim["intent"]["intent_id"], {
            "ok": True, "change_number": number, "module": "bgw", "change_id": "I1",
        }, [f"icode-cr-{number}"])

    def test_a_clean_source_restarts_once_and_replays(self):
        result = restart.execute(self.orch, self.source, self.snapshot, reason="需求变更")
        self.assertEqual(result["reason_code"], "RESTARTED", result)
        target = result["target_run_id"]
        self.assertEqual(self.orch.status(self.source)["state"], "STOPPED")
        target_events = self.orch.state.events(target)
        self.assertEqual([event["state"] for event in target_events], ["INTAKE"])
        self.assertEqual(target_events[0]["payload"]["restart_of"], self.source)
        self.assertEqual(self.orch.state.lineage(target)["source_run_id"], self.source)
        again = restart.execute(self.orch, self.source, self.snapshot, reason="需求变更")
        self.assertEqual(again["reason_code"], "RESTART_REPLAY")
        self.assertEqual(again["target_run_id"], target)

    def test_an_unreceipted_write_blocks_and_nothing_is_written(self):
        self.orch.state.claim_intent(self.source, "ipipe.rerun", "ipipe.rerun:x", {"stage_build_id": "s"})
        before = len(self.orch.state.events(self.source))
        result = restart.execute(self.orch, self.source, self.snapshot, reason="x")
        self.assertEqual(result["reason_code"], "RESTART_SOURCE_UNRESOLVED")
        self.assertEqual(result["blockers"][0]["kind"], "intent")
        self.assertEqual(len(self.orch.state.events(self.source)), before)
        self.assertIsNone(self.orch.state.lineage(self.source))

    def test_a_side_channel_write_does_not_block(self):
        self.orch.state.claim_intent(self.source, "ku.document.create", "ku:x", {"doc": "d"})
        self.assertTrue(restart.plan(self.orch, self.source)["ok"])

    def test_cr_dispositions(self):
        self._submitted_cr()
        for status, options, ok, disposition in [
            ("NEW", {}, False, "OPEN_UNDECIDED"),
            ("NEW", {"keep_open_crs": True}, True, "KEPT_FOR_REFERENCE"),
            ("ABANDONED", {}, True, "ABANDONED_CONFIRMED"),
            ("MERGED", {}, False, "MERGED_UNDECIDED"),
            ("MERGED", {"accept_merged": True}, True, "MERGED_ACCEPTED"),
            (None, {"keep_open_crs": True}, False, "ABANDONED_UNKNOWN"),
        ]:
            with self.subTest(status=status, options=options):
                planned = restart.plan(self.orch, self.source, cr_status=lambda _cr, s=status: s, **options)
                self.assertEqual(planned["ok"], ok, planned)
                self.assertEqual(planned["lineage"]["crs"][0]["disposition"], disposition)

    def test_a_running_or_unreadable_build_blocks(self):
        self.orch.state.save_idempotency_result(f"ipipe.build-binding:{self.source}:b1", {"build_id": "b1"})
        for status, ok in [("RUNNING", False), (None, False), ("SUCC", True), ("FAILING", True), ("SUCCEEDING", True)]:
            with self.subTest(status=status):
                planned = restart.plan(self.orch, self.source, build_status=lambda _b, s=status: s)
                self.assertEqual(planned["ok"], ok, planned)

    def test_an_excluded_or_unknown_build_still_blocks(self):
        self.orch.state.save_idempotency_result(f"ipipe.build-binding:{self.source}:b2", {"build_id": "b2"})
        self.orch.state.save_idempotency_result(f"ipipe.build-rejected:{self.source}:b2", {"build_id": "b2"})
        for status in ("RUNNING", "UNKNOWN"):
            with self.subTest(status=status):
                planned = restart.plan(self.orch, self.source, build_status=lambda _b, s=status: s)
                self.assertFalse(planned["ok"], planned)

    def test_an_intent_claimed_after_the_check_aborts_the_commit(self):
        original = restart.plan

        def plan_then_race(*args, **kwargs):
            result = original(*args, **kwargs)
            self.orch.state.claim_intent(self.source, "ipipe.trigger", "late", {"x": 1})
            return result

        with unittest.mock.patch("restart.plan", side_effect=plan_then_race):
            result = restart.execute(self.orch, self.source, self.snapshot, reason="x")
        self.assertEqual(result["reason_code"], "SOURCE_INTENTS_CHANGED")
        self.assertIsNone(self.orch.state.lineage(self.source))

    def test_environment_diagnoses_are_not_inherited_as_repair_rounds(self):
        envelope = lambda route, cls: {"valid": True, "envelope": {"content": {
            "evidence_state": "SUFFICIENT", "route": route, "classification": cls}}}
        with unittest.mock.patch.object(self.orch.artifacts, "phase_artifacts", return_value=[
                envelope("IPIPE", "ENV_UNSATISFIED"), envelope("REPAIR", "CODE_FAILURE")]):
            self.assertEqual(restart._repair_rounds(self.orch, self.source), 1)

    def test_a_changed_card_is_diffed_bound_to_g0_and_regrilled(self):
        from clients.icafe_client import requirement_snapshot_hash

        changed = {k: v for k, v in self.snapshot.items() if k != "content_hash"}
        changed["acceptance"] = ["AC-1", "AC-2"]
        changed["content_hash"] = requirement_snapshot_hash(changed)
        result = restart.execute(self.orch, self.source, changed, reason="需求变更")
        intake = self.orch.state.events(result["target_run_id"])[0]["payload"]
        self.assertIn("acceptance", intake["requirement_diff"]["changed"])
        self.assertEqual(intake["intake_hash_version"], "v5")
        self.assertNotEqual(intake["workflow_modes"].get("GRILL"), "auto")

    def test_attachment_or_link_changes_are_a_diff(self):
        for field, value in (("attachments", [{"name": "spec.pdf"}]), ("links", ["http://x"])):
            with self.subTest(field=field):
                after = dict(self.snapshot, **{field: value}, content_hash="new")
                self.assertIn(field, restart.requirement_diff(self.snapshot, after)["changed"])

    def test_v4_intake_hash_is_unchanged(self):
        from collaboration import intake_input_hash
        payload = {"intake_hash_version": "v4", "approval_mode": "scope", "project": "p"}
        self.assertEqual(intake_input_hash(payload), intake_input_hash({**payload, "requirement_diff": None}))

    def test_a_v4_intake_hashed_by_3ff3ee1_still_validates(self):
        from collaboration import _INTAKE_PREREQUISITE_KEYS_V5, _hash, _intake_hash_matches
        payload = {"intake_hash_version": "v4", "approval_mode": "scope", "project": "p"}
        payload["g0_input_hash"] = _hash({k: payload.get(k) for k in _INTAKE_PREREQUISITE_KEYS_V5})
        self.assertTrue(_intake_hash_matches(payload))
        self.assertFalse(_intake_hash_matches({**payload, "project": "q"}))

    def test_a_released_source_cannot_restart(self):
        self.orch.state.transition(self.source, "RELEASE_SUCCESS", {})
        self.assertEqual(restart.plan(self.orch, self.source)["reason_code"], "RESTART_SOURCE_RELEASED")

    def test_a_reason_is_required(self):
        self.assertEqual(restart.execute(self.orch, self.source, self.snapshot, reason=" ")["reason_code"],
                         "RESTART_REASON_REQUIRED")


if __name__ == "__main__":
    unittest.main()


class NoticeIntentTests(unittest.TestCase):
    def test_an_unreceipted_notice_does_not_block_resume(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        _write_profile(root, PROFILE)
        orch = Orchestrator(root)
        run_id = _start(orch)["run_id"]
        orch.state.claim_intent(run_id, "approval.resume_notice", "resume-notice:x", {"run_id": run_id})
        self.assertEqual(orch.recovery.resume(run_id)["actions"], [])
        orch.state.claim_intent(run_id, "icode.submit", "icode:x", {"n": 1})
        self.assertEqual(len(orch.recovery.resume(run_id)["actions"]), 1)


class PendingSubmitReconcileTests(unittest.TestCase):
    def test_continue_settles_an_unreceipted_submit_by_replaying_it(self):
        from unittest.mock import patch

        import worker_driver

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        _write_profile(root, PROFILE)
        orch = Orchestrator(root)
        run_id = _start(orch)["run_id"]
        orch.state.transition(run_id, "SUBMIT", {"task_id": "BGW-1-T1"})
        orch.state.claim_intent(run_id, "icode.submit", "icode.submit:x", {"module": "m"})
        with patch("orchestrator._submit", return_value={"ok": True, "state": "IPIPE"}) as replay:
            result = worker_driver._reconcile_pending_submit(orch, run_id, None)
        replay.assert_called_once_with(orch, run_id, "BGW-1-T1", None, None)
        self.assertEqual(result["state"], "IPIPE")
