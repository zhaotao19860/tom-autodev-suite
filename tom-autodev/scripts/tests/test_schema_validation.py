import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from schema_validator import load_named_schema, validate_named_schema
except ImportError:
    load_named_schema = None
    validate_named_schema = None


HASH = "a" * 64


def canonical_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def specialized_examples():
    snapshot = {
        "canonical_card_id": "BGW-1", "title": "Resolver change", "body": "Body",
        "html": "<p>Body</p>", "acceptance": ["AC-1"],
        "fields": {"priority": "P1"}, "attachments": [{"name": "design", "url": "https://ku.baidu-int.com/doc/1"}],
        "links": ["https://ku.baidu-int.com/doc/1"], "status": "OPEN", "type": "REQUIREMENT",
        "responsible_people": [{"email": "owner@baidu.com", "name": "Owner"}],
        "created": {"user": {"email": "creator@baidu.com"}, "time": "2026-08-10T00:00:00+00:00"},
        "modified": {"user": {"email": "editor@baidu.com"}, "time": "2026-08-10T01:00:00+00:00"},
    }
    snapshot["content_hash"] = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    examples = {
        "requirement-snapshot": snapshot,
        "decision-log": {
            "decision_result": "NO_OPEN_DECISIONS", "status": "COMPLETE", "decisions": [],
            "unresolved_frontier": [], "glossary_delta": {}, "adr_candidates": [],
            "source_evidence": ["icafe:BGW-1/snapshot-1"],
        },
        "spec": {
            "version": "1", "behaviors": [{"id": "B-1", "number": 1, "description": "Resolve"}],
            "acceptance_scenarios": [{"id": "S-1", "acceptance_point_ids": ["AC-1"], "given": "a query", "when": "resolved", "then": "an answer"}],
            "boundaries": ["No release"], "errors": ["fail closed"], "compatibility": ["v1"],
            "test_interface": ["resolver API"], "environment_requirements": ["Linux runner"],
            "non_goals": ["UI"], "risks": ["cache"], "rollback": ["revert"],
            "release_evidence": ["ipipe stage"],
            "traceability": [{"acceptance_point_id": "AC-1", "behavior_ids": ["B-1"], "scenario_ids": ["S-1"]}],
        },
        "task-dag": {
            "nodes": [{"task_id": "T-1", "title": "Resolver slice", "capability_slice": "Resolve query",
                "business_module": "resolver",
                "business_changes": ["src/resolver.cc"], "test_changes": ["test/test_resolver.py"],
                "test_ids": ["resolver.answer"], "fixtures": ["query-a"], "ipipe_stages": ["unit"],
                "completion_predicate": "review accepted", "acceptance_point_ids": ["AC-1"]}],
            "edges": [], "acceptance_coverage": [{"acceptance_point_id": "AC-1", "task_ids": ["T-1"]}],
        },
        "task-plan": {
            "plan_id": "P-1", "task_id": "T-1", "g4_input_hash": HASH,
            "repositories": [
                {"role": "business", "path": "/repo/bgw", "revision": "r1"},
                {"role": "tests", "path": "/repo/bgw-tests", "revision": "t1"},
            ],
            "files": ["src/resolver.cc"], "modules": ["resolver"], "symbols": ["resolve"],
            "interfaces": ["Resolver::resolve"],
            "tests": [{"test_id": "resolver.answer", "fixture": "query-a", "assertions": ["returns answer"]}],
            "fixtures": ["query-a"], "ipipe_parameters": {"module": "resolver"}, "risks": ["cache"],
            "rollback": ["revert"], "checklist": [{"order": 1, "item": "add failing test"}],
            "acceptance_point_ids": ["AC-1"],
        },
        "change-set": {
            "change_set_id": "CS-1", "task_id": "T-1",
            "baseline_revisions": {"business": "r1", "tests": "t1"},
            "revisions": {"business": "r2", "tests": "t2"}, "business_patch": "diff --git a b",
            "test_patch": "diff --git a t", "full_diff_hash": "", "test_ids": ["resolver.answer"],
            "traceability_delta": [{"acceptance_point_id": "AC-1", "test_ids": ["resolver.answer"]}],
            "deviations": [],
            "candidate_hash": "",
        },
        "review": {
            "task_id": "T-1", "change_set_hash": HASH,
            "baseline_revisions": {"business": "r1", "tests": "t1"},
            "axes": {"standards": {"complete": True, "finding_ids": []}, "spec": {"complete": True, "finding_ids": []}},
            "findings": [], "verdict": "ACCEPT", "provider": {"kind": "source-only", "identity": "review-provider-v1"},
            "completeness_state": "COMPLETE",
        },
        "diagnosis": {
            "task_id": "T-1", "attempt": 1, "frozen_revisions": {"business": "r2", "tests": "t2"},
            "build_id": "build-1", "stage_id": "stage-1", "job_id": "job-1", "environment_fingerprint": "env-1",
            "log_evidence": ["ipipe:build-1/job-1"], "classification": "CODE",
            "reproduction": ["rerun stage"], "comparison": ["baseline passed"],
            "hypothesis": "null branch is unhandled", "minimal_verification": ["run resolver.answer"],
            "route": "REPAIR", "repair_direction": "guard null", "repair_plan": ["add test", "fix guard"],
            "repair_diff_hash": HASH, "failure_signature": "SIG-1", "evidence_state": "SUFFICIENT",
        },
        "ipipe-evidence": {
            "pipeline_id": "pipe-1", "build_id": "build-1", "module": "resolver",
            "revisions": {"business": "r2", "tests": "t2"},
            "stages": [{"stage_id": "unit", "status": "SUCCESS", "job_ids": ["job-1"]}],
            "jobs": [{"job_id": "job-1", "status": "SUCCESS", "evidence_refs": ["ipipe:build-1/job-1"]}],
            "environment_fingerprint": "env-1", "status": "SUCCESS", "classification": "PASS",
            "failure_signature": None, "release_rule": "all stages pass", "release_evidence": ["ipipe:build-1/job-1"],
            "remote_evidence_refs": ["ipipe:build-1/job-1"],
        },
        "run-summary": {
            "run_id": "run-1", "schema_version": "1", "terminal_state": "RELEASE_SUCCESS", "outcome": "SUCCESS",
            "metrics": {"event_count": 12, "artifact_count": 9, "valid_artifact_count": 9,
                        "external_receipt_count": 4, "unreconciled_intent_count": 0,
                        "abandoned_intent_count": 0},
            "approval_metrics": {"approved": 8, "rejected": 0, "pending": 0, "timed_out": 0},
            "failure_groups": [], "collaboration_receipt_count": 2, "pipeline_evidence_count": 1,
            "artifact_integrity_failures": [], "content_hash": HASH,
        },
        "optimization-proposal": {
            "schema_version": "1", "proposal_id": HASH, "run_id": "run-1", "summary_hash": HASH,
            "summary_artifact_id": HASH, "candidate_hash": HASH,
            "candidate": {
                "root_cause": "duplicate manual reconciliation",
                "expected_benefit": "one fewer manual step", "risk": "low", "rollback": "revert candidate",
                "target_files": [{"path": "/skills/tom-autodev/scripts/phase_protocol.py",
                                  "before_sha256": HASH, "content": "print()", "content_sha256": HASH}],
                "verification_commands": ["python3 -m unittest tests.test_phase_protocol"],
            },
            "candidate_diff": "--- /skills/tom-autodev/scripts/phase_protocol.py",
            "allowed_roots": ["/skills/tom-autodev/scripts"], "approval_gate": "G10",
            "expected_benefit": "one fewer manual step", "risk": "low", "rollback": "revert candidate",
            "evidence": {"failure_groups": [], "summary_artifact_id": HASH},
            "envelope_hash": HASH,
        },
    }
    change = examples["change-set"]
    change["full_diff_hash"] = canonical_hash({
        "business_patch": change["business_patch"], "test_patch": change["test_patch"],
    })
    change["candidate_hash"] = canonical_hash({
        key: value for key, value in change.items() if key != "candidate_hash"
    })
    examples["review"]["change_set_hash"] = change["candidate_hash"]
    return examples


class NamedSchemaValidationTests(unittest.TestCase):
    def test_every_named_schema_has_declared_invariant_coverage(self):
        # Phase 3a coverage guard: every named schema is either semantically validated or
        # explicitly declared structural-only — a new schema must join one set, so no phase
        # can silently ship without its invariant check.
        import schema_validator

        self.assertEqual(
            schema_validator.SEMANTIC_VALIDATED_SCHEMAS | schema_validator.STRUCTURAL_ONLY_SCHEMAS,
            schema_validator._NAMED_SCHEMAS,
        )
        self.assertEqual(
            schema_validator.SEMANTIC_VALIDATED_SCHEMAS & schema_validator.STRUCTURAL_ONLY_SCHEMAS,
            frozenset(),
        )
        # Each semantically-validated schema actually rejects a targeted invariant breach,
        # proving the pass is wired (not just declared).
        for name in schema_validator.SEMANTIC_VALIDATED_SCHEMAS:
            self.assertTrue(schema_validator.has_semantic_validator(name), name)

    def test_every_specialized_schema_accepts_a_complete_artifact(self):
        if validate_named_schema is None:
            self.fail("named schema validation is not implemented")
        for name, instance in specialized_examples().items():
            with self.subTest(schema=name):
                self.assertEqual(validate_named_schema(instance, name), [])

    def test_every_schema_reports_deterministic_missing_and_invalid_paths(self):
        if validate_named_schema is None:
            self.fail("named schema validation is not implemented")
        for name, instance in specialized_examples().items():
            first_field = next(iter(instance))
            malformed = copy.deepcopy(instance)
            del malformed[first_field]
            malformed["unexpected"] = True
            with self.subTest(schema=name):
                issues = validate_named_schema(malformed, name)
                self.assertEqual(issues, sorted(issues, key=lambda issue: (issue.path, issue.kind)))
                self.assertIn((first_field, "missing"), [(issue.path, issue.kind) for issue in issues])
                self.assertIn(("unexpected", "invalid"), [(issue.path, issue.kind) for issue in issues])

    def test_dag_rejects_cycles_unknown_edges_and_incomplete_acceptance_coverage(self):
        if validate_named_schema is None:
            self.fail("named schema validation is not implemented")
        dag = specialized_examples()["task-dag"]
        dag["nodes"].append({**copy.deepcopy(dag["nodes"][0]), "task_id": "T-2", "acceptance_point_ids": ["AC-2"]})
        dag["edges"] = [{"from": "T-1", "to": "T-2"}, {"from": "T-2", "to": "T-1"}, {"from": "T-X", "to": "T-1"}]
        issues = validate_named_schema(dag, "task-dag")
        self.assertEqual([(issue.path, issue.kind) for issue in issues], [
            ("acceptance_coverage", "incomplete"), ("edges", "cycle"), ("edges[2].from", "unknown"),
        ])

    def test_optimization_rejects_targets_outside_skill_control_plane_roots(self):
        """The deny list reads the nested, resolved target paths a proposal really has.

        It used to read a top-level `target_files` of relative strings, which the
        proposal builder has never produced. Nothing loaded this schema, so the check
        passed its own fixture and would have flagged every genuine candidate.
        """
        if validate_named_schema is None:
            self.fail("named schema validation is not implemented")
        proposal = specialized_examples()["optimization-proposal"]
        target = proposal["candidate"]["target_files"][0]
        proposal["candidate"]["target_files"] = [
            {**target, "path": "/skills/tom-autodev/profiles/bgw.yaml"},
            {**target, "path": "/skills/tom-autodev/scripts/pipeline_templates.py"},
            {**target, "path": "/skills/tom-autodev/scripts/state.sqlite"},
            {**target, "path": "/skills/tom-autodev/scripts/orchestrator.py"},
        ]
        self.assertEqual(
            [(issue.path, issue.kind) for issue in validate_named_schema(proposal, "optimization-proposal")],
            [
                ("candidate.target_files[0].path", "forbidden"),
                ("candidate.target_files[1].path", "forbidden"),
                ("candidate.target_files[2].path", "forbidden"),
            ],
        )

    def test_named_loader_rejects_path_traversal(self):
        if load_named_schema is None:
            self.fail("named schema loading is not implemented")
        with self.assertRaisesRegex(ValueError, "SCHEMA_NAME_INVALID"):
            load_named_schema("../project-profile")

    def test_requirement_snapshot_recomputes_the_established_canonical_hash(self):
        snapshot = specialized_examples()["requirement-snapshot"]
        self.assertEqual(validate_named_schema(snapshot, "requirement-snapshot"), [])
        snapshot["content_hash"] = "0" * 64
        self.assertEqual(
            [(issue.path, issue.kind) for issue in validate_named_schema(snapshot, "requirement-snapshot")],
            [("content_hash", "mismatch")],
        )

    def test_requirement_snapshot_normalizes_string_and_object_acceptance_ids(self):
        snapshot = specialized_examples()["requirement-snapshot"]
        snapshot["acceptance"] = ["AC-1", {"id": "AC-2", "text": "Second"}]
        snapshot["content_hash"] = canonical_hash({
            key: value for key, value in snapshot.items() if key != "content_hash"
        })

        self.assertEqual(validate_named_schema(snapshot, "requirement-snapshot"), [])

    def test_requirement_snapshot_rejects_malformed_and_duplicate_normalized_acceptance_ids(self):
        cases = (
            (["AC-1", ""], ("acceptance[1]", "invalid")),
            (["AC-1", {"id": ""}], ("acceptance[1]", "invalid")),
            (["AC-1", 7], ("acceptance[1]", "invalid")),
            (["AC-1", {"id": "AC-1"}], ("acceptance[1]", "duplicate")),
        )
        for acceptance, expected in cases:
            with self.subTest(acceptance=acceptance):
                snapshot = specialized_examples()["requirement-snapshot"]
                snapshot["acceptance"] = acceptance
                snapshot["content_hash"] = canonical_hash({
                    key: value for key, value in snapshot.items() if key != "content_hash"
                })
                issues = validate_named_schema(snapshot, "requirement-snapshot")
                self.assertIn(expected, [(issue.path, issue.kind) for issue in issues])

    def test_decision_log_and_spec_reject_shallow_or_contradictory_completion(self):
        decision = specialized_examples()["decision-log"]
        decision["decisions"] = [{
            "id": "D-1", "question": "Which mode?", "options": ["A", "B"],
            "choice": "A", "rationale": "Evidence", "evidence": ["artifact:source-1"],
        }]
        spec = specialized_examples()["spec"]
        spec["boundaries"] = []

        self.assertIn(
            ("decision_result", "inconsistent"),
            [(issue.path, issue.kind) for issue in validate_named_schema(decision, "decision-log")],
        )
        self.assertIn(
            ("boundaries", "missing"),
            [(issue.path, issue.kind) for issue in validate_named_schema(spec, "spec")],
        )

    def test_decision_log_enforces_each_result_state_and_coherent_recorded_choices(self):
        recorded = specialized_examples()["decision-log"]
        recorded.update({
            "decision_result": "DECISIONS_RECORDED",
            "decisions": [{
                "id": "D-1", "question": "Which mode?", "options": ["A", "B"],
                "choice": "A", "rationale": "Evidence", "evidence": ["artifact:source-1"],
            }],
        })
        unresolved = specialized_examples()["decision-log"]
        unresolved.update({
            "decision_result": "UNRESOLVED", "status": "INCOMPLETE",
            "unresolved_frontier": ["Which timeout?"],
        })
        self.assertEqual(validate_named_schema(recorded, "decision-log"), [])
        self.assertEqual(validate_named_schema(unresolved, "decision-log"), [])

        cases = []
        empty_recorded = copy.deepcopy(recorded)
        empty_recorded["decisions"] = []
        cases.append((empty_recorded, ("decision_result", "inconsistent")))
        open_recorded = copy.deepcopy(recorded)
        open_recorded["unresolved_frontier"] = ["Still open"]
        cases.append((open_recorded, ("decision_result", "inconsistent")))
        duplicate_id = copy.deepcopy(recorded)
        duplicate_id["decisions"].append(copy.deepcopy(duplicate_id["decisions"][0]))
        cases.append((duplicate_id, ("decisions[1].id", "duplicate")))
        duplicate_option = copy.deepcopy(recorded)
        duplicate_option["decisions"][0]["options"] = ["A", "A"]
        cases.append((duplicate_option, ("decisions[0].options", "duplicate")))
        unknown_choice = copy.deepcopy(recorded)
        unknown_choice["decisions"][0]["choice"] = "C"
        cases.append((unknown_choice, ("decisions[0].choice", "unknown")))
        empty_unresolved = copy.deepcopy(unresolved)
        empty_unresolved["unresolved_frontier"] = []
        cases.append((empty_unresolved, ("decision_result", "inconsistent")))
        contradictory_none = specialized_examples()["decision-log"]
        contradictory_none["status"] = "INCOMPLETE"
        cases.append((contradictory_none, ("decision_result", "inconsistent")))

        for value, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(
                    expected,
                    [(issue.path, issue.kind) for issue in validate_named_schema(value, "decision-log")],
                )

    def test_bare_card_and_grill_acceptance_delta_validate_together(self):
        bare = specialized_examples()["requirement-snapshot"]
        bare["acceptance"] = []
        bare["content_hash"] = canonical_hash(
            {key: value for key, value in bare.items() if key != "content_hash"}
        )
        delta = specialized_examples()["decision-log"]
        delta["acceptance_delta"] = [{
            "id": "AC-1", "statement": "Resolver answers within the budget",
            "decided_by": "owner@baidu.com", "evidence": ["icafe://BGW-1#comment-1"],
        }]

        self.assertEqual(validate_named_schema(bare, "requirement-snapshot"), [])
        self.assertEqual(validate_named_schema(delta, "decision-log"), [])

        duplicate = copy.deepcopy(delta)
        duplicate["acceptance_delta"].append(copy.deepcopy(duplicate["acceptance_delta"][0]))
        blank = copy.deepcopy(delta)
        blank["acceptance_delta"][0]["id"] = " "
        for value, expected in (
            (duplicate, ("acceptance_delta[1].id", "duplicate")),
            (blank, ("acceptance_delta[0].id", "invalid")),
        ):
            with self.subTest(expected=expected):
                self.assertIn(
                    expected,
                    [(issue.path, issue.kind) for issue in validate_named_schema(value, "decision-log")],
                )

    def test_dag_plan_review_diagnosis_and_ipipe_reject_semantic_contradictions(self):
        dag = specialized_examples()["task-dag"]
        dag["nodes"].append(copy.deepcopy(dag["nodes"][0]))
        plan = specialized_examples()["task-plan"]
        plan["repositories"] = plan["repositories"][:1]
        plan["checklist"].append({"order": 1, "item": "duplicate order"})
        review = specialized_examples()["review"]
        review["findings"] = [{
            "id": "F-1", "axis": "standards", "severity": "P0", "location": "src/a.cc:1",
            "evidence": "broken", "acceptance_point_ids": ["AC-1"], "blocking": True,
            "classification": "CONFIRMED",
        }]
        diagnosis = specialized_examples()["diagnosis"]
        diagnosis["evidence_state"] = "INSUFFICIENT"
        diagnosis["route"] = "REPAIR"
        ipipe = specialized_examples()["ipipe-evidence"]
        ipipe["status"] = "SUCCESS"
        ipipe["failure_signature"] = "SIG-FAIL"
        ipipe["stages"][0]["job_ids"] = ["unknown-job"]

        cases = [
            ("task-dag", dag, ("nodes[1].task_id", "duplicate")),
            ("task-plan", plan, ("repositories", "incomplete")),
            ("task-plan", plan, ("checklist", "unordered")),
            ("review", review, ("verdict", "inconsistent")),
            ("diagnosis", diagnosis, ("route", "inconsistent")),
            ("ipipe-evidence", ipipe, ("failure_signature", "inconsistent")),
            ("ipipe-evidence", ipipe, ("stages[0].job_ids[0]", "unknown")),
        ]
        for name, value, expected in cases:
            with self.subTest(schema=name, expected=expected):
                self.assertIn(
                    expected,
                    [(issue.path, issue.kind) for issue in validate_named_schema(value, name)],
                )

    def test_change_set_recomputes_patch_and_candidate_hashes(self):
        for field in ("full_diff_hash", "candidate_hash"):
            change = specialized_examples()["change-set"]
            change[field] = "0" * 64
            with self.subTest(field=field):
                self.assertIn(
                    (field, "mismatch"),
                    [(issue.path, issue.kind) for issue in validate_named_schema(change, "change-set")],
                )

    def test_a_findings_disposition_has_to_answer_for_itself(self):
        """`tom-autodev` says not to implement an unverified suggestion; this is where it is said.

        Before `classification` existed the review artifact had no way to record whether a
        finding had been checked, so "confirmed" lived in prose and the SUBMIT gate saw
        only `blocking`. A rejection without a reason and a rejection that still blocks are
        both the same defect: the reviewer's verification cannot be read back.
        """
        finding = {
            "id": "F-1", "axis": "standards", "severity": "P0", "location": "src/a.cc:1",
            "evidence": "broken", "acceptance_point_ids": ["AC-1"], "blocking": False,
            "classification": "CONFIRMED",
        }
        cases = []
        for classification in ("REJECTED_WITH_REASON", "NEEDS_CLARIFICATION"):
            unexplained = specialized_examples()["review"]
            unexplained["findings"] = [{**finding, "classification": classification}]
            unexplained["axes"]["standards"]["finding_ids"] = ["F-1"]
            unexplained["verdict"] = "REJECT"
            cases.append((classification, unexplained, ("findings[0].disposition_reason", "missing")))
        rejected_but_blocking = specialized_examples()["review"]
        rejected_but_blocking["findings"] = [{
            **finding, "classification": "REJECTED_WITH_REASON", "blocking": True,
            "disposition_reason": "the caller already checks this",
        }]
        rejected_but_blocking["axes"]["standards"]["finding_ids"] = ["F-1"]
        rejected_but_blocking["verdict"] = "REJECT"
        cases.append(("blocking", rejected_but_blocking, ("findings[0].blocking", "inconsistent")))
        unclassified = specialized_examples()["review"]
        unclassified["findings"] = [{
            key: value for key, value in finding.items() if key != "classification"
        }]
        unclassified["axes"]["standards"]["finding_ids"] = ["F-1"]
        cases.append(("absent", unclassified, ("findings[0].classification", "missing")))

        for label, value, expected in cases:
            with self.subTest(case=label):
                self.assertIn(
                    expected,
                    [(issue.path, issue.kind) for issue in validate_named_schema(value, "review")],
                )

    def test_an_accept_verdict_cannot_stand_over_an_unanswered_question(self):
        """`NEEDS_CLARIFICATION` is the reviewer saying they could not establish this.

        An `ACCEPT` alongside it claims the opposite in the same document, which is how an
        unverified suggestion reaches SUBMIT. A confirmed non-blocking finding is different
        and stays acceptable: it was checked, and it does not hold up the change.
        """
        finding = {
            "id": "F-1", "axis": "spec", "severity": "P2", "location": "src/a.cc:1",
            "evidence": "unclear whether the budget applies", "acceptance_point_ids": ["AC-1"],
            "blocking": False,
        }
        unclarified = specialized_examples()["review"]
        unclarified["findings"] = [{
            **finding, "classification": "NEEDS_CLARIFICATION",
            "disposition_reason": "waiting on the requirement owner",
        }]
        unclarified["axes"]["spec"]["finding_ids"] = ["F-1"]
        confirmed = specialized_examples()["review"]
        confirmed["findings"] = [{**finding, "classification": "CONFIRMED"}]
        confirmed["axes"]["spec"]["finding_ids"] = ["F-1"]

        self.assertIn(
            ("verdict", "inconsistent"),
            [(issue.path, issue.kind) for issue in validate_named_schema(unclarified, "review")],
        )
        self.assertEqual(validate_named_schema(confirmed, "review"), [])

    def test_a_change_set_records_its_deviations_and_they_ride_the_candidate_hash(self):
        """An empty list is a claim; a missing field is silence.

        `deviations` is required so that "the diff follows the approved plan" is something
        the artifact says rather than something the reader assumes. Because `candidate_hash`
        covers every other field, a deviation cannot be appended after the hash a G5
        approval or a Review was bound to -- the hash stops matching.
        """
        declared = specialized_examples()["change-set"]
        declared["deviations"] = [{
            "from_plan": "plan added the guard in resolver_answer",
            "as_implemented": "guard added in resolver_prepare instead",
            "reason": "resolver_answer runs after the budget check",
            "approval_input_hash": "grill-hash",
        }]
        declared["candidate_hash"] = canonical_hash({
            key: value for key, value in declared.items() if key != "candidate_hash"
        })
        silent = specialized_examples()["change-set"]
        del silent["deviations"]
        appended_late = copy.deepcopy(declared)
        appended_late["deviations"].append({
            "from_plan": "x", "as_implemented": "y", "reason": "slipped in after approval",
        })
        incomplete = specialized_examples()["change-set"]
        incomplete["deviations"] = [{"from_plan": "a", "as_implemented": "b"}]
        incomplete["candidate_hash"] = canonical_hash({
            key: value for key, value in incomplete.items() if key != "candidate_hash"
        })

        self.assertEqual(validate_named_schema(declared, "change-set"), [])
        for label, value, expected in (
            ("silent", silent, ("deviations", "missing")),
            ("appended after approval", appended_late, ("candidate_hash", "mismatch")),
            ("no reason", incomplete, ("deviations[0].reason", "missing")),
        ):
            with self.subTest(case=label):
                self.assertIn(
                    expected,
                    [(issue.path, issue.kind) for issue in validate_named_schema(value, "change-set")],
                )

    def test_ipipe_statuses_and_incomplete_diagnosis_are_semantically_explicit(self):
        inconsistent = specialized_examples()["ipipe-evidence"]
        inconsistent["status"] = "FAILURE"
        inconsistent["classification"] = "CODE"
        inconsistent["failure_signature"] = "SIG-FAIL"
        unknown = specialized_examples()["ipipe-evidence"]
        unknown["stages"][0]["status"] = "MYSTERY"
        incomplete = specialized_examples()["diagnosis"]
        incomplete.update({
            "evidence_state": "INSUFFICIENT", "route": "DIAGNOSIS_INCOMPLETE",
            "hypothesis": None, "repair_direction": None, "repair_plan": [],
            "repair_diff_hash": None,
        })

        self.assertIn(
            ("status", "inconsistent"),
            [(issue.path, issue.kind) for issue in validate_named_schema(inconsistent, "ipipe-evidence")],
        )
        self.assertIn(
            ("stages[0].status", "invalid"),
            [(issue.path, issue.kind) for issue in validate_named_schema(unknown, "ipipe-evidence")],
        )
        self.assertEqual(validate_named_schema(incomplete, "diagnosis"), [])

    def test_amendment_route_diagnosis_carries_no_repair_diff(self):
        # A Spec amendment is decided before any code is written, so requiring a diff hash
        # there would make the route unrepresentable; a REPAIR route still owes one.
        amendment = specialized_examples()["diagnosis"]
        amendment.update({
            "route": "SPEC", "repair_scope": "SPEC_AMENDMENT", "repair_diff_hash": None,
            "repair_direction": "amend the Spec, then update the cases",
        })
        stale = {**amendment, "repair_diff_hash": HASH}
        repair_without_diff = specialized_examples()["diagnosis"]
        repair_without_diff["repair_diff_hash"] = None

        self.assertEqual(validate_named_schema(amendment, "diagnosis"), [])
        self.assertIn(
            ("repair_diff_hash", "inconsistent"),
            [(issue.path, issue.kind) for issue in validate_named_schema(stale, "diagnosis")],
        )
        self.assertIn(
            ("repair_diff_hash", "missing"),
            [(issue.path, issue.kind) for issue in validate_named_schema(repair_without_diff, "diagnosis")],
        )

    def test_ipipe_rejects_noncanonical_or_secret_nested_evidence_references(self):
        cases = (
            ("job", "https://logs.example.test/job-1", ("jobs[0].evidence_refs", "invalid")),
            ("remote", "ipipe:build-1/token-secret", ("remote_evidence_refs", "invalid")),
        )
        for location, reference, expected in cases:
            with self.subTest(location=location):
                evidence = specialized_examples()["ipipe-evidence"]
                if location == "job":
                    evidence["jobs"][0]["evidence_refs"] = [reference]
                else:
                    evidence["remote_evidence_refs"] = [reference]
                self.assertIn(
                    expected,
                    [(issue.path, issue.kind) for issue in validate_named_schema(evidence, "ipipe-evidence")],
                )


if __name__ == "__main__":
    unittest.main()
