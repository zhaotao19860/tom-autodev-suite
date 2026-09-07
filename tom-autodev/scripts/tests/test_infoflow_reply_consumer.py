import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_delivery import InfoflowApprovalTransport
from clients.infoflow_reply_client import InfoflowReplyConsumer, InfoflowReplyJournal
from state_store import StateStore


_OPENED = "2026-08-28T10:00:00+00:00"
_DEADLINE = "2026-08-28T20:00:00+00:00"
_NOW = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
_POLICY = {
    "comate": ["owner@example.test", "qa@example.test"],
    "infoflow": ["owner@example.test", "qa@example.test"],
}


def _record(**overrides):
    return {
        "request_id": "infoflow-req-1",
        "approval_id": "approval-1",
        "run_id": "run-1",
        "channel": "infoflow",
        "input_hash": "hash-a",
        "member_policy": _POLICY,
        "status": "PENDING",
        "created_at": _OPENED,
        "deadline_at": _DEADLINE,
        "heartbeat_at": None,
        "updated_at": None,
        "reply": None,
        "handoff": None,
        "reason_code": None,
        **overrides,
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
    }


class FakeJournal:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return sorted(self._messages, key=lambda item: (item["received_at"], item["message_id"]))


class FakeNotifyClient:
    def __init__(self):
        self.sends = []

    def send_markdown(self, recipients, content):
        self.sends.append((recipients, content))
        return {"message_key": f"key-{len(self.sends)}", "recipients": recipients}


def _message(text, *, sender="owner", message_id="msg-1", received_at="2026-08-28T11:00:00+00:00"):
    return {
        "message_id": message_id,
        "chat_type": "single",
        "sender": sender,
        "text": text,
        "received_at": received_at,
    }


class InfoflowReplyJournalTests(unittest.TestCase):
    def _journal(self, lines):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "infoflow-replies.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return InfoflowReplyJournal(path)

    def test_a_missing_journal_reads_as_no_messages(self):
        self.assertEqual(InfoflowReplyJournal("/nonexistent/replies.jsonl").messages(), [])

    def test_unparsable_and_incomplete_lines_are_skipped(self):
        journal = self._journal(
            [
                "not json",
                json.dumps({"message_id": "msg-2", "sender": "owner", "text": "APPROVE"}),
                json.dumps(_message("APPROVE approval-1")),
                "",
            ]
        )
        self.assertEqual([item["message_id"] for item in journal.messages()], ["msg-1"])

    def test_messages_are_ordered_by_observed_time(self):
        journal = self._journal(
            [
                json.dumps(_message("b", message_id="msg-b", received_at="2026-08-28T12:00:00+00:00")),
                json.dumps(_message("a", message_id="msg-a", received_at="2026-08-28T11:00:00+00:00")),
            ]
        )
        self.assertEqual([item["message_id"] for item in journal.messages()], ["msg-a", "msg-b"])

    def test_a_group_line_keeps_the_group_it_was_typed_in(self):
        journal = self._journal(
            [
                json.dumps(
                    {
                        **_message("APPROVE approval-1"),
                        "chat_type": "group",
                        "group_id": "13403269",
                    }
                ),
                json.dumps(_message("APPROVE approval-1", message_id="msg-2")),
            ]
        )
        messages = journal.messages()
        self.assertEqual(messages[0]["group_id"], "13403269")
        self.assertIsNone(messages[1]["group_id"])


def _group_message(text, *, sender="owner", group_id="13403269", message_id="msg-1"):
    return {
        **_message(text, sender=sender, message_id=message_id),
        "chat_type": "group",
        "group_id": group_id,
    }


class InfoflowReplyConsumerTests(unittest.TestCase):
    def _resolve(self, messages, record=None, *, group_resolver=None):
        consumer = InfoflowReplyConsumer(
            FakeJournal(messages), clock=lambda: _NOW, group_resolver=group_resolver
        )
        return consumer.resolve(record or _record())

    def test_a_bound_authorized_decision_becomes_a_reply(self):
        reply = self._resolve([_message("APPROVE approval-1")])
        self.assertEqual(
            reply,
            {
                "reply_id": "infoflow-msg-1",
                "decision": "APPROVE",
                "responder": "owner@example.test",
                "received_at": "2026-08-28T11:00:00+00:00",
            },
        )

    def test_the_request_id_also_binds_a_decision(self):
        reply = self._resolve([_message("REJECT infoflow-req-1")])
        self.assertEqual(reply["decision"], "REJECT")

    def test_a_decision_without_its_approval_id_is_ignored(self):
        self.assertIsNone(self._resolve([_message("APPROVE")]))

    def test_an_unauthorized_sender_is_ignored(self):
        self.assertIsNone(self._resolve([_message("APPROVE approval-1", sender="stranger")]))

    def test_an_ambiguous_body_is_ignored(self):
        self.assertIsNone(self._resolve([_message("APPROVE or REJECT approval-1")]))

    def test_a_message_outside_the_bound_window_is_ignored(self):
        early = _message("APPROVE approval-1", received_at="2026-08-28T09:59:00+00:00")
        late = _message("APPROVE approval-1", received_at="2026-08-28T20:00:01+00:00")
        self.assertIsNone(self._resolve([early]))
        self.assertIsNone(self._resolve([late]))

    def test_the_earliest_valid_decision_wins(self):
        messages = [
            _message("REJECT approval-1", message_id="msg-2", received_at="2026-08-28T12:00:00+00:00"),
            _message("APPROVE approval-1", message_id="msg-1", received_at="2026-08-28T11:00:00+00:00"),
        ]
        self.assertEqual(self._resolve(messages)["decision"], "APPROVE")

    def test_a_record_without_infoflow_members_resolves_to_nothing(self):
        record = _record(member_policy={"comate": ["owner@example.test"], "infoflow": []})
        self.assertIsNone(self._resolve([_message("APPROVE approval-1")], record))

    def test_a_reply_in_the_runs_group_is_a_decision(self):
        reply = self._resolve(
            [_group_message("APPROVE approval-1")],
            group_resolver=lambda run_id: "13403269" if run_id == "run-1" else None,
        )
        self.assertEqual(reply["decision"], "APPROVE")
        self.assertEqual(reply["responder"], "owner@example.test")

    def test_a_reply_in_another_group_is_ignored(self):
        self.assertIsNone(
            self._resolve(
                [_group_message("APPROVE approval-1", group_id="999")],
                group_resolver=lambda _: "13403269",
            )
        )

    def test_a_group_reply_is_ignored_when_the_runs_group_is_unknown(self):
        self.assertIsNone(self._resolve([_group_message("APPROVE approval-1")]))
        self.assertIsNone(
            self._resolve([_group_message("APPROVE approval-1")], group_resolver=lambda _: None)
        )

    def test_a_private_reply_still_lands_when_the_run_has_a_group(self):
        reply = self._resolve(
            [_message("APPROVE approval-1")], group_resolver=lambda _: "13403269"
        )
        self.assertEqual(reply["decision"], "APPROVE")


class TransportReplyIntegrationTests(unittest.TestCase):
    def _transport(self, messages, *, now=_NOW):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = StateStore(Path(directory.name) / "state.sqlite")
        consumer = InfoflowReplyConsumer(FakeJournal(messages), clock=lambda: now)
        notify = FakeNotifyClient()
        return state, notify, InfoflowApprovalTransport(
            state, notify, clock=lambda: now, reply_consumer=consumer
        )

    def test_a_journalled_approval_resolves_the_pending_request(self):
        _, _, transport = self._transport([_message("APPROVE approval-1")])
        request_id = transport.request(_envelope())["request_id"]
        result = transport.wait(request_id, 5)
        self.assertEqual(result["status"], "APPROVE")
        self.assertEqual(result["reply"]["responder"], "owner@example.test")
        self.assertEqual(result["updated_at"], "2026-08-28T11:00:00+00:00")

    def test_the_decision_survives_a_journal_that_no_longer_has_it(self):
        state, notify, transport = self._transport([_message("APPROVE approval-1")])
        request_id = transport.request(_envelope())["request_id"]
        transport.wait(request_id, 5)
        transport.reply_consumer = InfoflowReplyConsumer(FakeJournal([]), clock=lambda: _NOW)
        self.assertEqual(transport.wait(request_id, 5)["status"], "APPROVE")
        self.assertEqual(len(notify.sends), 1)

    def test_a_reply_is_never_applied_without_a_consumer(self):
        _, _, transport = self._transport([_message("APPROVE approval-1")])
        transport.reply_consumer = None
        request_id = transport.request(_envelope())["request_id"]
        self.assertEqual(transport.wait(request_id, 5)["status"], "PENDING")

    def test_an_expired_request_times_out_even_with_a_late_message(self):
        late = _message("APPROVE approval-1", received_at="2026-08-28T21:00:00+00:00")
        state, _, transport = self._transport([late])
        request_id = transport.request(_envelope())["request_id"]
        transport.clock = lambda: datetime.fromisoformat(_DEADLINE) + timedelta(minutes=1)
        transport.reply_consumer.clock = transport.clock
        result = transport.wait(request_id, 5)
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertEqual(
            [item["handoff_id"] for item in state.incomplete_handoffs("run-1")],
            ["approval-timeout-approval-1"],
        )


if __name__ == "__main__":
    unittest.main()
