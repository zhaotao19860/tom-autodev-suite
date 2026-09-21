# XFlow mirror and recirculation evidence

Use this reference only when a task changes mirror, encapsulation, recirculation, or collector behavior. It keeps device observations in the project profile instead of the language skill.

## Trace the complete path

Record the source and revision for each hop: control-plane session/profile and selector → NPL table/bus/function → ingress/egress special function → recirculation or sampler path → editor/encapsulation → output port and capture boundary. A field that is set in a control-plane table but never consumed by the current NPL path is not evidence of a working feature.

For each assertion retain the configured profile/session identity, packet direction/pass, source and collector ports, chip target, environment fingerprint, input/output capture, and expected copy/drop/truncate behavior. Validate both an enabled and disabled/default configuration so an inactive profile cannot be mistaken for a compiler failure.

Separate evidence levels:

- **Confirmed:** current source, generated metadata, and a matching remote/device result agree.
- **Observed:** a device or simulator result is recorded with complete identity, but the mechanism or cross-version portability is not established.
- **Historical/unresolved:** a previous experiment, rejected hypothesis, or incomplete capture. It may guide the next falsifiable check but must not become a universal rule or automatic repair.

For malformed or duplicate-looking captures, preserve raw packet bytes and the exact capture filter/metadata before changing NPL. Do not infer a residual-header, truncation, or duplicate-mirror cause from a summary decoder line alone. Keep packet behavior at the external capture/API boundary and route confirmed source causes through the normal Spec/Task Plan/review gates.

Historical prompts for targeted checks (not current guarantees):

- Recirculation success depended on the source/recirculation port's packet-processor relationship, while a collector could be elsewhere. Verify current port/PP mapping and profile metadata; do not import a hard-coded device-port table.
- A recirculation path produced bounded-length copies even when a separate sampler truncation profile changed. Distinguish the selected paths before inferring which length control applies; the observation does not prove a universal immutable hardware ceiling.
- Malformed short captures had several rejected explanations but no resolved root cause. Require the paired raw captures and current session/profile identity; a historical failed attempt only excludes that hypothesis under its recorded conditions.
