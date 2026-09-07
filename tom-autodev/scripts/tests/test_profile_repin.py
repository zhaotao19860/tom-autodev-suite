import copy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

import profile_repin
from orchestrator import Orchestrator
from test_orchestrator import PROFILE, _start, _write_profile


class ProfileRepinTests(unittest.TestCase):
    """Moving the pin is what makes an edited profile cheaper than a new run."""

    def _prepared(self, directory):
        root = Path(directory)
        _write_profile(root, PROFILE)
        orchestrator = Orchestrator(root)
        run_id = _start(orchestrator)["run_id"]
        profile_path = root / "config" / "projects" / "bgw.yaml"
        previous = root / "bgw.pinned.yaml"
        shutil.copyfile(profile_path, previous)
        return orchestrator, run_id, profile_path, previous

    @staticmethod
    def _edit(path, mutate):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(loaded)
        path.write_text(yaml.safe_dump(loaded), encoding="utf-8")

    def _approve(self, orchestrator, run_id, input_hash, *, action=profile_repin.ACTION,
                 decision="APPROVE"):
        request = orchestrator.approvals.request(
            action, input_hash, ["comate", "infoflow"], run_id=run_id,
            member_policy={"comate": ["a@example.test"], "infoflow": ["a@example.test"]},
        )
        for channel in ("comate", "infoflow"):
            # A decision is only accepted once the card is on record as delivered.
            orchestrator.approvals.record_delivery(
                request["approval_id"], channel, {"message_key": f"m-{channel}"},
                payload_hash=input_hash,
            )
        for channel in ("comate", "infoflow"):
            orchestrator.approvals.resolve(
                request["approval_id"], decision, input_hash, channel,
                run_id=run_id, responder="a@example.test",
            )
        return request["approval_id"]

    def test_an_untouched_profile_has_nothing_to_repin(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, run_id, _path, previous = self._prepared(directory)

            result = profile_repin.plan(orchestrator, run_id, previous)

        self.assertEqual(result["reason_code"], "NOTHING_TO_REPIN")

    def test_a_pipeline_edit_is_repinnable_and_restores_the_run(self):
        """Both pin checks and the action cache have to honour the same move.

        `phase_protocol` keeps its own copy of the check and embeds the pinned hash in
        the action's identity, so a re-pin that only `_runtime_profile` sees leaves
        `next` reporting PROFILE_CONFLICT and then ACTION_CONFLICT.
        """
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, run_id, path, previous = self._prepared(directory)
            before = orchestrator.next(run_id)["reason_code"]
            self._edit(path, lambda p: p["pipeline_profile"].update({"pipeline_id": "348142"}))

            broken = orchestrator._runtime_profile(run_id)
            broken_next = orchestrator.next(run_id)["reason_code"]
            prepared = profile_repin.plan(orchestrator, run_id, previous)
            approval_id = self._approve(orchestrator, run_id, prepared["input_hash"])
            applied = profile_repin.apply(orchestrator, run_id, approval_id, previous)
            healed = orchestrator._runtime_profile(run_id)
            healed_next = orchestrator.next(run_id)
            replay = profile_repin.apply(orchestrator, run_id, approval_id, previous)

        self.assertEqual(before, "OK")
        self.assertEqual(broken["reason_code"], "PROFILE_CONFLICT")
        self.assertEqual(broken_next, "PROFILE_CONFLICT")
        self.assertEqual(prepared["changed_keys"], ["pipeline_profile"])
        self.assertEqual(applied["reason_code"], "OK")
        self.assertEqual(healed["reason_code"], "OK")
        self.assertEqual(healed_next["reason_code"], "OK")
        self.assertEqual(healed["profile_hash"], prepared["new_hash"])
        self.assertEqual(healed["profile"]["pipeline_profile"]["pipeline_id"], "348142")
        self.assertEqual(replay["reason_code"], "ALREADY_REPINNED")

    def test_a_field_an_existing_binding_hashed_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, run_id, path, previous = self._prepared(directory)
            self._edit(path, lambda p: p["environment_profile"].update({"capacity": "large"}))

            result = profile_repin.plan(orchestrator, run_id, previous)

        self.assertEqual(result["reason_code"], "REPIN_FIELD_FORBIDDEN")
        self.assertEqual(result["forbidden"], ["environment_profile"])

    def test_repointing_the_repos_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, run_id, path, previous = self._prepared(directory)
            self._edit(path, lambda p: p["business_repos"][0].update({"branch": "other"}))

            result = profile_repin.plan(orchestrator, run_id, previous)

        self.assertEqual(result["reason_code"], "REPIN_FIELD_FORBIDDEN")
        self.assertEqual(result["forbidden"], ["business_repos"])

    def test_a_previous_copy_that_is_not_the_pinned_one_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, run_id, path, previous = self._prepared(directory)
            self._edit(path, lambda p: p["pipeline_profile"].update({"pipeline_id": "348142"}))
            previous.write_text("project_id: bgw\n", encoding="utf-8")

            result = profile_repin.plan(orchestrator, run_id, previous)

        self.assertEqual(result["reason_code"], "PREVIOUS_PROFILE_MISMATCH")

    def test_the_approval_must_be_bound_to_this_exact_move(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, run_id, path, previous = self._prepared(directory)
            self._edit(path, lambda p: p["pipeline_profile"].update({"pipeline_id": "348142"}))
            prepared = profile_repin.plan(orchestrator, run_id, previous)

            wrong_hash = self._approve(orchestrator, run_id, "f" * 64)
            wrong_gate = self._approve(orchestrator, run_id, prepared["input_hash"], action="G7")
            rejected = self._approve(
                orchestrator, run_id, prepared["input_hash"], decision="REJECT"
            )
            outcomes = [
                profile_repin.apply(orchestrator, run_id, wrong_hash, previous)["reason_code"],
                profile_repin.apply(orchestrator, run_id, wrong_gate, previous)["reason_code"],
                profile_repin.apply(orchestrator, run_id, rejected, previous)["reason_code"],
                orchestrator._runtime_profile(run_id)["reason_code"],
            ]

        self.assertEqual(outcomes, [
            "APPROVAL_INPUT_MISMATCH", "APPROVAL_GATE_MISMATCH", "APPROVAL_REQUIRED",
            "PROFILE_CONFLICT",
        ])

    def test_the_gate_can_be_opened_while_the_pin_is_conflicted(self):
        """The repair gate must not depend on the pin it repairs being valid."""

        class _Channel:
            def __init__(self):
                self.payloads = []

            def request(self, payload):
                self.payloads.append(payload)
                return {"approval_id": payload["approval"]["approval_id"],
                        "input_hash": payload["approval"]["input_hash"]}

        with tempfile.TemporaryDirectory() as directory:
            orchestrator, run_id, path, previous = self._prepared(directory)
            self._edit(path, lambda p: p["pipeline_profile"].update({"pipeline_id": "348142"}))
            comate, infoflow = _Channel(), _Channel()

            conflicted = orchestrator._runtime_profile(run_id)["reason_code"]
            opened = profile_repin.request(
                orchestrator, run_id, previous,
                comate_client=comate, infoflow_client=infoflow,
            )

        self.assertEqual(conflicted, "PROFILE_CONFLICT")
        self.assertEqual(opened["reason_code"], "OK")
        self.assertEqual(opened["approval"]["action"], profile_repin.ACTION)
        self.assertEqual(opened["approval"]["input_hash"], opened["input_hash"])
        # the approvers come from the pinned copy's role_members, not from a guess
        expected = sorted({
            email
            for values in PROFILE["approval_channels"]["role_members"].values()
            for email in values
        })
        self.assertEqual(opened["approval"]["member_policy"]["infoflow"], expected)
        self.assertEqual((len(comate.payloads), len(infoflow.payloads)), (1, 1))

    def test_a_submission_already_bound_to_a_pipeline_blocks_the_move(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, run_id, path, previous = self._prepared(directory)
            orchestrator.artifacts.put(run_id, "submission", b"{}", {
                "controller_binding": {
                    "pipeline_id": PROFILE["pipeline_profile"]["pipeline_id"],
                    "release_rule": PROFILE["pipeline_profile"]["release_rule"],
                },
            })
            self._edit(path, lambda p: p["pipeline_profile"].update({"pipeline_id": "348142"}))

            result = profile_repin.plan(orchestrator, run_id, previous)

        self.assertEqual(result["reason_code"], "REPIN_BREAKS_SUBMISSION_BINDING")


if __name__ == "__main__":
    unittest.main()
