import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_questions import apply_answers, validate_questions


QUESTIONS = [{
    "id": "strategy",
    "title": "修复策略",
    "prompt": "本次采用哪种修复策略？",
    "context": "证据指向低位校验缺少零守卫。",
    "options": [
        {"id": "A", "label": "恢复低位零守卫"},
        {"id": "B", "label": "增加兼容字段校验"},
        {"id": "C", "label": "两种方案都实现"},
        {"id": "D", "label": "其他"},
    ],
    "allow_multiple": False,
    "required": True,
    "other_allowed": True,
    "evidence": ["ku://analysis"],
    "impact": "决定后续 Spec 和验收边界。",
}]


class DecisionQuestionsTests(unittest.TestCase):
    def test_validates_question_shape_and_duplicate_options(self):
        self.assertEqual(validate_questions(QUESTIONS), [])
        bad = copy.deepcopy(QUESTIONS)
        bad[0]["options"].append({"id": "A", "label": "重复"})
        self.assertIn("choice_questions[0].options[4].id:DUPLICATE", validate_questions(bad))

    def test_partial_answers_keep_unanswered_questions_and_never_default_them(self):
        log = {
            "decision_result": "UNRESOLVED", "status": "INCOMPLETE",
            "decisions": [], "choice_questions": QUESTIONS,
            "unresolved_frontier": ["strategy"], "glossary_delta": {},
            "adr_candidates": [], "source_evidence": ["icafe://BGW-1"],
        }
        result = apply_answers(
            log, QUESTIONS,
            {},
            decided_by="owner@baidu.com",
            answer_evidence=["comate:round-1"],
        )
        self.assertEqual(result["decisions"], [])
        self.assertEqual(result["choice_questions"], QUESTIONS)
        self.assertEqual(result["decision_result"], "UNRESOLVED")

    def test_other_requires_text_and_maps_selected_option_to_decision(self):
        log = {
            "decision_result": "UNRESOLVED", "status": "INCOMPLETE",
            "decisions": [], "choice_questions": QUESTIONS,
            "unresolved_frontier": ["strategy"], "glossary_delta": {},
            "adr_candidates": [], "source_evidence": ["icafe://BGW-1"],
        }
        with self.assertRaisesRegex(ValueError, "OTHER_TEXT_REQUIRED"):
            apply_answers(
                log, QUESTIONS, {"strategy": "D"},
                decided_by="owner@baidu.com", answer_evidence=["comate:round-1"],
            )
        result = apply_answers(
            log, QUESTIONS,
            {"strategy": {"choice": "D", "other_text": "保留现有布局并增加零守卫"}},
            decided_by="owner@baidu.com",
            answer_evidence=["comate:round-1"],
        )
        self.assertEqual(result["decision_result"], "DECISIONS_RECORDED")
        self.assertEqual(result["choice_questions"], [])
        self.assertIn("其他：保留现有布局并增加零守卫", result["decisions"][0]["choice"])
