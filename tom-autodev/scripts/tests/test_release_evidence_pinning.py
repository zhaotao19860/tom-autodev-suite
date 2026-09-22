"""A mandatory-environment gate must bind to the pinned profile, not a caller value."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import Orchestrator
from pipeline_plan import canonical_hash


class ReleaseEvidencePinningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.orch = Orchestrator(Path(self.temp.name))
        self.environment_profile = {"os": "linux", "toolchain": "v1"}
        self.orch._runtime_profile = lambda run_id: {
            "ok": True, "profile": {"environment_profile": self.environment_profile}}

    def test_release_gate_env_fingerprint_is_pinned_not_caller_supplied(self):
        # Caller passes two identical-but-stale fingerprints; the expected side must be
        # overridden with the pinned profile's canonical hash, so the stale pair no longer
        # matches and cannot pass a mandatory-environment gate on the generic advance path.
        context = self.orch._ledger_backed_evidence("run-1", "IPIPE", "RELEASE", {
            "environment_fingerprint": "stale-equal",
            "evidence_environment_fingerprint": "stale-equal",
        })
        self.assertEqual(context["environment_fingerprint"], canonical_hash(self.environment_profile))
        self.assertNotEqual(context["environment_fingerprint"], "stale-equal")

    def test_release_gate_fails_closed_when_profile_unavailable(self):
        self.orch._runtime_profile = lambda run_id: {"ok": False, "reason_code": "PROFILE_CONFLICT"}
        context = self.orch._ledger_backed_evidence("run-1", "IPIPE", "RELEASE", {
            "environment_fingerprint": "stale-equal",
            "evidence_environment_fingerprint": "stale-equal",
        })
        self.assertIsNone(context["environment_fingerprint"])

    def test_non_environment_action_is_left_untouched(self):
        # SUBMIT->IPIPE does not require environment, so the override must not fire.
        context = self.orch._ledger_backed_evidence("run-1", "SUBMIT", "IPIPE", {
            "input_hash": "h", "approved_input_hash": "h",
        })
        self.assertNotIn("environment_fingerprint", context)


if __name__ == "__main__":
    unittest.main()
