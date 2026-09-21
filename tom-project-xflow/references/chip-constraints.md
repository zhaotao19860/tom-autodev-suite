# TD5 Chip Constraints

Before choosing an NPL implementation, verify against current TD5 project evidence:

- parser stage and legal transition;
- table type, key/result width, capacity, lookup/update behavior, and resource budget;
- logical bus direction, field width, validity, producer, and consumer;
- header offset/length and malformed/truncated behavior;
- special-function, mirror, tunnel, ACL, L2/L3, meter, counter, trace, and drop support at the selected stage;
- simulator/hardware requirement and environment profile.

Do not encode a limit from memory. Cite the current source or approved document revision. A compiler resource diagnostic is evidence only when its business/test revisions and environment fingerprint match the current task.

For a changed resource record: chip/variant, compiler profile, stage/component, current allocation, requested change, named limit or diagnostic, and source of the limit. Distinguish ASIC design capacity from runner memory/device availability; only the former supports a source-layout repair after current evidence confirms the cause.

For tables and special functions, check the current declaration and generated metadata in addition to the application note. For behavior tests, use the pinned simulator's capability matrix: a model can validate packet logic without proving hardware placement, and unsupported dynamic tables/learning/counters need a supported remote/device stage. Historical NPLSIM exclusions are summarized in the language skill; they do not define every simulator release.

Example: `16b mux exceeded` from the current back-end identifies a placement budget to investigate; a worker out-of-memory event identifies runner capacity. Neither proves that a remembered TCAM entry count is the relevant limit.
