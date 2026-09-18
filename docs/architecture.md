# 架构

本文描述当前代码；Commbitz 采购适配器已实现，真实扣款与交付验收仍待完成。目标资金流、采购状态及开发改造见 [转售 Bot 开发方案](reseller-bot-development.md)。

## 模块划分

```
shop_bot/
├── config.py           # pydantic-settings：config.yaml + SHOP_BOT_* 环境变量
├── models.py           # Product / Order / OrderStatus（StrEnum）
├── db.py               # aiosqlite 连接 + DAO（users / products / orders / order_events / fsm_state）
│                       # 含 FSMStorage：SQLite 持久化 FSM 存储，重启后对话状态恢复
├── keyboards.py        # 内联键盘（目录、确认、Web App 支付按钮）
├── logging_config.py   # 日志系统（彩色开发格式 + JSON 生产格式，按天轮转）
├── handlers/
│   ├── start.py        # /start、主菜单、我的订单、/query 查支付状态（触发一次履约推进）
│   ├── catalog.py      # 商品目录浏览
│   ├── order.py        # FSM 下单流程（按业务类型采集 ICCID/号码/天数 → 确认 → 支付链接）
│   ├── kyc.py          # /kyc 私聊补交证件（multipart/JSON）、/usage eSIM 用量
│   └── admin.py        # /orders、/paid（含实体卡确认发货）、/cancel、/purchases、/retry、/bind
├── services/
│   ├── orders.py       # 收款确认（pending → paid + 建采购任务），与履约分离
│   ├── purchasing.py   # 采购状态机（提交一次/详情轮询/KYC 等待/未知转人工）+ Demo/Commbitz 双模式
│   ├── fulfillment.py  # 买家私信（分条）+ 恢复循环（采购推进 + 通知补发）
│   ├── epay.py         # EPay 支付网关协议（MD5 签名、支付链接、回调验证、订单查询）
│   ├── commbitz_api.py # Commbitz 分销 API 客户端（令牌/目录/详情/采购/KYC/用量）
│   └── catalog_sync.py # 上游套餐同步为本地商品（SKU 映射；新商品 0 价下架待人工定价）
└── web/
    └── payment.py      # EPay 回调端点（验签核单 → 确认收款 → 快速应答）
```

## 数据流

```
用户 ──/start──> bot ──> 商品目录
  │
  └─选商品 ──> FSM 确认数量 ──> 创建订单 ──> 锁定在线渠道 ──> 生成 EPay 支付链接
                                              │
                                              v
                                    Web App 打开 EPay 收银台
                                              v
                                    用户完成支付
                                              v
EPay 网关 ──GET/POST /payment/callback──> 验签核单 ──> confirm_epay_payment() 确认收款
                                              │          + purchases 建任务（ready）
                                              v
                                    立即应答 success（规则 2）
                                              │  后台恢复循环（5s）
                                              v
                          CommbitzPurchaser.fulfill()：
                            ready → submitting（留痕）→ POST /v1/request
                            → 立即保存上游 _id（upstream_pending）
                            → GET /v1/details/:id 轮询 → 货品持久化
                                              │
                              ┌───────────────┴──────────────┐
                              v                              v
                        delivered + 通知买家         refund_pending → 同币种退款并关单
                                              submission_unknown → /purchases 人工核对
                                                      → /bind 绑定 或 /refund 退款
```

## 订单生命周期

订单收款状态（orders.status）与采购状态（purchases.state）分开保存；收款事实一旦落库，
采购等待、KYC、通知失败都不会让订单退回未付款。

```
pending_payment --epay_callback--> paid --采购+交付--> delivered
      |                                |                  （订单与采购终态同一事务落账）
      |                                +--履约需人工--> awaiting_dispatch（实体卡，/dispatch 确认）
      |                                +--上游明确拒绝--> refunded（自动退款到买家余额，终态）
      +--cancel--> cancelled
```

采购状态机（`services/purchasing.py`，规则详见 [开发方案](reseller-bot-development.md) 5.3/6 节）：

```
ready → submitting → upstream_pending → fulfilled
            │              │
            │              ├── awaiting_kyc → kyc_submitted（INR/强制 KYC，审核释放后才交付）
            │              └── awaiting_dispatch（实体 SIM 受理成功 ≠ 已发货）
            └── 超时/5xx/缺 _id → submission_unknown（钱货不明，人工核对后 /bind 或 /refund）
     4xx 明确拒绝 → refund_pending → 订单与采购 refunded（同币种退款、账本和关单原子提交）
```

- 用户下单后先选择余额或在线支付；在线渠道在生成链接前持久化，此后同一订单不能扣余额
- Web App 直接打开 EPay 收银台（`submit.php`），用户在 Telegram 内完成支付
- 支付成功后，EPay 网关 GET 或 POST 到 `/payment/callback`，带 MD5 签名
- 回调只做验签、核单、确认收款并建立采购任务（ready），随即应答（规则 2）
- 后台恢复循环（5 秒）驱动采购：提交一次先留痕，立即持久化上游 `_id`，
  已知 ID 只查询详情；货品校验完整后与采购终态同一事务落账并私信买家
- 用户可用 `/query <订单号>` 主动查询支付状态（兜底，同样触发一次履约推进）

- 状态转换持有连接锁、在写事务中核验前置状态，每次转换写入 `order_events` 审计；
  交付与采购终态通过 `db.finalize_delivery()` 原子落账，中断后恢复循环幂等收敛。

## 接入点

### 上游采购（已实现，配置 `upstream.provider: commbitz` 启用）

`services/purchasing.py` 提供 `Purchaser` 协议的两个实现：

- `DemoPurchaser`：未配置上游时的模拟交付（本地开发/测试）。
- `CommbitzPurchaser`：真实 Commbitz 采购，协议细节封装在 `services/commbitz_api.py`
  （令牌缓存/刷新、目录、`create_request`、KYC 双模式上传、用量查询）。

采购安全规则：创建请求前先持久化提交意图；收到响应立即保存上游 `_id`；
已有 ID 只查询；超时/5xx/缺 `_id` 转 `submission_unknown` 人工核对（上游无幂等键，
绝不自动重购）；上游明确拒绝先持久化 `refund_pending`，再原子退款到买家同币种余额并关闭，
/refund 供人工核对后退款，`/retry` 仅兼容自动退款前的历史数据。

人工核对入口：`/purchases` 列表、`/retry <订单号>`、`/bind <订单号> <上游请求ID>`
（事务内复核上游单唯一性，并按下单快照核对业务类型/数量/套餐）。

### 支付回调

EPay 网关 GET 或 POST 到 `/payment/callback`，form-urlencoded，带 MD5 签名。
`web/payment.py` 里的 `epay_callback()` 做验证和分发，`services/epay.py` 封装协议细节。

回调处理流程：
1. 验证 MD5 签名并解析订单号、核验金额/币种/商户 → 2. 持久化外部交易与付款事实 →
3. 为已付款订单建立采购任务后应答；重复或关单后的新增收款同币种补入钱包 →
4. 后台推进采购并通知买家。

### Telegram 原生支付（备选）

如果之后切 Telegram Payments，在 `handlers/order.py` 的确认回调里创建 invoice，
`successful_payment` 处理器里调用 `orders.mark_paid()` 复用整条链路。

## 持久化与交付边界

`Database.transaction()` 持有连接锁并使用 `BEGIN IMMEDIATE`，提交或回滚后才释放；读取与 FSM 也使用同一锁。
订单状态、事件和货品在一个事务提交。网络请求期间不持有数据库连接锁，同一订单的履约/通知按订单锁串行执行。

`EPayClient.validate_payment()` 统一核验回调和主动查询结果；数据库事务检查交易号与订单绑定。
`services/fulfillment.py` 只向持久化订单的买家私信货品（多张 eSIM 分条），记录
`notified_at` 与 `notification_pending`；恢复循环每 5 秒推进 `paid` 订单采购、收敛
「已交付但采购未终态」的历史残留（人工状态 submission_unknown/rejected 除外）、
补发未成功的私信。`delivery_failed` 经管理员重试返回 `paid`，不退回 `pending_payment`。

以上进程内订单锁对应单进程部署。Commbitz 无幂等承诺，采购适配器用持久化提交记录
阻止盲目重购：已有 `_id` 则查询，提交结果不明则人工核对；绑定上游单在事务内复核
唯一性，历史重复数据迁移时冻结为人工状态。


## 重新绑定与交付资料一致性

`/bind` 与同一订单的履约、通知共用订单锁。数据库事务同时完成上游单唯一性检查、采购引用更新、旧交付资料清除和审计记录写入；任一步失败全部回滚。

绑定成功后，订单恢复为 `paid`，保留金额、币种、支付交易号和采购输入快照；清空旧 `upstream_ref`、`payload`、`notified_at` 和通知待发标记。采购保留新上游 ID，进入 `upstream_pending`。绑定同一 ID 解除冻结时也必须重新核验货品。

后台只查询已绑定单据，不重新创建采购。等待、缺少安装资料、查询失败或 KYC 未通过时不发送货品；核验完成后，通过 `finalize_delivery()` 在一个事务中保存新引用、新货品及采购终态。实体 SIM 继续等待独立的发货确认。

通知入口要求有采购记录的订单已处于 `fulfilled`，订单交付引用与采购引用一致，且该上游引用没有其他订单占用。模拟模式使用明确的 `STUB-<订单号>` 引用。旧版错误重绑留下的引用不一致记录，即使已经通知、采购已为 `fulfilled`，也会被恢复扫描发现并重新核验；`submission_unknown` 仍由人工处理；历史 `paid + rejected` 由恢复循环补退。

## 多币种钱包、收款与退款

- `products.currency` 默认 USD，管理员 `/currency` 修改；订单创建时固定币种和金额，商品更新不影响已创建订单。
- `wallet_balances` 按 `(user_id, currency)` 记账；所有流水包含 `currency`，支付/退款使用订单快照币种。旧 `users.balance_cents` 保留为 CNY 兼容镜像，资金操作只写钱包事务。
- `orders.payment_method` 在生成 EPay 签名链接前设为 `epay`；余额扣款的事务检查该字段，保证两种支付方式互斥。旧待付 CNY 订单保守迁移为在线支付，以覆盖已发出的旧链接。
- `payment_receipts` 记录真实交易号和处理结果，订单/充值单共用交易号归属检查。重复回调只返回既有结果；余额付款后的真实在线收款、已关单后的新增收款，原子记入同币种钱包。人工先 `/paid` 的第一笔回调仅补齐原收款证据。
- `epay.currency` 声明当前商户实际收款币种，回调与查询核验相同；网关若回传币种也须一致。本店没有自动换汇机制。
- 明确失败先持久化 `refund_pending`，再在一笔事务内写余额、退款流水、订单和采购的 `refunded` 终态；中断后继续退款。历史 `paid + rejected` 也进入补退。退款通知使用持久化待通知标记。
- 人工退款与履约、KYC 使用同一订单锁；只允许未提交或已明确拒绝、人工核对的采购退款。上游仍在处理时必须先核对或取消。退款终态同时阻断 KYC、重绑和采购重试。

## eSIM 图片通知

`finalize_delivery` 同时保存文本货品和白名单字段组成的 ICCID/LPA JSON（`delivery_esims`），通知层只读取这份已核验的数据。二维码使用标准 QR，由 LPA 原文在内存中生成 PNG，作为 Telegram 图片上传；不下载二维码 URL，不把安装码写入日志或临时文件。

通知先发送订单提示，再逐张发送二维码与 ICCID/LPA 图文；图片说明放不下完整 LPA 时，在该图片之后另发完整文本。按 UTF-16 单位保守计数，不截断安装码。`notification_cursor` 保存成功步骤，`notification_plan_version` 标记步骤方案。失败后只续发尚未确认步骤；Telegram `retry_after` 写入 `notification_retry_at`，等待时间跨重启保留。全部发送完成才清除 `notification_pending`。Telegram 已发送但游标尚未写入时中断，仍可能重复最后一步，这是通知至少一次送达的边界，不会重新采购。

主动补发重置游标；重绑清除旧图片 JSON、游标和等待时间。冻结采购、KYC 未放行、引用冲突仍不能发送图片。旧版标准文本格式可解析出 LPA 生成图片；已成功通知的历史订单不自动重发。

### 发送方案升级

历史版本没有标记通知方案，且经历了“头部+文本+图片”到“头部+图文”的调整。无法仅凭旧游标确定哪些图片已经送达。新列默认版本 0；未完成通知通过原有采购、货品归属和限流检查后，事务性切换到当前方案并重置游标，从已保存资料重发。当前方案在每次发送前落库，后续重启不再重置进度。重绑清除方案版本与旧资料；修改未来方案的步骤顺序或含义时必须递增版本。

已完成通知不自动重发。如果旧版已经误标完成，无法从游标区分是否漏发，需由买家或管理员 `/query <订单号>` 主动补发同一份资料。

### 目录分页和金额精度

目录使用数据库分页，按 ID 排序，每页 5 款。目录摘要按 UTF-16 限制长度，正文使用纯文本；详情保留完整内容，按钮携带返回页码并兼容旧 `p:<id>` 格式。过期页码在当前有效页范围内收敛。

金额以字符串分段和整数运算精确解析，接受 1–4 位小数中的额外尾零，拒绝非零的分以下部分、非法格式及超过 SQLite 整数范围的金额。商品回调、充值回调和主动查询共用该规则；拒绝时不写支付凭据、余额或采购任务。
