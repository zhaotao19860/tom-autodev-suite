"""A requirement changed after G1 is amended in GRILL, and a G5 "需修改" redoes IMPLEMENT.

BGW-1995: the user dropped exact c2s_mss equality after G1 approved it. The change was
repaired at PLAN, then at SPEC; each regeneration re-derived the equality from the
untouched decision log, and the G5 rejection that followed had no route at all.
"""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_change
import workflow_spec
from phase_protocol import _diagnosis_reentry
from schema_validator import validate_named_schema
from test_phase_protocol_repair import PhaseProtocolPredecessorBindingTests
from test_schema_validation import specialized_examples


def _diagnosis(classification, route):
    diagnosis = specialized_examples()["diagnosis"]
    diagnosis.update({
        "classification": classification,
        "route": route,
        "evidence_state": "SUFFICIENT",
        "hypothesis": "用户在 G1 之后取消了 c2s_mss 严格等值",
        "repair_direction": "在决策日志中去掉 c2s_mss 严格等值，改为不拒绝任何合法签发的 cookie",
        "repair_plan": ["修订 GRILL 决策", "按新决策重出 Spec 与计划"],
        "repair_diff_hash": None,
        "build_id": None, "stage_id": None, "job_id": None, "environment_fingerprint": None,
    })
    diagnosis.pop("repair_scope", None)
    return diagnosis


class RequirementChangeRouteTests(unittest.TestCase):
    def _issues(self, content):
        return [(issue.path, issue.kind) for issue in validate_named_schema(content, "diagnosis")]

    def test_requirement_change_must_route_to_grill(self):
        self.assertEqual(self._issues(_diagnosis("USER_REQUIREMENT_CHANGED", "GRILL")), [])
        self.assertEqual(self._issues(_diagnosis("USER_REQUIREMENT_CHANGED", "STOPPED")), [])
        for route in ("SPEC", "REPAIR", "ARCHITECTURE_REVIEW"):
            with self.subTest(route=route):
                self.assertIn(("route", "inconsistent"),
                              self._issues(_diagnosis("USER_REQUIREMENT_CHANGED", route)))

    def test_grill_route_is_only_for_a_requirement_change(self):
        self.assertIn(("classification", "inconsistent"),
                      self._issues(_diagnosis("PLAN_INCOMPLETE", "GRILL")))

    def test_diagnose_to_grill_is_a_g6_gated_edge(self):
        self.assertIn("GRILL", workflow_spec.allowed_transitions()["DIAGNOSE"])
        self.assertEqual(workflow_spec.TRANSITION_REQUIREMENTS[("DIAGNOSE", "GRILL")],
                         (("failure-bundle",), "G6"))

    def test_approved_direction_travels_to_the_redone_phase(self):
        content = _diagnosis("USER_REQUIREMENT_CHANGED", "GRILL")
        reentry = _diagnosis_reentry({"content": content, "content_hash": "d" * 64}, "artifact-d")
        self.assertEqual(reentry["kind"], "DIAGNOSIS")
        self.assertEqual(reentry["reason"], content["repair_direction"])
        self.assertEqual(reentry["repair_plan"], content["repair_plan"])
        self.assertEqual(reentry["diagnosis_artifact_id"], "artifact-d")


class RequirementChangeReclassificationTests(PhaseProtocolPredecessorBindingTests):
    def _diagnose_after_block(self, run_id, classification):
        revisions = {"business": "r2", "tests": "t2"}
        self.seed(run_id, "IMPLEMENT", "T-1", specialized_examples()["change-set"], revisions=revisions)
        self.state.transition(run_id, "DIAGNOSE", {
            "requirement_id": "BGW-1", "profile_hash": "a" * 64, "task_id": "T-1",
            "source_revisions": copy.deepcopy(revisions), "classification": classification,
        })
        action = self.protocol.next(run_id)
        self.assertTrue(action["ok"], action)
        return action

    def test_reported_requirement_change_cannot_be_relabelled(self):
        action = self._diagnose_after_block("run-relabel", "USER_REQUIREMENT_CHANGED")
        relabelled = _diagnosis("PLAN_INCOMPLETE", "REPAIR")
        relabelled["frozen_revisions"] = dict(action["source_revisions"])
        result = self.validate_draft(action, relabelled)
        self.assertEqual(result["reason_code"], "REQUIREMENT_CHANGE_RECLASSIFIED")
        self.assertTrue(result.get("content_invalid"))

        kept = _diagnosis("USER_REQUIREMENT_CHANGED", "GRILL")
        kept["frozen_revisions"] = dict(action["source_revisions"])
        self.assertNotEqual(self.validate_draft(action, kept).get("reason_code"),
                            "REQUIREMENT_CHANGE_RECLASSIFIED")


class ChangeSetReviseTests(unittest.TestCase):
    def test_g5_is_revisable(self):
        self.assertIn("IMPLEMENT", workflow_spec.REVISABLE_STATES)
        self.assertIn("G5", workflow_spec.REVISABLE_GATES)

    def _revise(self, action):
        current = {"event_id": "implement-event", "state": "IMPLEMENT", "payload": {
            "source_revisions": {"business": "mutable", "tests": "mutable"},
        }}
        approval = {"run_id": "run-g5", "action": "G5", "approval_id": "approval-g5",
                    "effective_decision": "REVISE", "input_hash": "h" * 64}
        fake = MagicMock()
        fake.state.idempotency_result.return_value = None
        fake.state.events.return_value = [current]
        fake.approvals.get.return_value = approval
        fake.state.commit_transition_result.return_value = {
            "status": "COMMITTED", "result": {"ok": True, "state": "IMPLEMENT"}
        }
        with patch.object(run_change, "_current_draft",
                          return_value=({**action, "source_event_id": "implement-event"},
                                        "producer:old", {"summary": "rejected"})), \
                patch.object(run_change, "_draft_approval_hash", return_value="h" * 64):
            result = run_change.revise(fake, "run-g5", "approval-g5",
                                       feedback="c2s_mss 不得严格等值", actor="owner")
        return result, fake

    def test_redo_keeps_the_rejected_change_sets_plan_and_baseline(self):
        action = {
            "ok": True, "phase": "IMPLEMENT", "state": "IMPLEMENT", "task_id": "T1",
            "source_revisions": {"business": "b1", "tests": "t1"},
            "input_artifacts": [{"artifact_id": "plan-1", "phase": "PLAN",
                                 "task_id": "T1", "content_hash": "p" * 64}],
        }
        result, fake = self._revise(action)
        self.assertTrue(result["ok"], result)
        payload = fake.state.commit_transition_result.call_args.args[3]
        self.assertEqual(payload["source_revisions"], {"business": "b1", "tests": "t1"})
        self.assertEqual((payload["plan_artifact_id"], payload["plan_content_hash"]),
                         ("plan-1", "p" * 64))
        self.assertEqual(payload["task_id"], "T1")
        self.assertEqual(payload["reentry"]["from_state"], "IMPLEMENT")
        self.assertEqual(payload["reentry"]["feedback"], "c2s_mss 不得严格等值")

    def test_comate_feedback_settles_a_pending_gate_then_redoes(self):
        action = {
            "ok": True, "phase": "IMPLEMENT", "state": "IMPLEMENT", "task_id": "T1",
            "source_revisions": {"business": "b1", "tests": "t1"},
            "input_artifacts": [{"artifact_id": "plan-1", "phase": "PLAN",
                                 "task_id": "T1", "content_hash": "p" * 64}],
        }
        current = {"event_id": "implement-event", "state": "IMPLEMENT", "payload": {}}
        pending = {"run_id": "run-g5", "action": "G5", "approval_id": "approval-g5",
                   "effective_decision": None, "input_hash": "h" * 64}
        settled = {**pending, "effective_decision": "REVISE"}
        fake = MagicMock()
        fake.state.idempotency_result.return_value = None
        fake.state.events.return_value = [current]
        fake.approvals.get.side_effect = [pending, settled]
        fake.approvals.receive.return_value = {"reason_code": "OK"}
        fake.state.commit_transition_result.return_value = {
            "status": "COMMITTED", "result": {"ok": True, "state": "IMPLEMENT"}}
        with patch.object(run_change, "_current_draft",
                          return_value=({**action, "source_event_id": "implement-event"},
                                        "producer:old", {"summary": "pending"})), \
                patch.object(run_change, "_draft_approval_hash", return_value="h" * 64):
            result = run_change.revise(fake, "run-g5", "approval-g5",
                                       feedback="c2s_mss 不得严格等值", actor="owner")
        self.assertTrue(result["ok"], result)
        args, kwargs = fake.approvals.receive.call_args
        self.assertEqual(args[:4], ("approval-g5", "REVISE", "h" * 64, "comate"))
        self.assertEqual(args[4], "owner")

    def test_comate_feedback_without_text_cannot_settle_a_pending_gate(self):
        action = {"ok": True, "phase": "IMPLEMENT", "state": "IMPLEMENT", "task_id": "T1",
                  "source_revisions": {"business": "b1", "tests": "t1"},
                  "input_artifacts": [{"artifact_id": "plan-1", "phase": "PLAN",
                                       "task_id": "T1", "content_hash": "p" * 64}]}
        current = {"event_id": "implement-event", "state": "IMPLEMENT", "payload": {}}
        pending = {"run_id": "run-g5", "action": "G5", "approval_id": "approval-g5",
                   "effective_decision": None, "input_hash": "h" * 64}
        fake = MagicMock()
        fake.state.idempotency_result.return_value = None
        fake.state.events.return_value = [current]
        fake.approvals.get.return_value = pending
        with patch.object(run_change, "_current_draft",
                          return_value=({**action, "source_event_id": "implement-event"},
                                        "producer:old", {"summary": "pending"})), \
                patch.object(run_change, "_draft_approval_hash", return_value="h" * 64), \
                patch.object(run_change, "_journal_feedback", return_value=""):
            result = run_change.revise(fake, "run-g5", "approval-g5", feedback=None, actor="owner")
        self.assertEqual(result["reason_code"], "APPROVAL_NOT_REJECTED")
        fake.approvals.receive.assert_not_called()

    def test_redo_without_a_plan_pin_is_refused(self):
        action = {"ok": True, "phase": "IMPLEMENT", "state": "IMPLEMENT", "task_id": "T1",
                  "source_revisions": {"business": "b1", "tests": "t1"}, "input_artifacts": []}
        result, fake = self._revise(action)
        self.assertEqual(result["reason_code"], "PLAN_PIN_REQUIRED")
        fake.state.commit_transition_result.assert_not_called()


class DiagnosisCardTests(unittest.TestCase):
    def test_chinese_prose_with_identifiers_is_shown_not_held_for_translation(self):
        from approval_summary import _diagnosis_text, _signature_text

        for text in (
            "把 NAT44、NAT64、NAT66 各自列为验收点，正向含客户端 MSS 低于 c2s_mss 的情形。",
            "两者直接冲突，属需求变更；在 PLAN/SPEC 层修复会被未修订的决策日志推回。",
            "G1 决策 b2_eq_cfg（如流 CHOICE bd105030b305b342）：配置 c2s_mss 时严格等值。",
            "MSS 索引越界、nortt 保留位非零、wscale 超出合法范围才拒绝。",
        ):
            with self.subTest(text=text):
                self.assertNotIn("材料待翻译", _diagnosis_text(text))
        held = _diagnosis_text("原因：The current Review rejection has two causes.")
        self.assertIn("材料待翻译", held)
        signature = "USER_REQUIREMENT_CHANGED: exact-index equality withdrawn after approval"
        self.assertEqual(_signature_text(signature), f"`{signature}`")

    def test_diagnosis_body_lists_decision_fields_and_counts_supporting_evidence(self):
        from approval_delivery import _draft_lines

        draft = _diagnosis("USER_REQUIREMENT_CHANGED", "GRILL")
        lines = _draft_lines(draft)
        text = "\n".join(lines)
        self.assertIn("### 修复方向", text)
        self.assertIn("### 修复计划", text)
        self.assertNotIn("### 日志证据", text)
        self.assertIn("原文见材料包", text)
        self.assertNotIn("材料待翻译", text)


class AllCardsConciseTests(unittest.TestCase):
    """The reading-list trim and summary de-dup apply to every gate, not just G6."""

    def _render(self, action, draft):
        from approval_summary import content_summary, references
        from approval_delivery import _approval_markdown
        gate = {
            "subject": "示例", "effect": "示例", "checklist": ["核对点"],
            "summary": content_summary(draft), "references": references(draft),
            "decision": "同意仅授权本次 input_hash。", "content_hash": "a" * 64,
            "material_status": "PINNED", "_review_content": {"draft": draft},
        }
        req = {"approval_id": "a" * 32, "run_id": "r" * 32, "action": action,
               "input_hash": "b" * 64, "deadline_at": "2026-10-09T00:00:00+00:00",
               "evidence": {"card_id": "BGW-1", "gate": gate}}
        return _approval_markdown(req, ["zhaotao02"])

    def test_non_g6_cards_point_to_the_body_and_drop_raw_id_dumps(self):
        from test_schema_validation import specialized_examples
        for action, key in (("G2", "spec"), ("G5", "change-set")):
            with self.subTest(action=action):
                md = self._render(action, specialized_examples()[key])
                self.assertIn("完整内容见下方「审批正文」", md)
                self.assertIn("**审批正文", md)
                # The reading-list region (请先查看 … 决定边界) must not dump raw
                # artifact-id / evidence-ref labels; those only repeat the body/pack.
                region = md.split("**请先查看这些文件 / 证据**", 1)[1].split("**决定边界**", 1)[0]
                for noise in ("source_evidence_refs", "input_artifacts", "candidate_hash",
                              "full_diff_hash", "baseline_revisions", "artifact_id"):
                    self.assertNotIn(noise, region)
                # A card that shows the body never also claims the material is missing.
                self.assertNotIn("未提供本次固定版本材料的位置", md)
                self.assertNotIn("材料待翻译", md)


class DiagnosisReviseTests(unittest.TestCase):
    def test_g6_is_revisable_and_the_redo_keeps_the_entry_binding(self):
        self.assertIn("G6", workflow_spec.REVISABLE_GATES)
        entry = {"reason_code": "PLAN_INCOMPLETE", "producer_block_report_id": "block-1",
                 "classification": "USER_REQUIREMENT_CHANGED", "task_id": "T1",
                 "source_revisions": {"business": "b1", "tests": "t1"}}
        current = {"event_id": "diagnose-event", "state": "DIAGNOSE", "payload": entry}
        approval = {"run_id": "run-g6", "action": "G6", "approval_id": "approval-g6",
                    "effective_decision": "REVISE", "input_hash": "h" * 64}
        fake = MagicMock()
        fake.state.idempotency_result.return_value = None
        fake.state.events.return_value = [current]
        fake.approvals.get.return_value = approval
        fake.state.commit_transition_result.return_value = {
            "status": "COMMITTED", "result": {"ok": True, "state": "DIAGNOSE"}}
        action = {"ok": True, "phase": "DIAGNOSE", "state": "DIAGNOSE", "task_id": "T1",
                  "source_event_id": "diagnose-event",
                  "source_revisions": {"business": "b1", "tests": "t1"}}
        with patch.object(run_change, "_current_draft",
                          return_value=(action, "producer:old", {"route": "GRILL"})), \
                patch.object(run_change, "_draft_approval_hash", return_value="h" * 64):
            result = run_change.revise(fake, "run-g6", "approval-g6",
                                       feedback="材料要精简", actor="owner")
        self.assertTrue(result["ok"], result)
        payload = fake.state.commit_transition_result.call_args.args[3]
        for key, value in entry.items():
            self.assertEqual(payload[key], value)
        self.assertEqual(payload["reentry"]["from_state"], "DIAGNOSE")


def load_tests(loader, tests, pattern):
    # Only the tests defined here; the borrowed fixture class keeps its own suite.
    suite = unittest.TestSuite()
    for case in (RequirementChangeRouteTests, RequirementChangeReclassificationTests,
                 ChangeSetReviseTests, DiagnosisCardTests, DiagnosisReviseTests,
                 AllCardsConciseTests):
        for name in sorted(vars(case)):
            if name.startswith("test_"):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
