"""R11A-007: a TypeError raised inside the sender is not proof the correction was not sent."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

import approval_delivery

OWNER = "owner@example.test"


class CorrectionTests(unittest.TestCase):
    def _orchestrator(self):
        orch = MagicMock()
        orch.approvals.get.return_value = {
            "approval_id": "a1", "run_id": "run-1", "input_hash": "h" * 64, "action": "G6",
            "status": "PENDING", "effective_decision": None,
            "deadline_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "member_policy": {"comate": [OWNER], "infoflow": [OWNER]},
        }
        orch.state.pending_intents.return_value = []
        orch.state.idempotency_result.return_value = {"content": "原消息"}
        orch.state.result_by_idempotency_key.return_value = None
        orch.state.claim_intent.return_value = {"status": "CLAIMED", "intent": {"intent_id": "i1"}}
        return orch

    def _correct(self, orch, client):
        with patch.object(approval_delivery, "execution_guard", return_value=None), \
                patch("run_change.superseded", return_value=False), \
                patch("approval_summary.pinned_context",
                             return_value={"material_status": "PINNED", "_review_content": {"draft": {}}}), \
                patch("approval_summary._is_diagnosis", return_value=True), \
                patch.object(approval_delivery, "group_id_for_run", return_value=None), \
                patch.object(approval_delivery, "_approval_markdown", return_value="纠正"):
            return approval_delivery.correct_approval_message(
                orch, "run-1", "a1", "h" * 64, actor=OWNER, reason="中文化", client=client)

    def test_a_type_error_after_sending_keeps_the_intent_unknown(self):
        orch = self._orchestrator()
        sent = []

        class Client:
            def send_markdown(self, recipients, content):
                sent.append(content)
                raise TypeError("bad response shape")

        result = self._correct(orch, Client())
        self.assertEqual(result["reason_code"], "QUERY_REQUIRED")
        orch.state.withdraw_intent.assert_not_called()
        self.assertEqual(len(sent), 1)

    def test_a_client_with_the_wrong_signature_is_refused_before_the_claim(self):
        orch = self._orchestrator()

        class Client:
            def send_markdown(self, only_one):
                raise AssertionError("must not be called")

        self.assertEqual(self._correct(orch, Client())["reason_code"], "CORRECTION_REJECTED")
        orch.state.claim_intent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
