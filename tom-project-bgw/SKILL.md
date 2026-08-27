---
name: tom-project-bgw
description: Use when tom-autodev targets the BGW C/C++ project and needs repository topology, knowledge sources, test-repository, environment, Review, or iPipe rules.
---

# Tom Project BGW

## Profile

Treat `/Users/tom/icode/bgw` as the candidate BGW workspace only after setup confirms the actual business repositories, iCode modules, target branches, locks, and current revisions. BGW tasks are multi-repository but each task remains one end-to-end externally observable capability slice.

Require an independent BGW product-test repository. Require a configured source-only Review provider, stable iPipe pipeline template, environment profile, approval channels, and release rule. Missing or unreadable values return `PROJECT_NOT_READY`; never infer a test repository or pipeline from a directory name.

## Knowledge Priority

1. Current BGW business repositories at the recorded revision and repository rules.
2. GitNexus graphs for each participating repository and cross-repository impact.
3. BGW test precedents, environment rules, and approved historical errors.
4. External/reference repositories only as cited evidence; they never override current code.

Record repository, revision, path/symbol, source type, and index freshness for every reference. Re-index or stop impact analysis when the graph is older than the target revision.

## Required BGW Test and Knowledge Profile

The profile must explicitly name each business repository/module, independent product-test repository, branch/revision policy, external test interface, fixture/data source, and the iPipe build/unit/regression/integration stages. Never infer a test repository, executable, compiler flag, environment, or release target from `/Users/tom/icode/bgw` or a directory name. GitNexus queries are supporting impact evidence only; source and approved KU documents remain authoritative. Record graph query, index revision/freshness, cited KU URL/version, and fixture/test-plan hashes in every Task Plan and remote result.

BGW tests must assert the stable externally observable gateway/API/protocol boundary and include valid, boundary, malformed, compatibility, and regression cases. Environment readiness must include runner OS/architecture, image/toolchain digests, services/data/capacity, and reproducible cleanup. All build, unit, regression, integration, packaging, and release commands run only in the pinned iPipe runner; Mac performs source generation/Review and evidence parsing.

## Verification Contract

The BGW project profile must map each task to business revisions, product-test revision, test plan hash, environment fingerprint, iPipe build/stage evidence, and release version. Compile, unit, regression, integration, deployment, and release execution occur only in that profile's iPipe runner. The Mac may generate source/test patches and perform source Review, but never run BGW build or tests.

**REQUIRED PARENT:** Return topology, knowledge, environment, and release evidence requirements to `tom-autodev`; do not call iCode/iPipe directly.
