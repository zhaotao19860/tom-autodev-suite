# Tom Autodev Simple Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/tom-autodev` the simple daily interface for starting, viewing, continuing, and stopping runs while the existing WorkerDriver owns durable progression.

**Architecture:** Add a thin `AgentBridge` around the existing `Orchestrator` and `worker_driver` APIs, expose the bridge to the Comate skill through a few CLI commands, and resolve a card ID to a run only when the match is unique. Keep approval delivery, evidence checks, persistence, and external controllers on their existing code paths; update the skill and docs to describe the callable interface accurately.

**Tech Stack:** Python 3, argparse CLI, SQLite-backed state/artifacts/approval stores, existing Comate and Infoflow adapters, Markdown skill/docs, unittest.

**Spec:** `docs/superpowers/specs/2026-09-24-tom-autodev-simple-entry-design.md`

## Global Constraints

- Comate remains the only Agent entry point.
- Preserve G0–G10, exact `input_hash` binding, EvidenceGate, locks, revision/environment pinning, and idempotent external side effects.
- The Agent returns only the current ProducerJob's DraftContent; the worker constructs envelopes and controls state transitions.
- Keep CLI `resume` read-only; use a new `continue` action for WorkerDriver progression.
- Do not introduce Temporal Server, a second persistence store, a dashboard, or a business-project execution path.
- Project builds, tests, simulation, and release continue to run only through configured iPipe.

## Review Focus

1. A card with no active run resolves as `RUN_NOT_FOUND`; multiple active runs for the same card return candidates and never select one silently. Pin this in the target resolver tests.
2. A repeated `drive` call while a ProducerJob is waiting returns the same job identity and does not create another job. Pin this in AgentBridge tests.
3. A draft with the wrong job/action or stale hash is rejected without completing a phase or opening a different gate. Pin this in AgentBridge/worker delegation tests.
4. A valid approval handoff resumes once; absent, stale, duplicate, or mismatched handoffs fall back safely or return the existing recovery reason without repeating an external write. Pin this in continue tests using the existing handoff fixtures.
5. Missing or transient iPipe runtime parks with an explicit reason and cannot produce success evidence. Pin this in the CLI/bridge integration tests with a fake transport.

---

### Task 1: Add the AgentBridge and safe run target resolution

**Files:**
- Create: `tom-autodev/scripts/agent_bridge.py`
- Create: `tom-autodev/scripts/tests/test_agent_bridge.py`
- Reference: `tom-autodev/scripts/worker_driver.py`
- Reference: `tom-autodev/scripts/run_brief.py`

**Interfaces:**
- Consumes: `Orchestrator`, `worker_driver.advance`, `worker_driver.resume`, `worker_driver.submit_draft`, and a run lock manager.
- Produces: `AgentBridge(orchestrator, *, locks, ipipe_api_factory=None, icode_skill=None)` with `drive(run_id)`, `continue_run(run_id)`, `submit_draft(run_id, job_id, draft_content)`, `status(target)`, and `stop(target)` methods. Each returns a JSON-safe dict retaining the underlying `reason_code` and relevant worker result.
- `resolve_run_target(orchestrator, target)` accepts a 32-character run id or an iCafe card id. For card ids, it reads the event log's intake identity and latest state; it returns `RUN_NOT_FOUND`, one resolved run, or `AMBIGUOUS_RUN` with short candidate summaries.

- [ ] **Step 1: Add failing bridge contract tests** for forwarding `drive` with the shared run locks, preserving the ProducerJob payload, distinguishing parked approval/producer/remote states, and resolving zero/one/multiple matching card runs.
- [ ] **Step 2: Run the focused test file** with `python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_agent_bridge.py' -v`; confirm the new API is missing or behavior is incorrect.
- [ ] **Step 3: Implement target resolution and the thin bridge** by delegating to WorkerDriver; do not duplicate `next`, schema, hash, gate, or transition logic.
- [ ] **Step 4: Run the focused test file** and confirm target ambiguity and WorkerDriver delegation cases pass.
- [ ] **Step 5: Commit** as `feat: add tom-autodev agent bridge`.

### Task 2: Expose bridge actions through the CLI

**Files:**
- Modify: `tom-autodev/scripts/orchestrator.py` (`main` parser and command dispatch)
- Modify: `tom-autodev/scripts/tests/test_cli_operations.py`
- Reference: `tom-autodev/scripts/clients/ipipe_client.py`
- Reference: `tom-autodev/scripts/approval_delivery.py`

**Interfaces:**
- Add `drive TARGET`, `continue TARGET`, and `submit-draft TARGET JOB_ID DRAFT_JSON` commands. `TARGET` accepts a run id or a uniquely resolved card id. `DRAFT_JSON` is a path containing DraftContent only, never an ArtifactEnvelope.
- Keep `resume RUN_ID` unchanged as checkpoint inspection.
- `drive` and `continue` instantiate AgentBridge with the configured run locks and run-bound iPipe adapter. `submit-draft` passes content to WorkerDriver and surfaces `APPROVAL_REQUIRED` with its exact gate and hash.
- Gate delivery uses the existing request-approval path and idempotency key. It must not deliver another card when the same approval/hash already has a durable delivery receipt.

- [ ] **Step 1: Add failing CLI tests** for the new command parsing, JSON DraftContent loading, correct WorkerDriver method dispatch, card resolution ambiguity, and preserving the existing read-only `resume` behavior.
- [ ] **Step 2: Run the focused CLI tests** with `python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_cli_operations.py' -v`; confirm the commands are not yet registered.
- [ ] **Step 3: Add the CLI wrappers** and factor only the adapter construction needed by AgentBridge; keep gate delivery on `_request_approval`/`request_infoflow_approval` and use the real run-bound runtime factories.
- [ ] **Step 4: Add fake-runtime cases** showing `drive` parks if the iPipe transport is unavailable, and that an approved/replayed submission is not sent twice.
- [ ] **Step 5: Run the focused CLI tests** and confirm both new actions and legacy command behavior pass.
- [ ] **Step 6: Commit** as `feat: expose worker drive and draft commands`.

### Task 3: Make `/tom-autodev` use the bridge as its only normal workflow

**Files:**
- Modify: `tom-autodev/SKILL.md`
- Modify: `tom-autodev/references/producer-contract.md`
- Modify: `tom-autodev/scripts/tests/test_producer_validation.py` only if the bridge contract exposes a new producer response requirement

**Interfaces:**
- The skill's ordinary path calls `start`, `drive`, `submit-draft`, `continue`, `status`, and `stop` through supported interfaces. It does not invoke `next`/`complete-phase` to manually sequence phases.
- For a parked ProducerJob, the skill loads only its pinned inputs, schema, and applicable child skill, then returns DraftContent through `submit-draft`.
- For `APPROVAL_REQUIRED`, the skill requests the exact existing gate/hash and stops; it does not regenerate the approved candidate. After approval, `continue` consumes the handoff and worker reuses saved content.
- Error responses retain the existing recovery contract and direct advanced recovery to the appropriate reference.

- [ ] **Step 1: Add or update focused contract assertions** that the producer-facing instructions submit content only and never call `complete-phase` or `advance`.
- [ ] **Step 2: Run the producer contract test file** with `python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_producer_validation.py' -v` and confirm any new assertions fail before the skill/bridge wording changes.
- [ ] **Step 3: Rewrite the Start/Continue section** as a short intent router and callable command contract; retain critical safety boundaries and link phase, approval, and failure details to references.
- [ ] **Step 4: Update the Producer Contract** to name the actual supported `submit-draft` command/API and the JSON-file contract.
- [ ] **Step 5: Run the focused producer contract test file** and inspect the skill for command names that do not exist.
- [ ] **Step 6: Commit** as `docs: route tom-autodev through worker bridge`.

### Task 4: Align README, status output, and operations reference

**Files:**
- Modify: `README.md`
- Modify: `docs/OPERATIONS.md`
- Modify: `tom-autodev/scripts/run_brief.py`
- Create: `tom-autodev/scripts/tests/test_run_brief.py`

**Interfaces:**
- README's daily path demonstrates a single `/tom-autodev` request and simple progress/continue interaction; keep install, project registration, watchers, and maintenance commands in setup/operations sections.
- `status` accepts a run id or card id and renders the same compact projection used by the skill: current phase, human-readable wait state, owner, next action, and actionable blocker. Keep full event JSON available through `--json`.
- Do not expose raw hashes, full event ids, or adapter exception text in default user-facing status. Detailed approval identity remains available in the approval card and maintenance output.
- Operations reference clearly distinguishes `resume` inspection, `continue` worker progression, and `submit-draft` content submission.

- [ ] **Step 1: Add failing status-render tests** for producer wait, approval wait, external intent recovery, terminal state, and card-id resolution. Include that detailed JSON remains available.
- [ ] **Step 2: Run the focused status tests** with `python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_run_brief.py' -v` and confirm they fail against current wording/target behavior.
- [ ] **Step 3: Update the status projection and CLI target handling** without changing approval decisions or their hashes.
- [ ] **Step 4: Rewrite README daily-use examples and the operations command table** to match the implemented CLI; move internal procedures out of the Quick Start.
- [ ] **Step 5: Run status and CLI focused tests**, then check Markdown links and command references against argparse definitions.
- [ ] **Step 6: Commit** as `docs: simplify tom-autodev daily workflow`.

### Task 5: Exercise the complete bridge handoff and finish the docs

**Files:**
- Modify: `tom-autodev/scripts/tests/test_fake_e2e.py`
- Modify: `evals/skill-scenarios.json`
- Modify: `evals/README.md`
- Reference: `docs/superpowers/specs/2026-09-24-tom-autodev-simple-entry-design.md`

**Interfaces:**
- The fake end-to-end flow starts a confirmed card, drives to the first producer, submits a DraftContent, parks at the expected approval, resumes from a valid handoff, and proves replay does not duplicate external side effects.
- The skill scenario checks that the user can start and continue by card identity without manually supplying event IDs, artifact IDs, or hashes; it still checks explicit project/card confirmation and does not permit skipped approvals.

- [ ] **Step 1: Add the end-to-end failing case** using the existing fake profile, fake adapters, and temporary state/worktrees; assert the run stops at each expected human gate.
- [ ] **Step 2: Run the focused end-to-end test** with `python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_fake_e2e.py' -v` and confirm the new bridge handoff path is not yet satisfied.
- [ ] **Step 3: Add one skill scenario** for start/status/continue and update its evaluation instructions with the expected user-visible behavior.
- [ ] **Step 4: Run the focused integration/evaluation checks** and correct only failures in this feature's path.
- [ ] **Step 5: Run the control-plane unit suite** with `python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_*.py' -q`; no business project build or test command is permitted.
- [ ] **Step 6: Review the final diff and commit** as `test: cover tom-autodev bridge handoffs`.

## Execution Notes

- Do not start live iCafe, KU, Infoflow, iCode, or iPipe actions during implementation; use injected fakes and temporary state only.
- Keep changes on the current `phase1-workflowspec` branch and preserve unrelated user changes if any appear.
- Before each commit, inspect `git diff --check` and stage only files named in that task.
- If implementation reveals that profile discovery, watcher installation, or a new persistence schema is required, stop and return to design review instead of silently expanding scope.
