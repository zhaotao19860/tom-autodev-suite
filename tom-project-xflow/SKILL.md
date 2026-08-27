---
name: tom-project-xflow
description: Use when tom-autodev targets XFlow or TD5 NPL and needs repository topology, chip constraints, knowledge sources, test-repository, environment, Review, or iPipe rules.
---

# Tom Project XFlow

## Profile

Treat `/Users/tom/icode/td5` as the candidate workspace and `xflow-npl/` as current NPL source only after setup confirms repositories, iCode modules, target branches, locks, and revisions. Require an independent XFlow product-test repository, source-only Review provider, stable iPipe NCS pipeline, environment profile, approval channels, and release rule. Missing values return `PROJECT_NOT_READY`.

## Knowledge Priority

1. `xflow-npl/` current implementation at the recorded revision; use precise text search for NPL.
2. `tom-lang-npl` rules and `references/chip-constraints.md`.
3. `CNA_6_5_32_3_0/` official examples as read-only evidence.
4. `knowledge/docs/` approved official Markdown.
5. `knowledge/error-solutions/` approved historical failures.
6. `docs/` PDFs only when Markdown provenance or meaning is uncertain.

Use GitNexus for parseable C/C++, scripts, RPC and impact relationships; do not treat incomplete NPL graph coverage as source truth. Record repository, revision, path/symbol, source type, and freshness for every reference.

## Required XFlow Test, Environment, and Graph Profile

The profile must explicitly name the NPL business repository and independent product-test repository, branch/revision locks, external packet/API boundary, fixture generator and expected-result source, allowed iPipe stages, NCS/compiler/simulator versions, chip target, and capacity limits. Never infer a test repository, NCS command, simulator, chip limit, or environment from a checkout name. Incomplete NPL graph coverage requires precise source search and a cited KU/document revision; GitNexus remains supporting impact evidence only. Record graph query/index freshness, source path, KU URL/version, fixture/test-plan hashes, and environment fingerprint in each plan and remote result.

XFlow tests must cover valid packets, width/length boundaries, malformed/truncated headers, invalid/missing fields, unmatched/drop/trap/mirror behavior, compatibility, and the closest regression. Compile, NCS, simulator, unit, regression, integration, Docker, packaging, and release commands run only in the pinned iPipe runner; Mac generates/Reviews source and parses remote evidence.

## Verification Contract

Map each task to XFlow business revisions, independent test revision, test plan hash, environment fingerprint, iPipe NCS/compiler/simulator/regression/integration evidence, and release version. Existing `docker_build.sh`, `build_and_capture.sh`, NCS, compiler, and simulator entrypoints may run only inside the configured iPipe runner. Mac may parse their remote logs but never execute them.

Do not modify CNA or other read-only references. Do not write new error knowledge automatically; create a candidate and require human approval.

**REQUIRED PARENT:** Load `tom-lang-npl`, then return topology, constraints, knowledge, environment, and release requirements to `tom-autodev`.
