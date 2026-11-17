# websub-feed 来源契约

来源清单中的资产长度与 SHA-256 用于核对本地原文字节。文档的 collection 与 protocol 必须匹配清单，现有记录类型包括 lease, article, revision, withdrawal。每条记录以 kind、id、version 定位，refs 指向同集合的其他记录，payload 原样留存。来源版本冲突会中断接收；本地引用闭合只表示当前收到的引用能定位，不表示远端序列或授权资料已经完整。

## 回调接收（`capture_data.callback`）

一次回调请求必须在同一事务里通过以下核对，任一失败即整份拒收、不产生通知：

- 请求签名：`X-Hub-Signature-256`（或 `X-Hub-Signature`）对**原始字节**的 HMAC，按订阅密钥计算；
- 内容摘要：若提供 `Digest` / `Content-Digest`，必须与原始字节一致；
- topic：请求体 topic 必须等于订阅登记的 topic；
- hub：请求体 hub 必须等于订阅登记的 hub；
- 租约：hub + lease_id 必须已登记且未过期、未停用，回调声明的有效期不得超出登记租约。

回调只写入 `notifications`，表示“值得重新抓取”。通知**不会**写入 `records`（article/revision 账本），因此重复投递不会变成新稿；漏掉的通知也不会因为后续晚到稿件而被掩盖。同字节重送按回调摘要幂等，仅累加 `deliveries`。

## 租约代际（`capture_data.lease`）

- 每个新租约（hub + lease_id 未见过）分配递增 `generation`；
- 续租交叠期间，尚未到期的旧代际状态为 `overlapping`，其回调仍按**旧代际**接收，不会误算成新稿；
- 旧代际过期或显式 `retire` 后，新回调拒收；
- 换 hub 通过 `set-hub` 更新订阅身份与密钥，已有代际、通知与源站证据全部保留；旧 hub 的回调在换 hub 后拒收；
- 每份通知按 `(notification, callback, generation)` 记录送达收据，因此同一通知在新旧代际下都能追溯。

## 源站回补（`capture_data.source`）

源站分页包含 `cursor_start`、`next_cursor`/`cursor_end`、`end_of_feed` 与事件列表；每个事件带 `id`、`revision`、`source`、`deleted`、`cursor`。分页与回调可以乱序到达：

- 重复页按摘要幂等；重叠页合并去重；
- 同一游标出现互斥证据时拒收该页，保留冲突页原始字节与拒收原因，水位不推进；
- 连续水位的定义是“从 0 开始可无缝衔接的已接受区间右端”；缺口区间写入 `source_pages` 之外由 `coverage()` 计算并展示；
- `end_of_feed` 只表示尾部已声明结束；`closed` 需要同时满足无缺口且水位到达前沿（frontier）。

文章状态由 `source_events` 按游标推导（`article_states`）：

- 已接受的最高游标事件决定发表/撤稿；
- 晚到的较低游标分页不会覆盖更高游标，因此撤稿不会被旧页复活；
- 全部修订/撤稿证据都保留，不被覆盖；
- 通知相对源站证据处于 `confirmed` / `refuted` / `pending`；只有能证明覆盖到通知游标时才可能 `refuted`，否则保持 `pending` 供运营追查。

## 运营视图（`capture_data.report.build`）

报告给出：租约代际与状态、回调接受/拒收/重投计数、通知状态分布、连续水位与 `unresolved_gaps`（尚未补齐的空档）、文章状态与证据、以及 `anomalies`（缺口、尾部未闭合、未证实通知、拒收回调/分页、交叠期旧代际通知）。

`fingerprint` 只对决定与证据做规范化摘要，不含接收时刻与投递次数：服务重启、hub 重送后指纹不变；若最终文章状态或原始证据发生矛盾，指纹会变化，便于对比发现。

## 已接收的协议资料

`inspect` 命令继续只核对冻结清单的本地身份与引用，不推断远端连续性；`callback` / `page` / `report` 子命令作用于持久化账本 `--db`。

同集合第二次来源接收保存在 data/conflict-case.json。冲突事务必须保持第一份收据和旧投影。
