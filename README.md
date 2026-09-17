# shop-bot

Telegram 商店 bot（webhook 模式）：用户浏览商品、下单，EPay 支付回调确认后向上游（Commbitz 分销 API）采购并自动交付。

## 功能

- 🛍 商品目录（上游套餐自动同步）浏览、按业务类型下单（eSIM/激活/充值/兑换券/实体 SIM）、订单查询
- 💳 EPay 支付网关集成，Telegram Web App 内嵌收银台
- 🔄 支付回调确认收款 → 后台向上游采购 → 货品持久化 → 私信通知买家
- 📦 订单收款状态与采购状态机分离（提交一次、已知上游单只查询、结果不明转人工）
- 🪪 KYC 补交（私聊收集材料）与 eSIM 用量查询
- 👨‍💼 管理员命令（查单、人工核对、受控重试、补发、实体卡发货确认）
- 📊 结构化日志（JSON 格式，按天轮转）
- 🧪 回归测试（含并发、升级迁移、故障恢复与权限校验）

## 运行

```bash
uv sync
# 编辑 config.yaml 或设置环境变量
shop-bot            # 或 uv run python -m shop_bot
```

**webhook 模式要求**：必须有公网 HTTPS 地址（`webhook.url`），通常前面套一层 Nginx/Caddy 做 TLS 终止。

运行前必须设置 `webhook.secret_token`，通过该密钥验证 Telegram 请求来源。

配置 `upstream.provider: commbitz` 并填入分销商密钥后，启动时同步上游套餐目录（新商品 0 价下架，需管理员定价上架）；不配置则使用模拟采购（本地开发）。

## 用户流程

`/start` → 主菜单 → 商品目录 → 选商品 → 回复数量（激活/充值按需提供 ICCID/手机号/天数）→ 确认下单 → 「立即支付」按钮 → **Telegram 内嵌打开 EPay 收银台** → 支付完成 → 网关回调确认收款 → 后台向上游采购 → 货品私信给买家。

- 兜底：`/query <订单号>` 主动查询支付状态（回调延迟或丢失时核单并履约；已发货订单可补发货品到买家私聊）。
- KYC：需要身份核验的订单（INR/账户级强制），买家 `/kyc <订单号>` 在私聊补交证件，审核通过后自动发货。
- 用量：`/usage <订单号>` 查询已交付 eSIM 的流量用量。

## 支付回调

EPay 网关 GET 或 POST 到 `payment.callback_path`（默认 `/payment/callback`），form-urlencoded，带 MD5 签名。回调只做验签、核单、确认收款并建立采购任务，随即应答；上游采购由后台恢复循环异步执行。

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
- `/paid <订单号>` — 手动确认付款并推进履约（不重置已付款订单）
- `/cancel <订单号>` — 取消待支付订单
- `/purchases` — 列出需要人工处理的采购（结果不明/被拒）
- `/retry <订单号>` — 重试创建前被拒的采购（仅自动退款前的历史数据；已退款关闭的订单不可重试）
- `/bind <订单号> <上游请求ID>` — 核对并绑定已有上游订单；旧交付资料失效，重新查询核验后再发货
- `/dispatch <订单号>` — 确认实体 SIM 已发出
- `/refund <订单号>` — 人工退款到买家余额并关单（submission_unknown 核对未发货后；上游明确拒绝的订单系统自动退）
- `/adjust <用户ID> <±金额> [备注]` — 人工调账（写 adjust 流水并私信用户，不允许扣成负余额）

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

- **上游采购**：`src/shop_bot/services/purchasing.py` — 采购状态机（提交一次/详情轮询/结果不明转人工）+ Demo/Commbitz 双模式；`services/commbitz_api.py` 封装协议（令牌/目录/采购/KYC/用量）。
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
├── models.py           # Product / Order / Purchase / 状态枚举
├── db.py               # aiosqlite 连接与 DAO（users / products / orders / order_events / purchases / fsm_state）
├── keyboards.py        # 内联键盘（含 Web App 支付按钮）
├── logging_config.py   # 日志系统（彩色开发格式 + JSON 生产格式）
├── handlers/           # start / catalog / order(FSM) / kyc / admin
├── services/           # orders（收款）/ purchasing（采购状态机）/ fulfillment（私信+恢复）
│                       # epay.py（EPay 协议）/ commbitz_api.py（上游客户端）/ catalog_sync.py（目录同步）
└── web/                # payment.py（EPay 回调端点）、telegram.py（webhook 路由）
```

## 文档

- [架构设计](docs/architecture.md) — 模块划分、数据流、订单状态机
- [部署指南](docs/deployment.md) — 配置项、systemd、Nginx 示例
- [部署教程](docs/deploy-tutorial.md) — 从零到上线的完整步骤
- [功能进度](docs/progress.md) — 完成度、TODO、接入指南
- [转售开发方案](docs/reseller-bot-development.md) — 上游采购状态机与分阶段验收

## 协议

[MIT](LICENSE)

## 交付与恢复

- 回调和 `/query` 共用金额、币种、商户、订单号、交易号校验；重复通知返回 `success`。
- 付款、采购任务、发货状态、货品内容通过数据库事务持久化。所有货品只私信订单所有者。
- 交付与采购终态在同一事务落账（`finalize_delivery`）；启动后恢复循环每 5 秒推进 `paid` 订单采购、收敛历史中断残留、补发未成功的私信。`delivery_failed` 由管理员 `/paid` 重试。
- 采购提交前先持久化提交意图；上游无幂等键，已有上游单号绝不重新创建（结果不明转 `/purchases` 人工核对）。
- 重新绑定会在同一事务中清除旧货品、旧交付引用和通知标记，保留付款记录；后台重新核验当前上游单后才恢复交付。通知与补发都检查采购终态和引用一致性。
- 目前支持单进程部署。
- Telegram 发送成功但确认记录尚未落盘就中断时，可能重复收到同一份私信，不会因此再次购买货品。
