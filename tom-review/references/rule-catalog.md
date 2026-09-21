# Rule Catalog

A catalog of rule-ID heuristics folded from the standalone QA quality gates. tom-review **executes nothing** — every rule here is a thing the reviewer checks by **reading** the FIXED-baseline diff. There are no scripts. SQL-injection, ReDoS, arch-layer, hardcoded-secret, and sensitive-logging rules are read-checks, not scans.

Map severity to disposition via [`severity-taxonomy.md`](severity-taxonomy.md): a HIGH rule with a complete flow → `blocking: true` + `CONFIRMED`; a MEDIUM rule → non-blocking, and `NEEDS_CLARIFICATION` when context is missing. Gate every security rule through [`false-positive-suppression.md`](false-positive-suppression.md) §1 first. Most axis assignments are Standards (conventions); behavior-correctness cases feed Spec.

Report IDs may carry a sub-rule suffix (e.g. `G-SECRET-002-TOKEN`, `G-SEC-001-...`). Match on the main ID prefix; a suffix does not change the main rule.

## Sensitive information (G-SECRET) — Standards

| ID | Severity | What to look for |
|----|----------|------------------|
| G-SECRET-001 | HIGH | Hardcoded password / token / API key / AK/SK — literal in an assignment, constant, `@Value` default, Bearer string, or a connection string with an embedded password |
| G-SECRET-002 | HIGH (PII sub-rules MEDIUM) | Logging PII or signing material — password/pwd, token/jwt/authorization, secretKey/apiKey/privateKey/AK/SK, signature material (HIGH); phone / ID / bank card / email (MEDIUM, needs masking) |
| G-SECRET-004 | HIGH | Sensitive value in a config file not sourced from an env var / secret manager (config takes bare values; code requires a quoted literal, so `secret: ${ENV}` is not a hit) |

## Exception handling (G-EXCEPT) — Standards

| ID | Severity | What to look for |
|----|----------|------------------|
| G-EXCEPT-001 | MEDIUM | Empty catch block (exception silently swallowed) |
| G-EXCEPT-002 | MEDIUM | Catch a broad exception then only log, no handling |
| G-EXCEPT-004 | MEDIUM | Throw a new exception in `finally`, masking the original |
| G-EXCEPT-005 | MEDIUM | Rethrow without the cause and without recording the original stack (exception chain lost) |

## Input validation (G-INPUT) — Standards / Spec

| ID | Severity | What to look for |
|----|----------|------------------|
| G-INPUT-001 | MEDIUM | Public API parameter with no null check |
| G-INPUT-002 | MEDIUM | Numeric parameter with no range check |
| G-INPUT-004 | MEDIUM | External data (request body) not validated |

## Security (G-SEC) — Standards

| ID | Severity | What to look for |
|----|----------|------------------|
| G-SEC-001 | HIGH | DB access via string concatenation instead of a parameterized query (SQL injection — confirm attacker-controllable source per FP §4) |
| G-SEC-002 | MEDIUM | URL built with plaintext HTTP instead of HTTPS (internal domains exempt) |

## Database (G-DB) — Spec / Standards

| ID | Severity | What to look for |
|----|----------|------------------|
| G-DB-001 | HIGH | UPDATE/DELETE with no WHERE — unconditional full-table write/delete (migration paths exempt) |

## Performance (G-PERF) — Standards

| ID | Severity | What to look for |
|----|----------|------------------|
| G-PERF-001 | MEDIUM (HIGH on external input) | Regex with nested/overlapping quantifiers — catastrophic backtracking (ReDoS); escalate when it matches external input |
| G-PERF-002 | MEDIUM | SQL predicate column wrapped in a function, or a leading-wildcard LIKE, defeating the index |
| G-PERF-003 | MEDIUM | ORM EAGER association loading where LAZY + batched fetch is correct |

## Logging (G-LOG) — Standards

| ID | Severity | What to look for |
|----|----------|------------------|
| G-LOG-002 | MEDIUM | Wrong log level — sensitive debug material (signature/secret/credential) logged at info; should be debug |

## Architecture (G-ARCH) — Standards

| ID | Severity | What to look for |
|----|----------|------------------|
| G-ARCH-001 | MEDIUM | Inner package (domain/core) imports an outer/infrastructure concrete implementation — dependency-direction violation |

## Business-specific (BIZ-*) — Standards / Spec

Project-defined rules the reviewer applies by reading, matched to the project's own conventions rather than a config file.

| ID pattern | Severity | What to look for |
|------------|----------|------------------|
| BIZ-001 (example) | HIGH | Third-party service called directly (e.g. raw HTTP client) instead of through the project's unified client wrapper (loses shared timeout/retry/circuit-breaker) |
| BIZ-RETRY-001 (example) | HIGH | A third-party network call (e.g. TTS/ASR) with no retry/backoff — a jitter directly fails the user request |
| BIZ-FILE-001 (example) | HIGH | Local IDE / AI-assistant / OS metadata committed (`CLAUDE.md`, `.claude/`, `.DS_Store`, `.idea/`) — should be gitignored |

BIZ rules are open-ended: read the project's conventions and flag deviations by the same ID + severity + read-check shape.
