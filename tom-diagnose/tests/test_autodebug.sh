#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
export TOM_AUTODEBUG_HOME="$TMP_ROOT/state"

# shellcheck source=../scripts/autodebug.sh
source "$ROOT/scripts/autodebug.sh"
ORIGINAL_EXEC_ON_STATE="$(declare -f exec_on_state)"
ORIGINAL_CMD_CONNECT="$(declare -f cmd_connect)"
ORIGINAL_HEALTH_PROBE="$(declare -f health_probe)"

fail_test() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_contains() {
  local text="$1" expected="$2"
  [[ "$text" == *"$expected"* ]] || fail_test "missing [$expected] in [$text]"
}

assert_eq() {
  local actual="$1" expected="$2" label="$3"
  [[ "$actual" == "$expected" ]] || fail_test "$label: expected=$expected actual=$actual"
}

init_dirs
CASE_FILE="$(case_state_path multi)"
CREATED_AT="$(now)"
write_case_state "$CASE_FILE" multi autodebug-multi owner "$CREATED_AT" 3600 \
  "first=root@host-a,second=root@host-b,last=root@host-c"

resolved="$(resolve_case_role multi last "$CASE_FILE")"
assert_eq "$resolved" "root@host-c" "resolve last role"

CALL_LOG="$TMP_ROOT/connect.log"
: > "$CALL_LOG"
require_cmd() { return 0; }
tmux_alive() { return 0; }
tmux() {
  if [[ "${1:-}" == display-message ]]; then
    printf 'owner\n'
  fi
}
cmd_connect() {
  local target="$1" parts full state_file
  printf '%s\n' "$target" >> "$CALL_LOG"
  [[ "$target" != *fail-host* ]] || return 1
  parts="$(parse_host "$target")"
  full="$(printf '%s\n' "$parts" | sed -n '3p')"
  state_file="$(host_state_path "$full")"
  write_host_state "$state_file" "$full" root ssh default autodebug-multi %1 owner \
    "$CREATED_AT" "$CREATED_AT" 3600 connected
}

connect_output="$(cmd_connect_many multi)"
assert_eq "$(wc -l < "$CALL_LOG" | tr -d ' ')" "3" "connect target count"
assert_contains "$connect_output" "=== connect role=last ==="
assert_contains "$connect_output" "connect_summary expected=3 processed=3 ready=3 failed_roles=none"

write_case_state "$CASE_FILE" multi autodebug-multi owner "$CREATED_AT" 3600 \
  "first=root@host-a,broken=root@fail-host,last=root@host-c"
: > "$CALL_LOG"
set +e
connect_output="$(cmd_connect_many multi 2>&1)"
connect_rc=$?
set -e
assert_eq "$connect_rc" "1" "partial connect exit"
assert_eq "$(wc -l < "$CALL_LOG" | tr -d ' ')" "3" "partial connect target count"
assert_contains "$connect_output" "connect_summary expected=3 processed=3 ready=2 failed_roles=broken"

CLEAN_LOG="$TMP_ROOT/cleanup.log"
: > "$CLEAN_LOG"
cleanup_host_file() {
  printf '%s\n' "$1" >> "$CLEAN_LOG"
}
write_case_state "$CASE_FILE" multi autodebug-multi owner "$CREATED_AT" 3600 \
  "first=root@host-a,second=root@host-b,last=root@host-c"
cmd_cleanup --case multi >/dev/null
assert_eq "$(wc -l < "$CLEAN_LOG" | tr -d ' ')" "3" "cleanup target count"
assert_contains "$(sed -n '3p' "$CLEAN_LOG")" "$(safe_key 'root@host-c')"
[[ ! -f "$CASE_FILE" ]] || fail_test "cleanup must remove case manifest"

write_case_state "$CASE_FILE" multi autodebug-multi owner "$CREATED_AT" 3600 \
  "first=root@host-a,second=root@host-b,last=root@host-c"
EXEC_LOG="$TMP_ROOT/exec.log"
: > "$EXEC_LOG"
ORIGINAL_CMD_EXEC="$(declare -f cmd_exec)"
cmd_exec() {
  local joined="$*" payload=""
  if [[ "$joined" == *"--stdin"* ]]; then
    payload="$(cat)"
  fi
  printf 'args=%s payload=%s\n' "$joined" "$payload" >> "$EXEC_LOG"
}

exec_output="$(cmd_exec_all --case multi --timeout 7 --confirm-write -- "date -Ins")"
assert_eq "$(wc -l < "$EXEC_LOG" | tr -d ' ')" "3" "exec target count"
assert_eq "$(grep -c -- '--timeout 7 --confirm-write' "$EXEC_LOG")" "3" "exec flags forwarded"
assert_contains "$exec_output" "=== role=last ==="
assert_contains "$exec_output" "exec_summary expected=3 processed=3 succeeded=3 failed=0"

: > "$EXEC_LOG"
exec_output="$(printf '%s' 'hostname; uptime' | cmd_exec_all --case multi --timeout 9 --stdin)"
assert_eq "$(grep -c 'payload=hostname; uptime' "$EXEC_LOG")" "3" "stdin forwarded"
assert_contains "$exec_output" "exec_summary expected=3 processed=3 succeeded=3 failed=0"

set +e
authorize_command "cp /tmp/a /tmp/b" "" >/dev/null 2>&1
low_without_confirm=$?
authorize_command "sudo reboot" --confirm-write >/dev/null 2>&1
high_with_write_confirm=$?
set -e
assert_eq "$low_without_confirm" "2" "low risk requires confirmation"
assert_eq "$high_with_write_confirm" "2" "write confirmation cannot allow high risk"
authorize_command "cp /tmp/a /tmp/b" --confirm-write
authorize_command "sudo reboot" --confirm-dangerous

eval "$ORIGINAL_CMD_EXEC"
EXEC_ON_STATE_LOG="$TMP_ROOT/exec-on-state.log"
exec_on_state() {
  printf 'confirm=%s check_src=%s\n' "$6" "$7" >> "$EXEC_ON_STATE_LOG"
}
printf '%s' 'cp /tmp/a /tmp/b' |
  cmd_exec --case multi --role last --timeout 11 --confirm-write --stdin
assert_contains "$(cat "$EXEC_ON_STATE_LOG")" "confirm=--confirm-write check_src=cp /tmp/a /tmp/b"

assert_ready() {
  local label="$1" screen="$2" expected="$3" rc
  set +e
  relay_shell_ready "$screen"
  rc=$?
  set -e
  assert_eq "$rc" "$expected" "$label"
}

# relay-cli 登录成功后本机 relay shell 只留提示符，没有目标机的登录横幅。
assert_ready "relay shell prompt is ready" \
  "$(printf '%s\n' 'relay-cli -u zhaotao02 -t fp' \
    '提示：配置 RELAY_AI_API_KEY 可启用 AI 交互模式（登录后按 Ctrl+A 切换），配置方法可运行 relay-cli -h 查看' \
    '-bash-baidu-ssl$')" 0
assert_ready "target login banner still ready" \
  "$(printf '%s\n' 'relay-cli -u zhaotao02 -t fp' 'Last login: Mon Sep  8 10:00:00 2026' '[root@host ~]# ')" 0
# 认证未完成：屏幕停在扫码/指纹等待，最后一行不是提示符。
assert_ready "pending auth is not ready" \
  "$(printf '%s\n' 'relay-cli -u zhaotao02 -t fp' 'please touch your fingerprint sensor')" 1
# send-keys 尚未生效时不能把本地 shell 的提示符当成 relay 就绪。
assert_ready "local prompt before echo is not ready" "$(printf '%s\n' 'tom@mac ~ $')" 1

# --- end marker 解析：正常换行 / 输出无结尾换行导致 marker 粘在行尾 ---
BEGIN_M="__TOM_AUTODEBUG_BEGIN_t1__"
END_M="__TOM_AUTODEBUG_END_t1__"
STREAM="$TMP_ROOT/stream-normal"
printf '%s\n' "echo-line $BEGIN_M" "$BEGIN_M" "line1" "line2" "$END_M 3" > "$STREAM"
printf '%s\n' "$END_M 3" | marker_is_complete_line "$END_M" || fail_test "normal end marker not complete"
assert_eq "$(marked_exit_code_file "$STREAM" "$END_M")" "3" "normal exit code"
assert_eq "$(capture_marked_file "$STREAM" "$BEGIN_M" "$END_M")" "$(printf 'line1\nline2')" "normal capture"
STREAM="$TMP_ROOT/stream-glued"
printf '%s\n' "$BEGIN_M" "head" "0 1 2 3 $END_M 0" > "$STREAM"
printf '%s\n' "0 1 2 3 $END_M 0" | marker_is_complete_line "$END_M" || fail_test "glued end marker not complete"
assert_eq "$(marked_exit_code_file "$STREAM" "$END_M")" "0" "glued exit code"
assert_eq "$(capture_marked_file "$STREAM" "$BEGIN_M" "$END_M")" "$(printf 'head\n0 1 2 3')" "glued capture"
# 命令回显行里 marker 后跟的是 shell 片段而不是数字，不能被当成结束。
set +e
printf '%s\n' "printf '%s %s\\n' '$END_M' \"\$rc\"" | marker_is_complete_line "$END_M"
echo_rc=$?
set -e
assert_eq "$echo_rc" "1" "echoed command is not end marker"
# health/connect marker 仍按单字段整行匹配。
printf '%s\n' "__TOM_AUTODEBUG_HEALTH_x__" | marker_is_complete_line "__TOM_AUTODEBUG_HEALTH_x__" || fail_test "health marker"

# --- exec_on_state 端到端：send-keys 带 set +H，粘连输出可正确解析 ---
eval "$ORIGINAL_EXEC_ON_STATE"
health_probe() { return 0; }
SENT_LOG="$TMP_ROOT/sent.log"
FAKE_OUTPUT=""
tmux() {
  case "${1:-}" in
    display-message) printf 'owner\n' ;;
    send-keys)
      local payload="$4" begin end stream
      printf '%s\n' "$payload" >> "$SENT_LOG"
      begin="$(printf '%s' "$payload" | grep -o '__TOM_AUTODEBUG_BEGIN_[0-9a-f]*__' | head -1)"
      end="$(printf '%s' "$payload" | grep -o '__TOM_AUTODEBUG_END_[0-9a-f]*__' | head -1)"
      stream="$(ls -t "$STATE_ROOT"/logs/.stream-* | head -1)"
      printf '%s\n%s%s 0\n' "$begin" "$FAKE_OUTPUT" "$end" >> "$stream"
      ;;
  esac
}
EXEC_STATE="$(host_state_path root@host-x)"
write_host_state "$EXEC_STATE" root@host-x root ssh default autodebug-x %9 owner \
  "$CREATED_AT" "$CREATED_AT" 3600 connected
: > "$SENT_LOG"
FAKE_OUTPUT="0 1 2 3 "
exec_out="$(exec_on_state "$EXEC_STATE" "" "" "seq 0 3 | tr '\\n' ' '" 5 "")"
assert_eq "$exec_out" "0 1 2 3" "glued exec output"
assert_contains "$(cat "$SENT_LOG")" "set +H 2>/dev/null; printf"
eval "$ORIGINAL_HEALTH_PROBE"

# --- connect --new-pane 在已有 session 里新建 window，而不是 split-window ---
eval "$ORIGINAL_CMD_CONNECT"
TMUX_LOG="$TMP_ROOT/tmux.log"
: > "$TMUX_LOG"
write_defaults relayu root >/dev/null
resolve_relay() { RELAY_BIN=/bin/true; return 0; }
relay_help() { return 0; }
relay_login_mode() { printf 'top-level-user\n'; }
wait_for_relay_shell() { return 0; }
wait_for_marker() { return 0; }
spawn_cleanup_watcher() { return 0; }
tmux() {
  printf '%s\n' "$*" >> "$TMUX_LOG"
  case "${1:-}" in
    new-window) printf '%%42\n' ;;
  esac
}
cmd_connect root@host-new --session autodebug-multi --new-pane >/dev/null
assert_contains "$(cat "$TMUX_LOG")" "new-window -d -t autodebug-multi: -P -F #{pane_id}"
if grep -q split-window "$TMUX_LOG"; then fail_test "connect --new-pane must not split-window"; fi
assert_eq "$(state_get pane "$(host_state_path root@host-new)")" "%42" "new-window pane recorded"

assert_eq "$MAX_TARGETS" "32" "default max targets"

# --- cluster：解析 2 个服务 / 3 个实例，并按第一个服务建 case ---
CLUSTER_SAMPLE="SERVICE yq01-sys-ibgwcluster.IP.yq01
INSTANCE yq01-sys-x86ibgw44.yq01 10.198.156.149 0
INSTANCE yq01-sys-x86ibgw45.yq01 10.198.156.150 -1
SERVICE yq01-sys-other.IP.yq01
INSTANCE yq01-sys-x86ibgw44.yq01 10.198.156.149 0"
CLUSTER_STATE="$(host_state_path root@yq01-sys-x86ibgw44.yq01)"
write_host_state "$CLUSTER_STATE" root@yq01-sys-x86ibgw44.yq01 root ssh default s %1 owner \
  "$CREATED_AT" "$CREATED_AT" 3600 connected
CLUSTER_CALLS="$TMP_ROOT/cluster.log"
: > "$CLUSTER_CALLS"
resolve_state_file() { RESOLVED_STATE_FILE="$CLUSTER_STATE"; }
exec_on_state() {
  printf 'state=%s confirm=%s cmd=%s\n' "$1" "$6" "$4" >> "$CLUSTER_CALLS"
  printf '%s\n' "$CLUSTER_SAMPLE"
}
cluster_out="$(cmd_cluster yq01-sys-x86ibgw44.yq01)"
assert_contains "$cluster_out" "service=yq01-sys-ibgwcluster.IP.yq01"
assert_contains "$cluster_out" "  yq01-sys-x86ibgw45.yq01 10.198.156.150 -1"
assert_contains "$cluster_out" "cluster_summary service=yq01-sys-ibgwcluster.IP.yq01 instances=2 abnormal=1"
assert_contains "$cluster_out" "cluster_summary service=yq01-sys-other.IP.yq01 instances=1 abnormal=0"
assert_contains "$(cat "$CLUSTER_CALLS")" "confirm= cmd="
assert_contains "$(cat "$CLUSTER_CALLS")" "get_service_by_host -i"
assert_eq "$(danger_level "$CLUSTER_REMOTE_CMD")" "none" "cluster command is read-only"

cluster_out="$(cmd_cluster yq01-sys-x86ibgw44.yq01 --create-case bgwc --ttl 600 2>&1)"
assert_contains "$cluster_out" "next: autodebug.sh connect-many bgwc"
assert_eq "$(state_get targets "$(case_state_path bgwc)")" \
  "h1=root@yq01-sys-x86ibgw44.yq01,h2=root@yq01-sys-x86ibgw45.yq01" "cluster case targets"
assert_eq "$(state_get ttl_seconds "$(case_state_path bgwc)")" "600" "cluster case ttl"

CLUSTER_SAMPLE="NO_BNS_TOOLS"
set +e
cluster_out="$(cmd_cluster yq01-sys-x86ibgw44.yq01 2>&1)"
cluster_rc=$?
set -e
assert_eq "$cluster_rc" "3" "missing bns tools rc"
assert_contains "$cluster_out" "缺少 BNS 工具"

rm -f "$CLUSTER_STATE"
set +e
cluster_out="$(cmd_cluster yq01-sys-x86ibgw44.yq01 2>&1)"
cluster_rc=$?
set -e
assert_eq "$cluster_rc" "6" "cluster requires connect"
assert_contains "$cluster_out" "请先执行: autodebug.sh connect"

printf 'PASS: tom-autodebug regression tests\n'
