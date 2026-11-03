"""Workspace writeback is content-bound, isolated and never blindly re-pushed.

Only temporary local Git repositories are used. Every iCode call and approval
channel is injected; no real platform transport may be constructed by a test.
"""

import copy
import hashlib
import json
import shlex
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import knowledge_writeback
from approval_ledger import revise_supported
from orchestrator import Orchestrator
from phase_protocol import _approval_input_hash
from schema_validator import validate_knowledge_candidates, validate_named_schema
from test_phase_protocol import _seed_envelope
from test_schema_validation import specialized_examples
from test_workspace_knowledge import WorkspaceFixture


CR_NUMBER = "77"
CR_URL = f"https://icode.baidu.com/myreview/changes/{CR_NUMBER}"
MEMBER = "owner@example.test"
ABSENT = object()
INDEX = "# Environment\n\n## Knowledge Map\n\n- [Topology](topology.md)\n\n## Usage\n\nKeep these instructions.\n"
CHANGELOG = "# Workspace releases\n\n## 2026-10-01\n\n- Initial team knowledge.\n"


def canonical_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def candidate(target="docs/environment/runner-network.md", **changes):
    return {
        "target": target,
        "title": "检查 runner 的访问前置条件",
        "body": "触发前验证 runner 到制品服务的连通性。",
        "reason": "日志 log:network-1 与回执 receipt:rerun-1 已验证，可跨运行复用。",
        **changes,
    }


class FakeIcodeTransport:
    def __init__(self):
        self.calls = []
        self.worktrees = []
        self.changes = []
        self.on_push = None
        self.push_result = {"returncode": 0, "stdout": f"Created review: {CR_URL}\n", "stderr": ""}
        self.query_result = None

    @property
    def pushes(self):
        return [call for call in self.calls if "push_cr" in call[0]]

    @property
    def queries(self):
        return [call for call in self.calls if "get_repo_reviews" in call[0]]

    def run(self, argv, *, cwd=None, timeout=None):
        self.calls.append((list(argv), str(cwd) if cwd is not None else None, timeout))
        if "push_cr" in argv:
            worktree = Path(argv[argv.index("--repo-path") + 1])
            if not worktree.is_dir():
                raise AssertionError("push_cr must receive a live isolated worktree")
            if not (worktree / ".git").is_dir():
                raise AssertionError("iCode requires a directory-shaped .git, not a worktree gitfile")
            self.worktrees.append(worktree)
            if self.on_push is not None:
                self.on_push(worktree)
            if isinstance(self.push_result, Exception):
                raise self.push_result
            return dict(self.push_result)
        if "get_repo_reviews" in argv:
            if isinstance(self.query_result, Exception):
                raise self.query_result
            return self.query_result or {
                "returncode": 0,
                "stdout": json.dumps({"data": {"changes": self.changes}}),
                "stderr": "",
            }
        raise AssertionError(f"Unexpected platform command: {argv}")


class FakeApprovalClient:
    def __init__(self, channel):
        self.channel = channel
        self.calls = []

    def request(self, envelope):
        self.calls.append(copy.deepcopy(envelope))
        approval = envelope["approval"]
        request_id = f"fake-{self.channel}-{approval['approval_id']}"
        if self.channel == "comate":
            return {"request_id": request_id}
        return {
            "request_id": request_id,
            "approval_id": approval["approval_id"],
            "run_id": approval["run_id"],
            "channel": "infoflow",
            "input_hash": approval["input_hash"],
            "member_policy": approval["member_policy"],
            "status": "PENDING",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "deadline_at": approval["deadline_at"],
            "heartbeat_at": None,
            "updated_at": None,
            "reply": None,
            "handoff": None,
            "reason_code": None,
        }


class KnowledgeCandidateSchemaTests(unittest.TestCase):
    def diagnosis(self, candidates):
        return {**specialized_examples()["diagnosis"], "knowledge_candidates": candidates}

    def test_optional_empty_and_valid_candidates(self):
        self.assertEqual(validate_named_schema(specialized_examples()["diagnosis"], "diagnosis"), [])
        for candidates in ([], [candidate()], [candidate("docs/testing/nested/runner.v2_notes.md")],
                           [candidate("docs/environment.md")]):
            with self.subTest(candidates=candidates):
                self.assertEqual(validate_named_schema(self.diagnosis(candidates), "diagnosis"), [])
                self.assertEqual(validate_knowledge_candidates(candidates), [])

    def test_out_of_scope_paths_and_reserved_basenames_are_rejected(self):
        targets = (
            "../outside.md", "/docs/environment/file.md", "repos/file.md",
            "docs/../file.md", "docs/environment/../file.md",
            "docs/environment/a..b.md", "docs/environment/file..md",
            "docs/environment/index.md", "docs/environment/template.md",
            "docs/environment/README.md", "docs/index.md", "docs/template.md",
            "docs/README.md", "docs/testing/nested/index.md",
            "docs/Environment/file.md", "docs/environment/File.md",
            "docs/environment/file.txt", "docs/environment//file.md",
            "docs/environment/file.md\n", "docs/environment/file.md\x00",
            "docs/environment\\file.md", "docs/environment/%2e%2e/file.md",
        )
        for target in targets:
            with self.subTest(target=target):
                candidates = [candidate(target)]
                for issues in (validate_knowledge_candidates(candidates),
                               validate_named_schema(self.diagnosis(candidates), "diagnosis")):
                    self.assertIn("knowledge_candidates[0].target", [issue.path for issue in issues])

    def test_four_required_nonblank_string_fields_and_no_unknown_keys(self):
        original = candidate()
        for field in ("target", "title", "body", "reason"):
            cases = [{key: value for key, value in original.items() if key != field}]
            cases += [{**original, field: value} for value in ("", " \t\n", None, 1, False, [], {})]
            for value in cases:
                with self.subTest(field=field, value=value):
                    for issues in (validate_knowledge_candidates([value]),
                                   validate_named_schema(self.diagnosis([value]), "diagnosis")):
                        self.assertIn(f"knowledge_candidates[0].{field}", [issue.path for issue in issues])
        self.assertTrue(validate_knowledge_candidates([{**original, "unexpected": "not allowed"}]))
        self.assertTrue(validate_named_schema(self.diagnosis([{**original, "unexpected": "not allowed"}]), "diagnosis"))

    def test_nonarrays_and_nonobject_items_are_rejected(self):
        for value in (None, {}, "candidate", 1, False, [None], ["body"], [42], [[]]):
            with self.subTest(value=value):
                self.assertTrue(validate_knowledge_candidates(value))
                self.assertTrue(validate_named_schema(self.diagnosis(value), "diagnosis"))

    def test_semantics_also_check_candidates_for_an_incomplete_diagnosis(self):
        content = self.diagnosis([candidate("docs/environment/index.md")])
        content.update(route="DIAGNOSIS_INCOMPLETE", evidence_state="INSUFFICIENT",
                       hypothesis=None, repair_direction=None, repair_diff_hash=None, repair_plan=[])
        self.assertIn("knowledge_candidates[0].target",
                      [issue.path for issue in validate_named_schema(content, "diagnosis")])


class KnowledgeWritebackTests(WorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.git(self.workspace, "config", "user.name", "Workspace Fixture")
        self.git(self.workspace, "config", "user.email", "workspace@example.test")
        self.git(self.workspace, "remote", "add", "origin", "ssh://icode.example:8235/team/project-workspace")
        (self.workspace / "docs/environment/index.md").write_text(INDEX, encoding="utf-8")
        (self.workspace / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        self.commit = self.commit_workspace()
        self.pin["commit"] = self.commit
        self.transport = FakeIcodeTransport()
        self.comate = FakeApprovalClient("comate")
        self.infoflow = FakeApprovalClient("infoflow")
        self.sequence = 0
        transport_guard = patch(
            "knowledge_writeback.ProcessTransport",
            side_effect=AssertionError("Real iCode transport is forbidden in tests"),
        )
        transport_guard.start()
        self.addCleanup(transport_guard.stop)

    @staticmethod
    def git(path, *arguments):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *arguments],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()

    def new_run(self, *, released=True):
        self.sequence += 1
        started = self.start(fresh_token=f"writeback-{self.sequence}")
        self.assertIn("run_id", started, started)
        run_id = started["run_id"]
        if released:
            self.orchestrator.state.transition(run_id, "RELEASE_SUCCESS", {"requirement_id": "BGW-1"})
        return run_id

    def approval(self, run_id, input_hash, *, action=None, decision="APPROVE"):
        row = self.orchestrator.approvals.request(
            action or knowledge_writeback.ACTION, input_hash, ["comate", "infoflow"], run_id=run_id,
            member_policy={channel: [MEMBER] for channel in ("comate", "infoflow")},
        )
        for channel in ("comate", "infoflow"):
            self.orchestrator.approvals.record_delivery(
                row["approval_id"], channel, {"request_id": f"fake-{channel}"}, payload_hash=input_hash,
            )
        if decision is not None:
            self.orchestrator.approvals.resolve(
                row["approval_id"], decision, input_hash, "comate", run_id=run_id,
                responder=MEMBER, state_store=self.orchestrator.state,
            )
        return self.orchestrator.approvals.get(row["approval_id"])

    def seed_diagnosis(self, run_id, candidates=ABSENT, *, decision="APPROVE", action="G6",
                       approval_run=None, approval_hash=None, envelope_approval_hash=None):
        self.sequence += 1
        content = specialized_examples()["diagnosis"]
        content["repair_diff_hash"] = None
        content["attempt"] = self.sequence
        if candidates is not ABSENT:
            content["knowledge_candidates"] = copy.deepcopy(candidates)
        envelope = _seed_envelope(run_id, "DIAGNOSE", "T-1", content)
        envelope["action_id"] = canonical_hash({"run_id": run_id, "phase": "DIAGNOSE", "attempt": self.sequence})
        expected = _approval_input_hash(envelope, envelope["content_hash"], envelope["source_revisions"])
        if decision != "MISSING":
            row = self.approval(approval_run or run_id, approval_hash or expected, action=action, decision=decision)
            envelope["approval_id"] = row["approval_id"]
            envelope["approval_input_hash"] = envelope_approval_hash or row["input_hash"]
        return self.orchestrator.artifacts.put_envelope(envelope)

    def ready(self, run_id):
        planned = knowledge_writeback.plan(self.orchestrator, run_id)
        self.assertTrue(planned["ok"], planned)
        self.assertEqual(planned["reason_code"], "WRITEBACK_READY", planned)
        return planned

    def execute(self, run_id, approval_id, input_hash, *, orchestrator=None):
        return knowledge_writeback.execute(
            orchestrator or self.orchestrator, run_id, approval_id, input_hash,
            argv_transport=self.transport, icode_binary="icode",
        )

    def authorized(self, candidates=None):
        run_id = self.new_run()
        self.seed_diagnosis(run_id, [candidate()] if candidates is None else candidates)
        planned = self.ready(run_id)
        approval = self.approval(run_id, planned["input_hash"])
        return run_id, planned, approval

    def checkout_snapshot(self):
        files = {}
        for path in self.workspace.rglob("*"):
            relative = path.relative_to(self.workspace)
            if ".git" in relative.parts:
                continue
            if path.is_symlink():
                files[str(relative)] = ("symlink", str(path.readlink()))
            elif path.is_file():
                files[str(relative)] = path.read_bytes()
        return {
            "head": self.git(self.workspace, "rev-parse", "HEAD"),
            "branch": self.git(self.workspace, "symbolic-ref", "HEAD"),
            "status": self.git(self.workspace, "status", "--porcelain=v1", "--untracked-files=all"),
            "staged": self.git(self.workspace, "diff", "--cached", "--binary"),
            "unstaged": self.git(self.workspace, "diff", "--binary"),
            "config": (self.workspace / ".git/config").read_bytes(),
            "files": files,
        }

    def mutation_snapshot(self, run_id):
        return {
            "checkout": self.checkout_snapshot(),
            "refs": self.git(self.workspace, "for-each-ref"),
            "worktrees": self.git(self.workspace, "worktree", "list", "--porcelain"),
            "intents": self.orchestrator.state.all_intents(run_id),
            "events": self.orchestrator.state.events(run_id),
        }

    def intent_key(self, run_id, planned):
        return f"workspace.writeback:{run_id}:{planned['input_hash']}"

    def assert_cleaned(self):
        self.assertTrue(self.transport.worktrees)
        for path in self.transport.worktrees:
            self.assertFalse(path.exists(), path)
        worktrees = self.git(self.workspace, "worktree", "list", "--porcelain")
        self.assertEqual(sum(line.startswith("worktree ") for line in worktrees.splitlines()), 1)

    def test_nonreleased_and_missing_runs_are_refused(self):
        run_id = self.new_run(released=False)
        for target in (run_id, "missing-run"):
            with self.subTest(run_id=target):
                result = knowledge_writeback.plan(self.orchestrator, target)
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason_code"], "WRITEBACK_RUN_NOT_RELEASED")
        self.assertEqual(self.transport.calls, [])

    def test_released_run_without_workspace_pin_is_refused(self):
        self.profile["knowledge_sources"] = [source for source in self.profile["knowledge_sources"]
                                             if source["provider"] != "workspace"]
        self.write_profile()
        run_id = self.new_run()
        result = knowledge_writeback.plan(self.orchestrator, run_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "WRITEBACK_NO_WORKSPACE")

    def test_no_candidates_never_opens_or_delivers_approval(self):
        run_id = self.new_run()
        self.seed_diagnosis(run_id)
        self.seed_diagnosis(run_id, [])
        approvals = self.orchestrator.approvals.for_run(run_id)
        before = self.mutation_snapshot(run_id)
        for result in (knowledge_writeback.plan(self.orchestrator, run_id),
                       knowledge_writeback.request(self.orchestrator, run_id,
                                                   comate_client=self.comate, infoflow_client=self.infoflow)):
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["reason_code"], "WRITEBACK_NOTHING")
        self.assertEqual(self.orchestrator.approvals.for_run(run_id), approvals)
        self.assertEqual(self.mutation_snapshot(run_id), before)
        self.assertEqual(self.comate.calls + self.infoflow.calls + self.transport.calls, [])

    def test_plan_is_read_only_and_returns_full_pinned_proposal(self):
        run_id = self.new_run()
        item = candidate()
        artifact = self.seed_diagnosis(run_id, [item])
        before = self.mutation_snapshot(run_id)
        approvals = self.orchestrator.approvals.for_run(run_id)
        planned = self.ready(run_id)
        expected = {**item, "body_sha256": hashlib.sha256(item["body"].encode()).hexdigest(),
                    "source_artifact_id": artifact["artifact_id"]}
        self.assertEqual(planned["proposal"], {"run_id": run_id, "repository": self.pin["repository"],
                                               "commit": self.commit, "candidates": [expected]})
        self.assertEqual(planned["input_hash"], canonical_hash(planned["proposal"]))
        self.assertEqual(self.ready(run_id), planned)
        self.assertEqual(self.mutation_snapshot(run_id), before)
        self.assertEqual(self.orchestrator.approvals.for_run(run_id), approvals)
        self.assertIsNone(self.orchestrator.state.idempotency_result(
            f"workspace.writeback-material:{run_id}:{planned['input_hash']}"))

    def test_multiple_approved_g6_diagnoses_are_deduplicated_by_target_and_body(self):
        run_id = self.new_run()
        first = candidate()
        second = candidate(body="同一文件的另一项验证结论。", title="第二项结论")
        other_target = candidate("docs/testing/runner-network.md")
        first_artifact = self.seed_diagnosis(run_id, [first])
        second_artifact = self.seed_diagnosis(run_id, [{**first, "title": "重复标题", "reason": "重复理由"},
                                                     second, other_target])
        actual = self.ready(run_id)["proposal"]["candidates"]
        self.assertEqual({(item["target"], item["body"]) for item in actual},
                         {(item["target"], item["body"]) for item in (first, second, other_target)})
        self.assertEqual(len(actual), 3)
        retained = next(item for item in actual if (item["target"], item["body"]) == (first["target"], first["body"]))
        self.assertEqual(retained["title"], first["title"])
        self.assertEqual(retained["source_artifact_id"], first_artifact["artifact_id"])
        self.assertIn(second_artifact["artifact_id"], {item["source_artifact_id"] for item in actual})

    def test_missing_pending_and_rejected_g6_do_not_supply_candidates(self):
        run_id = self.new_run()
        for index, decision in enumerate(("MISSING", None, "REJECT")):
            self.seed_diagnosis(run_id, [candidate(f"docs/environment/unapproved-{index}.md")], decision=decision)
        result = knowledge_writeback.plan(self.orchestrator, run_id)
        self.assertEqual(result["reason_code"], "WRITEBACK_NOTHING")
        self.assertTrue(result["ok"])

    def test_g6_for_wrong_run_action_or_approval_hash_is_not_authority(self):
        run_id = self.new_run()
        cases = ({"approval_run": "other-run"}, {"action": "G5"},
                 {"envelope_approval_hash": "c" * 64})
        for index, options in enumerate(cases):
            self.seed_diagnosis(run_id, [candidate(f"docs/environment/wrong-binding-{index}.md")], **options)
        self.assertEqual(knowledge_writeback.plan(self.orchestrator, run_id)["reason_code"], "WRITEBACK_NOTHING")

    def test_matching_ledger_and_envelope_hash_still_requires_content_recomputation(self):
        run_id = self.new_run()
        artifact = self.seed_diagnosis(run_id, [candidate()], approval_hash="b" * 64)
        envelope = artifact["envelope"]
        record = self.orchestrator.approvals.get(envelope["approval_id"])
        self.assertEqual(record["input_hash"], envelope["approval_input_hash"])
        self.assertNotEqual(envelope["approval_input_hash"], _approval_input_hash(
            envelope, envelope["content_hash"], envelope["source_revisions"]))
        self.assertEqual(knowledge_writeback.plan(self.orchestrator, run_id)["reason_code"], "WRITEBACK_NOTHING")

    def test_body_already_present_at_pin_is_filtered_only_for_its_target(self):
        run_id = self.new_run()
        body = "Pinned environment\n"
        self.seed_diagnosis(run_id, [candidate("docs/environment/topology.md", body=body),
                                     candidate("docs/testing/copied-check.md", body=body)])
        actual = self.ready(run_id)["proposal"]["candidates"]
        self.assertEqual([item["target"] for item in actual], ["docs/testing/copied-check.md"])

    def test_all_candidates_already_present_mean_no_approval(self):
        run_id = self.new_run()
        self.seed_diagnosis(run_id, [candidate("docs/environment/topology.md", body="Pinned environment\n")])
        before = self.orchestrator.approvals.for_run(run_id)
        result = knowledge_writeback.request(self.orchestrator, run_id,
                                             comate_client=self.comate, infoflow_client=self.infoflow)
        self.assertEqual(result["reason_code"], "WRITEBACK_NOTHING")
        self.assertEqual(self.orchestrator.approvals.for_run(run_id), before)
        self.assertEqual(self.comate.calls + self.infoflow.calls, [])

    def test_hash_binds_every_field_of_every_candidate(self):
        run_id = self.new_run()
        items = [candidate(), candidate("docs/testing/runner-check.md", body="第二个结论。")]
        self.seed_diagnosis(run_id, items)
        baseline = self.ready(run_id)
        for index in range(len(items)):
            for field in ("target", "title", "body", "reason"):
                with self.subTest(index=index, field=field):
                    changed = copy.deepcopy(items)
                    changed[index][field] = (changed[index][field][:-3] + "-v2.md" if field == "target"
                                              else changed[index][field] + "x")
                    artifact = self.seed_diagnosis(run_id, changed)
                    with patch.object(self.orchestrator.artifacts, "phase_artifacts", return_value=[artifact]):
                        planned = self.ready(run_id)
                    self.assertEqual(planned["input_hash"], canonical_hash(planned["proposal"]))
                    self.assertNotEqual(planned["input_hash"], baseline["input_hash"])
                    self.assertEqual({item[field] for item in planned["proposal"]["candidates"]},
                                     {item[field] for item in changed})

    def test_missing_and_pending_writeback_approval_do_not_write(self):
        run_id = self.new_run()
        self.seed_diagnosis(run_id, [candidate()])
        planned = self.ready(run_id)
        pending = self.approval(run_id, planned["input_hash"], decision=None)
        for approval_id in ("missing-approval", pending["approval_id"]):
            with self.subTest(approval_id=approval_id):
                before = self.mutation_snapshot(run_id)
                result = self.execute(run_id, approval_id, planned["input_hash"])
                self.assertEqual(result["reason_code"], "WRITEBACK_APPROVAL_REQUIRED")
                self.assertEqual(self.mutation_snapshot(run_id), before)
        self.assertEqual(self.transport.calls, [])

    def test_wrong_input_hash_does_not_write(self):
        run_id, planned, approval = self.authorized()
        before = self.mutation_snapshot(run_id)
        result = self.execute(run_id, approval["approval_id"], "0" * 64)
        self.assertEqual(result["reason_code"], "WRITEBACK_INPUT_MISMATCH")
        self.assertEqual(self.mutation_snapshot(run_id), before)
        self.assertEqual(self.transport.calls, [])

    def test_approved_wrong_action_run_or_hash_does_not_write(self):
        run_id = self.new_run()
        self.seed_diagnosis(run_id, [candidate()])
        planned = self.ready(run_id)
        approvals = [self.approval(run_id, planned["input_hash"], action="G6"),
                     self.approval("other-writeback-run", planned["input_hash"]),
                     self.approval(run_id, "f" * 64)]
        for approval in approvals:
            with self.subTest(approval_id=approval["approval_id"]):
                before = self.mutation_snapshot(run_id)
                result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
                self.assertEqual(result["reason_code"], "WRITEBACK_APPROVAL_MISMATCH")
                self.assertEqual(self.mutation_snapshot(run_id), before)
        self.assertEqual(self.transport.calls, [])

    def test_replanned_candidate_changes_cannot_reuse_approval(self):
        run_id, planned, approval = self.authorized()
        self.seed_diagnosis(run_id, [candidate(body=candidate()["body"] + "x")])
        before = self.mutation_snapshot(run_id)
        result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(result["reason_code"], "WRITEBACK_INPUT_MISMATCH")
        self.assertEqual(self.mutation_snapshot(run_id), before)
        self.assertEqual(self.transport.calls, [])

    def test_reject_abandons_only_writeback_and_preserves_release_success(self):
        run_id = self.new_run()
        self.seed_diagnosis(run_id, [candidate()])
        planned = self.ready(run_id)
        before_events = self.orchestrator.state.events(run_id)
        approval = self.approval(run_id, planned["input_hash"], decision="REJECT")
        before = self.mutation_snapshot(run_id)
        result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(result["reason_code"], "WRITEBACK_REJECTED")
        self.assertEqual(self.orchestrator.state.events(run_id), before_events)
        self.assertEqual(self.orchestrator.status(run_id)["state"], "RELEASE_SUCCESS")
        self.assertEqual(self.mutation_snapshot(run_id), before)
        self.assertEqual(self.transport.calls, [])

    def test_request_delivers_full_proposal_to_both_profile_member_channels(self):
        run_id = self.new_run()
        items = [candidate(), candidate("docs/testing/runner-check.md", title="流水线前置检查", body="先核对 runner 容量。")]
        self.seed_diagnosis(run_id, items)
        planned = self.ready(run_id)
        result = knowledge_writeback.request(self.orchestrator, run_id,
                                             comate_client=self.comate, infoflow_client=self.infoflow)
        self.assertTrue(result["ok"], result)
        row = self.orchestrator.approvals.get(result["approval_id"])
        self.assertEqual(row["action"], "KNOWLEDGE_WRITEBACK")
        self.assertEqual(row["input_hash"], planned["input_hash"])
        members = sorted({value for group in self.profile["approval_channels"]["role_members"].values()
                          for value in group})
        self.assertEqual(row["member_policy"], {channel: members for channel in ("comate", "infoflow")})
        self.assertEqual(len(self.comate.calls), 1)
        self.assertEqual(len(self.infoflow.calls), 1)
        for delivery in self.comate.calls + self.infoflow.calls:
            material = json.dumps(delivery["approval"]["evidence"], ensure_ascii=False)
            for item in items:
                for field in ("target", "title", "body"):
                    self.assertIn(item[field], material)
        self.assertEqual(self.orchestrator.state.idempotency_result(
            f"workspace.writeback-material:{run_id}:{planned['input_hash']}"), planned["proposal"])
        self.assertFalse(any(intent["operation"] == "workspace.writeback"
                             for intent in self.orchestrator.state.all_intents(run_id)))
        self.assertEqual(self.transport.calls, [])

    def test_writeback_cannot_be_revised(self):
        run_id = self.new_run()
        self.assertFalse(revise_supported(knowledge_writeback.ACTION, self.orchestrator.state, run_id))
        pending = self.approval(run_id, "e" * 64, decision=None)
        result = self.orchestrator.approvals.receive(
            pending["approval_id"], "REVISE", pending["input_hash"], "comate", MEMBER,
            run_id=run_id, state_store=self.orchestrator.state,
        )
        self.assertEqual(result["reason_code"], "APPROVAL_REVISE_NOT_SUPPORTED")

    def test_approved_writeback_isolated_commit_index_changelog_receipt_and_replay(self):
        items = [candidate(), candidate("docs/environment/topology.md", title="运行前核对拓扑", body="以钉住版本核对角色映射。")]
        run_id, planned, approval = self.authorized(items)
        self.git(self.workspace, "switch", "-qc", "user-current-branch")
        (self.workspace / "docs/environment/topology.md").write_text("User's unstaged topology\n", encoding="utf-8")
        (self.workspace / "README.md").write_text("User's staged README\n", encoding="utf-8")
        self.git(self.workspace, "add", "README.md")
        (self.workspace / "user-note.txt").write_text("User's untracked note\n", encoding="utf-8")
        hooks = self.root / "failing-hooks"
        hooks.mkdir()
        marker = self.root / "hook-was-run"
        for name in ("pre-commit", "commit-msg", "post-checkout"):
            path = hooks / name
            path.write_text(f"#!/bin/sh\nprintf hook >> {shlex.quote(str(marker))}\nexit 77\n", encoding="utf-8")
            path.chmod(0o755)
        self.git(self.workspace, "config", "core.hooksPath", str(hooks))
        before = self.checkout_snapshot()
        registered = self.git(self.workspace, "worktree", "list", "--porcelain")
        key = self.intent_key(run_id, planned)
        observations = []

        def inspect_push(worktree):
            observations.append((worktree, self.git(worktree, "rev-parse", "HEAD^")))
            self.assertNotEqual(worktree, self.workspace)
            self.assertEqual(self.git(worktree, "branch", "--show-current"), f"tom-autodev/{run_id[:12]}")
            self.assertEqual(self.git(worktree, "status", "--porcelain"), "")
            intent = self.orchestrator.state.intent_by_idempotency_key(key)
            self.assertIsNotNone(intent)
            self.assertEqual(intent["operation"], "workspace.writeback")

        self.transport.on_push = inspect_push
        result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason_code"], "WRITEBACK_PUSHED")
        self.assertEqual(result["cr_url"], CR_URL)
        self.assertEqual(result["change_number"], CR_NUMBER)
        self.assertEqual(result["change_id"], "I" + planned["input_hash"][:40])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0][1], self.commit)
        branch = f"tom-autodev/{run_id[:12]}"
        revision = self.git(self.workspace, "rev-parse", branch)
        self.assertEqual(result["commit_revision"], revision)
        self.assertEqual(self.git(self.workspace, "show", f"{branch}:{items[0]['target']}"),
                         f"# {items[0]['title']}\n\n{items[0]['body']}")
        self.assertEqual(self.git(self.workspace, "show", f"{branch}:{items[1]['target']}"),
                         f"Pinned environment\n\n## {items[1]['title']}\n\n{items[1]['body']}")
        index = self.git(self.workspace, "show", f"{branch}:docs/environment/index.md")
        entry = f"- [{items[0]['title']}](runner-network.md)"
        self.assertEqual(index.count(entry), 1)
        self.assertLess(index.index("## Knowledge Map"), index.index(entry))
        self.assertLess(index.index(entry), index.index("## Usage"))
        self.assertEqual(index.count("](topology.md)"), 1)
        changelog = self.git(self.workspace, "show", f"{branch}:CHANGELOG.md")
        self.assertTrue(changelog.startswith(f"## {datetime.now().astimezone().date().isoformat()}\n"), changelog)
        self.assertIn(CHANGELOG.strip(), changelog)
        for item in items:
            self.assertIn(item["target"], changelog)
        message = self.git(self.workspace, "log", "-1", "--format=%B", branch)
        self.assertEqual(message.splitlines()[0], f"BGW-1 docs: tom-autodev 运行 {run_id[:12]} 写回知识")
        self.assertTrue(message.endswith(f"Change-Id: I{planned['input_hash'][:40]}"))
        for item in items:
            self.assertIn(item["target"], message)
            self.assertIn(item["title"], message)
        self.assertEqual(self.git(self.workspace, "log", "-1", "--format=%an <%ae>", branch),
                         "Workspace Fixture <workspace@example.test>")
        self.assertFalse(marker.exists(), "Git hooks must be disabled")
        self.assertEqual(self.checkout_snapshot(), before)
        self.assertEqual(self.git(self.workspace, "worktree", "list", "--porcelain"), registered)
        self.assert_cleaned()
        argv, cwd, _ = self.transport.pushes[0]
        self.assertEqual(argv, ["icode", "git", "push_cr", "--repo-path", cwd, "--branch", "master"])
        saved = self.orchestrator.state.result_by_idempotency_key(key)
        self.assertEqual(saved["operation"], "workspace.writeback")
        self.assertEqual(saved["receipt"]["response"]["cr_url"], CR_URL)
        calls = list(self.transport.calls)
        replay = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(replay["reason_code"], "WRITEBACK_REPLAY")
        self.assertEqual(replay["commit_revision"], revision)
        self.assertEqual(self.transport.calls, calls)
        self.assertEqual(self.checkout_snapshot(), before)

    def test_writeback_reads_and_branches_from_pin_not_newer_user_head(self):
        run_id = self.new_run()
        item = candidate()
        self.seed_diagnosis(run_id, [item])
        path = self.workspace / item["target"]
        path.write_text(f"# User's later title\n\n{item['body']}\n", encoding="utf-8")
        (self.workspace / "later-only.md").write_text("Must not enter the writeback branch\n", encoding="utf-8")
        newer = self.commit_workspace()
        self.assertNotEqual(newer, self.commit)
        planned = self.ready(run_id)
        self.assertEqual(planned["proposal"]["commit"], self.commit)
        approval = self.approval(run_id, planned["input_hash"])
        before = self.checkout_snapshot()
        result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(result["reason_code"], "WRITEBACK_PUSHED", result)
        revision = result["commit_revision"]
        self.assertEqual(self.git(self.workspace, "rev-parse", f"{revision}^"), self.commit)
        self.assertEqual(self.git(self.workspace, "show", f"{revision}:{item['target']}"),
                         f"# {item['title']}\n\n{item['body']}")
        self.assertNotIn("later-only.md", self.git(self.workspace, "ls-tree", "-r", "--name-only", revision).splitlines())
        self.assertEqual(self.checkout_snapshot(), before)
        self.assertEqual(self.git(self.workspace, "rev-parse", "HEAD"), newer)
        self.assert_cleaned()

    def test_new_category_gets_index_and_absent_changelog_is_not_created(self):
        (self.workspace / "CHANGELOG.md").unlink()
        self.commit = self.commit_workspace()
        item = candidate("docs/process/runner-check.md")
        run_id, planned, approval = self.authorized([item])
        result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(result["reason_code"], "WRITEBACK_PUSHED", result)
        revision = result["commit_revision"]
        index = self.git(self.workspace, "show", f"{revision}:docs/process/index.md")
        self.assertIn("## Knowledge Map", index)
        self.assertIn(f"- [{item['title']}](runner-check.md)", index)
        self.assertNotIn("CHANGELOG.md", self.git(self.workspace, "ls-tree", "-r", "--name-only", revision).splitlines())
        self.assert_cleaned()

    def test_cli_directory_view_preserves_git_binding_and_every_push_url(self):
        self.git(self.workspace, "config", "--add", "remote.origin.pushurl", "ssh://first.example/team/workspace")
        self.git(self.workspace, "config", "--add", "remote.origin.pushurl", "ssh://second.example/team/workspace")
        self.git(self.workspace, "remote", "add", "origin-https", "https://icode.example/team/workspace")
        run_id, planned, approval = self.authorized()
        before = self.checkout_snapshot()
        original_prepare = knowledge_writeback._prepare_cli_worktree
        seen = {}

        def prepare(worktree):
            marker = worktree / ".git"
            self.assertTrue(marker.is_file())
            self.assertFalse(marker.is_symlink())
            seen["gitfile"] = marker.read_bytes()
            seen["gitdir"] = self.git(worktree, "rev-parse", "--absolute-git-dir")
            seen["binding"] = self.git(worktree, "rev-parse", "HEAD", "HEAD^", "--show-toplevel", "--git-common-dir")
            seen["urls"] = {remote: self.git(worktree, "remote", "get-url", "--push", "--all", remote)
                            for remote in ("origin", "origin-https")}
            original_prepare(worktree)

        def inspect(worktree):
            marker = worktree / ".git"
            self.assertTrue(marker.is_symlink())
            self.assertTrue(marker.is_dir())
            self.assertEqual(str(marker.readlink()), seen["gitdir"])
            self.assertNotEqual(marker.resolve(), (self.workspace / ".git").resolve())
            self.assertEqual(self.git(worktree, "rev-parse", "HEAD", "HEAD^", "--show-toplevel", "--git-common-dir"),
                             seen["binding"])
            self.assertEqual(self.git(worktree, "rev-parse", "HEAD^"), self.commit)
            for remote, urls in seen["urls"].items():
                self.assertEqual(self.git(worktree, "remote", "get-url", "--push", "--all", remote), urls)
            self.assertFalse(knowledge_writeback._single_push_target(worktree))

        self.transport.on_push = inspect
        with patch("knowledge_writeback._prepare_cli_worktree", side_effect=prepare):
            result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(result["reason_code"], "WRITEBACK_PUSHED", result)
        self.assertEqual(self.checkout_snapshot(), before)
        self.assert_cleaned()

    def test_cli_view_failure_restores_gitfile_and_cleans_worktree_without_dispatch(self):
        run_id, planned, approval = self.authorized()
        before = self.checkout_snapshot()
        original_prepare = knowledge_writeback._prepare_cli_worktree
        temporary_worktrees = []

        def prepare(worktree):
            temporary_worktrees.append(worktree)
            original_prepare(worktree)
            self.assertTrue((worktree / ".git").is_dir())
            raise OSError("local adapter preparation failed before dispatch")

        with patch("knowledge_writeback._prepare_cli_worktree", side_effect=prepare):
            result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(result["reason_code"], "WRITEBACK_LOCAL_FAILED", result)
        self.assertEqual(result["intent_state"], "ABANDONED_LOCAL")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.checkout_snapshot(), before)
        self.assertTrue(temporary_worktrees)
        self.assertFalse(temporary_worktrees[0].exists())
        self.assertEqual(sum(line.startswith("worktree ") for line in
                             self.git(self.workspace, "worktree", "list", "--porcelain").splitlines()), 1)
        replay = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(replay["reason_code"], "WRITEBACK_LOCAL_FAILED")
        self.assertEqual(self.transport.calls, [])

    def test_multiple_sections_in_one_new_file_create_only_one_map_entry(self):
        first = candidate(body="A: 已验证的网络前置条件。")
        second = candidate(title="第二个检查", body="B: 已验证的容量前置条件。")
        run_id, planned, approval = self.authorized([first, second])
        result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(result["reason_code"], "WRITEBACK_PUSHED", result)
        text = self.git(self.workspace, "show", f"{result['commit_revision']}:{first['target']}")
        self.assertTrue(text.startswith(f"# {first['title']}\n"))
        self.assertIn(f"## {second['title']}\n\n{second['body']}", text)
        index = self.git(self.workspace, "show", f"{result['commit_revision']}:docs/environment/index.md")
        self.assertEqual(index.count("](runner-network.md)"), 1)

    def test_timeout_unknown_restart_only_queries_and_never_repushes(self):
        run_id, planned, approval = self.authorized()
        self.transport.push_result = TimeoutError("response was lost after sending")
        first = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(first["reason_code"], "WRITEBACK_RESULT_UNKNOWN", first)
        self.assertEqual(first["intent_state"], "UNKNOWN")
        key = self.intent_key(run_id, planned)
        self.assertIsNone(self.orchestrator.state.result_by_idempotency_key(key))
        intent = self.orchestrator.state.intent_by_idempotency_key(key)
        self.assertEqual(intent["operation"], "workspace.writeback")
        branch = f"tom-autodev/{run_id[:12]}"
        revision = self.git(self.workspace, "rev-parse", branch)
        self.assert_cleaned()
        restarted = Orchestrator(self.config)
        for _ in range(2):
            result = self.execute(run_id, approval["approval_id"], planned["input_hash"], orchestrator=restarted)
            self.assertEqual(result["reason_code"], "WRITEBACK_RESULT_UNKNOWN", result)
            self.assertEqual(result["intent_state"], "UNKNOWN")
        self.assertEqual(len(self.transport.pushes), 1)
        self.assertEqual(len(self.transport.queries), 2)
        for argv, cwd, _ in self.transport.queries:
            self.assertEqual(argv, ["icode", "api", "get_repo_reviews", "--repo", self.pin["repository"],
                                    "--status", "NEW", "-o", "json"])
            self.assertEqual(Path(cwd), self.workspace)
        self.assertEqual(self.git(self.workspace, "rev-parse", branch), revision)
        self.assertIsNone(restarted.state.result_by_idempotency_key(key))

    def test_unknown_can_be_receipted_by_exact_change_id_lookup_without_push(self):
        run_id, planned, approval = self.authorized()
        self.transport.push_result = TimeoutError("response lost")
        self.execute(run_id, approval["approval_id"], planned["input_hash"])
        revision = self.git(self.workspace, "rev-parse", f"tom-autodev/{run_id[:12]}")
        self.transport.changes = [{
            "_number": CR_NUMBER, "status": "NEW", "branch": "master", "project": self.pin["repository"],
            "change_id": "I" + planned["input_hash"][:40], "current_revision": revision, "url": CR_URL,
        }]
        recovered = self.execute(run_id, approval["approval_id"], planned["input_hash"],
                                 orchestrator=Orchestrator(self.config))
        self.assertEqual(recovered["reason_code"], "WRITEBACK_PUSHED", recovered)
        self.assertEqual(recovered["cr_url"], CR_URL)
        self.assertEqual(recovered["commit_revision"], revision)
        self.assertIsNotNone(self.orchestrator.state.result_by_idempotency_key(self.intent_key(run_id, planned)))
        self.assertEqual(len(self.transport.pushes), 1)
        self.assertEqual(len(self.transport.queries), 1)
        replay = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(replay["reason_code"], "WRITEBACK_REPLAY")
        self.assertEqual(len(self.transport.calls), 2)

    def test_unknown_recovery_does_not_mutate_checkout_refs_tree_or_worktree_registration(self):
        run_id, planned, approval = self.authorized()
        self.transport.push_result = TimeoutError("response lost")
        first = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(first["intent_state"], "UNKNOWN", first)
        self.git(self.workspace, "switch", "-qc", "user-working-after-timeout")
        (self.workspace / "README.md").write_text("User's later staged edit\n", encoding="utf-8")
        self.git(self.workspace, "add", "README.md")
        (self.workspace / "docs/environment/topology.md").write_text("User's later edit\n", encoding="utf-8")
        (self.workspace / "later-note.txt").write_text("User's untracked note\n", encoding="utf-8")
        before = self.mutation_snapshot(run_id)
        branch = f"tom-autodev/{run_id[:12]}"
        tree = self.git(self.workspace, "ls-tree", "-r", branch)
        restarted = Orchestrator(self.config)
        with patch("knowledge_writeback._write_candidates", side_effect=AssertionError("recovery must not write")), \
                patch("knowledge_writeback.tempfile.TemporaryDirectory",
                      side_effect=AssertionError("recovery must not allocate a worktree")):
            for _ in range(2):
                result = self.execute(run_id, approval["approval_id"], planned["input_hash"], orchestrator=restarted)
                self.assertEqual(result["reason_code"], "WRITEBACK_RESULT_UNKNOWN", result)
                self.assertEqual(result["intent_state"], "UNKNOWN")
                self.assertEqual(self.mutation_snapshot(run_id), before)
                self.assertEqual(self.git(self.workspace, "ls-tree", "-r", branch), tree)
        self.assertEqual(len(self.transport.pushes), 1)
        self.assertEqual(len(self.transport.queries), 2)
        self.assert_cleaned()

    def test_conflicting_change_id_matches_are_sticky_ambiguous_without_repush(self):
        cases = ("multiple-reviews", "wrong-branch", "wrong-repository", "wrong-revision")
        for conflict in cases:
            with self.subTest(conflict=conflict):
                run_id, planned, approval = self.authorized()
                self.transport.push_result = TimeoutError("response lost")
                first = self.execute(run_id, approval["approval_id"], planned["input_hash"])
                self.assertEqual(first["intent_state"], "UNKNOWN", first)
                revision = self.git(self.workspace, "rev-parse", f"tom-autodev/{run_id[:12]}")
                exact = {"_number": CR_NUMBER, "status": "NEW", "branch": "master",
                         "project": self.pin["repository"], "change_id": "I" + planned["input_hash"][:40],
                         "current_revision": revision, "url": CR_URL}
                if conflict == "multiple-reviews":
                    self.transport.changes = [exact, {**exact, "_number": "88",
                                                      "url": "https://icode.baidu.com/myreview/changes/88"}]
                else:
                    field, value = {"wrong-branch": ("branch", "other-branch"),
                                    "wrong-repository": ("project", "team/other-workspace"),
                                    "wrong-revision": ("current_revision", "0" * 40)}[conflict]
                    self.transport.changes = [{**exact, field: value}]
                before = self.mutation_snapshot(run_id)
                push_count = len(self.transport.pushes)
                query_count = len(self.transport.queries)
                result = self.execute(run_id, approval["approval_id"], planned["input_hash"],
                                      orchestrator=Orchestrator(self.config))
                self.assertEqual(result["reason_code"], "WRITEBACK_IDENTITY_AMBIGUOUS", result)
                self.assertEqual(result["intent_state"], "AMBIGUOUS")
                self.assertIsNone(self.orchestrator.state.result_by_idempotency_key(self.intent_key(run_id, planned)))
                self.assertEqual(self.mutation_snapshot(run_id), before)
                self.assertEqual(len(self.transport.queries), query_count + 1)
                # A later apparently clean listing cannot silently clear a conflict.
                self.transport.changes = [exact]
                again = self.execute(run_id, approval["approval_id"], planned["input_hash"])
                self.assertEqual(again["reason_code"], "WRITEBACK_IDENTITY_AMBIGUOUS", again)
                self.assertEqual(again["intent_state"], "AMBIGUOUS")
                self.assertEqual(len(self.transport.pushes), push_count)
                self.assertEqual(len(self.transport.queries), query_count + 1)
                self.assertEqual(self.mutation_snapshot(run_id), before)
        self.assert_cleaned()

    def test_failed_or_invalid_review_api_keeps_unknown_without_mutating_git(self):
        run_id, planned, approval = self.authorized()
        self.transport.push_result = TimeoutError("response lost")
        first = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(first["intent_state"], "UNKNOWN", first)
        before = self.mutation_snapshot(run_id)
        responses = [TimeoutError("read timeout"),
                     {"returncode": 1, "stdout": json.dumps({"data": {"changes": []}}), "stderr": "failed"},
                     {"returncode": 0, "stdout": "not JSON", "stderr": ""}]
        responses += [{"returncode": 0, "stdout": json.dumps(value), "stderr": ""} for value in (
            None, {"error": "access denied"}, {"data": {}}, {"data": {"changes": {}}},
            {"data": {"changes": [None]}}, {"data": {"changes": ["not a review"]}},
        )]
        for index, response in enumerate(responses, 1):
            with self.subTest(response=response):
                self.transport.query_result = response
                result = self.execute(run_id, approval["approval_id"], planned["input_hash"],
                                      orchestrator=Orchestrator(self.config))
                self.assertEqual(result["reason_code"], "WRITEBACK_RESULT_UNKNOWN", result)
                self.assertEqual(result["intent_state"], "UNKNOWN")
                self.assertEqual(self.mutation_snapshot(run_id), before)
                self.assertIsNone(self.orchestrator.state.result_by_idempotency_key(self.intent_key(run_id, planned)))
                self.assertEqual(len(self.transport.pushes), 1)
                self.assertEqual(len(self.transport.queries), index)
        self.assert_cleaned()

    def test_timeout_exit_code_is_unknown_not_rejection(self):
        run_id, planned, approval = self.authorized()
        self.transport.push_result = {"returncode": 124, "stdout": "", "stderr": "process timeout"}
        result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(result["reason_code"], "WRITEBACK_RESULT_UNKNOWN", result)
        self.assertEqual(result["intent_state"], "UNKNOWN")
        self.assertIsNone(self.orchestrator.state.result_by_idempotency_key(self.intent_key(run_id, planned)))
        self.assert_cleaned()

    def test_unexplained_nonzero_push_is_unknown_by_default(self):
        run_id, planned, approval = self.authorized()
        self.transport.push_result = {"returncode": 1, "stdout": "", "stderr": "fatal: remote end hung up unexpectedly"}
        result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(result["reason_code"], "WRITEBACK_RESULT_UNKNOWN", result)
        self.assertEqual(result["intent_state"], "UNKNOWN")
        self.assertIsNone(self.orchestrator.state.result_by_idempotency_key(self.intent_key(run_id, planned)))

    def test_zero_exit_without_cr_url_is_unknown_and_is_not_retried(self):
        run_id, planned, approval = self.authorized()
        self.transport.push_result = {"returncode": 0, "stdout": "submitted", "stderr": ""}
        first = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        second = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        for result in (first, second):
            self.assertEqual(result["reason_code"], "WRITEBACK_RESULT_UNKNOWN", result)
            self.assertEqual(result["intent_state"], "UNKNOWN")
        self.assertEqual(len(self.transport.pushes), 1)
        self.assertEqual(len(self.transport.queries), 1)
        self.assertIsNone(self.orchestrator.state.result_by_idempotency_key(self.intent_key(run_id, planned)))
        self.assert_cleaned()

    def test_single_push_target_gerrit_refusal_is_terminal_receipt(self):
        self.git(self.workspace, "config", "remote.origin.pushurl", "ssh://icode.example:8235/team/project-workspace")
        run_id, planned, approval = self.authorized()
        self.transport.push_result = {"returncode": 1, "stdout": "", "stderr": (
            "To ssh://icode.example:8235/team/project-workspace\n"
            " ! [remote rejected] HEAD -> refs/for/master (prohibited)\n")}
        first = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(first["reason_code"], "WRITEBACK_PUSH_REJECTED", first)
        saved = self.orchestrator.state.result_by_idempotency_key(self.intent_key(run_id, planned))
        self.assertEqual(saved["receipt"]["response"]["reason_code"], "WRITEBACK_PUSH_REJECTED")
        self.assertEqual(self.orchestrator.state.pending_intents(run_id), [])
        second = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(second["reason_code"], "WRITEBACK_PUSH_REJECTED", second)
        self.assertEqual(len(self.transport.pushes), 1)
        self.assertEqual(self.transport.queries, [])
        self.assert_cleaned()

    def test_gerrit_refusal_from_one_of_multiple_push_urls_remains_unknown(self):
        for remote in ("one", "two"):
            self.git(self.workspace, "config", "--add", "remote.origin.pushurl",
                     f"ssh://{remote}.example:8235/team/project-workspace")
        run_id, planned, approval = self.authorized()
        self.transport.push_result = {"returncode": 1, "stdout": "", "stderr": (
            "To ssh://one.example:8235/team/project-workspace\n"
            " ! [remote rejected] HEAD -> refs/for/master (prohibited)\n")}
        first = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(first["reason_code"], "WRITEBACK_RESULT_UNKNOWN", first)
        self.assertEqual(first["intent_state"], "UNKNOWN")
        self.assertIsNone(self.orchestrator.state.result_by_idempotency_key(self.intent_key(run_id, planned)))
        self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(len(self.transport.pushes), 1)
        self.assertEqual(len(self.transport.queries), 1)
        self.assert_cleaned()

    def test_tracked_symlink_parent_cannot_escape_workspace(self):
        outside = self.root / "outside-knowledge"
        outside.mkdir()
        sentinel = outside / "sentinel.md"
        sentinel.write_text("Do not overwrite\n", encoding="utf-8")
        (self.workspace / "docs/escaped").symlink_to(outside, target_is_directory=True)
        self.commit = self.commit_workspace()
        run_id, planned, approval = self.authorized([candidate("docs/escaped/new-knowledge.md")])
        before = self.checkout_snapshot()
        result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
        self.assertEqual(result["reason_code"], "WRITEBACK_UNSAFE_PATH", result)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "Do not overwrite\n")
        self.assertFalse((outside / "new-knowledge.md").exists())
        self.assertEqual(self.checkout_snapshot(), before)
        self.assertEqual(sum(line.startswith("worktree ") for line in self.git(
            self.workspace, "worktree", "list", "--porcelain").splitlines()), 1)

    def test_symlink_candidate_index_and_changelog_are_not_followed(self):
        outside = self.root / "external.md"
        outside.write_text("Do not overwrite\n", encoding="utf-8")
        for target in ("docs/environment/unsafe.md", "docs/environment/index.md", "CHANGELOG.md"):
            with self.subTest(target=target):
                link = self.workspace / target
                original = link.read_bytes() if link.exists() else None
                link.unlink(missing_ok=True)
                link.symlink_to(outside)
                self.commit = self.commit_workspace()
                item = candidate("docs/environment/unsafe.md")
                run_id, planned, approval = self.authorized([item])
                result = self.execute(run_id, approval["approval_id"], planned["input_hash"])
                self.assertEqual(result["reason_code"], "WRITEBACK_UNSAFE_PATH", result)
                self.assertEqual(self.transport.calls, [])
                self.assertEqual(outside.read_text(encoding="utf-8"), "Do not overwrite\n")
                link.unlink()
                if original is not None:
                    link.write_bytes(original)
                self.commit_workspace()


class PushedUrlTests(unittest.TestCase):
    def test_the_console_review_url_push_cr_prints_is_recognised(self):
        import knowledge_writeback
        output = ("To https://icode.baidu.com/baidu/sysip/bgw-workspace\n"
                  "https://console.cloud.baidu-int.com/devops/icode/myreview/changes/c/baidu/sysip/bgw-workspace/+/123095131\n")
        self.assertEqual(knowledge_writeback._pushed_url(output)[0], "123095131")


if __name__ == "__main__":
    unittest.main()
