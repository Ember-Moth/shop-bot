# 部署

## 配置

配置文件默认是工作目录下的 `config.yaml`，可用 `SHOP_BOT_CONFIG` 指定其他路径。
环境变量（前缀 `SHOP_BOT_`）会覆盖 YAML 里的同名键，嵌套键用双下划线：

| YAML 键 | 环境变量 | 说明 |
|---|---|---|
| `bot_token` | `SHOP_BOT_BOT_TOKEN` | Bot token（必填） |
| `admin_ids` | `SHOP_BOT_ADMIN_IDS` | 管理员 telegram id 列表 |
| `database_path` | `SHOP_BOT_DATABASE_PATH` | SQLite 文件路径 |
| `webhook.host` | `SHOP_BOT_WEBHOOK__HOST` | 监听地址 |
| `webhook.port` | `SHOP_BOT_WEBHOOK__PORT` | 监听端口 |
| `webhook.secret_token` | `SHOP_BOT_WEBHOOK__SECRET_TOKEN` | 必填，请求来源校验密钥，1–256 个字母/数字/下划线/连字符 |
| `webhook.path` | `SHOP_BOT_WEBHOOK__PATH` | Telegram 更新回调路径 |
| `webhook.url` | `SHOP_BOT_WEBHOOK__URL` | 公网 HTTPS 地址（必填，如 `https://bot.example.com`） |
| `payment.callback_path` | `SHOP_BOT_PAYMENT__CALLBACK_PATH` | 支付网关回调路径 |
| `payment.secret` | `SHOP_BOT_PAYMENT__SECRET` | 回调签名共享密钥（EPay 用不到） |
| `epay.pid` | `SHOP_BOT_EPAY__PID` | EPay 商户 ID |
| `epay.key` | `SHOP_BOT_EPAY__KEY` | EPay 商户密钥 |
| `epay.url` | `SHOP_BOT_EPAY__URL` | EPay 网关地址 |
| `epay.type` | `SHOP_BOT_EPAY__TYPE` | 默认支付方式（`alipay`/`wxpay` 等） |
| `epay.currency` | `SHOP_BOT_EPAY__CURRENCY` | 商户实际收款币种；新样例 USD，省略字段的旧配置保持 CNY；不自动换汇 |
| `features.kyc` | `SHOP_BOT_FEATURES__KYC` | 是否向买家开放 KYC 证件补交入口（默认 `true`） |
| `logging.level` | `SHOP_BOT_LOGGING__LEVEL` | 日志级别（默认 `INFO`） |
| `logging.log_dir` | `SHOP_BOT_LOGGING__LOG_DIR` | 日志文件目录（空表示只输出到 stdout） |
| `logging.json_logs` | `SHOP_BOT_LOGGING__JSON_LOGS` | 是否用 JSON 格式（生产建议 `true`） |
| `upstream.provider` | `SHOP_BOT_UPSTREAM__PROVIDER` | 上游供应商；`commbitz` 启动时同步目录，留空仅模拟发货 |
| `upstream.environment` | `SHOP_BOT_UPSTREAM__ENVIRONMENT` | `uat` / `live`（默认 uat） |
| `upstream.api_key` | `SHOP_BOT_UPSTREAM__API_KEY` | Commbitz 分销商 API Key |
| `upstream.secret_key` | `SHOP_BOT_UPSTREAM__SECRET_KEY` | Commbitz 分销商 Secret Key |
| `upstream.timeout` | `SHOP_BOT_UPSTREAM__TIMEOUT` | 上游请求超时秒数（默认 15） |
| `upstream.base_url` | `SHOP_BOT_UPSTREAM__BASE_URL` | 可选覆盖；留空按 environment 选择 |

## 反向代理示例（Nginx）

完整配置模板在 `examples/nginx.conf`，复制后只需改 `server_name` 和证书路径：

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;

    ssl_certificate     /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;

    # Telegram bot webhook
    location /webhook {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # EPay 支付回调
    location /payment/callback {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        # EPay 用 form-urlencoded，不需要额外传 Header
    }
}
```

## 运行

```bash
uv sync
cp config.example.yaml config.yaml  # 首次运行；config.yaml 不入库，编辑它或设置环境变量
shop-bot
# 或
uv run python -m shop_bot
```

首次启动自动建表；模拟模式（未配置 `upstream.provider`）且商品表为空时写入 `src/shop_bot/__init__.py` 里的 `DEMO_PRODUCTS`，真实模式不植入演示商品。

## 本地开发

```bash
# 用 ngrok 暴露本地端口
ngrok http 8080
# 把 ngrok 给的 https URL 填到 config.yaml 的 webhook.url
shop-bot
```

## 开发检查

```bash
uv run pytest          # 测试
uv run ruff check      # lint
uv run ty check        # 类型检查
```

## systemd 部署

把 `examples/shop-bot.service` 拷到 `/etc/systemd/system/shop-bot.service`，按实际路径改 `User` / `WorkingDirectory` / `ExecStart`：

```bash
sudo cp examples/shop-bot.service /etc/systemd/system/shop-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now shop-bot
sudo systemctl status shop-bot
journalctl -u shop-bot -f   # 看日志
```

配置方式三选一：
1. 读 `WorkingDirectory` 下的 `config.yaml`（默认）
2. 设 `SHOP_BOT_CONFIG` 环境变量指向其他路径
3. 完全用 `Environment=` 行注入（适合 secret 管理）

## 升级与恢复

升级前备份数据库；程序会事务性补齐订单字段、合并旧 FSM 重复行后再建立唯一索引。
FSM 保留旧版本实际读取的最早行，避免被后续重复行中的空字段覆盖。
环境变量只覆盖指定字段，例如注入 `SHOP_BOT_EPAY__KEY` 会保留 YAML 中的 `pid/url/type`。

仅部署一个 bot 进程，同一数据库不能运行多个实例。启动迁移建立 `work_items`、状态联动触发器和索引，补齐未完成任务，并释放上个进程的领取标记。运行时采购、交付和钱包通知分别领取到期任务；空闲检查间隔为 1 秒，失败按退避时间重试。

升级前先停旧进程并备份数据库。旧版已通知订单和没有通知记录的钱包历史流水不会主动群发；旧版中断交付记录仍会在启动时核对修复。新版本新增数据库触发器；回退前先备份当前库，核对升级后的收付记录及旧代码兼容性，不能用升级前备份直接覆盖新增账目。
升级前已发货订单不会主动重发，可用 `/query` 补发已保存的货品。
发货失败需要管理员 `/paid` 重试；`/query` 与 `/paid` 补发的货品都只私信订单买家。
接入真实上游前必须验证实际扣款与交付；提交结果不明时转人工核对，不盲目重购。
EPay V1 查询在 URL 中携带密钥，因此 HTTPX/HTTPCORE 请求调试日志被禁用，支付错误只输出安全的业务信息。

## 币种与升级

新商品默认 USD，使用 `/products` 查询商品 ID，`/currency <ID> <币种>` 设置币种。已有商品和订单不自动改币种。钱包按币种隔离，旧版余额、充值和流水自动迁移为 CNY。

继续使用 EPay；`epay.currency: USD` 仅适用于商户侧实际按 USD 收款的服务。设置该字段不能改变网关实际扣款币种，详见 [币种说明](../README.md#商品与收款币种)。旧币种未完成支付应在更换收款币种前处理完毕。

升级自动建立钱包/外部收款记录及退款恢复状态，保留既有数据。旧待付 CNY 订单可能已有收银台链接，因此迁移后只允许在线支付；新的订单先选择渠道，再生成链接。

## 商品管理、监控与备份

实现和配置详见 [商品与运维](operations.md)。新增环境变量组：

| YAML | 环境变量 | 默认 |
|---|---|---|
| `operations.alerts_enabled` | `SHOP_BOT_OPERATIONS__ALERTS_ENABLED` | true，收件人为 admin_ids |
| `operations.check_interval_seconds` | `SHOP_BOT_OPERATIONS__CHECK_INTERVAL_SECONDS` | 30 |
| `operations.alert_cooldown_seconds` | `SHOP_BOT_OPERATIONS__ALERT_COOLDOWN_SECONDS` | 1800 |
| `operations.stale_order_seconds` | `SHOP_BOT_OPERATIONS__STALE_ORDER_SECONDS` | 900 |
| `operations.notification_stale_seconds` | `SHOP_BOT_OPERATIONS__NOTIFICATION_STALE_SECONDS` | 300 |
| `operations.worker_stale_seconds` | `SHOP_BOT_OPERATIONS__WORKER_STALE_SECONDS` | 180 |
| `operations.health_timeout_seconds` | `SHOP_BOT_OPERATIONS__HEALTH_TIMEOUT_SECONDS` | 2 |
| `backup.enabled` | `SHOP_BOT_BACKUP__ENABLED` | true |
| `backup.directory` | `SHOP_BOT_BACKUP__DIRECTORY` | 数据库目录下 backups |
| `backup.interval_seconds` | `SHOP_BOT_BACKUP__INTERVAL_SECONDS` | 86400 |
| `backup.keep` | `SHOP_BOT_BACKUP__KEEP` | 14 |
| `backup.timeout_seconds` | `SHOP_BOT_BACKUP__TIMEOUT_SECONDS` | 120 |

systemd 模板现在使用 `shop-bot` 专用用户，`StateDirectory=shop-bot` 提供 `/var/lib/shop-bot` 数据目录，数据库和默认备份均在此目录。请先创建该用户并授予配置文件读取权限；代码目录保持只读。启用文件日志或额外备份磁盘时，在 `ReadWritePaths` 中允许对应目录。
