#!/usr/bin/env bash
# Read-only agent/process audit. Runs on the target host, prints key=value records.
# Placeholders are substituted by autodebug.sh before transfer:
#   @@PATTERN@@  extended regex selecting processes of interest
#   @@MAXPROC@@  max records to print
set -uo pipefail
export LC_ALL=C

PATTERN='@@PATTERN@@'
MAXPROC='@@MAXPROC@@'

# 每个字段必须用独立的 -o：`-o pid=,ppid=` 在 procps 里会把 ",ppid=" 当成 pid 列的表头，
# 结果只输出 PID 一列，args 全丢，审计会静默变成 0 匹配。
PS_OUT="${AUTODEBUG_PS_FIXTURE:-$(ps -e -o pid= -o ppid= -o pgid= -o sid= -o user= -o tty= -o etimes= -o pcpu= -o pmem= -o rss= -o args= 2>/dev/null)}"
[ -n "$PS_OUT" ] || { echo "audit_error=ps_failed"; exit 1; }

printf '%s\n' "$PS_OUT" | awk -v pat="$PATTERN" -v maxproc="$MAXPROC" '
function argstr(n,   i, s) { s=""; for (i=11; i<=n; i++) s = s (i>11 ? " " : "") $i; return s }
# Agent stack membership: used to walk up from a leaf to its launcher.
function is_stack(a) { return (a ~ /ducc|claude-go|claude|happy|codex/) }
# A daemon serves many sessions; it must never be treated as a per-session root.
# Match only real daemon launchers -- note "--started-by daemon" appears on ordinary
# session processes, so a loose / daemon$/ test would misclassify them as the daemon.
function is_daemon(a) { return (a ~ /daemon start-sync/ || a ~ /\.mjs daemon( |$)/) }
function session_key(a,   m) {
  if (match(a, /127\.0\.0\.1:[0-9]+/))      { return substr(a, RSTART, RLENGTH) }
  if (match(a, /session-hook-[0-9]+/))      { return substr(a, RSTART, RLENGTH) }
  return ""
}
function kind(a) {
  if (a ~ /baidu-cc|claude-go|\/ducc/)                 return "comate-ducc"
  if (a ~ /happy/ && a ~ /codex/)                      return "happy-codex"
  if (a ~ /happy/)                                     return "happy-claude"
  if (a ~ /claude/)                                    return "claude-other"
  return "other"
}
{
  pid=$1; A[pid]=argstr(NF); PP[pid]=$2; PG[pid]=$3; SD[pid]=$4; US[pid]=$5
  TT[pid]=$6; ET[pid]=$7; CP[pid]=$8; MP[pid]=$9; RSKB[pid]=$10; seen[pid]=1
  if (A[pid] ~ pat) matched[pid]=1
}
END {
  n=0
  for (p in matched) {
    cur=p
    # Walk up while the parent is still part of the agent stack and is not a shared daemon.
    while (1) {
      par=PP[cur]
      if (!(par in seen) || par==1) break
      if (!is_stack(A[par]) || is_daemon(A[par])) break
      cur=par
    }
    root[p]=cur
    par=PP[cur]
    if ((par in seen) && par!=1 && is_stack(A[par]) && is_daemon(A[par])) dmn[p]=par; else dmn[p]=0
    rootcount[cur]++
    if (dmn[p]) {
      # Count distinct sessions under a daemon, not raw process hits, so
      # shared_parent_sessions answers "how many sessions would I disturb".
      pair = dmn[p] ":" cur
      if (!(pair in seenpair)) { seenpair[pair]=1; dcount[dmn[p]]++ }
    }
    order[++n]=p
  }
  if (n==0) { print "audit_matches=0"; exit 0 }
  print "audit_matches=" n
  for (i=1; i<=n && i<=maxproc+0; i++) {
    p=order[i]; r=root[p]; d=dmn[p]
    sk=session_key(A[p]); if (sk=="") sk="pgid:" PG[p]
    print "---"
    print "pid=" p
    print "kind=" kind(A[p])
    print "session_key=" sk
    print "session_root=" r
    print "session_root_kind=" kind(A[r])
    print "session_root_user=" US[r]
    print "shared_parent=" (d ? d : "none")
    print "shared_parent_sessions=" (d ? dcount[d] : 0)
    print "user=" US[p]
    print "ppid=" PP[p] " pgid=" PG[p] " sid=" SD[p] " tty=" TT[p]
    print "elapsed_seconds=" ET[p]
    print "cpu_percent=" CP[p] " mem_percent=" MP[p] " rss_kb=" RSKB[p]
    print "permission_bypass=" (A[p] ~ /dangerously-skip-permissions/ ? "yes" : "no")
    print "root_tree_members=" rootcount[r]
    # Non-printable bytes are replaced so downstream parsing never sees illegal sequences.
    a=A[p]; gsub(/[^ -~]/, "?", a)
    print "args=" substr(a, 1, 400)
  }
  print "---"
  for (p in matched) {
    r=root[p]
    rss_sum[r] += RSKB[p]; cpu_sum[r] += CP[p]
    if (ET[p]+0 > age_max[r]+0) age_max[r] = ET[p]
    if (TT[p] != "?" && TT[p] != "-") tty_any[r] = 1
    if (A[p] ~ /dangerously-skip-permissions/) bypass_any[r] = 1
  }
  for (r in rootcount) {
    print "session_summary root=" r " kind=" kind(A[r]) " user=" US[r] \
      " members=" rootcount[r] " rss_total_kb=" rss_sum[r] " cpu_total_percent=" cpu_sum[r] \
      " oldest_seconds=" age_max[r] " tty_attached=" (tty_any[r] ? "yes" : "no") \
      " permission_bypass=" (bypass_any[r] ? "yes" : "no") \
      " long_lived=" (age_max[r]+0 > 86400 ? "yes" : "no")
  }
  print "note=cpu_total_percent 是采样瞬时值；tty_attached=no 只说明未接终端，不等于会话已废弃"
  for (r in rootcount) print "root_summary root=" r " members=" rootcount[r] " kind=" kind(A[r]) " user=" US[r]
  for (d in dcount)    print "daemon_summary daemon=" d " sessions=" dcount[d] " args=" substr(A[d], 1, 160)
}
'
