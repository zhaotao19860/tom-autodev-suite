#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
export TOM_AUTODEBUG_HOME="$TMP_ROOT/state"

# shellcheck source=../scripts/autodebug.sh
source "$ROOT/scripts/autodebug.sh"

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

printf 'PASS: tom-autodebug regression tests\n'
