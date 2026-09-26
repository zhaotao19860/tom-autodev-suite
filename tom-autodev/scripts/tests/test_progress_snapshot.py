from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import progress_snapshot
from pipeline_plan import canonical_hash


class SnapshotTests(unittest.TestCase):
    def _orchestrator(self, state, *, nodes=None, modules=None, profile=None):
        events = [{
            "run_id": "run-1",
            "state": state,
            "created_at": "2026-09-26T00:00:00+00:00",
            "payload": {},
        }]
        dag = {
            "valid": bool(nodes),
            "envelope": {"content": {"nodes": nodes or []}},
        }
        checkpoints = [
            {
                "run_id": "run-1",
                "build_id": f"build-{module}",
                "checkpoint": {
                    "module": module,
                    "build_id": f"build-{module}",
                    "status": status,
                },
            }
            for module, status in (modules or [])
        ]
        artifacts = SimpleNamespace(latest_phase=lambda run_id, phase, task: dag)
        state_store = SimpleNamespace(
            events=lambda run_id: events,
            ipipe_monitoring=lambda run_id: checkpoints,
        )
        orch = SimpleNamespace(
            state=state_store,
            artifacts=artifacts,
            phase_protocol=lambda: SimpleNamespace(
                _task_reviewed=lambda run_id, task_id: task_id == "done"
            ),
            _runtime_profile=lambda run_id: {
                "ok": True,
                "profile": profile or {},
            },
            next=lambda run_id, *, read_only=False: {
                "ok": True,
                "phase": state,
                "task_id": "active",
            },
        )
        return orch

    def test_terminal_progress_and_task_frontier(self):
        orch = self._orchestrator(
            "RELEASE_SUCCESS",
            nodes=[
                {"task_id": "done", "business_module": "bgw"},
                {"task_id": "active", "business_module": "qa"},
            ],
        )
        snapshot = progress_snapshot.build(orch, "run-1")
        self.assertEqual(snapshot["current_phase"], "RELEASE_SUCCESS")
        self.assertEqual(snapshot["tasks"]["done"], 1)
        self.assertEqual(snapshot["tasks"]["current"], "active")
        self.assertIn("整体进度", progress_snapshot.render(snapshot))

    def test_multirepo_pipeline_and_release_waiting(self):
        profile = {
            "business_repos": [
                {"module": "bgw", "branch": "main"},
                {"module": "agent", "branch": "main"},
            ],
            "test_repo": {"module": "qa", "branch": "main"},
        }
        orch = self._orchestrator(
            "IPIPE",
            modules=[("bgw", "SUCCESS"), ("agent", "MONITORING")],
            profile=profile,
        )
        # The plan is supplied through the frozen event, as production does.
        plan = {
            "version": 1,
            "run_id": "run-1",
            "required_modules": ["bgw", "agent"],
            "modules": {"bgw": {}, "agent": {}},
        }
        plan["plan_hash"] = canonical_hash({
            key: value for key, value in plan.items() if key != "plan_hash"
        })
        orch.state.events = lambda run_id: [{
            "run_id": "run-1",
            "state": "IPIPE",
            "payload": {
                "pipeline_plan": plan,
            },
        }]
        snapshot = progress_snapshot.build(orch, "run-1")
        self.assertEqual(snapshot["pipelines"]["total"], 2)
        self.assertEqual(snapshot["pipelines"]["done"], 1)
        self.assertEqual(snapshot["pipelines"]["status"], "MONITORING")
        self.assertEqual(snapshot["release"]["waiting_for"], ["agent"])


if __name__ == "__main__":
    unittest.main()
