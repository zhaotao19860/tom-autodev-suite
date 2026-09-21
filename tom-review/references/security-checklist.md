# Security Checklist

Feeds the **Standards** axis. Apply by reading only. Never confirm a security finding on pattern match alone. Use [`false-positive-suppression.md`](false-positive-suppression.md) for the appropriate evidence path: input-driven exploits need controllability and reachability; authorization, secret exposure, cryptography, and configuration need their own concrete trust-boundary evidence.

## Confidence rule

- Complete evidence for the defect type and a concrete adverse result → `CONFIRMED`.
- Missing fact prevents a material security judgment → `NEEDS_CLARIFICATION`, state the fact and required evidence. This stops the parent; do not use it for every speculative pattern.

For an input-driven candidate, trace the real source and guards. A verified constant may refute injection but may still expose a secret or encode an unsafe security default. User input alone does not prove an exploitable path.

## OWASP Top 10 (read-checks)

- **A01 Broken access control**: IDOR — an ID-keyed lookup that does not verify ownership; horizontal escalation (user A acts on user B's data); vertical escalation (a normal user reaches an admin endpoint).
- **A02 Cryptographic failure**: weak hash for passwords (MD5/SHA1; a file checksum may use MD5); weak config (ECB mode, hardcoded IV, TLS < 1.2, RSA < 2048); insecure randomness for a security purpose (should be a CSPRNG).
- **A03 Injection**: SQL from string concatenation (should be parameterized); command injection (`os.system` / `shell=True` + user input); XSS (`innerHTML` / `dangerouslySetInnerHTML` / `v-html` + un-escaped input); path traversal (user-controlled file path unchecked); SSTI (user input as template body). Read-cues: `raw(`, `execute(`, `+ "SELECT`, `f"SELECT`, `innerHTML`, `os.system`, `shell=True`.
- **A04 Insecure design (footgun)**: is the default value safe when a parameter is omitted? behavior on zero/null? does a security operation fail silently (a failed permission check passing quietly)? are security parameters optional?
- **A05 Security misconfiguration**: fail-open on auth failure (should fail-closed); debug exposure (DEBUG on, management/actuator endpoints, `CORS: *` in prod); unsafe default when an env var is missing (should fail).
- **A06 Supply chain**: a dependency vulnerability established by available evidence and an affected use; unpinned dependency changes; dynamic dependency loading (`exec(requests.get(url).text)`, importing a user-named module). A lock file is not itself a defect; do not invent current vulnerability intelligence.
- **A07 Authentication failure**: JWT decoded without signature verification (should verify + pin algorithm); state-changing operation without CSRF protection; session hygiene (regenerate session on login, invalidate old sessions on password reset, sane token expiry).
- **A08 Software/data integrity failure**: deserializing untrusted data (`pickle.loads` / `ObjectInputStream` / `yaml.load` without a safe loader); should use a safe format like JSON.
- **A09 Logging/monitoring failure**: protected personal data or secrets exposed in logs/errors; security-sensitive storage accessible across an unintended trust boundary. Confirm the actual readers and threat model; assess browser token storage with the application's authentication/CSRF design.
- **A10 SSRF**: user-controlled URL requested directly (should allow-list). Read-cues: `requests.get(user_input)`, an HTTP client + a user URL.

## Business-logic security

- **Race condition**: non-atomic check-then-act (e.g. stock oversell); should use a DB-level atomic operation.
- **Idempotency**: does a payment/charge endpoint carry an idempotency key; can a network retry cause a duplicate operation?
- **Bulk operations**: does a bulk delete/export have a count cap and paging/rate-limit?

## Applicable boundary checks

Apply only where the interface and authentication/transport design require the property; an absent generic mechanism is not by itself a defect.

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
