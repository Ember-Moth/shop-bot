# 部署教程

从零开始把 shop-bot 部署到一台 Linux 服务器，使用 Nginx 做反向代理、systemd 守护进程。

## 前置条件

- 一台有公网 IP 的服务器（Ubuntu 22.04 / Debian 12 或类似）
- 一个域名，解析到服务器 IP（例如 `bot.example.com`）
- 将 uv 安装到系统 PATH，并安装服务用户可读取的 Python ≥ 3.14.7
- 服务器上已安装 `nginx` 和 `certbot`（或自行处理 TLS 证书）

## 1. 安装代码

```bash
# 把代码放到 /opt/shop-bot（git clone 或 scp 上传均可）
sudo git clone <你的仓库地址> /opt/shop-bot
cd /opt/shop-bot

# 将解释器放到服务用户可读取的位置，避免 .venv 指向 /root 下的私有解释器
sudo env UV_PYTHON_INSTALL_DIR=/opt/shop-bot-python uv python install 3.14.7
sudo env UV_PYTHON_INSTALL_DIR=/opt/shop-bot-python uv sync --frozen --no-dev --python 3.14.7
```

## 2. 配置

```bash
sudo useradd --system --user-group --home-dir /opt/shop-bot --shell /usr/sbin/nologin shop-bot
sudo cp /opt/shop-bot/config.example.yaml /opt/shop-bot/config.yaml
sudo nano /opt/shop-bot/config.yaml
```

必填项：

```yaml
bot_token: "123456:ABC-DEF..."   # @BotFather 拿到的 token
admin_ids: [123456789]           # 你的 telegram id，多个用逗号
webhook:
  url: "https://bot.example.com" # 你的公网 HTTPS 地址
  secret_token: "REPLACE_WITH_RANDOM_SECRET" # 必填，仅允许字母/数字/下划线/连字符
epay:
  pid: "1000"
  key: "你的商户密钥"
  url: "https://pay.example.com"
  currency: USD                # 必须与网关实际收款币种一致
```

```bash
sudo chown root:shop-bot /opt/shop-bot/config.yaml
sudo chmod 640 /opt/shop-bot/config.yaml
```

仅演示时可不填 EPay 和上游凭据，使用管理员调入的测试余额。真实模式请先同步目录、用 `/price` 定价，再 `/publish` 上架。

## 3. 首次申请 TLS 证书

先确认域名解析和 80 端口可用。证书尚不存在时，不要先加载引用该证书的 HTTPS 配置。可先启用只监听 80 的临时 Nginx 站点，再运行：

```bash
sudo certbot --nginx -d bot.example.com
```

## 4. 配置 Nginx 反向代理

```bash
sudo cp /opt/shop-bot/examples/nginx.conf /etc/nginx/sites-available/shop-bot
sudo nano /etc/nginx/sites-available/shop-bot   # 把 bot.example.com 改成你的域名
```

```nginx
server {
    listen 80;
    server_name bot.example.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl;
    server_name bot.example.com;

    ssl_certificate     /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;

    location = /healthz {
        proxy_pass http://127.0.0.1:8080;
    }
    location = /readyz {
        proxy_pass http://127.0.0.1:8080;
        proxy_read_timeout 5s;
    }

    location /webhook {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /payment/callback {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Payment-Signature $http_x_payment_signature;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/shop-bot /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

完整 Nginx 配置模板在 `examples/nginx.conf`，复制后只需改 `server_name` 和证书路径。

## 5. 配置 systemd

```bash
sudo cp /opt/shop-bot/examples/shop-bot.service /etc/systemd/system/shop-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now shop-bot
```

service 使用 `shop-bot` 专用用户，数据库保存到 `/var/lib/shop-bot/shop_bot.db`，自动备份保存到其 `backups` 子目录；代码目录只读。若路径不是 `/opt/shop-bot`，同步修改 `WorkingDirectory` 和 `ExecStart`。

## 6. 验证

```bash
# 看服务状态
sudo systemctl status shop-bot

# 看日志（应该看到 webhook registered 和 listening on 127.0.0.1:8080）
journalctl -u shop-bot -f

# 在 Telegram 里给 bot 发 /start，应该收到主菜单
```

部署后检查 `curl --fail https://bot.example.com/readyz`，管理员私聊发送 `/status`。给管理员发起过私聊后才能接收异常告警。备份/恢复说明见 [商品与运维](operations.md)。

## 7. 日常维护

```bash
# 改代码后重启
sudo systemctl restart shop-bot

# 改配置后重启
sudo systemctl restart shop-bot

# 看实时日志
journalctl -u shop-bot -f

# 看最近 100 行
journalctl -u shop-bot -n 100
```

## 故障排查

| 现象 | 检查点 |
|---|---|
| `systemctl status` 显示 failed | `journalctl -u shop-bot -n 50` 看具体报错 |
| bot 无响应 | 确认 `webhook.url` 是 HTTPS 且证书有效；`curl -I https://bot.example.com/webhook` |
| 支付回调 401 | 确认 `epay.key` 和网关侧一致；看 `journalctl` 里的 signature mismatch |
| 订单卡住 | 管理员命令 `/orders` 看状态；中断的已付款订单自动恢复，发货失败用 `/paid <id>` 重试 |

## 升级

```bash
cd /opt/shop-bot
sudo git pull
sudo env UV_PYTHON_INSTALL_DIR=/opt/shop-bot-python uv sync --frozen --no-dev --python 3.14.7
sudo systemctl restart shop-bot
```
