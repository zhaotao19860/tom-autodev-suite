"""Binds every legacy workflow structure to workflow_spec, so drift is a test failure.

If you change a phase's gate/target/schema/side-effects, a transition edge, a gate
label, or an evidence requirement, change it in workflow_spec.py — these tests assert
the old scattered structures still match the spec exactly.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import approval_summary
import evidence_policy
import phase_protocol
import transition_policy
import workflow_spec as ws


class SelfConsistencyTests(unittest.TestCase):
    def test_every_transition_target_is_a_known_state(self):
        for state, spec in ws.STATES.items():
            for target in spec.transitions:
                self.assertIn(target, ws.STATES, f"{state} -> {target}")

    def test_terminal_states_have_no_transitions(self):
        for state, spec in ws.STATES.items():
            if spec.terminal:
                self.assertEqual(spec.transitions, frozenset(), state)

    def test_predecessors_are_known_states(self):
        for state, spec in ws.STATES.items():
            predecessors = spec.predecessor if isinstance(spec.predecessor, tuple) else (spec.predecessor,)
            for predecessor in predecessors:
                if predecessor is not None:
                    self.assertIn(predecessor, ws.STATES, f"{state} predecessor {predecessor}")

    def test_every_referenced_gate_has_a_label(self):
        gates = set()
        for spec in ws.STATES.values():
            gates.update(g for g in (spec.entry_gate, spec.output_gate) if g)
        for _artifacts, gate in ws.TRANSITION_REQUIREMENTS.values():
            if gate:
                gates.add(gate)
        for gate in gates:
            self.assertIn(gate, ws.GATE_LABELS, gate)

    def test_registry_values_are_valid(self):
        for state, spec in ws.STATES.items():
            self.assertIn(spec.registry, (None, "phase", "controller"), state)

    def test_canonical_hash_is_stable(self):
        self.assertEqual(ws.canonical_hash(), ws.canonical_hash())
        self.assertRegex(ws.canonical_hash(), r"^[0-9a-f]{64}$")


# placeholder-legacy
class LegacyStructureConsistencyTests(unittest.TestCase):
    def test_allowed_transitions_match(self):
        self.assertEqual(ws.allowed_transitions(), transition_policy.ALLOWED_TRANSITIONS)

    def test_terminal_states_match(self):
        self.assertEqual(ws.terminal_states(), transition_policy.TERMINAL_STATES)

    def test_failure_target_matches(self):
        policy = transition_policy.TransitionPolicy()
        reasons = [
            "REQUIREMENT_CHANGED", "ENV_UNSATISFIED", "ENV_TRANSIENT", "CODE_FAILURE",
            "TEST_FAILURE", "REVIEW_FAILED", "RELEASE_FAILED", "SOMETHING_ELSE",
        ]
        for reason in reasons:
            self.assertEqual(ws.failure_target(reason), policy.failure_target(reason), reason)

    def test_phase_registry_matches__PHASES(self):
        spec_phases = {s for s, spec in ws.STATES.items() if spec.registry == "phase"}
        self.assertEqual(spec_phases, set(phase_protocol._PHASES))
        for state, definition in phase_protocol._PHASES.items():
            spec = ws.STATES[state]
            self.assertEqual(definition.get("skill"), spec.skill, state)
            self.assertEqual(definition.get("controller"), spec.controller, state)
            self.assertEqual(definition.get("schema"), spec.schema, state)
            self.assertEqual(definition.get("gate"), spec.output_gate, state)
            self.assertEqual(definition.get("target"), spec.target, state)
            self.assertEqual(definition.get("predecessor"), spec.predecessor, state)
            self.assertEqual(definition.get("revisions", False), spec.envelope_revisions, state)

    def test_controller_registry_matches__CONTROLLERS(self):
        spec_controllers = {s for s, spec in ws.STATES.items() if spec.registry == "controller"}
        self.assertEqual(spec_controllers, set(phase_protocol._CONTROLLERS))
        for state, definition in phase_protocol._CONTROLLERS.items():
            spec = ws.STATES[state]
            self.assertEqual(definition.get("controller"), spec.controller, state)
            self.assertEqual(definition.get("schema"), spec.schema, state)
            self.assertEqual(definition.get("gate"), spec.output_gate, state)
            self.assertEqual(definition.get("target"), spec.target, state)
            self.assertEqual(definition.get("predecessor"), spec.predecessor, state)
            self.assertEqual(definition.get("revisions", False), spec.envelope_revisions, state)

    def test_titles_match(self):
        spec_titles = {s: spec.title for s, spec in ws.STATES.items() if spec.title is not None}
        self.assertEqual(spec_titles, phase_protocol._TITLE)

    def test_task_scoped_titles_match(self):
        spec_scoped = frozenset(s for s, spec in ws.STATES.items() if spec.task_scoped)
        self.assertEqual(spec_scoped, phase_protocol._TASK_SCOPED_TITLES)

    def test_side_effects_match(self):
        for state, spec in ws.STATES.items():
            if not spec.side_effects:
                continue
            is_controller = state in phase_protocol._CONTROLLERS
            self.assertEqual(
                list(spec.side_effects),
                phase_protocol._allowed_side_effects(state, is_controller),
                state,
            )

    def test_evidence_requirements_match(self):
        state_keys = {k for k in evidence_policy.REQUIREMENTS if not re.fullmatch(r"G\d+", k)}
        spec_keys = {
            s for s, spec in ws.STATES.items()
            if spec.entry_gate or spec.entry_artifacts
        }
        self.assertEqual(state_keys, spec_keys)
        for state in state_keys:
            spec = ws.STATES[state]
            expected = evidence_policy.EvidenceRequirement(
                artifacts=spec.entry_artifacts,
                approval_gate=spec.entry_gate,
                requires_revisions=spec.evidence_requires_revisions,
                requires_environment=spec.evidence_requires_environment,
            )
            self.assertEqual(evidence_policy.REQUIREMENTS[state], expected, state)

    def test_transition_requirements_match(self):
        spec_transitions = {
            key: evidence_policy.EvidenceRequirement(artifacts=arts, approval_gate=gate)
            for key, (arts, gate) in ws.TRANSITION_REQUIREMENTS.items()
        }
        self.assertEqual(spec_transitions, evidence_policy.TRANSITION_REQUIREMENTS)

    def test_gate_labels_match(self):
        self.assertEqual(ws.GATE_LABELS, approval_summary._GATES)


class ChangeClassTests(unittest.TestCase):
    def test_classify_defaults_to_standard(self):
        self.assertEqual(ws.classify_change({"type": "feature"}), "standard")
        self.assertEqual(ws.classify_change({}), "standard")
        self.assertEqual(ws.classify_change(None), "standard")

    def test_classify_suggests_a_class_from_the_card_type(self):
        self.assertEqual(ws.classify_change({"type": "bug"}), "express")
        self.assertEqual(ws.classify_change({"type": "缺陷"}), "express")
        self.assertEqual(ws.classify_change({"type": "HotFix"}), "hotfix")
        self.assertEqual(ws.classify_change({"type": "epic"}), "full")

    def test_explicit_override_wins_but_invalid_falls_through(self):
        self.assertEqual(ws.classify_change({"type": "bug"}, "standard"), "standard")
        self.assertEqual(ws.classify_change({"type": "feature"}, "full"), "full")
        # An override that is not a known class is ignored; type-based suggestion stands.
        self.assertEqual(ws.classify_change({"type": "bug"}, "nonsense"), "express")

    def test_phase_mode(self):
        # standard merges the design front: SPEC produces {spec, dag}, TASKS is ungated.
        self.assertEqual(ws.phase_mode("standard", "SPEC"), "merged")
        self.assertEqual(ws.phase_mode("standard", "TASKS"), "ungated")
        for state in ("GRILL", "PLAN", "IMPLEMENT", "REVIEW"):
            self.assertEqual(ws.phase_mode("standard", state), "full", state)
        # full is the escape hatch: every phase is a separate gated skill phase.
        for state in ("GRILL", "SPEC", "TASKS", "PLAN", "IMPLEMENT", "REVIEW"):
            self.assertEqual(ws.phase_mode("full", state), "full", state)
        self.assertEqual(ws.phase_mode("express", "GRILL"), "auto")
        self.assertEqual(ws.phase_mode("express", "SPEC"), "merged")
        self.assertEqual(ws.phase_mode("express", "TASKS"), "ungated")
        self.assertEqual(ws.phase_mode("express", "PLAN"), "full")
        # hotfix folds PLAN on top of express.
        self.assertEqual(ws.phase_mode("hotfix", "PLAN"), "ungated")
        self.assertEqual(ws.phase_mode("hotfix", "GRILL"), "auto")

    def test_waived_gates(self):
        # standard waives only TASKS's output G3 (the merged draft carries the DAG).
        self.assertEqual(ws.waived_gates("standard"), frozenset({"G3"}))
        self.assertEqual(ws.waived_gates("full"), frozenset())
        # GRILL auto waives its output G1; TASKS ungated waives its output G3; G0 stays.
        self.assertEqual(ws.waived_gates("express"), frozenset({"G1", "G3"}))
        self.assertNotIn("G0", ws.waived_gates("express"))
        # hotfix additionally waives PLAN's own output G4 (the WORKSPACE binding G4 stays).
        self.assertEqual(ws.waived_gates("hotfix"), frozenset({"G1", "G3", "G4"}))

    def test_change_class_modes_are_valid_and_reference_real_states(self):
        for change_class, spec in ws.CHANGE_CLASSES.items():
            for state, mode in spec.phase_modes.items():
                self.assertIn(mode, ("full", "auto", "ungated", "merged"), (change_class, state))
                self.assertIn(state, ws.STATES, (change_class, state))
                # Only the skill-backed clarify/design front is ever reshaped.
                self.assertEqual(ws.STATES[state].registry, "phase", (change_class, state))


class RunPolicyPinTests(unittest.TestCase):
    """MEDIUM-001: the control policy a run runs under is frozen at G0.

    change_class and the workflow_spec version hash are bound into the G0 input hash, and
    the class's phase-mode map is pinned so a later edit to CHANGE_CLASSES cannot re-route
    an in-flight run.
    """

    @staticmethod
    def _intake_events(**payload):
        return [{"payload": payload}]

    def test_canonical_hash_covers_change_class_policy(self):
        baseline = ws.canonical_hash()
        original = ws.CHANGE_CLASSES["standard"]
        patched = ws.ChangeClass("standard", phase_modes={"SPEC": "full", "TASKS": "full"})
        ws.CHANGE_CLASSES["standard"] = patched
        try:
            self.assertNotEqual(ws.canonical_hash(), baseline)
        finally:
            ws.CHANGE_CLASSES["standard"] = original
        self.assertEqual(ws.canonical_hash(), baseline)

    def test_canonical_hash_covers_knowledge_scope(self):
        baseline = ws.canonical_hash()
        original = ws._KNOWLEDGE_SCOPE["REVIEW"]
        ws._KNOWLEDGE_SCOPE["REVIEW"] = "both"
        try:
            self.assertNotEqual(ws.canonical_hash(), baseline)
        finally:
            ws._KNOWLEDGE_SCOPE["REVIEW"] = original
        self.assertEqual(ws.canonical_hash(), baseline)

    def test_run_workflow_modes_snapshots_the_started_class(self):
        self.assertEqual(
            ws.run_workflow_modes("standard"), {"SPEC": "merged", "TASKS": "ungated"}
        )
        # A snapshot, not a live view: mutating it never touches the class definition.
        snapshot = ws.run_workflow_modes("express")
        snapshot["SPEC"] = "full"
        self.assertEqual(ws.CHANGE_CLASSES["express"].phase_modes["SPEC"], "merged")

    def test_phase_mode_for_run_reads_the_pin_not_the_live_policy(self):
        events = self._intake_events(
            change_class="standard", workflow_modes=ws.run_workflow_modes("standard")
        )
        self.assertEqual(ws.phase_mode_for_run(events, "SPEC"), "merged")
        self.assertEqual(ws.phase_mode_for_run(events, "TASKS"), "ungated")
        self.assertEqual(ws.phase_mode_for_run(events, "PLAN"), "full")
        # Re-editing the live class does NOT change this pinned run's routing.
        original = ws.CHANGE_CLASSES["standard"]
        ws.CHANGE_CLASSES["standard"] = ws.ChangeClass("standard", phase_modes={})
        try:
            self.assertEqual(ws.phase_mode_for_run(events, "SPEC"), "merged")
        finally:
            ws.CHANGE_CLASSES["standard"] = original

    def test_phase_mode_for_run_falls_back_for_legacy_runs(self):
        # A legacy INTAKE payload carries a class but no pinned modes map.
        legacy = self._intake_events(change_class="express")
        self.assertEqual(ws.phase_mode_for_run(legacy, "GRILL"), "auto")
        self.assertEqual(ws.phase_mode_for_run(legacy, "SPEC"), "merged")
        # No events at all resolves to the default class.
        self.assertEqual(ws.phase_mode_for_run(None, "SPEC"), "merged")

    def test_pinned_spec_hash_reads_the_run_pin(self):
        events = self._intake_events(workflow_spec_hash="deadbeef")
        self.assertEqual(ws.pinned_spec_hash(events), "deadbeef")
        self.assertIsNone(ws.pinned_spec_hash(self._intake_events()))
        self.assertIsNone(ws.pinned_spec_hash(None))


if __name__ == "__main__":
    unittest.main()
