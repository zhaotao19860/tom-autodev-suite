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
            self.assertEqual(ws.failure_target(reason), policy.failure_target("IPIPE", reason), reason)

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


if __name__ == "__main__":
    unittest.main()
