# Grill Phase Contract

Input is an immutable Comate `RequirementSnapshot` plus project/language evidence and G0 approval. Output is a `decision-log` `ArtifactEnvelope` whose content is `CLARIFIED` or `NO_OPEN_DECISIONS`, with decision maker, options, decision, evidence refs, glossary delta, and optional ADR candidate. Persist the artifact in KU and add an iCafe comment through the parent controller. Approval is G1; stop on `DECISION_REQUIRED`, `REQUIREMENT_CHANGED`, missing snapshot, or hash mismatch. Do not create tasks, code, tests, or iPipe actions.
