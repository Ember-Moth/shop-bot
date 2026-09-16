# 功能进度

上游已确定为 Commbitz。阶段 A（只读客户端 + 目录同步）、阶段 B（采购状态机与交付）与阶段 C（全部业务 + KYC + 用量）已实现；配置 `upstream.provider: commbitz` 即启用真实采购。开发路线见 [转售 Bot 开发方案](reseller-bot-development.md)。

## 已完成 ✅

### 核心功能
- [x] 商品目录浏览、下单、订单查询
- [x] FSM 多步下单流程（选商品 → 数量 → 确认）
- [x] 订单状态机（待支付 → 已支付 → 已发货 / 发货失败 / 已取消）
- [x] 订单审计日志（`order_events` 表记录每次状态变更）
- [x] 单进程内订单锁、数据库事务和 FSM 事件隔离，防止本地并发重复处理

### 支付集成
- [x] EPay 支付网关协议封装（MD5 签名、支付链接生成、回调验证、订单查询）
- [x] Telegram Web App 内嵌支付（下单后直接打开 EPay 收银台）
- [x] 支付回调端点（`/payment/callback`，form-urlencoded + MD5 签名验证）
- [x] 支付成功后触发履约并私信买家的流程（模拟上游验证；真实 Commbitz 交付待接入）
- [x] `/query <订单号>` 主动查询支付状态（回调兜底）
- [x] 订单模型存 EPay 交易号（`trade_no` 字段）

### 管理功能
- [x] 管理员权限控制（`admin_ids` 配置）
- [x] `/orders [状态]` 查看订单
- [x] `/paid <订单号>` 手动标记已支付并触发发货
- [x] `/cancel <订单号>` 取消待支付订单

### 基础设施
- [x] webhook 模式（aiohttp 服务器，同时挂 `/webhook` 和 `/payment/callback`）
- [x] SQLite 持久化（users / products / orders / order_events）
- [x] 配置系统（config.yaml + SHOP_BOT_* 环境变量覆盖）
- [x] 结构化日志（彩色开发格式 + JSON 生产格式，按天轮转保留 30 天）
- [x] 关键业务操作带上下文字段（order_id / user_id / upstream_ref / error）

### 开发工具
- [x] ruff（lint）
- [x] ty（类型检查）
- [x] pytest（并发、升级迁移、支付核单及恢复回归）
- [x] uvloop（异步运行时加速）

### 部署
- [x] systemd 服务文件（`examples/shop-bot.service`）
- [x] Nginx 反向代理配置（`examples/nginx.conf`）
- [x] 部署教程（`docs/deploy-tutorial.md`）
- [x] MIT 协议

## 待接入 🔲

### 上游发货 API
- [x] 已取得 Commbitz PDF 并整理为 Markdown
- [x] 已读取公开 Live/UAT Swagger，核对 21/22 个接口路径及环境差异
- [x] Live 鉴权、刷新令牌、目录及全部 11 个套餐详情只读联调成功
- [x] Commbitz 只读客户端（`services/commbitz_api.py`）：令牌缓存/刷新、401 恢复、并发刷新协调、目录/详情查询
- [x] 商品目录同步（`services/catalog_sync.py`）：套餐按 SKU 同步为本地商品，新商品 0 价下架待管理员定价上架
- [x] 采购记录与状态机（阶段 B，`services/purchasing.py` + `purchases` 表）：提交一次先留痕、立即保存上游 `_id`、已知 ID 只查询、未知结果转人工
- [x] 人工核对入口：`/purchases` 列表、`/retry <订单号>` 重试被拒采购、`/bind <订单号> <上游ID>` 核对绑定（业务类型/数量校验 + 审计）
- [x] 支付回调快速应答：确认收款并建立采购任务后立即返回，上游请求交给后台恢复循环
- [x] 双模式：未配置 provider 走 DemoPurchaser（模拟交付）；配置 commbitz 即启用真实采购适配器
- [x] 全部业务的下单输入（阶段 C）：激活采集 ICCID、充值采集手机号+天数、兑换券/实体 SIM 按数量
- [x] KYC 流程（阶段 C）：建单 pending → 买家私聊补交证件（照片/文件 multipart 或 HTTPS 链接 JSON）→ 审核释放后才交付
- [x] 实体 SIM 物流边界（阶段 C）：上游受理成功转 awaiting_dispatch，管理员确认发货后才交付
- [x] eSIM 用量查询（阶段 C）：`/usage <订单号>`，仅订单买家和管理员
- [ ] 确认 Live 实际扣款、幂等/核对和失败退款规则（阶段 D 前置）
- [ ] 授权范围内的真实支付、采购和交付验收（阶段 D/E）

### 商品管理
- [x] 商品 SKU / 上游套餐 ID 字段及旧库迁移（`products.sku` / `products.upstream_plan_id`）
- [ ] 管理员为本店同步来的商品定价并上架
- [ ] 可选：管理员命令 `/add_product` / `/del_product` 动态管理商品

### 支付网关扩展（可选）
- [ ] 接其他支付网关时，实现和 `services/epay.py` 相同的接口
- [ ] Telegram 原生支付（备选方案，在 `successful_payment` 回调里调 `orders.mark_paid()`）

## 已知限制 ⚠️

- **SQLite 单文件**：适合中小规模，日订单量上千后建议迁移到 PostgreSQL
- **EPay 回调签名是 MD5**：协议本身的要求，安全性依赖商户密钥保密
- **Web App 要求 HTTPS**：`webhook.url` 必须配好有效证书

## 已解决 ✅

- ~~**FSM 用内存存储**~~：已改成 SQLite 持久化（`db.py` 里的 `FSMStorage`），bot 重启后对话状态恢复

## 接入上游 API 清单

阶段 A（协议与目录）、B（采购与交付）、C（全部业务 + KYC + 用量）已完成。接下来按 [开发阶段与验收](reseller-bot-development.md#8-开发阶段与验收) 实施：

1. ~~实现 Commbitz 客户端及商品 SKU 映射~~（阶段 A 完成）
2. ~~采购记录、提交一次、详情轮询、人工核对~~（阶段 B 完成）
3. ~~全部业务的输入采集、KYC、异步等待和用量查询~~（阶段 C 完成）
4. Live 采购对账（扣款币种/单位/失败退回）与各业务真实交付验收（阶段 D）。
5. 经确认的 SKU/业务上架，启用销售（阶段 E）。

## 测试覆盖

```
tests/
├── test_config.py              # 配置加载及嵌套覆盖
├── test_epay.py                # 支付协议
├── test_orders.py              # 收款确认、幂等、并发、演示履约
├── test_fsm.py                 # 对话状态持久化
├── test_commbitz.py            # 上游客户端：鉴权、刷新、401 恢复、并发刷新、目录解析
├── test_catalog_sync.py        # 目录同步、定价保留、旧库迁移、启动接线
├── test_purchasing.py          # 采购状态机：提交一次、等待、未知转人工、并发、重启恢复、绑定
├── test_requests_contract.py   # 阶段 C：各请求类型 payload 合同、KYC 流、实体卡边界、用量
├── test_database_recovery.py   # 事务隔离、回滚、旧库迁移
├── test_payment_flow.py        # 核单、权限、私信、恢复
└── test_startup.py             # 实际启动鉴权和资源清理
```

跑测试：`uv run pytest`

## 审计修复补充

已统一所有 DAO/FSM 读写的连接锁和事务，并在建唯一索引之前迁移重复会话数据。
回调与查询统一核单、按订单买家私信发货；重复成功通知返回成功，通知失败可从数据库恢复。
启动要求配置 webhook 密钥；退出和启动失败都会释放资源。环境变量逐字段覆盖 YAML。
`paid` 中断订单会自动恢复，`delivery_failed` 保留收款事实，由管理员重试。
真实上游适配器、采购结果不明处理以及真实支付/扣款联调仍待完成；单元、本地 HTTP 测试和已完成的 Live 只读查询分别提供不同层面的证据。
