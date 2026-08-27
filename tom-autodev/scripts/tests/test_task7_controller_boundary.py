import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import Orchestrator, main
from test_schema_validation import specialized_examples


def canonical_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def profile_fixture():
    return {
        "project_id": "bgw",
        "business_repos": [{
            "path": "/repo/business", "module": "resolver", "branch": "main",
            "lock": "business-main",
        }],
        "test_repo": {
            "path": "/repo/tests", "module": "resolver-tests", "branch": "main",
            "lock": "tests-main",
        },
        "language_skill": "/skills/language",
        "project_skill": "/skills/project",
        "knowledge_sources": [{
            "provider": "ku", "repository": "knowledge", "revision": "r1",
            "search_scope": "project", "priority": 1, "repo_id": "repo-1",
            "parent_doc_id": "parent-1",
        }],
        "review_provider": {"kind": "source-only", "command": "review"},
        "pipeline_profile": {
            "pipeline_id": "pipe-1", "allowed_parameters": [],
            "stage_classes": ["unit"], "release_rule": "all stages pass",
        },
        "environment_profile": {
            "runner": "linux", "os_arch": "linux/amd64", "image_digest": "sha256:env",
            "toolchain_digest": "sha256:tools", "hardware_or_simulator": "remote",
            "data": "fixture", "services": "none", "capacity": "small",
        },
        "approval_channels": {
            "comate": {"channel": "comate-review"},
            "infoflow": {"channel": "group-1"},
            "role_members": {
                "development": ["dev@example.test"],
                "test": ["test@example.test"],
                "project": ["owner@example.test"],
            },
        },
    }


def snapshot_fixture():
    snapshot = copy.deepcopy(specialized_examples()["requirement-snapshot"])
    snapshot["responsible_people"] = [{"email": "owner@example.test"}]
    snapshot["content_hash"] = canonical_hash({
        key: value for key, value in snapshot.items() if key != "content_hash"
    })
    return snapshot


class FakeIcodeRuntime:
    def __init__(self, state, run_id):
        self.state = state
        self.run_id = run_id

    def submit(self, change_set, approval):
        repo_path = str(Path(change_set["repo_path"]).expanduser().resolve())
        payload = {
            "run_id": self.run_id,
            "change_set_id": change_set["change_set_id"],
            "revision_set_id": change_set["revision_set_id"],
            "input_hash": change_set["input_hash"],
            "approval_id": approval["approval_id"],
            "repo_path": repo_path,
            "module": change_set["module"],
            "target_branch": change_set["target_branch"],
            "commit_revision": change_set["commit_revision"],
            "card_id": change_set["card_id"],
            "owner": change_set["owner"],
            "revision_set": copy.deepcopy(change_set["revision_set"]),
        }
        key = (
            f"icode.submit:{self.run_id}:{change_set['change_set_id']}:"
            f"{change_set['revision_set_id']}"
        )
        claim = self.state.claim_intent(self.run_id, "icode.submit", key, payload)
        response = {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "change_set_id": change_set["change_set_id"],
            "revision_set_id": change_set["revision_set_id"],
            "repo_path": repo_path,
            "module": change_set["module"],
            "target_branch": change_set["target_branch"],
            "commit_revision": change_set["commit_revision"],
            "revision_set": copy.deepcopy(change_set["revision_set"]),
            "change_number": "42",
            "patchset": change_set["commit_revision"],
            "cr_url": "https://icode.example/cr/42",
            "evidence_refs": ["icode-cr-42", "revision-r2", "revision-t2"],
        }
        self.state.receipt(claim["intent"]["intent_id"], response, response["evidence_refs"])
        return response


class RaisingIcodeRuntime:
    def __init__(self, run_id):
        self.run_id = run_id

    def submit(self, _change_set, _approval):
        raise RuntimeError("untrusted runtime detail")


class CafeFake:
    def __init__(self, result):
        self.result = copy.deepcopy(result)

    def snapshot(self, _requirement_id):
        return copy.deepcopy(self.result)


class KnowledgeFake:
    def __init__(self):
        self.calls = []

    def publish_phase(self, run_id, artifact):
        self.calls.append((run_id, copy.deepcopy(artifact)))
        return {
            "ok": True,
            "reason_code": "OK",
            "run_id": run_id,
            "artifact_hash": artifact["content_hash"],
            "child_doc_id": "ku-ipipe-42",
            "child_url": "https://ku.baidu-int.com/knowledge/ku-ipipe-42",
            "child_version": "v7",
            "index_doc_id": "run-root",
            "index_version": "v3",
            "comment_id": "comment-9",
            "evidence_refs": [
                "ku:ku-ipipe-42/v7", "ku:run-root/v3",
                "icafe:BGW-1/comment-9",
            ],
        }


class Task7ProductionControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = profile_fixture()
        profile_path = self.root / "config" / "projects" / "bgw.yaml"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(yaml.safe_dump(self.profile), encoding="utf-8")
        self.profile_patch = patch(
            "orchestrator.load_profile",
            return_value={"ready": True, "reason_code": "READY", "profile": self.profile},
        )
        self.profile_patch.start()
        self.addCleanup(self.profile_patch.stop)
        self.orchestrator = Orchestrator(self.root)
        started = self.orchestrator.start(
            "BGW-1", "bgw", requirement_snapshot=snapshot_fixture()
        )
        self.run_id = started["run_id"]
        self.change_set = {
            "run_id": self.run_id,
            "change_set_id": "change-1",
            "revision_set_id": "revisions-1",
            "repo_path": str(
                self.root / "worktrees" / "task-1" / ".." / "task-1" / "business"
            ),
            "module": "resolver",
            "target_branch": "main",
            "commit_revision": "r2",
            "card_id": "BGW-1",
            "owner": "dev",
            "revision_set": {
                "business": {"module": "resolver", "revision": "r2", "branch": "main"},
                "test": {"module": "resolver-tests", "revision": "t2", "branch": "main"},
            },
        }
        self.change_set["input_hash"] = canonical_hash(self.change_set)
        self.approval = self._advance_to_submit(self.change_set["input_hash"])
        self.knowledge = KnowledgeFake()
        self.protocol = self.orchestrator.phase_protocol(self.knowledge)

    def _approve(self, gate, input_hash):
        policy = {
            "comate": ["owner@example.test"],
            "infoflow": ["owner@example.test"],
        }
        approval = self.orchestrator.approvals.request(
            gate, input_hash, ["comate", "infoflow"],
            run_id=self.run_id, member_policy=policy,
        )
        for channel in ("comate", "infoflow"):
            self.orchestrator.approvals.record_delivery(
                approval["approval_id"], channel,
                {"request_id": f"{channel}-{approval['approval_id']}"},
                payload_hash=input_hash,
            )
        self.orchestrator.approvals.resolve(
            approval["approval_id"], "APPROVE", input_hash, "comate",
            run_id=self.run_id, responder="owner@example.test",
            state_store=self.orchestrator.state,
        )
        return {"approval_id": approval["approval_id"], "input_hash": input_hash}

    def _advance_to_submit(self, candidate_hash):
        requirements = (
            ("GRILL", "G0", ["requirement-snapshot", "collaboration-session"]),
            ("SPEC", "G1", ["grill"]),
            ("TASKS", "G2", ["spec"]),
            ("WORKSPACE", "G3", ["task-dag"]),
            ("PLAN", "G4", ["workspace", "task-plan"]),
            ("IMPLEMENT", "G4", ["task-plan"]),
            ("REVIEW", "G5", ["change-set"]),
            ("SUBMIT", "G7", ["review"]),
        )
        final_approval = None
        for target, gate, artifacts in requirements:
            input_hash = candidate_hash if target == "SUBMIT" else canonical_hash({
                "run_id": self.run_id, "target": target,
            })
            approval = self._approve(gate, input_hash)
            result = self.orchestrator.advance(self.run_id, target, {
                "input_hash": input_hash,
                "approval_id": approval["approval_id"],
                "artifacts": artifacts,
            })
            self.assertEqual(result["state"], target, result)
            if target == "SUBMIT":
                final_approval = approval
        return final_approval

    def _bind_submission(self, approval=None, runtime=None):
        return self.orchestrator.submit_to_ipipe(
            self.run_id,
            self.change_set,
            approval or self.approval,
            icode_runtime=runtime or FakeIcodeRuntime(
                self.orchestrator.state, self.run_id
            ),
        )

    def _ipipe_content(self):
        content = copy.deepcopy(specialized_examples()["ipipe-evidence"])
        content["environment_fingerprint"] = canonical_hash(
            self.profile["environment_profile"]
        )
        return content

    def test_submit_controller_creates_owned_artifact_and_top_level_ipipe_binding(self):
        controller = getattr(self.orchestrator, "submit_to_ipipe", None)
        result = (
            controller(
                self.run_id, self.change_set, self.approval,
                icode_runtime=FakeIcodeRuntime(self.orchestrator.state, self.run_id),
            )
            if controller is not None
            else {"reason_code": "SUBMISSION_CONTROLLER_MISSING"}
        )

        self.assertEqual(result.get("state"), "IPIPE", result)
        event = self.orchestrator.state.events(self.run_id)[-1]
        artifact = self.orchestrator.artifacts.get(event["payload"]["submission_artifact_id"])
        self.assertEqual(event["state"], "IPIPE")
        self.assertEqual(event["payload"]["pipeline_id"], "pipe-1")
        self.assertEqual(event["payload"]["module"], "resolver")
        self.assertEqual(event["payload"]["release_rule"], "all stages pass")
        self.assertEqual(event["payload"]["source_revisions"], {
            "business": "r2", "tests": "t2",
        })
        self.assertTrue(artifact["valid"], artifact)
        self.assertEqual(artifact["metadata"]["controller_binding"], {
            "pipeline_id": "pipe-1",
            "module": "resolver",
            "release_rule": "all stages pass",
            "source_revisions": {"business": "r2", "tests": "t2"},
            "environment_fingerprint": canonical_hash(self.profile["environment_profile"]),
        })

    def test_submit_controller_converts_runtime_exception_to_stable_failure(self):
        try:
            result = self.orchestrator.submit_to_ipipe(
                self.run_id,
                self.change_set,
                self.approval,
                icode_runtime=RaisingIcodeRuntime(self.run_id),
            )
        except Exception as error:
            result = {"reason_code": f"ESCAPED_{type(error).__name__}"}

        self.assertEqual(result["reason_code"], "SUBMISSION_BINDING_FAILED")
        self.assertEqual(self.orchestrator.status(self.run_id)["state"], "SUBMIT")

    def test_production_bound_ipipe_ingestion_succeeds_and_propagates_evidence(self):
        bound = self._bind_submission()
        content = self._ipipe_content()
        content["remote_evidence_refs"] = ["artifact:source-1", "ku:run-root/v3"]

        result = self.protocol.ingest_ipipe_evidence(self.run_id, content)
        stored = self.orchestrator.artifacts.phase_artifact(result["artifact_id"])
        next_action = self.protocol.next(self.run_id)

        self.assertEqual(bound["state"], "IPIPE", bound)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "RELEASE")
        self.assertEqual(stored["envelope"]["evidence_refs"], [
            "artifact:source-1", "ku:run-root/v3", "ku:ku-ipipe-42/v7",
            "icafe:BGW-1/comment-9",
        ])
        self.assertEqual(next_action["source_evidence_refs"], stored["envelope"]["evidence_refs"])

    def test_production_bound_ipipe_rejects_each_owned_content_mismatch(self):
        self._bind_submission()
        cases = (
            ("pipeline_id", "other-pipeline", "PIPELINE_IDENTITY_MISMATCH"),
            ("module", "other-module", "PIPELINE_IDENTITY_MISMATCH"),
            ("release_rule", "other-rule", "RELEASE_RULE_MISMATCH"),
            ("revisions", {"business": "other", "tests": "t2"}, "SOURCE_REVISION_MISMATCH"),
            ("environment_fingerprint", "other-environment", "ENV_FINGERPRINT_MISMATCH"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                content = self._ipipe_content()
                content[field] = value
                result = self.protocol.ingest_ipipe_evidence(self.run_id, content)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["reason_code"], expected)
        self.assertEqual(self.knowledge.calls, [])

    def test_production_bound_ipipe_rejects_submission_and_g7_mismatches(self):
        bad_approval = dict(self.approval, input_hash="0" * 64)
        rejected = self._bind_submission(approval=bad_approval)

        self.assertEqual(rejected["reason_code"], "APPROVAL_INPUT_MISMATCH")
        self.assertEqual(self.orchestrator.status(self.run_id)["state"], "SUBMIT")

        bound = self._bind_submission()
        artifact = self.orchestrator.artifacts.get(bound["submission_artifact_id"])
        Path(artifact["path"]).write_bytes(b"corrupt exact submission")
        result = self.protocol.ingest_ipipe_evidence(self.run_id, self._ipipe_content())

        self.assertEqual(bound["state"], "IPIPE", bound)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason_code"], "PREDECESSOR_REQUIRED")

    def test_production_bound_ipipe_rejects_nested_noncanonical_evidence(self):
        self._bind_submission()
        content = self._ipipe_content()
        content["jobs"][0]["evidence_refs"] = [
            "https://logs.example/job-1?token=must-not-persist"
        ]

        result = self.protocol.ingest_ipipe_evidence(self.run_id, content)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason_code"], "SCHEMA_INVALID")
        self.assertIn(
            {"path": "jobs[0].evidence_refs", "kind": "invalid"},
            result["schema_errors"],
        )
        self.assertEqual(self.knowledge.calls, [])
        self.assertNotIn(
            b"must-not-persist", (self.root / "state.sqlite").read_bytes()
        )

    def test_production_bound_ipipe_historical_replay_uses_exact_old_artifact(self):
        self._bind_submission()
        content = self._ipipe_content()
        first = self.protocol.ingest_ipipe_evidence(self.run_id, content)
        original = self.orchestrator.artifacts.phase_artifact(first["artifact_id"])["envelope"]
        newer = copy.deepcopy(original)
        newer["content"]["build_id"] = "build-newer"
        newer["content_hash"] = canonical_hash(newer["content"])
        newer["action_id"] = canonical_hash({
            "run_id": self.run_id, "phase": "IPIPE", "build_id": "build-newer",
        })
        newer["source_event_id"] = "later-ipipe-source-event"
        newer["knowledge_doc_id"] = "ku-ipipe-newer"
        newer["knowledge_url"] = "https://ku.baidu-int.com/knowledge/ku-ipipe-newer"
        newer["knowledge_version"] = "v8"
        newer["evidence_refs"] = [
            "ku:ku-ipipe-newer/v8", "icafe:BGW-1/comment-newer"
        ]
        self.orchestrator.artifacts.put_envelope(newer)

        replay = self.protocol.ingest_ipipe_evidence(self.run_id, copy.deepcopy(content))
        old = self.orchestrator.artifacts.get(first["artifact_id"])
        Path(old["path"]).write_bytes(b"corrupt exact old ipipe artifact")
        corrupt_replay = self.protocol.ingest_ipipe_evidence(
            self.run_id, copy.deepcopy(content)
        )

        self.assertEqual(replay, first)
        self.assertFalse(corrupt_replay["ok"])
        self.assertEqual(corrupt_replay["reason_code"], "ARTIFACT_INTEGRITY_FAILED")

    def test_production_bound_ipipe_replay_rejects_pinned_profile_drift(self):
        self._bind_submission()
        content = self._ipipe_content()
        first = self.protocol.ingest_ipipe_evidence(self.run_id, content)
        profile_path = self.root / "config" / "projects" / "bgw.yaml"
        changed = copy.deepcopy(self.profile)
        changed["review_provider"]["command"] = "other-review"
        profile_path.write_text(yaml.safe_dump(changed), encoding="utf-8")

        replay = self.protocol.ingest_ipipe_evidence(self.run_id, copy.deepcopy(content))

        self.assertTrue(first["ok"], first)
        self.assertFalse(replay["ok"], replay)
        self.assertEqual(replay["reason_code"], "PROFILE_CONFLICT")


class Task7CliStartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = profile_fixture()
        profile_path = self.root / "config" / "projects" / "bgw.yaml"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(yaml.safe_dump(self.profile), encoding="utf-8")
        self.orchestrator = Orchestrator(self.root)

    def _main(self, cafe_result):
        output = io.StringIO()
        with (
            patch("orchestrator.Orchestrator", return_value=self.orchestrator),
            patch("clients.icafe_client.CafeClient", return_value=CafeFake(cafe_result)),
            patch(
                "orchestrator.load_profile",
                return_value={"ready": True, "reason_code": "READY", "profile": self.profile},
            ),
            contextlib.redirect_stdout(output),
        ):
            code = main(["--config-root", str(self.root), "start", "BGW-1", "bgw"])
        return code, json.loads(output.getvalue())

    def test_cli_start_captures_canonical_snapshot_before_strict_start(self):
        code, result = self._main(snapshot_fixture())

        self.assertEqual(code, 0)
        self.assertEqual(result.get("state"), "INTAKE", result)
        payload = self.orchestrator.state.events(result["run_id"])[0]["payload"]
        self.assertEqual(payload["requirement_snapshot"], snapshot_fixture())

    def test_cli_start_returns_nonzero_for_missing_or_invalid_snapshot(self):
        for result, expected in (
            ({"ok": False, "reason_code": "ICAFE_CARD_NOT_FOUND"}, "ICAFE_CARD_NOT_FOUND"),
            ({"canonical_card_id": "BGW-1", "title": "partial"}, "ICAFE_SNAPSHOT_INVALID"),
        ):
            with self.subTest(expected=expected):
                code, output = self._main(result)
                self.assertEqual(code, 1)
                self.assertEqual(output["reason_code"], expected)


if __name__ == "__main__":
    unittest.main()
