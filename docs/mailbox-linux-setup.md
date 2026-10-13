# Gmail / Outlook 接入与 Linux 定时运行

**最新决定（2026-10-10）：** 用户已停止 OAuth 应用创建，暂不启用邮箱来源。以下 OAuth 步骤保留作备选；当前考虑把 LinkedIn、Jobright、Simplify 的推荐邮件集中到 Gmail，再走 IMAP。通用 IMAP 适配器尚未实现，不能把该方案当作已经接通。

不创建 OAuth 应用的两条路线：

- **个人 Gmail + IMAP + 应用专用密码：** 无需 Google Cloud 项目。账号须启用两步验证并有应用密码入口；部分组织账号、仅安全密钥两步验证或 Advanced Protection 账号可能不提供该入口。应用密码不是只读权限，读取程序应使用只读邮箱和不标记已读的正文获取方式；建议只汇入这三个平台的推荐邮件。[Google 应用密码](https://support.google.com/accounts/answer/185833)
- **Proton Mail Bridge + 本地 IMAP：** Ubuntu 可以运行 Bridge，但要求包含 Proton Mail 的付费套餐，还需要 Bridge 常驻及本地凭据存储。Gmail 转发到 Proton 只解决邮件汇集，程序仍要经过 Bridge 读取；若只是为了简化采集，这比直接读 Gmail 多一层。[Proton Bridge](https://proton.me/mail/bridge)、[Linux 支持](https://proton.me/support/bridge-for-linux)

Gmail 可用过滤器只转发特定推荐邮件，不必迁移整个邮箱。现成的手动备选仍是把选定邮件导出成 `.eml`，交给当前采集器读取。[Gmail 转发说明](https://support.google.com/mail/answer/10957)

代码已提供只读邮箱采集、OAuth 授权和后台 worker。真实邮箱授权需要你登录；Linux 服务需要在目标机器安装后才能称为运行中。下列命令使用同一份 `feeds.yaml` 和数据库，自动投递继续关闭。

**当前部署（2026-10-10）：** `home-ubuntu` 的 `/home/postedism/Desktop/job-search` 已部署并运行公共仓库定时采集，完成多轮检查；Gmail/Outlook 仍等待授权。目录、状态命令和验收结果见[部署记录](updates/2026-10-10-linux-deployment.md)。下文保留通用配置与安装步骤。

## 1. 从零创建 Google 应用并授权个人 Gmail

当前 Google 和 Microsoft OAuth 应用均未创建。以下网页设置由你在自己的浏览器完成，官方页面已于 2026-10-10 核对。

1. 用个人 Google 账号打开 [Google Cloud Console](https://console.cloud.google.com/)，创建或选择一个项目；在 **APIs & Services → Library** 搜索并启用 **Gmail API**。[官方准备步骤](https://developers.google.com/workspace/gmail/api/quickstart/python)
2. 打开 **Google Auth platform → Branding → Get started**，填应用名（如 `Personal Job Search`）、自己的支持邮箱和联系邮箱；个人 Gmail 的 Audience 选择 **External**。先保持 **Testing**，到 **Audience → Test users → Add users** 加入实际要读取的 Gmail 地址。[官方同意屏幕步骤](https://developers.google.com/workspace/guides/configure-oauth-consent)
3. 在 **Data Access → Add or Remove Scopes** 添加 `https://www.googleapis.com/auth/gmail.readonly` 并保存。它允许读取邮件正文，是 Gmail 的 restricted scope；项目需要正文解析职位，不能用只读邮件元数据权限替代。[官方权限表](https://developers.google.com/workspace/gmail/api/auth/scopes)
4. 在 **Clients → Create client** 选择 **Desktop app**，创建后下载 JSON。将文件留在本机私有目录，下一步使用它的实际路径。[官方 Desktop client 步骤](https://developers.google.com/workspace/gmail/api/quickstart/python#authorize_credentials_for_a_desktop_application)

给现有 `home-ubuntu` worker 授权时，在 **Mac 终端**执行；先把占位路径替换为下载文件的绝对路径：

```bash
ssh home-ubuntu 'mkdir -p /home/postedism/Desktop/job-search/runtime/credentials && chmod 700 /home/postedism/Desktop/job-search/runtime/credentials'
scp "/ABSOLUTE/PATH/google-client.json" home-ubuntu:/home/postedism/Desktop/job-search/runtime/credentials/google-client.json
ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:8765:127.0.0.1:8765 home-ubuntu
```

保持这个 SSH 会话，在其 **Linux 终端**执行：

```bash
chmod 600 /home/postedism/Desktop/job-search/runtime/credentials/google-client.json
cd /home/postedism/Desktop/job-search/auto-application
env AUTO_APP_HOME=/home/postedism/Desktop/job-search/runtime .venv/bin/auto-app ingest auth gmail \
  --client /home/postedism/Desktop/job-search/runtime/credentials/google-client.json \
  --credentials /home/postedism/Desktop/job-search/runtime/credentials/gmail.json \
  --no-browser --port 8765
```

在 **Mac 浏览器**打开终端输出的 Google URL，选择已加入 Test users 的 Gmail，完成只读授权；回调通过 SSH 返回 Linux，等待终端确认保存路径。当前命令等待五分钟，超时后重新执行；若更换端口，SSH 转发和 `--port` 必须一起更换。代码使用 loopback callback、PKCE 和 `access_type=offline`，不需要手动复制回调授权码。[Google Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)

仅在 Mac 本地试用时，也可在项目目录执行 `uv run auto-app ingest auth gmail --client /实际路径/google-client.json --credentials credentials/gmail.json`；省略 `--no-browser` 会尝试打开本机浏览器。该命令只给本地程序保存令牌，Linux worker 应使用上面的远程授权步骤。

**七天限制：** External + Testing 的应用签发的 refresh token 七天过期；只有所请求权限全部属于姓名、邮箱地址和基本资料的组合才有例外，`gmail.readonly` 不属于这个例外。长期运行时可在 **Audience** 中将应用发布为 **In production**，并重新授权取得新令牌，或保持 Testing 并在到期后重新授权。Google 另列有个人自用的验证例外；发布状态和通过验证是两件事，仍按控制台实际提示处理。移出 Testing 只消除该七天规则，令牌仍可能因撤销授权等原因失效。[令牌失效条件](https://developers.google.com/identity/protocols/oauth2#expiration)、[个人自用验证例外](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification#personal-use)

## 2. 从零创建 Microsoft 365 组织应用并授权 Outlook

已确认 Outlook 是学校/公司的 Microsoft 365 邮箱。先用该组织账号打开 [Microsoft Entra admin center](https://entra.microsoft.com/)，切到邮箱所属目录。

1. 在 **Entra ID → App registrations → New registration** 创建应用（如 `Personal Job Search`）；账号类型选 **Single tenant only / Accounts in this organizational directory only**。完成后在 **Overview** 记下 **Application (client) ID** 和 **Directory (tenant) ID**。[官方注册步骤](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)
2. 在 **Authentication → Advanced settings** 将 **Allow public client flows** 设为 **Yes** 并保存。本项目使用 device code flow，注册时的 Redirect URI 可以留空，不需要 client secret。[官方 public-client 配置](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration#enable-public-client-flow)
3. 在 **API permissions → Add a permission → Microsoft Graph → Delegated permissions** 添加 **Mail.Read**；不要选 Application permissions。命令实际请求 `offline_access` 和 `https://graph.microsoft.com/Mail.Read`，后者读取当前登录用户邮箱，前者用于刷新。[Graph 权限表](https://learn.microsoft.com/en-us/graph/permissions-reference#mailread)、[设备码与刷新令牌](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)

普通成员的应用注册权限和用户同意权限都可能被组织调整；先尝试上述步骤和登录。只有实际遇到“无权注册”“Need admin approval”或设备登录被组织策略阻止时，才根据该错误请 IT 允许/代建应用或处理授权。`Mail.Read` 的 **Delegated** 版本本身标记为不要求管理员同意，不能据此断言学校一定允许或一定禁止。[默认用户权限](https://learn.microsoft.com/en-us/entra/fundamentals/users-default-permissions)、[组织用户同意设置](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-user-consent)

登录 `home-ubuntu` 后，在 **Linux 终端**执行；替换两个 ID：

```bash
cd /home/postedism/Desktop/job-search/auto-application
env AUTO_APP_HOME=/home/postedism/Desktop/job-search/runtime .venv/bin/auto-app ingest auth outlook \
  --client-id YOUR_APPLICATION_CLIENT_ID \
  --tenant YOUR_DIRECTORY_TENANT_ID \
  --credentials /home/postedism/Desktop/job-search/runtime/credentials/outlook.json
```

在 Mac 或手机浏览器打开终端提供的网址，输入该次设备码，以学校/公司账号登录并同意只读权限，等待 Linux 终端确认保存。此流程不需要 SSH 端口转发；明确传入租户 ID，避免单租户应用误用 CLI 默认的 `common`。[Microsoft device authorization](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)

账号密码、MFA、设备码均由你在登录页面处理；无需在聊天中发送密码、client secret 或 token。两个授权文件由程序以 `0600` 权限保存，刷新会原子替换并保存轮换后的 refresh token。项目 `credentials/` 已忽略，Linux 的 `runtime/credentials/` 也不属于代码同步目录。

## 3. 设置采集范围

当前范围已确认为 **LinkedIn、Jobright、Simplify 的职位推荐邮件**。在 Gmail 建立 `Jobs` 标签，只将这三类推荐邮件加入该标签，可在 Gmail 内设置过滤规则自动加标签。Outlook 先从实际推荐邮件查看完整 **From 地址**；三个域名不能代替 `senders` 中的完整地址，也不要凭平台名猜发件人。

已有 worker 编辑 `/home/postedism/Desktop/job-search/runtime/config/feeds.yaml`，保留公共仓库来源。新安装才从 `config/feeds.example.yaml` 复制模板。将下面两项合并进现有 `sources:`；占位发件人替换、授权和范围确认完成后，逐项把 `enabled` 改为 `true`。每个账户使用独立来源名和 credentials 文件。

```yaml
sources:
  gmail-jobs:
    enabled: false
    kind: mailbox
    provider: gmail
    credentials: ../credentials/gmail.json
    query: 'label:Jobs {from:linkedin.com from:jobright.ai from:simplify.jobs}'
    lookback_days: 14
    page_size: 50
    max_pages: 4
  outlook-jobs:
    enabled: false
    kind: mailbox
    provider: outlook
    credentials: ../credentials/outlook.json
    folder: inbox
    senders:
      - LINKEDIN_RECOMMENDATION_FROM_ADDRESS
      - JOBRIGHT_RECOMMENDATION_FROM_ADDRESS
      - SIMPLIFY_RECOMMENDATION_FROM_ADDRESS
    lookback_days: 14
    page_size: 50
    max_pages: 4
```

Gmail `query` 先在 Gmail 搜索框核对；也支持 API `label_ids`。Outlook 上述配置取 **inbox 与指定发件人的交集**；已被规则移出 inbox 的推荐邮件需改用对应文件夹 ID，或去掉 `folder`、保留已核实的 `senders`。自定义文件夹显示名不能代替 Graph folder ID；现有 CLI 未提供列举 folder ID 命令。若同一个 From 也发送非推荐通知，应使用只存推荐邮件的专用文件夹再限定发件人。两平台都要求明确范围。

OAuth 授予的是邮箱只读权限；程序实际采集范围由上述配置缩小。邮箱接口只调用 GET，不发送、删除、移动邮件，也不改变已读状态。Gmail 读取 raw MIME，Outlook 读取 `/$value` MIME，再复用已有邮件岗位解析器。[Gmail list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)、[Graph MIME](https://learn.microsoft.com/en-us/graph/outlook-get-mime-message)

首次扫描最近 14 天，后续按收到时间增量扫描，保留两分钟重叠；重叠区邮件通过已处理记录跳过。分页窗口在处理完之前固定，`max_pages` 限制一次工作量，下一轮继续；有失败的页面不会越过。远端已删除的邮件记录为 `messages_gone`，不会挡住后续邮件，也不会把对应岗位判成已关闭。

这是**收到时间的增量扫描**。手动把较旧邮件移入目标范围，或需要补录历史邮件时，可用另一个来源名配置所需 `lookback_days` 做一次补扫，或者导出 `.eml` 导入。修改 query/folder 会重启该范围的初始窗口；已完成邮件有记录，仍不会重复入库。

授权及范围确认后，在 Linux 项目目录执行一次并检查输出：

```bash
env AUTO_APP_HOME=/home/postedism/Desktop/job-search/runtime .venv/bin/auto-app ingest run \
  --config /home/postedism/Desktop/job-search/runtime/config/feeds.yaml \
  --db /home/postedism/Desktop/job-search/runtime/data/auto_app.db
.venv/bin/auto-app-query --db /home/postedism/Desktop/job-search/runtime/data/auto_app.db context
.venv/bin/auto-app-query --db /home/postedism/Desktop/job-search/runtime/data/auto_app.db jobs --source gmail-jobs
.venv/bin/auto-app-query --db /home/postedism/Desktop/job-search/runtime/data/auto_app.db jobs --source outlook-jobs
```

`ingest run` 自带 JSON 输出，无需 `--json`。`sources[].ingest_run` 提供每个来源最后运行、最后成功、数量、错误及是否还有待处理页面；`sources[].mailbox` 提供日期窗口/进度。再次执行同一导入命令，核对已处理邮件跳过、已有岗位不重复创建；新收到的邮件仍可产生新增。一个来源失败时，其余来源继续执行。已有 worker 下一轮重读配置，无需重新安装服务；若手动导入遇到并发锁，等当前轮结束再试。

## 4. 启动每 30 分钟采集

以下是新安装或前台调试的通用命令，相对路径应指向同一个运行目录。已部署的 `home-ubuntu` 使用既有 systemd 服务，邮箱配置会在下一轮生效。

```bash
uv run auto-app ingest watch --config config/feeds.yaml --db data/auto_app.db --interval 1800
uv run auto-app ingest status --db data/auto_app.db
```

首次启动立即执行，之后每轮完成后等待 1800 秒。重启会保留下一次运行时间，避免启动时重复跑；需要立即验证时使用 `watch --once`。每轮重新加载配置，失败下一轮重试。SIGTERM 在当前轮结束后退出，Ctrl-C 会保存中断状态。服务退出前已完成的邮件和分页记录会保留。

状态文件在数据库旁，例如 `auto_app.db.ingest-state.json`，同一数据库使用 worker 锁与导入锁防止并发重复处理。`ingest status` 只读状态文件和运行锁，不创建数据库。此 worker 不需要 `searches.yaml`、浏览器登录或模型筛选器。旧的 `serve run` 仍用于浏览器来源和模型筛选。

## 5. 安装 Linux 用户服务

将当前代码、`uv.lock`、你的 `feeds.yaml` 和授权文件放到 Linux 用户拥有的目录；在目标机器执行 `uv sync --frozen`。若代码与运行数据分开，credentials 路径仍相对于运行目录下的 feeds.yaml 解析。下面是通用安装模板；`home-ubuntu` 已按部署记录中的实际路径完成服务安装。

```bash
mkdir -p ~/.config/systemd/user
uv run auto-app ingest systemd \
  --project /home/YOUR_USER/auto-application \
  --home /home/YOUR_USER/auto-app-home \
  --uv /home/YOUR_USER/.local/bin/uv \
  --interval 1800 > ~/.config/systemd/user/auto-app-ingest.service
systemctl --user daemon-reload
systemctl --user enable --now auto-app-ingest.service
systemctl --user status auto-app-ingest.service
journalctl --user -u auto-app-ingest.service -n 50 --no-pager
```

默认读取运行 home 的 `config/feeds.yaml`，写入其 `data/auto_app.db`。生成命令也支持显式 `--config` 和 `--db`。如果希望注销 SSH 后用户服务仍长期运行，目标机器还需允许该用户的 linger；部署时检查现有配置，再按需要执行 `loginctl enable-linger YOUR_USER`。

验收应包括：systemd 显示服务正在运行；`ingest status` 显示成功的一轮和下一次时间；数据库含实际邮箱来源；再跑一轮没有重复岗位。只生成 service 文本不算完成部署。

## 6. 待完成动作与替代方式（2026-10-10）

目前 Gmail/Outlook 均未完成本项目的真实授权。还需你创建 Google Desktop OAuth 应用、将下载文件保存到实际路径，创建组织 Microsoft 应用并取得 client ID/tenant ID，在自己的浏览器完成两次登录同意，以及核实推荐邮件标签和 Outlook 完整发件人地址。完成后才能启用邮箱来源并验收真实采集；客户端 JSON 和 ID 准备完成不等于已连接。

OAuth 授权本身是邮箱级只读；query/folder/senders 限制程序实际读取范围，并非缩小令牌权限。Codex 中已连接 Gmail 不代表 Linux worker 已获得授权。上述步骤仅修改用户随后选择配置的应用和本项目配置，无需把任何凭据发进聊天。

| 方式 | 当前可用性与取舍 |
|---|---|
| Gmail API / Outlook Graph OAuth | 已实现，支持后台刷新；首选长期运行方式。Outlook public-client device flow 不需要 client secret，实际遇到组织授权限制时再处理 |
| 导出 `.eml` 到固定目录 | 已实现导入及定时扫描；无需项目在线邮箱授权，导出动作需另行完成 |
| Gmail IMAP + App Password | 尚未实现；账号必须开启两步验证且允许 App Password，不能假设每个账号都有。见[官方说明](https://support.google.com/accounts/answer/185833) |
| Gmail / Outlook IMAP OAuth | 尚未实现，仍需 OAuth 应用；已有 API 路线更直接。见 [Google](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol)、[Microsoft](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth) |

Exchange Online 的 IMAP Basic Auth / App Password 不能作为组织邮箱 OAuth 的通用替代，见[停用说明](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-basic-authentication-exchange-online)。当前重新授权会生成新的账户实例标识并重扫配置窗口；这是身份隔离机制，不应在未核实同一账户前复用旧标识。重复岗位仍依实际职位身份去重。
