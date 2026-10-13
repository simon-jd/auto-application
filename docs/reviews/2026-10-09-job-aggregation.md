# 多来源职位聚合与公开反馈：代码审查和调优草案

日期：2026-10-09（America/Los_Angeles）。审查基线：`main`，`008bf87a47eb8b379216ba5eebe07197a5a6d46e`。

状态：已完成静态代码审查；本文是供用户审阅的建议，尚未修改业务代码或数据库。当前 checkout 没有 `.venv`；没有安装依赖、运行测试、真实采集、模型调用或投递，也没有测量性能提升。

## 目标与约束

用户要求保留已有功能，把重点放在多个仓库和信息源的职位抓取；外部反馈明确指公司与岗位的公开面经、签证赞助、招聘体验、岗位可信度。自动投递保留接口，继续默认关闭。

保留现有 CLI 命令和 JSON 输出、Textual TUI、Web 职位浏览与决策、AI 筛选、人工标签与审计评估、手工投递队列与历史、TTL/归档、定时运行、原有 LinkedIn/ATS 采集，以及已有投递适配器。新增信息应能解释来源、时间和适用对象。

假设：继续供单人在本机使用；首批新增职位源采用公开 GitHub 职位清单，公开反馈以用户指定链接、GitHub 讨论/Issues 和 Hacker News 的可读取接口起步。通用搜索服务采用可替换 provider；实际搜索服务和其他社区适配器另行配置，尚未选择付费供应商。

## 技术栈判断与方案比较

当前 Python 3.12/uv、httpx/asyncio、SQLAlchemy/Alembic/SQLite、Playwright、Typer/Textual、FastAPI/React/TypeScript 足以承载这一轮目标。优化重点是增量更新、查询方式、复用研究证据和接口边界；没有数据支持宣称语言或数据库是当前性能瓶颈。

| 方案 | 做法 | 代价与适用范围 |
| --- | --- | --- |
| 建议：扩展现有应用 | 复用现有 pipeline、来源插件、存储和审核，增加 HTTP 元信息、仓库适配器和公开证据模块 | 对已有功能的影响范围最小，可以分批验证 |
| 引入完整爬虫/工作流引擎 | 把请求调度迁入新框架，保留应用层 | 需要重新适配已有浏览器、预算、状态与重试；大量动态页面和复杂流程出现后再评估 |
| 拆分独立服务 | 分离采集、研究、筛选，使用独立任务队列与数据库服务 | 适合多人或多机器长期运行；当前缺少需要承担这种运维成本的证据 |

建议保留 SQLite 的 WAL 与短事务，先优化查询和写入批次。SQLite WAL 允许读写并行，但同一时刻只有一个 writer，新增研究 workers 应遵循已有写入边界。参见 [SQLite WAL](https://www.sqlite.org/wal.html)。

## 代码中已确认的调整点

以下行号来自审查基线。性能影响和界面风险均未实测；表中区分代码行为与由其推导的风险。

| 优先级 | 证据 | 当前行为与影响 | 调整建议 |
| --- | --- | --- | --- |
| P1 | `collect/collector_base.py:72`、`collect/api_collector.py:177` | 同一来源的已知 ID 只 touch，直接跳过保存。职位内容变更不会进入解析链 | 稳定 ID 加语义内容指纹；未变刷新见到时间，变更更新同一 posting 并重新解析 |
| P1 | `sources/registry.py:66`、`sources/api.py:21`、`collect/api_collector.py:171` | API 的 platform 声明与单雇主 company 校验耦合；RawJob 没有逐职位 company，卡片显示固定来源名 | 将传输/来源类型与雇主 ATS 身份分开；增加可选的逐职位 company，兼容现有公司插件 |
| P1 | `collect/api_collector.py:46`、`:73` | 请求只返回 JSON；没有 headers/text/304。403/429 都进入固定冷却 | 加响应 envelope 和条件请求；按 provider/host 共享限额，读取 Retry-After/reset，保留真实访问阻断的停止行为 |
| P1 | `core/db/tables.py:237`、`select/runner.py:83` | feedback 是用户的 good/bad 标签；网上检索仅嵌在每次判断 trace 中 | 独立保存公开证据及关联，保留原 feedback 含义 |
| P1 | `select/engine.py:63`、`core/db/store.py:465` | 判断复用只看 job/filter_version/status；同版本重跑覆盖旧结果 | 给输入与证据版本计算 fingerprint，保留每次研究/判断记录；只让未决岗位自动重新筛选，保留人工决定 |
| P1 | `web/src/App.tsx:118`、`:179` | 选中 B 时旧 A 的 detail 在请求返回前仍存在，动作使用 detail.job_id | 动作前验证 detail 与 selected 一致，忽略过时响应；增加快速切换的行为回归 |
| P2 | `collect/api_collector.py:132`、`platforms/workday.py:144` | API search 完整返回列表后才检查预算；API page cap 未被接入；缓存仅存于单次运行 | 增加兼容现有 search 的可选分页/批次接口，请求前检查预算；需要分页的来源持久化 checkpoint |
| P2 | `collect/runner.py:114` | 仅 API 来源的运行也进入 async_playwright 上下文 | 仅存在 browser task 时初始化 Playwright；复用 HTTP client，保持各来源身份与 cookie 隔离 |
| P2 | `core/db/store.py:435`、`select/engine.py:63`、`process/normalize.py:217` | 单岗位 lookup 扫描 jobs；筛选和处理多处全量读取、逐条读取 | 单岗位 SQL 查询、批量投影、DB 端筛选与分页；对候选集合建立适用索引，使用 query count/EXPLAIN 验证 |
| P2 | `process/dedup.py:145` | 不同已知官方职位 ID 没有冲突 veto；同公司/标题和近似模板描述可合并 | 在同一官方 ID 命名空间内增加冲突检查；疑似重复留待审阅，避免跨 ATS 直接比较裸 req_id |
| P2 | `core/db/store.py:557` | source/search 关联仅来自 first_search_run_id，后来搜索再次发现不扩展范围 | 保存紧凑的 posting-search 发现关联，独立于可清理的原始 sightings |
| P2 | `web/src/App.tsx:87`、`:118` | 轮询刷新列表和 meta，选中职位详情仅在 selection 变化时刷新 | 当前详情随版本/轮询更新，同时取消或隔离过时请求 |
| P3 | `cli.py:1`、`:28`、`:1566` | CLI 2069 行且顶层引入浏览器组件；读取申请队列也组装 profile/matcher/appliers | 按命令组渐进拆分，分离纯查询与执行组装，延迟加载浏览器和模型组件 |

现有原始数据保存后再解析、跨组去重检查、job/posting 分离、事务写入、归档保留训练标签及人工决策均值得保留。

## 建议的数据流与模块边界

```text
GitHub 清单 / RSS / ATS API / LinkedIn
       ↓ 原始响应与来源版本
来源适配器 → posting 变更检测 → 解析 / 规范化 / 去重 → jobs
                                                        ↓
公开反馈 provider → evidence / 关联 / 研究缓存 → 筛选输入 → AI 判断
                                                        ↓
                                               人工审核 / 待投递队列
                                                        ↓
                                      Applier 接口（自动执行保持关闭）
```

JobSource 负责职位；EvidenceProvider 负责公开讨论与事实来源；筛选器负责基于已保存的职位和证据判断；Applier 负责申请准备和将来的提交。这些模块通过存储和类型契约连接，新增采集不直接驱动投递。

### 1. GitHub 职位源与增量采集

先实现可配置的 GitHub JSON adapter，示例接入 SimplifyJobs/New-Grad-Positions 和 SimplifyJobs/Summer2027-Internships。同一 adapter 支持多个 owner/repo/ref/path；Markdown 表、CSV、RSS/Atom 按实际来源需求增加。现有 search() 作为兼容路径继续工作。

Simplify 清单有 `.github/scripts/listings.json`，可以读取结构化记录，README 是它的派生展示。参见 [New-Grad 贡献说明](https://github.com/SimplifyJobs/New-Grad-Positions/blob/dev/CONTRIBUTING.md)、[Internships 贡献说明与 schema](https://github.com/SimplifyJobs/Summer2027-Internships/blob/dev/CONTRIBUTING.md)。这些例子用来验证适配器，不代表已经完成真实接入。

每条记录保留 source ID、仓库/ref/path、snapshot revision/commit 或 blob SHA、原始 item ID、发现链接、雇主申请链接、抓取/验证时间和内容 hash。缺少 item ID 时优先使用稳定的官方申请 URL 身份，不能用行号，列表重排不能产生新职位。

来源平台（GitHub）与职位的申请平台（例如 Workday）分别记录。GitHub 清单通常没有完整 JD；用官方 ATS 地址补齐详情，并将清单与官方 posting 关联，失败时保持“描述缺失”状态，避免假装已获取。

HttpContext 增加 status/headers/body 响应 envelope；原 get_json/post_json 作为兼容 wrapper。使用 ETag/Last-Modified、持久化原始快照和逐条 fingerprint；相同快照的 304 验证仍刷新确认存在的条目，以免 TTL 误过期。GitHub 正确授权的 304 请求不计入 primary rate limit；429/限额 403 需按服务器响应等待，其他 403 不能自动当作限额错误。参见 [GitHub REST best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)。

适配器不能假设 contents API 一定返回 base64 JSON。超过 1 MB 的文件在 contents API 需要 raw/object media type，1–100 MB 的内容读取使用 raw；设置可配置响应体大小上限。参见 [GitHub contents API](https://docs.github.com/en/rest/repos/contents)。

同一 provider/凭据的来源共享限额和连接池；公共 client 不能混用个别来源的登录 cookie。只有成功完成完整快照才允许推导“该清单已移除”。部分抓取、429、解析失败、缺少的搜索结果与岗位关闭分开处理；某清单移除岗位也不自动证明雇主关闭岗位。

### 2. 公开证据、关联与研究缓存

新增三类记录，现有 feedback/reviews 保持兼容：

| 记录 | 最小内容 |
| --- | --- |
| public_evidence | 原始 URL、source family/item ID、标题与短摘要/摘录、内容 hash、published_at（可未知）、captured_at、topic、提取版本、状态/错误；同 URL 内容变更形成新版本 |
| evidence_links | evidence ID、company_norm、可选 job/posting ID、国家/地区/岗位适用范围、关联方法、置信度和原因；同名但不同公司不能自动关联 |
| research_cache | company/topic/query 版本作为 key，证据 ID、抓取/过期时间、成功/无结果/失败状态；并发同一 key 共享一个研究任务 |

topics 首批覆盖 interview、sponsorship、recruiting_experience、listing_credibility。优先精确岗位 URL/官方 board 与已验证公司身份关联；公司简称模糊匹配进入待确认。发布时间未知不能填入抓取时间冒充发布时间。

证据区分官方招聘要求、公开数据、社区经历、自动提取推断。保留不同结论及适用时间/地区，不把它们压成无法解释的公司总评分。过去 sponsor 过不等于当前岗位承诺 sponsor；未发现反馈不等于负面反馈。匿名经历默认用于提示或补充解释，不自动把岗位否决。

先做 URL 导入、GitHub 公开讨论和 HN item/comment adapter，再通过 provider 接口接通配置的搜索服务。HN 提供公开 item/comment JSON 接口，可作为第一批可验证 provider，参见 [官方 API](https://github.com/hackernews/api)。其他社区和搜索服务需要各自适配与实际可用性验证，不能声称通用爬虫已覆盖。

同一家公司多个职位复用有效证据；职位匹配本身继续逐岗位判断。缓存按主题设置有效期，外部证据变化进入筛选输入 fingerprint；费用未知时显式显示 unknown。并发筛选使用有限调度和进行中费用预留，max-cost 明确为估算控制，不声称是外部账单的绝对上限。

### 3. 自动投递接口与现有功能保留

保留 Applier.can_apply/prepare、已实现适配器的 submit、Applications 队列/历史和 agent 的 start/fill/submit。收敛 Outcome/ApplicationStatus 类型和 capability 描述，沿用默认 `automated=false`。

增加统一执行策略边界，使将来的 CLI、TUI、Web 或其他调用入口都经同一检查。采集和证据 worker 只写职位与证据，不调用 applier。队列查询与“手工已申请”记录不需要初始化 matcher、读取账号密码或加载自动执行 context。

当前 workers/pace/submit_mode 等配置没有完整执行保障，标为预留能力；本轮不将其包装成已实现的自动投递能力。保留代码和接口，不启动真实申请。

### 4. 查询与界面调优

按需求添加单条 job lookup、分页 list、eligible screening query、批量 posting 投影，避免读取整段 JD 来渲染列表。去重先用强身份候选和 company/title 分块缩小范围，继续进行跨成员冲突检查。是否需要 FTS5 由实际搜索需求和查询基线决定。

Web 首先修复 selection/detail 一致性与过时请求问题，再在职位详情中增加公开证据及出处；继续 localhost 单机访问。来源状态展示包括最后成功时间、是否完整、失败原因、缓存命中、更新/新增数量。TUI 和 CLI 沿用同一数据与行为层。

## 分批范围与验收依据

建议按下列范围分批落地；这是范围排序，详细实施计划将在设计确认后编写。

1. **采集正确性与兼容性**：逐职位 company、HTTP envelope、已知记录变更检测、条件快照、来源故障隔离、仅 API 的 Playwright 延迟初始化、Web 操作目标一致性。
2. **多仓库接入**：同一 adapter 接两个 GitHub JSON 清单；保留完整来源；补官方详情；跨来源去重与清单状态测试。
3. **外部公开反馈**：证据 schema/migration、明确关联、URL/GitHub/HN provider、公司主题缓存、详情展示及输入版本化。
4. **针对数据规模调优**：SQL 点查/分页/批量、候选索引、有限并发与预算；记录基线后比较查询次数、请求次数、缓存命中、筛选费用与时延分布。

关键行为测试：

- 列表重排 ID 不变；同 ID 内容改变更新同一 posting；多来源同一官方岗位合并并保留所有来源。
- 两个不同官方 requisition 但模板相同的岗位保持独立；不同 ID 命名空间不错误冲突。
- 304 正确复用快照并刷新存在时间；完整清单移除、部分抓取、失败和 TTL 过期分别处理。
- API-only 不初始化 Playwright；分页预算在请求前生效；一来源或一行失败不终止其他来源；重启不重复写入。
- 同公司同 topic 并发只研究一次；缓存过期、无结果、失败可区分；冲突反馈、缺少日期和模糊关联可追踪。
- JD/primary/evidence 改变使未决岗位的判断失效；保留历史判断、人工决定与原标签/审计语义。
- 快速选 A→B 并操作时不得写 A；延迟旧响应不得覆盖 B；后台更新使当前详情刷新。
- 默认自动投递关闭，直接业务入口也不执行；手工队列和已申请记录继续工作；既有 CLI/JSON/TUI 功能回归。

现有 tests 中已有 HTTP MockTransport、文件/数据库双存储 fixtures、去重、筛选、标签、TTL、serve 和申请关闭的覆盖。实现阶段应先运行基线，再增加上述有实质行为的回归；普通 pytest 中跳过的 Web 浏览器测试不能当作 UI 已验证。

## 当前交付状态

本次产物只有审查与调优草案，没有业务代码变更、schema migration、依赖安装、测试通过或性能提升的声明。所有新增 provider、表结构与界面均为建议。下一步是用户确认该架构方向，再形成书面设计与实施计划。
