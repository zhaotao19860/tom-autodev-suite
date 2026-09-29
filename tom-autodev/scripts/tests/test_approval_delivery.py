import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_delivery import (
    ComateApprovalClient, InfoflowApprovalTransport, _deliver_approval_card, _request_id,
)
from approval_summary import content_summary
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
        self.assertIn("**本次审批**", content)
        self.assertIn("材料的位置", content)
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

    def test_a_run_without_a_group_gets_individual_private_cards(self):
        notify = FakeCardClient()
        _, transport = self._transport(notify)

        transport.request(_envelope())

        self.assertEqual(
            [(card["target_type"], card["target_id"]) for card in notify.cards],
            [("user", "owner"), ("user", "qa")],
        )
        self.assertTrue(all(len(card["lines"]) <= 7 for card in notify.cards))
        self.assertEqual(len(notify.sends), 1)

    def test_a_rejected_card_leaves_the_gate_pending_and_records_why(self):
        notify = FakeCardClient(error=ValueError("MESSAGE_REJECTED:GATEWAY"))
        state, transport = self._transport(notify)
        self._with_group(state)

        result = transport.request(_envelope())

        self.assertEqual(result["status"], "PENDING")
        stored = state.idempotency_result(
            f"approval.gateway.infoflow:{result['request_id']}"
        )
        self.assertEqual(stored["card"]["status"], "DEGRADED")
        self.assertEqual(stored["card"]["targets"][0]["status"], "UNKNOWN")
        self.assertEqual(stored["card"]["targets"][0]["reason_code"], "ValueError")
        self.assertEqual(len(notify.group_sends), 2)
        warning = notify.group_sends[1][1]
        self.assertIn("不是新审批", warning)
        self.assertIn("这不表示审批已通过", warning)
        self.assertIn("APPROVE / REJECT", warning)

    def test_card_type_error_does_not_replay_the_markdown_message(self):
        notify = FakeCardClient(error=TypeError("card signature mismatch"))
        state, transport = self._transport(notify)
        self._with_group(state)

        first = transport.request(_envelope())
        second = transport.request(_envelope())

        self.assertEqual(first, second)
        self.assertEqual(len(notify.group_sends), 2)  # detail + explicit degradation warning
        self.assertEqual(len(notify.cards), 1)

    def test_card_runtime_error_is_recorded_as_best_effort_failure(self):
        notify = FakeCardClient(error=RuntimeError("card gateway down"))
        state, transport = self._transport(notify)
        self._with_group(state)

        result = transport.request(_envelope())
        stored = state.idempotency_result(f"approval.gateway.infoflow:{result['request_id']}")

        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(stored["card"]["status"], "DEGRADED")
        self.assertEqual(stored["card"]["targets"][0]["reason_code"], "RuntimeError")

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
        self.assertEqual(len(notify.sends), 2)  # detail + unsupported-button warning

    def test_a_changed_input_hash_is_a_different_request(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)
        first = transport.request(_envelope())
        second = transport.request(_envelope("hash-b"))
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(len(notify.sends), 4)

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
        self.assertEqual(len(notify.sends), 2)

    def test_client_reconcile_matches_the_bound_request(self):
        notify = FakeNotifyClient()
        _, transport = self._transport(notify)
        client = InfoflowApprovalClient(transport)
        envelope = {"channel": "infoflow", "approval": _envelope()}
        self.assertIsNone(client.reconcile(envelope))
        delivered = client.request(envelope)
        self.assertEqual(client.reconcile(envelope), delivered)
        self.assertEqual(len(notify.sends), 2)

    def test_private_target_aliases_are_deduplicated_before_card_send(self):
        notify = FakeCardClient()
        _, transport = self._transport(notify)
        envelope = _envelope()
        envelope["member_policy"] = {
            **_POLICY, "infoflow": ["owner@a.test", "owner@b.test"],
        }
        transport.request(envelope)
        self.assertEqual([card["target_id"] for card in notify.cards], ["owner"])

    def test_partial_card_failure_and_restart_do_not_resend_any_target(self):
        class PartialClient(FakeCardClient):
            def send_approval_card(self, **request):
                receipt = super().send_approval_card(**request)
                if request["target_id"] == "qa":
                    raise TimeoutError("remote result unknown")
                return receipt

        notify = PartialClient()
        state, transport = self._transport(notify)
        first = transport.request(_envelope())
        restarted = InfoflowApprovalTransport(
            StateStore(Path(self.directory.name) / "state.sqlite"), notify, clock=lambda: _NOW
        )
        self.assertEqual(restarted.request(_envelope()), first)
        self.assertEqual(len(notify.cards), 2)
        self.assertEqual(len(notify.sends), 2)
        card = state.idempotency_result(
            f"approval.gateway.infoflow:{first['request_id']}"
        )["card"]
        self.assertEqual([item["status"] for item in card["targets"]], ["DELIVERED", "UNKNOWN"])
        for target in ("owner", "qa"):
            intent = state.idempotency_result(
                f"approval.button:{first['request_id']}:user:{target}:intent"
            )
            self.assertRegex(intent["attempt"], r"^[0-9a-f]{32}$")
        self.assertEqual(first["status"], "PENDING")

    def test_card_intent_without_receipt_is_not_resent_but_other_target_is_sent(self):
        notify = FakeCardClient()
        state, _ = self._transport(notify)
        envelope = _envelope()
        state.save_idempotency_result(
            f"approval.button:{_request_id(envelope)}:user:owner:intent", {"attempt": "crashed"}
        )
        first = _deliver_approval_card(notify, state, envelope)
        second = _deliver_approval_card(notify, state, envelope)
        self.assertEqual(first, second)
        self.assertEqual([card["target_id"] for card in notify.cards], ["qa"])
        self.assertEqual(first["targets"][0]["reason_code"], "QUERY_REQUIRED")

    def test_invalid_card_receipt_is_unknown_and_never_replayed(self):
        notify = FakeCardClient()
        state, _ = self._transport(notify)
        with patch.object(notify, "send_approval_card", return_value={"card_id": 42}) as send:
            first = _deliver_approval_card(notify, state, _envelope())
            second = _deliver_approval_card(notify, state, _envelope())
        self.assertEqual(first, second)
        self.assertEqual(send.call_count, 2)
        self.assertTrue(all(item["status"] == "UNKNOWN" for item in first["targets"]))

    def test_partial_markdown_send_is_not_blindly_retried(self):
        notify = FakeCardClient()
        _, transport = self._transport(notify)

        def partial_send(*args):
            notify.sends.append(args)
            raise TimeoutError("one recipient may have received the message")

        with patch.object(notify, "send_markdown", side_effect=partial_send):
            with self.assertRaises(TimeoutError):
                transport.request(_envelope())
            with self.assertRaisesRegex(ValueError, "APPROVAL_DELIVERY_QUERY_REQUIRED"):
                transport.request(_envelope())
        self.assertEqual(len(notify.sends), 1)
        self.assertEqual(notify.cards, [])
        self.assertIsNone(transport.reconcile(_envelope()))

    def test_local_dispatch_defect_can_retry_through_orchestrator_after_client_fix(self):
        from orchestrator import Orchestrator

        for group in (False, True):
            for defect in ("missing_method", "not_callable", "wrong_signature"):
                with self.subTest(group=group, defect=defect), tempfile.TemporaryDirectory() as root:
                    orch = Orchestrator(Path(root))
                    if group:
                        self._with_group(orch.state)
                    approval = orch.approvals.request(
                        "G0", "hash-a", ["comate", "infoflow"],
                        run_id="run-1", member_policy=_POLICY,
                    )
                    envelope = {**_envelope(), **approval, "channel": "infoflow"}
                    broken = type("BrokenClient", (), {})()
                    method = "send_group_markdown" if group else "send_markdown"
                    if defect == "not_callable":
                        setattr(broken, method, None)
                    elif defect == "wrong_signature":
                        def wrong_signature():
                            self.fail("signature mismatch must be detected before entering the body")
                        setattr(broken, method, wrong_signature)
                    transport = InfoflowApprovalTransport(orch.state, broken)
                    client = InfoflowApprovalClient(transport)

                    def deliver():
                        return orch._deliver_approval_channel(
                            "run-1", approval["approval_id"], "infoflow", client,
                            {"channel": "infoflow", "approval": envelope}, "hash-a",
                        )

                    first = deliver()
                    self.assertEqual(first["reason_code"], "APPROVAL_DELIVERY_FAILED", first)
                    self.assertTrue(first["retry_allowed"])
                    intent_after_failure = orch.state.idempotency_result(
                        f"approval.delivery:{_request_id(envelope)}:intent"
                    )
                    fixed = FakeCardClient()
                    transport.notify_client = fixed
                    second = deliver()
                    self.assertEqual(second["reason_code"], "OK", second)
                    self.assertIsNone(intent_after_failure)
                    self.assertEqual(deliver()["reason_code"], "OK")
                    self.assertEqual(len(fixed.group_sends if group else fixed.sends), 1)
                    self.assertEqual(len(fixed.cards), 1 if group else 2)

    def test_dispatch_body_type_errors_remain_unknown_and_never_retry(self):
        # An exception class alone cannot prove nothing was sent: the client may
        # raise while handling a response after a successful remote dispatch.
        for error_type in (AttributeError, TypeError):
            with self.subTest(error_type=error_type):
                notify = FakeCardClient()
                state, transport = self._transport(notify)

                def sent_then_failed(recipients, content):
                    notify.sends.append((recipients, content))
                    raise error_type("response handling failed")

                with patch.object(notify, "send_markdown", new=sent_then_failed):
                    with self.assertRaises(error_type):
                        transport.request(_envelope())
                self.assertIsNotNone(state.idempotency_result(
                    f"approval.delivery:{_request_id(_envelope())}:intent"
                ))
                with self.assertRaisesRegex(ValueError, "APPROVAL_DELIVERY_QUERY_REQUIRED"):
                    transport.request(_envelope())
                self.assertEqual(len(notify.sends), 1)
                self.assertEqual(notify.cards, [])

    def test_crash_before_final_record_does_not_replay_details_or_cards(self):
        notify = FakeCardClient()
        state, transport = self._transport(notify)
        original = state.save_idempotency_result

        def crash(key, value):
            if key.startswith("approval.gateway.infoflow:"):
                raise KeyboardInterrupt("crash after send")
            return original(key, value)

        with patch.object(state, "save_idempotency_result", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                transport.request(_envelope())
        restarted = InfoflowApprovalTransport(state, notify, clock=lambda: _NOW)
        with self.assertRaisesRegex(ValueError, "APPROVAL_DELIVERY_QUERY_REQUIRED"):
            restarted.request(_envelope())
        self.assertEqual(len(notify.sends), 1)
        self.assertEqual(len(notify.cards), 2)

    def test_concurrent_transport_claim_precedes_every_send(self):
        notify = FakeCardClient()
        _, transport = self._transport(notify)
        entered, release = Event(), Event()
        original = notify.send_markdown

        def hold(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timed out")
            return original(*args)

        other = InfoflowApprovalTransport(
            StateStore(Path(self.directory.name) / "state.sqlite"), notify, clock=lambda: _NOW
        )
        with patch.object(notify, "send_markdown", side_effect=hold):
            with ThreadPoolExecutor(max_workers=1) as pool:
                worker = pool.submit(transport.request, _envelope())
                try:
                    self.assertTrue(entered.wait(5))
                    with self.assertRaisesRegex(ValueError, "APPROVAL_DELIVERY_QUERY_REQUIRED"):
                        other.request(_envelope())
                finally:
                    release.set()
                first = worker.result(timeout=5)
        self.assertEqual(other.request(_envelope()), first)
        self.assertEqual(len(notify.sends), 1)
        self.assertEqual(len(notify.cards), 2)

    def test_invalid_deadline_is_rejected_before_claim_or_send(self):
        notify = FakeCardClient()
        state, transport = self._transport(notify)
        envelope = {**_envelope(), "deadline_at": "bad"}
        with self.assertRaisesRegex(ValueError, "APPROVAL_DEADLINE_INVALID"):
            transport.request(envelope)
        self.assertIsNone(state.idempotency_result(
            f"approval.delivery:{_request_id(envelope)}:intent"
        ))
        self.assertEqual(notify.sends, [])

    def test_failed_degradation_warning_is_not_retried(self):
        notify = FakeCardClient(error=TimeoutError())
        state, transport = self._transport(notify)
        original = notify.send_markdown

        def warning_fails(recipients, content):
            receipt = original(recipients, content)
            if "不是新审批" in content:
                raise TimeoutError()
            return receipt

        with patch.object(notify, "send_markdown", side_effect=warning_fails):
            first = transport.request(_envelope())
            self.assertEqual(transport.request(_envelope()), first)
        self.assertEqual(len(notify.sends), 2)
        self.assertEqual(state.idempotency_result(
            f"approval.button.warning:{first['request_id']}"
        )["status"], "UNKNOWN")


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

    def test_private_card_uses_real_client_endpoint_without_network(self):
        client = self._client([
            '{"ok": true, "journal": "/tmp/replies.jsonl"}',
            '{"ok": true, "card_id": "approval-target-owner", "created": true}',
        ])
        result = client.send_approval_card(
            approval_id="a" * 32, target_type="user", target_id="owner",
            title="审批", question="请先核对材料", lines=["材料"],
        )
        method, url, body = self.opener.calls[1]
        self.assertEqual((method, url), ("POST", "http://127.0.0.1:18791/card/approval"))
        self.assertEqual(json.loads(body)["target_type"], "user")
        self.assertEqual(json.loads(body)["target_id"], "owner")
        self.assertEqual(result["card_id"], "approval-target-owner")
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


class ChangeSetSummaryTests(unittest.TestCase):
    """What the G5 card says about a diff that did not follow the approved plan.

    The approver already read the Task Plan at G4, so the one thing they cannot
    reconstruct from it is where the diff left it. Counting the deviations and naming
    their reasons is the difference between approving a diff and approving a hash.
    """

    def _change_set(self, deviations):
        return {
            "change_set_id": "CS-1", "task_id": "T-1", "full_diff_hash": "a" * 64,
            "test_ids": ["resolver.answer"],
            "traceability_delta": [{"acceptance_point_id": "AC-1", "test_ids": ["resolver.answer"]}],
            "deviations": deviations,
        }

    def test_the_card_counts_the_deviations_and_names_their_reasons(self):
        summary = content_summary(self._change_set([
            {"from_plan": "guard in resolver_answer", "as_implemented": "guard in resolver_prepare",
             "reason": "resolver_answer runs after the budget check"},
        ]))

        self.assertIn("偏离计划 1 处", summary[1])
        self.assertIn("偏离：resolver_answer runs after the budget check", summary)

    def test_a_diff_that_followed_the_plan_says_so_rather_than_staying_silent(self):
        summary = content_summary(self._change_set([]))

        self.assertIn("偏离计划 0 处", summary[1])
        self.assertEqual([line for line in summary if line.startswith("偏离：")], [])

    def test_g7_submit_descriptor_names_module_branch_and_revision(self):
        summary = content_summary({
            "change_set_id": "CS-1",
            "revision_set_id": "RS-1",
            "module": "baidu/nsiqa/x86bgw",
            "target_branch": "pipline_case",
            "commit_revision": "b" * 40,
            "revision_set": {
                "business": {
                    "module": "baidu/bgw",
                    "branch": "master",
                    "revision": "a" * 40,
                },
                "test": {
                    "module": "baidu/nsiqa/x86bgw",
                    "branch": "pipline_case",
                    "revision": "b" * 40,
                },
            },
        })

        self.assertIn("主仓库 baidu/nsiqa/x86bgw，分支 pipline_case", summary)
        self.assertIn("提交版本 bbbbbbbbbbbb…", summary)
        self.assertTrue(any(
            line.startswith("business 仓库 baidu/bgw，分支 master，版本")
            for line in summary
        ))
        self.assertTrue(any(
            line.startswith("test 仓库 baidu/nsiqa/x86bgw，分支 pipline_case")
            for line in summary
        ))
        self.assertNotIn("任务 -", summary)

    def test_g7_submit_descriptor_names_create_new_cr(self):
        summary = content_summary({
            "change_set_id": "CS-1",
            "revision_set_id": "RS-1",
            "module": "module",
            "target_branch": "branch",
            "commit_revision": "revision",
            "submission_mode": "create_new_cr",
            "revision_set": {},
        })
        self.assertEqual(summary[0], "提交方式 新建 CR")

    def test_g7_submit_descriptor_can_identify_existing_cr_append(self):
        summary = content_summary({
            "change_set_id": "CS-1",
            "revision_set_id": "RS-1",
            "module": "module",
            "target_branch": "branch",
            "commit_revision": "revision",
            "existing_cr": "122402145",
            "revision_set": {},
        })

        self.assertEqual(summary[0], "提交方式 追加现有 CR")


if __name__ == "__main__":
    unittest.main()
