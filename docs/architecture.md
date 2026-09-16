# 架构

本文描述当前代码；Commbitz 真实采购尚未接入。目标资金流、采购状态及开发改造见 [转售 Bot 开发方案](reseller-bot-development.md)。

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
  └─选商品 ──> FSM 确认数量 ──> 创建订单 ──> 生成 EPay 支付链接
                                              │
                                              v
                                    Web App 打开 EPay 收银台
                                              v
                                    用户完成支付
                                              v
EPay 网关 ──GET/POST /payment/callback──> 验签核单 ──> mark_paid() 确认收款
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
                        delivered + 通知买家         submission_unknown/rejected
                                                      → /purchases 人工核对
                                                      → /bind 绑定 或 /retry 重试
```

## 订单生命周期

订单收款状态（orders.status）与采购状态（purchases.state）分开保存；收款事实一旦落库，
采购等待、KYC、通知失败都不会让订单退回未付款。

```
pending_payment --epay_callback--> paid --采购+交付--> delivered
      |                                |                  （订单与采购终态同一事务落账）
      |                                +--履约需人工--> awaiting_dispatch（实体卡，/dispatch 确认）
      +--cancel--> cancelled
```

采购状态机（`services/purchasing.py`，规则详见 [开发方案](reseller-bot-development.md) 5.3/6 节）：

```
ready → submitting → upstream_pending → fulfilled
            │              │
            │              ├── awaiting_kyc → kyc_submitted（INR/强制 KYC，审核释放后才交付）
            │              └── awaiting_dispatch（实体 SIM 受理成功 ≠ 已发货）
            └── 超时/5xx/缺 _id → submission_unknown（停止自动重购，/bind 人工核对）
     4xx 明确拒绝 → rejected（/retry 仅允许无上游单号的记录重试）
```

- 用户下单后，bot 返回「立即支付」按钮（Telegram Web App）
- Web App 直接打开 EPay 收银台（`submit.php`），用户在 Telegram 内完成支付
- 支付成功后，EPay 网关 GET 或 POST 到 `/payment/callback`，带 MD5 签名
- 回调只做验签、核单、确认收款并建立采购任务（ready），随即应答（规则 2）
- 后台恢复循环（5 秒）驱动采购：提交一次先留痕，立即持久化上游 `_id`，
  已知 ID 只查询详情；货品校验完整后与采购终态同一事务落账并私信买家
- 用户可用 `/query <订单号>` 主动查询支付状态（兜底，同样触发一次履约推进）

- 状态转换持有连接锁、在写事务中核验前置状态，每次转换写入 `order_events` 审计；
  交付与采购终态通过 `db.finalize_delivery()` 原子落账，中断后恢复循环幂等收敛。## 接入点

### 上游采购（已实现，配置 `upstream.provider: commbitz` 启用）

`services/purchasing.py` 提供 `Purchaser` 协议的两个实现：

- `DemoPurchaser`：未配置上游时的模拟交付（本地开发/测试）。
- `CommbitzPurchaser`：真实 Commbitz 采购，协议细节封装在 `services/commbitz_api.py`
  （令牌缓存/刷新、目录、`create_request`、KYC 双模式上传、用量查询）。

采购安全规则：创建请求前先持久化提交意图；收到响应立即保存上游 `_id`；
已有 ID 只查询；超时/5xx/缺 `_id` 转 `submission_unknown` 人工核对（上游无幂等键，
绝不自动重购）；rejected 仅无上游单号的记录可受控重试。

人工核对入口：`/purchases` 列表、`/retry <订单号>`、`/bind <订单号> <上游请求ID>`
（事务内复核上游单唯一性，并按下单快照核对业务类型/数量/套餐）。

### 支付回调

EPay 网关 GET 或 POST 到 `/payment/callback`，form-urlencoded，带 MD5 签名。
`web/payment.py` 里的 `epay_callback()` 做验证和分发，`services/epay.py` 封装协议细节。

回调处理流程：
1. 验证 MD5 签名 → 2. 解析 `out_trade_no` 拿订单号 → 3. `orders.mark_paid()` 触发上游发货 →
4. 成功则 `bot.send_message()` 通知买家。

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
