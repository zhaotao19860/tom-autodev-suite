# Rule Catalog

These are source-reading cues internalized from QA review practice, not executed scans. Load only relevant families; do not claim a script or analyzer ran. Apply [`false-positive-suppression.md`](false-positive-suppression.md), then [`severity-taxonomy.md`](severity-taxonomy.md). Priority below is a starting point conditioned on a demonstrated consequence.

| Rule | Typical priority | Check and exclusion |
|---|---|---|
| `G-SECRET-001/004` | `P1` | Actual credentials/private keys committed in source or config and exposed across a trust boundary. Distinguish documented dummy fixtures from real secrets; redact evidence. Environment expansion alone does not prove the resolved value is handled safely |
| `G-SECRET-002` / `G-LOG-002` | `P1` credentials; `P2` bounded personal-data exposure | Logs/errors reveal credentials, signing material, or protected personal data to unauthorized readers. Remove or mask sensitive data; lowering to debug is insufficient. Report one defect, not one per matching rule |
| `G-EXCEPT-001/002/004/005` | `P2`, escalate for data loss/recovery failure | A catch/finally loses the required failure signal or original cause, masks failure as success, or breaks cleanup. Intentional handling, a documented best-effort boundary, or a preserved causal chain can refute the candidate |
| `G-INPUT-001/002/004` | `P2`, escalate for memory safety/security | A reachable invalid value produces wrong behavior or violates the boundary contract. Do not require repeated checks when type guarantees or upstream validation already establish the invariant |
| `G-SEC-001` | `P1` | Controllable data changes executable SQL structure despite applicable binding/validation; string concatenation alone is not proof |
| `G-SEC-002` | `P2`, escalate for credential compromise | Sensitive traffic crosses an untrusted link without required protection. Judge the trust boundary; an internal hostname alone is not an exemption |
| `G-DB-001` | `P1` | An unintended unbounded UPDATE/DELETE causes material data loss. A documented full-table operation or reviewed migration is not automatically defective |
| `G-PERF-001` | `P2` or `P1` denial of service | A feasible input/length triggers pathological regex cost on a relevant path. Inspect engine and input bounds; nested quantifiers alone do not establish exploitability |
| `G-PERF-002/003` | `P2` | Query shape, loading strategy, or repeated remote work violates a supported workload/latency constraint. Require scale/index/call-path evidence; no blanket rule that LAZY is always better |
| `G-ARCH-001` | `P2` | A concrete dependency violates an approved layering policy or causes a demonstrated compatibility/maintenance defect. Package names or direct client construction alone are insufficient |
| `BIZ-*` | Per project contract | Cite the actual pinned project rule and consequence. Examples from another project do not establish a new mandatory rule |

For a project wrapper bypass, establish which required timeout/authentication/retry/telemetry policy is lost. For retry advice, prove the operation is safely retryable and use bounded retry semantics. IDE/OS metadata and naming preferences are normally cleanup observations, not release blockers; a file containing secrets or active configuration is judged by its actual effect.

Language-specific undefined behavior, lifetime, packet parsing, and concurrency belong to the selected `tom-lang-*` reference. Review provider findings and any available static-analysis evidence are inputs to classify, not automatic verdicts.
