# Phase Artifacts

| Artifact | Producer | Required fields |
|---|---|---|
| Decision Result | `tom-grill` | result, decision log, acceptance delta when the card had none, glossary delta, evidence refs, input hash |
| Spec | `tom-spec` | version, content hash, behaviors, test interface, environment requirements, traceability |
| Task DAG | `tom-tasks` | nodes, edges, acceptance coverage, completion evidence, G3 hash |
| Task Plan | `tom-plan` | exact repositories/files/symbols, interfaces, test cases, iPipe parameters, checklist, G4 hash |
| Change Set | `tom-implement` | business/test patches, complete diff, test IDs, traceability delta, deviations from the plan, G5 hash |
| Review Report | `tom-review` | baseline, Standards axis, Spec axis, findings classified `CONFIRMED` / `REJECTED_WITH_REASON` / `NEEDS_CLARIFICATION` with a reason for the latter two, verdict |
| Diagnosis | `tom-diagnose` | reproduction, comparison, class, one hypothesis, minimal verification, route |

Every producer must return the current `run_id`, `task_id`, `spec_version`, `input_hash`, source artifact references, and canonical content hash. A consumer rejects artifacts whose identity or hash differs from the current state.
