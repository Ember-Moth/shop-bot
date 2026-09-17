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

`/start` → 主菜单 → 商品目录 → 选商品 → 回复数量（激活/充值按需提供 ICCID/手机号/天数）→ 确认下单 → 选择「在线支付」并锁定渠道 → 「立即支付」按钮 → **Telegram 内嵌打开 EPay 收银台** → 支付完成 → 网关回调确认收款 → 后台向上游采购 → 货品私信给买家。

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
  currency: USD  # 必须与网关商户实际收款币种一致
```

### 商品与收款币种

新商品默认 **USD**。管理员用 `/products` 查看商品编号，`/currency <商品ID> USD` 设置商品币种；支持 USD、CNY、EUR、GBP、HKD、SGD、AUD、CAD、NZD、CHF（目前金额统一支持两位小数）。切换币种只修改商品的计价单位，不换算价格数值；已有订单的价格、币种快照保持不变，上游目录同步也不会覆盖人工设置。

钱包按币种分别记账，商品只能使用同币种余额支付，退款回到同币种余额。充值使用当前 EPay 商户配置的币种。旧版人民币余额、充值单、流水和商品保持 CNY，不会因新默认值而被重新解释成美元。

`epay.currency` 是对网关商户实际收款单位的声明。示例配置为 USD；没有配置该字段的旧部署仍按 CNY 处理。标准 [EPay V1 文档](https://api.zhunfu.cn/doc/v1_legacy_api.html) 中的 `money` 并无通用的美元切换或换汇参数；仅把本地字段改为 USD 不会让人民币商户具备美元收款能力。使用 USD 时，需先确认所用 EPay 服务商/商户按 USD 收款；币种不匹配时 Bot 不生成支付链接。当前代码支持一个 EPay 商户币种，不自动换汇。

更换商户币种前应先处理旧币种的未完成收款。回调同时校验本地订单币种、配置币种和网关实际回传的币种（若有），不会把旧 CNY 订单按新 USD 设置入账。

签名验证和回调解析逻辑在 `src/shop_bot/services/epay.py` 和 `src/shop_bot/web/payment.py`。

## 管理员命令（需在 `admin_ids` 中）

- `/products` — 查看所有商品（含下架商品）的编号、价格、币种、SKU
- `/currency <商品ID> <币种>` — 设置商品计价币种；新商品默认 USD
- `/price <商品ID> <售价> [币种]` — 定价；省略币种保留原币种，不自动上架
- `/publish <商品ID>` / `/unpublish <商品ID>` — 上架/下架，已有订单保留快照
- `/status` — 查看就绪状态、最近备份和告警收件人数
- `/ackalert <标识>` — 确认已处理告警，持续异常会再次触发
- `/orders [状态]` — 查看订单
- `/paid <订单号>` — 手动确认付款并推进履约（不重置已付款订单）
- `/cancel <订单号>` — 取消待支付订单
- `/purchases` — 列出需要人工处理的采购（结果不明/被拒）
- `/retry <订单号>` — 重试创建前被拒的采购（仅自动退款前的历史数据；已退款关闭的订单不可重试）
- `/bind <订单号> <上游请求ID>` — 核对并绑定已有上游订单；旧交付资料失效，重新查询核验后再发货
- `/dispatch <订单号>` — 确认实体 SIM 已发出
- `/refund <订单号>` — 人工退款到买家同币种余额并关单；仅未提交、明确拒绝或 submission_unknown 已人工核对的采购可退。上游仍在提交、处理、KYC 或待发货时拒绝直接退款；明确失败由系统自动退
- `/adjust <用户ID> <币种> <±金额> [备注]` — 指定币种调账，例如 `/adjust 3 USD +10 退款补账`；旧格式省略币种时仍为 CNY，不允许扣成负余额

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
- [商品与运维](docs/operations.md) — 定价上下架、健康检查、管理员告警和自动备份/恢复
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

## 收款与退款恢复

- 新订单先选择支付渠道。生成 EPay 链接前将在线渠道持久化，之后该单不能再扣钱包余额；旧版可能已生成链接的待付 CNY 订单迁移为在线支付。
- 每笔外部交易保存到 `payment_receipts`，订单和充值单共享交易号唯一性校验。历史重复收款、关单后新增的真实收款按原币种补入钱包，重复回调不重复补款；已由 `/paid` 确认的第一笔回调只补齐交易号。
- 上游明确拒绝先记为 `refund_pending`；余额入账、退款流水、订单与采购 `refunded` 终态在同一事务提交。写入失败或重启后继续退款；旧版 `paid + rejected` 也会恢复补退。
- 人工退款、采购、KYC 共用订单锁。已退款订单不能补交 KYC、重购或重绑；退款通知失败会由恢复循环补发。

## 运维能力

提供 `/healthz` 存活检查与 `/readyz` 就绪检查；后台任务退出由进程监督和 systemd 重启处理。
管理员告警按收件人持久化去重与重试，覆盖人工采购、履约停滞、通知失败、后台任务、数据库和备份异常。
默认启动及每 24 小时在线备份 SQLite，校验成功后保留最近 14 份；也可运行 `shop-bot-backup`。
配置与恢复流程见 [商品与运维](docs/operations.md)。

## eSIM 二维码图片交付

真实 eSIM 订单核验完成后，机器人私信买家 ICCID、LPA 安装码和二维码图片；多张订单逐张标注编号。图片由已验证、已落库的 LPA 原文在本地生成 PNG，再通过 Telegram `sendPhoto` 上传，不需要二维码链接继续有效，也不调用第三方二维码生成服务。

文本和每张图片分别持久化发送进度。发送失败或进程重启后从未完成步骤继续；Telegram 限流时按其等待时间重试，不重新采购。全部步骤完成才标记通知成功。管理员或买家 `/query <订单号>` 可主动从头补发，图片始终只发给订单买家。

升级不会主动给已通知的历史订单补发图片；旧版标准格式的 ICCID/LPA 文本订单可通过 `/query` 补发二维码。重绑时图片资料与发送进度一起清除，重新核验后生成新二维码。模拟模式仍发送明确的模拟货品；真实二维码可否安装激活，仍需实单验收。

实现使用 [Segno 标准 QR 编码](https://segno.readthedocs.io/en/stable/serializers.html) 和 [Telegram 图片发送接口](https://core.telegram.org/bots/api#sendphoto)。
