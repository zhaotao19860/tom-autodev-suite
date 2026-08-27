import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repair_policy import next_action


class RepairPolicyTests(unittest.TestCase):
    def test_environment_failure_never_returns_repair(self):
        result = next_action([{"failure_class": "ENV_UNSATISFIED"}])

        self.assertEqual(result, {"action": "STOP", "reason_code": "ENV_UNSATISFIED"})

    def test_missing_root_cause_stops_with_diagnosis_incomplete(self):
        result = next_action(
            [
                {
                    "failure_class": "CODE_FAILURE",
                    "diagnosis_confirmed": False,
                }
            ]
        )

        self.assertEqual(result["action"], "STOP")
        self.assertEqual(result["reason_code"], "DIAGNOSIS_INCOMPLETE")

    def test_three_failed_repairs_force_architecture_review(self):
        history = [
            {
                "failure_class": "CODE_FAILURE",
                "diagnosis_confirmed": True,
                "failure_signature": f"sig-{number}",
                "resolved": False,
                "progress": True,
            }
            for number in range(3)
        ]

        result = next_action(history)

        self.assertEqual(result, {"action": "ARCHITECTURE_REVIEW", "reason_code": "THREE_FAILED_FIXES"})

    def test_two_identical_no_progress_rounds_stop(self):
        history = [
            {
                "failure_class": "CODE_FAILURE",
                "diagnosis_confirmed": True,
                "failure_signature": "same",
                "resolved": False,
                "progress": False,
            },
            {
                "failure_class": "CODE_FAILURE",
                "diagnosis_confirmed": True,
                "failure_signature": "same",
                "resolved": False,
                "progress": False,
            },
        ]

        result = next_action(history)

        self.assertEqual(result, {"action": "STOP", "reason_code": "NO_PROGRESS"})

    def test_fifth_repair_stops(self):
        history = [
            {
                "failure_class": "CODE_FAILURE",
                "diagnosis_confirmed": True,
                "failure_signature": f"sig-{number}",
                "resolved": False,
                "progress": True,
            }
            for number in range(5)
        ]

        result = next_action(history)

        self.assertEqual(result, {"action": "STOP", "reason_code": "REPAIR_LIMIT"})

    def test_confirmed_diagnosis_can_propose_one_repair(self):
        result = next_action(
            [
                {
                    "failure_class": "TEST_FAILURE",
                    "diagnosis_confirmed": True,
                    "failure_signature": "new",
                    "resolved": False,
                    "progress": True,
                }
            ]
        )

        self.assertEqual(result, {"action": "REPAIR", "reason_code": "DIAGNOSIS_CONFIRMED"})


if __name__ == "__main__":
    unittest.main()
