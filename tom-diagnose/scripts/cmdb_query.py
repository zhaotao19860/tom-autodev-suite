#!/usr/bin/env python3
"""cmdb_query: BSP CMDB open API 只读查询工具（BGW 集群/节点/主机、NLB VIP/VS/VS 变更日志）。

仅使用 python3 标准库。凭据从 ~/.config/tom-diagnose/bsp.env 读取（可用环境变量
TOM_DIAGNOSE_BSP_ENV 覆盖路径），文件权限必须为 600。密钥与 token 不会出现在任何输出中。

退出码：0 成功，2 参数错误，3 API 错误，4 网络/超时，5 凭据问题。
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

VERSION = "1.0.0"

E_OK, E_ARGS, E_API, E_NET, E_CRED = 0, 2, 3, 4, 5

CST = timezone(timedelta(hours=8))
SAFE_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
JWT_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?")
DEFAULT_ENV = "~/.config/tom-diagnose/bsp.env"
ENV_KEYS = ("BSP_USER_ID", "BSP_USER_SECRET", "BSP_JWT_ISSUER", "BSP_JWT_EXPIRE",
            "BSP_JWT_HEADER", "BSP_ENDPOINT")
REQUIRED_KEYS = ("BSP_USER_ID", "BSP_USER_SECRET", "BSP_JWT_ISSUER", "BSP_ENDPOINT")
API_PREFIX = "/cmdb/open/v1"
DEFAULT_TIMEOUT = 60.0
DEFAULT_CHUNK = 3600
MAX_RANGE = 7 * 86400
PROTO_NUM = {"tcp": 1, "udp": 2}
PROTO_NAME = {1: "TCP", 2: "UDP"}

# 运行期登记的敏感值（密钥、token），所有对外文本都会过一遍 redact()
_SENSITIVE = set()


class CmdbError(Exception):
    """带退出码的错误；消息在打印前会再次脱敏。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _register_sensitive(value):
    if value and len(value) >= 4:
        _SENSITIVE.add(value)


def redact(text):
    """去掉文本里的已知敏感值以及任何形似 JWT 的串。"""
    if text is None:
        return ""
    text = str(text)
    for value in sorted(_SENSITIVE, key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    return JWT_LIKE_RE.sub("<redacted-token>", text)


# ---------------------------------------------------------------- 凭据

def env_path():
    return os.path.expanduser(os.environ.get("TOM_DIAGNOSE_BSP_ENV") or DEFAULT_ENV)


def _cred_hint(path):
    return ("请创建该文件并设置权限 600（chmod 600 %s），每行一个 KEY=VALUE，需要的键：%s"
            % (path, ", ".join(ENV_KEYS)))


def load_credentials(path=None):
    path = path or env_path()
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise CmdbError(E_CRED, "凭据文件不存在：%s。%s" % (path, _cred_hint(path)))
    except OSError as exc:
        raise CmdbError(E_CRED, "无法访问凭据文件 %s：%s。%s"
                        % (path, exc.strerror, _cred_hint(path)))
    if st.st_mode & 0o077:
        raise CmdbError(E_CRED, "凭据文件权限过宽（当前 %s，组或其他用户可读）：%s。%s"
                        % (oct(st.st_mode & 0o777), path, _cred_hint(path)))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        raise CmdbError(E_CRED, "读取凭据文件失败：%s（%s）" % (path, exc.strerror))
    creds = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        creds[key] = value
    _register_sensitive(creds.get("BSP_USER_SECRET"))
    missing = [k for k in REQUIRED_KEYS if not creds.get(k)]
    if missing:
        raise CmdbError(E_CRED, "凭据文件 %s 缺少键：%s。%s"
                        % (path, ", ".join(missing), _cred_hint(path)))
    expire = creds.get("BSP_JWT_EXPIRE") or "3600"
    if not expire.isdigit() or int(expire) <= 0:
        raise CmdbError(E_CRED, "凭据文件 %s 中 BSP_JWT_EXPIRE 必须是正整数（秒）" % path)
    creds["BSP_JWT_EXPIRE"] = expire
    creds["BSP_JWT_HEADER"] = creds.get("BSP_JWT_HEADER") or "Authorization"
    creds["BSP_ENDPOINT"] = creds["BSP_ENDPOINT"].rstrip("/")
    return creds


# ---------------------------------------------------------------- JWT

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _user_id_value(user_id):
    return int(user_id) if user_id.isdigit() else user_id


def make_jwt(creds, now=None):
    now = int(time.time()) if now is None else int(now)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "userId": _user_id_value(creds["BSP_USER_ID"]),
        "iss": creds["BSP_JWT_ISSUER"],
        "iat": now,
        "nbf": now,
        "exp": now + int(creds["BSP_JWT_EXPIRE"]),
    }
    signing_input = "%s.%s" % (
        _b64url(json.dumps(header, separators=(",", ":")).encode()),
        _b64url(json.dumps(payload, separators=(",", ":")).encode()),
    )
    key = "%s|%s|2" % (creds["BSP_USER_ID"], creds["BSP_USER_SECRET"])
    sig = hmac.new(key.encode(), signing_input.encode(), hashlib.sha256).digest()
    token = "%s.%s" % (signing_input, _b64url(sig))
    _register_sensitive(token)
    return token
# ---------------------------------------------------------------- HTTP

class Client:
    def __init__(self, creds, timeout=DEFAULT_TIMEOUT):
        self.creds = creds
        self.timeout = timeout
        self.base = creds["BSP_ENDPOINT"] + API_PREFIX

    def request(self, method, path, params=None, body=None):
        url = self.base + path
        if params:
            clean = [(k, v) for k, v in params.items() if v is not None and v != ""]
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers[self.creds["BSP_JWT_HEADER"]] = make_jwt(self.creds)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - 读 body 失败不应掩盖 HTTP 状态
                text = ""
            finally:
                exc.close()
            raise CmdbError(E_API, "HTTP %s %s（%s %s）：%s"
                            % (exc.code, exc.reason or "", method, path, redact(text)[:300]))
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise CmdbError(E_NET, "网络错误或超时（%s %s，timeout=%ss）：%s"
                            % (method, path, self.timeout, redact(reason)))
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise CmdbError(E_API, "响应不是合法 JSON（%s %s）：%s"
                            % (method, path, redact(raw[:300].decode("utf-8", "replace"))))
        if not isinstance(obj, dict):
            raise CmdbError(E_API, "响应格式异常（%s %s）" % (method, path))
        if obj.get("code"):
            raise CmdbError(E_API, redact("%s: %s" % (obj.get("code"), obj.get("message") or "")))
        return obj.get("data")


# ---------------------------------------------------------------- 工具函数

def check_safe(value, what):
    if not isinstance(value, str) or not SAFE_RE.match(value):
        raise CmdbError(E_ARGS, "%s 含非法字符：%r（只允许字母、数字和 . _ : -）" % (what, value))
    return value


def parse_time(text, now=None):
    """epoch 秒、now、now-<N>[smhd]、或 YYYY-MM-DD HH:MM[:SS]（按 Asia/Shanghai，UTC+8）。"""
    now = int(time.time()) if now is None else int(now)
    s = (text or "").strip()
    if s == "now":
        return now
    m = re.fullmatch(r"now-(\d+)([smhd])", s)
    if m:
        return now - int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if re.fullmatch(r"\d{9,11}", s):
        return int(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=CST).timestamp())
        except ValueError:
            continue
    raise CmdbError(E_ARGS, "无法解析时间：%r（支持 epoch 秒、now、now-2h、'YYYY-MM-DD HH:MM[:SS]'）" % text)


def _epoch(value):
    """把记录里的时间字段转成 epoch 秒；无法识别返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        return v // 1000 if v > 10 ** 12 else v
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"\d+", s):
            return _epoch(int(s))
        try:
            return parse_time(s)
        except CmdbError:
            return None
    return None


def fmt_time(value):
    ts = _epoch(value)
    if ts is None:
        return str(value)
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:%M:%S") + "(+08)"


def _cell(value):
    if value is None or value == "":
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).replace("\n", " ")


def print_table(headers, rows, out=None):
    out = out or sys.stdout
    cells = [[_cell(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))
    out.write("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip() + "\n")
    for row in cells:
        out.write("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip() + "\n")


def print_json(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _list_of(data):
    if isinstance(data, dict):
        lst = data.get("list")
        return lst if isinstance(lst, list) else []
    return data if isinstance(data, list) else []


def proto_name(value):
    try:
        return PROTO_NAME.get(int(value), str(value))
    except (TypeError, ValueError):
        return _cell(value)
# ---------------------------------------------------------------- 子命令

def cmd_cluster(client, args):
    if args.name:
        check_safe(args.name, "集群名")
        data = client.request("GET", "/bgw/cluster", {"cluster": args.name})
        if args.json:
            print_json(data)
            return E_OK
        items = _list_of(data)
        if not items:
            raise CmdbError(E_API, "未找到集群：%s" % args.name)
        for i, item in enumerate(items):
            if i:
                sys.stdout.write("\n")
            for key in sorted(item):
                sys.stdout.write("%s: %s\n" % (key, _cell(item[key])))
        return E_OK
    data = client.request("GET", "/bgw/cluster")
    if args.json:
        print_json(data)
        return E_OK
    names = sorted(str(c.get("name")) for c in _list_of(data))
    for name in names:
        sys.stdout.write(name + "\n")
    sys.stderr.write("共 %d 个集群\n" % len(names))
    return E_OK


def cmd_nodes(client, args):
    check_safe(args.cluster, "集群名")
    data = client.request("GET", "/bgw/node", {"cluster": args.cluster})
    if args.json:
        print_json(data)
        return E_OK
    items = _list_of(data)
    print_table(["name", "status", "version", "capacity"],
                [[n.get("name"), n.get("status"), n.get("version"), n.get("capacity")]
                 for n in sorted(items, key=lambda n: str(n.get("name")))])
    sys.stderr.write("共 %d 个节点\n" % len(items))
    return E_OK


def cmd_host(client, args):
    for h in args.hostnames:
        check_safe(h, "主机名")
    data = client.request("POST", "/host/service",
                          body={"project": 1, "hostnameList": list(args.hostnames)})
    if args.json:
        print_json(data)
        return E_OK
    items = _list_of(data)
    rows = []
    for it in items:
        svc = it.get("service") or {}
        rows.append([it.get("hostname"), it.get("ip"), it.get("cluster"), svc.get("status"),
                     svc.get("subVersion"), svc.get("timeVersion"), svc.get("capacity")])
    print_table(["hostname", "ip", "cluster", "status", "subVersion", "timeVersion", "capacity"], rows)
    found = {it.get("hostname") for it in items}
    missing = [h for h in args.hostnames if h not in found]
    if missing:
        sys.stderr.write("未返回的主机：%s\n" % " ".join(missing))
    for it in items:
        svc = it.get("service") or {}
        if args.show_config:
            sys.stdout.write("\n===== %s config =====\n%s\n" % (it.get("hostname"), svc.get("config") or ""))
        if args.show_route:
            sys.stdout.write("\n===== %s routeConfig =====\n%s\n"
                             % (it.get("hostname"), svc.get("routeConfig") or ""))
    return E_OK


def cmd_vip(client, args):
    check_safe(args.vip, "VIP")
    data = client.request("GET", "/nlb/vip/info", {"vip": args.vip})
    if args.json:
        print_json(data)
        return E_OK
    if not isinstance(data, dict) or not data:
        raise CmdbError(E_API, "未找到 VIP：%s" % args.vip)
    for key in ("vip", "ipType", "networkType", "networkMode", "isp", "isAnycast"):
        sys.stdout.write("%s: %s\n" % (key, _cell(data.get(key))))
    instances = data.get("instanceList") or []
    sys.stdout.write("instances (%d):\n" % len(instances))
    if instances:
        print_table(["instanceId", "accountId", "name", "region", "zone", "cluster", "owners"],
                    [[i.get("instanceId"), i.get("accountId"), i.get("name"), i.get("region"),
                      i.get("zone"), i.get("cluster"),
                      ",".join(str(o) for o in (i.get("ownerList") or [])) or None]
                     for i in instances])
    return E_OK


def filter_vs(items, port=None, protocol=None):
    out = []
    want_proto = PROTO_NUM.get(protocol) if protocol else None
    for vs in items:
        if port is not None:
            try:
                if int(vs.get("vport")) != port:
                    continue
            except (TypeError, ValueError):
                continue
        if want_proto is not None:
            try:
                if int(vs.get("protocol")) != want_proto:
                    continue
            except (TypeError, ValueError):
                continue
        out.append(vs)
    return out


def print_vs(items, show_rs=False, show_config=False):
    rows = []
    for vs in items:
        rs = vs.get("rsList")
        rows.append([vs.get("vsId"), "%s:%s" % (vs.get("vip"), vs.get("vport")),
                     proto_name(vs.get("protocol")), vs.get("cluster"),
                     len(rs) if isinstance(rs, list) else None])
    print_table(["vsId", "vip:vport", "proto", "cluster", "rs_count"], rows)
    for vs in items:
        if not (show_rs or show_config):
            break
        sys.stdout.write("\n[vs %s %s:%s %s %s]\n" % (vs.get("vsId"), vs.get("vip"), vs.get("vport"),
                                                     proto_name(vs.get("protocol")), vs.get("cluster")))
        if show_config:
            cfg = vs.get("config") or {}
            if isinstance(cfg, dict):
                for key in sorted(cfg):
                    sys.stdout.write("  cfg %s=%s\n" % (key, _cell(cfg[key])))
            else:
                sys.stdout.write("  cfg %s\n" % _cell(cfg))
        if show_rs:
            for rs in vs.get("rsList") or []:
                sys.stdout.write("  rs %s:%s weight=%s\n" % (rs.get("rip"), rs.get("rport"), rs.get("weight")))


def cmd_vs(client, args):
    if args.vip:
        check_safe(args.vip, "VIP")
        if args.rport is not None:
            raise CmdbError(E_ARGS, "--rport 只能与 --rip 一起使用")
        if args.cluster:
            check_safe(args.cluster, "集群名")
        params = {"vip": args.vip, "cluster": args.cluster,
                  "isReturnConfig": 1 if args.config else 0,
                  "isReturnRS": 1, "isReturnMasterVS": 0}
        data = client.request("GET", "/nlb/vs-via-vip", params)
        items = filter_vs(_list_of(data), args.port, args.protocol)
    else:
        check_safe(args.rip, "RIP")
        if args.port is not None or args.protocol or args.cluster:
            raise CmdbError(E_ARGS, "--port/--protocol/--cluster 只能与 --vip 一起使用")
        params = {"rip": args.rip, "rport": args.rport}
        data = client.request("GET", "/nlb/vs-via-rip", params)
        items = _list_of(data)
    if args.json:
        if isinstance(data, dict):
            data = dict(data, list=items)
        print_json(data)
        return E_OK
    print_vs(items, args.rs, args.config)
    sys.stderr.write("共 %d 个 VS\n" % len(items))
    return E_OK
def _type_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def format_vslog_record(rec):
    cfg_parts = []
    for c in rec.get("config") or []:
        t, name = _type_int(c.get("type")), c.get("name")
        if t == 1:
            cfg_parts.append("+%s=%s" % (name, _cell(c.get("value"))))
        elif t == 2:
            cfg_parts.append("~%s: %s->%s" % (name, _cell(c.get("originValue")), _cell(c.get("value"))))
        elif t == 3:
            cfg_parts.append("-%s" % name)
        else:
            cfg_parts.append("?%s=%s" % (name, _cell(c.get("value"))))
    rs_parts = []
    for r in rec.get("rs") or []:
        t = _type_int(r.get("type"))
        ep = "%s:%s" % (r.get("rip"), r.get("rport"))
        if t == 1:
            rs_parts.append("+%s(%s)" % (ep, r.get("weight")))
        elif t == 2:
            rs_parts.append("~%s(%s)" % (ep, r.get("weight")))
        elif t == 3:
            rs_parts.append("-%s" % ep)
        else:
            rs_parts.append("?%s(%s)" % (ep, r.get("weight")))
    parts = [fmt_time(rec.get("startTime"))]
    if cfg_parts:
        parts.append("cfg[%s]" % " / ".join(cfg_parts))
    if rs_parts:
        parts.append("rs[%s]" % " ".join(rs_parts))
    if len(parts) == 1:
        parts.append("(无变更明细)")
    return " ".join(parts)


def summarize_vslog(records):
    """按小时（UTC+8）统计：记录数、RS 增/删/改、配置变更数。"""
    buckets = {}
    keys = ("records", "rs_add", "rs_del", "rs_upd", "cfg")
    for rec in records:
        ts = _epoch(rec.get("startTime"))
        hour = (datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:00")
                if ts is not None else "unknown")
        b = buckets.setdefault(hour, dict.fromkeys(keys, 0))
        b["records"] += 1
        b["cfg"] += len(rec.get("config") or [])
        for r in rec.get("rs") or []:
            t = _type_int(r.get("type"))
            b[{1: "rs_add", 2: "rs_upd", 3: "rs_del"}.get(t, "rs_upd")] += 1
    total = dict.fromkeys(keys, 0)
    for b in buckets.values():
        for k in keys:
            total[k] += b[k]
    return buckets, total


def plan_vslog(args, now=None):
    """校验 vslog 参数，返回 (公共查询参数, [(start, end), ...])。"""
    start = parse_time(args.time_from, now)
    end = parse_time(args.time_to, now)
    if end <= start:
        raise CmdbError(E_ARGS, "--to 必须晚于 --from")
    if end - start > MAX_RANGE and not args.force_range:
        raise CmdbError(E_ARGS, "时间范围 %.1f 天超过 7 天上限；确需查询请加 --force-range"
                        % ((end - start) / 86400.0))
    if args.chunk <= 0:
        raise CmdbError(E_ARGS, "--chunk 必须是正整数（秒）")
    if args.vs_id is not None:
        if args.vip or args.port is not None or args.protocol:
            raise CmdbError(E_ARGS, "--vs-id 与 --vip/--port/--protocol 只能二选一")
        base = {"vsId": args.vs_id}
    else:
        if not (args.vip and args.port is not None and args.protocol):
            raise CmdbError(E_ARGS, "需要 --vs-id，或同时给出 --vip --port --protocol")
        check_safe(args.vip, "VIP")
        if args.cluster:
            check_safe(args.cluster, "集群名")
        base = {"vip": args.vip, "vport": args.port, "protocol": PROTO_NUM[args.protocol],
                "vni": args.vni if args.vni is not None else -1, "cluster": args.cluster}
    chunks = []
    s = start
    while s < end:
        chunks.append((s, min(s + args.chunk, end)))
        s += args.chunk
    return base, chunks


def cmd_vslog(client, args, now=None):
    base, chunks = plan_vslog(args, now)
    records = []
    prev_seen = set()
    for idx, (cs, ce) in enumerate(chunks, 1):
        t0 = time.time()
        data = client.request("GET", "/nlb/vs/log", dict(base, startTime=cs, endTime=ce))
        got = _list_of(data)
        seen = set()
        added = 0
        for rec in got:
            key = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            seen.add(key)
            if key in prev_seen:  # 分段边界上可能重复返回同一条
                continue
            records.append(rec)
            added += 1
        prev_seen = seen
        sys.stderr.write("[vslog] 分段 %d/%d %s ~ %s：%d 条（%.1fs）\n"
                         % (idx, len(chunks), fmt_time(cs), fmt_time(ce), added, time.time() - t0))
    if args.json:
        print_json({"total": len(records), "list": records})
        return E_OK
    if args.summary:
        buckets, total = summarize_vslog(records)
        rows = [[h, b["records"], b["rs_add"], b["rs_del"], b["rs_upd"], b["cfg"]]
                for h, b in sorted(buckets.items())]
        rows.append(["TOTAL", total["records"], total["rs_add"], total["rs_del"],
                     total["rs_upd"], total["cfg"]])
        print_table(["hour(+08)", "records", "rs_add", "rs_del", "rs_upd", "cfg_changes"], rows)
        return E_OK
    for rec in records:
        sys.stdout.write(format_vslog_record(rec) + "\n")
    sys.stderr.write("共 %d 条变更记录\n" % len(records))
    return E_OK
# ---------------------------------------------------------------- CLI

def _port(value):
    try:
        p = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("端口必须是整数：%r" % value)
    if not 0 <= p <= 65535:
        raise argparse.ArgumentTypeError("端口超出范围 0-65535：%d" % p)
    return p


def _positive_float(value):
    try:
        v = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是数字：%r" % value)
    if v <= 0:
        raise argparse.ArgumentTypeError("必须大于 0：%r" % value)
    return v


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出原始 data JSON")
    common.add_argument("--timeout", type=_positive_float, default=argparse.SUPPRESS,
                        help="HTTP 超时秒数（默认 %d）" % DEFAULT_TIMEOUT)

    p = argparse.ArgumentParser(
        prog="cmdb_query.py",
        description="BSP CMDB open API 只读查询（凭据：%s，权限 600）" % DEFAULT_ENV)
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    p.add_argument("--json", action="store_true", help="输出原始 data JSON")
    p.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT,
                   help="HTTP 超时秒数（默认 %d）" % DEFAULT_TIMEOUT)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sp = sub.add_parser("cluster", parents=[common], help="单个集群信息；不带参数列出全部集群名")
    sp.add_argument("name", nargs="?")
    sp.set_defaults(func=cmd_cluster)

    sp = sub.add_parser("nodes", parents=[common], help="集群节点表")
    sp.add_argument("cluster")
    sp.set_defaults(func=cmd_nodes)

    sp = sub.add_parser("host", parents=[common], help="主机 BGW 服务信息")
    sp.add_argument("hostnames", nargs="+", metavar="HOSTNAME")
    sp.add_argument("--show-config", action="store_true", help="输出完整 config 文本")
    sp.add_argument("--show-route", action="store_true", help="输出完整 routeConfig 文本")
    sp.set_defaults(func=cmd_host)

    sp = sub.add_parser("vip", parents=[common], help="VIP 信息与实例")
    sp.add_argument("vip")
    sp.set_defaults(func=cmd_vip)

    sp = sub.add_parser("vs", parents=[common], help="按 VIP 或 RIP 查 VS")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--vip")
    g.add_argument("--rip")
    sp.add_argument("--port", type=_port, help="VIP 端口（客户端过滤）")
    sp.add_argument("--protocol", choices=sorted(PROTO_NUM), help="协议（客户端过滤）")
    sp.add_argument("--cluster")
    sp.add_argument("--rport", type=_port, help="RS 端口（仅 --rip）")
    sp.add_argument("--rs", action="store_true", help="列出 RS")
    sp.add_argument("--config", action="store_true", help="列出 VS 配置")
    sp.set_defaults(func=cmd_vs)

    sp = sub.add_parser("vslog", parents=[common], help="VS 变更日志（按时间分段顺序查询）")
    sp.add_argument("--vs-id", type=int)
    sp.add_argument("--vip")
    sp.add_argument("--port", type=_port)
    sp.add_argument("--protocol", choices=sorted(PROTO_NUM))
    sp.add_argument("--cluster")
    sp.add_argument("--vni", type=int, help="VNI（默认 -1）")
    sp.add_argument("--from", dest="time_from", required=True,
                    help="开始时间：epoch 秒 / now / now-2h / 'YYYY-MM-DD HH:MM[:SS]'(UTC+8)")
    sp.add_argument("--to", dest="time_to", required=True, help="结束时间，格式同 --from")
    sp.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="分段秒数（默认 3600）")
    sp.add_argument("--summary", action="store_true", help="按小时汇总")
    sp.add_argument("--force-range", action="store_true", help="允许超过 7 天的范围")
    sp.set_defaults(func=cmd_vslog)
    return p


def validate_args(args):
    for attr, what in (("name", "集群名"), ("cluster", "集群名"), ("vip", "VIP"), ("rip", "RIP")):
        value = getattr(args, attr, None)
        if value:
            check_safe(value, what)
    for h in getattr(args, "hostnames", None) or []:
        check_safe(h, "主机名")
    if args.command == "vslog":
        plan_vslog(args)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)  # 参数类错误在读凭据前先报出
        creds = load_credentials()
        client = Client(creds, timeout=args.timeout)
        return args.func(client, args)
    except CmdbError as exc:
        sys.stderr.write("错误：%s\n" % redact(exc.message))
        return exc.code
    except KeyboardInterrupt:
        sys.stderr.write("已中断\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
