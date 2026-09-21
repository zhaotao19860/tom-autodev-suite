# NPL Error Patterns

Every row emits one canonical parent class. The NPL subtype adds detail but never replaces the canonical value used by `tom-diagnose`, repair policy, or owner routing.

| Canonical class | NPL subtype | Evidence and disambiguation | Route |
|---|---|---|---|
| `CODE_FAILURE` | `NPL_SOURCE_SYNTAX_SEMANTIC` | NPL compiler diagnostic tied to the current business source revision, with a valid/pinned runner and toolchain | Freeze evidence, then `tom-diagnose`; repair only after approved G6 |
| `CODE_FAILURE` | `NPL_CHIP_RESOURCE` | iPipe compiler report names a stage, bus, field, table, or chip capacity limit and the environment profile itself is healthy and sufficiently provisioned; this is a design/source limit, not runner capacity | Check current project/chip rules, then diagnose and repair after root cause/G6 |
| `TEST_FAILURE` | `NPL_TEST_CONTRACT` | Product assertion or fixture mismatch while business/test revisions and environment fingerprint match and remote stages are healthy | Compare Spec, expected-result source, fixture, and business behavior through `tom-diagnose` |
| `ENV_UNSATISFIED` | `NPL_RUNNER_CAPACITY` | Runner/image/tool/data/service/hardware capacity is missing or below the pinned environment requirement (for example insufficient memory, device slots, or configured chip capacity before compilation); no valid product result exists | Restore or provision the environment; no business patch |
| `ENV_UNSATISFIED` | `NPL_ENVIRONMENT_CAPABILITY` | Required runner, image, tool, data, service, hardware, or simulator capability is absent from the pinned profile | Restore environment; keep business code unchanged |
| `ENV_TRANSIENT` | `NPL_ENVIRONMENT_TRANSIENT` | A normally eligible runner/service has a temporary health or availability failure and a bounded read-only recheck can establish recovery | Perform at most the parent-defined health rechecks; do not patch code |
| `PIPELINE_TRANSIENT` | `NPL_PIPELINE_TRANSIENT` | Query timeout, orchestration outage, or platform interruption without a verified stage result, while revision/profile identity remains known | Read-only retry; G8 controls any stage rerun/manual continuation |
| `REVISION_MISMATCH` | `NPL_REVISION_MISMATCH` | Pipeline business/test input, fixture/test-plan hash, profile, or evidence identity differs from the approved Change Set | Stop and discard the result; never reuse evidence |

Never convert a log tail into a root cause. Freeze revision, compare a passing revision under the same iPipe conditions, and use one falsifiable hypothesis.

For fix/triage detail once a compile failure is classified (nlc front-end vs xfc back-end stages, the 6 canonical pitfalls, and structural back-end fixes), see `npl-compile-diagnostics.md`.

For every class, retain stage/job IDs, business and test revisions, profile/environment fingerprints, fixture/test-plan hashes, and the complete evidence reference. Execution is iPipe-only: Mac must not invoke NPL compiler, NCS, simulator, Docker, unit, regression, or integration commands.
