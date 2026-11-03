from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import model_routing  # noqa: E402


class _State:
    def __init__(self, change_class="standard", receipts=None):
        self._events = [{"payload": {"change_class": change_class}}]
        self._receipts = receipts or []

    def events(self, run_id):
        return self._events

    def model_execution_receipts(self, run_id):
        return self._receipts


class _Orch:
    def __init__(self, **kw):
        self.state = _State(**kw)


def action(phase, **extra):
    return {"phase": phase, "child_skill": "tom-" + phase.lower(), "action_id": "a", **extra}


class JobRouteTests(unittest.TestCase):
    def test_design_phases_and_tier_from_change_class(self):
        for phase in ("GRILL", "SPEC", "TASKS", "PLAN", "DIAGNOSE"):
            self.assertEqual(model_routing.job_route(_Orch(), "r", action(phase))["role"], "design")
        self.assertEqual(model_routing.job_route(_Orch(change_class="express"), "r", action("SPEC"))["tier"], "S")
        self.assertEqual(model_routing.job_route(_Orch(change_class="full"), "r", action("SPEC"))["tier"], "L")

    def test_first_implement_then_repair_with_attempts(self):
        first = model_routing.job_route(_Orch(), "r", action("IMPLEMENT", task_id="T1"))
        self.assertEqual((first["role"], first["attempt"]), ("implement", 1))
        receipts = [{"phase": "IMPLEMENT", "task_id": "T1", "model": "gpt-6-luna"}]
        again = model_routing.job_route(_Orch(receipts=receipts), "r", action("IMPLEMENT", task_id="T1"))
        self.assertEqual((again["role"], again["attempt"]), ("repair", 2))
        other = model_routing.job_route(_Orch(receipts=receipts), "r", action("IMPLEMENT", task_id="T2"))
        self.assertEqual(other["role"], "implement")

    def test_reentry_after_diagnosis_is_repair(self):
        route = model_routing.job_route(_Orch(), "r", action("IMPLEMENT", reentry={"kind": "DIAGNOSIS"}))
        self.assertEqual(route["role"], "repair")

    def test_review_avoids_implementer(self):
        receipts = [{"phase": "IMPLEMENT", "task_id": "T1", "model": "gpt-6-luna"}]
        route = model_routing.job_route(_Orch(receipts=receipts), "r", action("REVIEW", task_id="T1"))
        self.assertEqual((route["role"], route["impl_model"]), ("review", "gpt-6-luna"))

    def test_non_skill_action_has_no_route(self):
        self.assertIsNone(model_routing.job_route(_Orch(), "r", {"phase": "SUBMIT", "controller": "submit"}))


class AdviseTests(unittest.TestCase):
    def test_missing_router_is_advisory_only(self):
        with patch("agent_host.find_skill", return_value=None):
            got = model_routing.advise(_Orch(), "r", action("PLAN"))
        self.assertFalse(got["available"])
        self.assertEqual(got["role"], "design")

    def test_calls_router_and_returns_pick(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "router.py"
            script.write_text(
                "import json,sys\nprint(json.dumps({'argv': sys.argv[1:], 'model': 'gpt-6-luna'}))\n",
                encoding="utf-8")
            receipts = [{"phase": "IMPLEMENT", "model": "gpt-6-luna"}]
            with patch("agent_host.find_skill", return_value=Path(tmp)), \
                    patch.dict(os.environ, {"TOM_AUTODEV_AGENT": "codex"}):
                got = model_routing.advise(_Orch(receipts=receipts), "r", action("REVIEW"))
        self.assertTrue(got["available"])
        argv = got["pick"]["argv"]
        self.assertEqual(argv[argv.index("--role") + 1], "review")
        self.assertEqual(argv[argv.index("--impl-model") + 1], "gpt-6-luna")
        self.assertNotIn("subagent", argv[argv.index("--runners") + 1])

    def test_router_failure_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "router.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
            with patch("agent_host.find_skill", return_value=Path(tmp)):
                got = model_routing.advise(_Orch(), "r", action("PLAN"))
        self.assertFalse(got["available"])


if __name__ == "__main__":
    unittest.main()
