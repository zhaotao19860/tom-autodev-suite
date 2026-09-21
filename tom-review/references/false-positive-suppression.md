# False-Positive Suppression

**Always loaded.** This is tom-review's false-positive firewall. Before emitting any security or defect finding, pass §1. Any known-benign pattern in §2 is suppressed and never written. Discipline: **宁可漏报不要误报** — prefer a missed report over a false one.

Industry consensus (Snyk/Sonar/Datadog/Qodana 2025, LLM4PFA, ZeroFalse): ~30% of default SAST rules are false positives, mostly "pattern match divorced from context." Two lowering paths, both done by reading here:

1. **Sink-first taint flow**: from the dangerous sink, trace backward to the external input (source). If no path reaches attacker-controllable input, suppress.
2. **Path feasibility + context-aware reasoning**: use variable names, annotations, signatures, and the call chain to judge whether the path is reachable at runtime.

## Mapping to tom-review classifications

The FP firewall does not have its own verdict vocabulary — it re-points into the required `classification` field:

- Suppressed (any §1 check fails, or a §2 blacklist match) → `REJECTED_WITH_REASON`, with a technical `disposition_reason` naming the check/pattern that cleared it. Not written as a blocking finding.
- Insufficient evidence to either confirm or clear → `NEEDS_CLARIFICATION`, with the missing evidence in `disposition_reason`.
- Survives §1 and §2 with a complete source→sink flow → `CONFIRMED`.

## §1 Self-check (run before every security/defect finding)

- [ ] Is this location really a dangerous sink? (DB exec, command exec, HTML render, file read/write, deserialization) — if not → suppress.
- [ ] Is the interpolated variable from attacker-controllable external input? (HTTP body/query/header/path, MQ message, third-party API return, file content) — if the source is a constant, enum, server-side config, or internal computation → suppress.
- [ ] Does a framework/middleware already guard it? (parameterized query placeholders, named-parameter queries, template auto-escape, an HTML-escape util) — if yes → suppress.
- [ ] Is the path reachable on the call graph? Entry must be public / controller / MQ listener to count — if only test code or dead code calls it → suppress.
- [ ] Does the literal really carry that vulnerability's semantics? (a string containing "from" is not necessarily SQL; "drop" is not necessarily DDL; "<script>" may be a test fixture) — if not → suppress.
- [ ] Does the proposed fix actually solve it? Run the fixed code in your head — if the fix breaks equivalence/function → re-evaluate; it may not be a bug.

Any check firing "suppress" → do not write the finding; dispose it `REJECTED_WITH_REASON`. When you cannot judge clearly → `NEEDS_CLARIFICATION` and note the reason; prefer a miss over a false positive.

## §2 Known false-positive blacklist (match → skip)

### 2.1 SQL keywords in a non-SQL literal

Error messages, log lines, user hints, URL query names, and test assertions often contain a lone SQL keyword (`from` / `into` / `where` / `select` / `drop`). A literal with only a single such keyword and no complete SQL structure (`SELECT...FROM`, `INSERT INTO`, `UPDATE...SET`, `DELETE FROM`) is non-SQL → suppress.

### 2.2 Placeholder semantics

| Placeholder | Meaning | Safe? |
|-------------|---------|-------|
| `#{xxx}` | parameterized bind | safe |
| `${xxx}` | string substitution | unsafe — only the true injection source; use for table/column/orderBy identifiers |
| `@{xxx}` | non-standard (framework SpEL/param ref) | default safe — do **not** treat like `${}`; confirm framework semantics first |
| `:xxx` | named parameter | safe |
| `?` | positional parameter | safe |

Only `${xxx}` is a SQL-injection source. Confirm the framework before flagging `@{xxx}`.

### 2.3 Other known FPs (suppress)

- A password-hash algorithm used for a **checksum / cache key / etag** — flag only when used for password hashing / signing / token generation.
- Non-crypto randomness used for **UI animation / sampling / load spreading** — flag only for password/token/CSRF/IV.
- A command exec whose argument is a pure constant literal with no interpolation.
- A sink whose downstream template applies escaping.
- An HTTP-client call whose URL is a constant or from config (not SSRF).
- A bcrypt hash whose salt comes from a generated salt (not a "hardcoded salt").

### 2.4 Framework/config `${var}`

`${var}` in `logback*.xml`, `log4j2*.xml`, `application*.yml` / `bootstrap*.yml` / `spring*.xml`, `pom.xml` / `build.xml` is a context/property variable, not SQL → suppress. Treat `${var}` as a SQL source only in a MyBatis mapper (`<mapper namespace>` / `<select|insert|update|delete>` nodes, or a `*Mapper.xml` / `mybatis-config.xml` filename).

### 2.5 Business-model changes are not data leaks

Removing a filter/`where` condition alone does not establish a data leak. Require the triple: (1) **source** — the attacker-controllable parameter the removed condition let through; (2) **sink** — the DAO call that crosses a tenant/permission boundary; (3) **flow** — a concrete counter-example where user A thereby reads user B's data. Missing any of the three → treat as an intentional business change, suppress.

### 2.6 Scaffold / template / generated code

Hardcoded constants, magic numbers, and pinned versions under paths containing `skills/`, `templates/`, `scaffold/`, `archetype/`, `stub/`, or in files marked `@generated` / `// generated` / `# AUTO-GENERATED`, are template artifacts → suppress, unless that file is itself the reviewed change and the constant is a dynamically-computed critical path.

### 2.7 Intentional business design

Access-control / consistency / deletion / fallback / timing findings often have a technically-exploitable flow yet are intentional by design (tenant id auto-injected by middleware; eventual-consistency/compensation; deliberate deletion; deliberate open-API fallback; timing change with no Spec constraint). A complete flow proves technical exploitability, not business intent. When the baseline and Spec cannot rule out "designed this way," dispose `NEEDS_CLARIFICATION` (or `REJECTED_WITH_REASON` if the Spec confirms intent) rather than a blocking finding.

## §3 Reachability

From a suspicious point, ask backward: (1) **entry reachability** — is the caller a controller/listener/scheduled entry, or only test/dead code (suppress)? (2) **parameter controllability** — does the interpolated variable trace to a request/message payload, or to config/constant (suppress)? (3) **filter presence** — does it pass a validator/sanitizer/parameterized-query/escape/allow-list before the sink that cannot be bypassed (suppress)? (4) **feature flag** — is the path behind a disabled flag (suppress, note "re-evaluate if enabled")?

## §4 Evidence completeness before emitting

For a **security** finding, the evidence must carry all five: `source` (attacker-controllable input point + variable name + origin), `sink` (full dangerous call expression + line), `flow` (source→sink path, >=2 hops), `why_not_safe` (why the framework/context did not stop it), `proof_of_exploit` (one concrete counter-example input + effect).

- All five present, each specific → `CONFIRMED` at the rule's severity.
- `source` cannot be filled (not external input) → suppress → `REJECTED_WITH_REASON` (uncontrollable input is not a vulnerability), regardless of the other fields.
- One field missing, or `proof_of_exploit` only "theoretically possible" → `NEEDS_CLARIFICATION` (record which field).
- Two or more missing → suppress, do not emit.

For a **non-security** finding (exception handling, input validation, logic bug), require three: `location`, `trigger_condition` (the scenario that triggers it), `consequence` (a concrete observable effect). Any one missing or filled vaguely → suppress, do not emit.
