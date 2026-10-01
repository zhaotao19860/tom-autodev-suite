"""Redo a phase without a new run: revise after REJECT, rewind under CHANGE_REQUEST."""

import copy
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_brief
import run_change
import worker_driver
import workflow_spec
from agent_bridge import AgentBridge
from approval_delivery import _approval_markdown
from approval_summary import pinned_context
from orchestrator import Orchestrator
import test_fake_e2e as e2e
from lock_manager import LockManager
from test_schema_validation import specialized_examples


class _Channel:
    def __init__(self):
        self.payloads = []

    def request(self, payload):
        self.payloads.append(payload)
        return {"approval_id": payload["approval"]["approval_id"],
                "input_hash": payload["approval"]["input_hash"]}


class RunChangeTests(unittest.TestCase):
    _write_profile = e2e.FakeE2ETests._write_profile
    _integrated_profile = e2e.FakeE2ETests._integrated_profile
    _approval = e2e.FakeE2ETests._approval
    _phase_envelope = e2e.FakeE2ETests._phase_envelope
    _phase_content = e2e.FakeE2ETests._phase_content
    _run_to_grill_action = e2e.FakeE2ETests._run_to_grill_action

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._write_profile("bgw", "I15ClP2KW4ZGAK", "tom-lang-c-cpp", "tom-project-bgw")
        self.orchestrator = Orchestrator(self.root)
        journal = patch.dict(os.environ, {
            "TOM_AUTODEV_INFOFLOW_REPLY_JOURNAL": str(self.root / "replies.jsonl"),
        })
        journal.start()
        self.addCleanup(journal.stop)

    def _draft(self, title_suffix=""):
        spec = copy.deepcopy(specialized_examples()["spec"])
        if title_suffix:
            spec["non_goals"] = [*spec["non_goals"], f"不改动旧分支{title_suffix}"]
        return {"spec": spec, "dag": copy.deepcopy(specialized_examples()["task-dag"])}

    def _at_g2(self, card="BGW-2001"):
        run_id, knowledge, _ = self._run_to_grill_action(card, "I15ClP2KW4ZGAK", "express")
        parked = worker_driver.advance(self.orchestrator, run_id, knowledge_sync=knowledge)
        job_id = parked["producer_job"]["job_id"]
        pending = worker_driver.submit_draft(
            self.orchestrator, run_id, job_id, self._draft(), knowledge_sync=knowledge
        )
        self.assertEqual((pending["reason_code"], pending["gate"]), ("APPROVAL_REQUIRED", "G2"))
        return run_id, knowledge, job_id, pending["approval_input_hash"]

    def test_reject_with_feedback_redrafts_the_same_phase_and_asks_g2_again(self):
        run_id, knowledge, job_id, rejected_hash = self._at_g2()
        rejected = self._approval(run_id, "G2", rejected_hash, decision="REJECT")
        brief = run_brief.build(self.orchestrator, run_id, include_progress=False)
        self.assertEqual(brief["status_label"], "已驳回待修改")
        self.assertIn("revise", brief["next"]["text"])

        missing = run_change.revise(
            self.orchestrator, run_id, rejected["approval_id"], feedback=None, actor="owner@example.test"
        )
        self.assertEqual(missing["reason_code"], "FEEDBACK_REQUIRED")
        before = self.orchestrator.next(run_id, read_only=True)

        revised = run_change.revise(
            self.orchestrator, run_id, rejected["approval_id"],
            feedback="测试仓库改用新分支", actor="owner@example.test",
        )
        self.assertEqual((revised["ok"], revised["state"]), (True, "SPEC"), revised)
        replay = run_change.revise(
            self.orchestrator, run_id, rejected["approval_id"],
            feedback="另一条意见", actor="owner@example.test",
        )
        self.assertEqual(replay["reason_code"], "ALREADY_REVISED")
        self.assertEqual(len([e for e in self.orchestrator.state.events(run_id) if e["state"] == "SPEC"]), 2)

        after = self.orchestrator.next(run_id, read_only=True)
        self.assertNotEqual(after["action_id"], before["action_id"])
        self.assertEqual(after["reentry"]["feedback"], "测试仓库改用新分支")
        self.assertEqual(after["reentry"]["previous_job_id"], job_id)

        locks = LockManager(self.root / "locks.sqlite")
        context = AgentBridge(self.orchestrator, locks=locks).context(run_id)
        self.assertEqual(context["revision_context"]["instruction"], "测试仓库改用新分支")
        self.assertEqual(context["revision_context"]["previous_draft"], self._draft())

        parked = worker_driver.advance(self.orchestrator, run_id, knowledge_sync=knowledge)
        new_job = parked["producer_job"]
        self.assertNotEqual(new_job["job_id"], job_id)
        self.assertEqual(new_job["payload"]["reentry"]["kind"], "REVISE")
        redraft = self._draft("（新分支）")
        pending = worker_driver.submit_draft(
            self.orchestrator, run_id, new_job["job_id"], redraft, knowledge_sync=knowledge
        )
        self.assertEqual(pending["reason_code"], "APPROVAL_REQUIRED")
        self.assertNotEqual(pending["approval_input_hash"], rejected_hash)

        detail = pinned_context(self.orchestrator, run_id, "G2", pending["approval_input_hash"])
        self.assertEqual(detail["_review_content"]["previous_draft"], self._draft())
        rendered = _approval_markdown({
            "approval_id": "a" * 32, "run_id": run_id, "action": "G2",
            "input_hash": pending["approval_input_hash"],
            "deadline_at": "2026-09-30T00:00:00+00:00",
            "evidence": {"card_id": "BGW-2001", "gate": detail},
        }, ["owner"])
        self.assertIn("本次修改说明", rendered)
        self.assertIn("测试仓库改用新分支", rendered)
        self.assertIn("与上一版的差异", rendered)
        self.assertIn("（新分支）", rendered)
        self.assertIn(f"REJECT {'a' * 32} <你的修改意见>", rendered)

        approved = self._approval(run_id, "G2", pending["approval_input_hash"])
        done = worker_driver.submit_draft(
            self.orchestrator, run_id, new_job["job_id"], redraft, knowledge_sync=knowledge
        )
        self.assertEqual(done["reason_code"], "MERGED_COMPLETE", done)
        self.assertTrue(approved["approval_id"])

    def test_revise_only_accepts_the_reject_of_the_draft_being_waited_on(self):
        run_id, _knowledge, _job_id, rejected_hash = self._at_g2()
        approved = self._approval(run_id, "G2", rejected_hash)
        self.assertEqual(run_change.revise(
            self.orchestrator, run_id, approved["approval_id"], feedback="x", actor="a",
        )["reason_code"], "APPROVAL_NOT_REJECTED")
        other = self._approval(run_id, "G2", "f" * 64, decision="REJECT")
        self.assertEqual(run_change.revise(
            self.orchestrator, run_id, other["approval_id"], feedback="x", actor="a",
        )["reason_code"], "APPROVAL_STALE")
        wrong_gate = self._approval(run_id, "G3", rejected_hash, decision="REJECT")
        self.assertEqual(run_change.revise(
            self.orchestrator, run_id, wrong_gate["approval_id"], feedback="x", actor="a",
        )["reason_code"], "APPROVAL_GATE_MISMATCH")

    def test_feedback_typed_after_reject_in_infoflow_is_used(self):
        run_id, _knowledge, _job_id, rejected_hash = self._at_g2()
        rejected = self._approval(run_id, "G2", rejected_hash, decision="REJECT")
        journal = Path(os.environ["TOM_AUTODEV_INFOFLOW_REPLY_JOURNAL"])
        lines = [
            {"message_id": "m1", "sender": "someone-else", "received_at": "2026-09-30T00:00:00+00:00",
             "text": f"REJECT {rejected['approval_id']} 别人的意见"},
            {"message_id": "m2", "sender": "owner", "received_at": "2026-09-30T00:00:01+00:00",
             "text": f"REJECT {rejected['approval_id']} 验收场景缺少回滚"},
        ]
        import json

        journal.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
        revised = run_change.revise(
            self.orchestrator, run_id, rejected["approval_id"], feedback=None, actor="owner@example.test",
        )
        self.assertEqual(revised["feedback"], "验收场景缺少回滚")

    def _journal(self, *texts, sender="owner"):
        import json

        journal = Path(os.environ["TOM_AUTODEV_INFOFLOW_REPLY_JOURNAL"])
        journal.write_text("\n".join(json.dumps({
            "message_id": f"m{index}", "sender": sender, "text": text,
            "received_at": f"2026-09-30T00:00:0{index}+00:00",
        }, ensure_ascii=False) for index, text in enumerate(texts)), encoding="utf-8")

    def test_copied_placeholder_is_no_feedback_and_only_the_leading_reject_is_stripped(self):
        run_id, _knowledge, _job_id, rejected_hash = self._at_g2()
        rejected = self._approval(run_id, "G2", rejected_hash, decision="REJECT")
        self._journal(f"REJECT {rejected['approval_id']} <你的修改意见>")
        self.assertEqual(run_change.revise(
            self.orchestrator, run_id, rejected["approval_id"], feedback=None, actor="a",
        )["reason_code"], "FEEDBACK_REQUIRED")
        self._journal(f"REJECT {rejected['approval_id']} reject path needs rollback")
        revised = run_change.revise(
            self.orchestrator, run_id, rejected["approval_id"], feedback=None, actor="a",
        )
        self.assertEqual(revised["feedback"], "reject path needs rollback")

    def test_a_rewind_that_loses_the_race_moves_neither_the_pin_nor_the_run(self):
        import profile_repin

        run_id, _knowledge, _job_id, _hash = self._at_g2()
        previous = self._edit_test_branch("feature/race")
        opened = run_change.request(
            self.orchestrator, run_id, "SPEC", "换分支", "owner@example.test", previous,
            comate_client=_Channel(), infoflow_client=_Channel(),
        )
        approval_id = opened["approval"]["approval_id"]
        self._settle(approval_id, opened["input_hash"], run_id)
        self.orchestrator.state.transition(run_id, "SPEC", {})
        moved = run_change.apply(self.orchestrator, run_id, approval_id)
        self.assertIn(moved["reason_code"], {"RUN_MOVED", "CHANGE_REQUEST_DRIFTED"})
        self.assertIsNone(profile_repin.record_for(self.orchestrator.state, run_id))

    def test_rewind_commits_pin_event_and_result_together(self):
        import profile_repin

        run_id, _knowledge, _job_id, _hash = self._at_g2()
        previous = self._edit_test_branch("feature/atomic")
        opened = run_change.request(
            self.orchestrator, run_id, "SPEC", "换分支", "owner@example.test", previous,
            comate_client=_Channel(), infoflow_client=_Channel(),
        )
        approval_id = opened["approval"]["approval_id"]
        self._settle(approval_id, opened["input_hash"], run_id)
        original = self.orchestrator.state.commit_transition_result

        def crash_after_commit(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("crash after commit")

        with patch.object(self.orchestrator.state, "commit_transition_result", crash_after_commit):
            with self.assertRaises(RuntimeError):
                run_change.apply(self.orchestrator, run_id, approval_id)
        landed = profile_repin.record_for(self.orchestrator.state, run_id)
        self.assertEqual(landed["approval_id"], approval_id)
        self.assertEqual(self.orchestrator.state.events(run_id)[-1]["payload"]["reentry"]["approval_id"], approval_id)
        self.assertEqual(run_change.apply(self.orchestrator, run_id, approval_id)["reason_code"], "ALREADY_REWOUND")

    def test_feedback_split_over_messages_is_joined_in_order(self):
        run_id, _knowledge, _job_id, rejected_hash = self._at_g2()
        rejected = self._approval(run_id, "G2", rejected_hash, decision="REJECT")
        self._journal(
            f"REJECT {rejected['approval_id']} 补回滚场景",
            "无关消息",
            f"REJECT {rejected['approval_id']} 环境要求写清楚",
        )
        revised = run_change.revise(
            self.orchestrator, run_id, rejected["approval_id"], feedback=None, actor="a",
        )
        self.assertEqual(revised["feedback"], "补回滚场景；环境要求写清楚")

    def test_extra_results_conflict_writes_nothing(self):
        run_id, _knowledge, _job_id, _hash = self._at_g2()
        state = self.orchestrator.state
        state.save_idempotency_result("held", {"value": 1})
        current = state.events(run_id)[-1]
        outcome = state.commit_transition_result(
            run_id, current["event_id"], "SPEC", {}, "result-key", {"ok": True},
            extra_results=[("held", {"value": 2})],
        )
        self.assertEqual(outcome["status"], "RESULT_CONFLICT")
        self.assertEqual(state.events(run_id)[-1]["event_id"], current["event_id"])
        self.assertIsNone(state.idempotency_result("result-key"))

    def test_workspace_that_already_had_code_cannot_rewind(self):
        run_id, _knowledge, _job_id, _hash = self._at_g2()
        for state in ("WORKSPACE", "PLAN", "IMPLEMENT", "REVIEW", "WORKSPACE"):
            self.orchestrator.state.transition(run_id, state, {})
        result = run_change.plan(self.orchestrator, run_id, "SPEC", "改设计", "owner@example.test")
        self.assertEqual(result["reason_code"], "REWIND_AFTER_CODE_FORBIDDEN")

    def test_acknowledgements_name_the_step_that_really_follows(self):
        from approval_watch import _ack_note

        change = {"approval_id": "c" * 32, "action": "CHANGE_REQUEST"}
        self.assertIn("rewind --approval-id", _ack_note(change, "APPROVE", False, None))
        gate = {"approval_id": "g" * 32, "action": "G2#retry-1"}
        self.assertIn("revise", _ack_note(gate, "REJECT", False, None))
        self.assertIn("已按意见登记重做", _ack_note(gate, "REJECT", False, {"feedback": "补回滚"}))
        self.assertIn("取代", _ack_note(gate, "APPROVE", True, None))
        self.assertIsNone(_ack_note({"approval_id": "x", "action": "G7"}, "REJECT", False, None))

    def test_pending_change_request_is_listed_and_an_approved_one_says_apply(self):
        run_id, _knowledge, _job_id, _hash = self._at_g2()
        opened = run_change.request(
            self.orchestrator, run_id, "GRILL", "重新澄清", "owner@example.test",
            comate_client=_Channel(), infoflow_client=_Channel(),
        )
        approval_id = opened["approval"]["approval_id"]
        for intent in self.orchestrator.state.pending_intents(run_id):
            self.orchestrator.state.receipt(intent["intent_id"], {"approval_id": approval_id}, [])
        brief = run_brief.build(self.orchestrator, run_id, include_progress=False)
        self.assertIn(approval_id, [gate["approval_id"] for gate in brief["waiting_on"]])
        self._settle(approval_id, opened["input_hash"], run_id)
        brief = run_brief.build(self.orchestrator, run_id, include_progress=False)
        self.assertEqual(brief["status_label"], "变更已批准待执行")
        self.assertIn(f"rewind --approval-id {approval_id}", brief["next"]["text"])

    def _settle(self, approval_id, input_hash, run_id):
        for channel in ("comate", "infoflow"):
            self.orchestrator.approvals.record_delivery(
                approval_id, channel, {"request_id": f"{channel}-change"}, payload_hash=input_hash,
            )
        self.orchestrator.approvals.resolve(
            approval_id, "APPROVE", input_hash, "comate", run_id=run_id,
            responder="owner@example.test", state_store=self.orchestrator.state,
        )

    def _edit_test_branch(self, branch):
        path = self.root / "config" / "projects" / "bgw.yaml"
        previous = self.root / "bgw.pinned.yaml"
        shutil.copyfile(path, previous)
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
        profile["test_repo"]["branch"] = branch
        path.write_text(yaml.safe_dump(profile), encoding="utf-8")
        return previous

    def test_rewind_moves_test_repo_with_one_change_request_and_redoes_from_target(self):
        run_id, knowledge, _job_id, pending_hash = self._at_g2()
        stale_g2 = self.orchestrator.approvals.request(
            "G2", pending_hash, ["comate", "infoflow"], run_id=run_id,
            member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
        )
        previous = self._edit_test_branch("feature/new-cases")
        self.assertEqual(self.orchestrator.next(run_id, read_only=True)["reason_code"], "PROFILE_CONFLICT")

        undeclared = run_change.plan(self.orchestrator, run_id, "SPEC", "换测试分支", "owner@example.test")
        self.assertEqual(undeclared["reason_code"], "PROFILE_CHANGE_UNDECLARED")
        forbidden = run_change.plan(
            self.orchestrator, run_id, "TASKS", "换测试分支", "owner@example.test", previous
        )
        self.assertEqual(forbidden["reason_code"], "REWIND_TARGET_FORBIDDEN")

        comate, infoflow = _Channel(), _Channel()
        opened = run_change.request(
            self.orchestrator, run_id, "SPEC", "测试仓库换到新分支", "owner@example.test", previous,
            comate_client=comate, infoflow_client=infoflow,
        )
        self.assertEqual(opened["reason_code"], "OK", opened)
        self.assertEqual(opened["profile"]["changed_keys"], ["test_repo"])
        self.assertEqual(opened["redo"], ["SPEC"])
        approval_id = opened["approval"]["approval_id"]
        detail = pinned_context(self.orchestrator, run_id, "CHANGE_REQUEST", opened["input_hash"])
        self.assertEqual(detail["material_status"], "PINNED")

        self.assertEqual(run_change.apply(self.orchestrator, run_id, approval_id)["reason_code"],
                         "APPROVAL_REQUIRED")
        self._settle(approval_id, opened["input_hash"], run_id)
        # The fake infoflow channel answers with no gateway result, which leaves its
        # delivery claim open; a real delivery closes it with the receipt.
        for intent in self.orchestrator.state.pending_intents(run_id):
            self.orchestrator.state.receipt(intent["intent_id"], {"approval_id": approval_id}, [])
        applied = run_change.apply(self.orchestrator, run_id, approval_id)
        self.assertEqual((applied["ok"], applied.get("state")), (True, "SPEC"), applied)
        self.assertEqual(run_change.apply(self.orchestrator, run_id, approval_id)["reason_code"],
                         "ALREADY_REWOUND")

        action = self.orchestrator.next(run_id, read_only=True)
        self.assertEqual((action["reason_code"], action.get("state")), ("OK", "SPEC"), action)
        self.assertEqual(action["reentry"]["kind"], "REWIND")
        self.assertTrue(run_change.superseded(
            self.orchestrator.state, run_id, self.orchestrator.approvals.get(stale_g2["approval_id"])
        ))
        brief = run_brief.build(self.orchestrator, run_id, include_progress=False)
        self.assertEqual(brief["waiting_on"], [])

    def test_change_request_is_void_once_the_run_moves(self):
        run_id, _knowledge, _job_id, _hash = self._at_g2()
        prepared = run_change.request(
            self.orchestrator, run_id, "GRILL", "重新澄清", "owner@example.test",
            comate_client=_Channel(), infoflow_client=_Channel(),
        )
        approval_id = prepared["approval"]["approval_id"]
        self._settle(approval_id, prepared["input_hash"], run_id)
        self.orchestrator.state.transition(run_id, "SPEC", {})
        self.assertEqual(run_change.apply(self.orchestrator, run_id, approval_id)["reason_code"], "RUN_MOVED")

    def test_repository_keys_cannot_move_after_a_workspace_was_bound(self):
        run_id, _knowledge, _job_id, _hash = self._at_g2()
        self.orchestrator.state.transition(run_id, "WORKSPACE", {})
        previous = self._edit_test_branch("feature/late")
        result = run_change.plan(
            self.orchestrator, run_id, "SPEC", "换分支", "owner@example.test", previous
        )
        self.assertEqual(result["reason_code"], "REPIN_FIELD_FORBIDDEN")

    def test_architecture_review_is_left_only_by_an_approved_rewind(self):
        run_id, _knowledge, _job_id, _hash = self._at_g2()
        self.orchestrator.state.transition(run_id, "ARCHITECTURE_REVIEW", {})
        brief = run_brief.build(self.orchestrator, run_id, include_progress=False)
        self.assertIn("rewind", brief["next"]["text"])
        planned = run_change.plan(self.orchestrator, run_id, "SPEC", "接口需重设计", "owner@example.test")
        self.assertEqual(planned["reason_code"], "OK")
        self.assertEqual(planned["redo"], ["SPEC", "TASKS"])
        self.assertEqual(
            run_change.plan(self.orchestrator, run_id, "PLAN", "x", "a")["reason_code"],
            "REWIND_TARGET_FORBIDDEN",
        )

    def test_rewind_policy_is_part_of_the_workflow_pin(self):
        original = workflow_spec.canonical_hash()
        with patch.dict(workflow_spec.REWIND_TARGETS, {"SPEC": frozenset({"SPEC"})}):
            self.assertNotEqual(workflow_spec.canonical_hash(), original)

    def test_superseded_gate_is_not_reissued(self):
        run_id, _knowledge, _job_id, pending_hash = self._at_g2()
        row = self.orchestrator.approvals.request(
            "G2", pending_hash, ["comate", "infoflow"], run_id=run_id,
            member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
        )
        current = self.orchestrator.state.events(run_id)[-1]
        self.orchestrator.state.transition(run_id, "SPEC", {
            "reentry": {"kind": "REWIND", "from_state": "SPEC", "from_event_id": current["event_id"]},
        })
        self.assertTrue(run_change.superseded(
            self.orchestrator.state, run_id, self.orchestrator.approvals.get(row["approval_id"])
        ))
        self.orchestrator.approvals.for_run = lambda _run: [{
            **self.orchestrator.approvals.get(row["approval_id"]), "effective_decision": "TIMEOUT",
        }]
        result = self.orchestrator.reissue_infoflow_approval(
            run_id, "G2", pending_hash,
            member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
            infoflow_client=object(),
        )
        self.assertEqual(result["reason_code"], "APPROVAL_SUPERSEDED")


if __name__ == "__main__":
    unittest.main()
