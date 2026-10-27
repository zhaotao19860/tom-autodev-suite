import unittest

from worker_driver import _next_ready_manual_stage


class BranchReleaseStageOrderTests(unittest.TestCase):
    def test_the_release_stage_waits_while_regression_is_still_running(self):
        # BGW-1995 BranchPipeline 275700446: 发布 shows PENDING_FOR_USER while 自动化回归测试
        # is SKIPPING (running); a G8 for 发布 at that point approved the wrong step.
        stages = [
            {"stage_build_id": "compile", "status": "SUCC"},
            {"stage_build_id": "scan", "status": "SKIPPED"},
            {"stage_build_id": "regression", "status": "SKIPPING"},
            {"stage_build_id": "test-request", "status": "WAITTING"},
            {"stage_build_id": "release", "status": "PENDING_FOR_USER"},
        ]
        self.assertIsNone(_next_ready_manual_stage(stages))

    def test_the_first_waiting_manual_stage_is_the_one_to_drive(self):
        stages = [
            {"stage_build_id": "compile", "status": "SUCC"},
            {"stage_build_id": "regression", "status": "PENDING_FOR_USER"},
            {"stage_build_id": "release", "status": "PENDING_FOR_USER"},
        ]
        self.assertEqual(_next_ready_manual_stage(stages), "regression")


if __name__ == "__main__":
    unittest.main()
