# Remote Transport Troubleshooting

Use the bundled script's `SUBCOMMAND --help` for supported arguments. These observations are scoped to the imported relay/tmux implementation; verify current tool versions before applying an old workaround.

| Signal | Action and interpretation |
|---|---|
| Missing tmux/relay or unsupported login flags | Stop before connection and identify the missing capability. Do not silently download or replace a binary. |
| Missing username defaults | Ask for only missing relay/SSH usernames; retain explicitly supplied identities. |
| auth_pending | Have the user complete interactive authentication in the managed pane, within the auth timeout. |
| Relay shell ready, target not connected | Target SSH must run inside that same relay pane. Exiting relay destroys the path. |
| Identity/owner mismatch | Never reuse the pane. Preserve resources not demonstrably owned and establish a correctly scoped session. |
| Invalid state, lock timeout, expired TTL | Use the helper's bounded state/lock recovery. Do not remove arbitrary state to force access. |
| timeout (exit 4) | No complete request marker arrived in time; the helper sends C-c and rechecks health. Reconcile mutating-command effects before retry. |
| output_decode_error (exit 7) | Local output decoding failed; this does not imply the remote command hung. Use a bounded binary-safe observation. |
| marker_incomplete (exit 7) | End marker lacks a trustworthy exit status. Report an incomplete observation, not success. |
| Old or clipped capture | Use request-specific complete marker lines and stream-file evidence; terminal echo does not prove completion. Report byte/line limits. |
| expected differs from processed/ready | The multi-host case is partial. Report the affected roles instead of declaring every target failed or ready. |
| Relay asks to upgrade | Ask the user first; do not overwrite the existing binary automatically. |
| `Permission denied` | Check the relay user, target SSH user and target permission; ask for the correct `user@host` instead of guessing. |
| `has-session` fails but `new-session` reports duplicate | The session name contains a character tmux normalizes (such as `.`). Use the helper-generated safe name; do not hand-reuse the odd session. |
| Unclosed quote leaves the pane at a `>` continuation | Handled as a timeout: the helper sends C-c and rechecks health. Continue if healthy, otherwise cleanup and reconnect. Do not resend the same command. |
| Read-only command flagged as a write | `>/dev/null`, `2>&1` and quoted literals such as `echo "a -> b"` are not writes; use `--confirm-write` only for a real write. |

The transport captures request streams through `tmux pipe-pane`; normal wide output should not require adding `head` just to fit a terminal. Keep bounded output and disclose truncation. Process output can contain invalid UTF-8; byte-safe parsing must not turn a decode issue into a network timeout.

For process snapshots, use separately specified `ps -o` fields in this helper's portable command templates. Verify the remote implementation before generalizing formatting behavior; do not turn one version's parsing workaround into a universal OS rule.

## UDP probe has no output

Identify the exact `nc`/Ncat implementation and its help first. Correlate verbose exit timing, route/source address and bounded hex output. A client that exits immediately on stdin EOF may stop receiving before a reply arrives; “sent bytes, zero received” alone is not proof of a dropped packet. An approved, bounded repeat that keeps stdin open briefly can distinguish this from server/path failure. Capture evidence at the relevant endpoints when needed; do not infer success from an open UDP socket.

## Process governance

Read `proc-audit`'s session root, shared parent, attached TTY, lifetime, CPU/RSS and session grouping together. Recompute the selected tree at `proc-stop` time; never reuse an old PID list. SIGKILL and broad process termination are not this helper's fallback. Report remaining processes and seek a scoped decision when graceful stop is insufficient.

## Known capability limits

- **No file transfer to the target.** Relay allows only interactive login: the target's port 22 has no direct route, `ssh -J <relay>` is refused (`Session open refused by peer`, direct-tcpip disabled), non-interactive `ssh <relay> '<command>'` is swallowed by the relay's custom shell, and `relay-cli` has no scp/sftp subcommand. Do not probe these paths again. When the target needs a file, have it pull the file itself (intranet BOS/mirror usually has enough bandwidth).
- Before a remote download/install, check for an identical concurrent process (`ps -ef | grep <keyword>`). Parallel pulls of one file compete for bandwidth, and many install scripts delete the previous partial file first, producing a "slower with every retry" false network fault.
- **`ps -e -o` needs a separate `-o` per field.** With `ps -eo pid=,ppid=,args=`, procps treats `,ppid=,args=` as the pid column header and prints one column; matching on args then silently returns zero hits.
- Remote command lines can contain invalid UTF-8 (for example a truncated Chinese system prompt). Local text processing runs under `LC_ALL=C`; otherwise `sed`/`awk` fail with `illegal byte sequence`, the marker is missed and the result is misreported as a timeout.

## Retry policy by stage

| Stage | Policy |
|---|---|
| Argument/state parsing | No retry; name the bad field. Quarantine a corrupt state file and rebuild. |
| Lock | Bounded wait (about 10 seconds); stop if still held. |
| Relay authentication | Never guess passwords or retry indefinitely; the user completes auth in the managed pane. |
| tmux creation | At most one retry, then report the tmux output. |
| SSH connect | At most two retries for a transient failure. |
| Command execution | Do not rerun a possibly mutating command; on timeout the helper sends C-c and rechecks health. |
| capture | At most one reread while the connection is healthy; otherwise report stale. |
| TTL / total lifetime | Stop, clean managed resources and return the next suggested step. |

## VIP unreachable template

With explicit authorization and client, server/RS and optional LB/observer roles:

1. Client, read-only: `hostname`, `date -Ins`, `ip addr`, `ip route get <VIP>`, `ip neigh`, bounded `ping -c 3 -W 1 <VIP>`.
2. Server/RS, read-only: `hostname`, `date -Ins`, relevant addresses and routes, `ss -lntup`, service process and a bounded log tail.
3. If dual-side capture is needed, first agree interface, filter, duration and packet count, e.g. `timeout 15 tcpdump -nn -i <if> host <VIP> -c 100`; sudo needs its own confirmation. Start the observer and get its request ID, then send the bounded client probe, then read both sides aligned by `role + date -Ins + request-id`.
4. Classify from both sides: client did not send, neighbor/route fault, loss on path, server received but did not answer, asymmetric return path, or service not listening. Do not conclude from one side. Clean up the case and confirm the capture and managed panes exited.

For BGW, first locate the subsystem and version with [domains/bgw.md](domains/bgw.md).
