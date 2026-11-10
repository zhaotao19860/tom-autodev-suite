#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/sugar_query.sh"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# shellcheck source=../scripts/sugar_query.sh
source "$SCRIPT"
set +e

fail_test() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_eq() {
  local actual="$1" expected="$2" label="$3"
  [[ "$actual" == "$expected" ]] || fail_test "$label: expected=[$expected] actual=[$actual]"
}

assert_contains() {
  local text="$1" expected="$2" label="$3"
  [[ "$text" == *"$expected"* ]] || fail_test "$label: missing [$expected] in [$text]"
}

assert_ok() {
  local label="$1"
  shift
  "$@" >/dev/null 2>&1 || fail_test "$label: expected success"
}

assert_rc() {
  local expected="$1" label="$2" rc
  shift 2
  "$@" >/dev/null 2>&1
  rc=$?
  [[ "$rc" == "$expected" ]] || fail_test "$label: expected rc=$expected actual=$rc"
}

# --- 语法 / shellcheck ---
bash -n "$SCRIPT" || fail_test "bash -n sugar_query.sh"
bash -n "${BASH_SOURCE[0]}" || fail_test "bash -n test"
[[ -x "$SCRIPT" ]] || fail_test "sugar_query.sh 不可执行"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck "$SCRIPT" "${BASH_SOURCE[0]}" || fail_test "shellcheck"
fi

# --- SQL 校验 ---
assert_ok "select" sugar_validate_sql "select * from bgw_port_node"
assert_ok "SELECT upper" sugar_validate_sql "  SELECT node FROM t WHERE \$__timeFilter(time)  "
assert_ok "show" sugar_validate_sql "show tables"
assert_ok "describe" sugar_validate_sql "describe bgw_port_node"
assert_ok "desc" sugar_validate_sql "desc bgw_port_node;"
assert_ok "update_time column" sugar_validate_sql "select update_time, created_by from t"
assert_eq "$(sugar_validate_sql 'select 1;' 2>/dev/null)" "select 1" "strip trailing ;"
assert_rc 2 "insert" sugar_validate_sql "insert into t values (1)"
assert_rc 2 "drop" sugar_validate_sql "drop table t"
assert_rc 2 "select then drop" sugar_validate_sql "select 1; drop table t"
assert_rc 2 "two selects" sugar_validate_sql "select 1; select 2;"
assert_rc 2 "comment --" sugar_validate_sql "select 1 -- x"
assert_rc 2 "comment /*" sugar_validate_sql "select /* x */ 1"
assert_rc 2 "comment #" sugar_validate_sql "select 1 # x"
assert_rc 2 "into outfile" sugar_validate_sql "select * from t into outfile '/tmp/x'"
assert_rc 2 "sleep(" sugar_validate_sql "select sleep(10)"
assert_rc 2 "BENCHMARK upper" sugar_validate_sql "SELECT BENCHMARK(1,1)"
assert_rc 2 "with" sugar_validate_sql "with a as (select 1) select * from a"
assert_rc 2 "set" sugar_validate_sql "set @a=1"
assert_rc 2 "empty" sugar_validate_sql "   "
assert_eq "$(sugar_validate_sql 'drop table t' 2>&1)" "[sugar-query] ERROR: 只允许 select/show/desc/describe 开头的只读 SQL（当前: drop）" "chinese msg"

# --- limit ---
assert_eq "$(sugar_apply_limit 'select * from t' 2000 10000)" "select * from t limit 2000" "limit appended"
assert_eq "$(sugar_apply_limit 'select * from t LIMIT 50' 2000 10000)" "select * from t LIMIT 50" "limit kept"
assert_eq "$(sugar_apply_limit 'select * from t limit 10, 20' 2000 10000)" "select * from t limit 10, 20" "limit offset kept"
assert_eq "$(sugar_apply_limit 'show tables' 2000 10000)" "show tables" "show no limit"
assert_rc 2 "limit over max" sugar_apply_limit "select * from t limit 20000" 2000 10000
assert_rc 2 "limit over max offset form" sugar_apply_limit "select * from t limit 0, 10001" 2000 10000

# --- 时间参数 ---
assert_eq "$(TZ=UTC sugar_time_arg '2026-11-09 17:07')" "1794215220000" "local time -> epoch ms (TZ=UTC)"
assert_eq "$(TZ=America/New_York sugar_time_arg '2026-11-09 17:07:00')" "1794215220000" "TZ independent"
assert_eq "$(sugar_time_arg '1970-01-01 08:00')" "0" "epoch zero"
assert_eq "$(sugar_time_arg '2024-02-29 08:00:01')" "1709164801000" "leap day"
expected_ms=$(( (1794215220 - 0) * 1000 ))
assert_eq "$(sugar_time_arg '2026-11-09 17:07')" "$expected_ms" "arith"
assert_eq "$(sugar_time_arg 'now-2h')" "now-2h" "now-2h passthrough"
assert_eq "$(sugar_time_arg 'now')" "now" "now passthrough"
assert_eq "$(sugar_time_arg '1794215220000')" "1794215220000" "epoch passthrough"
assert_rc 2 "bad time" sugar_time_arg "yesterday"
assert_rc 2 "bad month" sugar_time_arg "2026-13-01 00:00"
assert_rc 2 "injection time" sugar_time_arg "now'; drop"

# --- payload ---
TRICKY="select 'a''b' x, \"q\" y, 'c:\\\\d' z from t where \$__timeFilter(time) and n='\$x'"
payload="$(sugar_build_payload "$TRICKY" "now-1h" "now")" || fail_test "build payload"
printf '%s' "$payload" | python3 -I -c '
import json, sys
p = json.load(sys.stdin)
sql = sys.argv[1]
assert p["from"] == "now-1h" and p["to"] == "now", p
q = p["queries"][0]
assert q["rawSql"] == sql, (q["rawSql"], sql)
assert q["refId"] == "A" and q["format"] == "table"
assert q["datasource"] == {"type": "mysql", "uid": "gDMdnQ8Vk"}
' "$TRICKY" || fail_test "payload JSON 校验"
[[ "$TRICKY" == *'\'* && "$TRICKY" == *"'"* && "$TRICKY" == *'"'* ]] || fail_test "tricky 字符串构造"

# --- JS 生成 ---
js="$(sugar_build_js "$payload" 0)" || fail_test "build js"
assert_contains "$js" 'taskSpace(CFG.taskName)' "js taskSpace"
assert_contains "$js" 'x-grafana-org-id' "js header"
assert_contains "$js" 'task.handOff()' "js handoff"
printf '%s\n' "$js" | head -1 | sed 's/^const CFG = //; s/;$//' | python3 -I -c '
import json, sys
c = json.load(sys.stdin)
assert c["payload"]["queries"][0]["rawSql"] == sys.argv[1]
assert c["rawTime"] is False and c["orgId"] == "149"
' "$TRICKY" || fail_test "js CFG 字面量"
if command -v node >/dev/null 2>&1; then
  printf '%s\n' "$js" > "$TMP_ROOT/gen.mjs"
  node --check "$TMP_ROOT/gen.mjs" || fail_test "node --check 生成的 JS"
fi

# --- 标识符 ---
assert_rc 2 "node a'b" main port "a'b"
assert_rc 2 "cluster quote" main nodes "x' or '1'='1"
assert_rc 2 "port quote" main port node1 --port "vnic1'"
assert_rc 2 "table space" main columns "t; drop"
assert_rc 2 "bad by" main port node1 --by week
assert_rc 2 "bad limit" main sql "select 1" --limit 0
assert_rc 2 "limit over" main sql "select 1" --limit 10001
assert_rc 2 "unknown cmd" main foo
assert_rc 0 "help" main --help

# --- stub ego-browser ---
STUB="$TMP_ROOT/ego-stub"
cat > "$STUB" <<'STUB_EOF'
#!/usr/bin/env bash
cat > "${STUB_CAPTURE:-/dev/null}"
case "${STUB_MODE:-ok}" in
  # 真实 ego-browser 把 console.log/console.error 都写到 stderr，这里模拟该行为。
  ok) { echo '[error] [sugar-query] 打开 sugar 看板'; printf 'SUGAR_BEGIN\nt\tport_name\trx_pkts\n2026-11-09 17:00\tvnic1\t42\nSUGAR_END\n'; } >&2 ;;
  empty) printf 'SUGAR_BEGIN\nt\tport_name\nSUGAR_END\n' ;;
  login) printf 'SUGAR_ERR_LOGIN: 请在 ego-browser 窗口登录 UUAP 后重跑\n' ;;
  error) printf "SUGAR_ERR_QUERY: db query error: Error 1146: Table 'x' doesn't exist\n" ;;
  crash) echo boom >&2; exit 7 ;;
esac
STUB_EOF
chmod +x "$STUB"
export SUGAR_EGO_BIN="$STUB" STUB_CAPTURE="$TMP_ROOT/captured.js"

out="$(STUB_MODE=ok bash "$SCRIPT" sql "select * from t where \$__timeFilter(time)" 2>/dev/null)"
rc=$?
assert_eq "$rc" "0" "stub ok rc"
assert_eq "$out" "$(printf 't\tport_name\trx_pkts\n2026-11-09 17:00\tvnic1\t42')" "stub ok stdout"
assert_contains "$(cat "$TMP_ROOT/captured.js")" 'select * from t where $__timeFilter(time) limit 2000' "captured sql with limit"
err="$(STUB_MODE=ok bash "$SCRIPT" sql "select 1" 2>&1 >/dev/null)"
assert_contains "$err" "打开 sugar 看板" "diagnostics forwarded to stderr"
[[ "$err" != *SUGAR_BEGIN* && "$err" != *vnic1* ]] || fail_test "TSV leaked into stderr"

out="$(STUB_MODE=ok bash "$SCRIPT" port yq01-sys-x86ibgw44.yq01 --port vnic1 --by hour --from '2026-11-09 17:07' 2>/dev/null)"
assert_eq "$?" "0" "port rc"
captured="$(cat "$TMP_ROOT/captured.js")"
assert_contains "$captured" "date_format(time,'%Y-%m-%d %H:00') t" "port bucket"
assert_contains "$captured" "node='yq01-sys-x86ibgw44.yq01' and port_name='vnic1' and rx_nombuf<2000000000" "port where"
assert_contains "$captured" '"from": "1794215220000"' "port from epoch"

STUB_MODE=empty bash "$SCRIPT" tables >/dev/null 2>&1
assert_eq "$?" "0" "stub empty rc"
assert_rc 5 "stub login" env STUB_MODE=login bash "$SCRIPT" sql "select 1"
assert_rc 3 "stub error" env STUB_MODE=error bash "$SCRIPT" sql "select 1"
err="$(STUB_MODE=error bash "$SCRIPT" sql "select 1" 2>&1 >/dev/null)"
assert_contains "$err" "db query error: Error 1146" "error message printed"
assert_rc 4 "stub crash" env STUB_MODE=crash bash "$SCRIPT" sql "select 1"
assert_rc 4 "missing ego" env SUGAR_EGO_BIN="$TMP_ROOT/nope" bash "$SCRIPT" tables
assert_rc 2 "bad sql via cli" bash "$SCRIPT" sql "delete from t"

printf 'PASS: sugar_query tests\n'
