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
| `webhook.path` | `SHOP_BOT_WEBHOOK__PATH` | Telegram 更新回调路径 |
| `webhook.url` | `SHOP_BOT_WEBHOOK__URL` | 公网 HTTPS 地址（必填，如 `https://bot.example.com`） |
| `payment.callback_path` | `SHOP_BOT_PAYMENT__CALLBACK_PATH` | 支付网关回调路径 |
| `payment.secret` | `SHOP_BOT_PAYMENT__SECRET` | 回调签名共享密钥（EPay 用不到） |
| `epay.pid` | `SHOP_BOT_EPAY__PID` | EPay 商户 ID |
| `epay.key` | `SHOP_BOT_EPAY__KEY` | EPay 商户密钥 |
| `epay.url` | `SHOP_BOT_EPAY__URL` | EPay 网关地址 |
| `epay.type` | `SHOP_BOT_EPAY__TYPE` | 默认支付方式（`alipay`/`wxpay` 等） |
| `logging.level` | `SHOP_BOT_LOGGING__LEVEL` | 日志级别（默认 `INFO`） |
| `logging.log_dir` | `SHOP_BOT_LOGGING__LOG_DIR` | 日志文件目录（空表示只输出到 stdout） |
| `logging.json_logs` | `SHOP_BOT_LOGGING__JSON_LOGS` | 是否用 JSON 格式（生产建议 `true`） |
| `upstream.base_url` | `SHOP_BOT_UPSTREAM__BASE_URL` | 上游 API 地址 |
| `upstream.api_key` | `SHOP_BOT_UPSTREAM__API_KEY` | 上游 API 密钥 |

## 反向代理示例（Nginx）

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;

    location /webhook {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /payment/callback {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Payment-Signature $http_x_payment_signature;
    }
}
```

## 运行

```bash
uv sync
# 编辑 config.yaml 或设置环境变量
shop-bot
# 或
uv run python -m shop_bot
```

首次启动自动建表；商品表为空时写入 `src/shop_bot/__init__.py` 里的 `DEMO_PRODUCTS`。

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
