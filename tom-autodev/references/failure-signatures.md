# iPipe 故障签名 v2

新故障 occurrence 使用 `ipipe-failure:v2:<sha256>`。签名区分完整的错误码、测试 case ID、异常类型及其大小写，不再按数字或 hex 长度删词，也不在 200 字处截掉可能用于区分根因的消息后缀。运行时原始 job message 仍有既存的 1024 字上限。

消噪限定为带明确 `build`、`build_id`、`request`、`request_id`、`req`、`rev`、`revision`、`commit`、`hash`、`sha` 等标签的标识值，以及 UUID、ISO 时间和已知构建根目录中的路径。裸错误码、case ID 和异常名称不因“看起来像 hash”而被吞掉。已知构建根覆盖常见 Linux CI 根（`/work`、`/workspace`、`/build`、`/tmp`、`/var/tmp`、`/var/lib`、`/home`、`/opt`、`/data`、`/srv`、`/mnt`、`/ssd*`、`/nvme*`、`/jenkins`、`/ci`、`/runner`、`/agent` 等；baidu BGW checkout 在 `/home` 与 `/ssd*`），站点新增根可扩展 `_BUILD_ROOT`。构建路径保留源码相对目录、文件名和 pytest `::case` 后缀，但任何携带数字、UUID 或时间戳的目录段（易变的 checkout/构建目录，不限于根下第一段）都归一为 `<build-id>`，使同一根因不因 CI 根或嵌套构建目录不同而分裂签名；普通路径不全量抹除。

## 已冻结的故障

旧 run 的 evidence、审批和 FailureCase 继续引用原签名。重复监控同一已落盘 stage occurrence 时复用已冻结的签名，避免给同一次故障换版本，或改写 `ipipe.stage-failure` 不可变记录。部分 stage 已落盘的检查点恢复时，同一 occurrence 的其余 stage 使用该一致签名；若已有 stage 的签名互相冲突，返回 `FAILURE_SIGNATURE_CONFLICT`，不任意挑选。新的 stage/build occurrence 没有旧冻结记录时使用 v2。

## 历史保留与迁移界限

升级不修改、不删除旧 `failure_cases` 行，也不把新 v2 签名自动关联到旧 digest。旧算法可能将多个根因归到同一 digest，仅凭聚合 hash、计数和 run 集无法证明其中哪次属于哪个新根因。

因此，**v2 新故障的跨运行累计与旧签名累计分开**。旧故障历史仍在库中，可通过以下只读工具清点。该命令使用 SQLite `mode=ro`，不会创建数据库、调用 StateStore 的 schema 迁移或写入状态：

```sh
python3 tom-autodev/scripts/failure_signature_inventory.py /path/to/state.sqlite
```

输出保留所有行、run 集、次数、解决状态和首末发生信息，并将未带版本的 digest 标为 `RAW_OCCURRENCE_REPLAY_REQUIRED`。未带版本的 digest 也可能来自其他 producer，须通过证据确认来源。

要回填到 v2，必须取得**每次 occurrence 的原始 pipeline/module、失败 stage 的稳定身份、完整 job message、run 与 occurrence 身份，以及解决事件的时间顺序**。逐次重算、按新根因拆组、检查是否与当前累计重复，并保存旧到新映射的证据后，才能通过独立且明确的迁移实施回填。缺失原始消息时只能保留历史待查，不能宣称无损迁移；归档 IPIPE 结构化 evidence 和截断的通知摘要通常不足以重建这些原始输入。

本次提供清单和迁移边界，不自动执行历史回填。历史聚合表与旧冻结 evidence 原样保留。
