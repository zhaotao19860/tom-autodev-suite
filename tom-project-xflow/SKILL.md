---
name: tom-project-xflow
description: Use when tom-autodev targets XFlow or TD5 NPL and needs repository topology, chip constraints, knowledge sources, test-repository, environment, Review, or iPipe rules.
---

# Tom Project XFlow

## Profile

Treat the workspace and `xflow-npl/` checkout named by the configured project profile as candidates only after setup confirms repositories, iCode modules, target branches, locks, and revisions. Require an independent XFlow product-test repository, source-only Review provider, stable iPipe NCS pipeline, environment profile, approval channels, and release rule. Missing values return `PROJECT_NOT_READY`; never substitute a familiar local path.

## Knowledge Priority

1. `xflow-npl/` current implementation at the recorded revision; use precise text search for NPL.
2. `tom-lang-npl` rules and `references/chip-constraints.md` when the task actually changes NPL.
3. The profile's versioned official examples as read-only evidence.
4. Approved official Markdown from the configured document root.
5. Approved historical failures with their recorded applicability.
6. Original PDFs when Markdown provenance or meaning is uncertain.

Use GitNexus when its index covers the relevant parseable C/C++, scripts, RPC, or impact relationship. If it is unavailable, stale, or incomplete for NPL, use precise `rg`/source search and record the fallback; do not treat incomplete graph coverage as source truth. Record repository, revision, path/symbol, source type, provenance, and freshness for every reference.

## Required XFlow Test, Environment, and Graph Profile

The profile must explicitly name the NPL business repository and independent product-test repository, branch/revision locks, external packet/API boundary, fixture generator and expected-result source, allowed iPipe stages, NCS/compiler/simulator versions, chip target, and capacity limits. Never infer a test repository, NCS command, simulator, chip limit, or environment from a checkout name. Incomplete NPL graph coverage requires precise source search; cite the document/version when a document supplies the relevant fact. Record actual source paths/revisions, fixture/test-plan hashes and environment fingerprint; include graph query/freshness or document URL/version only when used, without inventing missing evidence.

XFlow tests must cover valid packets, width/length boundaries, malformed/truncated headers, invalid/missing fields, unmatched/drop/trap/mirror behavior, compatibility, and the closest regression. Read [simulator evidence](../tom-lang-npl/references/simulator-evidence.md) before relying on NPLSIM; unsupported behavior must move to a supported iPipe/device stage. Compile, NCS, simulator, unit, regression, integration, Docker, packaging, and release commands run only in the pinned iPipe runner; Mac generates/Reviews source and parses remote evidence.

## Verification Contract

Map each task to XFlow business revisions, independent test revision, test plan hash, environment fingerprint, iPipe NCS/compiler/simulator/regression/integration evidence, and release version. Existing `docker_build.sh`, `build_and_capture.sh`, NCS, compiler, and simulator entrypoints may run only inside the configured iPipe runner. Mac may parse their remote logs but never execute them.

Do not modify CNA or other read-only references. Do not write new error knowledge automatically; create a candidate and require human approval.

Load `tom-lang-npl` only when the task changes or reviews NPL source. For LT control-plane changes, read `references/lt-control-plane.md`; for mirror/recirculation behavior, read `references/mirror-evidence.md`; for general provenance and historical cases, read `references/knowledge-sources.md`. Return topology, constraints, knowledge, environment, and release requirements to `tom-autodev`; do not create a reciprocal required-skill loop.
