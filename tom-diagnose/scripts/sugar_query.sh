#!/usr/bin/env bash
# sugar_query: 通过用户已登录的 ego-browser 会话，向部门 sugar(Grafana) 数据源发只读 SQL，输出 TSV。
set -uo pipefail

readonly SUGAR_VERSION="1.0.0"
readonly SUGAR_BASE="https://console.cloud.baidu-int.com/mtgrafana/"
readonly SUGAR_DASH_URL="https://console.cloud.baidu-int.com/mtgrafana/p/d/0762Fx5vz?orgId=149"
readonly SUGAR_API_URL="https://console.cloud.baidu-int.com/mtgrafana/p/api/ds/query"
readonly SUGAR_ORG_ID="149"
readonly SUGAR_DS_UID="gDMdnQ8Vk"
readonly SUGAR_DEFAULT_LIMIT=2000
readonly SUGAR_MAX_LIMIT=10000
readonly SUGAR_TASK_NAME="tom-diagnose sugar"
SUGAR_EGO_BIN="${SUGAR_EGO_BIN:-ego-browser}"

# 退出码
readonly E_ARGS=2 E_QUERY=3 E_BROWSER=4 E_LOGIN=5

usage() {
  cat <<'EOF'
Usage:
  sugar_query.sh sql "<SQL>" [--from FROM] [--to TO] [--limit N] [--raw-time]
  sugar_query.sh tables
  sugar_query.sh columns <table>
  sugar_query.sh nodes <cluster> [--from FROM] [--to TO]
  sugar_query.sh port <node> [--port vnicN] [--by minute|hour|day] [--from FROM] [--to TO]
  sugar_query.sh -h|--help

Notes:
  - 只读：仅允许单条 select/show/desc/describe，拒绝写操作关键字与注释。
  - FROM/TO：Grafana 相对时间（now-1h、now）、epoch 毫秒，或本地时间 "YYYY-MM-DD HH:MM[:SS]"（按 UTC+8 解释）。
    默认 --from now-1h --to now。SQL 中可用 $__timeFilter(time)。
  - select 无 limit 时自动追加 limit N（默认 2000，最大 10000）。
  - time 类型字段按库内本地墙钟时间输出（YYYY-MM-DD HH:MM:SS）；--raw-time 输出原始 epoch 毫秒。
  - 依赖 ego-browser（可用环境变量 SUGAR_EGO_BIN 覆盖）与 python3。
  - 退出码：0 成功；2 参数/SQL 不合法；3 数据源报错；4 ego-browser 缺失或失败；5 需要登录 UUAP。
EOF
}

log() { printf '[sugar-query] %s\n' "$*" >&2; }
fail() {
  local message="${1:-未知错误}" status="${2:-2}"
  printf '[sugar-query] ERROR: %s\n' "$message" >&2
  return "$status"
}

sugar_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

sugar_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# 校验标识符（node/cluster/port/table），不允许引号等字符。
sugar_valid_ident() {
  [[ "${1:-}" =~ ^[A-Za-z0-9._-]+$ ]]
}

# 校验只读 SQL；成功时输出去掉末尾分号后的 SQL。
sugar_validate_sql() {
  local sql lower first word
  sql="$(sugar_trim "${1:-}")"
  [[ -n "$sql" ]] || { fail "SQL 为空" "$E_ARGS"; return $?; }
  if [[ "$sql" == *";" ]]; then
    sql="$(sugar_trim "${sql%;}")"
  fi
  [[ "$sql" != *";"* ]] && [[ -n "$sql" ]] || { fail "只允许单条 SQL 语句（不能含多余的 ;）" "$E_ARGS"; return $?; }
  if [[ "$sql" == *"--"* || "$sql" == *"/*"* || "$sql" == *"#"* ]]; then
    fail "SQL 不允许包含注释（--、/*、#）" "$E_ARGS"
    return $?
  fi
  lower="$(sugar_lower "$sql")"
  first="$(printf '%s' "$lower" | tr -c 'a-z0-9_' ' ' | awk '{print $1}')"
  case "$first" in
    select | show | desc | describe) ;;
    *)
      fail "只允许 select/show/desc/describe 开头的只读 SQL（当前: ${first:-空}）" "$E_ARGS"
      return $?
      ;;
  esac
  for word in $(printf '%s' "$lower" | tr -c 'a-z0-9_' ' '); do
    case "$word" in
      insert | update | delete | drop | alter | create | truncate | replace | grant | revoke | rename | \
        load_file | outfile | dumpfile | sleep | benchmark | set | call | handler | lock | unlock)
        fail "SQL 含禁止的关键字: $word" "$E_ARGS"
        return $?
        ;;
    esac
  done
  printf '%s' "$sql"
}

# select 无末尾 limit 时追加 limit N；已有 limit K 且 K > max 时失败。show/desc 原样返回。
sugar_apply_limit() {
  local sql="$1" limit="${2:-$SUGAR_DEFAULT_LIMIT}" max="${3:-$SUGAR_MAX_LIMIT}" lower first count
  lower="$(sugar_lower "$sql")"
  first="$(printf '%s' "$lower" | tr -c 'a-z0-9_' ' ' | awk '{print $1}')"
  if [[ "$first" != "select" ]]; then
    printf '%s' "$sql"
    return 0
  fi
  local re='[[:space:]]limit[[:space:]]+([0-9]+)([[:space:]]*,[[:space:]]*([0-9]+))?([[:space:]]+offset[[:space:]]+[0-9]+)?[[:space:]]*$'
  if [[ "$lower" =~ $re ]]; then
    count="${BASH_REMATCH[1]}"
    [[ -n "${BASH_REMATCH[3]}" ]] && count="${BASH_REMATCH[3]}"
    count=$((10#$count))
    if ((count > max)); then
      fail "limit $count 超过上限 $max" "$E_ARGS"
      return $?
    fi
    printf '%s' "$sql"
  else
    printf '%s limit %s' "$sql" "$limit"
  fi
}

# 时间参数：now/now-2h/epoch 毫秒原样；"YYYY-MM-DD HH:MM[:SS]" 按 UTC+8 转 epoch 毫秒。
sugar_time_arg() {
  local arg
  arg="$(sugar_trim "${1:-}")"
  if [[ "$arg" =~ ^now([+-][0-9]+[smhdwMy])?(/[smhdwMy])?$ || "$arg" =~ ^[0-9]{10,13}$ ]]; then
    printf '%s' "$arg"
    return 0
  fi
  local re='^([0-9]{4})-([0-9]{2})-([0-9]{2})[ T]([0-9]{2}):([0-9]{2})(:([0-9]{2}))?$'
  if [[ "$arg" =~ $re ]]; then
    local y=$((10#${BASH_REMATCH[1]})) mo=$((10#${BASH_REMATCH[2]})) d=$((10#${BASH_REMATCH[3]}))
    local h=$((10#${BASH_REMATCH[4]})) mi=$((10#${BASH_REMATCH[5]})) s=0
    [[ -n "${BASH_REMATCH[7]}" ]] && s=$((10#${BASH_REMATCH[7]}))
    if ((mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || s > 59)); then
      fail "时间参数不合法: $arg" "$E_ARGS"
      return $?
    fi
    # days_from_civil（Howard Hinnant），与 TZ 无关的纯整数运算。
    local yy=$y era yoe doy doe days
    ((mo <= 2)) && yy=$((y - 1))
    era=$(((yy >= 0 ? yy : yy - 399) / 400))
    yoe=$((yy - era * 400))
    doy=$(((153 * (mo > 2 ? mo - 3 : mo + 9) + 2) / 5 + d - 1))
    doe=$((yoe * 365 + yoe / 4 - yoe / 100 + doy))
    days=$((era * 146097 + doe - 719468))
    printf '%s' "$(((days * 86400 + h * 3600 + mi * 60 + s - 8 * 3600) * 1000))"
    return 0
  fi
  fail "无法识别的时间参数: $arg（支持 now-1h、epoch 毫秒、YYYY-MM-DD HH:MM[:SS]）" "$E_ARGS"
}

# 输出 /api/ds/query 请求体 JSON。参数：SQL FROM TO
sugar_build_payload() {
  command -v python3 >/dev/null 2>&1 || { fail "缺少依赖: python3" "$E_BROWSER"; return $?; }
  python3 -I -c '
import json, sys
sql, frm, to, uid = sys.argv[1:5]
print(json.dumps({"from": frm, "to": to, "queries": [{
    "refId": "A", "datasource": {"type": "mysql", "uid": uid},
    "rawSql": sql, "format": "table"}]}))
' "$1" "$2" "$3" "$SUGAR_DS_UID"
}

# 输出交给 ego-browser nodejs 执行的完整 JS。参数：PAYLOAD_JSON [RAW_TIME(0|1)]
sugar_build_js() {
  local payload="$1" raw="${2:-0}" cfg
  cfg="$(python3 -I -c '
import json, sys
print(json.dumps({"payload": json.loads(sys.argv[1]), "rawTime": sys.argv[2] == "1",
    "base": sys.argv[3], "dashUrl": sys.argv[4], "apiUrl": sys.argv[5],
    "orgId": sys.argv[6], "taskName": sys.argv[7]}))
' "$payload" "$raw" "$SUGAR_BASE" "$SUGAR_DASH_URL" "$SUGAR_API_URL" "$SUGAR_ORG_ID" "$SUGAR_TASK_NAME")" || {
    fail "生成 JS 配置失败" "$E_ARGS"
    return $?
  }
  printf 'const CFG = %s;\n' "$cfg"
  cat <<'EOF'
const out = (s) => console.log(s);
const esc = (v) => String(v).replace(/\\/g, "\\\\").replace(/\t/g, "\\t").replace(/\n/g, "\\n").replace(/\r/g, "\\r");
const fmtTime = (ms) => {
  if (ms === null || ms === undefined) return "NULL";
  if (CFG.rawTime) return String(ms);
  const d = new Date(Number(ms));
  if (isNaN(d.getTime())) return String(ms);
  // 库内存的是本地墙钟时间，Grafana 按 UTC 返回；用 UTC 读取即得到正确的本地时间。
  return d.toISOString().replace("T", " ").slice(0, 19);
};
const isLogin = (url, title) =>
  !String(url).startsWith(CFG.base) || /\/login/i.test(String(url)) || /uuap|login|登录/i.test(String(title || ""));

async function main() {
  let task;
  try {
    task = await taskSpace(CFG.taskName);
    const page = task.page("p1");
    let url = "";
    try { url = await page.url(); } catch (e) { url = ""; }
    if (!String(url).startsWith(CFG.base)) {
      console.error("[sugar-query] 打开 sugar 看板: " + CFG.dashUrl);
      await page.goto(CFG.dashUrl, { timeout: 60000 });
      await page.waitForTimeout(3000);
      url = await page.url();
    }
    let title = "";
    try { title = await page.title(); } catch (e) { title = ""; }
    if (isLogin(url, title)) {
      await task.handOff();
      out("SUGAR_ERR_LOGIN: 当前页面不在 sugar(Grafana) 下（" + url + "），请在 ego-browser 窗口登录 UUAP 后重跑");
      return;
    }
    const r = await page.fetch(CFG.apiUrl, {
      method: "POST",
      headers: { "content-type": "application/json", "x-grafana-org-id": CFG.orgId },
      body: JSON.stringify(CFG.payload),
      timeout: 90000,
    });
    const body = String(r.body || "");
    if (r.status === 401 || r.status === 403 || /^\s*</.test(body) || /"message"\s*:\s*"Unauthorized"/.test(body)) {
      await task.handOff();
      out("SUGAR_ERR_LOGIN: 接口返回 " + r.status + "，请在 ego-browser 窗口登录 UUAP 后重跑");
      return;
    }
    let data;
    try { data = JSON.parse(body); } catch (e) {
      out("SUGAR_ERR_BROWSER: 接口返回非 JSON（HTTP " + r.status + "）: " + body.slice(0, 300).replace(/\s+/g, " "));
      await task.finish({ keep: [] });
      return;
    }
    const res = (data && data.results && data.results.A) || {};
    if (res.error || !r.ok) {
      const msg = res.error || data.message || data.error || ("HTTP " + r.status);
      out("SUGAR_ERR_QUERY: " + String(msg).replace(/\s+/g, " "));
      await task.finish({ keep: [] });
      return;
    }
    const frame = (res.frames || [])[0];
    const fields = (frame && frame.schema && frame.schema.fields) || [];
    const values = (frame && frame.data && frame.data.values) || [];
    const lines = ["SUGAR_BEGIN"];
    if (fields.length) lines.push(fields.map((f) => esc(f.name)).join("\t"));
    const rows = values.length ? Math.max(...values.map((c) => (c ? c.length : 0))) : 0;
    for (let i = 0; i < rows; i++) {
      lines.push(fields.map((f, j) => {
        const v = values[j] ? values[j][i] : null;
        if (f.type === "time") return fmtTime(v);
        return v === null || v === undefined ? "NULL" : esc(v);
      }).join("\t"));
    }
    lines.push("SUGAR_END");
    out(lines.join("\n"));
    await task.finish({ keep: [] });
  } catch (e) {
    out("SUGAR_ERR_BROWSER: " + String((e && e.message) || e).replace(/\s+/g, " "));
    try { if (task) await task.finish({ keep: [] }); } catch (e2) { /* ignore */ }
  }
}
await main();
EOF
}

# 执行校验后的 SQL。参数：SQL FROM TO LIMIT RAW_TIME
sugar_run() {
  local sql="$1" from="$2" to="$3" limit="$4" raw="$5" payload js output rc status
  sql="$(sugar_validate_sql "$sql")" || return $?
  sql="$(sugar_apply_limit "$sql" "$limit" "$SUGAR_MAX_LIMIT")" || return $?
  from="$(sugar_time_arg "$from")" || return $?
  to="$(sugar_time_arg "$to")" || return $?
  command -v "$SUGAR_EGO_BIN" >/dev/null 2>&1 || { fail "未找到 ego-browser（$SUGAR_EGO_BIN），请先安装" "$E_BROWSER"; return $?; }
  payload="$(sugar_build_payload "$sql" "$from" "$to")" || return $?
  js="$(sugar_build_js "$payload" "$raw")" || return $?
  log "查询: $sql （from=$from to=$to）"
  # ego-browser 会把 console.log/console.error 都写到 stderr，因此合并两路后按标记拆分。
  output="$(printf '%s\n' "$js" | "$SUGAR_EGO_BIN" nodejs 2>&1)"
  rc=$?
  status=0
  # 标记块以外的行属于诊断信息，转发到 stderr。
  awk '/^SUGAR_BEGIN$/{f=1; next} /^SUGAR_END$/{f=0; next} !f && NF && !/^SUGAR_ERR_/ { sub(/^\[error\] /, ""); print }' <<<"$output" >&2
  if grep -q '^SUGAR_ERR_LOGIN:' <<<"$output"; then
    fail "需要登录：$(grep -m1 '^SUGAR_ERR_LOGIN:' <<<"$output" | sed 's/^SUGAR_ERR_LOGIN: *//')" "$E_LOGIN"
    return $?
  fi
  if grep -q '^SUGAR_ERR_QUERY:' <<<"$output"; then
    fail "数据源报错：$(grep -m1 '^SUGAR_ERR_QUERY:' <<<"$output" | sed 's/^SUGAR_ERR_QUERY: *//')" "$E_QUERY"
    return $?
  fi
  if grep -q '^SUGAR_ERR_BROWSER:' <<<"$output"; then
    fail "ego-browser 执行失败：$(grep -m1 '^SUGAR_ERR_BROWSER:' <<<"$output" | sed 's/^SUGAR_ERR_BROWSER: *//')" "$E_BROWSER"
    return $?
  fi
  if grep -q '^SUGAR_BEGIN$' <<<"$output"; then
    output="$(awk '/^SUGAR_BEGIN$/{f=1; next} /^SUGAR_END$/{f=0} f' <<<"$output")"
  elif ((rc != 0)); then
    [[ -n "$output" ]] && printf '%s\n' "$output" >&2
    fail "ego-browser 退出码 $rc" "$E_BROWSER"
    return $?
  fi
  if [[ -n "$output" ]]; then
    printf '%s\n' "$output"
  else
    printf '(empty)\n' >&2
  fi
  return "$status"
}

sugar_bucket_expr() {
  case "$1" in
    minute) printf "date_format(time,'%%Y-%%m-%%d %%H:%%i')" ;;
    hour) printf "date_format(time,'%%Y-%%m-%%d %%H:00')" ;;
    day) printf "date_format(time,'%%Y-%%m-%%d')" ;;
    *) fail "--by 只支持 minute|hour|day（当前: $1）" "$E_ARGS" ;;
  esac
}

sugar_need_ident() {
  local label="$1" value="${2:-}"
  sugar_valid_ident "$value" || { fail "$label 不合法: '$value'（只允许 [A-Za-z0-9._-]）" "$E_ARGS"; return $?; }
}

main() {
  local cmd="${1:-}"
  [[ $# -gt 0 ]] && shift
  case "$cmd" in
    -h | --help | help) usage; return 0 ;;
    -v | --version) printf 'sugar_query %s\n' "$SUGAR_VERSION"; return 0 ;;
    "") usage >&2; return "$E_ARGS" ;;
  esac
  local from="now-1h" to="now" limit="$SUGAR_DEFAULT_LIMIT" raw=0 port="" by="minute"
  local -a pos=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --from | --to | --limit | --port | --by)
        [[ $# -ge 2 ]] || { fail "$1 缺少参数值" "$E_ARGS"; return $?; }
        case "$1" in
          --from) from="$2" ;;
          --to) to="$2" ;;
          --limit) limit="$2" ;;
          --port) port="$2" ;;
          --by) by="$2" ;;
        esac
        shift 2
        ;;
      --raw-time) raw=1; shift ;;
      -h | --help) usage; return 0 ;;
      --*) fail "未知参数: $1" "$E_ARGS"; return $? ;;
      *) pos+=("$1"); shift ;;
    esac
  done
  [[ "$limit" =~ ^[0-9]+$ ]] && ((10#$limit >= 1 && 10#$limit <= SUGAR_MAX_LIMIT)) ||
    { fail "--limit 必须是 1..$SUGAR_MAX_LIMIT 的整数（当前: $limit）" "$E_ARGS"; return $?; }
  limit=$((10#$limit))
  local npos=${#pos[@]} sql bucket
  case "$cmd" in
    sql)
      ((npos == 1)) || { fail "用法: sugar_query.sh sql \"<SQL>\" [选项]" "$E_ARGS"; return $?; }
      sql="${pos[0]}"
      ;;
    tables)
      ((npos == 0)) || { fail "用法: sugar_query.sh tables" "$E_ARGS"; return $?; }
      sql="show tables"
      ;;
    columns)
      ((npos == 1)) || { fail "用法: sugar_query.sh columns <table>" "$E_ARGS"; return $?; }
      sugar_need_ident "表名" "${pos[0]}" || return $?
      sql="show columns from ${pos[0]}"
      ;;
    nodes)
      ((npos == 1)) || { fail "用法: sugar_query.sh nodes <cluster>" "$E_ARGS"; return $?; }
      sugar_need_ident "cluster" "${pos[0]}" || return $?
      sql="select distinct node from bgw_agent_node_cluster where \$__timeFilter(time) and cluster='${pos[0]}' order by node"
      ;;
    port)
      ((npos == 1)) || { fail "用法: sugar_query.sh port <node> [--port vnicN] [--by minute|hour|day]" "$E_ARGS"; return $?; }
      sugar_need_ident "node" "${pos[0]}" || return $?
      bucket="$(sugar_bucket_expr "$by")" || return $?
      sql="select $bucket t, port_name, sum(rx_pkts) rx_pkts, sum(rx_nombuf) rx_nombuf, sum(imissed) imissed, sum(rx_err) rx_err, sum(tx_err) tx_err from bgw_port_node where \$__timeFilter(time) and node='${pos[0]}'"
      if [[ -n "$port" ]]; then
        sugar_need_ident "port" "$port" || return $?
        sql+=" and port_name='$port'"
      fi
      sql+=" and rx_nombuf<2000000000 group by t, port_name order by t"
      ;;
    *)
      usage >&2
      fail "未知子命令: $cmd" "$E_ARGS"
      return $?
      ;;
  esac
  sugar_run "$sql" "$from" "$to" "$limit" "$raw"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
  exit $?
fi
