import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_delivery import ComateApprovalClient, InfoflowApprovalTransport
from approval_watch import ApprovalWatcher, auto_resume_from_hook
from clients.infoflow_approval_client import InfoflowApprovalClient
from clients.infoflow_reply_client import InfoflowReplyConsumer
from orchestrator import Orchestrator


_POLICY = {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]}


class FakeJournal:
    def __init__(self):
        self.entries = []

    def messages(self):
        return sorted(self.entries, key=lambda item: (item["received_at"], item["message_id"]))

    def add(self, text, *, sender="owner", message_id="msg-1", received_at=None):
        self.entries.append(
            {
                "message_id": message_id,
                "chat_type": "single",
                "sender": sender,
                "text": text,
                "received_at": received_at,
            }
        )


class FakeNotifyClient:
    def __init__(self):
        self.sends = []
        self.group_sends = []

    def send_markdown(self, recipients, content):
        self.sends.append((recipients, content))
        return {"message_key": f"ack-{len(self.sends)}", "recipients": recipients}

    def send_group_markdown(self, group_id, content, at_users):
        self.group_sends.append((group_id, content, at_users))
        return {"message_id": f"group-{len(self.group_sends)}", "group_id": group_id}


class ApprovalWatcherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = datetime.now(timezone.utc)
        self.deadline = (self.now + timedelta(hours=9)).isoformat()
        self.orchestrator = Orchestrator(Path(directory.name))
        self.orchestrator.status = lambda run_id: {"state": "INTAKE"}
        self.orchestrator.state.transition("run-1", "INTAKE", {"project": "bgw"})
        self.journal = FakeJournal()
        self.notify = FakeNotifyClient()
        self.delivery = FakeNotifyClient()
        self.watcher = ApprovalWatcher(
            self.orchestrator,
            self._client,
            self.notify,
            sleeper=lambda _seconds: None,
            clock=lambda: self.now,
        )

    def _client(self):
        return InfoflowApprovalClient(
            InfoflowApprovalTransport(
                self.orchestrator.state,
                self.delivery,
                clock=lambda: self.now,
                reply_consumer=InfoflowReplyConsumer(self.journal, clock=lambda: self.now),
            )
        )

    def _request(self, input_hash="hash-a"):
        requested = self.orchestrator.request_infoflow_approval(
            "run-1",
            "G0",
            input_hash,
            member_policy=_POLICY,
            deadline_at=self.deadline,
            comate_client=ComateApprovalClient(),
            infoflow_client=self._client(),
        )
        return requested["approval_id"]

    def test_an_open_approval_without_a_reply_stays_silent(self):
        self._request()
        self.assertEqual(self.watcher.tick(), [])
        self.assertEqual(self.notify.sends, [])

    def test_the_acknowledgement_goes_to_the_group_when_the_run_has_one(self):
        intent = self.orchestrator.state.intent(
            "run-1",
            "infoflow.group.create",
            "infoflow.group.create:run-1",
            {"group_name": "BGW-1956-url"},
        )
        self.orchestrator.state.receipt(
            intent["intent_id"], {"run_id": "run-1", "group_id": "13403269"}, []
        )
        approval_id = self._request()
        self.journal.add(
            f"APPROVE {approval_id}", received_at=(self.now + timedelta(minutes=1)).isoformat()
        )

        self.watcher.tick()

        group_id, content, at_users = self.notify.group_sends[0]
        self.assertEqual(group_id, "13403269")
        self.assertEqual(at_users, ["owner"])
        self.assertIn(approval_id[:12], content)
        # The position notice stays a private message even when a group exists.
        self.assertEqual(self.notify.sends, [])

    def test_a_state_change_is_pushed_to_infoflow_once(self):
        self._request()
        self.watcher.tick()  # first sight of the run is recorded silently
        self.notify.sends.clear()
        self.orchestrator.approvals.receive(
            self._open_approval(), "APPROVE", "hash-a", "infoflow", "owner@example.test",
            run_id="run-1", state_store=self.orchestrator.state,
        )
        self.orchestrator.state.transition("run-1", "GRILL", {"project": "bgw"})

        first = [item["reason_code"] for item in self.watcher.tick()]
        second = self.watcher.tick()

        self.assertIn("PROGRESS_NOTICE_SENT", first)
        self.assertEqual(len(self.notify.sends), 1)
        recipients, content = self.notify.sends[0]
        self.assertEqual(recipients, ["owner@example.test"])
        self.assertIn("当前阶段 GRILL", content)
        self.assertIn("下一步", content)
        # The same event must never be reported twice.
        self.assertNotIn("PROGRESS_NOTICE_SENT", [item["reason_code"] for item in second])

    def test_an_open_gate_suppresses_the_position_notice(self):
        self.watcher.tick()  # record first sight before any gate exists
        self.orchestrator.state.transition("run-1", "GRILL", {"project": "bgw"})
        self._request()

        outcomes = [item["reason_code"] for item in self.watcher.tick()]

        self.assertNotIn("PROGRESS_NOTICE_SENT", outcomes)
        self.assertEqual(self.notify.sends, [])

    def _open_approval(self) -> str:
        return self.orchestrator.approvals.pending()[0]["approval_id"]

    def test_a_journalled_decision_is_landed_and_acknowledged(self):
        approval_id = self._request()
        self.journal.add(
            f"APPROVE {approval_id}", received_at=(self.now + timedelta(minutes=1)).isoformat()
        )
        settled = self.watcher.tick()
        self.assertEqual(settled[0]["reason_code"], "OK")
        self.assertEqual(settled[0]["effective_decision"], "APPROVE")
        self.assertEqual(
            self.orchestrator.approvals.get(approval_id)["effective_decision"], "APPROVE"
        )
        recipients, content = self.notify.sends[0]
        self.assertEqual(recipients, ["owner@example.test"])
        self.assertIn("已同意", content)
        self.assertIn("owner@example.test", content)

    def test_the_acknowledgement_is_sent_once(self):
        approval_id = self._request()
        self.journal.add(
            f"APPROVE {approval_id}", received_at=(self.now + timedelta(minutes=1)).isoformat()
        )
        self.watcher.tick()
        self.watcher.tick()
        self.assertEqual(len([1 for _, body in self.notify.sends if "已同意" in body]), 1)

    def _gate_per_state(self, gates: dict[str, str]) -> None:
        """Answer `next` the way a ready project would, so the brief has a gate.

        The fixture has no registered project, so the real `next` only ever reports
        PROJECT_NOT_READY, which says nothing about whose turn it is.
        """
        self.orchestrator.next = lambda run_id: {
            "ok": True,
            "run_id": run_id,
            "required_human_gate": gates.get(
                self.orchestrator.state.events(run_id)[-1]["state"]
            ),
        }

    def test_an_approved_gate_that_still_needs_继续_is_announced_in_the_group(self):
        intent = self.orchestrator.state.intent(
            "run-1",
            "infoflow.group.create",
            "infoflow.group.create:run-1",
            {"group_name": "BGW-1956-url"},
        )
        self.orchestrator.state.receipt(
            intent["intent_id"], {"run_id": "run-1", "group_id": "13403269"}, []
        )
        approval_id = self._request()
        self._gate_per_state({"INTAKE": "G0"})
        self.journal.add(
            f"APPROVE {approval_id}", received_at=(self.now + timedelta(minutes=1)).isoformat()
        )

        outcomes = [item["reason_code"] for item in self.watcher.tick()]
        repeat = [item["reason_code"] for item in self.watcher.tick()]

        self.assertIn("RESUME_NOTICE_SENT", outcomes)
        notices = [body for _, body, _ in self.notify.group_sends if "等你在 IDE 继续" in body]
        self.assertEqual(len(notices), 1)
        self.assertIn("G0 已通过", notices[0])
        self.assertIn("**当前阶段** INTAKE", notices[0])
        self.assertIn("@owner", notices[0])
        handoffs = self.orchestrator.state.incomplete_handoffs("run-1")
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["payload"]["approval_id"], approval_id)
        self.assertEqual(handoffs[0]["payload"]["input_hash"], "hash-a")
        # The position is announced once; a watcher round must not repeat it.
        self.assertNotIn("RESUME_NOTICE_SENT", repeat)

    def test_an_open_gate_is_not_announced_as_waiting_on_继续(self):
        self._request()
        self._gate_per_state({"INTAKE": "G0"})

        outcomes = [item["reason_code"] for item in self.watcher.tick()]

        self.assertNotIn("RESUME_NOTICE_SENT", outcomes)
        self.assertEqual(self.notify.sends, [])

    def test_a_re_entered_phase_does_not_reuse_the_discarded_approval(self):
        # A phase can be re-entered (a mis-cut artifact), which discards the decision
        # that had approved it. The old APPROVE must not read as "your turn to 继续".
        approval_id = self._request()
        self._gate_per_state({"INTAKE": "G0", "GRILL": "G0"})
        self.journal.add(
            f"APPROVE {approval_id}", received_at=(self.now + timedelta(minutes=1)).isoformat()
        )
        self.watcher.tick()
        self.orchestrator.state.transition("run-1", "GRILL", {"project": "bgw"})

        outcomes = [item["reason_code"] for item in self.watcher.tick()]

        self.assertNotIn("RESUME_NOTICE_SENT", outcomes)

    def test_a_landed_phase_is_not_announced_again_as_waiting_on_继续(self):
        approval_id = self._request()
        self._gate_per_state({"INTAKE": "G0", "GRILL": "G1"})
        self.journal.add(
            f"APPROVE {approval_id}", received_at=(self.now + timedelta(minutes=1)).isoformat()
        )
        self.watcher.tick()
        # The phase lands, so the next gate is a different one and nobody owes 继续
        # for a decision that has already been consumed.
        self.orchestrator.state.transition("run-1", "GRILL", {"project": "bgw"})

        outcomes = [item["reason_code"] for item in self.watcher.tick()]

        self.assertNotIn("RESUME_NOTICE_SENT", outcomes)

    def test_a_failed_acknowledgement_does_not_lose_the_landed_decision(self):
        approval_id = self._request()
        self.journal.add(
            f"APPROVE {approval_id}", received_at=(self.now + timedelta(minutes=1)).isoformat()
        )

        def refuse(*_args, **_kwargs):
            raise ValueError("MESSAGE_REJECTED:GATEWAY")

        self.notify.send_markdown = refuse

        settled = self.watcher.tick()

        self.assertEqual(settled[0]["reason_code"], "OK")
        self.assertEqual(
            self.orchestrator.approvals.get(approval_id)["effective_decision"], "APPROVE"
        )
        self.assertEqual(settled[0]["acknowledgement_reason_code"], "APPROVAL_ACK_FAILED")
        pending = self.orchestrator.state.pending_intents("run-1")
        self.assertEqual([item["operation"] for item in pending], ["approval.ack"])

    def test_a_rejection_says_the_run_will_not_continue(self):
        approval_id = self._request()
        self.journal.add(
            f"REJECT {approval_id}", received_at=(self.now + timedelta(minutes=1)).isoformat()
        )
        self.watcher.tick()
        _, content = self.notify.sends[0]
        self.assertIn("已驳回", content)
        self.assertIn("不会继续", content)

    def test_an_expired_approval_is_reported_as_a_timeout(self):
        # The ledger judges the deadline on real time, so the bound deadline has to
        # actually pass rather than only move the watcher's clock.
        self.deadline = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        approval_id = self._request()
        time.sleep(1.2)
        self.now = datetime.fromisoformat(self.deadline) + timedelta(minutes=1)
        settled = self.watcher.tick()
        self.assertEqual(settled[0]["reason_code"], "APPROVAL_TIMEOUT")
        _, content = self.notify.sends[0]
        self.assertIn("超时", content)
        self.assertEqual(self.orchestrator.approvals.get(approval_id)["status"], "TIMEOUT")

    def test_run_reports_each_outcome_while_it_runs(self):
        reported = []
        self.watcher.reporter = reported.append
        approval_id = self._request()
        self.journal.add(
            f"APPROVE {approval_id}", received_at=(self.now + timedelta(minutes=1)).isoformat()
        )
        self.watcher.run(0, iterations=1)
        self.assertEqual([item["approval_id"] for item in reported], [approval_id])

    def test_an_undecided_gate_is_reminded_as_the_deadline_nears(self):
        approval_id = self._request()
        self.assertEqual(self.watcher.tick(), [])
        self.now = datetime.fromisoformat(self.deadline) - timedelta(hours=5)
        settled = self.watcher.tick()
        self.assertEqual(settled[0]["reason_code"], "APPROVAL_REMINDED")
        self.assertEqual(settled[0]["reminder"], 1)
        _, content = self.notify.sends[0]
        self.assertIn("待回复", content)
        # The reminder repeats the exact card the approvers already got.
        self.assertIn(f"APPROVE {approval_id}", content)
        self.assertIn("需求", content)

    def test_each_reminder_window_notifies_once(self):
        self._request()
        self.now = datetime.fromisoformat(self.deadline) - timedelta(hours=5)
        self.watcher.tick()
        self.watcher.tick()
        self.assertEqual(len(self.notify.sends), 1)
        self.now = datetime.fromisoformat(self.deadline) - timedelta(hours=1)
        self.watcher.tick()
        self.assertEqual(len(self.notify.sends), 2)

    def test_a_failed_reminder_is_not_blindly_resent(self):
        approval_id = self._request()
        self.now = datetime.fromisoformat(self.deadline) - timedelta(hours=5)
        calls = []

        def refuse(*args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError("delivery outcome unknown")

        self.notify.send_markdown = refuse

        first = self.watcher.tick()
        second = self.watcher.tick()

        self.assertEqual(first[0]["reason_code"], "APPROVAL_REMINDER_FAILED")
        self.assertEqual(second[0]["reason_code"], "APPROVAL_REMINDER_QUERY_REQUIRED")
        self.assertEqual(len(calls), 1)
        pending = self.orchestrator.state.pending_intents("run-1")
        self.assertEqual([item["operation"] for item in pending], ["approval.nudge"])

    def test_a_claimed_reissue_is_not_executed_by_a_second_watcher(self):
        approval_id = self._request()
        approval = self.orchestrator.approvals.get(approval_id)
        key = f"approval.reissue:{approval_id}"
        payload = {
            "approval_id": approval_id,
            "action": approval["action"],
            "input_hash": approval["input_hash"],
        }
        self.orchestrator.state.claim_intent("run-1", "approval.reissue", key, payload)
        calls = []
        original = self.orchestrator.reissue_infoflow_approval
        self.orchestrator.reissue_infoflow_approval = lambda *_args, **_kwargs: calls.append(1)
        self.addCleanup(setattr, self.orchestrator, "reissue_infoflow_approval", original)

        result = self.watcher._reissue(approval, self._client())

        self.assertEqual(result["reason_code"], "APPROVAL_REISSUE_QUERY_REQUIRED")
        self.assertEqual(calls, [])

    def test_a_timed_out_gate_is_reissued_once_against_the_same_input_hash(self):
        self.deadline = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        approval_id = self._request()
        time.sleep(1.2)
        self.now = datetime.fromisoformat(self.deadline) + timedelta(minutes=1)
        settled = self.watcher.tick()
        self.assertEqual(settled[0]["reason_code"], "APPROVAL_TIMEOUT")
        retry = [
            approval
            for approval in self.orchestrator.approvals.for_run("run-1")
            if approval["action"] == "G0#retry-1"
        ]
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0]["input_hash"], "hash-a")
        self.assertEqual(settled[0]["reissued_approval_id"], retry[0]["approval_id"])
        self.assertNotEqual(retry[0]["approval_id"], approval_id)
        # A second round must not open a third attempt.
        self.watcher.tick()
        self.assertEqual(
            len([a for a in self.orchestrator.approvals.for_run("run-1") if "#retry-" in a["action"]]),
            1,
        )

    def test_reissue_is_refused_while_the_gate_is_still_open(self):
        self._request()
        result = self.orchestrator.reissue_infoflow_approval(
            "run-1",
            "G0",
            "hash-a",
            member_policy=_POLICY,
            infoflow_client=self._client(),
        )
        self.assertEqual(result["reason_code"], "APPROVAL_NOT_TIMED_OUT")

    def test_a_dead_gateway_is_revived_before_replies_are_read(self):
        class Reviving(FakeNotifyClient):
            def __init__(self):
                super().__init__()
                self.checks = 0

            def ensure_ready(self):
                self.checks += 1
                return {"ok": True}

        self.notify = Reviving()
        self.watcher.notify_client = self.notify
        self._request()
        self.watcher.tick()
        self.assertEqual(self.notify.checks, 1)

    def test_an_unreachable_gateway_is_reported_not_swallowed(self):
        class Broken(FakeNotifyClient):
            def ensure_ready(self):
                raise ValueError("INFOFLOW_GATEWAY_UNAVAILABLE")

        self.watcher.notify_client = Broken()
        outcomes = self.watcher.tick()
        self.assertEqual(outcomes[0]["reason_code"], "INFOFLOW_GATEWAY_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()


class IdeTurnNoticeTests(unittest.TestCase):
    """The gateless stops are the ones nobody was told about."""

    class _Brief:
        def __init__(self, brief):
            self.brief = brief

        def build(self, _orchestrator, _run_id):
            return self.brief

    class _Notify:
        def __init__(self):
            self.sent = []

        def send_markdown(self, recipients, content):
            self.sent.append((recipients, content))
            return {"message_key": "m-1", "recipients": recipients}

    class _State:
        def __init__(self):
            self.saved = {}
            self.rows = [{"event_id": "e-9", "state": "REVIEW"}]

        def events(self, _run_id):
            return self.rows

        def idempotency_result(self, key):
            return self.saved.get(key)

        def save_idempotency_result(self, key, value):
            self.saved[key] = value

        def result_by_idempotency_key(self, _key):
            # no collaboration group for this run, so delivery takes the single-chat path
            return None

    class _Orchestrator:
        def __init__(self, state):
            self.state = state

        def _runtime_profile(self, _run_id):
            return {
                "ok": True,
                "profile": {
                    "approval_channels": {"role_members": {"development": ["a@example.test"]}}
                },
            }

    def _patched(self, brief):
        import approval_watch
        import run_brief

        original = run_brief.build
        run_brief.build = self._Brief(brief).build
        self.addCleanup(setattr, run_brief, "build", original)
        return approval_watch

    def test_gateless_stop_sends_one_notice_and_does_not_repeat(self):
        watch = self._patched({
            "run_id": "run-notice-1", "state": "REVIEW", "waiting_on": [], "gate": None,
            "next": {"owner": "Comate", "text": "审查改动"},
        })
        state = self._State()
        orchestrator = self._Orchestrator(state)
        notify = self._Notify()

        first = watch.notify_ide_turn(orchestrator, "run-notice-1", notify)
        second = watch.notify_ide_turn(orchestrator, "run-notice-1", notify)

        self.assertEqual(first["reason_code"], "OK")
        self.assertEqual(second["reason_code"], "ALREADY_NOTIFIED")
        self.assertEqual(len(notify.sent), 1)
        self.assertIn("等你在 IDE 继续", notify.sent[0][1])

    def test_a_stop_notifies_every_live_run_without_being_given_one(self):
        """A stop hook knows nothing about run ids, so the ledger has to supply them."""
        watch = self._patched({
            "run_id": "run-notice-4", "state": "IPIPE", "waiting_on": [], "gate": None,
            "next": {"owner": "Comate", "text": "收流水线证据"},
        })
        state = self._State()
        state.latest = [
            {"run_id": "run-notice-4", "state": "IPIPE"},
            {"run_id": "run-done", "state": "RELEASE_SUCCESS"},
            {"run_id": "run-stopped", "state": "STOPPED"},
        ]
        state.latest_states = lambda: state.latest
        orchestrator = self._Orchestrator(state)
        notify = self._Notify()

        first = watch.notify_every_parked_run(orchestrator, notify)
        second = watch.notify_every_parked_run(orchestrator, notify)

        self.assertEqual([item["run_id"] for item in first["notices"]], ["run-notice-4"])
        self.assertEqual(first["notices"][0]["reason_code"], "OK")
        self.assertEqual(second["notices"][0]["reason_code"], "ALREADY_NOTIFIED")
        self.assertEqual(len(notify.sent), 1)

    def test_a_stop_survives_a_run_whose_notice_raises(self):
        watch = self._patched({
            "run_id": "run-notice-5", "state": "REVIEW", "waiting_on": [], "gate": None,
            "next": {"owner": "Comate", "text": "审查改动"},
        })
        state = self._State()
        state.latest_states = lambda: [{"run_id": "run-notice-5", "state": "REVIEW"}]
        orchestrator = self._Orchestrator(state)

        class _Exploding:
            def send_markdown(self, *_args, **_kwargs):
                raise RuntimeError("gateway down")

            def send_group_markdown(self, *_args, **_kwargs):
                raise RuntimeError("gateway down")

        result = watch.notify_every_parked_run(orchestrator, _Exploding())

        self.assertTrue(result["ok"])
        self.assertEqual(result["notices"][0]["reason_code"], "IDE_TURN_NOTICE_FAILED")

    def test_open_gate_is_left_to_the_approval_card(self):
        watch = self._patched({
            "run_id": "run-notice-2", "state": "PLAN",
            "waiting_on": [{"approval_id": "ap-1", "action": "G4"}], "gate": "G4",
            "next": {"owner": "你", "text": "在如流回复 APPROVE ap-1"},
        })
        notify = self._Notify()

        result = watch.notify_ide_turn(self._Orchestrator(self._State()), "run-notice-2", notify)

        self.assertEqual(result["reason_code"], "APPROVAL_CARD_COVERS_IT")
        self.assertEqual(notify.sent, [])

    def test_declared_but_unrequested_gate_still_gets_a_notice(self):
        """The stop that stayed silent: a gate is named, but no card is out yet."""
        watch = self._patched({
            "run_id": "run-notice-3", "state": "IMPLEMENT", "waiting_on": [], "gate": "G5",
            "next": {"owner": "Comate", "text": "生成改动集，完成后开 G5 门等你批"},
        })
        notify = self._Notify()

        result = watch.notify_ide_turn(self._Orchestrator(self._State()), "run-notice-3", notify)

        self.assertEqual(result["reason_code"], "OK")
        self.assertEqual(len(notify.sent), 1)
        self.assertIn("等你在 IDE 继续", notify.sent[0][1])
        self.assertIn("之后要批** G5", notify.sent[0][1])


class AutoResumeHookTests(unittest.TestCase):
    class _State:
        def __init__(self, handoff):
            self.handoff = handoff
            self.completed = []

        def latest_states(self):
            return [{"run_id": "run-1", "state": "PLAN"}]

        def incomplete_handoffs(self, run_id):
            return [self.handoff] if run_id == "run-1" and self.handoff else []

        def complete_handoff(self, handoff_id):
            self.completed.append(handoff_id)
            self.handoff = None

    class _Approvals:
        def __init__(self, approval):
            self.approval = approval

        def get(self, approval_id):
            return self.approval if approval_id == self.approval["approval_id"] else None

    class _Orchestrator:
        def __init__(self, approval):
            self.state = AutoResumeHookTests._State({
                "handoff_id": "approval-resume-ap-1",
                "payload": {
                    "kind": "APPROVAL_RESUME",
                    "run_id": "run-1",
                    "event_id": "event-1",
                    "approval_id": "ap-1",
                    "input_hash": "hash-a",
                    "action": "G4",
                },
                "status": "PENDING",
            })
            self.approvals = AutoResumeHookTests._Approvals(approval)

    def _orchestrator(self, **changes):
        approval = {
            "approval_id": "ap-1",
            "run_id": "run-1",
            "input_hash": "hash-a",
            "effective_decision": "APPROVE",
        }
        approval.update(changes)
        return self._Orchestrator(approval)

    def test_stop_hook_returns_hash_bound_continuation_and_completes_once(self):
        orchestrator = self._orchestrator()

        result = auto_resume_from_hook(orchestrator, {
            "hook_event_name": "Stop",
            "status": "completed",
        })

        self.assertEqual(result["reason_code"], "AUTO_RESUME")
        self.assertEqual(result["decision"], "block")
        self.assertTrue(result["continue"])
        self.assertIn("run-1", result["additionalContext"])
        self.assertEqual(orchestrator.state.completed, ["approval-resume-ap-1"])

    def test_stop_hook_ignores_hash_mismatched_approval(self):
        orchestrator = self._orchestrator(input_hash="different")

        result = auto_resume_from_hook(orchestrator, {
            "hook_event_name": "Stop",
            "status": "completed",
        })

        self.assertEqual(result["reason_code"], "RESUME_HANDOFF_STALE")
        self.assertEqual(orchestrator.state.completed, [])

    def test_cancelled_stop_never_consumes_resume_handoff(self):
        orchestrator = self._orchestrator()

        result = auto_resume_from_hook(orchestrator, {
            "hook_event_name": "Stop",
            "status": "cancelled",
        })

        self.assertEqual(result["reason_code"], "SESSION_CANCELLED")
        self.assertEqual(orchestrator.state.completed, [])
