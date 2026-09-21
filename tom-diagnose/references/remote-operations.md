# Bounded Remote Investigation

This is tom-diagnose's bundled relay/tmux mode, imported from tom-autodebug 1.5.0. It is optional: workflow diagnosis with sufficient artifacts does not open a remote session. Workflow collection is limited to the parent-authorized target/commands; it never substitutes for iPipe build/test/release evidence.

## Entry and identity

Use the installed skill's base path, not another skill:

```bash
DIAG_SCRIPT="<tom-diagnose-base>/scripts/autodebug.sh"
bash "$DIAG_SCRIPT" defaults show
bash "$DIAG_SCRIPT" preflight
```

The filename, `TOM_AUTODEBUG_*` settings, owner marker and default `~/.tom-autodebug` state directory are retained for compatibility. The implementation lives here and makes no calls to the old skill directory. File checksums/version are in [transport-source.json](transport-source.json).

Targets must be exact `user@host` plus SSH transport/namespace. Reuse explicitly supplied or saved relay/SSH usernames; ask only for missing values before connecting. Never infer them from local whoami or a hostname suffix. `defaults set --relay-user USER --ssh-user USER` stores usernames with mode 0600, not passwords. Missing relay/tmux is a dependency blocker; do not auto-install software or invoke an installation skill.

Only ordinary SSH through relay is supported. BNS/Matrix/container routing and file transfer are not implemented. Do not probe guessed alternatives, forward an arbitrary agent socket, or feed passwords/tokens into commands.

## Single host and multi-host case

```bash
bash "$DIAG_SCRIPT" connect user@host --ttl 1800
bash "$DIAG_SCRIPT" exec user@host --timeout 30 -- "hostname; date -Ins; uptime"
bash "$DIAG_SCRIPT" capture user@host
bash "$DIAG_SCRIPT" cleanup --host user@host
```

The target precedes flags, which precede `--`. Commands are data for the helper; do not interpolate untrusted log text into a shell command. Multi-line commands use the documented `--stdin` path rather than nested quoting.

```bash
bash "$DIAG_SCRIPT" case-create vip-check \
  --target client=user1@client-host --target server=user2@server-host --ttl 1800
bash "$DIAG_SCRIPT" connect-many vip-check
bash "$DIAG_SCRIPT" exec --case vip-check --role client --timeout 30 -- "ip route get <VIP>"
bash "$DIAG_SCRIPT" exec --case vip-check --role server --timeout 30 -- "ss -lntup"
bash "$DIAG_SCRIPT" status --case vip-check
bash "$DIAG_SCRIPT" cleanup --case vip-check
```

`connect-many` connects sequentially so authentication can be reused. Check `expected/processed/ready` and failed roles; one ready machine does not make a case ready. `exec-all` is for independent reads only; inspect `exec_summary` and every role's current request output before a dependent step. The default limit is eight targets; expansion needs explicit scope.

## Authorization and bounded work

Default to read-only commands. For writes/copies/capture use `--confirm-write`; for privilege, restart, kill, deletion or network configuration changes use `--confirm-dangerous`. A flag records authorization, it does not obtain it. Honor existing explicit authorization for the concrete command and scope; if absent, present the command/impact and obtain it before setting the flag. The shell classifier is a guard, not a complete security sandbox.

Every command has a timeout (default 60 seconds), authentication wait (default 120 seconds), idle TTL (default 1800 seconds), and session lifetime bound (default 14400 seconds). Use `watch --until ... --timeout ...` for bounded observation; avoid unlimited tail, capture or background loops. A polling timeout is not proof of product failure. An unknown result of a mutating command requires reconciliation before retry.

The helper owns only its token-matched pane/state/locks. Reuse requires exact identity, TTL and a health probe. Authentication happens interactively in that pane; continue target SSH in the same relay shell. Do not reuse an unrelated existing terminal merely because it is logged in.

## Network and process evidence

For VIP failures, inspect each role's clock, routes, source address/neighbor state, listener, process version and bounded logs. Distinguish “client did not send”, “path loss”, “server received but did not answer”, and “reply took a different path”. ICMP failure alone does not establish application failure.

Packet capture needs an explicit interface, filter, duration, packet cap and applicable authorization. Start the observer, verify it is ready, then issue the bounded client probe and correlate both sides. Check the target's available capture/timeout tools instead of assuming syntax. Netcat EOF behavior differs by implementation; see [remote-troubleshooting.md](remote-troubleshooting.md).

For agent process inspection use `proc-audit`; do not equate a quiet process with an abandoned session. `proc-stop --session-root PID` previews a newly derived process set; only an authorized `--apply --confirm-dangerous` sends SIGTERM. Shared parents, root-owned processes and PID reuse require the helper's scope checks; never broaden to a global kill command.

## Completion

Keep case/role/host, request ID, time range, exit status, limits/truncation and redacted evidence. Retain evidence needed for the report before cleanup: `cleanup --all` removes managed logs as well as sessions. Use case/host cleanup for this task and report any resources whose ownership could not be proven.

For unsupported transport, auth failure, stale session, output decoding or timeout, use [remote-troubleshooting.md](remote-troubleshooting.md). Report transport uncertainty separately from the service diagnosis.
