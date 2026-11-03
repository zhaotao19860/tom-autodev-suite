---
name: tom-project-bgw
description: Use when tom-autodev targets the BGW C/C++ project and needs repository topology, knowledge sources, test-repository, environment, Review, iPipe rules, the 25G client/BGW/RS topology, associated logs, version or counter inspection, or P0新case回归 parameters get_bgw_test_case and get_bgwagent.
---

# Tom Project BGW

## Profile

Resolve BGW workspaces from the validated project profile and owned workspace binding. A requirement may span repositories; each runtime task binds one business module and its independent test repository as an externally observable capability slice. An indivisible multi-business-repository task is a runtime capability gap, not permission to guess workspace ownership.

Require an independent BGW product-test repository, a stable iPipe pipeline
template, approval channels, and a release rule. BGW runner selection, compiler,
test topology, fixtures, and services are owned by this skill and the selected
iPipe pipeline; tom-autodev does not require a separately verified local
environment provenance record. Missing or unreadable profile/pipeline values return
`PROJECT_NOT_READY`; never infer a test repository or pipeline from a directory name.

## Knowledge Priority

1. Current BGW business repositories at the recorded revision and repository rules.
2. Approved behavior/protocol documents and current BGW test precedents and environment rules.
3. Versioned historical cases, applied only when their trigger and environment match.
4. External/reference repositories only as cited evidence; they never override current code.

Record repository, revision, path/symbol and source type for each reference. GitNexus is optional supporting impact evidence. When its graph is missing, stale or incomplete, use precise source/caller/configuration search and record the limitation; do not return `PROJECT_NOT_READY` solely because a graph is unavailable. Record query and index freshness only when using a graph.

## Required BGW Test and Knowledge Profile

The profile must explicitly name each business repository/module, independent product-test repository, branch/revision policy, external test interface, fixture/data source, and the iPipe build/unit/regression/integration stages. Never infer a test repository, executable, compiler flag, environment, or release target from a directory name. Record the source paths/revisions, cited document versions, and fixture/test-plan hashes that actually support the Task Plan or remote result; do not invent graph queries or KU receipts.

BGW tests must assert the stable externally observable gateway/API/protocol boundary and include valid, boundary, malformed, compatibility, and regression cases. The iPipe pipeline and this skill define runner OS/architecture, image/toolchain, services/data/capacity, topology, and cleanup. All build, unit, regression, integration, packaging, and release commands run only in the pinned iPipe runner; Mac performs source generation/Review and evidence parsing.

## Team Knowledge

BGW's project facts live in the team workspace `baidu/sysip/bgw-workspace`, reviewed and
merged there; this skill no longer carries them. A run pins the workspace commit at INTAKE
and each producer action lists the files to read (`knowledge_workspace`). For BGW:

- `docs/environment/bgw-test-topology.md`: client/RS and server hosts, binaries, control
  plane, qa branches, and which stage replaces which artifact.
- `docs/environment/ipipe-pipelines.md`: pipeline ids, stage parameters and their product
  sources (`get_bgw_test_case`, `get_bgw`, `get_bgwagent`), release stages, and the test-branch
  submit-test / permit-out conventions.
- `docs/testing/product-case-triage.md`: log order and known misreadings after a product-case
  failure.
- `docs/testing/p0-regression-policy.md`: when `P0新case回归` may be skipped.
- `repos/<repo>/overview.md`: modules, pipelines and cross-repository relations.

When a run has no pinned workspace, read the same files from the workspace's current master
and say so. Rules that apply to every project (reading stage declarations from the live
pipeline configuration) are in the parent's `references/phase-protocol.md`.

## Parent Scheduling Contract

Return knowledge to `tom-autodev`. The parent discovers stage/job declarations and upstream
inputs even with empty manualParams; resolves verified products under the frozen
changed/branch/revision policy; fills the stage parameters; requests the exact G8 only if
runtime override is supported, otherwise blocks (no pipeline template changes; unknown write
results only reconcile the persisted intent); reads the new job log, then the client results,
then the server log; and classifies `ENV_UNSATISFIED` versus product `TEST_FAILURE` before
repair. This skill never calls iPipe, never reruns a stage, and never writes a BGW binary.

## Verification Contract

The BGW project profile must map each task to business revisions, product-test revision,
test plan hash, the stable environment-binding fingerprint, iPipe build/stage evidence,
and release version. The fingerprint binds evidence to the selected profile; it is not
local proof that the runner is available. Compile, unit, regression, integration,
deployment, and release execution occur only in that profile's iPipe runner. The Mac may
generate source/test patches and perform source Review, but never run BGW build or tests.

**REQUIRED PARENT:** Return topology, knowledge, environment, iPipe parameter mapping, and release evidence requirements to `tom-autodev`; do not call iCode/iPipe directly.
