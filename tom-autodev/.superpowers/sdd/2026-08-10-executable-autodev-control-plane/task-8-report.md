# Task 8 Report: Run Summary and G10 Optimization

Status: COMPLETE (implementation verification pending independent review)

## Design

- Added `RunSummary.build(run_id)`, `propose(summary, allowed_roots)`, and `apply(proposal_id, approval_id)`.
- The summary reads durable events, approvals, integrity-checked artifacts, collaboration receipts, and iPipe receipts. It groups stable failure signatures and redacts secrets/email addresses.
- Proposals are derived only from the durable run event candidate, content-hashed, persisted atomically in `StateStore`, archived only through `KnowledgeSync.publish_phase`, and constrained to roots inside the `tom-autodev` control-plane root.
- G10 requires the exact run-bound `G10` approval and candidate hash. Business, profile, iPipe/pipeline targets, symlinks, paths outside allowed roots, shell syntax, and project execution commands are rejected.
- Validation uses `subprocess.run(shlex.split(...))` with no shell and a fixed allowlist: `python3 -m unittest`, `discover -s scripts/tests`, or `scripts.tests.*`. Local BGW/XFlow commands cannot enter this path.
- Application checks prior hashes, writes atomically, and rolls every target back after validation, write, or result-archive failure. A proposal archive failure is terminal and cannot be applied.

## Changed Files

- `scripts/run_summary.py`: new summary/proposal/G10 controller.
- `scripts/state_store.py`: durable proposal index and verified external-result query.
- `scripts/artifact_store.py`: integrity-checked per-run artifact query.
- `scripts/approval_ledger.py`: run-scoped approval query.
- `scripts/orchestrator.py`: owned `run_summary()` factory.
- `SKILL.md`: strict post-run G10 process and local-execution boundary.
- `scripts/tests/test_run_summary.py`: 14 fake-only tests.

## TDD Evidence

Initial RED command:

```text
python3 -m unittest tom-autodev/scripts/tests/test_run_summary.py -v
ModuleNotFoundError: No module named 'run_summary'
```

This initial RED was the expected absence of the new module, not a behavioral assertion. Subsequent behavior-driven RED tests exposed and closed these real gaps:

- Symlink target was resolved before symlink validation.
- Result knowledge-archive failure left a local modification applied.
- Proposal knowledge-archive failure could leave an applyable proposal.

Focused GREEN command:

```text
cd tom-autodev && python3 -m unittest scripts/tests/test_run_summary.py -v
Ran 14 tests ... OK
```

The tests use only temporary SQLite/artifact roots, fake KnowledgeSync, and injected validation runners. No live iCafe/KU/Infoflow/iCode/iPipe calls occurred.

## Verification

```text
cd tom-autodev && python3 -m unittest discover -s scripts/tests -v
Ran 389 tests in 22.552s
OK

python3 -m compileall -q tom-autodev/scripts
exit 0
```

The first full-suite invocation from the workspace root had four `ModuleNotFoundError: scripts` discovery-import errors because that command omitted the `tom-autodev` package root. The corrected command above is the authoritative full-suite result.

Schema coverage: the full suite includes `test_schema_validation` and passed. Task 8 introduced no new phase schema because a run summary/G10 proposal is an ArtifactStore/StateStore control-plane record, not a PhaseProtocol phase artifact.

Safety scan:

```text
rg '(from|import)\\s+tom_autorelease|tom-autorelease|os\\.system|shell\\s*=\\s*True|subprocess\\.(call|check_call|Popen)' <Task-8 production files>
no matches
```

The only `subprocess.run` is the fixed G10 control-plane validation boundary in `run_summary.py`; candidate commands are parsed with `shlex.split`, checked against the allowlist, and never use a shell. The safety test rejects `make test`, Docker/shell chaining, and `../bgw/tests` discovery.

## File Hashes

```text
0ed97ee7c45677cd3dea2e8a4fd0dc205d7f2de2d63cb832a0affc8c6223ad66  scripts/run_summary.py
0dd131eaaf9fc06bb7de386a0deb0c8923d70962fc506b4c2d69cd7d0af40380  scripts/state_store.py
a49eb38faf5b8afbc75b2ae4ed403eef2924bcfb60770889a210dba6aee39f57  scripts/artifact_store.py
ac384d1019cfb7034f143577b85492343b2ce43f5b1d2727349bdc25ff63f479  scripts/approval_ledger.py
85aa209c87a0f6b441948edc471993ff8a3a0625369ff993c79a1b7d74addd7b  scripts/orchestrator.py
4740cc9c75b310878c13178b14b9aa6d5fa92174f3aef3848e987ec663081497  SKILL.md
a5f3cc8186ddbaaf50893cffc72a5ac3ad8d3115064d69c6e37a158635d6141d  scripts/tests/test_run_summary.py
```

## Concerns

- G10 is intentionally a separate terminal post-run scope; it does not change PhaseProtocol or introduce a new production phase.
- `KnowledgeSync` must be supplied by the owning orchestrator/controller for proposal/result archival. The implementation fails closed when a supplied archive boundary fails; fake tests never make network writes.

## Task 8 Fix Round 1

Status: implementation complete; awaiting independent re-review.

The independent review's C1-C3 and I1-I6 were addressed as one coherent
durable-control-plane repair:

- KU archival is mandatory. Missing, failed, malformed, or raising
  `KnowledgeSync.publish_phase()` boundaries return a stable failure; an
  unarchived proposal is terminal and cannot be applied.
- Proposals now use a versioned immutable envelope. SQLite stores and checks
  the complete envelope hash, denormalized run/candidate columns, deterministic
  proposal ID, approval ID, owner token, and journal. Every apply/replay reloads
  and validates all of these bindings.
- Application persists an exact before-write journal, then uses a SQLite CAS to
  claim `APPLYING`. It revalidates roots, symlinks, and baselines immediately
  before each write, preserves modes, catches validation/archive exceptions,
  rolls back from the durable journal, and returns `G10_APPLY_IN_PROGRESS` to a
  same-process competing caller. A restarted process rolls an interrupted
  operation back before it can be reused.
- Run summaries now classify `RELEASE_SUCCESS` as success even with historical
  repaired failures. Summary/G10 artifacts are excluded from their own source
  metrics, so repeated unchanged builds have a stable hash and reuse the same
  artifact. `propose()` accepts only an integrity-checked `run-summary` artifact.
- Failure/candidate extraction reads the versioned nested routed-evidence form;
  failure taxonomy accepts `*_FAILED`, `*_FAILURE`, timeout, and error reasons.
  Redaction covers Bearer/Authorization/API-key/token/credential/password forms,
  email, and phone data. Candidate source content containing a secret is rejected
  before it reaches proposal persistence.

Behavior repair validation was driven from the independent review reproductions:
missing/raising KU archive boundary, validation exception, partial write,
proposal run-field tamper, terminal replay with another approval, `RELEASE_SUCCESS`,
repeat build, nested failure evidence, forged summary, symlink root/ancestor, and
boundary-split secrets. The pre-repair reviewer transcript is the clean RED
evidence for these scenarios; the first implementation-focused run after the
stricter versioned-envelope migration failed until test candidates were upgraded
to the required `schema_version: "1"`, then passed after the migration fix.

Verification:

```text
cd tom-autodev && python3 -m unittest scripts/tests/test_run_summary.py -v
Ran 18 tests in 0.302s
OK

cd tom-autodev && python3 -m unittest discover -s scripts/tests -v
Ran 390 tests in 23.664s
OK

cd tom-autodev && python3 -m compileall -q scripts
exit 0
```

Schema validation subset: 14/14 PASS. State/artifact integrity subset: 15/15
PASS. No live transport, local BGW/XFlow build/test, Docker/NCS/simulator, Git
initialization, or production write was used.

Round-1 hashes:

```text
db7b4e02a1926317c12d052f868b81e640850813233c20ad69435e1daaa478b6  scripts/run_summary.py
836dfbc7c4a90d133978498193de8ba3b34bbb8af0c6126d6a2f71aa736c2fb6  scripts/state_store.py
d6ee3b070e43ad2b04a682fc2df509072bf88b11080fb63cfc0ff846bafdc359  scripts/tests/test_run_summary.py
```

Residual concern: result archival may itself be unavailable while reporting a
rollback; the durable ledger records the failure and the proposal is terminal,
so it cannot be replayed into another mutation. A later controller recovery can
surface this durable handoff without attempting a business or pipeline action.

## Task 8 Fix Round 2

Focused verification: `python3 -m unittest scripts/tests/test_run_summary.py -v`
ran 19 tests in 0.251s, all PASS. `python3 -m compileall -q scripts` PASS.

Changes completed in this pass: proposal records begin in `ARCHIVING` and become
applyable only after a durable KnowledgeSync receipt; apply rechecks that receipt.
Rejected approval IDs are stored in the durable column and same/wrong replay is
authenticated. A live foreign OS PID is treated as in-progress instead of being
rolled back. Candidate narrative fields now have explicit type validation and
quoted Authorization/API-key values are both redacted and rejected before
persistence.

Round-2 hashes:

```text
452f53b2ae7810f2e64f8544516b03463cc9b7a59d34fd737953d1241e54fd6d  scripts/run_summary.py
421ce7c6bd7d48be31f7482c9f8d0f02c10717552f607be3af9ba5d6469783fc  scripts/state_store.py
24c2f2854c82a0703ec99e5c776973d0652b8f65b930adda2be32f8ef55efd66  scripts/tests/test_run_summary.py
```

Remaining concern: this pass does not yet provide the requested dirfd/no-follow
atomic parent-directory replacement or a durable lease/heartbeat protocol. Those
two filesystem/concurrency hardening items require a further repair round before
the Task 8 result can be accepted.

Round-2 completion update: G10 writes and rollback now traverse from an allowed
root directory fd with `O_DIRECTORY|O_NOFOLLOW`, create a no-follow temporary
file in that same directory fd, and use fd-relative `os.replace`; unlink rollback
uses the same confined traversal. Journal records now carry allowed roots and a
canonical journal hash. A live foreign PID is not recovered. Final verification:
focused 19/19 PASS, full control-plane suite 395/395 PASS in 18.921s, and
`python3 -m compileall -q scripts` PASS. Residual work for a later review is a
durable lease heartbeat/expiry policy beyond OS-PID liveness.

Lease completion update: APPLYING rows now persist `heartbeat_at` and
`lease_expires_at`; `RunSummary.heartbeat()` delegates to an owner-token-bound
SQLite heartbeat API. Recovery returns `G10_APPLY_IN_PROGRESS` while the lease is
fresh or PID liveness is not conclusively absent. Only an expired lease plus a
`ProcessLookupError` PID result permits rollback recovery; permission/other OS
errors fail closed as live/ambiguous. Focused suite remains 19/19 PASS and
compileall remains PASS after this addition.

## Task 8 Fix Round 2 Final Verification

Status: complete for the durable lease/heartbeat follow-up.

The final repair makes the APPLYING lease part of the claim transaction, rather
than relying on a nullable post-claim value. `RunSummary.heartbeat()` now
exposes the owner-token-bound durable heartbeat operation. A contender may
recover an APPLYING proposal only after its lease has expired and the operating
system conclusively reports the stored owner PID absent. Invalid PID data,
permission errors, malformed lease timestamps, live owners, and fresh leases
all fail closed as `G10_APPLY_IN_PROGRESS`. Journal creation canonicalizes the
allowed roots before persisting them, so temporary-root aliases cannot produce
an empty journal in the recovery path.

Six lease regression behaviors passed:

1. Correct owner token renews the lease.
2. Wrong owner token is rejected with `OPTIMIZATION_OWNER_MISMATCH`.
3. Expired lease plus dead PID rolls back with `G10_INTERRUPTED_ROLLED_BACK`.
4. Expired lease plus live PID remains `G10_APPLY_IN_PROGRESS`.
5. Fresh lease plus dead PID remains `G10_APPLY_IN_PROGRESS`.
6. Ambiguous PID liveness remains `G10_APPLY_IN_PROGRESS`.

Final verification (all against temporary SQLite/artifact roots and fake
boundaries only):

```text
cd tom-autodev && python3 -m unittest scripts/tests/test_run_summary.py -v
Ran 22 tests in 0.327s
OK

cd tom-autodev && python3 -m unittest discover -s scripts/tests -q
Ran 398 tests in 22.451s
OK

cd tom-autodev && python3 -m unittest scripts/tests/test_schema_validation.py -q
Ran 14 tests in 0.010s
OK

cd tom-autodev && python3 -m compileall -q scripts
exit 0

rg -n --glob '*.py' '(tom-autorelease|os\\.system|shell\\s*=\\s*True|subprocess\\.(call|check_call|Popen))' \
  scripts/run_summary.py scripts/state_store.py scripts/artifact_store.py scripts/orchestrator.py
no matches
```

Final hashes:

```text
0fd202b0d3741be8e4665327aa6ee1c42fdefd63fda8d07c7026f65d7be3e2d0  scripts/run_summary.py
9aa1dd1100c5d4d05af4eca87bcefb7c6eb7a5b1dfc576ba436bc15503252b52  scripts/state_store.py
dc443d57c817f04dd9a524ab18da6c5f1525bb0505742075537e10ed72a2d1aa  scripts/tests/test_run_summary.py
```

Residual concern: result archival remains a separate durable handoff concern
described above; this lease repair neither loosens KU archival requirements nor
permits a failed archival path to apply a further mutation.

## Task 8 Fix Round 3

Status: complete for C1/C2/I5/I6/N1 re-review remediation.

### Design Changes

- Proposal and result receipts are persisted in distinct columns. A proposal
  starts in `ARCHIVING` and may enter `PROPOSED` only after a verified receipt.
  An operation enters `RESULT_ARCHIVING` before result publication and becomes
  `ARCHIVE_PENDING` on failed publication; a later `apply()` idempotently
  republishes the durable result. `APPLIED` and `ROLLED_BACK` require a verified
  result receipt.
- Receipt validation rejects bare success mappings and binds exact `run_id`,
  generated archive content hash, KU child/index identities, iCafe comment
  identity, schema version, and canonical ordered evidence references.
- A rollback failure now records `RECOVERY_REQUIRED` with the intact journal
  and returns `G10_ROLLBACK_RECOVERY_REQUIRED`. It never uses a terminal status
  or a `...ROLLED_BACK` reason until a retry restores bytes and archives the
  verified result.
- Journal schema version 2 binds proposal/run/candidate/approval identity,
  canonical allowed roots, ordered candidate target identities, exact backup
  hashes, and a claim containing the owner-token proof, PID, and initial lease
  grant. StateStore verifies those entries before claiming; every recovery
  verifies the durable binding again before rollback.
- Baseline journaling opens the root and target parent directories with
  `O_DIRECTORY|O_NOFOLLOW`, records root/parent device+inode identities, and
  reads the baseline through the pinned parent fd. The apply write uses that
  same fd and also verifies that the current pathname still resolves to the
  recorded root/parent identity. Recovery reopens and requires those identities
  before any rollback write or unlink.
- Redaction and candidate-secret detection share structured JSON handling plus
  escaped quoted-value, header, URL credential, and query-key handling.

### New Fake-Only Regression Coverage

`scripts/tests/test_run_summary.py` now covers:

1. Bare, wrong-run, wrong-hash, incomplete KU, and noncanonical-evidence
   receipts never make a proposal applyable.
2. Result archival remains durable in `ARCHIVE_PENDING` and resumes to a
   receipt-backed terminal result.
3. Rollback failure is nonterminal and a later retry restores then archives it.
4. Real parent-directory replacement is detected before a write can target the
   replacement directory.
5. Multiword/escaped structured values, headers, URL credentials, and query
   secrets are fully redacted and rejected by candidate detection.
6. A journal whose public hash was recomputed after an in-root cross-target
   substitution is rejected before recovery writes it.

### Verification

```text
cd tom-autodev && python3 -m unittest scripts/tests/test_run_summary.py -q
Ran 29 tests in 0.558s
OK

cd tom-autodev && python3 -m unittest discover -s scripts/tests -q
Ran 405 tests in 25.609s
OK

cd tom-autodev && python3 -m unittest scripts/tests/test_schema_validation.py -q
Ran 14 tests in 0.012s
OK

cd tom-autodev && python3 -m compileall -q scripts
exit 0

rg -n --glob '*.py' '(tom-autorelease|os\\.system|shell\\s*=\\s*True|subprocess\\.(call|check_call|Popen))' \
  scripts/run_summary.py scripts/state_store.py scripts/artifact_store.py scripts/orchestrator.py
no matches
```

No live iCafe/KU/Infoflow/iCode/iPipe operation, project build/test, Docker/NCS
operation, or Git operation occurred. All tests used temporary SQLite/artifact
roots and fake KnowledgeSync or injected validation boundaries.

### Round 3 Hashes

```text
49824617e789f10b0ab246b1540e2f1e15687ef63e6cda3c9b945c0a5a4aa443  scripts/run_summary.py
0bcd989d6fcfaeef305e9e8f4a83adf985ee82ef47025d4bcb1d10c59fae0bae  scripts/state_store.py
1a788f1efdb893293cd092d0f3f81323fdcd49abf06ef5a5c723cfc59b2c684e  scripts/tests/test_run_summary.py
```

Residual concern: pending result archival intentionally leaves the mutation in
a nonterminal, visible recovery state rather than claiming a released result.
It requires the configured KnowledgeSync boundary to recover; it never creates
a business-repository, profile, pipeline, or external delivery side effect.
