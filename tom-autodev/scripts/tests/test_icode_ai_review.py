import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.icode_ai_review import IcodeAiReview
from state_store import StateStore


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        if not self.responses:
            raise AssertionError("unexpected extra call")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeArtifactStore:
    def __init__(self):
        self.calls = []

    def put(self, run_id, kind, content, metadata):
        self.calls.append((run_id, kind, json.loads(content), metadata))
        return {"artifact_id": "artifact-1", "kind": kind}


def ok(body):
    return {"returncode": 0, "stdout": json.dumps(body), "stderr": ""}


class IcodeAiReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = StateStore(Path(self.temp.name) / "state.sqlite")

    def runtime(self, responses, **changes):
        values = {
            "state_store": self.state,
            "run_id": "run-1",
            "argv_transport": FakeTransport(responses),
            "working_directory": self.temp.name,
            "binary_candidates": ["/fake/icode-cli"],
            "executable_resolver": lambda value: value,
            "sleeper": lambda _seconds: None,
            "poll_interval": 0,
            "max_polls": 3,
        }
        values.update(changes)
        return IcodeAiReview(**values)

    def test_a_trigger_records_the_conversation_id_and_replays_it(self):
        started = ok({"status": "OK", "data": {"conversationId": "cid-1"}})
        runtime = self.runtime([started])

        first = runtime.start(122396573, "rev-1")
        # A second runtime shares only the ledger, which is where the id has to live.
        second = self.runtime([]).start(122396573, "rev-1")

        self.assertEqual(first["reason_code"], "OK")
        self.assertEqual(first["conversation_id"], "cid-1")
        self.assertEqual(second, first)
        results = self.state.external_results("run-1")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["receipt"]["response"]["conversation_id"], "cid-1")

    def test_a_trigger_that_left_no_receipt_is_reported_not_repeated(self):
        """The id cannot be recovered from the change number, so never re-trigger."""
        runtime = self.runtime([Exception("process died after the write")])
        with self.assertRaises(Exception):
            runtime.start(1, "rev-1")

        retried = self.runtime([]).start(1, "rev-1")

        self.assertEqual(retried["reason_code"], "AI_REVIEW_CONVERSATION_LOST")

    def test_a_rejected_trigger_keeps_the_platform_reason(self):
        runtime = self.runtime([ok({"status": "BAD_REQUEST", "message": "无权限！"})])

        result = runtime.start(1, "rev-1")

        self.assertEqual(result["reason_code"], "AI_REVIEW_REJECTED")
        self.assertIn("无权限", result["detail"])
        self.assertEqual(self.state.external_results("run-1"), [])

    def test_polling_waits_while_the_platform_still_owes_findings(self):
        pending = ok({"status": "OK", "data": {"status": "RUNNING"}})
        settled = ok({"status": "OK", "data": {"status": "FINISHED", "comments": [
            {"file": "a.c", "line": 10, "content": "boundary check missing"},
        ]}})
        runtime = self.runtime([pending, settled])

        result = runtime.poll("cid-1")

        self.assertEqual(result["reason_code"], "OK")
        self.assertEqual(result["status"], "SETTLED")
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["file"], "a.c")

    def test_polling_gives_up_as_pending_rather_than_claiming_success(self):
        pending = ok({"status": "OK", "data": {"status": "RUNNING"}})
        runtime = self.runtime([pending, pending, pending])

        result = runtime.poll("cid-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "AI_REVIEW_PENDING")

    def test_findings_are_found_under_whichever_key_the_deployment_uses(self):
        for key in ("comments", "findings", "issues", "reviews", "suggestions"):
            with self.subTest(key=key):
                body = ok({"status": "OK", "data": {"status": "DONE", key: [{"content": "x"}]}})
                result = self.runtime([body]).poll("cid-1")
                self.assertEqual(result["findings"], [{"content": "x"}])

    def test_the_raw_payload_travels_with_the_parse(self):
        body = ok({"status": "OK", "data": {"status": "DONE", "unexpectedShape": [1, 2]}})

        result = self.runtime([body]).poll("cid-1")

        self.assertEqual(result["findings"], [])
        self.assertEqual(result["raw"], {"status": "DONE", "unexpectedShape": [1, 2]})

    def test_the_shapes_the_platform_actually_returns_are_understood(self):
        """Recorded from icode-cli: the trigger is flat, the result keeps `data`."""
        flat_trigger = ok({
            "conversationId": "28dc0443", "message": "操作成功！", "status": "OK",
        })
        self.assertEqual(
            self.runtime([flat_trigger]).start(122396573, "rev-1")["conversation_id"],
            "28dc0443",
        )

        executing = ok({"status": "OK", "message": "操作成功！", "data": {
            "conversationId": "28dc0443", "crStatus": "EXECUTING", "results": None,
        }})
        pending = self.runtime([executing, executing, executing]).poll("28dc0443")
        self.assertEqual(pending["reason_code"], "AI_REVIEW_PENDING")

        finished = ok({"status": "OK", "message": "操作成功！", "data": {
            "conversationId": "28dc0443", "crStatus": "FINISHED",
            "results": [{"filePath": "dataplane/x.c", "content": "no bound check"}],
        }})
        settled = self.runtime([finished]).poll("28dc0443")
        self.assertEqual(settled["status"], "SETTLED")
        self.assertEqual(settled["findings"][0]["filePath"], "dataplane/x.c")

    def test_transcript_results_exclude_tool_trace_and_keep_review_report(self):
        body = ok({"status": "OK", "data": {"crStatus": "FINISHED", "results": [
            {"contentType": "THINKING_TOOL_CALL",
             "processedContent": {"text": "internal tool call"}},
            {"contentType": "OTHER",
             "processedContent": {"text": "## 不足\n真实问题"}},
            {"contentType": "CR_SCORE",
             "processedContent": {"text": "分数: 95"}},
        ]}})

        result = self.runtime([body]).poll("cid-1")

        self.assertEqual(
            result["findings"],
            [
                {"content_type": "OTHER", "text": "## 不足\n真实问题"},
                {"content_type": "CR_SCORE", "text": "分数: 95"},
            ],
        )
        self.assertEqual(result["review_report"], "## 不足\n真实问题")

    def test_settled_review_is_archived_with_raw_payload(self):
        artifacts = FakeArtifactStore()
        body = ok({"status": "OK", "data": {
            "crStatus": "FINISHED",
            "results": [{"contentType": "OTHER",
                         "processedContent": {"text": "report"}}],
        }})

        result = self.runtime([body], artifact_store=artifacts).poll("cid-1")

        self.assertEqual(result["artifact"]["artifact_id"], "artifact-1")
        self.assertEqual(len(artifacts.calls), 1)
        self.assertEqual(artifacts.calls[0][0:2], ("run-1", "ai-review"))
        self.assertEqual(artifacts.calls[0][2]["raw"]["crStatus"], "FINISHED")

    def test_an_invalid_target_never_reaches_the_platform(self):
        transport = FakeTransport([])
        runtime = self.runtime([], argv_transport=transport)

        self.assertEqual(runtime.start(0, "rev-1")["reason_code"], "AI_REVIEW_TARGET_INVALID")
        self.assertEqual(runtime.start(1, "")["reason_code"], "AI_REVIEW_TARGET_INVALID")
        self.assertEqual(runtime.poll("")["reason_code"], "AI_REVIEW_CONVERSATION_REQUIRED")
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
