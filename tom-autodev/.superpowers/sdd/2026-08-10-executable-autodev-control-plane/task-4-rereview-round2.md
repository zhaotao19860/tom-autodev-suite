# Task 4 Re-review: Fix Round 2

## Verdict

- Specification compliance: **FAIL**
- Code quality: **FAIL**
- I1 close behavior: **NOT ADDRESSED**
- I5 started-profile binding: **ADDRESSED**
- N1 run-ID ownership: **ADDRESSED**
- New Critical findings: 0
- New Important findings: 1

This was a static review of the round-2 implementation and focused tests against `task-4-rereview.md` and the latest appended `task-4-report.md`. Per instruction, the recorded test commands were not rerun. No live iCafe/KU write, BGW/XFlow command, or production worktree operation was performed.

## Review Baseline and Inputs

The workspace is not a Git repository, so no repository revision or Git diff was available. The fixed review baseline is the exact current bytes of the round-2 Change Set listed below, in this order; the SHA-256 of the ordered `shasum -a 256` manifest is `75011b6e3152da23fa02f9582153374b06f53436d23842c844d59b51e17193b0`:

`scripts/state_store.py`, `scripts/cli_transport.py`, `scripts/clients/icafe_client.py`, `scripts/clients/ku_client.py`, `scripts/knowledge_sync.py`, `scripts/orchestrator.py`, `scripts/tests/test_state_and_artifacts.py`, `scripts/tests/test_adapters.py`, `scripts/tests/test_knowledge_sync.py`, and `scripts/tests/test_orchestrator.py`.

Review input hashes:

- Task brief: `4aec9379f976c1244c8c4abe7642ccd2d3a39910ef4bdc06aef1c14f84278269`
- Original review: `7021e4110e9fa6d3ce977ed47dba74ef5a3e4fcacc00dd5e4dc0d585b265511b`
- Round-1 re-review: `7b2c99671d9698451705d3505e719693ca07ece37590a8e4b0e15d1b0f1a624f`
- Implementation report with round-2 appendix: `911fe6316f3519fafb1848c74093583de093358b56243e6a88848d944b61085f`

## Finding Classification

| Finding | Classification | Axis | Severity | Affected criterion | Blocking | Resolution |
| --- | --- | --- | --- | --- | --- | --- |
| I1 | `CONFIRMED` | Spec | Important | Required iCafe contract 7; canonical approved close input | Yes | Not addressed |
| I5 | `CONFIRMED` | Spec | Important | Required KU contract 5; started-project binding | No | Addressed |
| N1 | `CONFIRMED` | Spec | Important | Persistence contract 2-4; run-owned durable state | No | Addressed |
| N2 | `CONFIRMED` | Standards and Spec | Important | Persistence contract 2; unknown writes must not repeat | Yes | New finding |

No finding is `REJECTED_WITH_REASON` or `NEEDS_CLARIFICATION`.

## Independent Axis Reports

- **Specification:** **FAIL**. I1 rejects production-shaped KU document URLs, and N2 repeats a write after an explicitly unknown result. Both violate blocking Task 4 acceptance requirements.
- **Standards:** **FAIL**. N2 creates an unsafe recovery path and its regression test asserts the prohibited second external write, so code and test behavior agree with each other but not with the durable-write invariant.

## Remaining Finding

### I1: NOT ADDRESSED - the canonical URL validator rejects real KU document URLs

The close-level durable transaction itself is now present. It is keyed by the approved input and content hashes, stores the original expected status, replays a completed receipt before remote calls, and can reconcile a status-applied/comment-unknown attempt ([scripts/clients/icafe_client.py:399](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:399), [scripts/clients/icafe_client.py:403](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:403), [scripts/clients/icafe_client.py:407](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:407), [scripts/clients/icafe_client.py:441](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:441)). The exact replay and comment-unknown tests cover those repaired paths ([scripts/tests/test_adapters.py:540](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:540), [scripts/tests/test_adapters.py:594](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:594)).

However, `_KU_PATH` matches only the literal path `/knowledge/space/category/<repo>/<doc>` ([scripts/clients/icafe_client.py:16](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:16)). The real KU contract defines `spaceGuid` and `categoryGuid` as variable path components, for example `/knowledge/HFVrC7hq1Q/2tsPs8CtSd/E3d4LRExEl/1xosIYvQX3qxeI`. `_canonical_knowledge_url()` consequently rejects actual KU document URLs unless their first two identifiers happen to be the literal words `space` and `category` ([scripts/clients/icafe_client.py:690](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:690)).

The tests encode the same placeholder-as-literal mistake through `ku_url("doc")`, and the invalid list never includes a structurally valid real URL with non-placeholder space/category IDs ([scripts/tests/test_adapters.py:456](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:456), [scripts/tests/test_adapters.py:541](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:541)). Validate five path segments with variable safe components after `knowledge`, while retaining the exact HTTPS host, no userinfo/port/query/fragment, and non-empty repo/doc checks.

## New Important Finding

### N2. Pending close recovery automatically repeats an unknown status write

When a status update has an unknown result, the close-level and status intents remain pending. On retry, if the snapshot/current-status queries still show the original state, `_close_status_receipt()` calls `_execute_status()` again with the same pending operation key ([scripts/clients/icafe_client.py:446](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:446), [scripts/clients/icafe_client.py:486](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:486), [scripts/clients/icafe_client.py:515](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:515)). `StateStore.intent()` returns the existing intent, but `_execute_status()` still invokes `card update` again ([scripts/clients/icafe_client.py:267](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:267)).

This conflicts with the Task 4 persistence contract: an unknown result must remain pending for recovery/query and the external write must not be repeated. A stale/eventually consistent status query can report the old state after the first update was accepted, causing a second transition attempt and duplicate remote history/side effects. The new test explicitly requires two `card update` calls after the first timeout ([scripts/tests/test_adapters.py:653](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:653), [scripts/tests/test_adapters.py:704](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:704)), so the suite now codifies behavior forbidden by the brief. Keep the intent pending and return `QUERY_REQUIRED` unless authoritative remote evidence proves the original operation's result; do not infer permission to repeat from an unchanged cached/current state.

## Addressed Findings

### I5: ADDRESSED

`Orchestrator.knowledge_sync()` now validates the complete intake binding before constructing adapters: required recorded fields, exact configured profile path for the recorded project, current bytes hash against the recorded `profile_hash`, profile readiness, and loaded `project_id` equality ([scripts/orchestrator.py:80](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:80), [scripts/orchestrator.py:100](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:100), [scripts/orchestrator.py:103](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:103), [scripts/orchestrator.py:113](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:113), [scripts/orchestrator.py:116](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:116)). The focused tests cover changed bytes, changed project ID, and a noncanonical recorded path before adapter calls ([scripts/tests/test_orchestrator.py:116](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_orchestrator.py:116), [scripts/tests/test_orchestrator.py:154](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_orchestrator.py:154), [scripts/tests/test_orchestrator.py:181](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_orchestrator.py:181)).

### N1: ADDRESSED

`KnowledgeSync` now stores its construction-time run ID, rejects a different ID at the first line of `publish_phase()`, and uses the bound ID for all subsequent identities and state operations ([scripts/knowledge_sync.py:30](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:30), [scripts/knowledge_sync.py:39](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:39), [scripts/knowledge_sync.py:91](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:91)). The regression test confirms no KU/iCafe calls and no intents under either run on mismatch ([scripts/tests/test_knowledge_sync.py:390](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_knowledge_sync.py:390)).

## Assessment

Profile and run ownership are now correctly fail-closed, and the close-level receipt solves exact replay plus status-applied/comment-unknown recovery. Acceptance still fails because the URL validator rejects production-shaped KU links and the new pending-close branch repeats an external write whose result is explicitly unknown. No new Critical issue was found.
