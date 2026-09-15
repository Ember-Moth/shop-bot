# shop-bot

Telegram 商店 bot（webhook 模式）：用户浏览商品、下单，支付网关回调确认后通过上游供应商 API 发货。

## 运行

```bash
uv sync
# 编辑 config.yaml 或设置环境变量
shop-bot            # 或 uv run python -m shop_bot
```

**webhook 模式要求**：必须有公网 HTTPS 地址（`webhook.url`），通常前面套一层 Nginx/Caddy 做 TLS 终止。本地开发可用 [ngrok](https://ngrok.com/) 或 [localtunnel](https://localtunnel.me/) 暴露。

首次启动会自动建表；库中没有商品时写入两个示例商品。

## 用户流程

`/start` → 主菜单 → 商品目录 → 选商品 → 回复数量 → 确认下单 → 返回「立即支付」按钮 → **Telegram 内嵌打开 EPay 收银台** → 支付完成 → 网关回调自动发货 → 通知买家。

兜底：`/query <订单号>` 主动查询支付状态（回调延迟或丢失时用）。

## 支付回调

EPay 网关 POST 到 `payment.callback_path`（默认 `/payment/callback`），form-urlencoded，带 MD5 签名。验证通过后自动调用上游发货，成功则通知买家。

配置 `config.yaml` 的 `epay` 段即可启用：

```yaml
epay:
  pid: "1000"
  key: "你的商户密钥"
  url: "https://pay.example.com"
  type: alipay
```

签名验证和回调解析逻辑在 `src/shop_bot/services/epay.py` 和 `src/shop_bot/web/payment.py`。

## 管理员命令（需在 `admin_ids` 中）

- `/orders [状态]` — 查看订单
- `/paid <订单号>` — 手动标记已支付并触发发货
- `/cancel <订单号>` — 取消待支付订单

## 接入点（等你的上游 API 文档）

- **上游发货**：`src/shop_bot/services/upstream.py` — `StubUpstreamClient` 打日志模拟；实现 `UpstreamClient` 协议后在 `build_upstream()` 替换。
- **支付回调签名**：`src/shop_bot/web/payment.py` — 现在是标准 HMAC-SHA256，按网关实际方案改 `_verify_signature()`。

## 开发

```bash
uv run pytest          # 测试
uv run ruff check      # lint
uv run ty check        # 类型检查
```

## 结构

```
src/shop_bot/
├── config.py        # pydantic-settings：config.yaml + SHOP_BOT_* 环境变量
├── models.py        # Product / Order / OrderStatus
├── db.py            # aiosqlite 连接与 DAO
├── keyboards.py     # 内联键盘
├── handlers/        # start / catalog / order(FSM) / admin
├── services/        # upstream.py（上游接口+桩）, orders.py（订单状态机）
└── web/             # payment.py（支付回调端点）
```
