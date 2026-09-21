#!/usr/bin/env bash
# Session-scoped graceful stop. Re-derives the process set at execution time so stale
# PIDs collected in an earlier step can never be reused blindly.
# Placeholders substituted by autodebug.sh:
#   @@ROOT@@ @@PATTERN@@ @@GRACE@@ @@MODE@@ @@ALLOWROOT@@ @@INCLUDESHARED@@
set -uo pipefail
export LC_ALL=C

ROOT='@@ROOT@@'
PATTERN='@@PATTERN@@'
GRACE='@@GRACE@@'
MODE='@@MODE@@'
ALLOWROOT='@@ALLOWROOT@@'
INCLUDESHARED='@@INCLUDESHARED@@'

snapshot() {
  if [ -n "${AUTODEBUG_PS_FIXTURE:-}" ]; then printf '%s\n' "$AUTODEBUG_PS_FIXTURE"; return 0; fi
  # 每个字段独立 -o；`-o pid=,ppid=` 在 procps 下只会输出一列并丢掉 args。
  ps -e -o pid= -o ppid= -o user= -o args= 2>/dev/null
}

# Emits: "pid user args" for root plus all transitive descendants.
tree_of() {
  printf '%s\n' "$1" | awk -v root="$2" '
    { pid=$1; ppid=$2; usr=$3; a=""; for (i=4;i<=NF;i++) a = a (i>4?" ":"") $i
      P[pid]=ppid; U[pid]=usr; A[pid]=a; seen[pid]=1 }
    END {
      if (!(root in seen)) { exit 0 }
      inset[root]=1
      changed=1
      while (changed) {
        changed=0
        for (p in seen) { if (!(p in inset) && (P[p] in inset)) { inset[p]=1; changed=1 } }
      }
      for (p in inset) { a=A[p]; gsub(/[^ -~]/, "?", a); print p "\t" U[p] "\t" a }
    }'
}

# ps pads numeric columns, so field slicing must go through awk rather than cut -d' '.
args_of() { printf '%s\n' "$1" | awk -v r="$2" '$1==r { s=""; for (i=4;i<=NF;i++) s = s (i>4?" ":"") $i; print s; exit }'; }

SNAP="$(snapshot)"
[ -n "$SNAP" ] || { echo "stop_error=ps_failed"; exit 1; }

ROOT_LINE="$(printf '%s\n' "$SNAP" | awk -v r="$ROOT" '$1==r')"
if [ -z "$ROOT_LINE" ]; then
  echo "stop_error=root_not_found root=$ROOT"
  echo "hint=re-run proc-audit; PIDs may have exited or been recycled"
  exit 1
fi
ROOT_ARGS="$(args_of "$SNAP" "$ROOT")"
case "$ROOT_ARGS" in
  *ducc*|*claude*|*happy*|*codex*) : ;;
  *) echo "stop_error=root_not_agent root=$ROOT args=$ROOT_ARGS"; exit 1 ;;
esac

# Shared-daemon guard: a Happy daemon can serve several independent sessions.
ROOT_PPID="$(printf '%s\n' "$ROOT_LINE" | awk '{print $2}')"
PARENT_ARGS="$(args_of "$SNAP" "$ROOT_PPID")"
SHARED="none"; SHARED_OTHERS=0
case "$PARENT_ARGS" in
  *"daemon start-sync"*|*happy*daemon*)
    SHARED="$ROOT_PPID"
    SHARED_OTHERS="$(printf '%s\n' "$SNAP" | awk -v d="$ROOT_PPID" -v r="$ROOT" '$2==d && $1!=r' | wc -l | tr -d ' ')"
    ;;
esac

TREE="$(tree_of "$SNAP" "$ROOT")"
echo "plan_root=$ROOT"
echo "plan_root_args=$ROOT_ARGS"
echo "plan_shared_parent=$SHARED"
echo "plan_shared_parent_other_children=$SHARED_OTHERS"

TARGETS=""
SKIPPED=""
while IFS="$(printf '\t')" read -r pid usr args; do
  [ -n "$pid" ] || continue
  [ "$pid" != "1" ] || continue
  if [ "$usr" = "root" ] && [ "$ALLOWROOT" != "1" ]; then
    SKIPPED="$SKIPPED $pid"
    echo "skip pid=$pid user=$usr reason=root_owned args=$args"
    continue
  fi
  TARGETS="$TARGETS $pid"
  echo "target pid=$pid user=$usr args=$args"
done <<EOF
$TREE
EOF

if [ "$SHARED" != "none" ]; then
  if [ "$INCLUDESHARED" = "1" ] && [ "$SHARED_OTHERS" = "0" ]; then
    TARGETS="$TARGETS $SHARED"
    echo "target pid=$SHARED reason=shared_parent_no_other_sessions"
  elif [ "$INCLUDESHARED" = "1" ]; then
    echo "refuse_shared_parent=$SHARED other_children=$SHARED_OTHERS"
    echo "hint=daemon still serves other sessions; stop them first or leave the daemon running"
  else
    echo "keep_shared_parent=$SHARED (pass --include-shared-parent to consider it)"
  fi
fi

TARGET_COUNT="$(printf '%s' "$TARGETS" | wc -w | tr -d ' ')"
echo "plan_target_count=$TARGET_COUNT"
echo "plan_skipped=${SKIPPED:-none}"

if [ "$MODE" != "apply" ]; then
  echo "mode=plan (nothing was signalled)"
  exit 0
fi
[ "$TARGET_COUNT" != "0" ] || { echo "mode=apply result=no_targets"; exit 0; }

# ---APPLY-SECTION---
# Everything below is removed before transfer when running in plan mode, so a plan run
# carries no signal-sending code at all and needs no --confirm-dangerous.
# Leaves first, then the launcher, so a parent cannot respawn a child mid-shutdown.
ORDERED="$(printf '%s\n' $TARGETS | sort -rn | tr '\n' ' ')"
echo "term_order=$ORDERED"
for p in $ORDERED; do kill -TERM "$p" 2>/dev/null && echo "term_sent=$p" || echo "term_failed=$p"; done
sleep "$GRACE"

SNAP2="$(snapshot)"
REMAIN=""
for p in $TARGETS; do
  if printf '%s\n' "$SNAP2" | awk -v r="$p" '$1==r' | grep -q .; then REMAIN="$REMAIN $p"; fi
done
REMAIN_COUNT="$(printf '%s' "$REMAIN" | wc -w | tr -d ' ')"
echo "after_grace_remaining=${REMAIN:-none}"
echo "after_grace_remaining_count=$REMAIN_COUNT"
if [ "$REMAIN_COUNT" != "0" ]; then
  for p in $REMAIN; do kill -TERM "$p" 2>/dev/null && echo "term_retry=$p" || true; done
  sleep "$GRACE"
  SNAP3="$(snapshot)"
  STILL=""
  for p in $REMAIN; do
    if printf '%s\n' "$SNAP3" | awk -v r="$p" '$1==r' | grep -q .; then STILL="$STILL $p"; fi
  done
  echo "final_remaining=${STILL:-none}"
  [ -z "$STILL" ] || echo "hint=SIGKILL not sent by design; decide explicitly if these must be force-killed"
else
  echo "final_remaining=none"
fi
echo "mode=apply result=done"
