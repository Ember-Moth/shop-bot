# 功能进度

## 已完成 ✅

### 核心功能
- [x] 商品目录浏览、下单、订单查询
- [x] FSM 多步下单流程（选商品 → 数量 → 确认）
- [x] 订单状态机（待支付 → 已支付 → 已发货 / 发货失败 / 已取消）
- [x] 订单审计日志（`order_events` 表记录每次状态变更）
- [x] 并发安全（条件 UPDATE 防止双击重复发货）

### 支付集成
- [x] EPay 支付网关协议封装（MD5 签名、支付链接生成、回调验证、订单查询）
- [x] Telegram Web App 内嵌支付（下单后直接打开 EPay 收银台）
- [x] 支付回调端点（`/payment/callback`，form-urlencoded + MD5 签名验证）
- [x] 支付成功后自动触发上游发货、私信通知买家
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
- [x] pytest（19 个用例，含并发竞态回归）
- [x] uvloop（异步运行时加速）

### 部署
- [x] systemd 服务文件（`examples/shop-bot.service`）
- [x] Nginx 反向代理配置（`examples/nginx.conf`）
- [x] 部署教程（`docs/deploy-tutorial.md`）
- [x] MIT 协议

## 待接入 🔲

### 上游发货 API
- [ ] 拿到上游 API 文档
- [ ] 实现 `HttpUpstreamClient`（`services/upstream.py` 里的骨架）
- [ ] 在 `build_upstream()` 里替换 `StubUpstreamClient`
- [ ] 确认上游幂等性（防止网络重试导致重复发货）

### 商品管理
- [ ] 替换 `DEMO_PRODUCTS`（`src/shop_bot/__init__.py`）为真实商品目录
- [ ] 可选：加管理员命令 `/add_product` / `/del_product` 动态管理商品

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

拿到上游文档后，按这个顺序改：

1. **实现 `UpstreamClient` 协议**（`services/upstream.py`）：
   ```python
   class MyUpstreamClient:
       async def deliver(self, order: Order, product: Product) -> DeliveryResult:
           # 调上游 API 发货
           ...
   ```

2. **替换 `build_upstream()`**（`src/shop_bot/__init__.py`）：
   ```python
   def build_upstream() -> UpstreamClient:
       settings = get_settings()
       return MyUpstreamClient(settings.upstream.base_url, settings.upstream.api_key)
   ```

3. **确认幂等性**：上游 API 最好支持带 `order_id` 做幂等键，防止网络重试导致重复发货

4. **测试**：用 `tests/test_orders.py` 里的 `CountingStub` 模式验证并发安全

## 测试覆盖

```
tests/
├── test_config.py    # 配置加载（YAML、环境变量覆盖）
├── test_epay.py      # EPay 协议（签名、金额格式化、回调解析、订单查询 mock）
└── test_orders.py    # 订单生命周期（创建、支付、发货、取消、并发竞态）
```

跑测试：`uv run pytest`
