import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

from ipipe_watch import IpipeWatcher

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


class _State:
    """Just enough of StateStore: one run in IPIPE with one trigger receipt."""

    def __init__(self, state="IPIPE", build_id="b-1"):
        self.saved = {}
        self.state = state
        self.build_id = build_id

    def latest_states(self):
        return [{"run_id": "run-1", "state": self.state, "event_id": "e-1"}]

    def external_results(self, _run_id):
        if self.build_id is None:
            return []
        return [{
            "intent": {"operation": "ipipe.trigger"},
            "receipt": {"response": {"ok": True, "build_id": self.build_id}},
        }]

    def idempotency_result(self, key):
        return self.saved.get(key)

    def save_idempotency_result(self, key, value):
        self.saved[key] = value

    def result_by_idempotency_key(self, _key):
        # no collaboration group, so delivery takes the single-chat path
        return None


class _Orchestrator:
    def __init__(self, state):
        self.state = state

    def _runtime_profile(self, _run_id):
        return {
            "ok": True,
            "profile": {"approval_channels": {"role_members": {"test": ["t@example.test"]}}},
        }


class _Notify:
    def __init__(self):
        self.sent = []

    def send_markdown(self, recipients, content):
        self.sent.append((recipients, content))
        return {"message_key": f"m-{len(self.sent)}", "recipients": recipients}


class _Runtime:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def monitor(self, build_id, deadline):
        self.calls.append((build_id, deadline))
        return self.result


class IpipeWatcherTests(unittest.TestCase):
    def _watcher(self, result, *, state=None, now=_NOW, notify=None):
        state = state or _State()
        notify = notify or _Notify()
        watcher = IpipeWatcher(
            _Orchestrator(state),
            lambda _run_id: _Runtime(result),
            notify,
            clock=lambda: now,
            sleeper=lambda _seconds: None,
        )
        return watcher, state, notify

    def test_success_notifies_once_and_hands_back_to_the_ide(self):
        result = {"ok": True, "status": "SUCCESS", "build_id": "b-1",
                  "stages": [{"name": "编译", "status": "SUCC"}]}
        watcher, _state, notify = self._watcher(result)

        first = watcher.tick()
        second = watcher.tick()

        self.assertEqual(first[0]["reason_code"], "OK")
        self.assertEqual(second[0]["reason_code"], "ALREADY_NOTIFIED")
        self.assertEqual(len(notify.sent), 1)
        self.assertIn("iPipe 通过", notify.sent[0][1])
        self.assertIn("继续", notify.sent[0][1])

    def test_failure_carries_its_classification_and_repair_direction(self):
        result = {"ok": False, "status": "FAILURE", "build_id": "b-1",
                  "classification": "ENVIRONMENT_FAILURE", "failure_signature": "sig-1",
                  "stages": [{"name": "回归", "status": "FAIL"}],
                  "log_excerpt": "runner lost"}
        watcher, _state, notify = self._watcher(result)

        outcome = watcher.tick()

        self.assertEqual(outcome[0]["reason_code"], "OK")
        body = notify.sent[0][1]
        self.assertIn("需要人工介入", body)
        self.assertIn("ENVIRONMENT_FAILURE", body)
        self.assertIn("G8 重跑", body)
        self.assertIn("sig-1", body)
        self.assertIn("runner lost", body)

    def test_a_second_failure_signature_is_reported_again(self):
        state = _State()
        notify = _Notify()
        first = {"ok": False, "status": "FAILURE", "build_id": "b-1",
                 "classification": "CODE_FAILURE", "failure_signature": "sig-1", "stages": []}
        watcher, _s, _n = self._watcher(first, state=state, notify=notify)
        watcher.tick()

        second = dict(first, failure_signature="sig-2")
        watcher.runtime_factory = lambda _run_id: _Runtime(second)
        watcher.tick()

        self.assertEqual(len(notify.sent), 2)

    def test_manual_stage_notifies_once_then_nudges_every_half_hour(self):
        result = {"ok": False, "status": "MANUAL_WAIT", "build_id": "b-1",
                  "stage_build_id": "s-9", "stages": [{"name": "发布", "status": "PENDING_FOR_USER"}]}
        state = _State()
        notify = _Notify()
        watcher, _s, _n = self._watcher(result, state=state, notify=notify)

        opened = watcher.tick()
        watcher.clock = lambda: _NOW + timedelta(minutes=20)
        too_early = watcher.tick()
        watcher.clock = lambda: _NOW + timedelta(minutes=35)
        nudged = watcher.tick()
        repeat = watcher.tick()
        watcher.clock = lambda: _NOW + timedelta(minutes=65)
        again = watcher.tick()

        self.assertEqual(opened[0]["reason_code"], "OK")
        self.assertEqual(too_early[0]["reason_code"], "MANUAL_WAIT_TOO_EARLY")
        self.assertEqual(nudged[0]["reason_code"], "NUDGED")
        self.assertEqual(repeat[0]["reason_code"], "NUDGE_ALREADY_SENT")
        self.assertEqual(again[0]["reason_code"], "NUDGED")
        self.assertEqual([body.splitlines()[0] for _r, body in notify.sent], [
            "## iPipe 停在人工阶段，等人操作", "## iPipe 人工阶段还在等", "## iPipe 人工阶段还在等",
        ])
        self.assertIn("已等** 35 分钟", notify.sent[1][1])

    def test_transient_and_timeout_stay_quiet(self):
        for status in ("TIMEOUT", "TRANSIENT", "INVALID"):
            watcher, _state, notify = self._watcher({"ok": False, "status": status, "build_id": "b-1"})
            outcome = watcher.tick()
            self.assertEqual(outcome[0]["reason_code"], f"NO_NOTICE_{status}")
            self.assertEqual(notify.sent, [])

    def test_runs_outside_ipipe_and_builds_not_yet_triggered_are_skipped(self):
        watcher, _state, notify = self._watcher({"status": "SUCCESS"}, state=_State(state="REVIEW"))
        self.assertEqual(watcher.tick(), [])
        self.assertEqual(notify.sent, [])

        watcher, _state, notify = self._watcher({"status": "SUCCESS"}, state=_State(build_id=None))
        self.assertEqual(watcher.tick()[0]["reason_code"], "NO_BUILD_YET")
        self.assertEqual(notify.sent, [])

    def test_a_monitor_that_raises_does_not_stop_the_watch(self):
        class _Boom:
            def monitor(self, _build_id, _deadline):
                raise RuntimeError("gateway down")

        watcher, _state, notify = self._watcher({"status": "SUCCESS"})
        watcher.runtime_factory = lambda _run_id: _Boom()

        outcome = watcher.tick()

        self.assertEqual(outcome[0]["reason_code"], "MONITOR_CALL_FAILED")
        self.assertEqual(notify.sent, [])


if __name__ == "__main__":
    unittest.main()
