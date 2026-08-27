# Tasks Phase Contract

Input is a G2-approved `spec` envelope and its traceability. Output is a `task-dag` envelope: each node is one independently verifiable end-to-end behavior and contains business/test repositories, external test IDs and fixtures, expected iPipe evidence, dependencies, rollback, and completion predicate. Validate acyclicity and full acceptance coverage, persist to KU, and comment iCafe through the parent. G3 approval is required; stop on uncovered behavior, repository-only decomposition, cycle, or hash drift. Do not generate code or invoke iPipe.
