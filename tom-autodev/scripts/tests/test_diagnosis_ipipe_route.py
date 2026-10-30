"""route IPIPE: an environment diagnosis returns to the pipeline only after a G8 rerun."""
import unittest

from schema_validator import _validate_diagnosis


BASE = {"evidence_state": "SUFFICIENT", "hypothesis": "h", "repair_direction": "d",
        "repair_plan": ["p"], "repair_diff_hash": None}


class DiagnosisIpipeRouteTests(unittest.TestCase):
    def test_environment_class_may_return_to_the_pipeline(self):
        self.assertEqual(_validate_diagnosis({**BASE, "classification": "ENV_UNSATISFIED", "route": "IPIPE"}), [])

    def test_a_code_failure_cannot_skip_repair_by_returning_to_the_pipeline(self):
        issues = _validate_diagnosis({**BASE, "classification": "CODE_FAILURE", "route": "IPIPE"})
        self.assertEqual([i.path for i in issues], ["classification"])


if __name__ == "__main__":
    unittest.main()
