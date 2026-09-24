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
cp config.example.yaml config.yaml  # 首次运行；config.yaml 不入库，编辑它或设置环境变量
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

商品管理（`/products` `/price` `/publish` `/unpublish` `/currency`）、订单收款（`/orders` `/query` `/paid` `/cancel` `/refund` `/dispatch`）、采购人工核对（`/purchases` `/bind` `/retry`）、钱包调账（`/adjust`）与运维（`/status` `/ackalert`）的完整用法，见 [管理员手册](docs/admin-guide.md)。

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
- [管理员手册](docs/admin-guide.md) — 管理员命令的日常操作说明
- [功能进度](docs/progress.md) — 完成度、TODO、接入指南
- [转售开发方案](docs/reseller-bot-development.md) — 上游采购状态机与分阶段验收

## 协议

[MIT](LICENSE)

## 交付与恢复

- 回调和 `/query` 共用金额、币种、商户、订单号、交易号校验；重复通知返回 `success`。
- 付款、采购任务、发货状态、货品内容通过数据库事务持久化。所有货品只私信订单所有者。
- 交付与采购终态在同一事务落账（`finalize_delivery`）；启动时修复历史中断残留；常驻任务按到期时间分别执行采购、交付和钱包通知。`delivery_failed` 由管理员 `/paid` 重试。
- 采购提交前先持久化提交意图；上游无幂等键，已有上游单号绝不重新创建（结果不明转 `/purchases` 人工核对）。
- 重新绑定会在同一事务中清除旧货品、旧交付引用和通知标记，保留付款记录；后台重新核验当前上游单后才恢复交付。通知与补发都检查采购终态和引用一致性。
- 目前支持单进程部署。
- Telegram 发送成功但确认记录尚未落盘就中断时，可能重复收到同一份私信，不会因此再次购买货品。

## 付款业务广播

支持按事件分别配置通知目标，例如订单付款发频道、充值到账私信管理员，也可同时通知多个目标。未付款单不广播；按收件人持久化去重、失败重试，配置见 [付款业务广播](docs/operations.md#付款业务广播)。

## 后台调度

- EPay、余额支付、管理员确认收款，在一个 SQLite 事务内保存付款事实、采购记录和任务；回调不等待上游或 Telegram。
- 固定 3 个采购、2 个交付、1 个钱包通知及 1 个业务广播协程，通道独立；空闲时每秒检查到期任务，慢 ZIP 上传不会阻塞其他订单采购。
- 等待中的采购按 5–60 秒退避轮询；通知失败按 5–300 秒退避，Telegram 指定的等待时间优先。通知共享全局与单聊限速。
- `work_items` 只保存任务标识和调度信息；索引领取到期任务，不反复加载历史货品或扫描人工冻结单。历史不一致订单在启动时重新入队。
- 充值、调账、额外收款补偿通知与账本流水同事务入队，重复支付回调不会产生重复通知任务。Telegram 已收到但本地确认前中断时仍可能补发同一内容。
- 仍要求一个 bot 进程独占数据库；无需 Redis 或独立消息队列。调度机制见 [架构](docs/architecture.md#持久化任务调度)。

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

## eSIM 图文与 ZIP 交付

真实 eSIM 订单核验完成后，机器人只向买家私信交付：单张发送二维码图文；2–100 张合并为一份 ZIP 文件，超过 100 张按最多 100 张一包发送。二维码由已验证、已落库的 LPA 原文在本地生成 PNG，不依赖上游二维码链接，不调用第三方二维码生成服务。

单张 eSIM 的 LPA 超出图片说明容量时，二维码之后另发完整安装码，不截断内容。多张订单的 ZIP 内包含说明、汇总清单，以及按订单内编号排列的二维码和完整安装资料。发送步骤和方案版本一起持久化，每份 ZIP 是一个独立步骤。发送失败或进程重启后从同一方案的未完成步骤继续；Telegram 限流时按其等待时间重试，不重新采购。全部步骤完成才标记通知成功。管理员或买家 `/query <订单号>` 可申请后台补发，图片始终只发给订单买家。已完成的通知从头发送；正在发送或重试的任务保留进度，重复查询合并为同一次补发。

升级时，仍未完成且没有方案版本或使用旧方案的通知，会按当前格式重发同一份已保存资料，以避免误用旧游标；可能重复收到资料，但不会重新采购。升级不会主动给已通知的历史订单补发图片；旧版标准格式的 ICCID/LPA 文本订单可通过 `/query` 补发二维码。重绑时图片资料与发送进度一起清除，重新核验后生成新二维码。模拟模式仍发送明确的模拟货品。截至 2026-09-24，付款、采购与交付主链路已在生产环境用真实订单验证；尚未在生产触发的分支见 [生产验收记录](docs/progress.md#生产验收记录)。

ZIP 示例（两张订单）：

```text
esims-42.zip
├── README.txt
├── esims.json
├── 0001/
│   ├── qrcode.png
│   ├── installation.txt  # 号码、ICCID 与完整 LPA
│   └── lpa.txt           # 仅完整 LPA，方便复制
└── 0002/
    ├── qrcode.png
    ├── installation.txt
    └── lpa.txt
```

ZIP 在内存中生成，文件路径只使用本地卡片编号；不以 ICCID、SKU 或上游文本构造路径。文件内容和 ZIP 元数据固定，同一笔订单重试可生成相同文件。每包上传前检查大小不超过 48 MB，为 [Telegram 文件发送接口的 50 MB 限制](https://core.telegram.org/bots/api#senddocument) 预留余量；异常或超限不标记通知成功。

实现使用 [Segno 标准 QR 编码](https://segno.readthedocs.io/en/stable/serializers.html)、[Telegram 图片发送接口](https://core.telegram.org/bots/api#sendphoto) 和文件发送接口。

## 目录与精确核单

商品目录每页最多 5 款，提供上一页/下一页；名称和描述在目录中按文本长度保守摘要，详情页保留完整描述。从详情返回时保留原页码；商品下架导致页数减少时自动回到有效页。

EPay 回调和主动查询统一接受最多 4 位小数中的额外尾零（例如 `20.0000`、`9.990`），但分以下存在非零数字时拒绝核单，不再四舍五入。充值和商品订单均使用相同规则。


### 发货号码（MSISDN）

上游详情中每张 eSIM 的 `msisdn` 会随 ICCID/LPA 一起保存。单张图片说明展示号码，多张 ZIP 的 `installation.txt` 与 `esims.json` 包含对应号码；保留 `+` 和前导零，不猜测国家码。

上游号码为空或格式无效时显示“上游未提供号码”，仍可交付有效安装资料。旧订单没有保存过该字段时显示“历史订单未保存号码”，补发不会重新采购或自动查询补齐号码。号码不进入管理员/频道付款广播。

通知方案为版本 3：旧版本未完成通知按新格式从头补发同一份已保存资料，已完成通知不主动重发。
