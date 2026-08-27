# Task 6: Extract iCode, iPipe, and Release Runtime

## Files

- Create `scripts/clients/icode_runtime.py` and `scripts/clients/ipipe_runtime.py`.
- Modify `scripts/clients/icode_client.py`, `scripts/clients/ipipe_client.py`, and `scripts/orchestrator.py` only for the Task 6 runtime contracts.
- Create/update `scripts/tests/test_ipipe_runtime.py` and adapter/orchestrator contract tests.
- Read `/Users/tom/Desktop/skills/tom-autorelease/scripts/autorelease.py` only as source material. Production code must not import, execute, or resolve that Skill at runtime.

## Interfaces

- `IcodeRuntime.preflight(repo_path: Path) -> dict`
- `IcodeRuntime.submit(change_set: dict, approval: dict) -> dict`
- `IpipeRuntime.discover(profile: dict, revision_set: dict) -> dict`
- `IpipeRuntime.trigger(profile: dict, revision_set: dict, approval: dict) -> dict`
- `IpipeRuntime.monitor(build_id: str, deadline: str) -> dict`
- `IpipeRuntime.rerun(stage_build_id: str, approval: dict) -> dict`
- `IpipeRuntime.verify_release(build_id: str, revision_set: dict) -> dict`

## iCode contract

1. Use the real Comate system iCode CLI shape through an injected argv transport. Production discovery may resolve `ICODE_CLI_PATH`, `icode`, `~/.icode/bin/icode`, `icode-cli`, or `~/.icode/bin/icode-cli`, but it must verify the chosen binary exposes top-level `api`, `git`, and `login` commands. Never depend on an interactive shell alias.
2. `preflight()` verifies the path is a registered Git worktree at the recorded baseline, the system iCode Skill exists, the real CLI is executable, required subcommands exist, login succeeds, module/target branch are present, and the worktree belongs to the run. It must not amend, commit, push, or alter the repository.
3. `submit()` accepts only a reviewed Change Set whose canonical input hash matches a run-bound G7 approval and whose business/test repository revisions match the reviewed revision set. Invoke the real system `icode ... git push_cr` path; never use raw `git push` or an alias fallback.
4. Persist a durable write intent before submission. Query existing NEW CRs by module/revision/owner/card/target branch before a first write and after an unknown result. Exact existing CR/patchset returns the prior receipt; conflicting revision, branch, owner or card fails closed. Unknown submit results remain pending and are query-only on recovery; never repeat `push_cr` automatically.
5. A verified submission receipt contains run/change-set/revision-set IDs, repository/module/target branch, commit revision, CR/change number, patchset/current revision, canonical CR URL and evidence refs. Reject missing, malformed, cross-run, stale-baseline or secret-bearing responses.

## iPipe API and transport contract

1. Use an injected HTTP/API transport compatible with `ipipe-pipeline-assistant`. Production auth may read `COMATE_AUTH_TOKEN` then `~/.comate/login`, normalize the `Bearer-` prefix, and send only `x-ac-Authorization`; credentials and raw authorization headers must never enter StateStore, receipts, logs or errors.
2. Required read APIs include pipeline by ID/name/module, builds by revision/module/pipeline, build by ID, pipeline stage information, failed jobs/stage detail, and release info. Required write APIs are trigger-by-revision and manual/reexecute stage only.
3. HTTP transport uses bounded timeouts, bounded response/error bodies, stable reason codes, and bounded retry only for read-only transient failures. A 4xx other than 429 is definite. External writes are never automatically retried by the HTTP transport.
4. Parse exit/HTTP status and business response code. A successful transport response without the expected pipeline/build/stage/release identity is not success.

## Discovery, trigger, monitoring, and release contract

1. `discover()` uses only the validated project profile: exact `pipeline_profile.pipeline_id`, business/test repository modules/revisions, target branches, allowlisted parameters, stage classes, release rule, and environment fingerprint. It queries candidate builds/pipelines and accepts exactly one candidate matching pipeline ID, module and full revision set. Zero or multiple candidates return stable query/confirmation errors; never guess by name/directory.
2. `trigger()` requires a run-bound G8 approval bound to the exact pipeline ID, module/revision set, environment fingerprint and allowlisted parameter payload. Filter parameters by `allowed_parameters`; reject forbidden keys rather than silently forwarding them. Persist intent before the call, then query and verify build ID, trigger revision, module, pipeline ID and parameters. Unknown result is query-only/no replay.
3. `monitor()` is read-only and deadline-bounded. Each poll verifies build ownership by run/pipeline/module/revisions. Inspect stage states before trusting the aggregate pipeline state, so a failed stage cannot be hidden by a pending/success aggregate. Return normalized stage/job evidence, bounded log excerpts, environment fingerprint, failure signature and stable classification without sleeping in unit tests.
4. Distinguish success, failure, manual-stage wait, timeout, revision mismatch, pipeline transient, environment/test/code failure, and release waiting. Revision mismatch invalidates the evidence and stops; it is never repaired or reused.
5. `rerun()` accepts only a failed/manual-waiting `stage_build_id` that belongs to the verified build and a run-bound G8 approval for that exact stage/input hash. Enforce a durable per-stage retry budget. Persist intent before the write; completed receipts replay, pending unknowns query only, and a changed stage/build/revision/approval is a conflict.
6. `verify_release()` queries release evidence after pipeline success. Require exact module, branch, complete business/test revision set, associated pipeline build ID, configured release rule and success status. A revision/build mismatch returns `REVISION_MISMATCH`; no partial or latest-release guess is accepted.

## Orchestration and persistence contract

1. Store every iCode/iPipe write intent and verified receipt under the run. Evidence refs are canonical non-secret identifiers for CR, revisions, build, stage, jobs and release.
2. G7 binds iCode submission/patchset. G8 binds pipeline trigger, manual-stage continuation and each failed-stage rerun. G9 binds the exact release evidence/action. Approval input changes invalidate the operation before any external call.
3. iCode/iPipe failures route to the existing Diagnose state and Task 5 collaboration role routing: code/interface/revision failures -> development; tests/environment -> test; mixed -> both; platform/auth/release rule -> project owner.
4. Mac may only inspect source, create reviewed source/test changes, invoke local source Review, submit iCode and parse remote evidence. It must never run BGW/XFlow compile, unit, regression, integration, Docker, NCS, simulator or release commands locally.

## Extraction boundaries

- Extract only focused reusable behavior from `tom-autorelease`: real iCode CLI discovery/login/API/CR/push_cr, iPipe query/trigger/stage/job/release normalization, bounded failure evidence, retry budget and release verification.
- Do not copy the old CLI parser, interactive approval loops, Infoflow notification code, `/tmp/tom-autorelease` state, module lock implementation, local Git amend/commit helpers, `node_modules`, or runtime path references.
- Use current `StateStore`, `ApprovalLedger`, `CollaborationSession`, profile and evidence conventions; do not create parallel persistence or approval systems.

## Required TDD and verification

1. Add RED fake-contract tests for iCode binary/subcommand/login/preflight failures, raw-push prohibition, run/hash/G7 binding, exact existing CR reconciliation, unknown submit no-replay, CR identity conflicts, iPipe allowlist rejection, ambiguous discovery, revision association, stage-before-aggregate failure, bounded read retries/no write retries, G8 trigger/rerun binding, retry budget, manual wait, timeout, release mismatch and exact release success.
2. Include concurrency/restart tests for iCode submit, iPipe trigger and rerun intents. A non-owner must not execute a write; completed receipts replay; pending intents reconcile/query only.
3. Record focused RED failures before production edits. Use fake transports and temporary Git repositories/worktrees only.
4. Run focused runtime/adapters/orchestrator suites, full `scripts/tests` discovery, `py_compile`/`compileall`, and scans proving no `tom-autorelease`, raw `git push`, local project command, or secret persistence in Task 6 production files.
5. Do not perform a live iCode submit, iPipe trigger/rerun, project build/test, or release action in tests.
