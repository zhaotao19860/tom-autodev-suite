# Evidence and False-Positive Checks

Use the check for the candidate's defect type. Source-to-sink tracing is for injection and similar input-driven exploits; it is not a prerequisite for every security or correctness defect.

## Evidence by defect type

| Type | Evidence required for `CONFIRMED` |
|---|---|
| Injection, traversal, SSRF, unsafe deserialization | Actual controllable source; dangerous operation; reachable source-to-operation path; why applicable guards do not stop it; concrete input and observable effect |
| Authorization defect | Actor and granted permissions; protected resource/operation; reachable missing/bypassed check; concrete unauthorized result |
| Secret/log exposure, cryptography, security configuration | Actual sensitive value or security purpose; applicable trust boundary/contract; exposure or failing property and its concrete consequence. Attacker-controlled input is not required |
| Ordinary logic, error handling, compatibility, recovery, performance | Location/contract boundary; specific input/configuration/timing; observable consequence; expected behavior supported by Spec or an applicable project rule |
| Required behavior absent | AC/Spec location; responsible module/interface; complete inspected scope; demonstrated missing behavior. Follow [`spec-coverage.md`](spec-coverage.md); no fictitious source line |

Record revision and actual evidence references. The path may be one direct call or many; an arbitrary minimum hop count must not suppress a direct vulnerability. Check the proposed repair against the same counterexample and relevant compatibility constraints.

## Disposition

- Confirmed counterexample that survives self-refutation: `CONFIRMED`; assign impact using [`severity-taxonomy.md`](severity-taxonomy.md).
- Evidence positively disproves the suggestion, shows it is outside this task, or establishes only an optional preference: `REJECTED_WITH_REASON`, `blocking: false`, with the precise reason. Do not claim safety merely because evidence is missing.
- Missing fact prevents judging required behavior or a material risk: `NEEDS_CLARIFICATION`, `blocking: false`; give the question/evidence needed. This classification stops the parent even with a false blocking flag.
- Required files/Spec/provider scope could not be inspected: Review `INCOMPLETE`; do not report missing implementations from an incomplete search.
- Speculative patterns without an established defect or a material review gap need not become new findings. Every suggestion supplied by a review provider still receives an explicit disposition.

Never reduce severity solely to make uncertain evidence look confirmed. Do not fabricate a proof-of-exploit or a reproduction result; source-only reasoning describes a counterexample, not an executed test.

## Context checks

1. **Reachability:** identify a real entry and allowed configuration. Public methods are not the only entries: callbacks, scheduled jobs, exported library APIs, packet handlers, and product tests can matter. A disabled-by-default feature is not unreachable if the supported configuration enables it.
2. **Existing protection:** verify the actual parameter binding, escaping, validation, authorization, ownership, or compensation on this path; a similarly named utility elsewhere is insufficient.
3. **Contract and intent:** check approved behavior before judging deletion, fallback, consistency, or timing. Lack of intent evidence is not proof of intentional safety.
4. **Baseline:** distinguish a change-induced defect from an unrelated existing issue. Do not infer a missing implementation just because it is absent from added lines.
5. **Secret handling:** quote only redacted values; retain enough location/type evidence for repair. Changing the log level does not remove a credential leak.

## Common patterns that need context

| Pattern | Correct interpretation |
|---|---|
| SQL words in logs, URLs, messages, or tests | Not a SQL operation by themselves; find the actual executable query |
| `#{x}`, `?`, `:x`, `${x}`, `@{x}` | Resolve the actual framework/API binding semantics. In MyBatis, `#{}` binds and `${}` substitutes; these spellings do not establish safety across all languages/APIs |
| Framework configuration `${ENV}` | Usually property expansion, not SQL; determine where its resolved value is used |
| Constant/enum/config value passed to an operation | May disprove attacker control if its provenance is verified; it does not exempt secret exposure or logic defects |
| MD5/SHA1, ordinary randomness | Judge the purpose: a non-adversarial checksum or sampling differs from password storage, token generation, or integrity against an attacker |
| Escaping or validation exists | Confirm it is appropriate for this sink and cannot be bypassed; context-specific escaping is not interchangeable |
| Test/template/generated file | Path alone grants no exemption. Product-test correctness, shipped templates, generated runtime code, and committed secrets remain relevant |
| Removed tenant filter or consistency update | Trace the authorization/replication design; neither removal alone nor “perhaps intentional” proves the outcome |

Deduplicate candidates by defect and affected behavior. Keep the strongest evidence and related locations instead of reporting the same secret as separate secret, logging, and configuration defects.
