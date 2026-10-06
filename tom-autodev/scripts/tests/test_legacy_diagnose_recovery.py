import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker_driver


RUN_ID = "run-legacy"
EVENT_ID = "diagnose-event"
TASK_ID = "T1"
ACTION_ID = "old-action"
JOB_ID = f"producer:{ACTION_ID}"
PARENT_HASH = "p" * 64


def action():
    return {
        "ok": True,
        "action_id": ACTION_ID,
        "run_id": RUN_ID,
        "source_event_id": EVENT_ID,
        "state": "DIAGNOSE",
        "phase": "DIAGNOSE",
        "child_skill": "tom-diagnose",
        "task_id": TASK_ID,
        "input_hash": "input",
        "parent_artifact_hash": PARENT_HASH,
        "source_revisions": {"business": "b1", "tests": "t1"},
        "source_evidence_refs": [],
        "required_human_gate": "G6",
        "result_schema": "diagnosis",
    }


class LegacyDiagnoseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.orch = Mock()
        self.orch.state.pending_producer_jobs.return_value = [{
            "job_id": JOB_ID,
            "run_id": RUN_ID,
            "status": "PENDING",
            "payload": {
                "phase": "DIAGNOSE",
                "action_id": ACTION_ID,
                "source_event_id": EVENT_ID,
                "task_id": TASK_ID,
            },
        }]
        self.orch.state.producer_job.return_value = self.orch.state.pending_producer_jobs.return_value[0]
        self.orch.state.events.return_value = [{
            "state": "DIAGNOSE",
            "event_id": EVENT_ID,
            "payload": {
                "task_id": TASK_ID,
                "evidence": {"producer_block_report_id": PARENT_HASH},
            },
        }]
        self.orch.state.idempotency_result.return_value = action()
        protocol = Mock()
        protocol._repin.return_value = None
        protocol.legacy_diagnose_action.return_value = {
            **action(), "source_evidence_refs": ["artifact:" + PARENT_HASH],
        }
        protocol._legacy_diagnose_binding_valid.return_value = True
        self.orch.phase_protocol.return_value = protocol

    def test_current_action_reuses_only_the_historical_pending_job(self):
        recovered = worker_driver._recover_legacy_diagnose_action(
            self.orch, RUN_ID, JOB_ID)
        self.assertEqual(recovered["action_id"], ACTION_ID)
        self.assertEqual(recovered["source_evidence_refs"], ["artifact:" + PARENT_HASH])
        self.orch.state.idempotency_result.assert_called_once()

    def test_compat_action_builds_envelope_with_repaired_evidence(self):
        recovered = worker_driver._recover_legacy_diagnose_action(
            self.orch, RUN_ID, JOB_ID)
        envelope = worker_driver.build_envelope(recovered, {"hypothesis": "h"})
        self.assertEqual(envelope["evidence_refs"], ["artifact:" + PARENT_HASH])
        self.assertEqual(envelope["action_id"], ACTION_ID)

    def test_empty_repaired_refs_are_fail_closed(self):
        self.orch.phase_protocol().legacy_diagnose_action.return_value = {
            **action(), "source_evidence_refs": []
        }
        self.assertIsNone(
            worker_driver._recover_legacy_diagnose_action(self.orch, RUN_ID, JOB_ID)
        )

    def test_forged_repaired_refs_are_not_accepted(self):
        self.orch.phase_protocol().legacy_diagnose_action.return_value = {
            **action(), "source_evidence_refs": ["artifact:forged"]
        }
        self.assertIsNone(
            worker_driver._recover_legacy_diagnose_action(self.orch, RUN_ID, JOB_ID)
        )

    def test_source_revision_mismatch_is_rejected(self):
        self.orch.phase_protocol().legacy_diagnose_action.return_value = {
            **action(),
            "source_revisions": {"business": "other", "tests": "t1"},
            "source_evidence_refs": ["artifact:" + PARENT_HASH],
        }
        self.assertIsNone(
            worker_driver._recover_legacy_diagnose_action(self.orch, RUN_ID, JOB_ID)
        )

    def test_parent_artifact_mismatch_is_rejected(self):
        self.orch.state.idempotency_result.return_value = {
            **action(), "parent_artifact_hash": "q" * 64
        }
        self.assertIsNone(
            worker_driver._recover_legacy_diagnose_action(self.orch, RUN_ID, JOB_ID)
        )

    def test_wrong_run_task_or_job_is_rejected(self):
        for run_id, job_id in [
            ("other-run", JOB_ID),
            (RUN_ID, "producer:other-action"),
        ]:
            with self.subTest(run_id=run_id, job_id=job_id):
                self.assertIsNone(
                    worker_driver._recover_legacy_diagnose_action(
                        self.orch, run_id, job_id))

        self.orch.state.producer_job.return_value = {
            **self.orch.state.pending_producer_jobs.return_value[0],
            "payload": {
                **self.orch.state.pending_producer_jobs.return_value[0]["payload"],
                "task_id": "T2",
            },
        }
        self.assertIsNone(
            worker_driver._recover_legacy_diagnose_action(self.orch, RUN_ID, JOB_ID))

    def test_fulfilled_job_is_never_recovered(self):
        fulfilled = copy.deepcopy(self.orch.state.pending_producer_jobs.return_value[0])
        fulfilled["status"] = "FULFILLED"
        self.orch.state.producer_job.return_value = fulfilled
        self.assertIsNone(
            worker_driver._recover_legacy_diagnose_action(self.orch, RUN_ID, JOB_ID))


if __name__ == "__main__":
    unittest.main()
