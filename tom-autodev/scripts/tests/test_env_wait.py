"""ENV_WAIT: a stage whose input is unavailable waits on a schedule, never on a word."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

import env_wait
import worker_driver
from orchestrator import Orchestrator
from test_orchestrator import PROFILE, _start, _write_profile

UNPUBLISHED = {"ok": False, "reason_code": "COMPILE_ONLY_TRIGGER_UNSUPPORTED",
               "product_target": {"module": "baidu/sysip/x86bgw", "branch": "dev"}}


class EnvWaitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        _write_profile(root, PROFILE)
        self.orch = Orchestrator(root)
        self.run_id = _start(self.orch)["run_id"]
        self.state = self.orch.state

    def _prepare(self, resolved, now=None):
        calls = []

        def stage_parameters(*args, **kwargs):
            calls.append(1)
            return resolved

        with patch("orchestrator._stage_parameters", side_effect=stage_parameters), \
                patch("env_wait._now", side_effect=lambda value: (now or datetime.now(timezone.utc))):
            result = worker_driver._prepare_manual_stage(self.orch, object(), self.run_id, "stage-1", "m")
        return result, len(calls)

    def test_an_unavailable_input_opens_a_wait_with_its_schedule(self):
        result, checks = self._prepare(UNPUBLISHED)
        self.assertEqual((result["parked"], checks), ("ENV_WAIT", 1))
        wait = result["wait"]
        self.assertEqual(wait["wait_reason"], "COMPILE_ONLY_TRIGGER_UNSUPPORTED")
        self.assertIn("baidu/sysip/x86bgw", wait["next_actor"])
        self.assertTrue(wait["recheck_after"] < wait["max_wait"])

    def test_before_recheck_after_the_platform_is_not_asked_again(self):
        self._prepare(UNPUBLISHED)
        result, checks = self._prepare(UNPUBLISHED)
        self.assertEqual((result["parked"], checks), ("ENV_WAIT", 0))

    def test_a_due_recheck_that_still_fails_reschedules(self):
        self._prepare(UNPUBLISHED)
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        result, checks = self._prepare(UNPUBLISHED, now=later)
        self.assertEqual((result["parked"], checks, result["wait"]["checks"]), ("ENV_WAIT", 1, 2))
        self.assertGreater(result["wait"]["recheck_after"], later.isoformat())

    def test_only_a_fresh_passing_check_ends_the_wait(self):
        self._prepare(UNPUBLISHED)
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        cleared = {"ok": True, "parameters": {}, "declaration": {"names": []}}
        with patch("orchestrator._prepare_ipipe_g8", return_value={"ok": True, "reason_code": "X"}):
            self._prepare(cleared, now=later)
        self.assertIsNone(env_wait.opened(self.state, self.run_id, "stage-1"))
        ended = [r for k, r in self.state.idempotency_results_with_prefix("env.wait:") if ":end:" in k]
        self.assertEqual(ended[0]["outcome"], "CLEARED")

    def test_past_max_wait_the_run_stops(self):
        self._prepare(UNPUBLISHED)
        result, checks = self._prepare(UNPUBLISHED, now=datetime.now(timezone.utc) + timedelta(days=4))
        self.assertEqual((result["reason_code"], checks), ("ENV_WAIT_EXPIRED", 0))
        self.assertEqual(self.orch.status(self.run_id)["state"], "STOPPED")

    def test_a_permission_or_unknown_download_failure_does_not_wait(self):
        mixed = {"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED", "http_statuses": [404],
                 "candidates": [{"http_status": 404}, {"build_id": "b"}]}
        self.assertNotEqual(self._prepare(mixed)[0].get("parked"), "ENV_WAIT")
        for statuses in ([403], [503], []):
            with self.subTest(statuses=statuses):
                result, _ = self._prepare({"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED",
                                           "http_statuses": statuses})
                self.assertNotEqual(result.get("parked"), "ENV_WAIT")

    def test_an_expired_wait_whose_stop_did_not_land_stops_on_the_next_call(self):
        self._prepare(UNPUBLISHED)
        record = env_wait.opened(self.state, self.run_id, "stage-1")
        env_wait.end_wait(self.state, record, "EXPIRED")
        result, checks = self._prepare(UNPUBLISHED)
        self.assertEqual((result["reason_code"], checks), ("ENV_WAIT_EXPIRED", 0))
        self.assertEqual(self.orch.status(self.run_id)["state"], "STOPPED")

    def test_other_failures_do_not_wait(self):
        result, _ = self._prepare({"ok": False, "reason_code": "STAGE_PARAMETER_NAMES_MISMATCH"})
        self.assertNotEqual(result.get("parked"), "ENV_WAIT")
        self.assertEqual(env_wait.open_waits(self.state, self.run_id), [])


if __name__ == "__main__":
    unittest.main()
