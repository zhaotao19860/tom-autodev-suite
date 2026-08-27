# Task 10 Independent Review

Date: 2026-08-11

Reviewed the exact files and hashes listed in `task-10-review-package.md`. The
reported hashes match the files on disk. No live service was called and no
BGW/XFlow project command was run by this reviewer.

## Findings

### I1 - Important - The required six-component live preflight is still not complete

The Task 10 brief requires the read-only live preflight to be run and to check
iCafe, KU, iCode, Review, Infoflow, and iPipe (`task-10-brief.md:18`). The
implementation report records `NOT_RUN` (`task-10-report.md:57-59`). The root
controller subsequently invoked BGW and XFlow preflight, but both correctly
returned `PROJECT_NOT_READY` with `components={}` before any probe because
`~/.tom-autodev/config/projects` does not exist. This validates the fail-closed
missing-profile path only; none of the six live dependency checks ran. Keep the
result recorded as an environment blocker and do not mark Task 10 complete until
confirmed profiles exist and the authorized query-only checks produce exact
component evidence.

### I2 - Important - The new fake E2E suite does not exercise the required integrated production boundaries

The BGW/XFlow scenario only calls `start()`, `next()`, and `trace()` and stops at
INTAKE (`scripts/tests/test_fake_e2e.py:48-64`). The trace scenario inserts an
artifact, external intent/receipt, and REVIEW transition directly through the
stores (`scripts/tests/test_fake_e2e.py:66-83`). The failure scenarios likewise
inject IPIPE with `state.transition()` (`scripts/tests/test_fake_e2e.py:85-98`),
the crash scenario directly inserts a KU intent (`scripts/tests/test_fake_e2e.py:100-107`),
and G10 replaces the real runtime with a stub (`scripts/tests/test_fake_e2e.py:109-122`).
No new scenario drives iCafe, KnowledgeSync/KU, canonical Infoflow G0, Review,
`submit_to_ipipe()`, `IpipeRuntime`, and RunSummary through one run using fake
transports. Existing isolated suites do not satisfy the explicit full-trace and
meaningful production-boundary requirement. Add at least one BGW and one XFlow
integrated fake run, including production-bound failure/recovery, duplicate, G10
reject/apply, and exact event/artifact/group/role-route assertions.

### I3 - Important - `optimize()` permits replacement of the owned G10 runtime

`Orchestrator.optimize()` publicly accepts `summary_runtime` and dispatches all
operations to it without first authenticating the run (`scripts/orchestrator.py:138-164`).
The new test demonstrates the bypass by applying G10 to nonexistent `run-1`
through an arbitrary stub and accepting its success (`scripts/tests/test_fake_e2e.py:109-122`).
This contradicts the Task 8 ownership contract requiring archived summary,
proposal, result, approval-hash, and recovery receipts. Remove whole-runtime
replacement from the production method. Test seams should inject narrow
KnowledgeSync transports or a constructor/private factory while the real
RunSummary remains controller-owned, and every operation should bind the given
run to durable state.

### I4 - Important - Preflight does not bind the requested project to the loaded profile identity

`preflight()` loads `<project>.yaml` and immediately queries it without checking
that `profile.project_id == project` (`scripts/orchestrator.py:167-184`). A
temporary fake-only reproduction changed `bgw.yaml` to `project_id: xflow` and
`preflight("bgw")` returned `READY`, reporting requested project `bgw` while
querying the XFlow-identified profile. This can check the wrong repositories and
pipeline under a misleading project identity and violates the no-inference rule.
Reject project/profile mismatch before constructing probes or contexts and
constrain project lookup to a canonical profile name.

### I5 - Important - Preflight output redaction leaks credentials embedded in ordinary strings

`_redact()` masks secret-named dictionary keys and email addresses, but applies
no credential patterns to string values (`scripts/preflight.py:99-121`). A
fake-only reproduction returned
`{"detail": "Authorization: Bearer top-secret"}` unchanged in the structured
preflight result. The existing test only places a secret under a key literally
named `token` (`scripts/tests/test_live_preflight.py:100-115`), so it misses this
path. Reuse the hardened Task 8 structured redaction/secret policy and cover
Bearer/Authorization, API keys, token assignments, URL credentials, phone data,
and values split across nested structures.

### I6 - Important - `trace()` reports mutable current KU identity as if it were run-pinned

`trace()` reloads the profile path from disk and reads KU identity without
checking the recorded profile hash (`scripts/orchestrator.py:186-220`). A
fake-only reproduction started BGW, changed the profile KU parent to the XFlow
parent, and the old run's trace reported `meQ-Acjg0K09Xr` alongside the old
intake hash. The audit trace can therefore misstate the exact target used by a
run. Resolve the profile through the existing pinned-profile validation and fail
with `PROFILE_CONFLICT`, or persist and read the exact KU binding from the intake
event.

### I7 - Important - The iCode probe is not incapable of authentication writes

The live probe invokes bare `icode-cli login` once per configured repository
(`scripts/preflight.py:160-172`). `_ArgvQueryTransport` inherits process stdin
because it does not set `stdin` (`scripts/preflight.py:219-230`). The installed
iCode documentation defines bare `login` as interactive and says an unauthenticated
invocation prompts for a token (`/Users/tom/.comate/skills/.system/icode/references/login.md:1-16,28-36`).
Consequently a terminal preflight is capable of accepting a token and changing
local authentication state, despite the strict query-only contract. Use a
documented status-only operation. If the tool has no such operation, enforce a
noninteractive read-only invocation with closed stdin and reject any response
other than an already-authenticated status; add argv/stdin tests for every live
probe, not only KU.

## Verdicts

**Specification: FAIL.** I1 and I2 leave two explicit Task 10 requirements
unfulfilled. I3, I4, and I6 also break preserved Task 7/8 ownership and exact
project/profile trace contracts.

**Standards/Security: FAIL.** I3 is a G10 ownership bypass, I5 leaks secret-bearing
diagnostics, I6 corrupts audit identity under profile drift, and I7 leaves a
potentially mutable authentication command inside a purported read-only probe.

No Critical or Minor findings were identified in this review round.

## Verification Evidence

- Reviewer focused suite: 13/13 PASS.
- Root verification: full suite 418/418 PASS, schema 14/14 PASS, compileall PASS.
- Root BGW/XFlow preflight: both `PROJECT_NOT_READY`, `components={}`, zero probes.
- Fake-only reproductions confirmed I4, I5, and I6 without live requests.
- Exact BGW/XFlow KU parent constants and the single forwarding CLI entrypoint
  are present; no Codex or `tom-autorelease` runtime dependency was found in the
  reviewed Task 10 production surface.

The green suites establish regression stability for what is asserted, but do
not close the missing integrated scenarios or the live-preflight blocker above.
