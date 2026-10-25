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

## Runtime Topology and Manual Stages

Read [`references/runtime-topology.md`](references/runtime-topology.md) before diagnosing or continuing a BGW iPipe product-case stage. It names the live client/BGW/RS topology, associated logs, version and counter inspection, and the `P0新case回归` parameter map (`get_bgw_test_case`, `get_bgwagent`). That stage updates client `bgw_auto` and `tool/bgwagent` only; the server BGW binary is replaced by earlier compile/regression stages.

Empty `manualParams` does not mean no job inputs. `get_bgwagent` can inherit an upstream
static default. Resolve it from the run's frozen changed status: unchanged uses target
branch BranchPipeline successful release; changed uses owned exact-candidate ChangePipeline
compile, with verifiable revision/branch and download. Require a proven job override
contract without changing the external template; unsupported override or compile-only
trigger blocks explicitly. Never report `params={}` as dynamic resolution or a fixed old
node as this run's product.

## Verification Contract

The BGW project profile must map each task to business revisions, product-test revision,
test plan hash, the stable environment-binding fingerprint, iPipe build/stage evidence,
and release version. The fingerprint binds evidence to the selected profile; it is not
local proof that the runner is available. Compile, unit, regression, integration,
deployment, and release execution occur only in that profile's iPipe runner. The Mac may
generate source/test patches and perform source Review, but never run BGW build or tests.

**REQUIRED PARENT:** Return topology, knowledge, environment, iPipe parameter mapping, and release evidence requirements to `tom-autodev`; do not call iCode/iPipe directly.
