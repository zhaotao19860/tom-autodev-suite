# Higress + MCP 链路排障方法

适用链路：如流机器人 → 桥接服务 → irouter 域名/BFE → Higress gateway → 路由/key-auth → 服务来源 → MCP 服务 → code agent/模型网关。

本文件只写方法和信号含义。irouter 域名、gateway NodePort、Higress 控制台、部署机等环境地址不写在这里，以环境文档（或 action 提供的 `knowledge_workspace`）为准；下文用 `<domain>`、`<node-ip>:<nodeport>`、`<console>`、`<route-host>`、`<port>` 等占位符。

## 1. 分层定位：从后往前逐跳绕过

从链路末端开始，每次绕过上一层直接打下一层。第一个不正常的跳就是故障层，上一个正常的跳是证据边界。全部为只读观测，请求体用固定的 `tools/list`，不调用会产生副作用的工具。

| 顺序 | 观测 | 正常时说明 |
|---|---|---|
| 1 | 在 MCP 服务所在机器本地：`curl -sS -XPOST localhost:<port>/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'` | MCP 服务本身监听且能列工具 |
| 2 | 绕过域名打 gateway NodePort：`curl -sS -XPOST http://<node-ip>:<nodeport>/<path> -H 'Host: <route-host>' -H '<key-header>: $KEY' ...` | Higress 路由、key-auth、服务来源和上游可达 |
| 3 | 走域名：`curl -sS -XPOST https://<domain>/<path> ...`（同样的头和体） | 域名/DNS/BFE 指向 gateway 且转发正常 |
| 4 | 对比不同路径/不同路由（如 `/oneapi`、`/qianfan`、一个不存在的路径） | 区分“单条路由问题”与“域名整体问题” |
| 5 | `curl -sS https://<domain>/` 用 GET 看返回内容 | 若返回 Higress 控制台或其他网关页面 HTML，说明域名后端不是 gateway |

远端主机上的观测（第 1 步、部署机日志、进程和端口）用本 skill 的 relay/tmux 工具 `scripts/autodebug.sh` 执行，遵守 [remote-operations.md](../remote-operations.md) 的只读、超时和确认规则。key 只从环境变量引用（如 `$KEY`），不要把明文写进命令记录、报告或文件。

## 2. 信号对照表

以下信号均为 2026-11-03/04 实测。

| 信号 | 含义 | 下一步区分观测 |
|---|---|---|
| 401 | key-auth 拒绝：缺 key、key 错，或消费者不在该路由的 allow 列表 | 用控制台 API 只读核对路由的 key-auth 配置与消费者列表 |
| 405，且所有路径（含 `/oneapi`、`/qianfan` 和不存在的路径）都 405，GET 返回 Higress 控制台 HTML | 域名后端不是 gateway（例如被指到控制台），所有路由受影响 | NodePort 直连同一请求返回 200 即可确认 |
| 503 `upstream connect error ... Connection refused` | Higress 已转发，上游端口无人监听（服务没起或端口配错） | 查服务来源/服务的目标端口，在部署机上 `ss -lntp` 核对监听，再做第 1 步本机 curl |
| 500 `Server: bfe`，约 50 秒返回 | 域名前的 BFE 超时断开；NodePort 直连长请求正常 | 用 NodePort 直连同一长请求对比耗时。对策：服务端改异步（提交返回 `job_id`，再用 `get_answer` 轮询），保证单次调用 < 40s |
| 长连接已连上，但收不到群 @消息 | 机器人未开“接收群聊中@机器人消息”权限；或同一应用有多个长连接客户端（集群投递，随机投给其中一个，会互相抢消息） | 核对机器人权限；在所有部署机上查同一应用的长连接进程数。注意长连接切换后不能切回短连接 |
| `claude -p --bare` 返回 "Not logged in" | bare 模式不读 settings/apiKeyHelper | 显式传 `ANTHROPIC_BASE_URL`、`ANTHROPIC_API_KEY` 环境变量后重试 |
| 某模型 `is_error`，或只回答“没有 Grep 工具” | 模型网关下该模型的工具调用兼容性问题 | 用固定冒烟题验证（让 agent grep 一个已知宏定义），换模型对比 |

## 3. Higress 控制台只读 API

```bash
# 凭据只从环境变量读，禁止写进文件、命令历史回显或日志
curl -sS -c /tmp/higress.cookie -XPOST "<console>/session/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$HIGRESS_USER\",\"password\":\"$HIGRESS_PASSWORD\"}"
curl -sS -b /tmp/higress.cookie "<console>/v1/routes"
curl -sS -b /tmp/higress.cookie "<console>/v1/service-sources"
curl -sS -b /tmp/higress.cookie "<console>/v1/consumers"
curl -sS -b /tmp/higress.cookie "<console>/v1/services"
curl -sS -b /tmp/higress.cookie "<console>/v1/domains"
```

用完删除 cookie 文件。报告中引用消费者时只写名称，不写 key 值。改路由、改消费者、改服务来源都是写操作，必须先展示变更内容和影响并取得用户授权。

## 4. 证据边界

- 域名、DNS、BFE 属于外部团队。证据指向这一层时，结论写“域名后端疑似不是 gateway（或 BFE 超时），需负责人确认”，附 NodePort 直连与域名访问的对比结果，不要自己改。
- NodePort 直连正常只证明 gateway 及以后正常，不证明桥接服务和机器人侧正常；反之亦然，逐跳给出结论。
- 单一路径 405/404 不代表域名整体问题，必须对比多个路径和 GET 返回内容后再下结论。
- 模型兼容性结论只针对所测模型与网关版本，用固定冒烟题复现后再写入报告。
