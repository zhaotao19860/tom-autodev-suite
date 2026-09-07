import io
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_delivery import ComateApprovalClient, InfoflowApprovalTransport
from clients.infoflow_approval_client import InfoflowApprovalClient
from clients.infoflow_bot_client import InfoflowBotClient
from state_store import StateStore


_DEADLINE = "2026-08-28T20:00:00+00:00"
_NOW = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
_POLICY = {
    "comate": ["owner@example.test", "qa@example.test"],
    "infoflow": ["qa@example.test", "owner@example.test"],
}


def _envelope(input_hash="hash-a"):
    return {
        "run_id": "run-1",
        "approval_id": "approval-1",
        "channel": "infoflow",
        "action": "G0",
        "input_hash": input_hash,
        "deadline_at": _DEADLINE,
        "member_policy": _POLICY,
        "evidence": {
            "card_id": "BGW-1956",
            "card_title": "url",
            "card_url": "https://console.cloud.baidu-int.com/devops/icafe/issue/BGW-1956/show",
            "group_name": "BGW-1956-url",
            "documents": [
                {"label": "需求分析目录", "url": "https://ku.baidu-int.com/knowledge/a/b/c/d"},
                {"label": "BGW requirement analysis", "url": "https://ku.baidu-int.com/knowledge/e/f"},
                {"label": "坏链接", "url": "javascript:alert(1)"},
            ],
        },
    }


class FakeNotifyClient:
    def __init__(self):
        self.sends = []
        self.group_sends = []

    def send_markdown(self, recipients, content):
        self.sends.append((recipients, content))
        return {"message_key": f"key-{len(self.sends)}", "recipients": recipients}

    def send_group_markdown(self, group_id, content, at_users):
        self.group_sends.append((group_id, content, at_users))
        return {"message_id": f"group-{len(self.group_sends)}", "group_id": group_id}


class FakeCardClient(FakeNotifyClient):
    """A notify client whose gateway can also render the one-tap button card."""

    def __init__(self, *, error=None):
        super().__init__()
        self.cards = []
        self.error = error

    def send_approval_card(self, **request):
        self.cards.append(request)
        if self.error is not None:
            raise self.error
        return {"card_id": f"approval-{request['approval_id']}", "created": True}


class InfoflowApprovalTransportTests(unittest.TestCase):
    def _transport(self, notify, *, now=_NOW):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        state = StateStore(Path(self.directory.name) / "state.sqlite")
        return state, InfoflowApprovalTransport(state, notify, clock=lambda: now)

    def _with_group(self, state):
        intent = state.intent(
            "run-1",
            "infoflow.group.create",
            "infoflow.group.create:run-1",
            {"group_name": "BGW-1956-url"},
        )
        state.receipt(intent["intent_id"], {"run_id": "run-1", "group_id": "13403269"}, [])

    def test_request_returns_contract_shaped_pending_result(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)
        result = InfoflowApprovalClient(transport).request(
            {"channel": "infoflow", "approval": _envelope()}
        )
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(result["channel"], "infoflow")
        self.assertEqual(result["member_policy"], {key: sorted(_POLICY[key]) for key in _POLICY})
        self.assertTrue(result["request_id"].startswith("infoflow-"))
        recipients, content = notify.sends[0]
        self.assertEqual(recipients, sorted(_POLICY["infoflow"]))
        self.assertIn("APPROVE approval-1", content)
        self.assertIn("REJECT approval-1", content)
        # The gateway sends JSON, so line breaks travel as real newlines.
        self.assertIn("\n", content)
        self.assertNotIn("\\n", content)
        self.assertIn("BGW-1956", content)
        self.assertIn("2026-08-28 20:00 UTC", content)
        self.assertIn(
            "[BGW-1956](https://console.cloud.baidu-int.com/devops/icafe/issue/BGW-1956/show)",
            content,
        )
        self.assertIn("[需求分析目录](https://ku.baidu-int.com/knowledge/a/b/c/d)", content)
        # Infoflow renders a label with whitespace twice, so labels arrive hyphenated.
        self.assertIn("[BGW-requirement-analysis](https://ku.baidu-int.com/knowledge/e/f)", content)
        # A non-http link is dropped rather than rendered as a clickable decision aid.
        self.assertNotIn("javascript:", content)

    def test_the_message_says_what_is_approved_and_what_approving_causes(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)
        envelope = _envelope()
        envelope["action"] = "G1"
        envelope["evidence"]["gate"] = {
            "subject": "GRILL 决策日志：澄清结论、决策、验收点",
            "effect": "发布决策日志，进入 SPEC 起草",
            "summary": ["决策 3 条，验收点 13 条，未决前沿 0 条", ""],
            "content_hash": "249afb1dd94b2341ea98bc08152e9b41",
        }
        InfoflowApprovalClient(transport).request(
            {"channel": "infoflow", "approval": envelope}
        )
        _, content = notify.sends[0]
        self.assertIn("**本次审批** GRILL 决策日志：澄清结论、决策、验收点", content)
        self.assertIn("**批准后** 发布决策日志，进入 SPEC 起草", content)
        self.assertIn("- 决策 3 条，验收点 13 条，未决前沿 0 条", content)
        self.assertIn("产物哈希 `249afb1dd94b…`", content)
        # An empty summary line would render as a bare bullet.
        self.assertNotIn("\n- \n", content)

    def test_a_gate_without_summary_evidence_still_renders(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)
        InfoflowApprovalClient(transport).request(
            {"channel": "infoflow", "approval": _envelope()}
        )
        _, content = notify.sends[0]
        self.assertNotIn("**本次审批**", content)
        self.assertIn("APPROVE approval-1", content)

    def test_a_run_with_a_group_is_asked_in_the_group_and_mentions_the_approvers(self):
        notify = FakeNotifyClient()
        state, transport = self._transport(notify)
        intent = state.intent(
            "run-1",
            "infoflow.group.create",
            "infoflow.group.create:run-1",
            {"group_name": "BGW-1956-url"},
        )
        state.receipt(intent["intent_id"], {"run_id": "run-1", "group_id": "13403269"}, [])

        result = InfoflowApprovalClient(transport).request(
            {"channel": "infoflow", "approval": _envelope()}
        )

        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(notify.sends, [])
        group_id, content, at_users = notify.group_sends[0]
        self.assertEqual(group_id, "13403269")
        # A group mention resolves against the uuap prefix, so the address is cut down
        # both in the AT block and in the body tokens that make it read as a mention.
        self.assertEqual(at_users, ["owner", "qa"])
        self.assertIn("**待审批** @owner @qa", content)
        self.assertIn("APPROVE approval-1", content)

    def test_a_group_gate_also_gets_the_one_tap_button_card(self):
        notify = FakeCardClient()
        state, transport = self._transport(notify)
        self._with_group(state)

        transport.request(_envelope())

        card = notify.cards[0]
        # The buttons carry the approval id, so a tap reaches the journal as the same
        # decision a person would have typed; the markdown message still goes out.
        self.assertEqual(card["approval_id"], "approval-1")
        self.assertEqual(card["target_type"], "group")
        self.assertEqual(card["target_id"], "13403269")
        self.assertEqual(card["title"], "tom-autodev G0 审批")
        self.assertEqual(len(notify.group_sends), 1)

    def test_a_run_without_a_group_gets_no_card(self):
        notify = FakeCardClient()
        _, transport = self._transport(notify)

        transport.request(_envelope())

        # A private chat has no verified card target, and typing there was never the
        # thing that hurt, so the gate stays a markdown message.
        self.assertEqual(notify.cards, [])

    def test_a_rejected_card_leaves_the_gate_pending_and_records_why(self):
        notify = FakeCardClient(error=ValueError("MESSAGE_REJECTED:GATEWAY"))
        state, transport = self._transport(notify)
        self._with_group(state)

        result = transport.request(_envelope())

        self.assertEqual(result["status"], "PENDING")
        stored = state.idempotency_result(
            f"approval.gateway.infoflow:{result['request_id']}"
        )
        self.assertEqual(stored["card"], {"error": "MESSAGE_REJECTED:GATEWAY"})

    def test_a_run_without_a_group_still_goes_to_private_chats(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)

        InfoflowApprovalClient(transport).request(
            {"channel": "infoflow", "approval": _envelope()}
        )

        self.assertEqual(notify.group_sends, [])
        _, content = notify.sends[0]
        self.assertNotIn("**待审批**", content)

    def test_retry_reuses_the_stored_request_without_notifying_again(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)
        first = transport.request(_envelope())
        second = transport.request(_envelope())
        self.assertEqual(first, second)
        self.assertEqual(len(notify.sends), 1)

    def test_a_changed_input_hash_is_a_different_request(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)
        first = transport.request(_envelope())
        second = transport.request(_envelope("hash-b"))
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(len(notify.sends), 2)

    def test_wait_reports_pending_before_the_deadline(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)
        request_id = transport.request(_envelope())["request_id"]
        self.assertEqual(transport.wait(request_id, 5)["status"], "PENDING")

    def test_wait_after_the_deadline_times_out_and_records_a_handoff(self):
        notify = FakeNotifyClient()
        state, transport = self._transport(notify)
        request_id = transport.request(_envelope())["request_id"]
        transport.clock = lambda: datetime.fromisoformat(_DEADLINE) + timedelta(minutes=1)
        result = transport.wait(request_id, 5)
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertEqual(result["reason_code"], "APPROVAL_TIMEOUT")
        self.assertEqual(result["handoff"], {"handoff_id": "approval-timeout-approval-1", "status": "PENDING"})
        self.assertEqual(
            [item["handoff_id"] for item in state.incomplete_handoffs("run-1")],
            ["approval-timeout-approval-1"],
        )

    def test_wait_for_an_unknown_request_is_rejected(self):
        _, transport = self._transport(FakeNotifyClient())
        with self.assertRaises(ValueError):
            transport.wait("infoflow-missing", 5)

    def test_envelope_without_infoflow_members_is_rejected(self):
        _, transport = self._transport(FakeNotifyClient())
        envelope = {**_envelope(), "member_policy": {"comate": ["owner@example.test"], "infoflow": []}}
        with self.assertRaises(ValueError):
            transport.request(envelope)

    def test_reconcile_returns_a_delivered_request_and_nothing_otherwise(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)
        self.assertIsNone(transport.reconcile(_envelope()))
        delivered = transport.request(_envelope())
        self.assertEqual(transport.reconcile(_envelope()), delivered)
        self.assertIsNone(transport.reconcile(_envelope("hash-b")))
        self.assertEqual(len(notify.sends), 1)

    def test_client_reconcile_matches_the_bound_request(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)
        client = InfoflowApprovalClient(transport)
        envelope = {"channel": "infoflow", "approval": _envelope()}
        self.assertIsNone(client.reconcile(envelope))
        delivered = client.request(envelope)
        self.assertEqual(client.reconcile(envelope), delivered)
        self.assertEqual(len(notify.sends), 1)


class FakeGatewayOpener:
    """Answer localhost gateway calls without a process or a network."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append((request.method, request.full_url, request.data))
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Body(outcome)


class _Body:
    def __init__(self, payload):
        self.stream = io.BytesIO(payload.encode("utf-8"))

    def read(self):
        return self.stream.read()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False


class InfoflowBotClientTests(unittest.TestCase):
    def _client(self, responses, **options):
        self.opener = FakeGatewayOpener(responses)
        self.launched = []
        return InfoflowBotClient(
            base_url="http://127.0.0.1:18791",
            opener=self.opener,
            launcher=lambda *argv, **kwargs: self.launched.append(argv),
            sleeper=lambda _seconds: None,
            **options,
        )

    def test_send_goes_to_a_healthy_gateway_with_uuap_recipients(self):
        client = self._client(
            ['{"ok": true, "journal": "/tmp/replies.jsonl"}', '{"ok": true, "message_key": "mk-1"}']
        )
        receipt = client.send_markdown(["owner@example.test", "qa@example.test"], "body")
        self.assertEqual(receipt, {"message_key": "mk-1", "recipients": ["owner@example.test", "qa@example.test"]})
        self.assertEqual(self.opener.calls[1][0], "POST")
        self.assertIn(b'"owner"', self.opener.calls[1][2])
        self.assertIn(b'"qa"', self.opener.calls[1][2])
        self.assertEqual(self.launched, [])

    def test_an_absent_gateway_is_started_once_and_then_used(self):
        client = self._client(
            [
                urllib.error.URLError("refused"),
                '{"ok": true, "journal": "/tmp/replies.jsonl"}',
                '{"ok": true, "message_key": "mk-2"}',
            ]
        )
        receipt = client.send_markdown(["owner@example.test"], "body")
        self.assertEqual(receipt["message_key"], "mk-2")
        self.assertEqual(len(self.launched), 1)

    def test_a_gateway_that_never_answers_fails_the_send(self):
        client = self._client([urllib.error.URLError("refused")] * 4, startup_attempts=3)
        with self.assertRaisesRegex(ValueError, "INFOFLOW_GATEWAY_UNAVAILABLE"):
            client.send_markdown(["owner@example.test"], "body")

    def test_a_gateway_journaling_elsewhere_is_refused(self):
        client = self._client(['{"ok": true, "journal": "/tmp/other.jsonl"}'], journal_path="/tmp/replies.jsonl")
        with self.assertRaisesRegex(ValueError, "INFOFLOW_GATEWAY_JOURNAL_MISMATCH"):
            client.send_markdown(["owner@example.test"], "body")

    def test_a_rejected_send_is_never_reported_as_delivered(self):
        rejected = urllib.error.HTTPError("http://127.0.0.1:18791/notify", 500, "err", {}, None)
        client = self._client(['{"ok": true, "journal": "/tmp/replies.jsonl"}', rejected])
        with self.assertRaisesRegex(ValueError, "MESSAGE_REJECTED:GATEWAY"):
            client.send_markdown(["owner@example.test"], "body")

    def test_a_response_without_a_message_key_is_invalid(self):
        client = self._client(['{"ok": true, "journal": "/tmp/replies.jsonl"}', '{"ok": true}'])
        with self.assertRaisesRegex(ValueError, "MESSAGE_RESPONSE_INVALID"):
            client.send_markdown(["owner@example.test"], "body")

    def test_a_member_without_a_usable_address_is_rejected(self):
        client = self._client(['{"ok": true, "journal": "/tmp/replies.jsonl"}'])
        with self.assertRaisesRegex(ValueError, "MEMBER_CONFIRMATION_REQUIRED"):
            client.send_markdown(["owner"], "body")


class ComateApprovalClientTests(unittest.TestCase):
    def test_request_echoes_the_bound_approval(self):
        receipt = ComateApprovalClient().request({"channel": "comate", "approval": _envelope()})
        self.assertEqual(receipt["approval_id"], "approval-1")
        self.assertEqual(receipt["input_hash"], "hash-a")
        self.assertEqual(receipt["surface"], "comate-cli")

    def test_request_without_an_approval_is_rejected(self):
        with self.assertRaises(ValueError):
            ComateApprovalClient().request({"channel": "comate"})


if __name__ == "__main__":
    unittest.main()
