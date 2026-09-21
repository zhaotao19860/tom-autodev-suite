# Security Checklist

Feeds the **Standards** axis. Apply by reading only — tom-review executes nothing. Core rule: never emit a security finding on pattern match alone; a finding is `CONFIRMED` only when attacker-controllable input reaches the sink. See [`false-positive-suppression.md`](false-positive-suppression.md) for the sink-first firewall that gates every security finding.

## Confidence rule

- Clear vulnerable pattern **and** confirmed attacker-controllable input → `CONFIRMED`.
- Vulnerable pattern but input source uncertain → `NEEDS_CLARIFICATION`, state what source must be confirmed.

When a suspicious pattern appears, trace the data source first. Source is server-side config/constant → suppress (`REJECTED_WITH_REASON`). Source is user input → confirm.

## OWASP Top 10 (read-checks)

- **A01 Broken access control**: IDOR — an ID-keyed lookup that does not verify ownership; horizontal escalation (user A acts on user B's data); vertical escalation (a normal user reaches an admin endpoint).
- **A02 Cryptographic failure**: weak hash for passwords (MD5/SHA1; a file checksum may use MD5); weak config (ECB mode, hardcoded IV, TLS < 1.2, RSA < 2048); insecure randomness for a security purpose (should be a CSPRNG).
- **A03 Injection**: SQL from string concatenation (should be parameterized); command injection (`os.system` / `shell=True` + user input); XSS (`innerHTML` / `dangerouslySetInnerHTML` / `v-html` + un-escaped input); path traversal (user-controlled file path unchecked); SSTI (user input as template body). Read-cues: `raw(`, `execute(`, `+ "SELECT`, `f"SELECT`, `innerHTML`, `os.system`, `shell=True`.
- **A04 Insecure design (footgun)**: is the default value safe when a parameter is omitted? behavior on zero/null? does a security operation fail silently (a failed permission check passing quietly)? are security parameters optional?
- **A05 Security misconfiguration**: fail-open on auth failure (should fail-closed); debug exposure (DEBUG on, management/actuator endpoints, `CORS: *` in prod); unsafe default when an env var is missing (should fail).
- **A06 Supply chain**: known-vulnerable dependency version; lock file committed; dynamic dependency loading (`exec(requests.get(url).text)`, importing a user-named module).
- **A07 Authentication failure**: JWT decoded without signature verification (should verify + pin algorithm); state-changing operation without CSRF protection; session hygiene (regenerate session on login, invalidate old sessions on password reset, sane token expiry).
- **A08 Software/data integrity failure**: deserializing untrusted data (`pickle.loads` / `ObjectInputStream` / `yaml.load` without a safe loader); should use a safe format like JSON.
- **A09 Logging/monitoring failure**: PII (password, phone, ID number) not masked in logs; error responses leaking stack/SQL/service addresses to the client; tokens stored in `localStorage` (XSS-stealable; should be an httpOnly cookie).
- **A10 SSRF**: user-controlled URL requested directly (should allow-list). Read-cues: `requests.get(user_input)`, an HTTP client + a user URL.

## Business-logic security

- **Race condition**: non-atomic check-then-act (e.g. stock oversell); should use a DB-level atomic operation.
- **Idempotency**: does a payment/charge endpoint carry an idempotency key; can a network retry cause a duplicate operation?
- **Bulk operations**: does a bulk delete/export have a count cap and paging/rate-limit?

## Quick checklist

- [ ] All external input validated
- [ ] SQL / command / template parameterized
- [ ] User output escaped
- [ ] Every endpoint has an authorization check (including data ownership)
- [ ] State-changing endpoints have CSRF protection
- [ ] Sensitive operations have an audit log
- [ ] Error responses return only generic messages
- [ ] Tokens/keys stored and transmitted securely
- [ ] Crypto algorithms meet current standards
- [ ] Concurrent paths have no race
- [ ] Debug config off in production
- [ ] Deserialization accepts only trusted formats
- [ ] Security-relevant defaults are fail-closed
