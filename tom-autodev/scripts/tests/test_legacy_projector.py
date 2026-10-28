"""MVP §7: the read-only projector answers for every run without the live workflow."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import legacy_projector as lp
from orchestrator import Orchestrator


class LegacyProjectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.orchestrator = Orchestrator(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_an_unknown_run_is_reported_not_raised(self):
        self.assertEqual(lp.status(self.orchestrator, "missing")["state"], "RUN_NOT_FOUND")

    def test_projection_ignores_the_live_workflow(self):
        state = self.orchestrator.state
        state.transition("run-1", "INTAKE", {"requirement_id": "CARD-1", "project": "p",
                                               "evidence_contract": "v2"})
        row = self.orchestrator.approvals.request("G0", "a" * 64, ["comate", "infoflow"], run_id="run-1",
                                                 member_policy={"comate": ["a@x"], "infoflow": ["a@x"]})
        # Whatever the live spec says, the projector does not read it.
        with mock.patch("workflow_spec.spec_drift_for_state", side_effect=AssertionError):
            projected = lp.context(self.orchestrator, "run-1")
        self.assertEqual((projected["state"], projected["open_gates"]), ("INTAKE", ["G0"]))
        self.assertEqual(projected["evidence_contract"], "v2")
        self.assertEqual(projected["decisions"], [])
        self.assertTrue(row["approval_id"])


if __name__ == "__main__":
    unittest.main()
