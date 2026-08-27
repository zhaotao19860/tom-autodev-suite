# Task 1 Review: Close Evidence and Transition Gates

## Verdict

- Spec: **FAIL**
- Quality: **FAIL**

## Review Basis

- Baseline: `/Users/tom/Desktop/skills` is not a Git worktree, so no revision/diff baseline is available. Review covered the current content of the Task 1 brief and requested implementation/test files.
- Task brief SHA-256: `0b086b6b57eaef2be70a8b573d8cd0ec08726a1319dd6fec995aa707ecf3cd5c`.
- Reviewed file SHA-256 values: `evidence_policy.py` `1ed60d4c4921f06ed786a97a31f9d28792e49326a464788bae1cc5af24b41e39`; `transition_policy.py` `6775df7e219e5951b591e93e94466e6d6bea10fdf4c9b727aac9ee0e8c41b324`; `evidence_gate.py` `43d905038684b4ba3266d399330174e315d2c3ca0ceabc8c4c68b33a793a8a9a`; `orchestrator.py` `daa8a46274c17296628b6be3508d80b4543479d505f78a495cca2f0583bd1b0a`; `test_gates.py` `cf6160e428b7851883f2e18ad90c51bd9fa4938b200aee8b92c5281595bb7083`; `test_orchestrator.py` `0fa9eaab6f66412b55d4b661aaf4495bc13dfcce1332d93c605c79fa66eaa09f`.
- Tests were not run. The review procedure prohibits executing project tests; test adequacy was assessed statically. No BGW/XFlow build or test command was run.

## Confirmed Findings

### Critical: Gate approval can be forged and G6/G8/G10 are not modeled

- Axis: Spec
- Blocking: yes
- Evidence: `scripts/evidence_policy.py:17` through `scripts/evidence_policy.py:35` omit G6, G8, and G10 requirements, and the generic G5/G9 entries require no approval. `scripts/evidence_policy.py:43` through `scripts/evidence_policy.py:55` accept an arbitrary caller-supplied approval dictionary. `scripts/orchestrator.py:81` supplies that untrusted context directly to the gate; it never looks up `ApprovalLedger` nor binds an approval ID to the run/gate/input. A caller can therefore advance with a fabricated `{gate, decision, input_hash}` record.
- Affected acceptance criteria: G0-G10 approvals must bind the exact canonical input hash; missing gate approval must reject; all transitions must fail closed.
- Required correction: define explicit evidence/approval requirements for every G0-G10 operation, require an approval identifier and canonical input hash, and validate the approved ledger record rather than trusting context data. Add positive and forged/mismatched approval tests for each relevant gate, especially G6/G8/G10.

### Critical: Empty revision evidence satisfies a release gate

- Axis: Spec
- Blocking: yes
- Evidence: `scripts/evidence_policy.py:79` through `scripts/evidence_policy.py:83` only require both revision values to be dictionaries. Empty dictionaries pass; `scripts/evidence_gate.py:18` through `scripts/evidence_gate.py:20` then treats matching `{}` values as valid. Thus `RELEASE` with `repo_revisions={}` and `evidence_revisions={}` is accepted even though no revision evidence exists.
- Affected acceptance criteria: missing revision evidence is rejected; empty evidence cannot advance a non-INTAKE phase.
- Required correction: require non-empty, schema-valid revision sets with required repositories/revisions before comparing them; test empty, `None`, partial, and mismatching values.

### Important: `stop()` bypasses the legal-transition policy and writes after a terminal state

- Axis: Spec
- Blocking: yes
- Evidence: `scripts/orchestrator.py:145` through `scripts/orchestrator.py:150` call `StateStore.transition()` directly. `RELEASE_SUCCESS` has no outgoing transition in `scripts/transition_policy.py:6` through `scripts/transition_policy.py:21`, yet `stop(run_id)` from that state appends a `STOPPED` event rather than returning a structured terminal-state block.
- Affected acceptance criteria: illegal and terminal-state transitions return structured blocks without writing state events; transitions use one legal-transition policy.
- Required correction: express terminal states explicitly in `TransitionPolicy`, route `stop()` through it, and test the event count remains unchanged for every terminal transition.

### Important: Successful events do not retain the policy decision that authorized them

- Axis: Spec
- Blocking: yes
- Evidence: `scripts/orchestrator.py:90` through `scripts/orchestrator.py:98` persist only prior state, input hash, and caller evidence. `scripts/orchestrator.py:131` through `scripts/orchestrator.py:135` persist only failure reason and evidence. Neither payload contains the `TransitionPolicy.validate()` result returned at `scripts/transition_policy.py:24` through `scripts/transition_policy.py:38`.
- Affected acceptance criteria: idempotency is preserved and policy evidence is recorded.
- Required correction: persist a stable policy-decision object, including current state, target state, policy version/identifier, and result, in both successful advance and failure-route events; assert it in tests.

### Important: Malformed timestamps raise instead of producing a stable gate block

- Axis: Standards and Spec
- Blocking: yes
- Evidence: `scripts/evidence_gate.py:27` through `scripts/evidence_gate.py:30` call `_parse_time()` without validation handling; `scripts/evidence_gate.py:49` through `scripts/evidence_gate.py:50` raises `ValueError` for an invalid ISO timestamp. This produces an exception rather than the specified structured, stable reason-code response.
- Affected acceptance criteria: evidence validation returns stable reason codes and illegal/invalid progress is structured and fail closed.
- Required correction: catch malformed/non-string timestamps and return a dedicated blocked result. Add malformed timestamp coverage alongside stale timestamp coverage.

### Important: Required transition and evidence coverage is absent

- Axis: Standards and Spec
- Blocking: yes
- Evidence: `scripts/tests/test_gates.py:47` through `scripts/tests/test_gates.py:129` test only `GRILL` for empty evidence and do not cover empty/`None`/partial revision sets or every G0-G10 requirement. `scripts/tests/test_orchestrator.py:97` through `scripts/tests/test_orchestrator.py:136` cover one illegal failure route but do not test terminal `advance()`/`route_failure()`/`stop()`, unchanged event counts, policy payload recording, idempotent repeated advance/failure calls, or persisted-ledger approval integrity.
- Affected acceptance criteria: explicit task verification requires tests for every non-INTAKE empty-evidence path, `None` revision/environment evidence, terminal runs, invalid next state without insertion, and policy event payloads.
- Required correction: add focused unit tests for each named requirement before accepting the implementation.

## Assessment

Both review axes fail because the confirmed Critical findings permit phase advancement without authentic, complete approval and revision evidence. The implementation does not meet Task 1's fail-closed gate requirement. No provider suggestions were supplied; all findings above are `CONFIRMED` from the current code, task brief, and implementation plan.
