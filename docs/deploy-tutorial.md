# 部署教程

从零开始把 shop-bot 部署到一台 Linux 服务器，使用 Nginx 做反向代理、systemd 守护进程。

## 前置条件

- 一台有公网 IP 的服务器（Ubuntu 22.04 / Debian 12 或类似）
- 一个域名，解析到服务器 IP（例如 `bot.example.com`）
- 服务器上已安装 `nginx` 和 `certbot`（或自行处理 TLS 证书）

## 1. 安装代码

```bash
# 把代码放到 /opt/shop-bot（git clone 或 scp 上传均可）
sudo git clone <你的仓库地址> /opt/shop-bot
cd /opt/shop-bot

# 用 uv 装依赖并生成虚拟环境
sudo uv sync
```

## 2. 配置

```bash
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
```

## 3. 配置 Nginx 反向代理

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

## 4. 申请 TLS 证书（首次）

```bash
sudo certbot --nginx -d bot.example.com
# 按提示完成验证，certbot 会自动改好 Nginx 配置
```

## 5. 配置 systemd

```bash
sudo cp /opt/shop-bot/examples/shop-bot.service /etc/systemd/system/shop-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now shop-bot
```

service 文件默认用 `root` 运行，如果路径不是 `/opt/shop-bot`，改 `WorkingDirectory` 和 `ExecStart` 两行即可。

## 6. 验证

```bash
# 看服务状态
sudo systemctl status shop-bot

# 看日志（应该看到 webhook registered 和 listening on 127.0.0.1:8080）
journalctl -u shop-bot -f

# 在 Telegram 里给 bot 发 /start，应该收到主菜单
```

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
sudo uv sync
sudo systemctl restart shop-bot
```
