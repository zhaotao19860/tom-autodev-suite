# Skill regression scenarios

`skill-scenarios.json` captures the original eight decisions that previously drifted or conflicted with the controller. This is a reusable fixture set, not proof that all models behave equally.

For each candidate model, use a fresh context per case and provide only the case's `request`, the named installed skills, and the same read-only source/schema evidence. Keep `expected_decisions` and `fail_if` for the evaluator, outside the producer prompt. No host connection, project execution, posting or approval action is part of this evaluation.

Record the actual model/version (or explicitly unknown), scenario version/hash, prompt/skill content hashes, controller revision, full response, decision checks and validator results. Compare observable routing, required fields, evidence and false positives, not prose similarity. Never fill a missing model identity with a guess. Formal M qualification follows [TAD-REVIEW-EXIT](../docs/WORKFLOW_EXIT_CRITERIA.md): run every case three times in independent contexts and require every mandatory decision/contract check to pass. A controller mismatch is a contract failure, not an invitation to make the model fabricate a valid-looking artifact.

The 2026-09-21 independent read-only pass covered the original eight version-1 scenarios. It found a diagnosis-without-diff controller blocker and a `spec_version` documentation mismatch; the latter was corrected to `version`. That historical pass is not a formal 8×3 M qualification or a cross-model comparison.

Version 2 (2026-09-22) updates `diagnosis-before-diff` after the controller correction: a truthful patchless REPAIR proposal is accepted with a null hash, and the later candidate needs its own G5. Source Review diagnoses also use null pipeline identifiers while pipeline diagnoses remain bound to the real failure. `test_diagnosis_contract.py` verifies these controller paths, but no model has been qualified against version 2 yet; the old behavioral result is not inherited as a new pass.

Deterministic local checks (these execute suite helpers in isolated fixtures, not business projects):

```bash
python3 -m unittest discover -s tom-review/tests -p 'test_*.py' -v
bash tom-diagnose/tests/test_autodebug.sh
git diff --check
```

The scope collector rejects moving revisions and missing objects, preserves dirty workspaces, handles unusual paths/binary/mode changes, and disables external diff/textconv execution. The remote helper regression uses isolated local mocks; it is not a relay authentication or real-host acceptance test. Validate produced content with the existing `tom-autodev/scripts/schema_validator.py` as well as checking the decisions above.

Version 3 (2026-09-24) adds `bridge-card-start-and-continue`. It checks that the normal skill path uses explicit project/card confirmation, card-or-run status and continuation, content-only ProducerJob submission, exact approval binding, and replay-safe handoff continuation. It does not authorize live platform actions.
