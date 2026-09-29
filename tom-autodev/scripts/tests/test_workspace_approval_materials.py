"""UX-03: read actual controller G4 bindings without preparing any workspaces."""
import hashlib
import json
import sqlite3
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker_driver
from approval_summary import _hash, pinned_context
from orchestrator import _approval_context
from phase_protocol import PhaseProtocol
from workspace_manager import _ownership_database
import test_fake_e2e


class WorkspaceApprovalMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_fake_e2e.FakeE2ETests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.orch = self.fixture.orchestrator
        self.run_id, self.knowledge = self.fixture._run_to_workspace(
            "BGW-UX03", "bgw", "I15ClP2KW4ZGAK"
        )

    def _prepare(self):
        result = worker_driver.execute_controller(
            self.orch, self.run_id, knowledge_sync=self.knowledge
        )
        self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED", result)
        self.assertEqual(result["gate"], "G4")
        return result

    def _files(self):
        return {
            str(path.relative_to(self.fixture.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.fixture.root.rglob("*") if path.is_file()
        }

    def _read(self, input_hash, *, run_id=None):
        before = self._files()
        original_next = PhaseProtocol.next

        def read_only_next(protocol, requested_run, *, read_only=False):
            self.assertTrue(read_only, "material lookup must not use a writable next oracle")
            return original_next(protocol, requested_run, read_only=True)

        with ExitStack() as stack:
            stack.enter_context(patch.object(PhaseProtocol, "next", read_only_next))
            for target, method in (
                (self.orch.workspaces, "create"),
                (self.orch.workspaces, "inspect"),
                (self.orch.state, "save_idempotency_result"),
                (self.orch.state, "transition"),
                (self.orch.artifacts, "put"),
                (worker_driver, "execute_controller"),
            ):
                stack.enter_context(patch.object(
                    target, method, side_effect=AssertionError(f"unexpected write/preparation: {method}")
                ))
            result = pinned_context(self.orch, run_id or self.run_id, "G4", input_hash)
        self.assertEqual(self._files(), before, "material lookup changed fixture files")
        return result

    def _assert_missing(self, detail):
        self.assertEqual(detail["material_status"], "MISSING")
        self.assertEqual(detail["references"], [])
        self.assertNotIn("_review_content", detail)

    def test_controller_binding_is_pinned_read_only_and_excludes_owner_tokens(self):
        needed = self._prepare()
        binding = self.orch.workspace_binding(self.run_id, needed["workspace_receipts"])
        self.assertTrue(binding["ok"], binding)
        self.assertNotEqual(
            self.orch.next(self.run_id, read_only=True)["input_hash"],
            needed["approval_input_hash"],
        )
        detail = self._read(needed["approval_input_hash"])
        self.assertEqual(detail["material_status"], "PINNED")
        self.assertEqual(detail["_review_content"], {
            "gate": "G4", "workspace_binding": binding["workspace_binding"],
        })
        self.assertEqual(_hash(detail["_review_content"]), needed["approval_input_hash"])
        self.assertEqual(self._read(needed["approval_input_hash"]), detail)
        encoded = json.dumps(detail)
        self.assertNotIn("owner_token", encoded)
        for receipt in needed["workspace_receipts"].values():
            self.assertNotIn(receipt["owner_token"], encoded)
            self.assertIn(receipt["baseline_revision"], encoded)
            self.assertIn(receipt["worktree_path"], [ref["ref"] for ref in detail["references"]])

    def test_approval_context_archives_the_verified_binding_as_openable_json(self):
        needed = self._prepare()
        context = _approval_context(
            self.orch, self.run_id, action="G4", input_hash=needed["approval_input_hash"]
        )
        detail = context["evidence"]["gate"]
        self.assertEqual(detail["material_status"], "PINNED")
        packet = json.loads(Path(detail["references"][0]["ref"]).read_text())
        self.assertEqual(packet["run_id"], self.run_id)
        self.assertEqual(packet["input_hash"], needed["approval_input_hash"])
        self.assertEqual(_hash(packet["material"]), needed["approval_input_hash"])
        self.assertEqual(packet["material"]["workspace_binding"]["run_id"], self.run_id)
        self.assertNotIn("owner_token", json.dumps(packet))

    def test_wrong_hash_and_generic_action_hash_are_not_workspace_approval_materials(self):
        needed = self._prepare()
        action_hash = self.orch.next(self.run_id, read_only=True)["input_hash"]
        for wrong in ("0" * 64, action_hash):
            with self.subTest(input_hash=wrong):
                self.assertNotEqual(wrong, needed["approval_input_hash"])
                self._assert_missing(self._read(wrong))

    def test_unprepared_workspace_stays_missing_without_creating_reservations(self):
        self._assert_missing(self._read("0" * 64))
        self.assertFalse(_ownership_database(self.orch.workspaces.worktree_root).exists())

    def test_inactive_or_foreign_ownership_cannot_supply_the_current_binding(self):
        needed = self._prepare()
        database = _ownership_database(self.orch.workspaces.worktree_root)
        for field, value in (("status", "RELEASED"), ("run_id", "foreign-run"), ("task_id", "foreign-task")):
            with self.subTest(field=field):
                with sqlite3.connect(database) as connection:
                    original = connection.execute(
                        f"SELECT {field} FROM worktree_ownership WHERE run_id = ? LIMIT 1",
                        (self.run_id,),
                    ).fetchone()[0]
                    connection.execute(
                        f"UPDATE worktree_ownership SET {field} = ? WHERE run_id = ?",
                        (value, self.run_id),
                    )
                self._assert_missing(self._read(needed["approval_input_hash"]))
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        f"UPDATE worktree_ownership SET {field} = ? WHERE {field} = ?",
                        (original, value),
                    )

    def test_existing_ownership_must_still_pass_live_read_only_verification(self):
        needed = self._prepare()
        with patch.object(self.orch.workspaces, "query_ownership", return_value={
            "status": "BLOCKED", "reason_code": "WORKTREE_ACTIVE_MISMATCH",
        }) as verify:
            self._assert_missing(self._read(needed["approval_input_hash"]))
        verify.assert_called()


if __name__ == "__main__":
    unittest.main()
