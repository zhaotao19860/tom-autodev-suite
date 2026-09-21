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

The transport captures request streams through `tmux pipe-pane`; normal wide output should not require adding `head` just to fit a terminal. Keep bounded output and disclose truncation. Process output can contain invalid UTF-8; byte-safe parsing must not turn a decode issue into a network timeout.

For process snapshots, use separately specified `ps -o` fields in this helper's portable command templates. Verify the remote implementation before generalizing formatting behavior; do not turn one version's parsing workaround into a universal OS rule.

## UDP probe has no output

Identify the exact `nc`/Ncat implementation and its help first. Correlate verbose exit timing, route/source address and bounded hex output. A client that exits immediately on stdin EOF may stop receiving before a reply arrives; “sent bytes, zero received” alone is not proof of a dropped packet. An approved, bounded repeat that keeps stdin open briefly can distinguish this from server/path failure. Capture evidence at the relevant endpoints when needed; do not infer success from an open UDP socket.

## Process governance

Read `proc-audit`'s session root, shared parent, attached TTY, lifetime, CPU/RSS and session grouping together. Recompute the selected tree at `proc-stop` time; never reuse an old PID list. SIGKILL and broad process termination are not this helper's fallback. Report remaining processes and seek a scoped decision when graceful stop is insufficient.
