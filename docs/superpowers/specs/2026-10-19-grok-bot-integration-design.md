# Grok Bot 接入求职流水线（第一版）设计

状态：草稿，待用户批准；未提交 git。日期 2026-10-19。

## 1. 要解决什么

现在流水线只走到"服务器扫描并筛选"：home-ubuntu 每 30 分钟采集、按规则筛选（2026-10-19：13,962 个职位，规则保留 4,761 个），但没有任何渠道把新职位交给用户，"想投"为 0，申请队列为空。用户要的完整流程是：

1. 服务器照旧定时扫描、筛选。
2. Grok Bot 定时取回新筛出的职位，挑出候选列给用户；用户确认后，Grok 在 auto-app 里把它们标为"想投"。
3. 用户在 Claude Code 里用 jd-resume-tailor 为"想投"的职位生成 resume 和 cover letter，看过后发布给 Grok。
4. Grok 读速查表和该职位的材料，在它的云电脑浏览器里填申请表，第一版停在最后的 Submit 前等用户确认，再把结果写回 auto-app。
5. Grok 记录"已提交"之后，用户让 Claude Code 按真实投递日期写 LinkedIn message，由用户自己发送。

## 2. 用户已经定下的

- 分工如上；Grok 负责挑候选和投递，Claude Code 负责生成材料和 message。
- 不用公网 tunnel，不启用 8769 公网 MCP。Grok 通过 Execution on Local Computer 使用本机工具。
- 第一版就接入 home-ubuntu：Grok Bot 桌面版装在服务器上，用单独的低权限账户，只能跑职位相关的命令。
- 隔离程度：挡住用户的数据、凭据、本机所有服务、局域网和 tailnet；剩下的可见性（进程列表、Tailscale 设备名、打印服务）只检查并报告，不改全机设置。
- 服务器 runtime 不加 `profile/`、`resumes/` 和回答库；简历和个人信息留在 Mac。
- 速查表放在 Mac 的 `~/Desktop/Work/Search job/apply-kit/answers.md`，按"简历 / Agent / 固定 / ASK"四类来源组织（初稿已写好）。凡是简历上有的内容，以该职位新生成的简历为准，不用母版。
- 自我介绍、项目介绍等开放题可由 Grok 或 Claude Code 自行填写，只能使用该职位的简历、cover letter 和 JD 里的事实。
- 积压的 744 个待推送职位，让 Grok 分批看，不一次性标记为已推送。
- LinkedIn message 在 Grok 记录"已提交"之后再生成，用真实投递日期。
- **岗位按签证分三类**：提供担保（例如 Google）、不提供担保（例如 Visa）、要求 security clearance 或美国公民（例如 Amazon Leo）。前两类都要，担保只用于排序；只排除第三类。服务器的筛选规则已经这样做（`citizenship_required` 默认排除，担保只是排序信号）。2026-10-19 已把这条规则写进 jd-resume-tailor（SKILL.md、`data/domains.json`、`references/resume-guide.md`：不提供担保不再需要停下来问，要求 clearance 才停）、auto-app 路线图和 `docs/linux-worker.md`，以及速查表。

## 3. 目标、非目标、反目标

**目标**

- G1：Grok 在服务器上用一组短命令完成：取新职位、看详情、记录用户确认的决定、认领职位、取投递材料包、写回结果、记录网站账号。
- G2：grokbot 账户除了这组命令，碰不到用户的数据和凭据、本机的各种服务、局域网和 tailnet 上的设备。
- G3：Claude Code 能按 auto-app 职位编号取 JD，生成材料后发布到 Mac 的 apply-kit；投递后能按真实日期补写 message。
- G4：Grok 能从 Mac 读速查表和职位材料，并把 PDF 复制到它的云电脑。
- G5：用一个真实职位跑通整条流程，停在 Submit 前。

**非目标**

- 公网 MCP、tunnel、OAuth。
- 服务器保存 profile、简历、回答库或证据文件。
- 启用 Discord bot（另行处理）。
- 自动发送 LinkedIn 消息。
- 编写 Grok 的 skill、提示词或 routine：这些由用户自己写。我们只写接口说明。
- 修改筛选规则或采集源。
- 隐藏进程列表、收紧全机套接字权限等改动整台服务器的加固。

**反目标**（即使功能能用也算失败）

- grokbot 能读到 `/home/postedism` 下的任何文件，或能连上 8767（无认证 MCP）、8768（OpenAI 隧道）、Codex/JetBrains 本地端口、Redis、SQL Server 等服务。
- grokbot 能执行"职位命令"以外的写操作，例如暂停流水线、改筛选规则、改简历登记、跑任意 SQL。
- 投递时用了别的职位的简历，或材料包与 auto-app 职位对不上而没有被发现。
- jd-resume-tailor 现有用法（粘贴 JD、URL、截图）因为这次改动坏掉。
- 现有服务（采集、筛选 worker、Dot 的私有 MCP、准备 worker、OpenAI 隧道）的行为发生变化。

## 4. 约束与事实

- 服务器保持确定性，不内置任何模型或 agent 调用，只提供接口。
- Claude 不替用户提交申请（项目 CLAUDE.md）。投递由用户通过 Grok 进行。
- home-ubuntu：Ubuntu 26.04.1，x86_64；postedism 有一个登录着的 GNOME（Wayland）会话；gdm 没开自动登录；`/home/postedism` 权限 750；只有 postedism 一个普通用户；`kernel.apparmor_restrict_unprivileged_userns = 1`；postedism 的 systemd 用户服务已开启 linger。
- 服务器上除了 auto-app，还有很多本机服务：127.0.0.1 上的 8767（auto-app MCP，无认证，带控制工具）、8768（tunnel-client）、多个 Codex 和 JetBrains 端口、3001、8091、16443 等；0.0.0.0 上的 6379（Redis）、1433（SQL Server）、8081、8083、8084、9090 等。任何本机用户默认都能连这些端口。
- Grok Bot 官方有 Linux 版（Ubuntu 用 .deb，x64）。从 app 自带代码看，本机工具是 Shell、Read、AwaitShell、CopyToBox（本机文件复制到 Grok 的云电脑）和 CopyFromBox；可以登记多台电脑，调用时用 machineId 选择。审批只有"每次询问 / 始终允许 / 从不"三档，没有命令白名单；定时任务触发的审批大约 10 分钟就过期。
- Grok 的浏览器和电脑操作在它的云电脑上进行；云电脑不能直接访问 home-ubuntu 或 Mac。
- 待推送的新职位（与 Discord digest 同一套规则）2026-10-19 有 744 个，自 2026-10-14 worker 首次运行起累计，因为从没有渠道推送过。
- 部署：`scripts/deploy_linux.sh` 把 Mac 上的 auto-application 同步到服务器并执行 `uv sync`，只同步该目录。
- 需要 root 的服务器操作（建账户、sudoers、防火墙、装 .deb、AppArmor）Claude 无法完成，因为 sudo 需要用户输入密码。
- `jobs` 是 shell 内置命令，所以命令名用 `jobq`。

## 5. 整体结构

```text
home-ubuntu（一直开着）
  postedism：auto-app 现有服务 + runtime（数据库、配置、凭据）
     ▲  sudo，只放行一个命令
  grokbot（新）：Grok Bot 桌面版，跑在一个看不见的显示器上
                 本机执行设为"始终允许"；能做的危险操作只有 `jobq …`

Mac（用户的笔记本）
  Claude Code + jd-resume-tailor ──ssh（postedism）──> 同一个 jobq 包装脚本
  apply-kit/：answers.md + jobs/<职位编号>/ 材料包
  Grok Bot：本机执行设为"每次询问"；只用 Read 和 CopyToBox

Grok 的云电脑：浏览器填表；PDF 通过 CopyToBox 从 Mac 复制过来
```

## 6. 组件

### 6.1 服务器：职位命令 `jobq`

auto-app 新增一个轻量命令行入口（新的 console script，启动时不加载 Playwright、Textual 等重依赖），只封装现有服务，不重写业务逻辑。

**运行目录固定。** runtime 根目录（`~/Desktop/job-search/runtime`）由服务器上的包装脚本写死。命令本身不接受 `--db`、`--preferences` 或任何文件路径参数，从不迁移、不建库。包装脚本固定工作目录，用隔离模式启动 Python（不读 `PYTHON*` 环境变量，`sys.path` 不含当前目录），不信任调用方环境。

**输出。** stdout 只输出一个 JSON 对象。业务错误输出 `{"status": "error", "error": "…"}`，退出码 1；参数错误退出码 2。

| 命令 | 作用 | 复用 |
|---|---|---|
| `new [--limit N]` | 待推送的新职位（N ≤ 50，默认 20）和剩余数量；与 Discord digest 同一套规则：只含筛选保留的、签证担保优先、只有标题的职位等两小时 | `worker.digest.pending`（同 `ControlService.digest`） |
| `seen ID…` | 把这些职位标为"已推送给用户"；与 Discord 共用 `notified` 标记 | `store.set_notified` |
| `search [--query --company --source --state --sponsorship --limit --cursor]` | 搜索职位库，默认排除国家和规则排除的职位 | `QueryService.jobs` |
| `show ID` | 一个职位的详情：JD 文本、申请链接、筛选结果、签证信号、决定历史、时间线（含投递日期） | `QueryService.job` |
| `want ID --reason TEXT` | 记录用户确认要投（reason 必填） | `AgentService.record_application_status` |
| `skip ID --reason TEXT` | 记录用户决定不投（reason 必填） | 同上 |
| `queue [--limit N]` | 可投队列，含额度、租约、去重等限制 | `HandoffService.queue` |
| `claim ID` | 认领一个职位，agent 名固定为 `grok-bot`，返回 token 和租约到期时间 | `HandoffService.claim` |
| `packet TOKEN` | 认领后的 JD、申请链接和筛选信息 | `HandoffService.packet` |
| `record TOKEN OUTCOME [--note TEXT]` | 写回结果：submitted / needs_user / blocked / failed / skipped；不接受证据文件 | `HandoffService.record_attempt` |
| `release TOKEN` | 放弃认领 | `HandoffService.release` |
| `account SITE EMAIL [--existing] [--note TEXT]` | 记录在某招聘网站有账号（只记站点和邮箱，不记密码） | `HandoffService.record_account` |

- 每个职位的签证信号（`sponsorship`）在 `new`、`search`、`show` 里都能看到：`offers`（提供担保）、`no`（不提供担保）、`unknown`（未写明）。要求 clearance 或美国公民的职位（`citizenship_required`）已经被筛选排除，不会出现在 `new` 和 `queue` 里。如果在 JD 或申请表里才发现这类要求：没认领的用 `skip`，已认领的用 `record … skipped`，原因写清楚。
- 输入校验：职位编号为正整数；数量有上限；reason 和 note 是纯文本，有长度上限，不含控制字符；不认识的参数直接报错。
- `packet` 里的 profile、answers 和 resumes 会是空的，这是预期结果，因为服务器不存这些数据。
- 不提供：暂停和恢复、筛选规则、过滤器反馈、时间线事件、简历和证据登记、`requeue`、MCP 服务。这些只能由 postedism 本人通过现有 CLI 操作，例如 `needs_user` 或 `blocked` 的职位处理完后，由用户或 Claude Code 执行 `auto-app handoff requeue`。
- Claude Code 在 Mac 上以 postedism 身份通过 ssh 调用同一个包装脚本，不经过 sudo。

### 6.2 服务器：grokbot 账户

**账户**

- 新建普通用户 `grokbot`：有自己的 home（权限 750），锁定密码，不加入 sudo、adm、docker、lxd 等任何组，开启 linger（服务器重启后它的用户服务自动运行）。
- 账户里除了 Grok Bot app 自己的数据，不放任何职位数据或凭据副本。

**命令权限**

- sudoers 只允许 grokbot 以 postedism 身份运行那一个包装脚本，免密码，不保留调用方环境变量。
- `/usr/local/bin/jobq`（root 所有）调用 `sudo -n -u postedism <包装脚本> "$@"`，所以 Grok 直接输入 `jobq new`、`jobq show 123` 即可。
- 包装脚本及其目录对 grokbot 不可写。

**网络出口限制**

用 nftables 单独建一张表，只对 grokbot 用户发出的流量生效，不清空、不改动 Docker、ufw、Tailscale 已有的规则：

1. 已建立的连接放行（这样 postedism 通过 ssh 端口转发连 grokbot 的本地 VNC 时，回包不会被挡）。
2. 放行到 127.0.0.53 和 127.0.0.54 的 DNS。
3. 拒绝目标为本机任何地址的连接（loopback、局域网 IP、tailnet IP、本机 IPv6），对应 nftables 的 `fib daddr type local`。
4. 拒绝私有、CGNAT（tailnet 100.64.0.0/10）和链路本地 IPv4 地址。
5. 拒绝全部 IPv6，并让对方快速失败，让程序立刻退回 IPv4。原因是局域网设备有公网 IPv6 地址，无法按网段挡住。
6. 其余（公网 IPv4）放行。

这张表由一个开机启动的 oneshot systemd 服务加载，必须在 grokbot 的服务之前生效；重复加载结果不变（先删除这张表再重建）。

**剩下的可见性（只检查并报告，不封堵）**

- grokbot 能看到所有进程的命令行（`/proc`）。验收时检查 postedism 的进程命令行里没有 token、密码等秘密；有的话报告给用户，不改全机设置。
- grokbot 能读 Tailscale 的本机状态接口（设备名和 IP），但连不上这些设备。
- grokbot 能使用打印服务等对所有用户开放的本机套接字。

**Grok Bot app 的运行方式**

- 用 Grok Bot 官方 .deb 安装。
- 以 grokbot 的 systemd 用户服务运行：一个看不见的显示器（例如 Xvfb）加 Grok Bot app，服务器重启后自动启动，不碰 postedism 的 GNOME 会话和 gdm。
- 只在 127.0.0.1 上开 VNC，用户从 Mac 用 ssh 端口转发连接，用于第一次登录 Cursor 账号、回答首次的本机执行提示，以及偶尔接手操作。
- 两个已知风险需要实现时验证，并在文档里写清处理方式：
  - AppArmor 限制了非特权 user namespace，Electron 的沙箱可能起不来；如果 .deb 没带 AppArmor 配置，就为它加一个只放开 `userns` 的配置，不使用 `--no-sandbox`。
  - 没有桌面钥匙串时，app 的登录状态可能保存不住；需要验证重启 app 后仍保持登录。
- 这台电脑在 Grok Bot 里的"本机执行"设为"始终允许"，危险操作的边界由账户权限保证。由用户在 app 里设置。

### 6.3 Mac：apply-kit 目录约定

```text
~/Desktop/Work/Search job/apply-kit/
  answers.md          速查表（已有）
  README.md           目录约定、文件含义和 jobq 命令清单，给用户和 agent 看的接口说明
  jobs/
    index.json        已发布的职位清单：职位编号、公司、职位、发布时间、是否已有 message
    <职位编号>/
      job.json        清单：职位编号、公司、职位、申请链接、每个文件的 sha256 和字节数、简历 build_id、发布时间
      jd.txt
      resume.pdf
      cover_letter.pdf
      answers.md      Claude Code 写好的本职位开放题回答
      message.txt     LinkedIn 消息（Subject / Body / Connection note）；投递之后才出现，用户自己发
```

- 这个目录不在任何 git 仓库里，含个人信息。
- Grok 在 Mac 上只需要 Read 和 CopyToBox，不需要 Shell。
- 投递前，Grok 核对 `job.json` 里的职位编号与认领的职位一致；复制到云电脑后，用 sha256 核对文件与 `job.json` 一致。这是接口约定，由用户写进自己给 Grok 的指示里。

### 6.4 jd-resume-tailor 的改动

新增一个只用标准库的脚本（例如 `scripts/apply_kit.py`），配置项包括服务器 ssh 主机、`jobq` 包装脚本路径和 apply-kit 路径，都不涉密，配置文件不放在 `data/` 下（以免影响数据完整性测试）。子命令：

- `todo`：列出两类待办：队列里还没有发布材料包的职位（`jobq queue` 减去 `index.json`）；已提交但还没有 message 的职位（`index.json` 里的职位中，`jobq show` 显示已投递、材料包里没有 `message.txt` 的）。
- `fetch 职位编号 --out output/<目录>`：通过 `jobq show` 取职位，写出 `jd.txt`（JD 原文）和 `job.json`（职位编号、公司、职位、申请链接、地点、签证信号、投递日期（如果已投递）、取回时间）。服务器上没有 JD 原文时，不写 `jd.txt`，并返回申请链接，由技能按现有流程用 WebFetch 获取。
- `publish output/<目录>`：先核对 resume 和 cover letter 的 build 报告状态成功、PDF 的 sha256 与报告一致、简历只有 1 页、`job.json` 有职位编号；再整目录原子地写入 `apply-kit/jobs/<职位编号>/`，写出带哈希的 `job.json`，原子更新 `index.json`。已存在的材料包只有加 `--replace` 才覆盖。不要求 `message.txt`。
- `add-message output/<目录>`：职位必须已经投递（`jobq show` 的时间线里有 applied 记录），把 `message.txt` 写进已发布的材料包，并更新 `job.json` 和 `index.json`。

SKILL.md 的改动：

- 第一阶段：JD 来源新增"auto-app 职位编号"，用 `fetch`；输出目录名带职位编号（`output/<公司_岗位>_<职位编号>/`）。
- 第八阶段（InMail）：从 auto-app 来的职位在生成材料时跳过，等投递之后再写。新增"投递后写 message"：`todo` 列出已提交但没有 message 的职位后，新建一个生成子 agent，给它 `jd.txt`、`analysis.md`、方向 track、当前的 `resume.tex` 和 `cover_letter.tex`，以及 auto-app 记录的投递日期；按现有 LinkedIn 指南写"已申请"版本，只引用这份简历上的经历；存成 `message.txt`（同时打印到屏幕），再执行 `add-message`。粘贴 JD 的旧用法仍按现有规则当场生成。
- 生成子 agent 额外写一份 `answers.md`：几道常见开放题（为什么选这家公司或这个岗位、自我介绍、一个项目或技术难题）的英文回答，每道不超过 100 词，只用这份简历和 cover letter 里的事实。修改循环中与 cover letter 一起更新。
- 新增最后一步：用户确认最终版本后执行 `publish`；没有用户确认不发布；没有职位编号（粘贴 JD 的用法）时跳过。
- 支持"处理队列"：先 `todo`，再按现有规则每个 JD 起一个生成子 agent。
- 现有用法（URL、截图、粘贴文本）保持不变。

### 6.5 Grok Bot 的设置（用户做，不写进代码）

- 在 home-ubuntu 上以 grokbot 身份安装、登录同一个 Cursor 账号；这台电脑的本机执行设为"始终允许"。
- Mac 的本机执行设为"每次询问"。
- 在 Auto-review 里加一条 Ask first 规则：提交申请表之前问我。
- 用户自己写 Grok 的 skill 或 routine；apply-kit 的 README 提供命令和文件说明。

## 7. 一个职位的完整走向

1. 服务器筛选保留 → 出现在 `jobq new`。积压的 744 个也从这里按签证担保优先、每次最多 50 个出来，头几天量会大一些。
2. Grok 列给用户 → 执行 `jobq seen`（之后不再出现在 `new` 里，Discord 以后也不会重复推送）。
3. 用户确认 → `jobq want`（"想投"永不过期）→ 出现在 `jobq queue`。用户拒绝 → `jobq skip`。
4. Claude Code `todo` / `fetch` → 生成 resume、cover letter、`answers.md` → 用户确认 → `publish`。
5. Grok：`claim` → `packet` → 核对材料包 → 填表 → 用户确认 Submit → `record submitted`（自动记为 applied 并写入时间线）。需要用户处理时 `record needs_user`，卡住时 `record blocked`（都会暂停这个职位，直到用户 `requeue`）；失败 `record failed`（可再认领）；放弃 `record skipped`（记为不投）。
6. Claude Code `todo` 列出这个已提交的职位 → 按真实投递日期写 message → `add-message` → 用户隔 1–2 天自己在 LinkedIn 发送。
7. 额度：沿用 handoff 默认值（每天 20 个、同一公司 30 天 3 个）。租约改为 120 分钟（见 §9）。

## 8. 验收

1. auto-app：新命令的单元测试（用虚构职位的临时数据库），覆盖每个命令、参数错误、拒绝路径类参数、固定 agent 名；全量测试通过。
2. jd-resume-tailor：`apply_kit.py` 的单元测试（假 ssh、临时目录）：`fetch` 的有 JD、无 JD、已投递三种情况；`publish` 在 build 失败、哈希不符、超过 1 页、缺文件、重复发布时拒绝；`add-message` 在未投递时拒绝；`todo` 两类待办都能列出；现有 63 个测试仍通过。
3. 服务器，以 grokbot 身份：
   - `jobq new`、`jobq show`、`jobq queue` 正常。
   - 读 `/home/postedism` 被拒；`sudo -l` 只列出 jobq 命令；`sudo -u postedism id` 被拒；docker、lxd 套接字不可用。
   - 连 127.0.0.1:8767、127.0.0.1:8768、本机 IP 的 6379 和 1433、Mac 的 tailnet IP 都失败；访问公网 HTTPS 成功。
   - 传 `--db` 等参数被拒。
   - 报告剩下的可见性：postedism 进程命令行里有没有秘密、Tailscale 状态接口和打印服务是否可用。
4. Grok Bot 在 home-ubuntu：app 在看不见的显示器上运行并保持登录；重启服务后仍保持登录；在 Grok 对话里成功执行一次 `jobq new`。
5. 端到端（用户在场）：一个真实职位依次经过 Grok 列候选 → 用户确认 → `want` → Claude Code `fetch`、生成、发布 → Grok 读材料包、复制、核对哈希、填表、停在 Submit 前 → 用户决定 → `record` → `jobq show` 显示结果 → 如果已提交，Claude Code 写 message 并 `add-message`。Claude 不点 Submit。

## 9. 我替用户做的决定（请过目）

- 命令名用 `jobq`，因为 `jobs` 是 shell 内置命令。
- `seen` 与 Discord 共用"已推送"标记：Grok 推过的职位，以后启用 Discord 也不会再推。
- `want` 和 `skip` 必须带 reason，用于留下"用户确认过"的记录；服务器无法验证用户是否真的确认过，这一点靠用户给 Grok 的指示。
- 租约从 30 分钟改为 120 分钟（写在服务器 `runtime/config/handoff.json`，这是额度配置，不是个人数据），因为等用户确认 Submit 可能超过 30 分钟。
- grokbot 只能访问公网 IPv4，IPv6 全部拒绝。
- Grok Bot app 跑在看不见的显示器上，而不是让 gdm 自动登录 grokbot。
- 输出目录名带职位编号；Claude Code 默认写本职位的 `answers.md`。

## 10. 交给实现者决定的

- 新命令的模块名、console script 名、参数解析库（建议与 `auto-app-query` 一样用 argparse，保证启动快）。
- 包装脚本在仓库里的位置和部署方式（要求：sudoers 里是固定绝对路径、grokbot 不可写、随部署更新或有明确的安装步骤）。
- nftables 规则写法、systemd 单元名、看不见的显示器用 Xvfb 还是无头 Wayland。
- 需要 root 的步骤做成一个可重复执行的安装脚本加说明，由用户在服务器上用 sudo 运行。

## 11. 风险与未验证项

- Grok Bot 在无头显示器下能否正常运行、保持登录、使用本机执行，尚未验证（§6.2）。如果不行，退路是用 gdm 给 grokbot 开一个真实的图形会话；这会改变服务器屏幕的使用方式，需要用户同意。
- Grok 读到的网页可能藏着诱导性指令。在服务器上，危险操作被账户权限挡住；在 Mac 上，每个操作都要用户批准。
- `/tmp` 等公共目录里如果有 postedism 写出的全局可读文件，grokbot 能读到。验收时检查一次。
- 头几天 Grok 要分批看完 744 个积压职位，会多用一些 Grok 额度。
