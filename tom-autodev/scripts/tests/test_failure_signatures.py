"""Failure identity must preserve causes and freeze already-recorded occurrences."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval_ledger import ApprovalLedger
from clients.ipipe_runtime import _failure_signature
from state_store import StateStore
from test_ipipe_runtime import FakeApi, PROFILE, REVISIONS, build, pinned_runtime


def signature(message):
    return _failure_signature(
        "pipe-1", "bgw", [{"stage_conf_id": "conf-1", "name": "unit", "status": "FAIL"}],
        [{"name": "run-tests", "status": "FAIL", "message": message}],
    )


class FailureNormalizationTests(unittest.TestCase):
    def test_codes_and_case_identity_survive_normalization(self):
        pairs = [
            ("error code 2147942402", "error code 2147942405"),
            ("error deadbeef", "error baadcafe"),
            ("error code 0xdeadbeef", "error code 0xbaadcafe"),
            ("case abcdefab failed", "case deadbeef failed"),
            ("case 987654321 failed", "case 987654322 failed"),
            ("case ID AbCdEfAb failed", "case ID abcdefab failed"),
            ("ValueError: failed", "valueerror: failed"),
            ("AssertError: case failed", "TimeoutError: case failed"),
            ("build failed: code 503", "build denied: code 503"),
            ("request failed", "request rejected"),
            ("request HTTP401Error", "request HTTP503Error"),
            ("build Module1Exception", "build Module2Exception"),
            ("test case id 12345678-abcd-4eef-8afe-123456789abc failed", "test case id 87654321-dead-4ace-9abe-cba987654321 failed"),
            ("case abcdefab-abcd-abcd-abcd-abcdefabcdef failed", "case deadbeef-abcd-abcd-abcd-abcdefabcdef failed"),
            ("expected " + "x" * 210 + " case deadbeef", "expected " + "x" * 210 + " case baadcafe"),
            ("failed at /work/build-123/tests/test_api.py::case_deadbeef", "failed at /work/build-456/tests/test_api.py::case_baadcafe"),
            ("failed at /work/build-123/tests/test_api.py:14", "failed at /work/build-456/tests/test_dns.py:14"),
            ("failed at /work/build-123/tests/api/test_base.py::test_run", "failed at /work/build-456/tests/dns/test_base.py::test_run"),
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertNotEqual(signature(left), signature(right))

    def test_explicit_occurrence_fields_do_not_split_the_same_failure(self):
        pairs = [
            ("AssertionError build 987", "AssertionError build 654"),
            ("test build 987 failed", "test build 654 failed"),
            ("AssertionError build_id=build-987", "AssertionError build_id=build-654"),
            ('AssertionError build_id="build-987"', 'AssertionError build_id="build-654"'),
            ("AssertionError request_id=req-123", "AssertionError request_id=req-456"),
            ('AssertionError "request_id": "req-123"', 'AssertionError "request_id": "req-456"'),
            ("AssertionError request 987", "AssertionError request 654"),
            ("AssertionError rev 6f3a9c1d", "AssertionError rev 8b2e5f7a"),
            ("AssertionError commit deadbeef", "AssertionError commit baadcafe"),
            ("AssertionError hash=deadbeef", "AssertionError hash=baadcafe"),
            ("AssertionError request-id: 00001234", "AssertionError request-id: 00005678"),
            ("AssertionError at 2026-09-21T10:00:00+08:00", "AssertionError at 2026-09-22T12:31:22-04:00"),
            ("AssertionError req 12345678-abcd-4eef-8afe-123456789abc", "AssertionError req 87654321-dead-4ace-9abe-cba987654321"),
            ("AssertionError at /work/build-987/tests/test_api.py:42:3", "AssertionError at /work/build-654/tests/test_api.py:57:4"),
            (r"AssertionError at C:\work\build-987\tests\test_api.py:42", r"AssertionError at C:\work\build-654\tests\test_api.py:57"),
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertEqual(signature(left), signature(right))

    def test_ci_checkout_roots_beyond_work_do_not_split_the_same_failure(self):
        # The volatile checkout root (and a numeric/uuid build dir under it) must not
        # fragment one root cause across CI machines whose root is not /work. baidu BGW
        # checkouts live under /home and /ssd*; the volatile dir may also be nested.
        pairs = [
            ("failed at /home/work/ci-1/pkg/test_api.py:5", "failed at /home/work/ci-2/pkg/test_api.py:5"),
            ("failed at /opt/ci/run-111/test_x.py:14", "failed at /opt/ci/run-222/test_x.py:14"),
            ("failed at /ssd2/build-99/api/test_base.py::case_a", "failed at /ssd7/build-12/api/test_base.py::case_a"),
            ("failed at /data/jobs/2026/pkg/test_run.py::t", "failed at /data/jobs/2025/pkg/test_run.py::t"),
            ("failed at /home/work/run5/pkg/test_v2.py::t", "failed at /home/work/run8/pkg/test_v2.py::t"),
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertEqual(signature(left), signature(right))

    def test_distinct_tests_stay_distinct_under_broadened_roots(self):
        # Broadening the recognized roots must not collapse genuinely different tests:
        # the filename and non-volatile source directories are preserved.
        pairs = [
            ("failed at /home/work/ci-1/api/test_base.py::t", "failed at /home/work/ci-1/dns/test_base.py::t"),
            ("failed at /opt/ci/run-111/test_api.py:1", "failed at /opt/ci/run-111/test_dns.py:1"),
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertNotEqual(signature(left), signature(right))

    def test_v2_does_not_alias_an_unchanged_legacy_digest(self):
        # Even a message unchanged by v2 cannot prove that the old bucket was never mixed.
        payload = {
            "pipeline_id": "pipe-1", "module": "bgw",
            "stages": [["conf-1", "unit", "FAIL"]],
            "jobs": [["run-tests", "FAIL", "assertion failed"]],
        }
        legacy = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        current = signature("assertion failed")
        self.assertRegex(current, r"^ipipe-failure:v2:[0-9a-f]{64}$")
        self.assertNotEqual(current, legacy)
        self.assertEqual(current, signature("assertion failed"))

    def test_order_and_occurrence_ids_do_not_change_v2(self):
        stages = [
            {"stage_conf_id": "conf-1", "name": "unit", "status": "FAIL", "stage_build_id": "s-1"},
            {"stage_conf_id": "conf-2", "name": "compile", "status": "FAIL", "stage_build_id": "s-2"},
        ]
        jobs = [
            {"name": "a", "status": "FAIL", "message": "code 401", "job_build_id": "j-1"},
            {"name": "b", "status": "FAIL", "message": "code 503", "job_build_id": "j-2"},
        ]
        current = _failure_signature("pipe-1", "bgw", stages, jobs)
        for item in stages:
            item["stage_build_id"] += "-new"
        for item in jobs:
            item["job_build_id"] += "-new"
        self.assertEqual(current, _failure_signature("pipe-1", "bgw", list(reversed(stages)), list(reversed(jobs))))


class FrozenFailureSignatureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.state = StateStore(root / "state.sqlite")
        self.ledger = ApprovalLedger(root / "approvals.sqlite")
        self.api = FakeApi()
        self.runtime = pinned_runtime(self.state, self.ledger, self.api)

    def monitor(self, build_id, stage_ids):
        candidate = build(build_id, status="FAIL")
        self.api.candidates = [candidate]
        self.api.builds[build_id] = candidate
        self.api.stages[build_id] = [
            {"id": stage_id, "stageName": "unit", "status": "FAIL"} for stage_id in stage_ids
        ]
        self.api.jobs[build_id] = [{
            "id": f"job-{build_id}", "jobName": "run-tests", "status": "FAIL",
            "message": "error code 2147942402 build 987",
        }]
        self.assertTrue(self.runtime.discover(PROFILE, REVISIONS)["ok"])
        return self.runtime.monitor(build_id, (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())

    def test_legacy_occurrence_keeps_its_frozen_signature_and_history(self):
        legacy = "a" * 64
        key = "ipipe.stage-failure:run-1:old-stage"
        self.state.save_idempotency_result(key, {"failure_signature": legacy})
        self.state.record_failure_case(legacy, "CODE", "old-run-a")
        self.state.record_failure_case(legacy, "CODE", "old-run-b")
        before = self.state.failure_cases()
        try:
            observed = self.monitor("old-build", ["old-stage"])
        except ValueError as error:
            self.fail(f"Re-observing frozen evidence must not overwrite it: {error}")
        self.assertEqual(observed["failure_signature"], legacy)
        self.assertEqual(self.state.idempotency_result(key), {"failure_signature": legacy})
        self.assertEqual(self.state.failure_cases(), before)
        fresh = self.monitor("new-build", ["new-stage"])
        self.assertRegex(fresh["failure_signature"], r"^ipipe-failure:v2:")
        self.assertNotEqual(fresh["failure_signature"], legacy)

    def test_conflicting_frozen_stages_fail_without_writing_a_new_signature(self):
        for stage, value in (("old-a", "a" * 64), ("old-b", "b" * 64)):
            self.state.save_idempotency_result(f"ipipe.stage-failure:run-1:{stage}", {"failure_signature": value})
        try:
            result = self.monitor("old-build", ["new-stage", "old-a", "old-b"])
        except ValueError as error:
            self.fail(f"Frozen identity conflict needs a deterministic result: {error}")
        self.assertEqual(result["reason_code"], "FAILURE_SIGNATURE_CONFLICT")
        self.assertIsNone(self.state.idempotency_result("ipipe.stage-failure:run-1:new-stage"))

    def test_new_stages_in_the_same_occurrence_share_the_frozen_identity(self):
        legacy = "a" * 64
        self.state.save_idempotency_result("ipipe.stage-failure:run-1:old-stage", {"failure_signature": legacy})
        try:
            result = self.monitor("old-build", ["old-stage", "new-stage"])
        except ValueError as error:
            self.fail(f"A partial checkpoint must resume with one identity: {error}")
        self.assertEqual(result["failure_signature"], legacy)
        self.assertEqual(self.state.idempotency_result("ipipe.stage-failure:run-1:new-stage"), {"failure_signature": legacy})

    def test_concurrent_stage_checkpoints_never_split_one_occurrence(self):
        import clients.ipipe_runtime as runtime_module

        barrier = threading.Barrier(2)

        def observe(stage_ids, candidate):
            barrier.wait(timeout=3)
            return runtime_module._freeze_stage_failure_signature(self.state, "run-1", stage_ids, candidate)

        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(observe, ["s-a", "s-b"], "ipipe-failure:v2:" + "a" * 64)
            b = executor.submit(observe, ["s-b", "s-a"], "ipipe-failure:v2:" + "b" * 64)
            results = [a.result(timeout=5), b.result(timeout=5)]
        self.assertEqual(results[0], results[1])
        for stage in ("s-a", "s-b"):
            self.assertEqual(self.state.idempotency_result(f"ipipe.stage-failure:run-1:{stage}"),
                             {"failure_signature": results[0]})


class FailureSignatureInventoryTests(unittest.TestCase):
    def test_inventory_preserves_legacy_rows_and_reports_replay_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            state = StateStore(database)
            state.record_failure_case("a" * 64, "CODE", "legacy-a")
            state.record_failure_case("a" * 64, "CODE", "legacy-b")
            state.record_failure_case("ipipe-failure:v2:" + "b" * 64, "CODE", "current")
            before = database.read_bytes()
            script = Path(__file__).resolve().parents[1] / "failure_signature_inventory.py"
            process = subprocess.run([sys.executable, str(script), str(database)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(process.stdout)
            self.assertEqual(report["counts"], {"legacy_unversioned": 1, "v2": 1, "other": 0})
            self.assertFalse(report["automatic_migration_safe"])
            legacy = next(case for case in report["cases"] if case["signature"] == "a" * 64)
            self.assertEqual(legacy["run_ids"], ["legacy-a", "legacy-b"])
            self.assertEqual(legacy["occurrences"], 2)
            self.assertEqual(legacy["migration_status"], "RAW_OCCURRENCE_REPLAY_REQUIRED")
            self.assertEqual(database.read_bytes(), before)

    def test_inventory_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "missing.sqlite"
            script = Path(__file__).resolve().parents[1] / "failure_signature_inventory.py"
            process = subprocess.run([sys.executable, str(script), str(database)], capture_output=True, text=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
