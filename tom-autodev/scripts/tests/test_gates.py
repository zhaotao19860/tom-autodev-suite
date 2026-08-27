import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_ledger import ApprovalLedger
from evidence_gate import EvidenceGate
from workspace_manager import WorkspaceManager


class WorkspaceGateTests(unittest.TestCase):
    def test_dirty_repo_is_reported_and_missing_baseline_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            (repo / "user.txt").write_text("uncommitted\n", encoding="utf-8")

            evidence = WorkspaceManager().inspect(repo, "run-1", "task-1")

            self.assertEqual(evidence["baseline_status"], "BASELINE_UNVERIFIED")
            self.assertIn("user.txt", evidence["user_changes"])

    def test_matching_baseline_is_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            revision = _git(repo, "rev-parse", "HEAD")

            evidence = WorkspaceManager().inspect(
                repo,
                "run-1",
                "task-1",
                baseline_evidence={
                    "revision": revision,
                    "environment_fingerprint": "env-a",
                },
                environment_fingerprint="env-a",
            )

            self.assertEqual(evidence["baseline_status"], "READY")
            self.assertEqual(evidence["baseline_revision"], revision)


class EvidenceGateTests(unittest.TestCase):
    def test_rejects_empty_evidence_for_a_phase_gate(self):
        result = EvidenceGate().check("GRILL", {})

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason_code"], "MISSING_INPUT_HASH")

    def test_rejects_empty_evidence_for_every_non_intake_phase(self):
        phases = [
            "GRILL",
            "SPEC",
            "TASKS",
            "WORKSPACE",
            "PLAN",
            "IMPLEMENT",
            "REVIEW",
            "DIAGNOSE",
            "ARCHITECTURE_REVIEW",
            "SUBMIT",
            "IPIPE",
            "ENVIRONMENT_BLOCKED",
            "RELEASE",
            "RELEASE_SUCCESS",
        ]
        for phase in phases:
            with self.subTest(phase=phase):
                result = EvidenceGate().check(phase, {})

                self.assertFalse(result["passed"])
                self.assertEqual(result["reason_code"], "MISSING_INPUT_HASH")

    def test_rejects_missing_revision_and_environment_evidence_for_release(self):
        result = EvidenceGate().check(
            "RELEASE",
            {
                "input_hash": "same",
                "approved_input_hash": "same",
                "artifacts": ["ipipe-evidence"],
                "approval_id": "release-approval",
                "approval_record": {
                    "approval_id": "release-approval",
                    "action": "G9",
                    "input_hash": "same",
                    "effective_decision": "APPROVE",
                },
            },
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason_code"], "MISSING_REVISION_EVIDENCE")

    def test_rejects_changed_input_hash(self):
        result = EvidenceGate().check(
            "G5",
            {
                "input_hash": "new",
                "approved_input_hash": "old",
                "required_artifacts": [],
                "artifacts": [],
            },
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason_code"], "INPUT_HASH_MISMATCH")

    def test_rejects_missing_and_stale_evidence(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        result = EvidenceGate().check(
            "G9",
            {
                "input_hash": "same",
                "approved_input_hash": "same",
                "approval_id": "approval-g9",
                "approval_record": {
                    "approval_id": "approval-g9",
                    "action": "G9",
                    "input_hash": "same",
                    "effective_decision": "APPROVE",
                },
                "required_artifacts": ["release-evidence"],
                "artifacts": [],
                "verification_created_at": old,
                "checkpoint_started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason_code"], "MISSING_ARTIFACT")
        self.assertEqual(result["missing_evidence"], ["release-evidence"])

    def test_rejects_revision_mismatch(self):
        result = EvidenceGate().check(
            "G9",
            {
                "input_hash": "same",
                "approved_input_hash": "same",
                "approval_id": "approval-g9",
                "approval_record": {
                    "approval_id": "approval-g9",
                    "action": "G9",
                    "input_hash": "same",
                    "effective_decision": "APPROVE",
                },
                "required_artifacts": [],
                "artifacts": [],
                "repo_revisions": {"business": "r2", "tests": "t2"},
                "evidence_revisions": {"business": "r1", "tests": "t2"},
            },
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason_code"], "REVISION_MISMATCH")

    def test_requires_the_gate_approval_bound_to_the_input_hash(self):
        result = EvidenceGate().check(
            "GRILL",
            {
                "input_hash": "same",
                "approved_input_hash": "same",
                "artifacts": ["requirement-snapshot", "collaboration-session"],
            },
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED")

    def test_every_human_gate_requires_its_approved_ledger_record(self):
        for gate in [f"G{number}" for number in range(11)]:
            with self.subTest(gate=gate):
                result = EvidenceGate().check(
                    gate,
                    {
                        "input_hash": "canonical-hash",
                        "approved_input_hash": "canonical-hash",
                        "approval_id": f"approval-{gate}",
                        "approval_record": {
                            "approval_id": f"approval-{gate}",
                            "action": gate,
                            "input_hash": "canonical-hash",
                            "effective_decision": "APPROVE",
                        },
                    },
                )

                self.assertTrue(result["passed"])

    def test_rejects_forged_or_mismatched_ledger_approval(self):
        for gate in [f"G{number}" for number in range(11)]:
            with self.subTest(gate=gate):
                result = EvidenceGate().check(
                    gate,
                    {
                        "input_hash": "canonical-hash",
                        "approved_input_hash": "canonical-hash",
                        "approval_id": "real-id",
                        "approval_record": {
                            "approval_id": "different-id",
                            "action": gate,
                            "input_hash": "canonical-hash",
                            "effective_decision": "APPROVE",
                        },
                    },
                )

                self.assertFalse(result["passed"])
                self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED")

    def test_rejects_empty_none_partial_and_mismatched_revision_evidence(self):
        base = {
            "input_hash": "same",
            "approved_input_hash": "same",
            "artifacts": ["ipipe-evidence"],
            "approval_id": "release-approval",
            "approval_record": {
                "approval_id": "release-approval",
                "action": "G9",
                "input_hash": "same",
                "effective_decision": "APPROVE",
            },
            "environment_fingerprint": "environment-a",
            "evidence_environment_fingerprint": "environment-a",
            "required_repositories": ["business", "tests"],
        }
        cases = [
            ({}, {}, "MISSING_REVISION_EVIDENCE"),
            (None, None, "MISSING_REVISION_EVIDENCE"),
            ({"business": "r1"}, {"business": "r1"}, "MISSING_REVISION_EVIDENCE"),
            ({"business": "r1", "tests": "t1"}, {"business": "r2", "tests": "t1"}, "REVISION_MISMATCH"),
        ]
        for repo_revisions, evidence_revisions, expected_reason in cases:
            with self.subTest(repo_revisions=repo_revisions, evidence_revisions=evidence_revisions):
                result = EvidenceGate().check(
                    "RELEASE",
                    {
                        **base,
                        "repo_revisions": repo_revisions,
                        "evidence_revisions": evidence_revisions,
                    },
                )

                self.assertFalse(result["passed"])
                self.assertEqual(result["reason_code"], expected_reason)

    def test_malformed_timestamps_return_a_structured_gate_block(self):
        result = EvidenceGate().check(
            "G10",
            {
                "input_hash": "same",
                "approved_input_hash": "same",
                "approval_id": "approval-g10",
                "approval_record": {
                    "approval_id": "approval-g10",
                    "action": "G10",
                    "input_hash": "same",
                    "effective_decision": "APPROVE",
                },
                "verification_created_at": "not-an-iso-timestamp",
                "checkpoint_started_at": "2026-08-10T00:00:00+00:00",
            },
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason_code"], "INVALID_TIMESTAMP")


class ApprovalLedgerTests(unittest.TestCase):
    def test_rejects_unrequested_or_unsupported_approval_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            request = ledger.request("G4", "hash-a", ["comate", "infoflow"])

            with self.assertRaisesRegex(ValueError, "APPROVAL_CHANNEL_NOT_CONFIGURED"):
                ledger.resolve(request["approval_id"], "APPROVE", "hash-a", "other")
            with self.assertRaisesRegex(ValueError, "APPROVAL_CHANNEL_NOT_CONFIGURED"):
                ledger.request("G5", "hash-b", ["other"])
            with self.assertRaisesRegex(ValueError, "APPROVAL_CHANNEL_NOT_CONFIGURED"):
                ledger.request("G6", "hash-c", ["comate"])

    def test_first_valid_decision_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            request = ledger.request("G4", "hash-a", ["comate", "infoflow"])
            duplicate = ledger.request("G4", "hash-a", ["comate", "infoflow"])

            self.assertEqual(request["approval_id"], duplicate["approval_id"])
            accepted = ledger.resolve(request["approval_id"], "APPROVE", "hash-a", "comate")
            conflict = ledger.resolve(request["approval_id"], "REJECT", "hash-a", "infoflow")

            self.assertEqual(accepted["effective_decision"], "APPROVE")
            self.assertEqual(conflict["effective_decision"], "APPROVE")
            self.assertTrue(conflict["conflict"])

    def test_changed_input_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            request = ledger.request("G5", "hash-a", ["comate", "infoflow"])

            with self.assertRaisesRegex(ValueError, "APPROVAL_INPUT_MISMATCH"):
                ledger.resolve(request["approval_id"], "APPROVE", "hash-b", "comate")

    def test_later_valid_responses_are_audited_without_changing_the_first_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "approvals.sqlite"
            ledger = ApprovalLedger(database)
            request = ledger.request("G5", "hash-a", ["comate", "infoflow"])

            ledger.resolve(request["approval_id"], "APPROVE", "hash-a", "comate")
            late = ApprovalLedger(database).resolve(
                request["approval_id"],
                "REJECT",
                "hash-a",
                "infoflow",
            )

            self.assertEqual(late["effective_decision"], "APPROVE")
            self.assertTrue(late["conflict"])
            self.assertEqual(
                [
                    (response["channel"], response["decision"], response["effective"])
                    for response in ApprovalLedger(database).responses(request["approval_id"])
                ],
                [
                    ("comate", "APPROVE", True),
                    ("infoflow", "REJECT", False),
                ],
            )

    def test_invalid_decision_cannot_become_effective_and_is_audited_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            request = ledger.request("G5", "hash-a", ["comate", "infoflow"])

            with self.assertRaisesRegex(ValueError, "APPROVAL_DECISION_INVALID"):
                ledger.resolve(request["approval_id"], "MAYBE", "hash-a", "comate")

            self.assertEqual(ledger.get(request["approval_id"])["status"], "PENDING")
            response = ledger.responses(request["approval_id"])[0]

        self.assertFalse(response["valid"])
        self.assertEqual(response["rejected_reason"], "APPROVAL_DECISION_INVALID")

    def test_concurrent_duplicate_requests_share_one_approval_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "approvals.sqlite"
            worker_count = 12
            barrier = threading.Barrier(worker_count)
            ledgers = [ApprovalLedger(database) for _ in range(worker_count)]

            def request_approval(index):
                barrier.wait()
                return ledgers[index].request(
                    "G5", "hash-concurrent", ["comate", "infoflow"]
                )

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                results = list(executor.map(request_approval, range(worker_count)))

            self.assertEqual(len({result["approval_id"] for result in results}), 1)
            self.assertTrue(all(result["status"] == "PENDING" for result in results))


def _git_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
