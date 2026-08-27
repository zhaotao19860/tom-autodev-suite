# TD5 Chip Constraints

Before choosing an NPL implementation, verify against current TD5 project evidence:

- parser stage and legal transition;
- table type, key/result width, capacity, lookup/update behavior, and resource budget;
- logical bus direction, field width, validity, producer, and consumer;
- header offset/length and malformed/truncated behavior;
- special-function, mirror, tunnel, ACL, L2/L3, meter, counter, trace, and drop support at the selected stage;
- simulator/hardware requirement and environment profile.

Do not encode a limit from memory. Cite the current source or approved document revision. A compiler resource diagnostic is evidence only when its business/test revisions and environment fingerprint match the current task.
