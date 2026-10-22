# 项目审阅与 Dot 非破坏性兼容（2026-10-21，Pacific）

## 范围与结论

基线提交：`e6e3f8a`。审阅覆盖采集/去重、确定性筛选、JD 补全、worker/摘要、
手动跟踪、handoff/jobq、MCP、插件、部署说明及 Mac apply-kit 接口；这不是逐行安全审计。
本轮不改变生产数据、筛选规则、Grok wrapper、认领协议、服务配置或认证要求。

现有结构适合继续保留：服务器保存统一岗位库；Grok 的固定 `jobq` 和 Dot 的 MCP
复用服务层。认领/结果写入使用事务，配额考虑有效认领，读取 packet 再检查资格。
无需为 Dot 另建数据库或重写 Grok 流程。

## 已验证的线上状态

通过现有 ChatGPT 连接成功调用 `get_job_context`、`get_pipeline_status`、
`search_jobs(limit=1)`、`get_job(15416)`、`get_digest(limit=1)`；均为读取。
没有调用标记、暂停、规则修改、准备表单、认领或提交工具。

首次快照为 2026-10-21 19:52 PDT：15,416 个岗位，筛选保留 5,263 个；
sponsorship 为 offers 7、no 179、unknown 5,077。unknown 不是不提供担保，
也不代表适合候选人。采集与 worker 状态均 ok，连续失败为 0。
18 个工具在当前会话可发现；仅上述 5 个实际调用得到验证。
线上 `applied=0` 与既有实跑记录“已到 Submit、尚未真正提交”一致。

## 审阅发现与改进优先级

| 优先级 | 证据/缺口 | 建议 |
|---|---|---|
| P1 | `handoff/service.py` 的 claim 替换过期 token；旧代理在重认领后无法记录真实迟到提交。现有测试明确接受这一限制 | 开放多代理同时投递前，增加续租、持久认领历史、提交结果不确定状态和对账；不能只延长租期 |
| P1 | `enrich/service.py` 对 ok/gone/unsupported 没有 TTL；完成补全的岗位退出待补全集合 | 添加分状态 TTL、主动刷新、投递前存活检查，保留原证据版本；避免把源列表消失当作已关闭 |
| P2 | 5,077/5,263 个保留岗位的担保信息未知；13,359 个岗位 pending enrichment | 优先补全用户想投的岗位，并分开统计“暂未尝试、无法解析、平台不支持、重试中”；不要通过默认排除 unknown 来提高表面准确率 |
| P2 | handoff 的 failed 立即重新进入可认领集合，没有执行失败次数上限 | 区分安全重试与提交结果不确定，增加退避/次数上限；后者先对账 |
| P2 | claim 保存 agent，但 outcome 删除 claim，attempts.applier 只保存 external-agent | 将 agent/run/claim 标识持久保存在结果历史，便于区分 Dot 与 Grok |
| P2 | `track/events.py` 手动同类面试/测评事件在 24 小时内合并 | 使用请求幂等键去重，允许同日多轮面试；这是产品能力改进，不能简单删除去重 |
| P2 | worker health 步骤主要检查磁盘；缺少业务积压指标 | 加入源新鲜度、可补全积压、过期认领、反复失败、通知积压；区分进程存活与业务正常 |
| P2 | Dot 没有当前 Grok 认领查看能力；want_to_apply 只是意向 | 后续增加不含 token/材料的只读执行状态接口；需要重新发现云端 schema |
| P3 | 日常详情 MCP 返回完整 ingestion；jobq show 已去掉原始导入记录 | 可增加 opt-in 的精简详情接口，保留旧 get_job 契约；尤其避免邮件导入跟踪链接传播 |

这些是已有设计限制及扩展建议，不是声称本轮新引入的回归。先做多代理对账与证据
刷新，再扩展 Dot 真正执行投递，比增加一套浏览器自动化更重要。

## 本轮兼容修正

1. `scripts/check_agent_mcp.py`：原先要求恰好 4 个工具，现改为要求基础工具存在、
   允许额外工具。仍只调用 context/search/detail，不自动调用发现的写工具。
2. `get_job_context.capabilities.agent_interface`：增量提供接口版本及 control、
   handoff、preparation 工具组配置；不删除旧字段、不新增写工具、不改变认证。
   配置不等于当前聊天的可调用工具，客户端必须继续发现工具。
3. Job Search 插件源版本 0.3.1：与后端一致保留具有美国选项的美加混合岗位；
   明确区分想投、被认领、已提交；说明 Dot 和 Grok 共用记录但不共享本地文件路径。
4. `tests/track/test_updates.py`：将 applied 测试事件固定在邮件事件之前，消除随着
   当天日期变化而出现的 15 个假失败；没有更改生产事件排序。

## 验证与部署边界

- 新增真实内存 MCP 测试：工具组配置、附加工具兼容、缺失基础工具拒绝、探测前后
  数据库字节保持不变。新测试先失败、实现后通过。
- 兼容/插件打包/邮件跟踪针对性验证：86 passed。
- 修改前核心链路回归：784 passed、16 failed；其中 15 个为上述测试日期问题，
  1 个为本环境禁止绑定 localhost 端口（不是服务逻辑失败）。
- 独立代码复核未发现阻断问题；旧版技能会错误排除美加混合岗位，修改后场景验证
  保留该岗位，并正确拒绝将 want_to_apply 当作 Grok 正在执行。
- 修改后核心回归：**805 passed、1 deselected、1 warning**（76.75 秒）；仅排除
  已确认无法绑定本机端口的 streamable-http 用例。覆盖 agent/handoff/jobq/query/
  screen/worker/track/plugins/ops。warning 为既有 discord audioop 弃用提示。
- 全套回归结果见下方补充；不将受环境限制的用例当作通过。
- SSH 到 home-ubuntu 被执行环境拒绝：`Operation not permitted`。因此本轮本地
  代码/插件源修正不能宣称已部署，现有云端读取成功也不证明新代码已上线。

## 后续部署与回滚

部署本轮时只更新已审阅的 MCP 文件、检查脚本与插件说明；保持现有认证参数、
数据库路径、Grok wrapper、ingestion/worker 配置和 handoff 开关。无需数据库迁移。
重启现有 MCP 后运行只读检查脚本；在 ChatGPT 刷新连接并确认能力字段，再更新
插件到 0.3.1。当前 MCP 工具名和输入参数不变，旧客户端仍能工作。

回滚只恢复本轮 MCP 文件和插件说明后重启 MCP；数据库没有本轮 schema 或状态变更。
需要完整 Dot 执行能力时另行设计认证和跨代理结果对账，不能删除现有认证门槛来启用。

## 全套回归的环境限制

完整 pytest 已尝试，因持续出现本机端口绑定和 Chromium MachPort 权限拒绝而中断，
结果为 **70 failed、169 passed、1 warning**（130 秒），不是全绿。部分浏览器工作流
表现为后续 handoff/status 断言失败；未在可启动浏览器的环境重跑，因此不能证明
这些端到端路径通过。以下保留所有已报告失败名称，避免将未验证项遗漏：

```text
FAILED tests/agent/test_mcp.py::test_real_client_initializes_reads_records_and_reports_errors[streamable-http]
FAILED tests/apply/test_agent.py::test_snapshot_sees_labels_groups_frames_and_hides_secrets
FAILED tests/apply/test_agent.py::test_tools_fill_the_form_and_record_what_they_put_there
FAILED tests/apply/test_agent.py::test_guards_hold_whatever_is_asked - playwr...
FAILED tests/apply/test_agent.py::test_a_description_page_leads_to_the_form
FAILED tests/apply/test_agent.py::test_time_limit - playwright._impl._errors....
FAILED tests/apply/test_agent.py::test_start_mode_end_to_end - AssertionError...
FAILED tests/apply/test_agent.py::test_the_claude_driver_exposes_only_the_sessions_tools
FAILED tests/apply/test_agent.py::test_sign_in_with_your_email_and_password_unseen
FAILED tests/apply/test_agent.py::test_creating_an_account_and_the_terms_box
FAILED tests/apply/test_agent.py::test_no_password_set_means_action_needed - ...
FAILED tests/apply/test_agent.py::test_clicks_go_through_an_overlay_and_progress_is_reported
FAILED tests/apply/test_agent.py::test_refs_from_an_old_snapshot_dont_linger
FAILED tests/apply/test_agent.py::test_trial_run_then_submit - AssertionError...
FAILED tests/apply/test_agent.py::test_fill_only_leaves_the_last_step_to_you
FAILED tests/apply/test_agent.py::test_submit_without_a_confirmation_never_goes_to_another_applier
FAILED tests/apply/test_agent.py::test_a_new_question_on_submit_comes_back_to_you
FAILED tests/apply/test_agent.py::test_account_info_reads_the_site_and_your_records
FAILED tests/apply/test_agent.py::test_the_password_can_live_in_profile_yaml_and_never_reaches_claude
FAILED tests/apply/test_agent.py::test_a_failed_sign_in_isnt_an_account - pla...
FAILED tests/apply/test_dry_run.py::test_scan_and_reconcile - assert Outcome(...
FAILED tests/apply/test_dry_run.py::test_simplify_auto_start_needs_input_then_submit
FAILED tests/apply/test_dry_run.py::test_simplify_auto_falls_back_when_its_panel_is_missing
FAILED tests/apply/test_dry_run.py::test_labels_without_label_elements - play...
FAILED tests/apply/test_dry_run.py::test_simplify_auto_goes_through_a_multi_step_form
FAILED tests/apply/test_dry_run.py::test_a_new_question_on_a_later_step_comes_back_to_you
FAILED tests/apply/test_fill.py::test_fills_and_submits - AssertionError: ass...
FAILED tests/apply/test_fill.py::test_a_wrong_answer_is_not_a_confirmation - ...
FAILED tests/apply/test_fill.py::test_a_missing_field_hands_it_to_the_next_applier
FAILED tests/apply/test_fill.py::test_a_captcha_headless_needs_you - Assertio...
FAILED tests/collect/test_runner_fakesite.py::test_first_run_saves_raw_postings[files]
FAILED tests/collect/test_runner_fakesite.py::test_first_run_saves_raw_postings[db]
FAILED tests/collect/test_runner_fakesite.py::test_second_run_skips_known_and_stops_early[files]
FAILED tests/collect/test_runner_fakesite.py::test_second_run_skips_known_and_stops_early[db]
FAILED tests/collect/test_runner_fakesite.py::test_one_source_error_preserves_other_sources_and_finishes_batch[files-False]
FAILED tests/collect/test_runner_fakesite.py::test_one_source_error_preserves_other_sources_and_finishes_batch[files-True]
FAILED tests/collect/test_runner_fakesite.py::test_one_source_error_preserves_other_sources_and_finishes_batch[db-False]
FAILED tests/collect/test_runner_fakesite.py::test_one_source_error_preserves_other_sources_and_finishes_batch[db-True]
FAILED tests/collect/test_runner_fakesite.py::test_session_transient_failures_retry_without_cooldown[files-2-ok]
FAILED tests/collect/test_runner_fakesite.py::test_session_transient_failures_retry_without_cooldown[files-3-error]
FAILED tests/collect/test_runner_fakesite.py::test_session_transient_failures_retry_without_cooldown[db-2-ok]
FAILED tests/collect/test_runner_fakesite.py::test_session_transient_failures_retry_without_cooldown[db-3-error]
FAILED tests/collect/test_runner_fakesite.py::test_three_gone_details_do_not_block_source[files]
FAILED tests/collect/test_runner_fakesite.py::test_three_gone_details_do_not_block_source[db]
FAILED tests/collect/test_runner_fakesite.py::test_retry_run_is_finished_when_fetch_stops[files-block-blocked]
FAILED tests/collect/test_runner_fakesite.py::test_retry_run_is_finished_when_fetch_stops[files-error-error]
FAILED tests/collect/test_runner_fakesite.py::test_retry_run_is_finished_when_fetch_stops[files-cancel-interrupted]
FAILED tests/collect/test_runner_fakesite.py::test_retry_run_is_finished_when_fetch_stops[db-block-blocked]
FAILED tests/collect/test_runner_fakesite.py::test_retry_run_is_finished_when_fetch_stops[db-error-error]
FAILED tests/collect/test_runner_fakesite.py::test_retry_run_is_finished_when_fetch_stops[db-cancel-interrupted]
FAILED tests/collect/test_runner_fakesite.py::test_retry_pass_refetches_transient_failed_cards[files]
FAILED tests/collect/test_runner_fakesite.py::test_retry_pass_refetches_transient_failed_cards[db]
FAILED tests/collect/test_runner_fakesite.py::test_daily_ceiling_skips_are_recorded[files]
FAILED tests/collect/test_runner_fakesite.py::test_daily_ceiling_skips_are_recorded[db]
FAILED tests/collect/test_runner_fakesite.py::test_cards_only_fetches_nothing[files]
FAILED tests/collect/test_runner_fakesite.py::test_cards_only_fetches_nothing[db]
FAILED tests/collect/test_runner_fakesite.py::test_captcha_stops_source_and_sets_cooldown[files]
FAILED tests/collect/test_runner_fakesite.py::test_captcha_stops_source_and_sets_cooldown[db]
FAILED tests/collect/test_runner_fakesite.py::test_security_challenge_interstitial_is_a_block[files]
FAILED tests/collect/test_runner_fakesite.py::test_security_challenge_interstitial_is_a_block[db]
FAILED tests/collect/test_runner_fakesite.py::test_manual_check_waits_for_user_then_continues[files]
FAILED tests/collect/test_runner_fakesite.py::test_manual_check_waits_for_user_then_continues[db]
FAILED tests/collect/test_runner_fakesite.py::test_manual_check_not_completed_times_out_and_blocks[files]
FAILED tests/collect/test_runner_fakesite.py::test_manual_check_not_completed_times_out_and_blocks[db]
FAILED tests/collect/test_runner_fakesite.py::test_logged_out_is_needs_login[files]
FAILED tests/collect/test_runner_fakesite.py::test_logged_out_is_needs_login[db]
FAILED tests/collect/test_runner_fakesite.py::test_hourly_limit_pauses_instead_of_skipping[files]
FAILED tests/collect/test_runner_fakesite.py::test_hourly_limit_pauses_instead_of_skipping[db]
FAILED tests/collect/test_runner_fakesite.py::test_retry_pass_uses_at_most_half_the_hourly_limit[files]
FAILED tests/collect/test_runner_fakesite.py::test_retry_pass_uses_at_most_half_the_hourly_limit[db]
```
