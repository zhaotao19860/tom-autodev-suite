import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase_document import render_phase_markdown


def _spec_content():
    return {
        "version": "1.0.0",
        "behaviors": [
            {"id": "B1", "number": 1, "description": "URL 归一化：按 17 条规则归一化三段。"},
            {"id": "B2", "number": 2, "description": "没有短主语的一整句行为描述，直接落在正文里。"},
        ],
        "acceptance_scenarios": [
            {
                "id": "S-F1",
                "acceptance_point_ids": ["AC-F1", "AC-F2"],
                "given": "开启 offer_hash_enable",
                "when": "调用 offer_hash_extract_resource()",
                "then": "返回 PARSE_OK",
            }
        ],
        "boundaries": ["候选规模上限 512"],
        "errors": ["解析失败回退默认调度"],
        "compatibility": ["未开启的 service 逐字节一致"],
        "test_interface": ["offer_hash_extract_resource()"],
        "environment_requirements": ["ASan 构建"],
        "non_goals": ["不做 TCP 方向"],
        "risks": ["分布不均"],
        "rollback": ["关闭开关"],
        "release_evidence": ["iPipe 单测通过"],
        "traceability": [
            {"acceptance_point_id": "AC-F1", "behavior_ids": ["B1"], "scenario_ids": ["S-F1"]}
        ],
    }


class SpecRenderingTests(unittest.TestCase):
    def _render(self, content=None):
        return render_phase_markdown("02-spec", content or _spec_content(), "hash-a", "{}")

    def test_a_behaviors_subject_becomes_its_heading(self):
        rendered = self._render()
        self.assertIn("### B1 URL 归一化", rendered)
        self.assertIn("按 17 条规则归一化三段。", rendered)
        # Without a short leading subject the description stays whole in the body.
        self.assertIn("### B2\n", rendered)
        self.assertIn("没有短主语的一整句行为描述，直接落在正文里。", rendered)

    def test_a_scenario_is_readable_as_given_when_then_with_its_acceptance_points(self):
        rendered = self._render()
        self.assertIn("### S-F1", rendered)
        self.assertIn("- 验收点：AC-F1、AC-F2", rendered)
        self.assertIn("- Given：开启 offer_hash_enable", rendered)
        self.assertIn("- When：调用 offer_hash_extract_resource()", rendered)
        self.assertIn("- Then：返回 PARSE_OK", rendered)

    def test_every_spec_section_and_the_trace_table_are_rendered(self):
        rendered = self._render()
        for heading in (
            "## 概览", "## 行为", "## 验收场景", "## 边界", "## 错误与降级", "## 兼容性",
            "## 测试接口", "## 环境要求", "## 非目标", "## 风险", "## 回滚", "## 发布证据",
            "## 追溯",
        ):
            self.assertIn(heading, rendered)
        self.assertIn("| 验收点 | 行为 | 验收场景 |", rendered)
        self.assertIn("| AC-F1 | B1 | S-F1 |", rendered)
        self.assertIn("- Spec 版本：1.0.0", rendered)
        self.assertIn("行为 2 条，验收场景 1 条，验收点追溯 1 条", rendered)

    def test_no_schema_field_is_dumped_as_raw_json_outside_the_appendix(self):
        rendered = self._render()
        body = rendered.split("## 附录：规范化 JSON")[0]
        self.assertNotIn('{"', body)
        self.assertNotIn("acceptance_point_ids", body)

    def test_content_without_the_spec_shape_still_uses_the_generic_renderer(self):
        rendered = render_phase_markdown("03-tasks", {"nodes": ["T1"]}, "hash-b", "{}")
        self.assertIn("## nodes", rendered)


if __name__ == "__main__":
    unittest.main()
