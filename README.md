# shop-bot

Telegram 商店 bot（webhook 模式）：用户浏览商品、下单，EPay 支付回调确认后通过上游供应商 API 发货。

## 功能

- 🛍 商品目录浏览、下单、订单查询
- 💳 EPay 支付网关集成，Telegram Web App 内嵌收银台
- 🔄 支付回调自动触发上游发货，私信通知买家
- 📦 订单状态机（待支付 → 已支付 → 已发货 / 发货失败 / 已取消）
- 👨‍💼 管理员命令（查单、手动发货、取消订单）
- 📊 结构化日志（JSON 格式，按天轮转）
- 🧪 回归测试（含并发、升级迁移、故障恢复与权限校验）

## 运行

```bash
uv sync
# 编辑 config.yaml 或设置环境变量
shop-bot            # 或 uv run python -m shop_bot
```

**webhook 模式要求**：必须有公网 HTTPS 地址（`webhook.url`），通常前面套一层 Nginx/Caddy 做 TLS 终止。本地开发可用 [ngrok](https://ngrok.com/) 或 [localtunnel](https://localtunnel.me/) 暴露。

运行前必须设置 `webhook.secret_token`，通过该密钥验证 Telegram 请求来源。

首次启动会自动建表；库中没有商品时写入两个示例商品。

## 用户流程

`/start` → 主菜单 → 商品目录 → 选商品 → 回复数量 → 确认下单 → 返回「立即支付」按钮 → **Telegram 内嵌打开 EPay 收银台** → 支付完成 → 网关回调自动发货 → 通知买家。

兜底：`/query <订单号>` 主动查询支付状态（回调延迟或丢失时核单并履约；已发货订单可补发货品到买家私聊）。

## 支付回调

EPay 网关 GET 或 POST 到 `payment.callback_path`（默认 `/payment/callback`），form-urlencoded，带 MD5 签名。验证通过后自动调用上游发货，成功则通知买家。

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

## 日志

`config.yaml` 的 `logging` 段控制：

```yaml
logging:
  level: INFO        # DEBUG/INFO/WARNING/ERROR/CRITICAL
  log_dir: ""        # 空 = 只输出 stdout；填路径 = 同时写文件
  json_logs: false   # 生产环境建议改成 true
```

关键业务操作（下单、支付、发货）带上下文字段（`order_id`/`user_id`/`upstream_ref`），方便检索和告警。

## 接入点

- **上游发货**：`src/shop_bot/services/upstream.py` — `StubUpstreamClient` 打日志模拟；实现 `UpstreamClient` 协议后在 `build_upstream()` 替换。
- **支付网关**：`src/shop_bot/services/epay.py` — 实现 EPay V1 签名、查询与回调核单；接其他网关时实现相同接口即可。

## 开发

```bash
uv run pytest          # 测试
uv run ruff check      # lint
uv run ty check        # 类型检查
```

## 结构

```
src/shop_bot/
├── config.py           # pydantic-settings：config.yaml + SHOP_BOT_* 环境变量
├── models.py           # Product / Order / OrderStatus
├── db.py               # aiosqlite 连接与 DAO（users / products / orders / order_events）
├── keyboards.py        # 内联键盘（含 Web App 支付按钮）
├── logging_config.py   # 日志系统（彩色开发格式 + JSON 生产格式）
├── handlers/           # start / catalog / order(FSM) / admin
├── services/           # upstream.py（上游接口+桩）, orders.py（订单状态机）, epay.py（EPay 协议）
└── web/                # payment.py（EPay 回调端点）
```

## 文档

- [架构设计](docs/architecture.md) — 模块划分、数据流、订单状态机
- [部署指南](docs/deployment.md) — 配置项、systemd、Nginx 示例
- [部署教程](docs/deploy-tutorial.md) — 从零到上线的完整步骤
- [功能进度](docs/progress.md) — 完成度、TODO、接入指南

## 协议

[MIT](LICENSE)

## 交付与恢复

- 回调和 `/query` 共用金额、币种、商户、订单号、交易号校验；重复通知返回 `success`。
- 付款、发货状态、货品内容通过数据库事务持久化。所有货品只私信订单所有者。
- 启动后及每 30 秒恢复中断的 `paid` 订单、重试尚未成功的私信。`delivery_failed` 由管理员 `/paid` 重试。
- `/paid` 不会把已付款订单重置为待付款；已发货时复用已保存的货品。
- 目前支持单进程部署。真实上游必须按本系统 `order.id` 幂等发货或查询已有订单，覆盖上游成功、本地未落盘就中断的情况。
- 当前仍使用模拟上游。升级前已发货订单不会主动重发，可用 `/query` 补发；历史上未保存任何货品的订单需要人工从上游找回。
- Telegram 发送成功但确认记录尚未落盘就中断时，可能重复收到同一份私信，不会因此再次购买货品。
