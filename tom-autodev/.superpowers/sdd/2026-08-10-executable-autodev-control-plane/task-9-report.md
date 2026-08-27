# Task 9 Implementation Report

## Status

PASS. Updated the Comate-only parent, all stage skills, C/C++ and NPL language skills, BGW/XFlow project skills, and setup skill. Added phase contracts/checklists without changing runtime code or invoking external systems.

## Changed Skills and References

- `tom-autodev/SKILL.md`: Comate-only entry boundary, remote execution boundary, ArtifactEnvelope/KU/iCafe contract, child ownership and G10 wording.
- `tom-grill`, `tom-spec`, `tom-tasks`, `tom-plan`, `tom-implement`, `tom-review`, `tom-diagnose`: phase-specific input/output, schema, KU persistence, iCafe comment, approval, and stop contracts; each has `references/phase-contract.md`.
- `tom-lang-c-cpp`: concrete source-review, external test, fixture, failure-class, and iPipe-only rules in `references/testing-and-review.md`.
- `tom-lang-npl`: construct/field/stage review checklist, deterministic packet cases, and remote failure evidence requirements in `references/npl-core-rules.md` and `references/error-patterns.md`.
- `tom-project-bgw`: explicit repository/test/environment/knowledge-graph profile and stable external boundary requirements.
- `tom-project-xflow`: explicit NPL/test/chip/NCS/environment/knowledge-graph profile and remote-only execution requirements.
- `setup-tom-autodev`: explicit Comate-only setup, independent test ownership, graph freshness, and remote stage declarations.

All phase artifacts are required to carry the existing runtime envelope fields (`run_id`, `phase`, `task_id`, predecessor/content/input hashes, source revisions, KU/iCafe receipts, evidence refs, approval binding). Child skills return to the parent and do not own external adapters.

## Validation

Command:

```text
python3 /Users/tom/.codex/skills/.system/skill-creator/scripts/quick_validate.py <skill-dir>
```

Exact output for every modified directory:

```text
tom-autodev       Skill is valid!
tom-grill         Skill is valid!
tom-spec          Skill is valid!
tom-tasks         Skill is valid!
tom-plan          Skill is valid!
tom-implement     Skill is valid!
tom-review        Skill is valid!
tom-diagnose      Skill is valid!
tom-lang-c-cpp    Skill is valid!
tom-lang-npl      Skill is valid!
tom-project-bgw   Skill is valid!
tom-project-xflow Skill is valid!
setup-tom-autodev Skill is valid!
```

Deterministic scans:

- Forbidden runtime import/execute/register/entrypoint scan for Codex or standalone autorelease terms: **0 matches**.
- Local-execution scan found only explicit prohibitions and remote-only statements; no local build/test command was run.
- No iCafe, KU, Infoflow, iCode, or iPipe write was performed.

## SHA-256

Hashes were calculated with `shasum -a 256` after edits:

```text
878855ba971ffd8a01237921c029471a5b07ce4b4728cebe81a59d74ad7294ea  tom-autodev/SKILL.md
5b92b0b49cc8900824b8074b34575542216b2b7a286465fc5f1f89d2f2627d80  tom-grill/SKILL.md
6c48490e59b16623008d4f35f368d134e2bdd262e4507eb1ce469cbdb8290268  tom-spec/SKILL.md
cbbe485febdd1c682868093432459ae6b6c8e7c805ab4184f732fbc8bd0c0b74  tom-tasks/SKILL.md
c780206afada18a112d0f95acc54afccf6c42c5cacd0ace39f7c5fe11e25872b  tom-plan/SKILL.md
b239b8fa4c46f76c7721372a5dba066c8c33292990f33a70c947bf2b1474dfc3  tom-implement/SKILL.md
cb5772f7016a7320946cdb3823276c23fa998b933fe395ec665fb1e796f2a26d  tom-review/SKILL.md
6bea94a1496001dad572e1a65a83c66ebf22e9d0b2ec84f24b4755978e609d16  tom-diagnose/SKILL.md
83a8aeb6a1ab26d706132b19f38c37a5eebcc60350ec24442bdb6473265ad6e4  tom-lang-c-cpp/SKILL.md
a1482b62ae4fb28803b749a3f89fc962f50a3ccbac7556984aedc248bd8f46a1  tom-lang-npl/SKILL.md
cdbbd7b3f2d155f66543a26a3c01ab559201b94c1426f0adcbce34382cfb68d6  tom-project-bgw/SKILL.md
15f9d447e17c6622fc1c62c7613cb5e24f62df419fafd77e25a58ab256b7c0ef  tom-project-xflow/SKILL.md
66f4a7f8745ce2b7f234e117984c44361433873af674a7ca9e747bbdfb6fb3f4  setup-tom-autodev/SKILL.md
b7d514285c9acc1b244d91d05cb83ddaaadc6279c2b9b027adf2a832fc2a01cd  tom-autodev/references/phase-protocol.md
24413b24fc408a022b0d30a04b5719de57e2dfcfec59ef5a79fa71289095c479  tom-grill/references/phase-contract.md
7647cdf453fb16abceaa74e5683dbd070c22785d3e05f23b6e3dde1b6200fbc9  tom-spec/references/phase-contract.md
3e2866881c12ac0b0e590a44ae88aad25bdb85fc661333f31163a2ad4351d5fe  tom-tasks/references/phase-contract.md
3e1b00916900202e209d899a5d9bcfbc0d6cad9518ea3495f29b91179b2d9e9f  tom-plan/references/phase-contract.md
108a33a1dbd759b69916e108dcb3d56a5def57b54c76b4ce4be2e02a97a0a616  tom-implement/references/phase-contract.md
ca3f7a9d073c7e2a0103250ec133dfc0692d4688c93c39fd60519b136812e971  tom-review/references/phase-contract.md
2360c307627cd8dc7b30c243902fee34094fc17f193087c2caa32cf88ae7b86a  tom-diagnose/references/phase-contract.md
07adc2fdcd845c87af761eefe8199e8cbe43ae7024faeab69e7c0ff4999a9cce  tom-lang-c-cpp/references/testing-and-review.md
997bf36c0fc75c69c8d97007f65c217524f17bbb526da0ca15e59967a5147940  tom-lang-npl/references/npl-core-rules.md
2cedecbb7bae5e416ba258f06cf3e08f5f96b599878b7b392e60a6f7ff270a73  tom-lang-npl/references/error-patterns.md
```

## Concerns

- Existing historical source-material notes outside the modified trigger/workflow text were not removed.
- Live profile values, team rosters, and iPipe runner health remain setup/runtime concerns and were not guessed or contacted.
- The phase contract references the existing runtime schemas and adapters; schema evolution must update both the runtime validator and these references in one approved change.

## Task 9 Fix Round 1

### Status

COMPLETE. Addressed independent review findings I1-I3 without changing runtime code or weakening Comate-only and iPipe-only boundaries.

### Repairs

- I1: Extended `tom-autodev/references/phase-protocol.md` with parent-owned `WORKSPACE`, `SUBMIT`, `IPIPE`, `RELEASE`, `RELEASE_SUCCESS`, and `STOPPED` contracts. Each lists required inputs, output/receipt fields, completion predicate, G4/G7/G8/G9/G10 ownership and hash binding, and stop conditions. G10 is explicitly post-run optimization only and restricted to control-plane roots.
- I2: Replaced NPL language-only failure rows with canonical parent classes and explicit subtypes. Chip/compiler resource limits with a healthy runner map to `CODE_FAILURE`; missing runner/device/service/capacity maps to `ENV_UNSATISFIED`; transient health maps to `ENV_TRANSIENT`; pipeline interruption maps to `PIPELINE_TRANSIENT`; identity drift maps to `REVISION_MISMATCH`.
- I3: Made Review non-approval-bearing in the parent Skill, protocol, and phase contract. Only dual-axis `PASS` permits the parent to request separately hash-bound G7 iCode approval. Added explicit stops/routes for `INCOMPLETE`, `NEEDS_CLARIFICATION`, stale/hash-mismatched baselines, missing inputs, and blocking findings.

### Validation Commands and Exact Outputs

Command:

```text
for d in tom-autodev tom-grill tom-spec tom-tasks tom-plan tom-implement tom-review tom-diagnose tom-lang-c-cpp tom-lang-npl tom-project-bgw tom-project-xflow setup-tom-autodev; do python3 /Users/tom/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/tom/Desktop/skills/$d; done
```

Exact result for every directory: `Skill is valid!` (14/14).

```text
link_scan_exit=0
forbidden_runtime_scan: 0 matches
local_execution_scan: 10 explicit prohibition/remote-only lines; no local project command executed
```

No iCafe, KU, Infoflow, iCode, or iPipe write was performed. No BGW/XFlow build, test, regression, integration, Docker, NCS, simulator, or release command was run locally.

### Round 1 SHA-256

```text
b752cae961c004e2ec7275df63cd6658c8cc86b359f827a9560b3d6f0b8535fe  tom-autodev/SKILL.md
f353f72f467351afcb470f01cfd4617bfc48e13c0e897d500d6668d5af1116c0  tom-autodev/references/phase-protocol.md
643fce587a79d21f66b5675577adc385d88001d3fea815c77fe7b5a702daef94  tom-review/SKILL.md
82455684219f83d4c5ecce205237641b28226307fd95a6a33266aaaa61dfb4a8  tom-review/references/phase-contract.md
f4014ed4f786e5b9996e05b087cf37bcbbed47b52c54b5eeda4d42bcbe1c674a  tom-lang-npl/references/error-patterns.md
```

### Remaining Concerns

None from I1-I3. Live profiles, approvals, rosters, and remote runner health remain intentionally unverified because validation is fake/read-only only.

## Task 9 Fix Round 2

### Status

COMPLETE. Addressed the remaining I1 and round-1 N1 in `tom-autodev/references/phase-protocol.md` only.

### Repairs

- N1: `WORKSPACE` is now explicitly a fail-closed evidence prerequisite with **no new approval**. G3 remains the Task DAG/frontier approval; G4 is exclusively parent approval of the completed, exact `Task Plan` hash.
- I1/G8: Added an explicit parent controller-action contract: frozen failed-stage inputs, canonical Comate/Infoflow G8 hash binding, durable intent/receipt identity, same-identity evidence completion, idempotent replay, and stop conditions for unknown results, missing receipts, timeout/rejection, drift, unauthorized parameters, and unbounded retry.
- I1/G10: Added an explicit parent post-run contract: archived RunSummary/proposal inputs, candidate-hash approval, KnowledgeSync/iCafe proposal/result receipts, and `APPLIED`, `ROLLED_BACK`, `ARCHIVE_PENDING`, and `RECOVERY_REQUIRED` semantics. It names forbidden targets and approval/validation/archive stops.

### Validation Commands and Exact Outputs

```text
for d in tom-autodev tom-grill tom-spec tom-tasks tom-plan tom-implement tom-review tom-diagnose tom-lang-c-cpp tom-lang-npl tom-project-bgw tom-project-xflow setup-tom-autodev; do python3 /Users/tom/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/tom/Desktop/skills/$d; done
```

Each of the 14 directories printed exactly `Skill is valid!`.

```text
link_scan_exit=0
forbidden_runtime_matches=0
local_execution_matches=10
```

The 10 local-execution matches are explicit Mac/iPipe prohibition statements. No local project command and no iCafe, KU, Infoflow, iCode, or iPipe write was performed.

### Round 2 SHA-256

```text
b752cae961c004e2ec7275df63cd6658c8cc86b359f827a9560b3d6f0b8535fe  tom-autodev/SKILL.md
5956d5fdacf7257358f20da58bb0b8e75fad22ecf9fc0cfa557db9b5d0533aa1  tom-autodev/references/phase-protocol.md
```

### Remaining Concerns

None from I1/N1. Real approval and remote execution remain intentionally outside documentation validation.
