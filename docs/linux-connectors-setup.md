# Linux 上填写 Discord 与 Grok 配置

服务模板可以先安装，连接器保持 disabled/inactive，等你在 Linux 填好配置后再启动。
本页命令供你随后在 `home-ubuntu` 上执行；配置文件、真实密钥与连接成功是分别验证的状态。
Grok 的公开入口使用独立的 Bearer 认证服务；现有 Dot 私有入口使用 8767。

## 需要你准备的值

| 用途 | 需要填写 | Linux 位置 |
|---|---|---|
| Discord Bot | Bot Token | `~/Desktop/job-search/runtime/credentials/bot.env` 的 `AUTO_APP_DISCORD_BOT_TOKEN` |
| Discord 操作权限 | 你的数字 User ID | 同文件的 `AUTO_APP_DISCORD_OWNER_ID` |
| Discord 服务器 | 数字 Server/Guild ID | 同文件的 `AUTO_APP_DISCORD_GUILD_ID` |
| Discord 通知频道 | 数字 Channel ID | 同文件的 `AUTO_APP_DISCORD_CHANNEL_ID` |
| Grok 对外地址 | 你选择的 HTTPS 主机地址，不带 `/mcp` | `runtime/config/grok.env` 的 `AUTO_APP_MCP_PUBLIC_URL` |
| MCP 认证 | 独立的高随机性 Bearer 密钥，32–4096 个无空格 ASCII 字符 | `runtime/credentials/mcp-token`，文件只放密钥 |
| Cloudflare named tunnel | Tunnel UUID、对应的 JSON 凭据、你控制的域名/子域名 | `runtime/config/grok-tunnel.yml` 与 `runtime/credentials/<UUID>.json` |
| 可选告警/周报 | Discord Incoming Webhook URL | `runtime/credentials/worker.env` 的 `AUTO_APP_DISCORD_WEBHOOK_URL` |

Bot Token、Webhook URL、MCP Bearer 密钥、Cloudflare JSON 是四种不同凭据。
密钥由你在 Linux 编辑，不放到仓库或聊天中。本方案的 Grok 接入信息是 MCP URL 和其认证信息，
不是 xAI 模型 API Key。

## 1. 在 Linux 复制示例并填写

以下复制命令只为缺失文件创建示例，不覆盖已有配置。示例的空值和占位符都需要替换。
只配置准备启用的组件即可。

```bash
cd ~/Desktop/job-search
umask 077
install -d -m 700 runtime/config runtime/credentials
test -e runtime/credentials/bot.env || install -m 600 auto-application/deploy/bot.env.example runtime/credentials/bot.env
test -e runtime/config/grok.env || install -m 600 auto-application/deploy/grok.env.example runtime/config/grok.env
test -e runtime/config/grok-tunnel.yml || install -m 600 auto-application/deploy/grok-tunnel.yml.example runtime/config/grok-tunnel.yml
nano runtime/credentials/bot.env
nano runtime/config/grok.env
nano runtime/config/grok-tunnel.yml
nano runtime/credentials/mcp-token
chmod 600 runtime/credentials/bot.env runtime/credentials/mcp-token runtime/config/grok.env runtime/config/grok-tunnel.yml
```

`bot.env` 使用原始 `KEY=value`，不加 `export` 或引号，保留示例中的四个键。
Bot Token 来自你创建的 Discord application；三个 ID 必须为正整数。将 Bot 安装到对应服务器，
授权 `bot`、`applications.commands` 并允许它查看目标频道、发送消息和嵌入链接。
[已有 Bot 设置与功能说明](discord-bot.md)。服务通过 `--credentials` 读取 mode-600 普通文件，
不会把 Bot Token 放进 systemd 的 `EnvironmentFile`；凭据文件不能是符号链接。

## 2. 准备 named tunnel 与 Grok 地址

使用你已有的 Cloudflare named tunnel，或自行完成登录和创建。Cloudflare 的
[官方创建流程](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/)
会提供 Tunnel UUID 和对应 JSON 凭据；创建 tunnel 本身不等于已经启动转发。
将该 JSON 文件以普通文件保存到 `runtime/credentials/<UUID>.json`，并设置 `chmod 600`。

在 `grok-tunnel.yml` 中把 `tunnel` 与凭据文件名填为同一个 UUID，`credentials-file` 填完整绝对路径：
`/home/postedism/Desktop/job-search/runtime/credentials/<UUID>.json`。
`hostname` 填你选择的主机名，并与 `grok.env` 中 HTTPS 地址的主机部分一致。
保留 `service: http://127.0.0.1:8769` 和最后的 `http_status:404`。
Cloudflare [官方 ingress 说明](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/)
要求末尾有匹配其他请求的规则。**公开隧道只转发 8769，不能转发无 Bearer 认证的 8767。**

在已完成 Cloudflare 登录的 Linux 终端，填写下面两个非秘密变量后创建 DNS 路由并检查规则：

```bash
cd ~/Desktop/job-search
TUNNEL_UUID='填写实际 Tunnel UUID'
MCP_HOST='填写实际主机名，不带 https://'
chmod 600 "runtime/credentials/$TUNNEL_UUID.json"
./bin/cloudflared tunnel route dns "$TUNNEL_UUID" "$MCP_HOST"
./bin/cloudflared --config "$PWD/runtime/config/grok-tunnel.yml" tunnel ingress validate
./bin/cloudflared --config "$PWD/runtime/config/grok-tunnel.yml" tunnel ingress rule "https://$MCP_HOST/mcp"
```

检查命中的是 `http://127.0.0.1:8769`。隧道服务会先检查 YAML 中指定的 JSON 是否位于
`runtime/credentials/`、名称与 UUID 一致、是 mode-600 普通文件且非符号链接；不会读取凭据内容。
缺文件时服务跳过启动，配置错误或认证失败需要修正后再启动。

填完后，在 Grok 中需要使用的 URL 是 `https://<你的主机名>/mcp`，认证凭据与 Linux
`mcp-token` 文件一致。Grok 客户端是否支持这个静态 Bearer 认证方式仍需实际验证；
如果界面仅支持 OAuth，不要关掉服务认证或改转发 8767。参见
[Grok 接入与验收说明](remote-mcp.md#grok-connector-acceptance)。

## 3. 由你选择启动已配置的组件

确认配置和凭据填写完成后，按需要执行以下命令。公开 MCP 保持 `127.0.0.1:8769`、
Bearer 必需和 `--control-tools`；本模板不启用 `--handoff-tools`。

部署脚本只同步代码，不安装 unit 文件。仓库里的 unit 有更新时（这次：worker 的
`--steps`、各服务的重启上限），先把它们复制到 systemd 用户目录：

```bash
cd ~/Desktop/job-search
cp auto-application/deploy/{auto-app-worker,auto-app-bot,auto-app-agent,auto-app-agent-public,job-search-grok-tunnel}.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart auto-app-worker.service auto-app-agent.service
```

```bash
systemctl --user daemon-reload
# Discord：启动后会连接 Discord，并可能发送符合条件的待通知内容。
systemctl --user enable --now auto-app-bot.service
systemctl --user --no-pager --lines=0 status auto-app-bot.service
# Grok：先启动认证服务，再启动公开隧道。
systemctl --user enable --now auto-app-agent-public.service
systemctl --user enable --now job-search-grok-tunnel.service
systemctl --user --no-pager --lines=0 status auto-app-agent-public.service job-search-grok-tunnel.service
```

若之前因无文件或配置错误跳过/失败，填好后可用 `systemctl --user restart <服务名>` 重试。
配置错误（凭据缺项、Bot Token 被 Discord 拒绝、MCP token 文件不合格）会以退出码 2 结束，
服务不再自动重启而直接进入 failed，并触发 `OnFailure` 告警（需要 `worker.env` 里的 Webhook）；
其他原因连续崩溃时，30 分钟内重启 10 次后同样进入 failed。修好后先
`systemctl --user reset-failed <服务名>` 再 `restart`。部署脚本会连同 Grok 隧道一起停止并恢复。
检查服务 active 与真实外部连接是两步：Discord 用 `/status`、`/jobs`；Grok 先确认未认证访问
得到 401，再由已认证客户端调用 `get_job_context` 和 `search_jobs`。不要把服务启动当作已连接证明。
本页没有执行这些外部调用，也没有发送测试消息。

worker 模板固定使用 `screen,enrich,summary,health`，不包含 `notify`，由 Bot 发 digest 和
handoff 结果，避免双方重复发送。只用 Webhook、不运行 Bot 时，才在 `--steps` 里加回 `notify`。
worker 与 Bot 的通知职责见 [Discord 运行方式](discord-bot.md)。订阅源采集独立继续运行。

## 4. 可选 Webhook 告警与周报

只有需要故障告警和 worker 周报时才填写。若文件已存在，保留原有值，只在编辑器里修改需要的键。

```bash
cd ~/Desktop/job-search
umask 077
test -e runtime/credentials/worker.env || install -m 600 auto-application/deploy/worker.env.example runtime/credentials/worker.env
nano runtime/credentials/worker.env
chmod 600 runtime/credentials/worker.env
systemctl --user restart auto-app-worker.service
```

`auto-app-alert@.service` 由失败服务的 `OnFailure` 调用，无需独立 enable；没有 `worker.env` 时跳过。
该文件存在但 Webhook 为空时也不会发送告警。填好并重启 worker 后，后续真实故障/到期周报可能发送消息；
无需为完成安装而触发测试告警。Bot Token 不会自动充当 Webhook URL。
