"""The three moves the x86bgw CDN-URL run had to make outside the CLI.

Every one of them was reachable only by hand: the state machine advanced from a
`python3 -c` call with a pasted `input_hash` (mistyped once), an archived artifact was
read by guessing its path under `artifacts/` and `cat`-ing it past the hash check, and a
stuck intent was cleared with `DELETE FROM external_intents` against the live database.
What is pinned here is that the supported commands do those things without the hand:
the hash comes from the ledger, the read is integrity-checked, and the abandonment is
written rather than deleted.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

from orchestrator import Orchestrator, main
from test_orchestrator import PROFILE, _approve_for_run, _start, _write_profile


class CliOperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _write_profile(self.root, PROFILE)
        self.orchestrator = Orchestrator(self.root)
        self.run_id = _start(self.orchestrator)["run_id"]

    def _run(self, *arguments):
        output = io.StringIO()
        with (
            patch("orchestrator.Orchestrator", return_value=self.orchestrator),
            contextlib.redirect_stdout(output),
        ):
            code = main(["--config-root", str(self.root), *arguments])
        return code, json.loads(output.getvalue())

    def test_advance_takes_the_gates_hash_from_the_ledger_instead_of_the_operator(self):
        approval = _approve_for_run(self.orchestrator, self.run_id, "G0", "grill-hash")

        code, result = self._run(
            "advance", self.run_id, "GRILL",
            "--artifact", "requirement-snapshot", "--artifact", "collaboration-session",
        )
        recorded = self.orchestrator.status(self.run_id)["events"][-1]["payload"]

        self.assertEqual((code, result["state"]), (0, "GRILL"))
        # Nothing on the command line said "grill-hash": the approved row did.
        self.assertEqual(recorded["input_hash"], "grill-hash")
        self.assertEqual(recorded["evidence"]["approval_id"], approval["approval_id"])

    def test_advance_without_an_approved_gate_asks_for_it_rather_than_guessing_a_hash(self):
        code, result = self._run(
            "advance", self.run_id, "GRILL",
            "--artifact", "requirement-snapshot", "--artifact", "collaboration-session",
        )

        self.assertEqual((code, result["reason_code"]), (1, "APPROVAL_REQUIRED"))
        self.assertEqual(result["missing_evidence"], ["G0"])
        self.assertEqual(self.orchestrator.status(self.run_id)["state"], "INTAKE")

    def test_advance_leaves_the_artifact_claim_with_the_operator_and_the_gate_names_it(self):
        """Deriving the list would be the lie: these are evidence names, not artifact kinds.

        Nothing ever calls `put(kind="requirement-snapshot")`, so a name-to-kind guess
        would make the gate read as though it had checked the archive. Omitting them
        gets the requirement back by name instead.
        """
        _approve_for_run(self.orchestrator, self.run_id, "G0", "grill-hash")

        code, result = self._run("advance", self.run_id, "GRILL")

        self.assertEqual((code, result["reason_code"]), (1, "MISSING_ARTIFACT"))
        self.assertEqual(
            result["missing_evidence"], ["requirement-snapshot", "collaboration-session"]
        )

    def test_advance_prefers_a_newly_approved_gate_over_the_one_that_timed_out(self):
        _approve_for_run(self.orchestrator, self.run_id, "G0", "stale-hash")
        reissued = _approve_for_run(self.orchestrator, self.run_id, "G0", "fresh-hash")

        code, result = self._run(
            "advance", self.run_id, "GRILL",
            "--artifact", "requirement-snapshot", "--artifact", "collaboration-session",
        )
        recorded = self.orchestrator.status(self.run_id)["events"][-1]["payload"]

        self.assertEqual((code, result["state"]), (0, "GRILL"))
        self.assertEqual(recorded["input_hash"], "fresh-hash")
        self.assertEqual(recorded["evidence"]["approval_id"], reissued["approval_id"])

    def test_abandoning_an_intent_writes_the_giving_up_instead_of_deleting_the_row(self):
        intent = self.orchestrator.state.intent(
            self.run_id, "collaboration.notify", "notify:card", {"card": "BGW-1"}
        )

        code, result = self._run(
            "abandon-intent", intent["intent_id"],
            "--reason", "如流机器人下线，人工在群里通知了",
            "--actor", "manager@example.test",
        )
        results = self.orchestrator.state.external_results(self.run_id)

        self.assertEqual((code, result["status"]), (0, "ABANDONED"))
        # Unblocked, because `pending_intents` means "intent with no receipt"...
        self.assertEqual(self.orchestrator.state.pending_intents(self.run_id), [])
        # ...and still on the record, which the three raw DELETEs destroyed.
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["intent"]["intent_id"], intent["intent_id"])
        self.assertEqual(results[0]["receipt"]["response"]["reason_code"], "INTENT_ABANDONED")
        self.assertEqual(
            results[0]["receipt"]["response"]["actor"], "manager@example.test"
        )

    def test_abandoning_a_reconciled_intent_fails_closed_without_touching_the_receipt(self):
        intent = self.orchestrator.state.intent(
            self.run_id, "ipipe.trigger", "trigger:build", {"pipeline": "bgw-pipeline"}
        )
        self.orchestrator.state.receipt(
            intent["intent_id"], {"ok": True, "build_id": "build-1"}, ["ipipe:build-1/job-1"]
        )

        code, result = self._run(
            "abandon-intent", intent["intent_id"], "--reason", "give up", "--actor", "dev",
        )
        stored = self.orchestrator.state.external_results(self.run_id)[0]

        self.assertEqual((code, result["reason_code"]), (1, "INTENT_ALREADY_RECONCILED"))
        self.assertEqual(stored["receipt"]["response"], {"ok": True, "build_id": "build-1"})

    def test_artifact_show_reads_content_back_through_the_hash_check(self):
        archived = self.orchestrator.artifacts.put(
            self.run_id, "spec", b'{"acceptance":["AC-1"]}', {"spec_version": "1"}
        )

        code, result = self._run("artifact", "show", archived["artifact_id"])

        self.assertEqual((code, result["kind"]), (0, "spec"))
        self.assertEqual(json.loads(result["content"]), {"acceptance": ["AC-1"]})
        self.assertEqual(result["sha256"], archived["sha256"])

    def test_artifact_show_refuses_tampered_bytes_rather_than_printing_them(self):
        archived = self.orchestrator.artifacts.put(self.run_id, "spec", b"trusted", {})
        Path(archived["path"]).write_bytes(b"tampered")

        code, result = self._run("artifact", "show", archived["artifact_id"])
        missing_code, missing = self._run("artifact", "show", "no-such-artifact")

        self.assertEqual((code, result["reason_code"]), (1, "ARTIFACT_CONTENT_HASH_MISMATCH"))
        self.assertNotIn("content", result)
        self.assertEqual((missing_code, missing["reason_code"]), (1, "ARTIFACT_NOT_FOUND"))


if __name__ == "__main__":
    unittest.main()
