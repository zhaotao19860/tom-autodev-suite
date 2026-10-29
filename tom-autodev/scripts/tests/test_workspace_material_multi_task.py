"""A run with two tasks: the WORKSPACE G4 material reads only the current task's rows."""
import inspect
import unittest

import approval_summary


class WorkspaceMaterialTaskScopeTests(unittest.TestCase):
    def test_the_ownership_query_is_scoped_to_the_task(self):
        # BGW-2019: T1's ACTIVE rows came first and T2's G4 card had no material.
        source = inspect.getsource(approval_summary._workspace_context)
        self.assertIn("run_id = ? AND task_id = ? AND status = 'ACTIVE'", source)


if __name__ == "__main__":
    unittest.main()
