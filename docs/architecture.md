# 架构

## 模块划分

```
shop_bot/
├── config.py        # pydantic-settings：config.yaml + SHOP_BOT_* 环境变量
├── models.py        # Product / Order / OrderStatus（StrEnum）
├── db.py            # aiosqlite 连接 + DAO（users / products / orders / order_events）
├── keyboards.py     # 内联键盘（目录、确认、订单操作）
├── handlers/
│   ├── start.py     # /start、主菜单、我的订单
│   ├── catalog.py   # 商品目录浏览
│   ├── order.py     # FSM 下单流程（选商品 → 数量 → 确认）
│   └── admin.py     # /orders、/paid、/cancel（管理员限定）
└── services/
    ├── upstream.py  # UpstreamClient 协议 + StubUpstreamClient + HttpUpstreamClient 骨架
    └── orders.py    # 订单状态机（create → paid → delivered / failed / cancelled）
```

## 订单生命周期

```
pending_payment --paid--> paid --deliver--> delivered
      |                    |
      |                    +--deliver_fail--> delivery_failed
      +--cancel--> cancelled
```

- 状态转换在 `db.transition_order()` 里用原子 `UPDATE ... WHERE status = ?` 完成，
  并发双击不会导致重复发货。
- 每次转换写入 `order_events` 表做审计。

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

拿到上游文档后填 `HttpUpstreamClient` 骨架即可，其余代码不用动。

### 支付回调

支付网关 POST 到 `/payment/callback`，带 `X-Payment-Signature` 头（HMAC-SHA256）。
`web/payment.py` 里的 `_verify_signature()` 做验证，拿到网关文档后按实际方案调整。

回调处理流程：
1. 验证签名 → 2. 解析 `order_id` → 3. `orders.mark_paid()` 触发上游发货 →
4. 成功则 `bot.send_message()` 通知买家。

### Telegram 原生支付（备选）

如果之后切 Telegram Payments，在 `handlers/order.py` 的确认回调里创建 invoice，
`successful_payment` 处理器里调用 `orders.mark_paid()` 复用整条链路。
