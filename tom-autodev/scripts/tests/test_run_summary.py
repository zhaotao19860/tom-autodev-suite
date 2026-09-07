import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_ledger import ApprovalLedger
from artifact_store import ArtifactStore
from run_summary import RunSummary, _has_secret, _redact
from schema_validator import validate_named_schema
from state_store import StateStore


def canonical(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


class KnowledgeFake:
    def __init__(self):
        self.published = []

    def publish_phase(self, run_id, artifact):
        self.published.append((run_id, artifact))
        return {
            "schema_version": "1",
            "ok": True, "reason_code": "OK", "run_id": run_id,
            "artifact_hash": artifact["content_hash"], "child_doc_id": "ku-summary",
            "child_url": "https://ku.baidu-int.com/knowledge/ku-summary",
            "child_version": "v1", "index_doc_id": "root", "index_version": "v1",
            "comment_id": "comment-1",
            "evidence_refs": ["ku:ku-summary/v1", "ku:root/v1", "icafe:CARD-1/comment-1"],
        }


class FailingKnowledgeFake(KnowledgeFake):
    def publish_phase(self, _run_id, _artifact):
        return {"ok": False, "reason_code": "KU_WRITE_FAILED"}


class BareKnowledgeFake(KnowledgeFake):
    def publish_phase(self, _run_id, _artifact):
        return {"ok": True}


class ResultFailingKnowledgeFake(KnowledgeFake):
    def __init__(self):
        super().__init__()
        self.fail_results = True

    def publish_phase(self, run_id, artifact):
        if self.fail_results and artifact["title"] == "G10 Optimization Result":
            return {"ok": False, "reason_code": "KU_WRITE_FAILED"}
        return super().publish_phase(run_id, artifact)


class RunSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = StateStore(self.root / "state.sqlite")
        self.artifacts = ArtifactStore(self.root / "artifacts")
        self.ledger = ApprovalLedger(self.root / "approvals.sqlite")
        self.knowledge = KnowledgeFake()
        self.summary = RunSummary(
            self.state, self.artifacts, self.ledger, knowledge_sync=self.knowledge,
            control_root=self.root,
            validation_runner=lambda _commands, _root: {"ok": True, "reason_code": "OK"},
        )

    def _approve(self, proposal):
        request = self.ledger.request(
            "G10", proposal["candidate_hash"], ["comate", "infoflow"],
            run_id="run-1",
            member_policy={
                "comate": ["owner@example.test"],
                "infoflow": ["owner@example.test"],
            },
        )
        for channel in ("comate", "infoflow"):
            self.ledger.record_delivery(
                request["approval_id"], channel, {"request_id": channel},
                payload_hash=proposal["candidate_hash"],
            )
        self.ledger.resolve(
            request["approval_id"], "APPROVE", proposal["candidate_hash"], "comate",
            run_id="run-1", responder="owner@example.test", state_store=self.state,
        )
        return request["approval_id"]

    def _candidate(self, target, content="after", commands=None):
        return {
            "schema_version": "1",
            "root_cause": "review rule needs a narrower guard",
            "expected_benefit": "prevents the repeated false positive",
            "risk": "small control-plane-only edit",
            "rollback": "restore the prior bytes",
            "target_files": [{"path": str(target), "content": content}],
            "verification_commands": commands or ["python3 -m unittest"],
        }

    def _record_candidate(self, candidate):
        self.state.transition("run-1", "DIAGNOSE", {
            "reason_code": "REVIEW_FAILURE", "failure_signature": "review:rule-1",
            "optimization_candidate": candidate,
        })

    def test_build_groups_failures_and_redacts_secrets_and_personal_data(self):
        self.state.transition("run-1", "INTAKE", {"requirement_id": "CARD-1"})
        self.state.transition("run-1", "DIAGNOSE", {
            "reason_code": "TEST_FAILURE", "message": "token=keep-secret owner@example.test",
            "failure_signature": "unit:flaky-case",
        })
        request = self.ledger.request(
            "G8", "input", ["comate", "infoflow"], run_id="run-1",
            member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
        )
        built = self.summary.build("run-1")

        self.assertEqual(built["outcome"], "FAILED")
        self.assertEqual(built["metrics"]["event_count"], 2)
        self.assertEqual(built["failure_groups"][0]["signature"], "unit:flaky-case")
        self.assertNotIn("keep-secret", json.dumps(built))
        self.assertNotIn("owner@example.test", json.dumps(built))
        self.assertEqual(built["approval_metrics"]["pending"], 1)
        self.assertTrue(self.artifacts.get(built["artifact_id"])["valid"])

    def test_a_verification_failure_left_open_is_reported_not_hidden(self):
        """A read-back failure writes no receipt, so only the open intent shows it."""
        self.state.transition("run-1", "INTAKE", {"requirement_id": "CARD-1"})
        self.state.intent("run-1", "knowledge.publish-phase", "knowledge-sync:abc", {})
        built = self.summary.build("run-1")

        self.assertEqual(built["metrics"]["unreconciled_intent_count"], 1)
        self.assertEqual(
            [group["reason_code"] for group in built["failure_groups"]],
            ["EXTERNAL_INTENT_UNRECONCILED"],
        )
        self.assertEqual(built["outcome"], "FAILED")

    def test_an_abandoned_intent_counts_as_a_failure_not_as_a_verified_write(self):
        """`abandon-intent` unblocks a run; it must not also launder the write.

        The receipt exists so `pending_intents` stops holding the run, which means the
        naive read -- every receipt is a completed external write -- would show G10 a
        collaboration notice that reached somebody and a pipeline that was polled. What
        actually happened is that a human stopped waiting, and the outcome is unknown.
        """
        self.state.transition("run-1", "IPIPE", {"pipeline_id": "pipe-1"})
        notice = self.state.intent("run-1", "collaboration.notify", "notify:1", {})
        poll = self.state.intent("run-1", "ipipe.poll", "ipipe:1", {})
        self.state.abandon_intent(notice["intent_id"], "机器人下线", "owner")
        self.state.abandon_intent(poll["intent_id"], "机器人下线", "owner")

        built = self.summary.build("run-1")

        self.assertEqual(built["metrics"]["abandoned_intent_count"], 2)
        self.assertEqual(built["metrics"]["external_receipt_count"], 0)
        self.assertEqual(built["metrics"]["unreconciled_intent_count"], 0)
        self.assertEqual(built["collaboration_receipt_count"], 0)
        self.assertEqual(built["pipeline_evidence_count"], 0)
        self.assertEqual(
            {group["reason_code"] for group in built["failure_groups"]}, {"INTENT_ABANDONED"}
        )
        self.assertEqual(built["outcome"], "FAILED")

    def test_build_marks_timeout_and_collects_collaboration_and_pipeline_receipts(self):
        self.state.transition("run-1", "IPIPE", {"pipeline_id": "pipe-1"})
        intent = self.state.intent("run-1", "collaboration.create-group", "group:1", {})
        self.state.receipt(intent["intent_id"], {"ok": True, "group_id": "group-1"}, ["group-1"])
        intent = self.state.intent("run-1", "ipipe.poll", "ipipe:1", {})
        self.state.receipt(intent["intent_id"], {"ok": False, "reason_code": "REMOTE_TIMEOUT"}, ["ipipe:build-1/job-1"])
        request = self.ledger.request(
            "G8", "input", ["comate", "infoflow"], run_id="run-1",
            member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
        )
        self.ledger.timeout(
            request["approval_id"], "input", run_id="run-1", state_store=self.state,
            now=__import__("datetime").datetime.max.replace(tzinfo=__import__("datetime").timezone.utc),
        )
        built = self.summary.build("run-1")

        self.assertEqual(built["outcome"], "TIMEOUT")
        self.assertEqual(built["collaboration_receipt_count"], 1)
        self.assertEqual(built["pipeline_evidence_count"], 1)

    def test_release_success_is_stable_after_a_repaired_failure(self):
        self.state.transition("run-1", "DIAGNOSE", {"reason_code": "REVIEW_FAILED", "evidence": {"failure_signature": "review:one"}})
        self.state.transition("run-1", "RELEASE_SUCCESS", {"reason_code": "OK"})
        first = self.summary.build("run-1")
        second = self.summary.build("run-1")

        self.assertEqual(first["outcome"], "SUCCESS")
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(first["artifact_id"], second["artifact_id"])

    def test_propose_requires_knowledge_sync_and_verified_summary_artifact(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir(); target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        built = self.summary.build("run-1")
        without_sync = RunSummary(self.state, self.artifacts, self.ledger, control_root=self.root)

        self.assertEqual(without_sync.propose(built, [self.root])["reason_code"], "KNOWLEDGE_SYNC_REQUIRED")
        self.assertEqual(self.summary.propose({"ok": True, "run_id": "run-1", "content_hash": "forged"}, [self.root])["reason_code"], "RUN_SUMMARY_ARTIFACT_REQUIRED")

    def test_nested_versioned_evidence_produces_a_proposal(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir(); target.write_text("before", encoding="utf-8")
        self.state.transition("run-1", "DIAGNOSE", {"reason_code": "REVIEW_FAILED", "evidence": {"failure_signature": "review:nested", "optimization_candidate": self._candidate(target)}})

        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        self.assertEqual(proposal["reason_code"], "OK")
        self.assertEqual(proposal["evidence"]["failure_groups"][0]["signature"], "review:nested")

    def test_the_schemas_describe_the_documents_this_module_really_writes(self):
        """Drift between these two schema files and their builders is a test failure now.

        Both described documents no code has ever produced: `run-summary.schema.json`
        required a `result` and `phase_timings`, and `optimization-proposal.schema.json`
        wanted a flat `target_files` of relative path strings. Nothing loaded either, so
        being wrong was free. They are loaded now -- the summary through
        `ArtifactStore.put`, the proposal in `propose` before the immutable row is
        written -- and what is validated here is what durably exists: the archived
        bytes, and the persisted proposal G10 later reads back.
        """
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir(); target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))

        built = self.summary.build("run-1")
        archived = json.loads(self.artifacts.get(built["artifact_id"])["content"])
        proposal = self.summary.propose(built, [self.root])
        persisted = self.state.optimization_proposal(proposal["proposal_id"])["proposal"]

        self.assertEqual(validate_named_schema(archived, "run-summary"), [])
        self.assertEqual(validate_named_schema(persisted, "optimization-proposal"), [])

    def test_propose_rejects_a_symlink_allowed_root(self):
        real = self.root / "scripts"; real.mkdir()
        target = real / "guard.py"; target.write_text("before", encoding="utf-8")
        linked_root = self.root / "linked"; linked_root.symlink_to(real, target_is_directory=True)
        self._record_candidate(self._candidate(target))

        self.assertEqual(self.summary.propose(self.summary.build("run-1"), [linked_root])["reason_code"], "G10_ALLOWED_ROOT_INVALID")

    def test_propose_rejects_business_ipipe_and_profile_mutations(self):
        for name in ("business.cpp", "ipipe.yml", "pipeline-profile.yaml"):
            target = self.root / name
            target.write_text("before", encoding="utf-8")
            self._record_candidate(self._candidate(target))
            built = self.summary.build("run-1")
            proposal = self.summary.propose(built, [self.root])
            self.assertEqual(proposal["reason_code"], "G10_TARGET_FORBIDDEN")

    def test_propose_rejects_project_test_and_shell_validation_commands(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        for command in (
            "python3 -m unittest discover -s ../bgw/tests",
            "make test",
            "python3 -m unittest; docker build .",
        ):
            with self.subTest(command=command):
                self._record_candidate(self._candidate(target, commands=[command]))
                proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
                self.assertEqual(proposal["reason_code"], "G10_VALIDATION_FORBIDDEN")

    def test_propose_rejects_a_symlink_even_when_its_resolved_target_is_allowed(self):
        target = self.root / "scripts" / "real.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        link = self.root / "scripts" / "link.py"
        link.symlink_to(target)
        self._record_candidate(self._candidate(link))

        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        self.assertEqual(proposal["reason_code"], "G10_TARGET_SYMLINK_FORBIDDEN")

    def test_proposal_is_hashed_bound_and_archived_through_knowledge_boundary(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])

        self.assertEqual(proposal["candidate_hash"], canonical(proposal["candidate"]))
        self.assertTrue(proposal["candidate_diff"])
        self.assertEqual(len(self.knowledge.published), 1)
        self.assertEqual(self.state.optimization_proposal(proposal["proposal_id"])["candidate_hash"], proposal["candidate_hash"])

    def test_proposal_archive_failure_is_not_applyable(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        failing = RunSummary(
            self.state, self.artifacts, self.ledger, knowledge_sync=FailingKnowledgeFake(),
            control_root=self.root,
        )

        proposal = failing.propose(self.summary.build("run-1"), [self.root])
        self.assertEqual(proposal["reason_code"], "G10_ARCHIVE_FAILED")
        request = self.ledger.request(
            "G10", proposal["candidate_hash"], ["comate", "infoflow"], run_id="run-1",
            member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
        )
        self.assertEqual(failing.apply(proposal["proposal_id"], request["approval_id"])["reason_code"], "G10_ARCHIVE_FAILED")

    def test_tampered_persisted_candidate_is_not_applied(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        approval_id = self._approve(proposal)
        with sqlite3.connect(self.state.database_path) as connection:
            row = connection.execute("SELECT payload_json FROM optimization_proposals WHERE proposal_id = ?", (proposal["proposal_id"],)).fetchone()
            tampered = json.loads(row[0])
            tampered["candidate"]["target_files"][0]["content"] = "tampered"
            connection.execute("UPDATE optimization_proposals SET payload_json = ? WHERE proposal_id = ?", (json.dumps(tampered, sort_keys=True, separators=(",", ":")), proposal["proposal_id"]))

        result = self.summary.apply(proposal["proposal_id"], approval_id)
        self.assertEqual(result["reason_code"], "G10_PROPOSAL_TAMPERED")
        self.assertEqual(target.read_text(encoding="utf-8"), "before")

    def test_apply_requires_g10_approval_bound_to_the_candidate_hash(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        wrong = self.ledger.request(
            "G10", "wrong", ["comate", "infoflow"], run_id="run-1",
            member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
        )

        result = self.summary.apply(proposal["proposal_id"], wrong["approval_id"])
        self.assertEqual(result["reason_code"], "G10_APPROVAL_HASH_MISMATCH")
        self.assertEqual(target.read_text(encoding="utf-8"), "before")

    def test_rejected_g10_never_applies_candidate(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        request = self.ledger.request(
            "G10", proposal["candidate_hash"], ["comate", "infoflow"], run_id="run-1",
            member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
        )
        self.ledger.record_delivery(request["approval_id"], "comate", {"request_id": "comate"}, payload_hash=proposal["candidate_hash"])
        self.ledger.record_delivery(request["approval_id"], "infoflow", {"request_id": "infoflow"}, payload_hash=proposal["candidate_hash"])
        self.ledger.resolve(request["approval_id"], "REJECT", proposal["candidate_hash"], "comate", run_id="run-1", responder="owner@example.test", state_store=self.state)

        result = self.summary.apply(proposal["proposal_id"], request["approval_id"])
        self.assertEqual(result["reason_code"], "G10_REJECTED")
        self.assertEqual(target.read_text(encoding="utf-8"), "before")
        self.assertEqual(self.summary.apply(proposal["proposal_id"], request["approval_id"])["reason_code"], "G10_REJECTED")
        self.assertEqual(self.summary.apply(proposal["proposal_id"], "wrong-approval")["reason_code"], "G10_REPLAY_AUTH_REQUIRED")

    def test_malformed_versioned_candidate_and_quoted_secret_fail_closed(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir(); target.write_text("before", encoding="utf-8")
        candidate = self._candidate(target)
        candidate["root_cause"] = 123
        self._record_candidate(candidate)
        self.assertEqual(self.summary.propose(self.summary.build("run-1"), [self.root])["reason_code"], "G10_CANDIDATE_INVALID")
        text = '{"Authorization": "Bearer actual-secret", "api_key": "second-secret"}'
        self.assertTrue(_has_secret(text))
        self.assertNotIn("actual-secret", _redact(text))
        self.assertNotIn("second-secret", _redact(text))

    def _applying_proposal(self):
        target = self.root / "scripts" / f"lease-{len(self.state.events('run-1'))}.py"
        target.parent.mkdir(exist_ok=True); target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        approval_id = self._approve(proposal)
        candidate = proposal["candidate"]
        journal, _ = self.summary._journal(candidate, [self.root], proposal=proposal, approval_id=approval_id)
        claim = self.state.claim_optimization_apply(proposal["proposal_id"], approval_id, journal)
        return proposal, approval_id, claim

    def test_lease_heartbeat_extends_and_wrong_token_is_rejected(self):
        proposal, _, claim = self._applying_proposal()
        before = claim["proposal"]["lease_expires_at"]
        self.assertEqual(self.summary.heartbeat(proposal["proposal_id"], claim["owner_token"])["status"], "APPLYING")
        self.assertEqual(self.summary.heartbeat(proposal["proposal_id"], "wrong")["reason_code"], "OPTIMIZATION_OWNER_MISMATCH")
        self.assertGreater(self.state.optimization_proposal(proposal["proposal_id"])["lease_expires_at"], before)

    def test_expired_dead_recovers_but_live_or_fresh_waits(self):
        for expiry, live, expected in ((-1, False, "G10_INTERRUPTED_ROLLED_BACK"), (-1, True, "G10_APPLY_IN_PROGRESS"), (300, False, "G10_APPLY_IN_PROGRESS")):
            with self.subTest(expiry=expiry, live=live):
                proposal, approval_id, claim = self._applying_proposal()
                with sqlite3.connect(self.state.database_path) as connection:
                    connection.execute("UPDATE optimization_proposals SET lease_expires_at = ? WHERE proposal_id = ?", ((datetime.now(timezone.utc)+timedelta(seconds=expiry)).isoformat(), proposal["proposal_id"]))
                with patch("run_summary._pid_live", return_value=live):
                    result = self.summary.apply(proposal["proposal_id"], approval_id)
                self.assertEqual(result["reason_code"], expected)

    def test_ambiguous_liveness_fails_closed(self):
        proposal, approval_id, _ = self._applying_proposal()
        with sqlite3.connect(self.state.database_path) as connection:
            connection.execute("UPDATE optimization_proposals SET lease_expires_at = ? WHERE proposal_id = ?", ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), proposal["proposal_id"]))
        with patch("run_summary._pid_live", return_value=True):
            self.assertEqual(self.summary.apply(proposal["proposal_id"], approval_id)["reason_code"], "G10_APPLY_IN_PROGRESS")

    def test_validation_failure_rolls_back_every_file(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        approval_id = self._approve(proposal)
        failing = RunSummary(
            self.state, self.artifacts, self.ledger, knowledge_sync=self.knowledge,
            control_root=self.root, validation_runner=lambda _commands, _root: {"ok": False, "reason_code": "VALIDATION_FAILED"},
        )

        result = failing.apply(proposal["proposal_id"], approval_id)
        self.assertEqual(result["reason_code"], "G10_VALIDATION_FAILED_ROLLED_BACK")
        self.assertEqual(target.read_text(encoding="utf-8"), "before")
        self.assertEqual(self.state.optimization_proposal(proposal["proposal_id"])["status"], "ROLLED_BACK")

    def test_partial_write_failure_rolls_back_files_already_written(self):
        first = self.root / "scripts" / "first.py"
        second = self.root / "scripts" / "second.py"
        first.parent.mkdir()
        first.write_text("before-first", encoding="utf-8")
        second.write_text("before-second", encoding="utf-8")
        candidate = self._candidate(first, "after-first")
        candidate["target_files"].append({"path": str(second), "content": "after-second"})
        self._record_candidate(candidate)
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        approval_id = self._approve(proposal)
        import run_summary
        real_write = run_summary._write_pinned
        calls = []

        def fail_second_write(pin, entry, data):
            calls.append(entry["path"])
            if len(calls) == 2:
                raise OSError("disk full")
            real_write(pin, entry, data)

        with patch("run_summary._write_pinned", side_effect=fail_second_write):
            result = self.summary.apply(proposal["proposal_id"], approval_id)
        self.assertEqual(result["reason_code"], "G10_APPLY_FAILED_ROLLED_BACK")
        self.assertEqual(first.read_text(encoding="utf-8"), "before-first")
        self.assertEqual(second.read_text(encoding="utf-8"), "before-second")

    def test_archive_failure_after_apply_rolls_back_and_never_marks_applied(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        approval_id = self._approve(proposal)
        failing = RunSummary(
            self.state, self.artifacts, self.ledger, knowledge_sync=FailingKnowledgeFake(),
            control_root=self.root, validation_runner=lambda _commands, _root: {"ok": True},
        )

        result = failing.apply(proposal["proposal_id"], approval_id)
        self.assertEqual(result["reason_code"], "G10_RESULT_ARCHIVE_PENDING")
        self.assertEqual(target.read_text(encoding="utf-8"), "after")
        self.assertEqual(self.state.optimization_proposal(proposal["proposal_id"])["status"], "ARCHIVE_PENDING")

    def test_apply_is_idempotent_after_validation(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        approval_id = self._approve(proposal)

        first = self.summary.apply(proposal["proposal_id"], approval_id)
        second = self.summary.apply(proposal["proposal_id"], approval_id)
        self.assertEqual(first["reason_code"], "OK")
        self.assertEqual(second, first)
        self.assertEqual(target.read_text(encoding="utf-8"), "after")

    def test_bare_or_wrong_receipt_never_makes_proposal_applyable(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir(); target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        bare = RunSummary(self.state, self.artifacts, self.ledger, knowledge_sync=BareKnowledgeFake(), control_root=self.root)
        proposal = bare.propose(bare.build("run-1"), [self.root])
        self.assertEqual(proposal["reason_code"], "G10_ARCHIVE_FAILED")
        self.assertEqual(self.state.optimization_proposal(proposal["proposal_id"])["status"], "ARCHIVE_FAILED")

    def test_receipt_identity_and_canonical_evidence_binding_are_required(self):
        for field, value in (("run_id", "other-run"), ("artifact_hash", "wrong"), ("child_doc_id", ""), ("evidence_refs", [])):
            with self.subTest(field=field):
                target = self.root / "scripts" / f"receipt-{field}.py"
                target.parent.mkdir(exist_ok=True); target.write_text("before", encoding="utf-8")
                self._record_candidate(self._candidate(target))

                class WrongReceipt(KnowledgeFake):
                    def publish_phase(self, run_id, artifact):
                        receipt = super().publish_phase(run_id, artifact)
                        receipt[field] = value
                        return receipt

                summary = RunSummary(self.state, self.artifacts, self.ledger, knowledge_sync=WrongReceipt(), control_root=self.root)
                self.assertEqual(summary.propose(summary.build("run-1"), [self.root])["reason_code"], "G10_ARCHIVE_FAILED")

    def test_result_archive_is_durable_pending_and_recovers_idempotently(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir(); target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        approval_id = self._approve(proposal)
        flaky_knowledge = ResultFailingKnowledgeFake()
        flaky = RunSummary(self.state, self.artifacts, self.ledger, knowledge_sync=flaky_knowledge, control_root=self.root, validation_runner=lambda _c, _r: {"ok": True})
        self.assertEqual(flaky.apply(proposal["proposal_id"], approval_id)["reason_code"], "G10_RESULT_ARCHIVE_PENDING")
        self.assertEqual(self.state.optimization_proposal(proposal["proposal_id"])["status"], "ARCHIVE_PENDING")
        flaky_knowledge.fail_results = False
        recovered = flaky.apply(proposal["proposal_id"], approval_id)
        stored = self.state.optimization_proposal(proposal["proposal_id"])
        self.assertEqual(recovered["reason_code"], "OK")
        self.assertEqual(stored["status"], "APPLIED")
        self.assertIsInstance(stored["result_receipt"], dict)

    def test_rollback_failure_stays_recoverable_until_a_verified_retry(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir(); target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        approval_id = self._approve(proposal)
        failing = RunSummary(self.state, self.artifacts, self.ledger, knowledge_sync=self.knowledge, control_root=self.root, validation_runner=lambda _c, _r: {"ok": False})
        with patch.object(failing, "_rollback_journal", return_value=False):
            result = failing.apply(proposal["proposal_id"], approval_id)
        self.assertEqual(result["reason_code"], "G10_ROLLBACK_RECOVERY_REQUIRED")
        self.assertEqual(self.state.optimization_proposal(proposal["proposal_id"])["status"], "RECOVERY_REQUIRED")
        self.assertEqual(target.read_text(encoding="utf-8"), "after")
        recovered = self.summary.apply(proposal["proposal_id"], approval_id)
        self.assertEqual(recovered["reason_code"], "G10_ROLLBACK_RECOVERED")
        self.assertEqual(self.state.optimization_proposal(proposal["proposal_id"])["status"], "ROLLED_BACK")
        self.assertEqual(target.read_text(encoding="utf-8"), "before")

    def test_real_parent_replacement_is_recoverable_not_a_rebound_write(self):
        target = self.root / "scripts" / "guard.py"
        target.parent.mkdir(); target.write_text("before", encoding="utf-8")
        self._record_candidate(self._candidate(target))
        proposal = self.summary.propose(self.summary.build("run-1"), [self.root])
        approval_id = self._approve(proposal)
        import run_summary
        real_write = run_summary._write_pinned

        def replace_parent(pin, entry, data):
            scripts = self.root / "scripts"
            scripts.rename(self.root / "scripts-original")
            scripts.mkdir(); (scripts / "guard.py").write_text("replacement", encoding="utf-8")
            return real_write(pin, entry, data)

        with patch("run_summary._write_pinned", side_effect=replace_parent):
            result = self.summary.apply(proposal["proposal_id"], approval_id)
        self.assertEqual(result["reason_code"], "G10_ROLLBACK_RECOVERY_REQUIRED")
        self.assertEqual(self.state.optimization_proposal(proposal["proposal_id"])["status"], "RECOVERY_REQUIRED")
        self.assertEqual((self.root / "scripts" / "guard.py").read_text(encoding="utf-8"), "replacement")
        self.assertEqual((self.root / "scripts-original" / "guard.py").read_text(encoding="utf-8"), "before")

    def test_multiline_quoted_header_and_url_secrets_are_fully_redacted_and_rejected(self):
        text = r'{"password":"hello world \\"quoted\\"", "Authorization":"Bearer multi word", "url":"https://alice:wide secret@example.test/x?api_key=one%20two"}'
        redacted = _redact(text)
        for secret in ("hello world", "multi word", "wide secret", "one%20two"):
            self.assertNotIn(secret, redacted)
        self.assertTrue(_has_secret(text))

    def test_recomputed_cross_target_journal_is_not_recovered(self):
        proposal, approval_id, _ = self._applying_proposal()
        unrelated = self.root / "scripts" / "unrelated.py"; unrelated.write_text("safe", encoding="utf-8")
        stored = self.state.optimization_proposal(proposal["proposal_id"])
        journal = stored["apply_journal"]
        journal["entries"][0]["path"] = str(unrelated)
        journal["entries"][0]["bytes_b64"] = "YXR0YWNrZXI="
        journal["entries"][0]["sha256"] = hashlib.sha256(b"attacker").hexdigest()
        journal["journal_hash"] = canonical({key: value for key, value in journal.items() if key != "journal_hash"})
        with sqlite3.connect(self.state.database_path) as connection:
            connection.execute("UPDATE optimization_proposals SET apply_journal_json=?, lease_expires_at=? WHERE proposal_id=?", (json.dumps(journal, sort_keys=True, separators=(",", ":")), (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), proposal["proposal_id"]))
        with patch("run_summary._pid_live", return_value=False):
            self.assertEqual(self.summary.apply(proposal["proposal_id"], approval_id)["reason_code"], "G10_RECOVERY_REQUIRED")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "safe")


if __name__ == "__main__":
    unittest.main()
