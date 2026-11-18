# 手机上推动投递：设计（2026-11-18）

## 意图

用户在手机上只跟 Grok 说话（或直接跟服务器上的 Claude 说话），就能推动整个投递流程：不用开 Mac，也不在 agent 之间传话。Discord 推送里能标"不该推荐"，规则随之改进；入职时间早于毕业的职位不再推送。

## 目标、非目标、反目标

- **目标**
  - 说"投"之后，几分钟内开始做材料。
  - 做材料之前和之后都能提修改。
  - 随时能问到进度。
  - 有一条直接跟 Claude 对话的渠道。
  - Discord 上能标"不该推荐"。
  - 不再推送入职早于毕业的职位。
- **非目标**
  - Grok 给 Claude 下任意指令。
  - 自动提交申请。
  - 自动发 LinkedIn message。
  - Claude 筛选器（方案 B）：用户对 A 不满意时再做。
- **反目标**（即使能用也算失败）
  - 任何让 grokbot 间接执行任意命令的路径。
  - 没经用户确认就改 `screening.json`。

## 约束

- 项目 CLAUDE.md 规则照旧：
  - 不提交申请。
  - 改筛选规则、搜索和日程前先给用户看，确认后才改。
  - 不读 `.env` 和 `profile/`。
- grokbot 的 sudo 只允许运行 `jobq`。
- token 走 stdin。
- 只用 `scripts/deploy_linux.sh home-ubuntu` 部署，部署前先问用户。
- 服务器的情况（2026-11-18 查过）：
  - Claude 是 claude.ai 登录，版本 2.1.326。
  - 已装 tmux。
  - 打分记录为 0：服务器上没有在跑的 Claude 筛选器。
  - 推到 Discord 的职位只过了 `screening.json` 的固定规则。

## 方案

| 问题 | 考虑过的做法 | 选择 |
|---|---|---|
| 直接对话的渠道 | Remote Control（官方，Claude 手机 App 直接对话，权限确认弹到手机）／Discord Channels（研究预览，要 Bun 和一个新 bot，没人确认时会话卡住）／自己写 Discord `/claude` 命令（不能当场确认权限，要自己维护） | **Remote Control**（我的决定，用户提出要这条渠道） |
| "不该推荐"之后 | A：记理由，汇总成 `screening.json` 修改提案，用户确认才生效／B：加 Claude 筛选器，每个新职位都过模型 | **A**（用户 2026-11-18 选定；不满意再上 B） |
| 加快响应 | `jobq` 写触发文件，配 systemd `.path` 单元／定时器改成每 10 分钟一次 | **触发文件**（我的决定：秒级启动，没活时不做检查；整点定时器保留兜底） |

## 设计

### 1. 加快响应

- `jobq want`、`request`、`questions` 成功后，更新触发文件 `runtime/data/apply-queue.kick` 的时间戳。
- 新增 `deploy/jd-apply-queue.path`：文件一改就启动 `jd-apply-queue.service`。整点定时器保留。
- `jd-apply-queue.sh` 跑完一轮后，如果触发文件比这一轮的开始时间新（运行期间又来了请求），就再跑一轮，最多 3 轮。
- 处理器把自己的状态写进 `apply-kit/runner.json`：是否正在跑、开始和结束时间、退出码、待办数。`jobq status` 读这个文件。

### 2. `jobq request ID --text "用户原话"`

- **谁能用**：grokbot 和 Claude Code 都能用。Dots 用 MCP 里同名的工具。
- **条件**
  - 职位是 want_to_apply 且没归档。
  - 职位没有被认领，也没有填好表在等确认。如果有，报错并提示：在填表的那次对话里用 `questions`，kind 设为 change。
- **效果**
  - 职位还没有 application 记录就新建一条。
  - 写一组修改，kind=change，原话照存。
  - application 状态改成 needs_answers，职位离开队列，Grok 不会拿旧材料去填。
  - 更新触发文件。
- **Claude 这边不用新流程**
  - `todo` 的 needs_answers 已经包含这类职位。
  - 已有材料包的，按修改循环改完，用 `--replace` 重发。
  - 还没有材料包的（folder 为 null），走完整流程，原话作为要求交给生成子 agent。SKILL.md「定时自动处理」第 3 条补一句。
  - 做完后运行 `answers-ready`，职位回到队列。
- **长度**：`--text` 只能一行，最多 1000 个字符（我的决定，和其他文字参数的检查方式一致）。

### 3. `jobq status ID`

- **返回**
  - 决定。
  - application 状态和备注。
  - 认领或"填好等确认"的到期时间。
  - 还没处理的题目和修改：条数和提交时间。
  - 材料包：是否已发布、发布时间、有没有 message。
  - 处理器状态（来自 `runner.json`）。
- **`summary` 字段**：一句人能直接读的话，例如"材料还没做；处理器正在跑（09:12 开始）"。
- **不返回**：处理器日志和 `_runs/` 总结的全文。那是模型写的长文本，里面可能有 JD 内容。
- Dots 用 MCP 里同名的工具。

### 4. 直接对话：Remote Control

- **服务**：新增 `deploy/claude-remote.service`（systemd 用户服务）。
  - 在 tmux 里运行 `claude remote-control --spawn same-dir --permission-mode auto --name job-search`。
  - 工作目录是 `~/Desktop/job-search/auto-application`，环境变量 `AUTO_APP_HOME` 指向 runtime。
  - 断网超过约 10 分钟进程会退出，由 systemd 自动重启。
- **手机**：Claude App 里打开 Code，选这台机器的会话。每开一个新对话就是一个新会话。
- **规则不变**：项目 CLAUDE.md 和技能照常生效，例如改筛选规则仍然先给你看。
- **auto 模式**：我的决定。理由是每条消息都是你本人发的；auto 模式拦下的危险操作会弹到手机上等你确认。
- **首次启动**：你要 ssh 上去手动确认两个提示："Enable Remote Control"和目录信任。这两个我不替你回答。

### 5. Discord「不该推荐」

- **按钮**：推送里每个职位加第四个按钮 "Shouldn't recommend"，只限 owner。点了弹出输入框写理由，必填，最多 300 字。
- **提交后**
  - 职位记为 not_applied，理由前加"不该推荐："。
  - 另写一条筛选反馈（新表，Alembic 迁移）：职位号、理由、当时的标题、公司和筛选信号、时间、处理状态。
- **汇总**：新增 `deploy/screening-feedback.{sh,service,timer}`，每天一次。
  - 先用不调用模型的命令数一下还没处理的理由。少于 3 条、并且最早一条不到 3 天，就不启动 Claude（我的决定）。
  - 否则运行 `claude -p`：读这些理由和当前的 `screening.json`，用现有的 `propose_screening_change` 生成一份修改提案。提案自带预览：多排除几个、少排除几个，各举例子。
  - 关键词解决不了的理由，写进提案说明，不硬改规则。
- **确认**
  - Discord 发出提案：改了什么、预览数字和例子、Apply / Discard 按钮（只限 owner）。
  - Apply 调用 `apply_screening_change`。
  - Apply 或 Discard 之后，这些理由才标为已处理。
  - 提案 24 小时后过期；过期的提案，理由回到待处理，下次汇总重新提。

### 6. 入职早于毕业

- **规则**：`ScreeningRules` 加一个 `graduation` 字段，格式 "YYYY-MM"，默认空（不启用）。`RULES_VERSION` 升到 9，所有职位重新筛一遍。这一步是固定规则，不调用模型。
- **只按明确证据排除**
  - 标题写明更早的毕业届或入职年份，例如 "2026 New Grad"、"New Grad (2026)"、"Class of 2026"、"2026 Start"。
  - 描述写明入职时间早于毕业，例如 "start date: January 2027"、"starting summer 2026"。
    - 季节按它最晚的月份算。Spring 2027 是 3–5 月，不早于 2027-05，所以保留。
    - 只写年份的按 12 月算。
  - 描述写明毕业时间的最晚期限早于毕业，例如 "graduating between Dec 2025 and Jun 2026"、"must graduate by August 2026"。
- **不排除**
  - 写在 preferred 或 nice-to-have 里的。
  - 带 may、typically 这类限定词的。
- **排除原因**：`start_before_graduation:<证据里的年月>`。
- **启用**：用 `propose_screening_change` 提出 `{"graduation": "2027-05"}`，把预览给你看（多排除几个、例子），你确认后才写入。

## 文档

更新以下文档：
- `docs/updates/` 新条目和索引
- 对应的 step 文档
- `README.md`（对照 `--help`）
- `docs/grok-bot.md` 第 9 节
- apply-kit 速查表：给 Grok 加规则，用户提修改就用 `request`，问进度就用 `status`，原话照传

## 部署

本地测试全部通过后，先问你，再用 `scripts/deploy_linux.sh home-ubuntu` 部署。

## 留给实现者

- 正则的具体写法
- 新表和 `runner.json` 的字段名
- Discord 消息的排版
- 触发文件的权限：grokbot 通过 sudo 以 postedism 身份运行，所以能写
