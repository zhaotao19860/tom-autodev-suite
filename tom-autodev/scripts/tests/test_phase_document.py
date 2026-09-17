import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase_document import render_phase_markdown


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _change_set_content():
    business = "\n".join(
        ["diff --git a/src/offer_hash.c b/src/offer_hash.c", "--- a/src/offer_hash.c",
         "+++ b/src/offer_hash.c", "@@ -1,3 +1,4 @@", "-unique-business-body old",
         "+unique-business-body new", "+/* " + "b" * 1200 + " */", " unchanged"]
    )
    test = "\n".join(
        ["diff --git a/test/test_offer_hash.py b/test/test_offer_hash.py",
         "--- a/test/test_offer_hash.py", "+++ b/test/test_offer_hash.py", "@@ -1 +1,2 @@",
         " unchanged", "+unique-test-body " + "t" * 1200]
    )
    content = {
        "change_set_id": "CS-T3-3", "task_id": "T3",
        "baseline_revisions": {"business": "b0", "tests": "t0"},
        "revisions": {"business": "b1", "tests": "t1"},
        "business_patch": business, "test_patch": test,
        "full_diff_hash": hashlib.sha256(
            _canonical_json({"business_patch": business, "test_patch": test}).encode("utf-8")
        ).hexdigest(),
        "test_ids": ["offer_hash.nat64_udp_001"],
        "traceability_delta": [
            {"acceptance_point_id": "AC-F1", "test_ids": ["offer_hash.nat64_udp_001"]}
        ],
        "deviations": [],
    }
    content["candidate_hash"] = hashlib.sha256(
        _canonical_json(content).encode("utf-8")
    ).hexdigest()
    return content


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


class ChangeSetRenderingTests(unittest.TestCase):
    """A change set is published as a patch summary, never as the patch bytes.

    Inlining the diff twice — rendered and in the appendix — is what pushed a real
    document past what the knowledge base would store, and it came back empty. The
    patches are content-addressed in the artifact store, so the document has to be
    enough to review the shape of the change and enough to fetch the exact bytes.
    """

    def _render(self, content=None):
        content = content or _change_set_content()
        return render_phase_markdown(
            "05-change-set/T3-r3", content, "hash-c", _canonical_json(content)
        )

    def test_the_patch_bytes_are_summarized_not_inlined(self):
        rendered = self._render()
        self.assertNotIn("unique-business-body", rendered)
        self.assertNotIn("unique-test-body", rendered)
        self.assertIn("## 业务补丁", rendered)
        self.assertIn("- `src/offer_hash.c` +2 -1", rendered)
        self.assertIn("- `test/test_offer_hash.py` +1 -0", rendered)

    def test_the_summary_carries_the_counts_and_the_digest_needed_to_fetch_the_bytes(self):
        content = _change_set_content()
        rendered = self._render(content)
        self.assertIn("- 1 个文件，新增 2 行，删除 1 行，共 " + str(len(content["business_patch"])), rendered)
        self.assertIn("- 1 个文件，新增 1 行，删除 0 行，共 " + str(len(content["test_patch"])), rendered)
        self.assertIn("- 全量 diff 哈希：" + content["full_diff_hash"], rendered)
        digest = hashlib.sha256(content["business_patch"].encode("utf-8")).hexdigest()
        self.assertIn(digest, rendered)

    def test_the_appendix_elides_the_patches_and_says_what_it_elided(self):
        content = _change_set_content()
        rendered = self._render(content)
        appendix = rendered.split("## 附录：规范化 JSON")[1]
        self.assertNotIn("unique-business-body", appendix)
        self.assertIn("`business_patch` " + str(len(content["business_patch"])) + " 字符", appendix)
        self.assertIn("被哈希的是未省略的原文", appendix)
        # The elided appendix must still be JSON, and must still carry the short fields.
        parsed = json.loads(appendix.split("```json")[1].split("```")[0])
        self.assertEqual(parsed["change_set_id"], content["change_set_id"])
        self.assertIn("省略", parsed["business_patch"])

    def test_a_document_with_no_oversized_field_keeps_the_canonical_json_byte_exact(self):
        rendered = render_phase_markdown("03-tasks", {"nodes": ["T1"]}, "hash-b", '{"nodes":["T1"]}')
        appendix = rendered.split("## 附录：规范化 JSON")[1]
        self.assertIn('{"nodes":["T1"]}', appendix)
        self.assertNotIn("省略", appendix)

    def test_the_generic_renderer_also_refuses_to_inline_a_payload_sized_value(self):
        body = "x" * 5000
        rendered = render_phase_markdown("09-release", {"log": body}, "hash-d", "{}")
        self.assertNotIn(body, rendered)
        self.assertIn("共 5000 字符", rendered)
        self.assertIn(hashlib.sha256(body.encode("utf-8")).hexdigest(), rendered)

    def test_deviations_are_stated_even_when_there_are_none(self):
        rendered = self._render()
        self.assertIn("无偏离，改动与获批的任务计划一致。", rendered)
        content = {**_change_set_content(), "deviations": [
            {"from_plan": "计划里用查表", "as_implemented": "改成位运算", "reason": "查表越界"}
        ]}
        rendered = self._render(content)
        self.assertIn("- 计划：计划里用查表", rendered)
        self.assertIn("  - 实现：改成位运算", rendered)
        self.assertIn("  - 理由：查表越界", rendered)


if __name__ == "__main__":
    unittest.main()
