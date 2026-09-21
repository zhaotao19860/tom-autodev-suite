# Root-Cause Protocol

Use existing source and immutable runtime evidence first. The method is local to this skill; it does not invoke a separate debugging skill.

## Evidence before hypothesis

Capture one failure occurrence: expected behavior/acceptance point, actual behavior, first failing timestamp, operation/build/stage/job, business and test revisions, artifact identity, environment/toolchain, and complete relevant log window. A tail can locate a symptom but cannot establish that earlier stages succeeded. Redact credentials and nonessential personal data.

Compare the same input/fixture and observation point with the latest *comparable* passing occurrence. List differing source, configuration, dependencies, toolchain, environment and topology. If no comparable baseline exists, say so; a differently configured success is not a counterexample.

For a chain such as client → gateway → service or nlc → xfc → packaging, write a short boundary table:

| Boundary | Expected input/output | Observed input/output + evidence | First divergence? |
|---|---|---|---|
| Selected component | From Spec/protocol | Exact log/request/trace reference | yes / no / unknown |

Follow a bad value backward to its producing caller/configuration, not only the site that finally rejects it. Then trace forward to demonstrate the reported consequence. Correlate multi-host evidence by role, request/packet identity and clock skew; packet capture order alone does not prove causal order.

## One discriminating hypothesis

State: “Under condition C, mechanism M produces symptom S; evidence E supports it; observation F would refute it.” Pick the next observation that separates this hypothesis from a plausible alternative. Change one variable per approved experiment. Record the result before proposing another experiment; if disproved, keep the negative evidence instead of repeating the attempt.

- Source reasoning may confirm a deterministic defect, but does not prove it reproduced in the runner.
- An exit code of zero or a created binary cannot override an earlier compiler/backend error or mismatched artifact revision.
- For timing/performance failures, record load, duration, percentile/counter definition and comparable environment; do not infer a leak or race from one snapshot.
- Environment or transport failure leads to an environment/collection decision. Only evidence linking the product/test change to the defect supports a code/test repair.

## Handoff that another run can reuse

Return the observed failure identity separately from the root-cause hypothesis; include trigger conditions, exclusions, supporting and refuting evidence, minimal repair direction, regression case and the verification needed to close it. A historical case is a candidate until its version/environment/trigger applies. A matching job name is not proof of the same root cause; one run passing does not prove all occurrences fixed.

If repeated repairs show no progress, return the existing failure and repair history to the controller's budget decision. Do not autonomously reset attempt counts or persist new knowledge as an approved fact.

## Sources

Adapted locally from tom-autodebug 1.5.0's role/time/request evidence discipline and the installed systematic-debugging method's backward tracing, working-baseline comparison and single-hypothesis testing. The external entrypoints, code-edit loop, automatic local tests and remote installation suggestions are not dependencies of this protocol.
