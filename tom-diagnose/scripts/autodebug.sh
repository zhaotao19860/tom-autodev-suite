#!/usr/bin/env bash
# tom-autodebug: bounded relay-cli/tmux remote diagnosis wrapper.
set -uo pipefail

readonly VERSION="1.5.0"
readonly DEFAULT_TTL="${TOM_AUTODEBUG_TTL:-1800}"
readonly CONNECT_TIMEOUT="${TOM_AUTODEBUG_CONNECT_TIMEOUT:-120}"
readonly COMMAND_TIMEOUT="${TOM_AUTODEBUG_COMMAND_TIMEOUT:-60}"
readonly MAX_TARGETS="${TOM_AUTODEBUG_MAX_TARGETS:-8}"
# pane 宽度直接决定远端 pty 的 COLUMNS；过窄会让 ss/ip/ps 等宽表格输出在折行处粘连丢字符。
readonly PANE_COLS="${TOM_AUTODEBUG_PANE_COLS:-512}"
readonly PANE_ROWS="${TOM_AUTODEBUG_PANE_ROWS:-50}"
readonly STATE_ROOT="${TOM_AUTODEBUG_HOME:-$HOME/.tom-autodebug}"
readonly DEFAULTS_FILE="$STATE_ROOT/defaults.env"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly REMOTE_DIR="$SCRIPT_DIR/remote"
# 默认识别的 Agent 进程集合；proc-audit/proc-stop/wait-processes 共用。
readonly AGENT_PATTERN_DEFAULT="${TOM_AUTODEBUG_AGENT_PATTERN:-ducc|claude-go|claude|happy|codex}"

RELAY_BIN=""
RELAY_HELP=""

usage() {
  cat <<'EOF'
Usage:
  autodebug.sh preflight
  autodebug.sh defaults set --relay-user USER --ssh-user USER
  autodebug.sh defaults show
  autodebug.sh defaults clear
  autodebug.sh connect <host-or-ip> [--user USER] [--relay-user USER] [--ttl SECONDS] [--session NAME] [--namespace NAME] [--transport ssh] [--new-pane]
  autodebug.sh exec <host-or-ip> [--timeout SECONDS] [--confirm-write|--confirm-dangerous] [--stdin] -- COMMAND...
  autodebug.sh exec --case CASE_ID --role ROLE [--timeout SECONDS] [--confirm-write|--confirm-dangerous] -- COMMAND...
  autodebug.sh watch <host-or-ip> --until "TEST_CMD" [--probe "CMD"] [--interval SECONDS] [--timeout SECONDS]
  autodebug.sh proc-audit <host-or-ip> [--pattern REGEX] [--max N] [--timeout SECONDS]
  autodebug.sh proc-stop <host-or-ip> --session-root PID [--apply --confirm-dangerous] [--grace SECONDS]
                                      [--include-shared-parent] [--allow-root-owned]
  autodebug.sh wait-processes <host-or-ip> --pattern REGEX [--interval SECONDS] [--timeout SECONDS]
  autodebug.sh capture <host-or-ip> [--lines N]
  autodebug.sh status [--case CASE_ID]
  autodebug.sh cleanup [--host HOST] [--case CASE_ID] [--all]
  autodebug.sh case-create CASE_ID --target ROLE=[USER@]HOST [--target ROLE=[USER@]HOST ...] [--ttl SECONDS]
  autodebug.sh connect-many CASE_ID
  autodebug.sh exec-all --case CASE_ID [--timeout SECONDS] [--confirm-write|--confirm-dangerous] -- COMMAND...
  autodebug.sh exec-all --case CASE_ID [--timeout SECONDS] [--confirm-write|--confirm-dangerous] --stdin < SCRIPT

Notes:
  - 只支持 transport=ssh；BNS/Matrix 入口未实现。
  - TTL 为空闲滑动窗口，硬上限由 TOM_AUTODEBUG_MAX_LIFETIME 控制（默认 14400s）。
  - 副作用命令分两级：低危（写文件/cp/ln/tee/tcpdump/nohup）需 --confirm-write；
    高危（sudo/kill/rm/mv/chmod/dd/systemctl/iptables/ip 变更/reboot）需 --confirm-dangerous。
  - 任何子命令加 --help 打印自己的用法。
EOF
}

log() { printf '[tom-autodebug] %s\n' "$*"; }
warn() { printf '[tom-autodebug] WARN: %s\n' "$*" >&2; }
fail() {
  local message="${1:-未知错误}" status="${2:-2}"
  printf '[tom-autodebug] ERROR: %s\n' "$message" >&2
  return "$status"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { fail "缺少依赖: $1" 3; return $?; }
}

now() { date +%s; }

valid_token() {
  [[ "${1:-}" =~ ^[A-Za-z0-9_.:-]+$ ]]
}

safe_key() {
  printf '%s' "$1" | tr '@/:[]' '_____'
}

safe_tmux_name() {
  local normalized
  normalized="$(printf '%s' "$1" | tr -c 'A-Za-z0-9_-' '_')"
  normalized="${normalized#_}"
  normalized="${normalized%_}"
  [[ -n "$normalized" ]] || normalized="autodebug"
  printf '%.120s\n' "$normalized"
}

resolve_relay() {
  if [[ -n "${RELAY_BIN:-}" && -x "$RELAY_BIN" ]]; then
    return 0
  fi
  if [[ -n "${TOM_AUTODEBUG_RELAY_BIN:-}" ]]; then
    RELAY_BIN="$TOM_AUTODEBUG_RELAY_BIN"
  elif command -v relay-cli >/dev/null 2>&1; then
    RELAY_BIN="$(command -v relay-cli)"
  elif command -v relay >/dev/null 2>&1; then
    RELAY_BIN="$(command -v relay)"
  else
    fail "找不到 relay-cli 或 relay；请显式安装后重试" 3
    return $?
  fi
  [[ -x "$RELAY_BIN" ]] || { fail "relay 不可执行: $RELAY_BIN" 3; return $?; }
}

relay_help() {
  resolve_relay || return $?
  RELAY_HELP="$($RELAY_BIN -h 2>&1 || true)"
  [[ -n "$RELAY_HELP" ]] || { fail "无法读取 relay 运行时帮助: $RELAY_BIN -h" 3; return $?; }
  printf '%s\n' "$RELAY_HELP"
}

relay_login_mode() {
  [[ -n "$RELAY_HELP" ]] || relay_help >/dev/null || return $?
  if printf '%s\n' "$RELAY_HELP" | grep -Eq '(^|[[:space:]])-u([=[:space:]]|$)'; then
    printf '%s\n' top-level-user
  elif printf '%s\n' "$RELAY_HELP" | grep -Eq '(^|[[:space:]])login([[:space:]]|$)'; then
    printf '%s\n' login-subcommand
  else
    printf '%s\n' unsupported
  fi
}

preflight() {
  require_cmd tmux || return $?
  require_cmd mktemp || return $?
  require_cmd sed || return $?
  require_cmd tr || return $?
  require_cmd date || return $?
  resolve_relay || return $?
  relay_help >/dev/null || return $?
  local mode
  mode="$(relay_login_mode)"
  log "tmux=$(command -v tmux)"
  log "relay=$RELAY_BIN"
  log "relay_login_mode=$mode"
  if [[ "$mode" == unsupported ]]; then
    fail "当前 relay 不支持可识别的用户登录参数；请检查 relay -h 或更新 relay" 3
    return $?
  fi
}

parse_host() {
  local input="${1:-}" explicit_user="${2:-}" parsed_user parsed_host
  [[ -n "$input" ]] || { fail "目标 host 不能为空"; return $?; }
  [[ "$input" != *[[:space:]]* ]] || { fail "目标不能包含空白字符"; return $?; }
  [[ "$input" != -* ]] || { fail "目标不能以 - 开头"; return $?; }

  if [[ "$input" == *@* ]]; then
    parsed_user="${input%%@*}"
    parsed_host="${input#*@}"
    [[ -n "$parsed_user" && -n "$parsed_host" && "$parsed_host" != *@* ]] || {
      fail "目标格式必须是 [USER@]HOST"; return $?
    }
    [[ -z "$explicit_user" || "$explicit_user" == "$parsed_user" ]] || {
      fail "--user 与 user@host 不一致"; return $?
    }
    explicit_user="$parsed_user"
  else
    parsed_host="$input"
  fi

  explicit_user="${explicit_user:-$(default_get ssh_user 2>/dev/null || true)}"
  [[ -n "$explicit_user" ]] || {
    fail "缺少默认 SSH 用户。请先输入 relay_user=你的relay用户名, ssh_user=目标机用户名，然后执行 defaults set" 7
    return $?
  }
  valid_token "$explicit_user" || { fail "用户名包含不支持的字符"; return $?; }
  [[ -n "$parsed_host" ]] || { fail "host 不能为空"; return $?; }
  printf '%s\n' "$explicit_user" "$parsed_host" "${explicit_user}@${parsed_host}"
}

parse_target() {
  local spec="${1:-}" role rest user host full
  [[ "$spec" == *=* ]] || { fail "目标必须是 ROLE=[USER@]HOST"; return $?; }
  role="${spec%%=*}"
  rest="${spec#*=}"
  valid_token "$role" || { fail "role 只能包含字母、数字、下划线、点、冒号或短横线"; return $?; }
  local parsed
  parsed="$(parse_host "$rest")" || return $?
  user="$(printf '%s\n' "$parsed" | sed -n '1p')"
  host="$(printf '%s\n' "$parsed" | sed -n '2p')"
  full="$(printf '%s\n' "$parsed" | sed -n '3p')"
  printf '%s\n' "$role" "$user" "$host" "$full"
}

init_dirs() {
  mkdir -p "$STATE_ROOT/sessions" "$STATE_ROOT/cases" "$STATE_ROOT/locks" "$STATE_ROOT/logs" || {
    fail "无法创建状态目录: $STATE_ROOT" 6
    return $?
  }
  chmod 700 "$STATE_ROOT" "$STATE_ROOT/sessions" "$STATE_ROOT/cases" "$STATE_ROOT/locks" "$STATE_ROOT/logs" 2>/dev/null || true
}

host_state_path() { printf '%s/sessions/%s.env\n' "$STATE_ROOT" "$(safe_key "$1")"; }
case_state_path() { printf '%s/cases/%s.env\n' "$STATE_ROOT" "$(safe_key "$1")"; }
lock_path() { printf '%s/locks/%s.lock\n' "$STATE_ROOT" "$(safe_key "$1")"; }

random_token() {
  if command -v od >/dev/null 2>&1; then
    od -An -tx1 -N16 /dev/urandom 2>/dev/null | tr -d ' \n'
  else
    printf '%s-%s' "$(now)" "$$"
  fi
}

state_get() {
  local key="$1" file="$2"
  [[ -f "$file" ]] || return 1
  awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }' "$file"
}

atomic_write() {
  local file="$1" content="$2" dir tmp
  dir="${file%/*}"
  mkdir -p "$dir" || return 1
  tmp="$(mktemp "$dir/.state.XXXXXX")" || return 1
  chmod 600 "$tmp"
  if ! printf '%s\n' "$content" > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$file"
}

default_get() {
  state_get "$1" "$DEFAULTS_FILE"
}

write_defaults() {
  local relay_user="$1" ssh_user="$2"
  valid_token "$relay_user" || { fail "relay 用户名包含不支持的字符"; return 2; }
  valid_token "$ssh_user" || { fail "SSH 用户名包含不支持的字符"; return 2; }
  init_dirs || return $?
  atomic_write "$DEFAULTS_FILE" "schema=1
relay_user=$relay_user
ssh_user=$ssh_user" || {
    fail "写入默认账号失败: $DEFAULTS_FILE" 6
    return $?
  }
  chmod 600 "$DEFAULTS_FILE" 2>/dev/null || true
}

cmd_defaults() {
  local action="${1:-}" relay_user="" ssh_user="" arg
  shift || true
  case "$action" in
    set)
      while [[ $# -gt 0 ]]; do
        arg="$1"
        case "$arg" in
          --relay-user) [[ $# -ge 2 ]] || { fail "--relay-user 缺少值"; return 2; }; relay_user="$2"; shift 2 ;;
          --ssh-user) [[ $# -ge 2 ]] || { fail "--ssh-user 缺少值"; return 2; }; ssh_user="$2"; shift 2 ;;
          *) fail "defaults set 未知参数: $arg"; return 2 ;;
        esac
      done
      [[ -n "$relay_user" && -n "$ssh_user" ]] || {
        fail "请同时输入 relay_user=你的relay用户名, ssh_user=目标机用户名" 7
        return $?
      }
      write_defaults "$relay_user" "$ssh_user" || return $?
      log "已保存默认账号: relay_user=$relay_user ssh_user=$ssh_user"
      ;;
    show)
      [[ $# -eq 0 ]] || { fail "defaults show 不接受其他参数"; return 2; }
      [[ -f "$DEFAULTS_FILE" ]] || {
        fail "尚未配置默认账号。请输入 relay_user=你的relay用户名, ssh_user=目标机用户名" 7
        return $?
      }
      relay_user="$(default_get relay_user 2>/dev/null || true)"
      ssh_user="$(default_get ssh_user 2>/dev/null || true)"
      if [[ -z "$relay_user" || -z "$ssh_user" ]] || ! valid_token "$relay_user" || ! valid_token "$ssh_user"; then
        fail "默认账号文件不完整或已损坏，请重新输入 relay_user=你的relay用户名, ssh_user=目标机用户名" 7
        return $?
      fi
      printf 'relay_user=%s\nssh_user=%s\n' "$relay_user" "$ssh_user"
      ;;
    clear)
      [[ $# -eq 0 ]] || { fail "defaults clear 不接受其他参数"; return 2; }
      rm -f "$DEFAULTS_FILE"
      log "已清除默认账号"
      ;;
    *) fail "用法: defaults set|show|clear" 2; return $? ;;
  esac
}

write_host_state() {
  local file="$1" host="$2" user="$3" transport="$4" namespace="$5" session="$6" pane="$7" owner="$8" created="$9" last_used="${10}" ttl="${11}" phase="${12:-connected}"
  atomic_write "$file" "schema=1
host=$host
user=$user
transport=$transport
namespace=$namespace
session=$session
pane=$pane
owner_token=$owner
created_at=$created
last_used_at=$last_used
ttl_seconds=$ttl
phase=$phase
managed_pid=$$"
}

write_case_state() {
  local file="$1" case_id="$2" session="$3" owner="$4" created="$5" ttl="$6" targets="$7"
  atomic_write "$file" "schema=1
case_id=$case_id
session=$session
owner_token=$owner
created_at=$created
ttl_seconds=$ttl
target_count=$(printf '%s' "$targets" | tr ',' '\n' | awk 'NF {n++} END {print n+0}')
targets=$targets"
}

state_expired() {
  local file="$1" now_value created last_used ttl max_lifetime
  now_value="$(now)"
  created="$(state_get created_at "$file" 2>/dev/null || true)"
  last_used="$(state_get last_used_at "$file" 2>/dev/null || true)"
  ttl="$(state_get ttl_seconds "$file" 2>/dev/null || true)"
  [[ "$created" =~ ^[0-9]+$ && "$ttl" =~ ^[0-9]+$ ]] || return 0
  [[ "$last_used" =~ ^[0-9]+$ ]] || last_used="$created"
  (( last_used < created )) && last_used="$created"
  max_lifetime="${TOM_AUTODEBUG_MAX_LIFETIME:-14400}"
  [[ "$max_lifetime" =~ ^[0-9]+$ ]] || max_lifetime=14400
  # 空闲 TTL 滑动窗口；同时受总生命周期硬上限约束。
  (( now_value > last_used + ttl )) && return 0
  (( now_value > created + max_lifetime ))
}

state_matches_host() {
  local file="$1" host="$2" user="$3" transport="$4" namespace="$5"
  [[ -f "$file" ]] || return 1
  [[ "$(state_get host "$file")" == "$host" ]] || return 1
  [[ "$(state_get user "$file")" == "$user" ]] || return 1
  [[ "$(state_get transport "$file")" == "$transport" ]] || return 1
  [[ "$(state_get namespace "$file")" == "$namespace" ]] || return 1
}

lock_acquire() {
  local name="$1" path deadline owner_file stamp lock_age
  init_dirs || return $?
  path="$(lock_path "$name")"
  deadline=$(( $(now) + ${TOM_AUTODEBUG_LOCK_TIMEOUT:-10} ))
  while ! mkdir "$path" 2>/dev/null; do
    owner_file="$path/owner"
    stamp="$(awk -F= '$1 == "created_at" {print $2; exit}' "$owner_file" 2>/dev/null || true)"
    lock_age=0
    [[ "$stamp" =~ ^[0-9]+$ ]] && lock_age=$(( $(now) - stamp ))
    if (( lock_age > ${TOM_AUTODEBUG_LOCK_STALE:-120} )); then
      warn "清理超时的本地锁: $name"
      rm -f "$owner_file" 2>/dev/null || true
      rmdir "$path" 2>/dev/null || true
      continue
    fi
    (( $(now) >= deadline )) && { fail "获取锁超时: $name" 6; return $?; }
    sleep 1
  done
  atomic_write "$path/owner" "pid=$$
created_at=$(now)
owner_token=$(random_token)" || {
    rmdir "$path" 2>/dev/null || true
    fail "写入锁 owner 失败: $name" 6
    return $?
  }
  printf '%s\n' "$path"
}

lock_release() {
  local path="$1"
  [[ -d "$path" ]] || return 0
  rm -f "$path/owner" 2>/dev/null || true
  rmdir "$path" 2>/dev/null || true
}

parse_positive_int() {
  [[ "${1:-}" =~ ^[0-9]+$ && "$1" -gt 0 ]] || { fail "$2 必须是正整数"; return 1; }
}

validate_transport() {
  case "${1:-ssh}" in
    ssh) return 0 ;;
    *) fail "当前只支持 transport=ssh；BNS/Matrix 等入口尚未实现，请人工处理" 2; return $? ;;
  esac
}

shell_quote() { printf '%q' "$1"; }

tmux_alive() {
  tmux has-session -t "$1" 2>/dev/null
}

tmux_capture() {
  tmux capture-pane -p -t "$1" -S "${2:--200}" 2>/dev/null || true
}

refresh_host_state() {
  local file="$1" host="$2" user="$3" transport="$4" namespace="$5" session="$6" pane="$7" owner="$8" created="$9" ttl="${10}"
  write_host_state "$file" "$host" "$user" "$transport" "$namespace" "$session" "$pane" "$owner" "$created" "$(now)" "$ttl"
}

cleanup_host_file() {
  local file="$1" pane session owner pane_owner
  [[ -f "$file" ]] || return 0
  pane="$(state_get pane "$file" 2>/dev/null || true)"
  session="$(state_get session "$file" 2>/dev/null || true)"
  owner="$(state_get owner_token "$file" 2>/dev/null || true)"
  if [[ -n "$pane" && -n "$owner" ]]; then
    pane_owner="$(tmux display-message -p -t "$pane" '#{@tom_autodebug_owner}' 2>/dev/null || true)"
    if [[ "$pane_owner" == "$owner" ]]; then
      tmux kill-pane -t "$pane" 2>/dev/null || true
    else
      warn "pane owner 不匹配，保留 pane 并移除 stale state: pane=$pane"
      rm -f "$file"
      return 0
    fi
  elif [[ -n "$session" ]]; then
    warn "没有可靠 pane owner，保留 session: $session"
    return 6
  fi
  rm -f "$file"
}

relay_auth_command() {
  local relay_user="$1" mode="$2" cmd
  if [[ "$mode" == top-level-user ]]; then
    cmd="$(shell_quote "$RELAY_BIN") -u $(shell_quote "$relay_user")"
  elif [[ "$mode" == login-subcommand ]]; then
    cmd="$(shell_quote "$RELAY_BIN") login -u $(shell_quote "$relay_user")"
  else
    fail "relay 不支持可识别的认证入口" 3
    return $?
  fi
  if printf '%s\n' "$RELAY_HELP" | grep -Eq '(^|[[:space:]])-t([=[:space:]]|$)'; then
    cmd="$cmd -t fp"
  fi
  printf '%s\n' "$cmd"
}

marker_is_complete_line() {
  local marker="$1"
  # LC_ALL=C：远端进程命令行可能含非法 UTF-8 字节（例如被截断的中文 system prompt），
  # 按当前 locale 解析会让 awk/sed 直接报 illegal byte sequence，marker 永远匹配不到，
  # 最终被误判成"命令超时"。按字节处理可避免这种假故障。
  LC_ALL=C awk -v wanted="$marker" '
    { sub(/\r$/, "") }
    $1 == wanted && (NF == 1 || (NF == 2 && $2 ~ /^[0-9]+$/)) { found=1 }
    END { exit(found ? 0 : 1) }
  '
}

# 快轮询后退避：前 FAST_WINDOW 秒用 FAST_INTERVAL，之后退避到 SLOW_INTERVAL。
poll_sleep() {
  local elapsed="$1"
  if (( elapsed < ${TOM_AUTODEBUG_FAST_WINDOW:-5} )); then
    sleep "${TOM_AUTODEBUG_FAST_INTERVAL:-0.2}"
  else
    sleep "${TOM_AUTODEBUG_SLOW_INTERVAL:-1}"
  fi
}

wait_for_marker() {
  local pane="$1" marker="$2" timeout="$3" start deadline screen
  start="$(now)"
  deadline=$(( start + timeout ))
  while (( $(now) < deadline )); do
    screen="$(tmux_capture "$pane")"
    if printf '%s\n' "$screen" | marker_is_complete_line "$marker"; then
      return 0
    fi
    if printf '%s\n' "$screen" | grep -Eqi 'permission denied|login.*failed|connection closed|authentication failed'; then
      fail "认证或远程连接失败，请查看 tmux pane: $pane" 5
      return $?
    fi
    poll_sleep "$(( $(now) - start ))"
  done
  fail "等待认证/远程 shell 超时（${timeout}s），请在 tmux pane 完成认证: $pane" 4
  return $?
}

# 去掉 ANSI 转义与 CR，使 pipe-pane 原始流可按行解析。
# 注意 CSI 中间字节必须写成 [ -/]（0x20-0x2F）。写成 [ -\/] 会被当成 space-to-backslash
# 区间（0x20-0x5C），把 ESC[K 后面的空格、*、: 一路吞掉并多吃一个字符，
# 造成 `ss`/`ps` 这类宽表格输出粘连丢字（例如 `*:8311sers:`）。
strip_terminal_control() {
  LC_ALL=C sed -E -e 's/\x1B\[[0-9;?]*[ -/]*[@-~]//g' -e 's/\x1B[]P^_][^\x07\x1B]*(\x07|\x1B\\)?//g' -e 's/\x1B[()][0-9A-B]//g' -e 's/\x1B[=><]//g' -e 's/\r$//' -e 's/\r/\n/g'
}

# 输出清洗是否因非法字节等原因失败；用于把"解析失败"和"远端命令超时"区分开。
stream_decode_ok() {
  local stream="$1"
  [[ -f "$stream" ]] || return 1
  strip_terminal_control < "$stream" > /dev/null 2>&1
}

# 从 pipe-pane 落盘的原始流里等待 end marker（不受 pane 宽高限制）。
wait_for_marker_file() {
  local stream="$1" marker="$2" timeout="$3" start deadline
  start="$(now)"
  deadline=$(( start + timeout ))
  while (( $(now) < deadline )); do
    if [[ -f "$stream" ]] && strip_terminal_control < "$stream" | marker_is_complete_line "$marker"; then
      return 0
    fi
    poll_sleep "$(( $(now) - start ))"
  done
  return 4
}

# 取 begin/end 之间的增量输出；命令回显行本身不等于 marker，故不会被计入。
capture_marked_file() {
  local stream="$1" begin="$2" end="$3" max_lines="${4:-200}" max_bytes="${5:-65536}"
  strip_terminal_control < "$stream" | LC_ALL=C awk -v begin="$begin" -v end="$end" -v max="$max_lines" '
    $0 == begin { active=1; count=0; next }
    $1 == end && $2 ~ /^[0-9]+$/ { active=0 }
    active && count < max { print; count++ }
  ' | head -c "$max_bytes"
}

marked_exit_code_file() {
  local stream="$1" end="$2"
  strip_terminal_control < "$stream" | LC_ALL=C awk -v marker="$end" '$1 == marker && $2 ~ /^[0-9]+$/ { code=$2 } END { print code }'
}

# 命令超时后 pane 可能停在续行提示符或前台进程未退出，发 C-c 让 shell 回到可用状态。
recover_pane() {
  local pane="$1" state_file="$2"
  tmux send-keys -t "$pane" C-c 2>/dev/null || return 1
  sleep "${TOM_AUTODEBUG_RECOVER_WAIT:-1}"
  tmux send-keys -t "$pane" C-c 2>/dev/null || true
  sleep "${TOM_AUTODEBUG_RECOVER_WAIT:-1}"
  health_probe "$state_file"
}

# relay 就绪的稳定信号是 pane 停在交互提示符上，且该提示符出现在 relay-cli 命令
# 回显之后。旧判定只认 `Last login:` / `successfully logined`，但这两行是后续 ssh
# 到目标机才打印的横幅；relay 自身的 shell（如 `-bash-baidu-ssl$`）两个关键词都不
# 出现，于是认证成功也会一路走到超时。要求提示符在回显之后，是为了排除 send-keys
# 尚未生效时本地 shell 自己的提示符。
relay_shell_ready() {
  printf '%s\n' "$1" | LC_ALL=C awk '
    /relay-cli/ { seen=1; line=""; next }
    seen && NF { line=$0 }
    END {
      sub(/\r$/, "", line)
      if (seen && line ~ /[$#>][ \t]*$/) exit 0
      exit 1
    }
  '
}

wait_for_relay_shell() {
  local pane="$1" timeout="$2" start deadline screen
  start="$(now)"
  deadline=$(( start + timeout ))
  while (( $(now) < deadline )); do
    screen="$(tmux_capture "$pane")"
    if relay_shell_ready "$screen"; then
      return 0
    fi
    if printf '%s\n' "$screen" | grep -Eqi 'permission denied|login.*failed|authentication failed'; then
      fail "relay 认证失败，请查看 tmux pane: $pane" 5
      return $?
    fi
    poll_sleep "$(( $(now) - start ))"
  done
  fail "等待 relay shell 超时（${timeout}s），请在 tmux pane 完成认证: $pane" 4
  return $?
}

discover_tmux_pane() {
  local session="$1" deadline pane
  deadline=$(( $(now) + ${TOM_AUTODEBUG_TMUX_READY_TIMEOUT:-5} ))
  while (( $(now) <= deadline )); do
    pane="$(tmux list-panes -t "$session" -F '#{pane_id}' 2>/dev/null | head -1)"
    if [[ -n "$pane" ]]; then
      printf '%s\n' "$pane"
      return 0
    fi
    sleep 1
  done
  return 1
}

health_probe() {
  local file="$1" pane owner nonce
  [[ -f "$file" ]] || return 1
  state_expired "$file" && return 1
  pane="$(state_get pane "$file" 2>/dev/null || true)"
  owner="$(state_get owner_token "$file" 2>/dev/null || true)"
  [[ -n "$pane" && -n "$owner" ]] || return 1
  [[ "$(tmux display-message -p -t "$pane" '#{@tom_autodebug_owner}' 2>/dev/null || true)" == "$owner" ]] || return 1
  nonce="__TOM_AUTODEBUG_HEALTH_$(random_token)__"
  tmux send-keys -t "$pane" "printf '%s\\n' '$nonce'" Enter 2>/dev/null || return 1
  wait_for_marker "$pane" "$nonce" "${TOM_AUTODEBUG_HEALTH_TIMEOUT:-10}" >/dev/null 2>&1
}

spawn_cleanup_watcher() {
  local file="$1" owner="$2" ttl="$3" deadline
  deadline=$(( $(now) + ttl + ${TOM_AUTODEBUG_GRACE:-30} ))
  (
    while (( $(now) < deadline )); do
      sleep "${TOM_AUTODEBUG_WATCH_INTERVAL:-15}"
    done
    if [[ -f "$file" ]] && [[ "$(state_get owner_token "$file" 2>/dev/null || true)" == "$owner" ]] && state_expired "$file"; then
      cleanup_host_file "$file" >/dev/null 2>&1 || true
    fi
  ) >/dev/null 2>&1 &
}

cmd_connect() {
  local host_input="${1:-}" user="" ttl="$DEFAULT_TTL" session="" namespace="tom-autodebug" transport="ssh" relay_user="${TOM_AUTODEBUG_RELAY_USER:-}" arg new_pane=0 defaults_ready=0
  shift || true
  [[ -n "$host_input" ]] || { usage >&2; return 2; }
  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --user) [[ $# -ge 2 ]] || { fail "--user 缺少值"; return 2; }; user="$2"; shift 2 ;;
      --ttl) [[ $# -ge 2 ]] || { fail "--ttl 缺少值"; return 2; }; ttl="$2"; shift 2 ;;
      --session) [[ $# -ge 2 ]] || { fail "--session 缺少值"; return 2; }; session="$2"; shift 2 ;;
      --namespace) [[ $# -ge 2 ]] || { fail "--namespace 缺少值"; return 2; }; namespace="$2"; shift 2 ;;
      --transport) [[ $# -ge 2 ]] || { fail "--transport 缺少值"; return 2; }; transport="$2"; shift 2 ;;
      --relay-user) [[ $# -ge 2 ]] || { fail "--relay-user 缺少值"; return 2; }; relay_user="$2"; shift 2 ;;
      --new-pane) new_pane=1; shift ;;
      *) fail "connect 未知参数: $arg"; return $? ;;
    esac
  done
  parse_positive_int "$ttl" --ttl || return $?
  valid_token "$namespace" || { fail "namespace 包含不支持的字符"; return 2; }
  validate_transport "$transport" || return $?
  local parts full host_file lock_path_value created owner mode pane marker auth_cmd ssh_cmd session_target
  [[ -n "$relay_user" ]] || relay_user="$(default_get relay_user 2>/dev/null || true)"
  [[ -n "$relay_user" ]] || {
    fail "缺少默认 relay 用户。请先输入 relay_user=你的relay用户名, ssh_user=目标机用户名，然后执行 defaults set" 7
    return $?
  }
  valid_token "$relay_user" || { fail "relay user 包含不支持的字符"; return 2; }
  parts="$(parse_host "$host_input" "$user")" || return $?
  user="$(printf '%s\n' "$parts" | sed -n '1p')"
  full="$(printf '%s\n' "$parts" | sed -n '3p')"
  if [[ -n "$(default_get relay_user 2>/dev/null || true)" && -n "$(default_get ssh_user 2>/dev/null || true)" ]]; then
    defaults_ready=1
  fi
  if [[ "$defaults_ready" -eq 0 ]]; then
    write_defaults "$relay_user" "$user" || return $?
    log "已记录首次输入的默认账号，后续连接可省略 --relay-user 和 --user"
  fi
  require_cmd tmux || return $?
  host_file="$(host_state_path "$full")"
  [[ -n "$session" ]] || session="autodebug-$(safe_key "$full")"
  session="$(safe_tmux_name "$session")"
  valid_token "$session" || { fail "session 包含不支持的字符"; return 2; }
  init_dirs || return $?
  resolve_relay || return $?
  relay_help >/dev/null || return $?
  mode="$(relay_login_mode)"
  [[ "$mode" != unsupported ]] || { fail "relay 版本不支持认证参数" 3; return $?; }

  lock_path_value="$(lock_acquire "$full")" || return $?
  if state_matches_host "$host_file" "$full" "$user" "$transport" "$namespace" && ! state_expired "$host_file"; then
    pane="$(state_get pane "$host_file")"
    if [[ -n "$pane" ]] && tmux_alive "$(state_get session "$host_file")" && tmux list-panes -a -F '#{pane_id}' 2>/dev/null | grep -qxF "$pane" && health_probe "$host_file"; then
      refresh_host_state "$host_file" "$full" "$user" "$transport" "$namespace" "$(state_get session "$host_file")" "$pane" "$(state_get owner_token "$host_file")" "$(state_get created_at "$host_file")" "$ttl"
      lock_release "$lock_path_value"
      log "复用健康连接 pane=$pane host=$full ttl=${ttl}s"
      return 0
    fi
    warn "已有 state 但 pane/health/owner 校验失败，将只清理受管连接并重建"
  fi
  if [[ -f "$host_file" ]]; then
    if ! cleanup_host_file "$host_file" >/dev/null 2>&1 && [[ "$new_pane" -eq 0 ]]; then
      session="${session}-$(random_token | cut -c1-8)"
    fi
  fi

  if tmux_alive "$session"; then
    if [[ "$new_pane" -eq 1 ]]; then
      pane="$(tmux split-window -d -t "$session" -P -F '#{pane_id}' 2>/dev/null || true)"
    else
      session="${session}-$(random_token | cut -c1-8)"
      tmux new-session -d -s "$session" -x "$PANE_COLS" -y "$PANE_ROWS" || { lock_release "$lock_path_value"; fail "创建独立 tmux session 失败: $session" 6; return $?; }
      pane="$(discover_tmux_pane "$session" || true)"
    fi
  else
    tmux new-session -d -s "$session" -x "$PANE_COLS" -y "$PANE_ROWS" || { lock_release "$lock_path_value"; fail "创建 tmux session 失败: $session" 6; return $?; }
    pane="$(discover_tmux_pane "$session" || true)"
  fi
  [[ -n "$pane" ]] || { lock_release "$lock_path_value"; fail "无法探测 tmux pane" 6; return $?; }
  owner="$(random_token)"
  created="$(now)"
  tmux set-option -p -t "$pane" "@tom_autodebug_owner" "$owner" 2>/dev/null || {
    lock_release "$lock_path_value"; fail "记录 tmux pane owner 失败" 6; return $?;
  }
  write_host_state "$host_file" "$full" "$user" "$transport" "$namespace" "$session" "$pane" "$owner" "$created" "$created" "$ttl" auth_pending || {
    lock_release "$lock_path_value"; fail "写入 host state 失败" 6; return $?;
  }

  marker="__TOM_AUTODEBUG_CONNECTED_${owner}__"
  auth_cmd="$(relay_auth_command "$relay_user" "$mode")" || { lock_release "$lock_path_value"; return $?; }
  ssh_cmd="ssh -tt -o ConnectTimeout=15 $(shell_quote "$full") $(shell_quote "printf '%s\\n' '$marker'; exec bash -l")"
  session_target="$pane"
  tmux send-keys -t "$session_target" "$auth_cmd" Enter || {
    lock_release "$lock_path_value"; fail "向 tmux pane 发送 relay 认证命令失败" 6; return $?;
  }
  log "已启动 relay_user=$relay_user 的交互认证，等待最多 ${CONNECT_TIMEOUT}s"
  wait_for_relay_shell "$pane" "$CONNECT_TIMEOUT"
  local relay_rc=$?
  if [[ "$relay_rc" -ne 0 ]]; then
    lock_release "$lock_path_value"
    return "$relay_rc"
  fi
  tmux send-keys -t "$session_target" "$ssh_cmd" Enter || {
    lock_release "$lock_path_value"; fail "从 relay shell 下发目标 SSH 命令失败" 6; return $?;
  }
  log "relay 认证完成，正在连接 $full"
  if wait_for_marker "$pane" "$marker" "$CONNECT_TIMEOUT"; then
    refresh_host_state "$host_file" "$full" "$user" "$transport" "$namespace" "$session" "$pane" "$owner" "$created" "$ttl"
    spawn_cleanup_watcher "$host_file" "$owner" "$ttl"
    lock_release "$lock_path_value"
    log "连接就绪: host=$full pane=$pane ttl=${ttl}s"
    return 0
  else
    local connect_rc=$?
    lock_release "$lock_path_value"
    return "$connect_rc"
  fi
}

# 去掉引号内的字面量内容再做 shell 语法判断，避免 echo "a -> b" 这类字符串里的
# `->`、`|`、`>` 被误判成重定向。关键字匹配仍用原始串，防止 "$(rm -rf x)" 被藏起来。
strip_quoted_literals() {
  printf '%s' "$1" | sed -E -e "s/'[^']*'/''/g" -e 's/"[^"]*"/""/g'
}

# 高危：删除、杀进程、提权、重启、内核/网络配置变更、覆盖写。
danger_high() {
  printf '%s\n' "$1" | grep -Eiq '(^|[;&|[:space:]])(sudo|reboot|shutdown|poweroff|halt|kill|pkill|killall|rm[[:space:]]|mv[[:space:]]|truncate[[:space:]]|chmod[[:space:]]|chown[[:space:]]|mkfs[^[:space:]]*|swapoff|sysctl[[:space:]]+-w|modprobe|insmod|rmmod|ethtool[[:space:]]+-[KGCLA]|systemctl[[:space:]]+(start|stop|restart|reload|enable|disable)|service[[:space:]]+[^ ]+[[:space:]]+(start|stop|restart|reload)|env_start|bgw_ctl|iptables|ip6tables|nft|route[[:space:]]+(add|del)|ip[[:space:]]+(-[46][[:space:]]+)?(route|addr|link|neigh)[[:space:]]+(add|del|change|replace|set)|dd[[:space:]])'
}

# 低危但仍有副作用：拷贝/建链接、写文件、抓包、后台驻留。
danger_low() {
  local sanitized
  printf '%s\n' "$1" | grep -Eiq '(^|[;&|[:space:]])(cp[[:space:]]|ln[[:space:]]|tee[[:space:]]|nohup|tcpdump|tail[[:space:]]+-f)' && return 0
  # 第二个参数为 keywords-only 时跳过重定向启发式：不解析语法就无法把 `>` 与 awk/shell
  # 的比较运算（如 `if (a > b)`）区分开，对 skill 自带模板只按动作关键字判定，
  # 否则纯只读的审计脚本会被误判成写文件。
  [[ "${2:-}" == keywords-only ]] && return 1
  # 写文件重定向；/dev/null 等丢弃目标和 fd 复制不算。
  sanitized="$(strip_quoted_literals "$1" | sed -E -e 's#[0-9]?>>?[[:space:]]*/dev/(null|stderr|stdout)##g' -e 's/[0-9]?>&[0-9-]//g')"
  # `>` 必须位于行首或空白之后，且目标首字符不是数字，才算重定向。
  # 否则 awk/shell 里的比较表达式（i>11、x >= 3）会被误判成写文件。
  printf '%s\n' "$sanitized" | grep -Eq '(^|[[:space:]])[0-9]?>>?[[:space:]]*[^[:space:]&|<>0-9=][^[:space:]&|]*'
}

# 打印 high / low / none
danger_level() {
  if danger_high "$1"; then
    printf 'high\n'
  elif danger_low "$1" "${2:-}"; then
    printf 'low\n'
  else
    printf 'none\n'
  fi
}


redact_text() {
  LC_ALL=C sed -E \
    -e 's/((password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|cookie|authorization|bearer|RELAY_PASSWD)[=: ]+)[^ ]+/\1<redacted>/Ig' \
    -e 's/-----BEGIN [A-Z ]*PRIVATE KEY-----/<redacted-private-key>/g' \
    -e 's/^[A-Za-z0-9+\/]{60,}={0,2}$/<redacted-blob>/'
}

record_evidence() {
  local case_id="$1" role="$2" host="$3" request_id="$4" command="$5" status="$6" started="$7" ended="$8" output="$9" file max_bytes size
  init_dirs || return $?
  file="$STATE_ROOT/logs/$(safe_key "${case_id:-single}-${host}").log"
  max_bytes="${TOM_AUTODEBUG_LOG_MAX_BYTES:-5242880}"
  if [[ -f "$file" ]]; then
    size="$(wc -c < "$file" 2>/dev/null | tr -d ' ')"
    if [[ "$size" =~ ^[0-9]+$ ]] && (( size > max_bytes )); then
      mv -f "$file" "$file.1" 2>/dev/null || true
      chmod 600 "$file.1" 2>/dev/null || true
    fi
  fi
  {
    printf 'request_id=%s case=%s role=%s host=%s started_at=%s ended_at=%s status=%s\n' "$request_id" "${case_id:-single}" "$role" "$host" "$started" "$ended" "$status"
    printf 'command=%s\n' "$(printf '%s' "$command" | redact_text)"
    printf 'output_begin\n%s\noutput_end\n' "$(printf '%s' "$output" | redact_text)"
  } >> "$file"
  chmod 600 "$file" 2>/dev/null || true
}

resolve_case_role() {
  local case_id="$1" role="$2" file="$3" targets item item_role target
  targets="$(state_get targets "$file" 2>/dev/null || true)"
  while IFS= read -r item; do
    [[ -n "$item" ]] || continue
    item_role="${item%%=*}"
    target="${item#*=}"
    if [[ "$item_role" == "$role" ]]; then
      printf '%s\n' "$target"
      return 0
    fi
  done < <(printf '%s\n' "$targets" | tr ',' '\n')
  fail "case 中不存在 role=$role: $case_id" 6
  return $?
}

case_target_status() {
  local target="$1" parts full state_file phase pane owner
  parts="$(parse_host "$target" 2>/dev/null)" || { printf 'invalid_target\n'; return 0; }
  full="$(printf '%s\n' "$parts" | sed -n '3p')"
  state_file="$(host_state_path "$full")"
  [[ -f "$state_file" ]] || { printf 'missing\n'; return 0; }
  phase="$(state_get phase "$state_file" 2>/dev/null || true)"
  [[ "$phase" == connected ]] || { printf '%s\n' "${phase:-invalid_state}"; return 0; }
  state_expired "$state_file" && { printf 'expired\n'; return 0; }
  pane="$(state_get pane "$state_file" 2>/dev/null || true)"
  owner="$(state_get owner_token "$state_file" 2>/dev/null || true)"
  if [[ -z "$pane" ]] ||
     ! tmux_alive "$(state_get session "$state_file" 2>/dev/null || true)" ||
     [[ "$(tmux display-message -p -t "$pane" '#{@tom_autodebug_owner}' 2>/dev/null || true)" != "$owner" ]]; then
    printf 'stale\n'
    return 0
  fi
  printf 'ready\n'
}

authorize_command() {
  local check_src="$1" confirm="$2" danger_mode="${3:-}" level
  level="$(danger_level "$check_src" "$danger_mode")"
  case "$level" in
    high)
      if [[ "$confirm" != --confirm-dangerous ]]; then
        fail "检测到高危命令（删除/杀进程/提权/网络配置变更）；确认后使用 --confirm-dangerous: $check_src" 2
        return $?
      fi
      ;;
    low)
      if [[ "$confirm" != --confirm-dangerous && "$confirm" != --confirm-write ]]; then
        fail "检测到有副作用的命令（写文件/拷贝/抓包/后台驻留）；确认后使用 --confirm-write（或 --confirm-dangerous）: $check_src" 2
        return $?
      fi
      ;;
  esac
}

exec_on_state() {
  local state_file="$1" case_id="$2" role="$3" command="$4" timeout="$5" confirm="$6" check_src="${7:-$4}" danger_mode="${8:-}"
  local pane host owner request begin end start end_time output rc lock_path_value stream
  [[ -f "$state_file" ]] || { fail "连接状态不存在，请先 connect" 6; return $?; }
  host="$(state_get host "$state_file" 2>/dev/null || true)"
  pane="$(state_get pane "$state_file" 2>/dev/null || true)"
  owner="$(state_get owner_token "$state_file" 2>/dev/null || true)"
  [[ "$(state_get phase "$state_file" 2>/dev/null || true)" == connected ]] || { fail "连接尚未完成认证: $host" 5; return $?; }
  state_expired "$state_file" && { cleanup_host_file "$state_file" >/dev/null 2>&1 || true; fail "连接 TTL 已到期: $host" 4; return $?; }
  [[ "$(tmux display-message -p -t "$pane" '#{@tom_autodebug_owner}' 2>/dev/null || true)" == "$owner" ]] || { fail "tmux pane owner 不匹配: $host" 6; return $?; }
  health_probe "$state_file" || { fail "远端 shell 健康检查失败: $host" 5; return $?; }
  authorize_command "$check_src" "$confirm" "$danger_mode" || return $?
  lock_path_value="$(lock_acquire "${case_id:-single}-$host")" || return $?
  request="$(random_token)"
  begin="__TOM_AUTODEBUG_BEGIN_${request}__"
  end="__TOM_AUTODEBUG_END_${request}__"
  start="$(now)"
  stream="$STATE_ROOT/logs/.stream-$(safe_key "$host")-$request"
  : > "$stream" 2>/dev/null || { lock_release "$lock_path_value"; fail "无法创建输出流文件: $stream" 6; return $?; }
  chmod 600 "$stream" 2>/dev/null || true
  # pipe-pane 抓取 pane 原始输出流，不受 pane 宽高和 history-limit 限制。
  if ! tmux pipe-pane -o -t "$pane" "cat >> $(shell_quote "$stream")" 2>/dev/null; then
    rm -f "$stream"
    lock_release "$lock_path_value"
    fail "启动 tmux pipe-pane 失败: $host" 6
    return $?
  fi
  if ! tmux send-keys -t "$pane" "printf '%s\\n' '$begin'; { $command; rc=\$?; }; printf '%s %s\\n' '$end' \"\$rc\"" Enter; then
    tmux pipe-pane -t "$pane" 2>/dev/null || true
    rm -f "$stream"
    lock_release "$lock_path_value"
    record_evidence "$case_id" "$role" "$host" "$request" "$command" send_failed "$start" "$(now)" ""
    fail "向远端 pane 下发命令失败: $host" 6
    return $?
  fi
  if wait_for_marker_file "$stream" "$end" "$timeout"; then
    end_time="$(now)"
    tmux pipe-pane -t "$pane" 2>/dev/null || true
    rc="$(marked_exit_code_file "$stream" "$end")"
    [[ "$rc" =~ ^[0-9]+$ ]] || rc=0
    output="$(capture_marked_file "$stream" "$begin" "$end" "${TOM_AUTODEBUG_MAX_LINES:-2000}" "${TOM_AUTODEBUG_MAX_BYTES:-1048576}")"
    rm -f "$stream"
    write_host_state "$state_file" "$(state_get host "$state_file")" "$(state_get user "$state_file")" "$(state_get transport "$state_file")" "$(state_get namespace "$state_file")" "$(state_get session "$state_file")" "$pane" "$owner" "$(state_get created_at "$state_file")" "$end_time" "$(state_get ttl_seconds "$state_file")" connected
    lock_release "$lock_path_value"
    record_evidence "$case_id" "$role" "$host" "$request" "$command" "$rc" "$start" "$end_time" "$output"
    printf '%s\n' "$output"
    return "$rc"
  fi
  end_time="$(now)"
  tmux pipe-pane -t "$pane" 2>/dev/null || true
  # 区分失败类型：输出解码失败不代表远端命令卡住，此时发送 C-c 会打断一条本来正常的查询。
  if ! stream_decode_ok "$stream"; then
    output="$(LC_ALL=C tr -c '[:print:][:space:]' '?' < "$stream" 2>/dev/null | tail -n 40)"
    rm -f "$stream"
    record_evidence "$case_id" "$role" "$host" "$request" "$command" output_decode_error "$start" "$end_time" "$output"
    lock_release "$lock_path_value"
    fail "输出解析失败（非法字节或流不可解码），未打断远端命令；请缩小输出或改用 LC_ALL=C/二进制安全命令: host=$host request=$request" 7
    return $?
  fi
  output="$(strip_terminal_control < "$stream" | tail -n 40)"
  if printf '%s\n' "$output" | LC_ALL=C grep -Fq "$end"; then
    rm -f "$stream"
    record_evidence "$case_id" "$role" "$host" "$request" "$command" marker_incomplete "$start" "$end_time" "$output"
    lock_release "$lock_path_value"
    fail "已看到结束 marker 但缺少退出码，输出可能被截断，未打断远端命令: host=$host request=$request" 7
    return $?
  fi
  rm -f "$stream"
  record_evidence "$case_id" "$role" "$host" "$request" "$command" timeout "$start" "$end_time" "$output"
  if recover_pane "$pane" "$state_file"; then
    lock_release "$lock_path_value"
    fail "命令超时（${timeout}s），已发送 C-c 并确认 pane 恢复可用: host=$host request=$request" 4
    return $?
  fi
  lock_release "$lock_path_value"
  fail "命令超时（${timeout}s）且 C-c 后 pane 仍不可用，请 cleanup 后重建: host=$host request=$request" 5
  return $?
}

cmd_exec() {
  local host_input="" case_id="" role="" timeout="$COMMAND_TIMEOUT" confirm="" arg command state_file parts full
  local from_stdin=0 script b64 check_src reconnect=0
  [[ $# -gt 0 ]] || { usage >&2; return 2; }
  if [[ "${1:-}" == --case ]]; then
    [[ $# -ge 4 ]] || { fail "case exec 需要 --case CASE --role ROLE -- COMMAND"; return 2; }
    case_id="$2"; shift 2
    [[ "${1:-}" == --role && $# -ge 2 ]] || { fail "case exec 需要 --role ROLE"; return 2; }
    role="$2"; shift 2
  else
    host_input="$1"; shift
  fi
  while [[ $# -gt 0 && "$1" != -- ]]; do
    arg="$1"
    case "$arg" in
      --timeout) [[ $# -ge 2 ]] || { fail "--timeout 缺少值"; return 2; }; timeout="$2"; shift 2 ;;
      --confirm-dangerous|--confirm-write) confirm="$arg"; shift ;;
      --stdin) from_stdin=1; shift ;;
      --reconnect-on-stale) reconnect=1; shift ;;
      *) fail "exec 未知参数: $arg（提示：<host> 必须紧跟 exec，flag 在 host 之后、-- 之前；见 exec --help）"; return 2 ;;
    esac
  done
  if [[ "$from_stdin" -eq 1 ]]; then
    [[ "${1:-}" != -- ]] || { fail "--stdin 与 -- COMMAND 互斥"; return 2; }
    script="$(cat)"
    [[ -n "$script" ]] || { fail "--stdin 未读到脚本内容"; return 2; }
    if (( ${#script} > ${TOM_AUTODEBUG_MAX_STDIN_BYTES:-16384} )); then
      fail "--stdin 脚本过大（${#script} 字节），超过 send-keys 安全上限" 2
      return $?
    fi
    b64="$(printf '%s' "$script" | base64 | tr -d '\n')"
    # base64 传输可彻底避开嵌套引号转义；危险等级仍按原始脚本判定。
    command="printf '%s' $b64 | base64 -d | bash"
    check_src="$script"
  else
    [[ "${1:-}" == -- ]] || { fail "exec 命令前必须使用 --（或用 --stdin 从标准输入读脚本）"; return 2; }
    shift
    [[ $# -gt 0 ]] || { fail "exec 缺少远端命令"; return 2; }
    command="$*"
    check_src="$command"
  fi
  parse_positive_int "$timeout" --timeout || return $?
  if [[ -n "$case_id" ]]; then
    [[ -f "$(case_state_path "$case_id")" ]] || { fail "case 不存在: $case_id"; return 6; }
    full="$(resolve_case_role "$case_id" "$role" "$(case_state_path "$case_id")")" || return $?
    parts="$(parse_host "$full")" || return $?
    full="$(printf '%s\n' "$parts" | sed -n '3p')"
    state_file="$(host_state_path "$full")"
  else
    resolve_state_file "$host_input" "$reconnect" || return $?
    state_file="$RESOLVED_STATE_FILE"
  fi
  exec_on_state "$state_file" "$case_id" "$role" "$command" "$timeout" "$confirm" "$check_src"
}

cmd_capture() {
  local host_input="${1:-}" lines="${TOM_AUTODEBUG_MAX_LINES:-200}" arg parts full file pane owner
  shift || true
  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --lines) [[ $# -ge 2 ]] || { fail "--lines 缺少值"; return 2; }; lines="$2"; shift 2 ;;
      *) fail "capture 未知参数: $arg"; return 2 ;;
    esac
  done
  parse_positive_int "$lines" --lines || return $?
  parts="$(parse_host "$host_input")" || return $?
  full="$(printf '%s\n' "$parts" | sed -n '3p')"
  file="$(host_state_path "$full")"
  [[ -f "$file" ]] || { fail "连接状态不存在: $full"; return 6; }
  pane="$(state_get pane "$file")"; owner="$(state_get owner_token "$file")"
  [[ "$(tmux display-message -p -t "$pane" '#{@tom_autodebug_owner}' 2>/dev/null || true)" == "$owner" ]] || { fail "tmux pane owner 不匹配: $full"; return 6; }
  tmux_capture "$pane" "-$((lines))"
}

cmd_status() {
  local case_id="" arg file host status targets item role target
  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --case) [[ $# -ge 2 ]] || { fail "--case 缺少值"; return 2; }; case_id="$2"; shift 2 ;;
      *) fail "status 未知参数: $arg"; return $? ;;
    esac
  done
  init_dirs || return $?
  if [[ -n "$case_id" ]]; then
    file="$(case_state_path "$case_id")"
    [[ -f "$file" ]] || { fail "case 不存在: $case_id"; return 6; }
    cat "$file"
    targets="$(state_get targets "$file" 2>/dev/null || true)"
    while IFS= read -r item; do
      [[ -n "$item" ]] || continue
      role="${item%%=*}"
      target="${item#*=}"
      printf 'role=%s target=%s status=%s\n' "$role" "$target" "$(case_target_status "$target")"
    done < <(printf '%s\n' "$targets" | tr ',' '\n')
    return 0
  fi
  shopt -s nullglob
  for file in "$STATE_ROOT"/sessions/*.env; do
    host="$(state_get host "$file" 2>/dev/null || true)"
    status=healthy
    if state_expired "$file"; then
      status=expired
    elif [[ "$(state_get phase "$file" 2>/dev/null || true)" == auth_pending ]]; then
      status=auth_pending
    elif ! tmux_alive "$(state_get session "$file" 2>/dev/null || true)" || [[ "$(tmux display-message -p -t "$(state_get pane "$file" 2>/dev/null || true)" '#{@tom_autodebug_owner}' 2>/dev/null || true)" != "$(state_get owner_token "$file" 2>/dev/null || true)" ]]; then
      status=stale
    fi
    printf 'host=%s status=%s session=%s pane=%s\n' "$host" "$status" "$(state_get session "$file")" "$(state_get pane "$file")"
  done
  shopt -u nullglob
}

cmd_cleanup() {
  local host_input="" case_id="" all=0 arg file
  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --host) [[ $# -ge 2 ]] || { fail "--host 缺少值"; return 2; }; host_input="$2"; shift 2 ;;
      --case) [[ $# -ge 2 ]] || { fail "--case 缺少值"; return 2; }; case_id="$2"; shift 2 ;;
      --all) all=1; shift ;;
      *) fail "cleanup 未知参数: $arg"; return $? ;;
    esac
  done
  init_dirs || return $?
  if [[ -n "$host_input" ]]; then
    local parts full
    parts="$(parse_host "$host_input")" || return $?
    full="$(printf '%s\n' "$parts" | sed -n '3p')"
    cleanup_host_file "$(host_state_path "$full")"
    local cleanup_rc=$?
    if [[ "$cleanup_rc" -eq 0 ]]; then
      log "已清理 host: $full"
    fi
    return "$cleanup_rc"
  fi
  if [[ -n "$case_id" ]]; then
    file="$(case_state_path "$case_id")"
    [[ -f "$file" ]] || { fail "case 不存在: $case_id"; return 6; }
    local targets role target_host cleanup_failed=0
    targets="$(state_get targets "$file" 2>/dev/null || true)"
    while IFS= read -r role; do
      [[ -n "$role" ]] || continue
      target_host="${role#*=}"
      local target_parts target_full
      target_parts="$(parse_host "$target_host")" || { cleanup_failed=1; continue; }
      target_full="$(printf '%s\n' "$target_parts" | sed -n '3p')"
      cleanup_host_file "$(host_state_path "$target_full")" || cleanup_failed=1
    done < <(printf '%s\n' "$targets" | tr ',' '\n')
    if [[ "$cleanup_failed" -ne 0 ]]; then
      warn "case 中存在无法安全清理的资源，保留 manifest: $case_id"
      return 6
    fi
    rm -f "$file"
    log "已清理 case: $case_id"
    return 0
  fi
  [[ "$all" -eq 1 ]] || { fail "cleanup 需要 --host、--case 或 --all"; return 2; }
  local cleanup_failed=0
  shopt -s nullglob
  for file in "$STATE_ROOT"/sessions/*.env; do
    cleanup_host_file "$file" || cleanup_failed=1
  done
  for file in "$STATE_ROOT"/cases/*.env; do
    [[ "$cleanup_failed" -eq 0 ]] && rm -f "$file"
  done
  shopt -u nullglob
  if [[ "$cleanup_failed" -ne 0 ]]; then
    warn "部分资源无法安全清理，保留相关状态文件"
    return 6
  fi
  rm -f "$STATE_ROOT"/logs/*.log "$STATE_ROOT"/logs/*.log.1 "$STATE_ROOT"/logs/.stream-* 2>/dev/null || true
  log "已清理全部 tom-autodebug 受管状态和取证日志"
}

cmd_case_create() {
  local case_id="${1:-}" ttl="$DEFAULT_TTL" arg parsed role user host full targets="" count=0 file owner session
  shift || true
  [[ -n "$case_id" ]] || { fail "case-create 缺少 CASE_ID"; return 2; }
  valid_token "$case_id" || { fail "CASE_ID 包含不支持的字符"; return 2; }
  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --target)
        [[ $# -ge 2 ]] || { fail "--target 缺少 ROLE=[USER@]HOST"; return 2; }
        parsed="$(parse_target "$2")" || return $?
        role="$(printf '%s\n' "$parsed" | sed -n '1p')"
        user="$(printf '%s\n' "$parsed" | sed -n '2p')"
        host="$(printf '%s\n' "$parsed" | sed -n '3p')"
        full="$(printf '%s\n' "$parsed" | sed -n '4p')"
        printf '%s' "$targets" | tr ',' '\n' | grep -q "^${role}=" && { fail "case 中 role 重复: $role"; return 2; }
        count=$((count + 1))
        (( count <= MAX_TARGETS )) || { fail "case 目标数超过上限 $MAX_TARGETS"; return 2; }
        [[ -z "$targets" ]] || targets="$targets,"
        targets="${targets}${role}=${full}"
        shift 2
        ;;
      --ttl) [[ $# -ge 2 ]] || { fail "--ttl 缺少值"; return 2; }; ttl="$2"; shift 2 ;;
      *) fail "case-create 未知参数: $arg"; return 2 ;;
    esac
  done
  parse_positive_int "$ttl" --ttl || return $?
  (( count > 0 )) || { fail "case 至少需要一个 --target"; return 2; }
  init_dirs || return $?
  file="$(case_state_path "$case_id")"
  [[ ! -f "$file" ]] || { fail "case 已存在: $case_id"; return 6; }
  session="$(safe_tmux_name "autodebug-$(safe_key "$case_id")")"
  owner="$(random_token)"
  write_case_state "$file" "$case_id" "$session" "$owner" "$(now)" "$ttl" "$targets" || {
    fail "写入 case manifest 失败: $case_id" 6
    return $?
  }
  log "case 已创建: $case_id targets=$count ttl=${ttl}s session=$session"
}

cmd_connect_many() {
  local case_id="${1:-}" file targets item role target ttl session placeholder created_session=0 tmp overall=0 i
  local expected processed=0 ready=0 status failed_roles=""
  [[ -n "$case_id" && $# -eq 1 ]] || { fail "用法: connect-many CASE_ID"; return 2; }
  init_dirs || return $?
  require_cmd tmux || return $?
  file="$(case_state_path "$case_id")"
  [[ -f "$file" ]] || { fail "case 不存在: $case_id"; return 6; }
  state_expired "$file" && { fail "case TTL 已到期，请重新创建: $case_id" 4; return $?; }
  targets="$(state_get targets "$file" 2>/dev/null || true)"
  expected="$(state_get target_count "$file" 2>/dev/null || true)"
  [[ "$expected" =~ ^[0-9]+$ ]] || expected=0
  ttl="$(state_get ttl_seconds "$file")"
  session="$(state_get session "$file")"

  if ! tmux_alive "$session"; then
    tmux new-session -d -s "$session" -x "$PANE_COLS" -y "$PANE_ROWS" || { fail "创建 case tmux session 失败: $session" 6; return $?; }
    placeholder="$(tmux list-panes -t "$session" -F '#{pane_id}' 2>/dev/null | head -1)"
    created_session=1
  fi
  # 串行连接：relay 首次认证后凭据被缓存，后续角色可复用，避免并发触发 N 次指纹/扫码。
  local -a roles=() targets_seen=() files=()
  while IFS= read -r item; do
    [[ -n "$item" ]] || continue
    role="${item%%=*}"
    target="${item#*=}"
    processed=$((processed + 1))
    tmp="$(mktemp "$STATE_ROOT/.connect-many.XXXXXX")" || return 6
    if ! cmd_connect "$target" --session "$session" --ttl "$ttl" --new-pane > "$tmp" 2>&1; then
      overall=1
      warn "role=$role 连接失败"
    fi
    roles+=("$role"); targets_seen+=("$target"); files+=("$tmp")
  done < <(printf '%s\n' "$targets" | tr ',' '\n')
  for ((i=0; i<${#files[@]}; i++)); do
    printf '=== connect role=%s ===\n' "${roles[$i]}"
    cat "${files[$i]}"
    rm -f "${files[$i]}"
    status="$(case_target_status "${targets_seen[$i]}")"
    printf 'role=%s target=%s status=%s\n' "${roles[$i]}" "${targets_seen[$i]}" "$status"
    if [[ "$status" == ready ]]; then
      ready=$((ready + 1))
    else
      overall=1
      [[ -z "$failed_roles" ]] || failed_roles="$failed_roles,"
      failed_roles="${failed_roles}${roles[$i]}"
    fi
  done
  if [[ "$processed" -ne "$expected" ]]; then
    overall=1
    warn "case target 数量不一致: expected=$expected processed=$processed"
  fi
  printf 'connect_summary expected=%s processed=%s ready=%s failed_roles=%s\n' \
    "$expected" "$processed" "$ready" "${failed_roles:-none}"
  if [[ "$created_session" -eq 1 && -n "$placeholder" ]]; then
    tmux kill-pane -t "$placeholder" 2>/dev/null || true
    tmux select-layout -t "$session" tiled 2>/dev/null || true
  fi
  return "$overall"
}

cmd_exec_all() {
  local case_id="" command="" arg file targets item role tmp overall=0 pid i
  local timeout="$COMMAND_TIMEOUT" confirm="" from_stdin=0 script expected processed=0 succeeded=0
  [[ $# -gt 0 ]] || { fail "exec-all 缺少参数"; return 2; }
  while [[ $# -gt 0 && "$1" != -- ]]; do
    arg="$1"
    case "$arg" in
      --case) [[ $# -ge 2 ]] || { fail "--case 缺少值"; return 2; }; case_id="$2"; shift 2 ;;
      --timeout) [[ $# -ge 2 ]] || { fail "--timeout 缺少值"; return 2; }; timeout="$2"; shift 2 ;;
      --confirm-dangerous|--confirm-write) confirm="$arg"; shift ;;
      --stdin) from_stdin=1; shift ;;
      *) fail "exec-all 未知参数: $arg"; return 2 ;;
    esac
  done
  [[ -n "$case_id" ]] || { fail "exec-all 需要 --case CASE_ID"; return 2; }
  parse_positive_int "$timeout" --timeout || return $?
  if [[ "$from_stdin" -eq 1 ]]; then
    [[ "${1:-}" != -- ]] || { fail "--stdin 与 -- COMMAND 互斥"; return 2; }
    script="$(cat)"
    [[ -n "$script" ]] || { fail "--stdin 未读到脚本内容"; return 2; }
    if (( ${#script} > ${TOM_AUTODEBUG_MAX_STDIN_BYTES:-16384} )); then
      fail "--stdin 脚本过大（${#script} 字节），超过 send-keys 安全上限" 2
      return $?
    fi
  else
    [[ "${1:-}" == -- ]] || { fail "exec-all 命令前必须使用 --（或用 --stdin）"; return 2; }
    shift
    [[ $# -gt 0 ]] || { fail "exec-all 缺少远端命令"; return 2; }
    command="$*"
  fi
  init_dirs || return $?
  file="$(case_state_path "$case_id")"
  [[ -f "$file" ]] || { fail "case 不存在: $case_id"; return 6; }
  state_expired "$file" && { fail "case TTL 已到期: $case_id" 4; return $?; }
  targets="$(state_get targets "$file" 2>/dev/null || true)"
  expected="$(state_get target_count "$file" 2>/dev/null || true)"
  [[ "$expected" =~ ^[0-9]+$ ]] || expected=0
  local -a exec_flags=(--timeout "$timeout")
  [[ -z "$confirm" ]] || exec_flags+=("$confirm")
  local -a pids=() roles=() files=()
  while IFS= read -r item; do
    [[ -n "$item" ]] || continue
    role="${item%%=*}"
    processed=$((processed + 1))
    tmp="$(mktemp "$STATE_ROOT/.exec-all.XXXXXX")" || return 6
    if [[ "$from_stdin" -eq 1 ]]; then
      (printf '%s' "$script" | cmd_exec --case "$case_id" --role "$role" "${exec_flags[@]}" --stdin > "$tmp" 2>&1) &
    else
      (cmd_exec --case "$case_id" --role "$role" "${exec_flags[@]}" -- "$command" > "$tmp" 2>&1) &
    fi
    pids+=("$!"); roles+=("$role"); files+=("$tmp")
  done < <(printf '%s\n' "$targets" | tr ',' '\n')
  for ((i=0; i<${#pids[@]}; i++)); do
    pid="${pids[$i]}"
    if wait "$pid"; then
      succeeded=$((succeeded + 1))
    else
      overall=1
    fi
    printf '=== role=%s ===\n' "${roles[$i]}"
    cat "${files[$i]}"
    rm -f "${files[$i]}"
  done
  if [[ "$processed" -ne "$expected" ]]; then
    overall=1
    warn "case target 数量不一致: expected=$expected processed=$processed"
  fi
  printf 'exec_summary expected=%s processed=%s succeeded=%s failed=%s\n' \
    "$expected" "$processed" "$succeeded" "$((processed - succeeded))"
  return "$overall"
}

# 解析 host 并把 state 路径写入全局 RESOLVED_STATE_FILE。
# 刻意不用 stdout 返回值：调用方若写成 $(...)，connect 会落在命令替换子 shell 里，
# 子 shell 退出时新建的 tmux server 和 cleanup watcher 会被一起带走，状态随即消失。
RESOLVED_STATE_FILE=""
resolve_state_file() {
  local host_input="$1" auto_reconnect="${2:-0}" parts full state_file
  parts="$(parse_host "$host_input")" || return $?
  full="$(printf '%s\n' "$parts" | sed -n '3p')"
  state_file="$(host_state_path "$full")"
  if [[ ! -f "$state_file" || "$(state_get phase "$state_file" 2>/dev/null || true)" != connected ]]; then
    if [[ "$auto_reconnect" == 1 ]]; then
      warn "连接状态缺失或未就绪，按相同目标重连一次: $full"
      cmd_connect "$full" || return $?
    fi
  fi
  RESOLVED_STATE_FILE="$state_file"
}

# 把本地脚本模板做占位符替换后，经 base64 送到远端执行（免嵌套引号转义）。
# 危险等级按替换后的脚本原文判定，不因 base64 包装而绕过确认。
remote_script_run() {
  local state_file="$1" template="$2" timeout="$3" confirm="$4"; shift 4
  local script b64 pair key value
  [[ -f "$template" ]] || { fail "远端脚本模板不存在: $template" 6; return $?; }
  script="$(cat "$template")" || return 6
  for pair in "$@"; do
    key="${pair%%=*}"
    value="${pair#*=}"
    if [[ "$key" == __CUT__ ]]; then
      # plan 模式把标记之后的内容整段去掉：脚本里根本不含 kill，
      # 危险分级自然是只读，不需要用户为"只看计划"做高危确认。
      script="${script%%"$value"*}"
      continue
    fi
    script="${script//@@${key}@@/$value}"
  done
  if (( ${#script} > ${TOM_AUTODEBUG_MAX_STDIN_BYTES:-16384} )); then
    fail "远端脚本过大（${#script} 字节）" 2
    return $?
  fi
  b64="$(printf '%s' "$script" | base64 | tr -d '\n')"
  # keywords-only：模板是 skill 自带并经过审阅的脚本，只按动作关键字分级，
  # 不让 awk 里的比较运算触发重定向误判；--apply 这类真实动作仍由子命令层强制确认。
  exec_on_state "$state_file" "" "" "printf '%s' $b64 | base64 -d | bash" "$timeout" "$confirm" "$script" keywords-only
}

cmd_proc_audit() {
  local host_input="${1:-}" pattern="$AGENT_PATTERN_DEFAULT" maxproc=40 timeout=90 reconnect=0 arg state_file
  [[ -n "$host_input" && "$host_input" != --* ]] || { subcommand_usage proc-audit >&2; return 2; }
  shift
  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --pattern) [[ $# -ge 2 ]] || { fail "--pattern 缺少值"; return 2; }; pattern="$2"; shift 2 ;;
      --max) [[ $# -ge 2 ]] || { fail "--max 缺少值"; return 2; }; maxproc="$2"; shift 2 ;;
      --timeout) [[ $# -ge 2 ]] || { fail "--timeout 缺少值"; return 2; }; timeout="$2"; shift 2 ;;
      --reconnect-on-stale) reconnect=1; shift ;;
      *) fail "proc-audit 未知参数: $arg"; return 2 ;;
    esac
  done
  parse_positive_int "$maxproc" --max || return $?
  parse_positive_int "$timeout" --timeout || return $?
  resolve_state_file "$host_input" "$reconnect" || return $?
  state_file="$RESOLVED_STATE_FILE"
  remote_script_run "$state_file" "$REMOTE_DIR/proc_audit.sh" "$timeout" "" \
    "PATTERN=$pattern" "MAXPROC=$maxproc"
}

cmd_proc_stop() {
  local host_input="${1:-}" root="" pattern="$AGENT_PATTERN_DEFAULT" grace=10 timeout=120
  local mode=plan allow_root=0 include_shared=0 reconnect=0 confirm="" arg state_file
  [[ -n "$host_input" && "$host_input" != --* ]] || { subcommand_usage proc-stop >&2; return 2; }
  shift
  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --session-root) [[ $# -ge 2 ]] || { fail "--session-root 缺少值"; return 2; }; root="$2"; shift 2 ;;
      --pattern) [[ $# -ge 2 ]] || { fail "--pattern 缺少值"; return 2; }; pattern="$2"; shift 2 ;;
      --grace) [[ $# -ge 2 ]] || { fail "--grace 缺少值"; return 2; }; grace="$2"; shift 2 ;;
      --timeout) [[ $# -ge 2 ]] || { fail "--timeout 缺少值"; return 2; }; timeout="$2"; shift 2 ;;
      --apply) mode=apply; shift ;;
      --allow-root-owned) allow_root=1; shift ;;
      --include-shared-parent) include_shared=1; shift ;;
      --reconnect-on-stale) reconnect=1; shift ;;
      --confirm-dangerous) confirm="$arg"; shift ;;
      *) fail "proc-stop 未知参数: $arg"; return 2 ;;
    esac
  done
  [[ -n "$root" ]] || { fail "proc-stop 需要 --session-root PID（先用 proc-audit 取 session_root）"; return 2; }
  [[ "$root" =~ ^[0-9]+$ ]] || { fail "--session-root 必须是数字 PID: $root"; return 2; }
  [[ "$root" != 1 ]] || { fail "拒绝以 PID 1 作为 session root"; return 2; }
  parse_positive_int "$grace" --grace || return $?
  parse_positive_int "$timeout" --timeout || return $?
  if [[ "$mode" == apply && "$confirm" != --confirm-dangerous ]]; then
    fail "proc-stop --apply 会发送 SIGTERM；确认后追加 --confirm-dangerous（先不带 --apply 看执行计划）" 2
    return $?
  fi
  resolve_state_file "$host_input" "$reconnect" || return $?
  state_file="$RESOLVED_STATE_FILE"
  local -a extra=("ROOT=$root" "PATTERN=$pattern" "GRACE=$grace" "MODE=$mode" \
    "ALLOWROOT=$allow_root" "INCLUDESHARED=$include_shared")
  [[ "$mode" == apply ]] || extra+=("__CUT__=# ---APPLY-SECTION---")
  remote_script_run "$state_file" "$REMOTE_DIR/proc_stop.sh" "$timeout" "$confirm" "${extra[@]}"
}

cmd_wait_processes() {
  local host_input="${1:-}" pattern="" interval=5 total=120 reconnect=0 arg state_file deadline rc out
  [[ -n "$host_input" && "$host_input" != --* ]] || { subcommand_usage wait-processes >&2; return 2; }
  shift
  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --pattern) [[ $# -ge 2 ]] || { fail "--pattern 缺少值"; return 2; }; pattern="$2"; shift 2 ;;
      --interval) [[ $# -ge 2 ]] || { fail "--interval 缺少值"; return 2; }; interval="$2"; shift 2 ;;
      --timeout) [[ $# -ge 2 ]] || { fail "--timeout 缺少值"; return 2; }; total="$2"; shift 2 ;;
      --reconnect-on-stale) reconnect=1; shift ;;
      *) fail "wait-processes 未知参数: $arg"; return 2 ;;
    esac
  done
  [[ -n "$pattern" ]] || { fail "wait-processes 需要 --pattern REGEX"; return 2; }
  parse_positive_int "$interval" --interval || return $?
  parse_positive_int "$total" --timeout || return $?
  resolve_state_file "$host_input" "$reconnect" || return $?
  state_file="$RESOLVED_STATE_FILE"
  deadline=$(( $(now) + total ))
  while :; do
    out="$(exec_on_state "$state_file" "" "" "pgrep -f -- $(shell_quote "$pattern") | wc -l | tr -d ' '" "$COMMAND_TIMEOUT" "" 2>&1)"
    rc=$?
    if (( rc != 0 )); then
      printf '%s\n' "$out" >&2
      fail "wait-processes 探测失败: $host_input" 5
      return $?
    fi
    out="$(printf '%s' "$out" | tr -dc '0-9')"
    [[ -n "$out" ]] || out=0
    log "matching=$out pattern=$pattern"
    if (( out == 0 )); then
      log "wait-processes 完成：已无匹配进程"
      return 0
    fi
    if (( $(now) + interval > deadline )); then
      fail "wait-processes 在 ${total}s 内仍有 $out 个匹配进程: $pattern" 4
      return $?
    fi
    sleep "$interval"
  done
}

cmd_watch() {
  local host_input="${1:-}" until_cmd="" probe_cmd="" interval=10 total=1800 arg parts full state_file
  local deadline rc probe_out
  [[ -n "$host_input" && "$host_input" != --* ]] || { subcommand_usage watch >&2; return 2; }
  shift
  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --until) [[ $# -ge 2 ]] || { fail "--until 缺少值"; return 2; }; until_cmd="$2"; shift 2 ;;
      --probe) [[ $# -ge 2 ]] || { fail "--probe 缺少值"; return 2; }; probe_cmd="$2"; shift 2 ;;
      --interval) [[ $# -ge 2 ]] || { fail "--interval 缺少值"; return 2; }; interval="$2"; shift 2 ;;
      --timeout) [[ $# -ge 2 ]] || { fail "--timeout 缺少值"; return 2; }; total="$2"; shift 2 ;;
      *) fail "watch 未知参数: $arg"; return 2 ;;
    esac
  done
  [[ -n "$until_cmd" ]] || { fail "watch 需要 --until \"<远端测试命令>\""; return 2; }
  parse_positive_int "$interval" --interval || return $?
  parse_positive_int "$total" --timeout || return $?
  parts="$(parse_host "$host_input")" || return $?
  full="$(printf '%s\n' "$parts" | sed -n '3p')"
  state_file="$(host_state_path "$full")"
  deadline=$(( $(now) + total ))
  while :; do
    if [[ -n "$probe_cmd" ]]; then
      probe_out="$(exec_on_state "$state_file" "" "" "$probe_cmd" "$COMMAND_TIMEOUT" "" 2>&1)" || true
      printf '[tom-autodebug] probe @%s\n%s\n' "$(date +%H:%M:%S)" "$probe_out"
    fi
    exec_on_state "$state_file" "" "" "$until_cmd" "$COMMAND_TIMEOUT" "" >/dev/null 2>&1
    rc=$?
    if (( rc == 0 )); then
      log "watch 条件已满足: host=$full until=$until_cmd"
      return 0
    fi
    if (( $(now) + interval > deadline )); then
      fail "watch 在 ${total}s 内条件未满足: host=$full until=$until_cmd" 4
      return $?
    fi
    sleep "$interval"
  done
}

subcommand_usage() {
  case "$1" in
    exec)
      cat <<'EOF'
Usage:
  autodebug.sh exec <host-or-ip> [--timeout SECONDS] [--confirm-write|--confirm-dangerous] [--stdin] -- COMMAND...
  autodebug.sh exec --case CASE_ID --role ROLE [同上选项] -- COMMAND...

参数顺序固定：<host> 紧跟 exec，flag 在 host 之后、-- 之前。
  正确: exec host1 --timeout 300 --confirm-dangerous -- "ps -ef | grep bgw"
  错误: exec --confirm-dangerous host1 -- "..."   # 报 exec 未知参数

  --timeout SECONDS      单条命令超时，默认 60（TOM_AUTODEBUG_COMMAND_TIMEOUT）
  --confirm-write        放行低危副作用：写文件/重定向、cp、ln、tee、tcpdump、nohup、tail -f
  --confirm-dangerous    放行高危：sudo、kill/pkill、rm、mv、chmod/chown、dd、
                         systemctl/service、iptables/nft、ip route|addr|link|neigh 变更、
                         modprobe/insmod/rmmod、reboot/shutdown（同时覆盖低危）
  --stdin                从标准输入读多行脚本，经 base64 传输，免去嵌套引号转义；
                         与 -- COMMAND 互斥，上限 16KB（TOM_AUTODEBUG_MAX_STDIN_BYTES）

  引号内的字面量（如 echo "a -> b"）不会被当成重定向，不再误判为有副作用。
EOF
      ;;
    connect)
      cat <<'EOF'
Usage:
  autodebug.sh connect <host-or-ip> [--user USER] [--relay-user USER] [--ttl SECONDS]
                                    [--session NAME] [--namespace NAME] [--transport ssh] [--new-pane]

  --ttl SECONDS   空闲滑动窗口，默认 1800（TOM_AUTODEBUG_TTL）
EOF
      ;;
    capture)
      printf 'Usage:\n  autodebug.sh capture <host-or-ip> [--lines N]\n'
      ;;
    watch)
      cat <<'EOF'
Usage:
  autodebug.sh watch <host-or-ip> --until "<远端测试命令>" [--probe "<远端观察命令>"]
                                  [--interval SECONDS] [--timeout SECONDS]

有界轮询等待远端长任务，替代 tail -f / 无限 while / nohup。
  --until      退出码为 0 时视为完成，例如 "test -f /home/work/.done"
  --probe      每轮额外执行一条只读观察命令并打印，例如 "stat -c %s /tmp/f"
  --interval   轮询间隔，默认 10
  --timeout    总等待上限，默认 1800；超时返回 4

示例（等一个下载完成，同时观察文件大小）:
  autodebug.sh watch host1 --until "! pgrep -f 'wget.*pkg.tar.gz'" \
    --probe "stat -c '%s' /tmp/pkg.tar.gz" --interval 15 --timeout 1800
EOF
      ;;
    proc-audit)
      cat <<'EOF'
Usage:
  autodebug.sh proc-audit <host-or-ip> [--pattern REGEX] [--max N] [--timeout SECONDS]
                                       [--reconnect-on-stale]

只读审计 Agent 进程树。默认 pattern: ducc|claude-go|claude|happy|codex
每条记录含 kind、session_key（MCP 端口或 session-hook）、session_root、
shared_parent（共享 daemon）、shared_parent_sessions、cpu/mem/rss、elapsed_seconds、
permission_bypass（是否 --dangerously-skip-permissions）。
末尾 root_summary / daemon_summary 用于判断某个父进程是否被多个会话共用。
拿到 session_root 后再交给 proc-stop，不要手工记 PID。
EOF
      ;;
    proc-stop)
      cat <<'EOF'
Usage:
  autodebug.sh proc-stop <host-or-ip> --session-root PID [--pattern REGEX] [--grace SECONDS]
                         [--include-shared-parent] [--allow-root-owned]
                         [--apply --confirm-dangerous] [--timeout SECONDS]

默认只打印执行计划，不发信号；确认计划无误后再加 --apply --confirm-dangerous。
进程集合在远端执行时重新推导，不复用上一步采集的 PID（避免 PID 复用误杀）。
安全约束：
  - 跳过 PID 1；root 所属进程默认跳过，需 --allow-root-owned 才纳入
  - 共享 daemon 默认保留；--include-shared-parent 仅在它没有其他会话时才纳入，
    否则打印 refuse_shared_parent 并停止
  - 先叶子后 launcher 发 SIGTERM，等待 --grace 后复查并重试一次
  - 从不发送 SIGKILL；仍有残留会打印 final_remaining 交给人判断
EOF
      ;;
    wait-processes)
      cat <<'EOF'
Usage:
  autodebug.sh wait-processes <host-or-ip> --pattern REGEX [--interval SECONDS]
                              [--timeout SECONDS] [--reconnect-on-stale]

有界轮询等待匹配进程全部退出（pgrep -f）。全部退出返回 0，超时返回 4。
EOF
      ;;
    status)
      printf 'Usage:\n  autodebug.sh status [--case CASE_ID]\n'
      ;;
    cleanup)
      printf 'Usage:\n  autodebug.sh cleanup [--host HOST] [--case CASE_ID] [--all]\n'
      ;;
    defaults)
      cat <<'EOF'
Usage:
  autodebug.sh defaults set --relay-user USER --ssh-user USER
  autodebug.sh defaults show
  autodebug.sh defaults clear
EOF
      ;;
    case-create)
      printf 'Usage:\n  autodebug.sh case-create CASE_ID --target ROLE=[USER@]HOST [--target ...] [--ttl SECONDS]\n'
      ;;
    connect-many)
      printf 'Usage:\n  autodebug.sh connect-many CASE_ID\n'
      ;;
    exec-all)
      cat <<'EOF'
Usage:
  autodebug.sh exec-all --case CASE_ID [--timeout SECONDS]
                            [--confirm-write|--confirm-dangerous] -- COMMAND...
  autodebug.sh exec-all --case CASE_ID [--timeout SECONDS]
                            [--confirm-write|--confirm-dangerous] --stdin < SCRIPT
EOF
      ;;
    *)
      usage >&2
      printf '[tom-autodebug] ERROR: 未知子命令: %s\n' "$1" >&2
      return 2
      ;;
  esac
}

main() {
  local subcommand="${1:-}"
  [[ -n "$subcommand" ]] || { usage; return 2; }
  shift
  case "$subcommand" in
    -h|--help|help) usage; return 0 ;;
    --version|version) printf '%s\n' "$VERSION"; return 0 ;;
  esac
  # 任何子命令支持 --help，便于自查参数顺序。
  if [[ "${1:-}" == -h || "${1:-}" == --help ]]; then
    subcommand_usage "$subcommand"
    return $?
  fi
  case "$subcommand" in
    preflight) [[ $# -eq 0 ]] || { usage; return 2; }; preflight ;;
    defaults) cmd_defaults "$@" ;;
    connect) cmd_connect "$@" ;;
    exec) cmd_exec "$@" ;;
    capture) cmd_capture "$@" ;;
    watch) cmd_watch "$@" ;;
    proc-audit) cmd_proc_audit "$@" ;;
    proc-stop) cmd_proc_stop "$@" ;;
    wait-processes) cmd_wait_processes "$@" ;;
    status) cmd_status "$@" ;;
    cleanup) cmd_cleanup "$@" ;;
    case-create) cmd_case_create "$@" ;;
    connect-many) cmd_connect_many "$@" ;;
    exec-all) cmd_exec_all "$@" ;;
    *) usage >&2; fail "未知子命令: $subcommand" 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
