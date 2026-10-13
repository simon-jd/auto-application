# 职位与面经信息源核查 · 2026-10-10

**后续部署更新：** 下文保留研究时的核查快照。随后已接入 SpeedyApply、ApplyGuy、Ricsign 和 Jobright SWE，共新增 8 个 feed，Linux 当前启用 10 个。三轮线上采集新增职位分别为 2,379、0、0；详情见 [部署验证记录](../updates/2026-10-10-priority-repository-feeds.md)。机器清单的 `integration_status` 和 `live_state_observation` 已更新，原始研究证据仍保留。

这份合集用于选择后续接入顺序。本阶段完成来源研究、公开数据抽查及格式兼容性核验；没有启用新来源，也没有购买会员或访问已登录账号。机器可读清单见 [source-catalog.json](source-catalog.json)。它是研究资料，程序目前不会自动加载；实际采集仍由 `config/feeds.yaml` / `config/searches.yaml` 控制，筛选偏好在 `config/preferences.json`。

清单包含 **16 个 GitHub 仓库、22 个网站/ATS/发现入口、14 个面经与反馈平台**，另列 **5 类赞助/可信度证据入口、2 个邮件渠道**，共 59 个用途条目。条目数不是独立数据源数量。

关注美国的实习、应届及初级软件、后端、云基础设施、ML/数据相关岗位。继续执行排除 Canada 的偏好；混合地区仓库仍可作为来源，逐岗位筛选，不能把 California 的 CA 排除。美国是本次研究重点，不代表新增了“只能美国”的程序规则。

核查日期为洛杉矶 2026-10-10 晚间；部分证据时间为 UTC 2026-10-11。下表提交时间统一使用 UTC。仓库提交、平台收录、雇主发布、首次发现是不同时间。

## 1. 当前真正接入了什么

本次通过 SSH 只读检查 `home-ubuntu` 的实际配置和 query context：

- 启用：`SimplifyJobs/New-Grad-Positions`、`SimplifyJobs/Summer2027-Internships` 两个公开 JSON feed。
- Gmail、Outlook 和 saved-mail 配置均未启用；真实邮箱授权仍待完成。邮箱不是独立招聘来源，是 LinkedIn / Jobright / 公司等通知的传递渠道。
- 在 `2026-10-11T04:10:53Z` 查询快照中有 7,840 个 job、7,841 个 posting；Canada 偏好排除 463 个，默认展示 7,377 个。它们仍是待补充/待评估线索，不能称为已核实适合投递的机会。
- 以下新增项目都是研究清单。平台存在、公开页面可读、账号可访问、适配器可解析、后台已启用，是五种不同状态。

## 2. GitHub 职位来源合集

P0：已使用的基础源；P1：下一批优先候选；P2：按需补充；P3：参考或重复聚合。优先级是本项目判断，不是平台质量评级。16 个独立仓库地址不等于 16 套独立职位数据。

| 来源 | 覆盖与读取入口 | 最新数据/表格提交 UTC | 判断 |
|---|---|---|---|
| [Simplify New Grad](https://github.com/SimplifyJobs/New-Grad-Positions) | SWE、AI/ML/Data、硬件等；`dev/.github/scripts/listings.json` | 10-11 03:01 | P0，已接入；全量 JSON 包含历史/关闭记录，README 年份不能替代具体职位周期 |
| [Simplify Summer 2027](https://github.com/SimplifyJobs/Summer2027-Internships) | 实习、co-op、off-season；同格式 JSON | 10-11 03:31 | P0，已接入；仍须核对 term、级别和地区 |
| [vanshb03/New-Grad-2027](https://github.com/vanshb03/New-Grad-2027) | 应届 SWE/tech/PM/quant；`dev/.github/scripts/listings.json` | **08-21 15:30** | P2，历史补充；最新 `date_posted` 为 08-05，不能当作持续更新的主力 |
| [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships) | 多季实习/co-op；同格式 JSON | **08-23 15:43** | P2，历史补充；重新查雇主页面 |
| [SpeedyApply SWE 2027](https://github.com/speedyapply/2027-SWE-College-Jobs) | 美国实习 `README.md`；应届 `NEW_GRAD_USA.md` | 10-10 17:12 | P1，补 SWE/backend/cloud/SRE/platform；需要 Markdown/HTML 表格适配 |
| [SpeedyApply AI 2027](https://github.com/speedyapply/2027-AI-College-Jobs) | AI/ML/DS；同样两张美国表 | 10-10 17:11 | P1，与 SWE 同来源家族；部分硕博及跨地区岗位须单独判断 |
| [Jobright SWE New Grad](https://github.com/jobright-ai/2026-Software-Engineer-New-Grad) | README，最近 7 天滚动窗口 | 10-11 03:41 | P1，仓名 2026 但有 2027 Start；链接先到 Jobright |
| [Jobright SWE Internship](https://github.com/jobright-ai/2026-Software-Engineer-Internship) | README，最近 7 天实习 | 10-11 03:19 | P1，与其网站及邮件去重；窗口移除不表示关闭 |
| [Jobright Data New Grad](https://github.com/jobright-ai/2026-Data-Analysis-New-Grad) | 数据分析/DS/相邻初级职位 | 10-11 03:40 | P2，业务分析噪音及非美国岗位较多 |
| [Jobright Data Internship](https://github.com/jobright-ai/2026-Data-Analysis-Internship) | 数据/分析实习 | 10-11 03:20 | P2，按角色和地区筛选 |
| [ApplyGuy New Grad 2027](https://github.com/ApplyGuy/2027-New-Grad-Jobs) | `main/data/new-grad-jobs.json`，`{updatedAt,jobs}` | 10-11 04:00 | P1，340 行；雇主链接在 `listingUrl`，不是 `url`；需 JSON 映射 |
| [Ricsign New Grads 2027](https://github.com/ricsign/Ricsign-New-Grads-Jobs-2027) | `main/data/v1/jobs.json`；SWE/实习/quant/AI research 分板 | 10-10 21:49 | P1，490 active 原始记录，最新提交说明标注 449 live，须区分口径；需映射并保留 degree/link_status |
| [Tar-ive ML Newgrad 2027](https://github.com/Tar-ive/ML_Newgrad_2027) | `main/data/listings.json`；ML/AI/DS/ML相关SWE | 10-11 02:12 | P2，直采 ATS + 再聚合，保留 `sources[]` 和 verification；3,259 原始行不等于美国可投数 |
| [Zapply New Grad 2027](https://github.com/zapplyjobs/New-Grad-Jobs-2027) | README + 跳转链接；行业范围广 | 10-11 04:05 | P2低优先，存在级别噪音；[LICENSE](https://github.com/zapplyjobs/New-Grad-Jobs-2027/blob/main/LICENSE) 对回链和再分发有明确限制 |
| [DereC4 internships-and-newgrad](https://github.com/DereC4/internships-and-newgrad) | `README.md` / `NEWGRAD.md` | 10-11 03:54 | P3，主要再聚合 Simplify、Vansh、SpeedyApply 等，增量价值待证明 |
| [Surya 2027 internship tracker](https://github.com/SuryaHarikrishnan/2027-internship-tracker) | `master/data/listings.json` | 10-11 00:11 | P3，主要合并 Simplify/Vansh；刷新聚合文件不代表上游职位更新 |

**你给的 Vansh 仓库值得收录，但不是当前最鲜活的源。** 现场读取 1,142 行，以项目现有纯解析器检查得到 1,135 条可解析、7 个无效 HTTP(S) URL，其中可解析且 active 的 624 条仍只是上游标记。没有导入生产库。[数据提交](https://github.com/vanshb03/New-Grad-2027/commit/fac3d90535e7327d78a87628ccce1d81d0dfa487)与[原始 feed](https://raw.githubusercontent.com/vanshb03/New-Grad-2027/dev/.github/scripts/listings.json)可复核。

旧地址 [cvrve/New-Grad](https://github.com/cvrve/New-Grad) 已重定向到 Vansh，应作为别名，不能再算一个独立来源。所有已核查仓库的 `fork=false` 也不代表数据原创。

抽查 Vansh 中的 [CTC Associate Engineer 2027](https://job-boards.greenhouse.io/ctccampusboard/jobs/4709991005) 和 Ricsign 中的 [Figure Winter 2027 Security Intern](https://job-boards.greenhouse.io/figureai/jobs/4719593006)，雇主 ATS 可显示岗位与申请表；另两条 Ashby 链接仅返回 JS 页面，未确认开放状态。这是样本验证，不是全库有效率。

## 3. 网站、公司直采和邮件

完整逐项入口、官方证据链接、访问方式与限制保存在 JSON 的 `sources` 中。建议顺序是：**公司原始页面/ATS → 活跃社区清单 → 平台发现与邮件推荐 → 专项补漏**。

| 来源 | 作用与建议读取方式 | 已核实能力 / 尚未验证部分 |
|---|---|---|
| 公司官网 / 校招页：[Google](https://www.google.com/about/careers/applications/jobs/results)、[Amazon University](https://amazon.jobs/content/en/career-programs/university/technical-engineering) | P0，确认原始 JD、地点、资格和雇主申请链接；为关注公司建立目录 | 公开入口；公司与 ATS 租户的对应关系逐个核实，不能按公司名猜 board ID |
| [Greenhouse](https://docs.greenhouse.io/job-board.html) | P0，公开 Job Board API，按 `board_token` 读取 | GET 无需认证；`updated_at` 是更新，detail 的 `first_published` 才是首次发布时间；本次读到示例 JSON |
| [Lever](https://github.com/lever/postings-api) | P0，公开 Postings API，按公司 site，`skip/limit` 分页 | 官方接口及 demo 可读；所查字段表未列发布时间，用首次发现时间并保留未知 |
| [Ashby](https://developers.ashbyhq.com/docs/public-job-posting-api) | P0，公开 board API，职位详情/薪资/地点 | 已读取官方示例 JSON；`publishedAt` 是最近发布，`isListed=false` 不是公开列表职位 |
| [SmartRecruiters](https://developers.smartrecruiters.com/docs/endpoints) | P0，按公司 Posting API，`offset/limit` | 文档有 `releasedDate` 和地区筛选；无凭据示例返回空数组，真实公司分页还要实测 |
| [Workday](https://doc.workday.com/workday-education/en-us/course-manuals/recruiting-for-administrators/career-sites.html) | P1，公司公开职业页及其启用的 job alerts | 公开企业 API 文档不代表候选人可匿名读取租户数据；现有浏览器适配与新公开 API 方案是不同能力 |
| [iCIMS](https://developer-community.icims.com/applications/applicant-tracking/job-portal) | P1，公司公开招聘门户 | 文档里的 Job Portal API 需要 Basic Authorization；本次未验证匿名候选人 API |
| [LinkedIn Jobs](https://www.linkedin.com/jobs/) | P1，用户指定岗位搜索、授权推荐邮件、具体岗位链接 | [官方 alerts](https://www.linkedin.com/help/linkedin/answer/a511279/job-alerts-on-linkedin?intendedLocale=en&lang=en-us)支持日/周通知；账号未查，无开放读取 API 验证；[官方限制网站抓取/自动化](https://www.linkedin.com/help/linkedin/answer/a1341387/prohibition-of-scraping-software?intendedLocale=en&lang=en-us)，优先邮件与用户提供链接 |
| [Jobright](https://jobright.ai/) | P1，个性化推荐、公开职位、收到的邮件 | 公共网站已查；账号、推荐邮件频率未验证。与 GitHub 子集共享来源身份 |
| [Simplify](https://simplify.jobs/) | P1，实习/应届合集、推荐邮件 | [官方邮件设置](https://help.simplify.jobs/en/articles/6390631-navigating-your-dashboard)有 Daily/Weekly/Never；网站个性化账号未验证，与两个 GitHub feed 去重 |
| [Handshake](https://joinhandshake.com/) | P1，校园实习与初级岗位、保存搜索提醒 | [官方文档](https://support.joinhandshake.com/hc/en-us/articles/218693388-Saving-Job-Searches-and-Receiving-Job-Alerts)说明邮件最多 25 个岗位；完整结果需登录，不能把邮件当全量 |
| [Northeastern NUworks](https://northeastern-csm.symplicity.com/students/?signin_tab=0) | P1，学校 co-op / 校园渠道 | 已核实学校和登录入口；[学校资格/审批说明](https://careers.northeastern.edu/co-op-information/)适用，未验证你的账号权限或当前可见职位 |
| [Wellfound](https://wellfound.com/candidates/overview) | P1，初创公司岗位，公开页/保存搜索邮件 | 官方有求职者免费入口及日/周提醒；级别混合，用户账号未验证 |
| [YC Work at a Startup](https://www.ycombinator.com/jobs/) | P1，YC 初创公司 backend/infra 等 | 公开清单可读，级别/地区混合；YC jobs 与 workatastartup 是同一家入口 |
| [HiringCafe](https://hiringcafe.com/) | P1，跨公司职业页发现；时间、经验、学历等筛选 | 公开列表与控件可见；`hiring.cafe` 重定向至此，无公开 API/账号能力验证 |
| [Built In](https://builtin.com/jobs) | P2，美国技术公司、城市、远程岗位 | [官方覆盖说明](https://builtin.com/articles/our-sites)也含国际岗位；提醒频率、完整读取未验证 |
| [Google 职位搜索](https://support.google.com/websearch/answer/7498276?hl=en) | P2，发现遗漏公司和 ATS 页面，定向 `site:` 搜索 | 索引并非全量且可能滞后；JobPosting / indexing 文档不是候选人职位 feed API |
| [Indeed](https://www.indeed.com/) | P2，科技公司之外的职位与邮件提醒 | [官方提醒说明](https://support.indeed.com/hc/en-us/articles/204488890-Starting-Stopping-and-Managing-Job-Alerts)要求账号，邮件可能放宽地点；需重查实际地点 |
| [Dice](https://www.dice.com/) | P2，cloud/infra/data 技术岗位 | [日/周 alerts](https://www.dice.com/support/candidate-help/finding-a-job/job-alerts-and-setting-them-up.html)需登录；区分招聘代理与真正雇主 |
| [Levels.fyi](https://www.levels.fyi/jobs/title/software-engineer/level/internship) / [Internships](https://www.levels.fyi/internships/) | P2，实习、初级职位发现及薪资背景 | 两类公共页面可读，地区全球；不能用薪资样本证明职位仍开放 |
| [HN Who is hiring · 2026-10](https://news.ycombinator.com/item?id=49922569) | P2，初创公司/infra 补漏，可用[官方 HN API](https://github.com/HackerNews/API) | 发帖规则要求雇主身份，但身份/岗位仍需核实；按单条招聘评论计时，不能与求职者自荐帖混淆 |
| [USAJOBS](https://www.usajobs.gov/) | P2按资格选用，联邦实习/毕业生路径 | [Search API](https://developer.usajobs.gov/api-reference/job-apis)需 key；多数职位有公民身份等限制，逐公告判断，不默认推荐 |

P0 的 ATS API 指“值得优先建立公司直采能力”，不表示这些公开 API 已接入 Linux worker。现有公司浏览器插件与这次 API 核查分别记录。

Gmail / Outlook 以后读取限定文件夹、发件人或查询范围的推荐邮件，保留 `message_id`、发送时间、推荐平台、原始链接及最终雇主链接。发送时间是邮件时间，不能填成职位发布日期；同一职位在 LinkedIn 邮件、GitHub 和官网出现，合并职位身份，保留三次发现记录。

对职位的基本核实应包括：公司域名或 ATS 身份、requisition ID、完整 JD、地点、学历/毕业时间、明确的年限要求、当前申请入口及工作授权说明。仓库作者的 sponsorship/eligibility/verified 字段作为线索保存，不能直接改写成雇主结论。

## 4. 面经与招聘反馈在哪里查

下表是实际查过入口或样例的 14 个平台。筛选能力主要依据公开控件或官方说明，没有逐项交互测试。公开预览、会员正文和匿名自述的真实性分别判断；本次没有验证任何付费账号权限或价格。

| 平台 | 用途和可用筛选 | 时效与访问边界 |
|---|---|---|
| [一亩三分地](https://www.1point3acres.com/bbs/forum.php?mod=forumdisplay&fid=145) | 中文原始面经优先；公司、岗位、NG/跳槽、YOE、年度/季度、实习/全职、轮次等 | 可按发帖及一个月/三个月窗口找；部分正文需要登录/积分/阅读权限。最后回复不是面试日期 |
| [LeetCode Discuss](https://leetcode.com/discuss/) | OA、coding、面试流程原帖，搜索公司和岗位 | 不少报告来自印度，需正文确认地区；公司题频 Premium 与论坛面经是不同产品 |
| [Reddit](https://www.reddit.com/r/csMajors/) | csMajors 看 NG/intern，leetcode 看面试轮次，cscareerquestions 看招聘体验 | New + 日/周/月/年窗口；排除求面经、广告和传言；评论单独计时 |
| [Glassdoor Interviews](https://www.glassdoor.com/Interview/junior-software-engineer-interview-questions-SRCH_KO0%2C24_SDRD.htm) | 轮次、耗时、体验、结果；岗位/地点、Most recent | 有带日期的公开样例，但完整访问依会话；本次未验证最新美国目标岗位全文 |
| [Blind](https://www.teamblind.com/channels/interview-experiences) | recruiter/team/headcount、面试及招聘体验补充 | senior/staff 较多；完整单帖、日期另查；员工徽章不验证这次面试 |
| [Indeed](https://www.indeed.com/cmp/Microsoft) | 普通公司的招聘体验交叉核对 | 当前美国 interview URL 跳回公司页；未证实近期美国 SWE 面经，低优先 |
| [PracHub](https://prachub.com/interview-experiences) | 公司/岗位/轮次/资历，Newest；题库与面经入口 | 部分面经正文公开；题库有 XP/Premium，面经权限逐条确认；含翻译、编辑和汇总，须追溯来源及事件时间 |
| [InterviewDB](https://www.interviewdb.io/) | 公司/岗位/轮次，Last Reported / Frequency 题库线索 | 正确域名为 `.io`；既有投稿也采自一亩三分地等，详情及会员权限未验证 |
| [Interview Query](https://www.interviewquery.com/guides) | 公司、岗位、Last 90 days；DS/DE/ML及部分SWE/infra | 有候选人面经；部分 AI 匿名化，投稿或订阅解锁；季度日期不是精确发生日 |
| [Hello Interview](https://www.hellointerview.com/community/questions) | 用户报告题目；公司、级别、地区、题型、Recent | 已见筛选控件，本次未获取带日期题卡；教程与真实报告要分开 |
| [Aced（原 Exponent）](https://www.aced.io/experiences?src=nav) | 公司/岗位、候选人及面试官经历、题库 | 区分发布时长与面试月份；预览可读，完整报告通常需真实投稿或会员 |
| [interviewing.io](https://interviewing.io/learn) | 公司流程说明、模拟面试、系统设计练习 | 准备材料；模拟面试日期不能证明公司真实招聘流程的时效 |
| [IGotAnOffer](https://igotanoffer.com/en/advice/google-interview-experience) | 编辑综述及原帖线索 | 2026 更新的页面仍混有多年旧材料；逐原帖判断时间 |
| [牛客](https://www.nowcoder.com/home/) | 中文同公司岗位补充 | 本次未核实近期美国目标面经；中国地区内容较多，不列默认主力 |

另将 FastPrep 留在“待核实发现”中：搜索可见相关预览，但全文只返回 Loading，尚不能确认来源和事件日期，不纳入已核实主力平台。

**PracHub 是有用的发现和准备入口，但不能把所有条目视为一手面经。** [这个 Anthropic 样例](https://prachub.com/interview-experiences/anthropic-software-engineer-interview-experience-a-compilation-on-culture-practical-coding-and-timelines)在 2026-10-06 发布，正文说明是汇总、不是作者亲历；侧栏却写 Interview date Oct 2026。应保存为二手汇总，逐原始材料追溯。同一段经历被 PracHub、InterviewDB 和原论坛同时收录，只算一个证据来源链。

## 5. 面经时效及后续研究的返回标准

1. 先确定公司、地区/办公室、岗位族、级别、招聘周期；“同公司 Staff 印度面经”不能代替“美国 NG SWE”。排除加拿大目标匹配；地点不明保持未知。
2. 默认找**实际面试发生在最近 90 天**的匹配材料，最近 30 天优先。不足时扩至 180 天，再至 365 天，并标明扩窗。超过一年通常只作题型和历史背景。
3. 分别保存 `interview/event date`、`published_at`、`updated_at/curated_at`、`retrieved_at` 和 `cohort_year`。月份/季度保留区间，跨过时效截止线则标“边界不确定”；只有发布日期的材料标“事件日期未知”。缓存页的 yesterday/2h ago 不能直接按本次抓取时间倒推。
4. 原始自述优先，其次经编辑/匿名化的经历与可溯源转载，再是多帖汇总和课程说明。面经真实性仍是候选人自述，不以网站的 Verified 标签替代独立验证。
5. 返回**具体事实摘要 + 原文链接 + 事件日期/精度 + 发布日期 + 地区/岗位/级别 + 可见范围 + 原创/转载分类 + 可信度依据**。区分确认页面如此记载与确认经历真实发生；保留矛盾和缺口，不凭一条经历概括全公司。

例如 [Amazon SDE1 USA / Seattle / New Grad 原帖](https://www.reddit.com/r/leetcode/comments/1x1fmxc/amazon_sde_1_usa_interview_experience_4_rounds/)搜索呈现 2026-10-09 发布，正文却只说面试在 8 月 17 日、未写年份。可以说“近期发布的美国 NG 报告”，不能说“最近 30 天发生的面试”。

实际检索组合会包括：`公司 + Software Engineer/New Grad/Intern + US/具体城市 + interview/OA/onsite`，中文加“面经/校招/实习/时间线”；平台日期筛选和搜索引擎 `site:` 仅用于找材料，最后仍打开正文核对。找不到近 90 天同地区同级别资料时，直接报告缺口。

## 6. 赞助、可信度与来源关系

赞助研究优先看**当前职位 JD/公司招聘说明**，其次查 [USCIS Employer Data Hub](https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub) 与 [DOL Disclosure Data](https://www.dol.gov/agencies/eta/foreign-labor/performance) 的历史记录，[MyVisaJobs](https://www.myvisajobs.com/)作辅助检索，再用社区近期同岗位反馈补充。历史申请/批准记录不能证明这个岗位当前愿意 sponsor；没有搜到也不证明从未赞助。

[E-Verify Employer Search](https://www.e-verify.gov/e-verify-employer-search)另用于雇主参与情况，不能替代 sponsorship 结论。本次 USCIS/E-Verify 官方资料可定位，但实时查询/最新下载未核实；DOL 已核实公开下载页，未下载分析整份数据。

DOL 的 LCA 认证与 USCIS 的申请决定是不同阶段，LCA 认证不等于 H-1B 申请获批；匹配雇主法定实体和财年后再解释记录。见 [DOL LCA 流程说明](https://flag.dol.gov/programs/lca)。

职位去重以雇主职位 ID / 规范链接为主，来源记录继续独立；面经去重以原帖、事件和转载链为主。Jobright 网站/邮件/GitHub，Simplify 网站/邮件/GitHub，以及 PracHub/InterviewDB 对论坛内容的整理，都不应按域名数计算独立证据。仓库窗口过期或来源消失也不等于雇主关闭岗位。

## 7. 本次建议的后续优先级

- **职位覆盖**：保留两个 Simplify feed；下一批考虑 SpeedyApply SWE/AI、ApplyGuy、Ricsign、Jobright SWE 两仓；并为关注公司建立 ATS/官网来源。Vansh 留作补漏并核对雇主状态。
- **平台补漏**：LinkedIn、Jobright、Simplify 推荐邮件及学校 NUworks/Handshake，再加 HiringCafe、YC/Wellfound、Levels.fyi、HN 等专项发现入口。邮箱授权和账号访问要单独实测。
- **公司研究**：一亩三分地/LeetCode/Reddit/Glassdoor 查原始经历；PracHub/InterviewDB 补近期题目；Interview Query 补 ML/数据；Blind 补招聘信号。每家公司按具体证据缺口扩展。

以上是接入和检索建议，未在本阶段实现新适配器或研究调度。清单覆盖所需主要来源类别，不声称穷尽互联网；未登录或未取到全文的项已注明。后续是否增加来源，应看它能带来多少去重后的、仍开放且条件相关的新职位或独立面经。
