# 发布记录

本 skill 属于 tom-autodev 套件，整套共 12 个。端到端流程依赖套件内控制面、相位与所选项目/语言规则；职责见 SKILL.md，外部参考 skill 不再是运行时依赖。

## 2026-11-10

- 新增 `cluster <host> [--create-case ID]`：在已连接主机上只读执行 `get_service_by_host` / `get_instance_by_service`，列出所属 BNS 服务和全部同集群实例，可直接生成多机 case 做同类机器对照。
- 修复：命令输出没有末尾换行时结束 marker 粘在同一行，exec 误报"缺少退出码"（码 7）；三个解析函数改为也接受行尾 `marker rc`。
- 修复：下发命令含 `!`（如 awk `!~`）时远端 bash 历史展开报 `event not found`；下发和健康检查都先 `set +H`。
- 修复：`connect-many` 超过约 4 台时 `split-window` 空间不足报"无法探测 tmux pane"；每个角色改用独立 tmux window。
- 新增 `scripts/sugar_query.sh`：复用用户已登录的 ego-browser 会话，经 mtgrafana `ds/query` 对 sugar BGW 指标库做只读 SQL 查询（`sql`/`tables`/`columns`/`nodes`/`port`），SQL 只允许单条 select/show/desc 并强制 limit，未登录时交给用户扫码/指纹后退出码 5；`domains/bgw.md` 8.4 节记录常用表、字段与典型查询。
- 新增 `scripts/cmdb_query.py`：只读查询 BSP 基础服务 CMDB 开放接口（`/cmdb/open/v1`，HS256 JWT，凭据 `~/.config/tom-diagnose/bsp.env` 600 权限、不打印）——BGW 集群/节点、单机版本与 bgw.conf/zebra 配置、VIP 归属、VS 配置与 RS、VS 配置和 RS 变更历史（vslog 按小时分片）；`domains/bgw.md` 8.5 节记录用法与 ibgw44 案例结论。
- case 目标上限默认值 8 → 32（`TOM_AUTODEBUG_MAX_TARGETS`）。
- `domains/bgw.md` 新增第 8 节：rx_nombuf/imissed 计数语义（mlx5 批量累加、per-queue err）、RX mempool 规模、逐秒取差与 `--list-ratio`/`--check-timer` 找卡住的 worker、`set_cpu_affinity.sh` 把 heyeAgent 绑到 worker 核的绑核冲突、集群对照方法与易误判点（来自 2026-11-09 yq01-sys-ibgwcluster 实际定位）。
- evals 新增 3 条（rx_nombuf 定位、同集群对照、集群看板不等于单机因果），共 28 条。

## 2026-11-04

- 并入 tom-autodebug 1.5.0 的文档与评测：BGW 领域知识（原 `bgw-domain-knowledge.md` 与 README 中的 BGW 诊断顺序、controller 配置链路、累计计数增量、证据边界、BGW-1956 CDN URL Offer Hash 排障清单）迁移为 `references/domains/bgw.md`；如流群聊取证及降级规则迁移为 `references/infoflow-evidence.md`；VIP 不通模板、已知能力边界与踩过的坑、分阶段重试策略补入 `references/remote-troubleshooting.md`。
- description 增加中文触发场景：内网机器名/IP、登录看日志、网络/VIP/BGW 定位、如流群号/群链接结合群聊定位、Higress/irouter/MCP/如流机器人链路故障；SKILL.md 新增 “Select the domain” 小表。
- 新增 `references/domains/higress-mcp.md`：如流机器人 → irouter/BFE → Higress → MCP → 模型网关链路的逐跳绕过定位方法、2026-11-03/04 实测信号对照表、控制台只读 API 与证据边界。
- 新增 `evals/evals.json`：迁移 tom-autodebug 的 22 条评测（路径和 skill 名已更新），追加 3 条 Higress+MCP 场景（全路径 405、503 Connection refused、约 50 秒 BFE 500），共 25 条。
- 脚本未改动。tom-autodebug 自此下线，相关能力只在 tom-diagnose 维护。

## 2026-11-02

- DIAGNOSE 草案增加可选 `knowledge_candidates`：只收录有证据、跨 run 可复用的环境/流程结论，不收录业务代码结论；明确四字段、受限文档路径与索引/模板保留文件名校验，候选需在发布成功后另经 `KNOWLEDGE_WRITEBACK` 审批生成 CR。
- action 提供 `knowledge_workspace` 时，先用其 `show` 命令读取钉住版本的团队知识；同名内容优先于项目 skill，钉住 revision 的业务源码仍是最终事实。

## 2026-10-08

- 用户在 G1 后改变需求时保持 `USER_REQUIREMENT_CHANGED` 分类并使用 `route=GRILL`（或
  `STOPPED`）；控制面拒绝其他路由、改分类及非需求变更的 `GRILL` 路由。

## 2026-09-22

- 对齐修复后的控制器：REPAIR 提案尚无补丁时使用 `repair_diff_hash=null`；G6 审批提案，PLAN/IMPLEMENT 生成候选后重新取得 G5。
- 源码 Review 诊断的 build/stage/job 与环境字段为 null；流水线诊断保留真实失败身份，并校验 stage/job 归属。更新阶段合同，删除已解决的身份合同与非空 diff 限制。

## 2026-09-21

- 内置 tom-autodebug 1.5.0 的 relay/tmux、多机角色、TTL、进程取证脚本及原回归；保留原配置/状态目录以兼容已有用户，不再调用外部 skill。
- 分开流水线 diagnosis DraftContent 与独立远程取证报告；补根因证据、反证、时间线、清理和不支持的 transport 边界。
- 修复 Producer/Envelope 分工，明确当前 REPAIR 无 diff 时的 schema/controller 冲突，禁止虚构 hash 或越阶段写修复。

## 2026-09-07

- 首次发布到 OneTool 平台，skillId `28446`
- 发布范围由空间可见调整为广场可见
