"""B-05: a superseded (or rejected) build is never current IPIPE or release evidence.

`ipipe-rebuild` marks the replaced build `ipipe.build-superseded`; a build refused
after TRIGGER_MODE_MISMATCH is marked `ipipe.build-rejected`. Module verdicts, the
release selection, the RELEASE predecessor / G9 binding, evidence ingestion and the
IPIPE-state rebuild justification all have to honour those markers.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_markers
import ipipe_rebuild
import orchestrator as controller
import test_pipeline_plan
from pipeline_plan import frozen_plan, release_builds, successful_builds


class SupersededBuildEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.base = test_pipeline_plan.PipelinePlanTests(
            "test_plan_is_frozen_and_does_not_follow_live_cr_patchsets")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.orch, self.run = self.base.orch, self.base.run
        self.base._start_plan()

    def mark(self, build_id, marker="superseded"):
        key = (build_markers.superseded_key if marker == "superseded" else build_markers.rejected_key)(
            self.run, build_id)
        self.orch.state.save_idempotency_result(key, {
            "run_id": self.run, "build_id": build_id, "superseded_by": "later",
        })

    def plan(self):
        return frozen_plan(self.orch.state.events(self.run))

    def test_superseded_success_does_not_pass_the_module_or_reach_release(self):
        self.base._build("A", "a-old")
        self.mark("a-old")

        self.base._build("B", "b-built")

        plan = self.plan()
        self.assertEqual(set(successful_builds(self.orch.artifacts, self.orch.state, self.run, plan)), {"B"})
        with self.assertRaisesRegex(ValueError, "RELEASE_EVIDENCE_INCOMPLETE"):
            release_builds(self.orch.artifacts, self.orch.state, self.run, plan)
        self.assertEqual(self.base.protocol.ipipe_outstanding_modules(self.run), ["A"])
        # The run never reaches RELEASE, so no G9 can be requested over the old build.
        self.assertEqual(self.orch.status(self.run)["state"], "IPIPE")
        self.assertNotEqual(self.base.protocol.next(self.run).get("state"), "RELEASE")

    def test_rejected_success_is_excluded_the_same_way(self):
        self.base._build("A", "a-rejected")
        self.mark("a-rejected", "rejected")
        self.base._build("B", "b-built")

        self.assertEqual(self.base.protocol.ipipe_outstanding_modules(self.run), ["A"])
        self.assertEqual(self.orch.status(self.run)["state"], "IPIPE")

    def test_release_inputs_and_g9_binding_name_only_the_current_build(self):
        self.base._build("A", "a-old")
        old_artifact = self.orch.artifacts.latest_phase(self.run, "IPIPE", None)["artifact_id"]
        self.mark("a-old")
        # What ipipe_rebuild.rebind does: a fresh IPIPE event cloned from the entry.
        entry = next(e for e in reversed(self.orch.state.events(self.run)) if e["state"] == "IPIPE")
        self.orch.state.transition(self.run, "IPIPE", {
            **entry["payload"], "previous_state": "IPIPE",
            "rebuild": {"kind": "IPIPE_REBUILD", "original_build_id": "a-old", "build_id": "a-new"},
        })
        self.base._build("A", "a-new")
        self.base._build("B", "b-built")

        selected = release_builds(self.orch.artifacts, self.orch.state, self.run, self.plan())
        action = self.base.protocol.next(self.run)

        self.assertEqual(selected["A"]["envelope"]["content"]["build_id"], "a-new")
        self.assertEqual(action["state"], "RELEASE", action)
        self.assertNotIn(old_artifact, {item["artifact_id"] for item in action["input_artifacts"]})

    def test_late_evidence_of_a_superseded_build_is_not_ingested(self):
        self.mark("a-late")
        content = {"build_id": "a-late", "module": "A", "status": "SUCCESS"}

        result = self.base.protocol.ingest_ipipe_evidence(self.run, content)

        self.assertEqual(result["reason_code"], "BUILD_SUPERSEDED", result)
        self.assertEqual(self.orch.artifacts.phase_artifacts(self.run, "IPIPE"), [])

    def test_rebuild_from_ipipe_refuses_to_replace_a_successful_build_without_proof(self):
        self.base._build("A", "a-built")
        self.assertEqual(self.orch.status(self.run)["state"], "IPIPE")
        frozen = {item["module"]: item["revision"] for item in self.plan()["revision_set"]["repositories"]}
        with patch.object(controller, "_current_patchset",
                          side_effect=lambda module, path, number: frozen.get(module)), \
                patch.object(controller, "_open_test_repo_cr", return_value=None):
            refused = ipipe_rebuild.plan(
                self.orch, self.base._runtime(), self.run, reason="想重新编一次",
                module="A", skip_probe=True,
            )

        self.assertEqual(refused["reason_code"], "REBUILD_NOT_JUSTIFIED", refused)
        self.assertEqual(refused["detail"], "REPLACED_BUILD_NOT_FAILED")


if __name__ == "__main__":
    unittest.main()
