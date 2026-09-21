# Skill regression scenarios

`skill-scenarios.json` captures eight decisions that previously drifted or conflicted with the controller. This is a reusable fixture set, not proof that all models behave equally.

For each candidate model, use a fresh context per case and provide only the case's `request`, the named installed skills, and the same read-only source/schema evidence. Keep `expected_decisions` and `fail_if` for the evaluator, outside the producer prompt. No host connection, project execution, posting or approval action is part of this evaluation.

Record the actual model/version (or explicitly unknown), prompt/skill content hashes, controller revision, full response, decision checks and validator results. Compare observable routing, required fields, evidence and false positives, not prose similarity. Never fill a missing model identity with a guess. Repeat borderline cases to measure variance. A controller mismatch is a contract failure, not an invitation to make the model fabricate a valid-looking artifact.

The 2026-09-21 independent read-only pass covered all eight scenarios. It found the remaining diagnosis-without-diff controller blocker and a `spec_version` documentation mismatch; the latter was corrected to `version`. This is one-model behavioral validation; a cross-model pass has not been run.

Deterministic local checks (these execute suite helpers in isolated fixtures, not business projects):

```bash
python3 -m unittest discover -s tom-review/tests -p 'test_*.py' -v
bash tom-diagnose/tests/test_autodebug.sh
git diff --check
```

The scope collector rejects moving revisions and missing objects, preserves dirty workspaces, handles unusual paths/binary/mode changes, and disables external diff/textconv execution. The remote helper regression uses isolated local mocks; it is not a relay authentication or real-host acceptance test. Validate produced content with the existing `tom-autodev/scripts/schema_validator.py` as well as checking the decisions above.
