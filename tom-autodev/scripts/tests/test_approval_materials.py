"""Approval copy must describe the exact candidate, not a nearby/latest artifact."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_summary import gate_intent, pinned_context, references, _hash
from approval_delivery import _approval_markdown, _deliver_approval_card
from artifact_store import ArtifactStore
from orchestrator import Orchestrator, _approval_context, _deliver_worker_approval
from worker_driver import build_envelope
from test_orchestrator import PROFILE, _start, _write_profile
from test_run_brief import BriefFixture
import progress_snapshot


class ApprovalMaterialsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _render(self, gate, detail):
        return _approval_markdown({
            "approval_id": "a" * 32, "run_id": "example-run",
            "action": gate, "input_hash": "b" * 64,
            "deadline_at": "2026-09-30T00:00:00+00:00",
            "evidence": {"card_id": "EXAMPLE-1", "gate": detail},
        }, ["owner"])

    def test_every_gate_including_retry_has_review_focus_and_effect(self):
        for gate in [*(f"G{i}" for i in range(11)), "PROFILE_REPIN"]:
            with self.subTest(gate=gate):
                detail = gate_intent(gate + "#retry-1")
                self.assertTrue(detail["subject"])
                self.assertTrue(detail["effect"])
                self.assertEqual(len(detail["checklist"]), 2)
                self.assertIn("input_hash", detail["decision"])

    def test_button_card_preserves_missing_material_warning(self):
        orch = Orchestrator(self.root)
        sent = []
        def card(**payload):
            sent.append(payload)
            return {"card_id": "fake-card"}
        request = {
            "approval_id": "a" * 32, "run_id": "run-1", "action": "G5",
            "input_hash": "b" * 64, "deadline_at": "2026-09-30T00:00:00+00:00",
            "member_policy": {"infoflow": ["owner@example.test"]},
            "evidence": {"gate": {"material_status": "MISSING"}},
        }
        _deliver_approval_card(SimpleNamespace(send_approval_card=card), orch.state, request)
        self.assertIn("请勿批准", " ".join(sent[0]["lines"]))
        self.assertLessEqual(len(sent[0]["lines"]), 8)

    def test_g10_reads_verified_proposal_from_its_actual_state_store(self):
        from run_summary import _proposal_id
        candidate = {"target_files": [{"path": "/control/script.py", "before_sha256": "c" * 64}]}
        binding = _hash(candidate)
        proposal = {"run_id": "run-1", "proposal_id": _proposal_id("run-1", binding),
                    "candidate": candidate, "candidate_hash": binding}
        proposal["envelope_hash"] = _hash(proposal)
        stored = {**proposal, "proposal": proposal}
        orch = SimpleNamespace(state=SimpleNamespace(optimization_proposal=lambda _: stored))
        detail = pinned_context(orch, "run-1", "G10", binding)
        self.assertEqual(detail["material_status"], "PINNED")
        self.assertEqual(detail["_review_content"], proposal)
        self.assertIn("/control/script.py", [ref["ref"] for ref in detail["references"]])
        proposal["candidate"]["target_files"][0]["path"] = "/different.py"
        self.assertEqual(pinned_context(orch, "run-1", "G10", binding)["material_status"], "MISSING")

    def test_g0_exports_actual_profile_selection_without_advancing_or_requesting(self):
        _write_profile(self.root, PROFILE)
        orch = Orchestrator(self.root)
        run_id = _start(orch)["run_id"]
        payload = orch.state.events(run_id)[0]["payload"]
        detail = _approval_context(
            orch, run_id, action="G0", input_hash=payload["g0_input_hash"]
        )["evidence"]["gate"]
        paths = [ref["ref"] for ref in detail["references"]]
        self.assertIn(payload["profile_path"], paths)
        profile = orch._runtime_profile(run_id)["profile"]
        self.assertIn(profile["business_repos"][0]["path"], paths)
        self.assertIn(profile["test_repo"]["path"], paths)
        packet = json.loads(Path(paths[0]).read_text())
        self.assertEqual(packet["input_hash"], payload["g0_input_hash"])
        self.assertEqual(packet["material"]["workflow_modes"], payload["workflow_modes"])
        self.assertEqual(len(orch.state.events(run_id)), 1)
        self.assertEqual(orch.approvals.for_run(run_id), [])
        repeat = _approval_context(
            orch, run_id, action="G0", input_hash=payload["g0_input_hash"]
        )["evidence"]["gate"]
        self.assertEqual(repeat, detail)
        mismatch = _approval_context(orch, run_id, action="G0", input_hash="wrong")
        self.assertEqual(mismatch["error"]["reason_code"], "APPROVAL_CONTENT_MISMATCH")
        rendered = self._render("G0", detail)
        self.assertIn(paths[0], rendered)
        self.assertIn("审批重点", rendered)
        self.assertIn("不授权代码提交或发布", rendered)

    def _draft_orchestrator(self, *, merged=False):
        action = {
            "ok": True, "run_id": "run-1", "action_id": "action-1",
            "source_event_id": "event-1", "phase": "SPEC" if merged else "IMPLEMENT",
            "required_human_gate": "G2" if merged else "G5",
            "input_hash": "action-hash", "task_id": "T-1",
            "source_revisions": {"business": "base"}, "parent_artifact_hash": None,
        }
        content = {"change_set_id": "C-1", "revisions": {"business": "candidate"},
                   "business_patch": "diff --git a/main.c b/main.c\n+fix\n",
                   "test_patch": "diff --git a/test.py b/test.py\n+test\n",
                   "full_diff_hash": "d" * 64}
        draft = {"spec": {"version": "1"}, "dag": {"nodes": []}} if merged else content
        orch = SimpleNamespace(
            state=SimpleNamespace(producer_job=lambda _: {"status": "FULFILLED", "draft": draft}),
            next=lambda run_id, *, read_only=False: action,
        )
        return orch, action, draft

    def test_g5_reads_complete_saved_draft_only_when_envelope_hash_matches(self):
        orch, action, draft = self._draft_orchestrator()
        binding = build_envelope(action, draft)["approval_input_hash"]
        detail = pinned_context(orch, "run-1", "G5", binding)
        self.assertEqual(detail["material_status"], "PINNED")
        self.assertEqual(detail["_review_content"]["draft"], draft)
        self.assertIn("candidate", [ref["ref"] for ref in detail["references"]])
        rendered = self._render("G5", detail)
        self.assertIn("业务与测试完整 diff", rendered)
        self.assertIn("candidate", rendered)
        mismatch = pinned_context(orch, "run-1", "G5", "different")
        self.assertEqual(mismatch["material_status"], "MISSING")
        self.assertNotIn("_review_content", mismatch)

    def test_merged_g2_explains_dag_is_associated_not_separately_approved(self):
        orch, action, draft = self._draft_orchestrator(merged=True)
        binding = build_envelope(action, draft["spec"])["approval_input_hash"]
        detail = pinned_context(orch, "run-1", "G2", binding)
        self.assertEqual(detail["_review_content"]["draft"]["dag"], draft["dag"])
        self.assertIn("G2 只绑定 Spec", " ".join(detail["summary"]))

    def test_g9_reads_exact_referenced_artifacts_not_latest_build(self):
        store = ArtifactStore(self.root / "artifacts")
        refs, expected_paths = [], []
        for module in ("business", "agent"):
            content = {"module": module, "build_id": f"build-{module}",
                       "environment_fingerprint": "env-pinned"}
            saved = store.put("run-1", "test-evidence", json.dumps({"content": content}).encode(), {})
            refs.append({"artifact_id": saved["artifact_id"], "content_hash": _hash(content)})
            expected_paths.append(saved["path"])
        action = {"ok": True, "required_human_gate": "G9", "phase": "RELEASE",
                  "input_hash": "release-hash", "input_artifacts": refs}
        plan = {"version": 1, "run_id": "run-1", "required_modules": ["business", "agent"],
                "modules": {"business": {}, "agent": {}}}
        plan["plan_hash"] = _hash(plan)
        action["pipeline_plan_hash"] = plan["plan_hash"]
        orch = SimpleNamespace(next=lambda run_id: action, artifacts=store,
                               state=SimpleNamespace(
                                   producer_job=lambda _: None,
                                   events=lambda _: [{"run_id": "run-1", "state": "IPIPE",
                                                      "payload": {"pipeline_plan": plan}}],
                               ))
        detail = pinned_context(orch, "run-1", "G9", "release-hash")
        self.assertEqual(detail["material_status"], "PINNED")
        self.assertEqual(detail["_review_content"]["pipeline_plan"], plan)
        paths = [ref["ref"] for ref in detail["references"]]
        for path in expected_paths:
            self.assertIn(path, paths)
        rendered = self._render("G9", detail)
        self.assertIn("build-business", rendered)
        self.assertIn("build-agent", rendered)
        self.assertIn("不强制发布", rendered)
        refs[0]["content_hash"] = "stale"
        self.assertEqual(pinned_context(orch, "run-1", "G9", "release-hash")["material_status"], "MISSING")

    def test_references_do_not_copy_parameters_or_inline_patch_as_file_path(self):
        refs = references({"files": ["/repo/main.c"], "parameters": {"password": "secret"},
                           "business_patch": "diff --git a/x b/x\n+secret"})
        self.assertIn("/repo/main.c", [ref["ref"] for ref in refs])
        self.assertNotIn("secret", json.dumps(refs))

    def test_progress_without_ledger_row_never_tells_user_to_approve(self):
        for action in (
            {"ok": True, "phase": "INTAKE", "required_human_gate": "G0"},
            {"ok": False, "reason_code": "APPROVAL_REQUIRED"},
        ):
            orch = BriefFixture("INTAKE", action)
            snapshot = progress_snapshot.build(orch, "run-1")
            self.assertEqual(snapshot["next_action"]["owner"], "Comate")
            self.assertNotIn("APPROVE", progress_snapshot.render(snapshot))
            self.assertNotIn("处理G0审批", progress_snapshot.render(snapshot))

    def test_nested_parked_action_actually_requests_approval(self):
        orch = object()
        result = {"ok": True, "reason_code": "PARKED", "parked": "APPROVAL_WAIT",
                  "run_id": "run-1", "decision": {"action": {
                      "required_human_gate": "G0", "input_hash": "bound-hash"}}}
        with patch("orchestrator._request_approval", return_value={
            "status": "PENDING", "approval_id": "a" * 32
        }) as request:
            delivered = _deliver_worker_approval(orch, result)
        request.assert_called_once_with(orch, "run-1", "G0", "bound-hash")
        self.assertEqual(delivered["approval"]["status"], "PENDING")

    def test_frozen_context_survives_next_becoming_recovery_required(self):
        from orchestrator import _request_approval
        from test_task5_safety import _pending_delivery_receipt
        class Transport:
            ready = False
            reconciles = 0
            def request(self, payload):
                raise RuntimeError("outcome unknown")
            def reconcile(self, payload):
                self.reconciles += 1
                return _pending_delivery_receipt(payload) if self.ready else None
        _write_profile(self.root, PROFILE)
        orch = Orchestrator(self.root)
        run_id = _start(orch)["run_id"]
        _, action, draft = self._draft_orchestrator()
        action["run_id"] = run_id
        bound = build_envelope(action, draft)["approval_input_hash"]
        with patch.object(orch, "next", return_value=action), patch.object(
            orch.state, "producer_job", return_value={"status": "FULFILLED", "draft": draft}
        ):
            first = _approval_context(orch, run_id, action="G5", input_hash=bound)
            transport = Transport()
            with patch("orchestrator._infoflow_approval_client", return_value=transport):
                sent = _request_approval(orch, run_id, "G5", bound)
            self.assertEqual(sent["reason_code"], "QUERY_REQUIRED")
        with patch.object(orch, "next", return_value={
            "ok": False, "reason_code": "RECOVERY_REQUIRED"
        }):
            retry = _approval_context(orch, run_id, action="G5", input_hash=bound)
            transport.ready = True
            count = transport.reconciles
            with patch("orchestrator._infoflow_approval_client", return_value=transport):
                recovered = _request_approval(orch, run_id, "G5", bound)
            self.assertEqual(recovered["status"], "PENDING")
            self.assertGreater(transport.reconciles, count)
            self.assertEqual(orch.state.pending_intents(run_id), [])
        self.assertEqual(retry, first)
        self.assertEqual(retry["evidence"]["gate"]["material_status"], "PINNED")

    def test_explicit_envelope_recomputes_hash_and_checks_run(self):
        _write_profile(self.root, PROFILE)
        orch = Orchestrator(self.root)
        run_id = _start(orch)["run_id"]
        _, action, draft = self._draft_orchestrator()
        action["run_id"] = run_id
        envelope = build_envelope(action, draft)
        bound = envelope["approval_input_hash"]
        path = self.root / "candidate.json"
        for mutation in ("foreign-run", "other-content", "other-task"):
            bad = json.loads(json.dumps(envelope))
            if mutation == "foreign-run":
                bad["run_id"] = "another-run"
            elif mutation == "other-content":
                bad["content"]["business_patch"] = "unrelated change"
                bad["content_hash"] = _hash(bad["content"])
            else:
                bad["task_id"] = "T-other"
            path.write_text(json.dumps(bad))
            with patch.object(orch, "next", return_value=action):
                rejected = _approval_context(
                    orch, run_id, action="G5", input_hash=bound, content_path=str(path)
                )
            self.assertEqual(rejected["error"]["reason_code"], "APPROVAL_CONTENT_MISMATCH")
        path.write_text(json.dumps(envelope))
        with patch.object(orch, "next", return_value=action):
            accepted = _approval_context(
                orch, run_id, action="G5", input_hash=bound, content_path=str(path)
            )
        self.assertEqual(accepted["evidence"]["gate"]["material_status"], "PINNED")

    def test_upgrade_reuses_legacy_cli_evidence_only_on_exact_delivery_digest(self):
        from orchestrator import _icafe_url, _requirement_documents
        from approval_summary import _GATES
        _write_profile(self.root, PROFILE)
        orch = Orchestrator(self.root)
        run_id = _start(orch)["run_id"]
        intake = orch.state.events(run_id)[0]["payload"]
        binding = intake["collaboration_binding"]
        members = sorted({email for values in PROFILE["approval_channels"]["role_members"].values()
                          for email in values})
        policy = {"comate": members, "infoflow": members}
        subject, effect = _GATES["G0"]
        evidence = {
            "project": intake["project"], "card_id": binding["card_id"],
            "card_title": binding["card_title"], "card_url": _icafe_url(binding["card_id"]),
            "group_name": binding["group_name"], "documents": _requirement_documents(PROFILE),
            "gate": {"subject": subject, "effect": effect,
                     "summary": [
                         f"群名 {binding['group_name']}",
                         f"成员 {'、'.join(binding['member_snapshot'])}",
                         *(f"{role} {'、'.join(people)}" for role, people in sorted(binding["roles"].items())),
                     ], "content_hash": intake["requirement_snapshot"]["content_hash"]},
        }
        approval = orch.approvals.request(
            "G0", intake["g0_input_hash"], ["comate", "infoflow"],
            run_id=run_id, member_policy=policy,
        )
        payload = {key: approval[key] for key in
                   ("approval_id", "run_id", "action", "input_hash", "deadline_at", "member_policy")}
        payload["evidence"] = evidence
        orch.state.claim_intent(
            run_id, "approval.delivery.comate",
            f"approval.delivery:{approval['approval_id']}:comate",
            {"approval_id": approval["approval_id"],
             "canonical_payload_sha256": _hash({"channel": "comate", "approval": payload})},
        )
        restored = _approval_context(orch, run_id, action="G0", input_hash=intake["g0_input_hash"])
        self.assertEqual(restored, {"member_policy": policy, "evidence": evidence})


if __name__ == "__main__":
    unittest.main()
