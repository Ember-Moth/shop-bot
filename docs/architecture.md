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
│   ├── start.py        # /start、主菜单、我的订单、/query 查支付状态
│   ├── catalog.py      # 商品目录浏览
│   ├── order.py        # FSM 下单流程（选商品 → 数量 → 确认 → 生成支付链接）
│   └── admin.py        # /orders、/paid、/cancel（管理员限定）
├── services/
│   ├── upstream.py     # UpstreamClient 协议 + StubUpstreamClient + HttpUpstreamClient 骨架
│   ├── orders.py       # 订单状态机（create → paid → delivered / failed / cancelled）
│   ├── epay.py         # EPay 支付网关协议（MD5 签名、支付链接、回调验证、订单查询）
│   ├── commbitz_api.py # Commbitz 分销 API 只读客户端（令牌缓存/刷新、目录、订单详情）
│   └── catalog_sync.py # 上游套餐同步为本地商品（SKU 映射；新商品 0 价下架待人工定价）
└── web/
    └── payment.py      # EPay 回调端点（form-urlencoded + MD5 签名验证）
```

## 数据流

```
用户 ──/start──> bot ──> 商品目录
  │
  └─选商品 ──> FSM 确认数量 ──> 创建订单 ──> 生成 EPay 支付链接
                                              │
                                              v
                                    Web App 打开 EPay 收银台
                                              │
                                              v
                                    用户完成支付
                                              │
                                              v
EPay 网关 ──GET/POST /payment/callback──> 验证 MD5 签名 ──> orders.mark_paid()
                                              │
                                              v
                                    upstream.deliver() 发货
                                              │
                                              v
                                    bot.send_message() 通知买家
```

## 订单生命周期

```
pending_payment --epay_callback--> paid --deliver--> delivered
      |                                |
      |                                +--deliver_fail--> delivery_failed
      +--cancel--> cancelled
```

- 用户下单后，bot 返回「立即支付」按钮（Telegram Web App）
- Web App 直接打开 EPay 收银台（`submit.php`），用户在 Telegram 内完成支付
- 支付成功后，EPay 网关 GET 或 POST 到 `/payment/callback`，带 MD5 签名
- 验证通过后 `orders.mark_paid()` 触发上游发货，成功则通知买家
- 用户可用 `/query <订单号>` 主动查询支付状态（兜底）

- 状态转换在 `db.transition_order()` 里持有连接锁、在写事务中核验前置状态，
  每次转换写入 `order_events` 表做审计。

## 接入点

### 上游发货 API

实现 `UpstreamClient` 协议（`services/upstream.py`），在 `build_upstream()` 里替换
`StubUpstreamClient`：

```python
class MyUpstreamClient:
    async def deliver(self, order, product) -> DeliveryResult:
        async with httpx.AsyncClient(...) as client:
            resp = await client.post(...)
            ...
```

Commbitz 的正常请求接口已明确，但当前文档未承诺创建请求幂等。真实接入还需要商品 SKU/业务输入快照、采购记录、上游 `_id` 持久化，以及待处理/KYC/结果不明状态；不能仅在骨架中增加一次 POST 就启用自动恢复。

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
`services/fulfillment.py` 只向持久化订单的买家私信货品，记录 `notified_at` 与 `notification_pending`；启动和定时扫描处理 `paid` 与通知待重试的 `delivered`。
`delivery_failed` 经管理员重试返回 `paid`，不退回 `pending_payment`。

以上进程内订单锁对应单进程部署。当前 `UpstreamClient` 约定重试应幂等；Commbitz 尚无已验证的幂等承诺，因此真实适配器必须用持久化提交记录阻止盲目重购：已有 `_id` 则查询，提交结果不明则人工核对。
