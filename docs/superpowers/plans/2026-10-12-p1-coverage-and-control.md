# P1：采集覆盖 + 聊天管理 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先修复 P0 审查发现的问题。然后：用公司招聘板的公开 API 和 IMAP 邮件提醒扩大采集覆盖；给 MCP 加一个带 token 认证的公网入口（给 Grok 和用户自己的 agent 用）；增加暂停/恢复、改规则、筛选反馈、摘要等控制工具；把 Discord webhook 升级为带按钮和斜杠命令的 bot。全程不调用任何模型。

**Architecture:**
- 公司招聘板复用现有的 repository feed 机制：在 `feeds.yaml` 里新增三种 format（`greenhouse-board` / `lever-board` / `ashby-board`），由 ingest worker 每 30 分钟拉取。招聘板 API 直接返回 JD，不需要再补全。
- IMAP 是 mailbox 的第三个 provider，复用现有的分页窗口、收据表和重试队列。
- 认证 MCP 是 `auto-app-agent serve` 的第二个实例：仍只监听 loopback，由 Tailscale Funnel 或 Cloudflare named tunnel 暴露，用 SDK 自带的 `token_verifier` 做 Bearer 校验。
- 控制工具通过一个独立的控制文件（`<db>.worker-control.json`）和 worker 通信，不与 worker 的状态文件抢写。
- Discord bot 是独立进程：负责推送（带按钮）和斜杠命令；worker 在 bot 模式下只做 screen / enrich 和告警。

**Tech Stack:** Python 3.12、uv、SQLAlchemy Core、httpx、imaplib（标准库）、mcp 2.3（`MCPServer`、`TokenVerifier`、`AuthSettings`、`TransportSecuritySettings`）、discord.py 2.x（新依赖）、Typer、pytest。

**Spec:** [2026-10-12-roadmap-p0-p3.md](2026-10-12-roadmap-p0-p3.md) §5；P0 的实际实现（未提交的工作区，见 `docs/updates/2026-10-12-linux-worker.md`）。

## Global Constraints

- 系统内**不调用任何模型或 AI agent**：不用 Claude CLI、Agent SDK，不写 agent 提示词。所有 agent 都由用户自己安排，只通过 CLI / MCP 接口读写。
- 不提交申请；`CLAUDE.md` 的 "Never submit a job application" 保持不变。
- 密钥（Discord bot token、webhook URL、MCP token、IMAP 应用密码）只放在 `$AUTO_APP_HOME/credentials/`，文件权限必须是 600。代码读到权限过宽的密钥文件时拒绝启动。密钥不打印、不写日志、不出现在报错里。
- 新的网络服务一律只监听 loopback，对公网暴露只能通过隧道。
- `src/auto_app/query/` 保持只用标准库、只读。
- 数据库结构只通过 Alembic 迁移修改。P1 不需要新表。
- 新依赖只有 `discord.py`（`>=2.4,<3`），用 `uv add` 写入 `pyproject.toml` 和 `uv.lock`。
- 文档规则（CLAUDE.md）：每个功能都要更新 `docs/updates/` 条目和索引、对应的 step 文档、`README.md`。
- 提交规则（CLAUDE.md "Commit or push only when the user asks."）：每个任务末尾的 commit 步骤**只有用户批准时才执行**。提交信息结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 测试命令：`uv run pytest -q`；单个文件：`uv run pytest tests/<path>.py -q`。

## Review Focus

1. **公司招聘板和仓库 feed 里的同一个岗位要合并成一个 job**（同一个 employer key），不能变成两个推送。→ Task 2 `test_board_job_merges_with_the_repository_copy`。
2. **IMAP 只读**：不能把邮件标为已读，也不能移动邮件。期望：只用 EXAMINE 和 `BODY.PEEK[]`。→ Task 3 `test_session_is_read_only_and_never_marks_seen`。
3. **公网 MCP 不带 token 或 token 错误时一律 401**；Host 头不在白名单时拒绝。→ Task 4 `test_requests_without_the_token_are_rejected`。
4. **控制工具和 worker 的状态文件写冲突**：暂停期间 worker 不能运行新的一轮，恢复后按原节奏继续；控制工具写的是独立文件，不会被 worker 覆盖。→ Task 5 `test_a_paused_worker_waits_and_resumes`。
5. **Discord 按钮被别人点击**（例如频道里的其他成员）：期望拒绝，不写入任何决定。→ Task 7 `test_only_the_owner_can_record_decisions`。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/auto_app/enrich/fetch.py`、`src/auto_app/worker/round.py`、`src/auto_app/query/service.py`、`src/auto_app/screen/rules.py`、`src/auto_app/worker/cli.py` | Task 1：P0 修复 |
| `src/auto_app/ingest/repository_boards.py` | Task 2：三种招聘板 JSON → `IngestRecord` |
| `src/auto_app/ingest/repository.py`、`src/auto_app/ingest/runner.py` | Task 2：注册新 format，传入可选的 `company` |
| `src/auto_app/worker/boards.py` | Task 2：从库里统计值得加的招聘板，输出 feeds.yaml 片段 |
| `src/auto_app/ingest/imap.py` | Task 3：只读 IMAP 会话 |
| `src/auto_app/ingest/mailbox.py` | Task 3：`provider: imap` 分支 |
| `src/auto_app/agent/auth.py`、`src/auto_app/agent/cli.py`、`src/auto_app/agent/mcp_server.py` | Task 4：静态 Bearer token、公网 Host 白名单 |
| `src/auto_app/worker/control.py`、`src/auto_app/worker/loop.py` | Task 5：暂停/恢复控制文件 |
| `src/auto_app/agent/control.py`、`src/auto_app/screen/rules.py` | Task 6：控制服务（状态、暂停、改规则、反馈、摘要） |
| `src/auto_app/worker/digest.py` | Task 7：拆出 `batches` / `mark_notified`，供 bot 复用 |
| `src/auto_app/bot/` | Task 7：Discord bot |
| `deploy/auto-app-agent-public.service`、`deploy/auto-app-bot.service` | Task 4、7：systemd 模板 |
| `docs/remote-mcp.md`、`docs/discord-bot.md`、`docs/linux-worker.md`、`docs/mailbox-linux-setup.md`、`docs/updates/…`、`README.md` | Task 8：文档 |

---

### Task 1: P0 审查问题修复

P0 代码审查发现 8 个问题，这里修 7 个。第 7 个（digest 每轮全表读取）推迟：一万多条数据下只多耗约 1–2 秒，改起来还要动国家排除的逻辑。

**Files:**
- Modify: `src/auto_app/enrich/fetch.py`（`_validate_record`、`_ashby_record`）
- Modify: `src/auto_app/worker/round.py`（enrich 失败判定、rescreen 条件）
- Modify: `src/auto_app/query/service.py`（`SCREENED_OUT_SQL`）
- Modify: `src/auto_app/screen/rules.py`（`NON_CANDIDATE_EXPERIENCE`、`RULES_VERSION`）
- Modify: `src/auto_app/worker/cli.py`（状态文件后缀改用 `loop.STATE`）
- Test: `tests/enrich/test_fetch.py`、`tests/worker/test_round.py`、`tests/query/test_screening.py`、`tests/screen/test_rules.py`

**Interfaces:**
- Produces: `fetch._same_requisition(expected: str, found: str) -> bool`；其余函数签名不变。

- [ ] **Step 1: 写失败测试**

追加到 `tests/enrich/test_fetch.py`：

```python
def test_workday_copy_suffixes_and_formats_match_the_requisition():
    from auto_app.enrich.fetch import _same_requisition

    assert _same_requisition("R123456-1", "R123456")
    assert _same_requisition("JR2026343", "JR-2026343")
    assert _same_requisition("2026343", "JR2026343")
    assert _same_requisition("REQ_0000082877-1", "REQ-0000082877")
    assert not _same_requisition("R123456", "R654321")


def test_a_workday_record_with_a_suffixed_url_id_is_accepted():
    url = ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-TX-Austin/"
           "Software-Engineer_JR2026343-1")
    f = fetcher(lambda request: httpx.Response(200, json=WORKDAY))  # jobReqId JR2026343
    result = f.fetch(target_for(url))
    assert result.status == "ok" and "Write C++." in result.posting.description_text


def test_one_malformed_ashby_job_does_not_fail_the_board_and_unlisted_jobs_are_readable():
    board = {"jobs": [{"id": UUID, "title": "Backend Engineer", "location": "New York", "jobUrl": "https://x",
                       "descriptionPlain": "Own services.", "isListed": False},
                      {"id": OTHER, "title": "", "jobUrl": "https://y"}]}
    f = fetcher(lambda request: httpx.Response(200, json=board))
    unlisted = f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{UUID}"))
    broken = f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{OTHER}"))
    assert unlisted.status == "ok" and unlisted.posting.description_text == "Own services."
    assert broken.status == "failed" and "invalid" in broken.error
```

追加到 `tests/worker/test_round.py`：

```python
def test_a_few_failed_requests_do_not_fail_the_round(store, tmp_path):
    from auto_app.enrich.fetch import FetchResult

    ingest(store, [record(str(200 + i), f"Backend Engineer {i}") for i in range(4)])
    results = iter([FetchResult("failed", error="HTTPStatusError: HTTP 503")] + [ok("Build APIs.")] * 10)
    report = run_round(store, options(tmp_path, steps=("screen", "enrich")), fetcher=FakeFetcher(lambda t: next(results)),
                       discord=None, state={}, now=NOW)
    assert report["steps"]["enrich"]["failed"] == 1 and report["steps"]["enrich"]["status"] == "ok"
    assert report["status"] == "ok"


def test_when_every_request_fails_the_round_reports_an_outage(store, tmp_path):
    from auto_app.enrich.fetch import FetchResult

    ingest(store, [record(str(200 + i), f"Backend Engineer {i}") for i in range(4)])
    report = run_round(store, options(tmp_path, steps=("screen", "enrich")),
                       fetcher=FakeFetcher(FetchResult("failed", error="ConnectError")), discord=None, state={}, now=NOW)
    assert report["steps"]["enrich"]["status"] == "error" and report["status"] == "partial"


def test_rescreen_runs_only_when_enrichment_changed_facts(store, tmp_path):
    from auto_app.enrich.fetch import FetchResult

    nothing = run_round(store, options(tmp_path, steps=("screen", "enrich")),
                        fetcher=FakeFetcher(FetchResult("failed", error="x")), discord=None, state={}, now=NOW)
    assert "rescreen" not in nothing["steps"]
```

> 第三个测试里只有 1 个候选岗位（实习会被规则排除），失败数 1 < 3，不算"全部失败"。它验证的是"没有新事实就不 rescreen"。

追加到 `tests/query/test_screening.py`：

```python
def test_rule_exclusions_never_hide_jobs_you_decided_on(screening_db):  # noqa: F811
    with sqlite3.connect(screening_db) as conn:
        conn.execute("UPDATE jobs SET decision='want_to_apply' WHERE job_id=2")
    with service(screening_db) as query:
        assert ids(query.jobs()) == [3, 2, 1]
        assert ids(query.jobs(state="want_to_apply")) == [2]
```

追加到 `tests/screen/test_rules.py`：

```python
@pytest.mark.parametrize("description", [
    "With 25 years of industry experience, Acme builds payment systems.",
    "Acme has over 30 years of experience in banking software.",
    "Founded in 1999, we bring 25+ years of experience to our clients.",
])
def test_employer_tenure_is_not_a_requirement(description):
    assert years_required(description) is None


def test_requirements_after_a_tenure_sentence_still_count():
    text = "With 25 years of industry experience, Acme builds payments. Requires 5+ years of professional experience."
    assert years_required(text) == 5
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/enrich/test_fetch.py tests/worker/test_round.py tests/query/test_screening.py tests/screen/test_rules.py -q`
Expected: 新增的测试 FAIL（`_same_requisition` 不存在；enrich 状态是 error；job 2 被隐藏；tenure 句子返回 25）

- [ ] **Step 3: 实现**

`src/auto_app/enrich/fetch.py`：在 `_validate_record` 之前加：

```python
_COPY_SUFFIX = re.compile(r"[-_]\d{1,2}$")


def _same_requisition(expected: str, found: str) -> bool:
    """Workday URL ids often add a copy/location suffix (R123-1) or drop a prefix (JR123 vs 123)."""
    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", _COPY_SUFFIX.sub("", value.strip().lower()))

    def digits(value: str) -> str:
        return max(re.findall(r"\d+", value), key=len, default="")

    if not norm(expected):
        return False
    if norm(expected) == norm(found):
        return True
    a, b = digits(_COPY_SUFFIX.sub("", expected)), digits(found)
    return len(a) >= 4 and a.lstrip("0") == b.lstrip("0")
```

（文件顶部 import 加 `import re`。）

把 `_validate_record` 里的 id 检查替换为：

```python
    found = record.get(id_key)
    if platform == "workday":
        # The request already named this exact posting path; a missing or reformatted requisition is fine.
        if found not in (None, "") and not _same_requisition(job_id, str(found)):
            raise ValueError("record does not match requested job")
    elif str(found or "").lower() != job_id.lower():
        raise ValueError("record does not match requested job")
```

`_ashby_record` 里的循环改为逐条校验、跳过坏记录、保留未公开（unlisted）的岗位：

```python
                jobs = {}
                for job in data["jobs"]:
                    if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not job["id"]:
                        continue
                    try:
                        _validate_record("ashby", job, job["id"])
                    except ValueError:
                        jobs[job["id"].lower()] = FetchResult("failed", error="invalid job record on the board")
                        continue
                    jobs[job["id"].lower()] = job  # unlisted jobs stay readable by their direct link
                self._boards[target.board] = jobs
```

并把 `fetch()` 里 Ashby 分支的处理改为：

```python
                record = self._ashby_record(target)
                if isinstance(record, FetchResult):
                    return record
```

（`_ashby_record` 最后一行保持 `return jobs if isinstance(jobs, FetchResult) else jobs.get(target.job_id)`：单条坏记录现在是 `FetchResult`，会原样返回。）

`fetch()` 的 except 分支里，**只有**整块招聘板读取失败时才缓存失败：把 `if target.platform == "ashby": self._boards[target.board] = failed` 改为 `if target.platform == "ashby" and target.board not in self._boards: self._boards[target.board] = failed`。

`src/auto_app/worker/round.py`：把

```python
        if enriched.get("failed"):
            enriched.update(status="error", error="Employer requests failed; retry scheduled (see failed count)")
        if "screen" in opts.steps:
            # A gone/404 result changes screening even without any new description.
            step("rescreen", screen)
```

替换为：

```python
        changed = sum(enriched.get(key, 0) for key in ("ok", "from_cache", "gone"))
        if enriched.get("failed", 0) >= 3 and changed == 0:
            # Routine transient failures retry later; only a round where everything failed is an outage.
            enriched.update(status="error", error=f"every employer request failed this round ({enriched['failed']})")
        if "screen" in opts.steps and (changed or enriched.get("jobs_enriched")):
            # New descriptions and gone/404 results both change screening facts.
            step("rescreen", screen)
```

`src/auto_app/query/service.py`：

```python
SCREENED_OUT_SQL = ("EXISTS (SELECT 1 FROM job_screenings s WHERE s.job_id=j.job_id AND s.outcome='excluded') "
                    "AND COALESCE(j.decision, '') = ''")  # your decisions are never hidden by rules
```

（`conditions.append(f"NOT {SCREENED_OUT_SQL}")` 要加括号：改成 `conditions.append(f"NOT ({SCREENED_OUT_SQL})")`。）

`src/auto_app/screen/rules.py`：把 `RULES_VERSION` 改为 `"4"`，并把 `NON_CANDIDATE_EXPERIENCE` 改为：

```python
NON_CANDIDATE_EXPERIENCE = re.compile(
    r"\b(?:our|the)\s+(?:company|team|business|founders?|engineers?)\b|\bwe\s+(?:have|bring)\b|"
    r"^\s*(?:with|for|after|over|nearly|almost)\s+(?:over\s+|more\s+than\s+)?\d{1,3}\+?\s+years?\s+of\s+"
    r"(?:\w+\s+){0,2}experience\s*,|"  # "With 25 years of industry experience, Acme …"
    r"\b(?:has|have|brings?)\s+(?:over\s+|more\s+than\s+|nearly\s+)?\d{1,3}\+?\s+years?\s+of\s+"
    r"(?:\w+\s+){0,2}experience\b", re.I)  # "Acme has over 30 years of experience …"
```

`src/auto_app/worker/cli.py`：`scheduler._write_state(db_path, state, ".worker-state.json")` 改为：

```python
            from auto_app.worker import loop

            scheduler._write_state(db_path, state, loop.STATE)
```

- [ ] **Step 4: 运行，确认通过；再跑相关目录**

Run: `uv run pytest tests/enrich tests/worker tests/query tests/screen tests/agent -q`
Expected: PASS

- [ ] **Step 5: 用 ReportFindings 回报这些审查结果的处理状态**

本会话中执行时，按 code-review 的约定，再调用一次 ReportFindings，带上 8 条结果和各自的 `outcome`：第 1–6 条和第 8 条为 `fixed`，第 7 条为 `skipped`。

- [ ] **Step 6: Commit（仅在用户批准时）**

```bash
git add src/auto_app/enrich/fetch.py src/auto_app/worker/round.py src/auto_app/query/service.py src/auto_app/screen/rules.py src/auto_app/worker/cli.py tests
git commit -m "fix(worker): Workday id variants, Ashby board isolation, decided jobs stay visible

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7:「用户」部署 P0 并完成验收**：按 [P0 计划 Task 11](2026-10-12-p0-linux-screening-worker.md) 执行。P1 的 Task 2–8 可以并行开发，但它们的线上验收以 P0 已上线为前提。

---

### Task 2: 公司招聘板 feed（Greenhouse / Lever / Ashby）和招聘板推荐

**Files:**
- Create: `src/auto_app/ingest/repository_boards.py`、`src/auto_app/worker/boards.py`
- Modify:
  - `src/auto_app/ingest/repository.py`：`REPOSITORY_FORMATS`、`parse_repository(content, *, format, company=None)`
  - `src/auto_app/ingest/runner.py`：`run_repository(..., company=None)`；`load_feed_config` 读取可选的 `company`；`run_config` 把它传下去
  - `src/auto_app/worker/cli.py`：新增 `boards` 命令
  - `tests/test_cli_structure.py`：`"worker": ["run", "watch", "status", "boards"]`
- Test: `tests/ingest/test_repository_boards.py`、`tests/worker/test_boards.py`

**Interfaces:**
- Produces:
  - `BOARD_FORMATS = {"greenhouse-board": GreenhouseSource, "lever-board": LeverSource, "ashby-board": AshbySource}`
  - `parse_board(content: bytes, *, format: str, company: str | None = None) -> ParsedFeed`
  - `board_location(platform: str, board: str) -> str`
  - `suggest_boards(store, *, configured: set[str], top: int = 30, min_jobs: int = 2) -> list[dict]`。每项包含：`platform`、`board`、`company`、`kept_jobs`、`sponsoring_jobs`、`location`、`source`、`configured`。
  - feeds.yaml 示例：

```yaml
  board-greenhouse-stripe:
    kind: repository
    format: greenhouse-board
    company: Stripe
    location: https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true
```

- [ ] **Step 1: 写失败测试**

```python
# tests/ingest/test_repository_boards.py
"""Employer boards as complete repository snapshots, with descriptions, merging with repository copies."""

import json

import pytest

from auto_app.ingest.repository import parse_repository
from tests.imported_jobs import ingest, make_store, record

GREENHOUSE = {"jobs": [
    {"id": 4012345, "title": "Software Engineer, New Grad", "company_name": "Acme",
     "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4012345", "location": {"name": "Boston, MA"},
     "content": "&lt;p&gt;Build APIs in Python.&lt;/p&gt;", "updated_at": "2026-10-01T00:00:00Z"},
    {"id": 4012346, "title": "", "absolute_url": "not a url"},
]}
LEVER = [{"id": "6ed76ce8-1111-2222-3333-444455556666", "text": "Data Engineer",
          "hostedUrl": "https://jobs.lever.co/acme/6ed76ce8-1111-2222-3333-444455556666",
          "applyUrl": "https://jobs.lever.co/acme/6ed76ce8-1111-2222-3333-444455556666/apply",
          "categories": {"location": "Remote", "commitment": "Full-time"}, "descriptionPlain": "Own pipelines.",
          "createdAt": 1760000000000}]
ASHBY = {"jobs": [
    {"id": "91eed659-1111-2222-3333-444455556666", "title": "Backend Engineer", "location": "New York",
     "jobUrl": "https://jobs.ashbyhq.com/acme/91eed659-1111-2222-3333-444455556666", "descriptionPlain": "Own services.",
     "employmentType": "FullTime", "isListed": True},
    {"id": "aaaaaaaa-1111-2222-3333-444455556666", "title": "Hidden", "jobUrl": "https://x", "isListed": False},
]}


def test_greenhouse_board_rows_carry_descriptions_and_bad_rows_are_isolated():
    parsed = parse_repository(json.dumps(GREENHOUSE).encode(), format="greenhouse-board")
    assert [r.external_id for r in parsed.records] == ["4012345"]
    first = parsed.records[0]
    assert first.company == "Acme" and "Build APIs in Python." in first.description
    assert first.url == "https://job-boards.greenhouse.io/acme/jobs/4012345" and first.active is True
    assert len(parsed.issues) == 1


def test_lever_and_ashby_boards_use_the_configured_company_and_skip_unlisted():
    lever = parse_repository(json.dumps(LEVER).encode(), format="lever-board", company="Acme")
    ashby = parse_repository(json.dumps(ASHBY).encode(), format="ashby-board", company="Acme")
    assert lever.records[0].company == "Acme" and lever.records[0].employment_type == "full_time"
    assert [r.title for r in ashby.records] == ["Backend Engineer"] and not ashby.issues


def test_board_job_merges_with_the_repository_copy(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("4012345", "Software Engineer, New Grad")])  # the repository copy, no description
    board = parse_repository(json.dumps(GREENHOUSE).encode(), format="greenhouse-board")
    from auto_app.ingest.service import IngestService

    IngestService(store).ingest("board-greenhouse-acme", board, kind="repository", origin="board",
                                revision="r", complete_snapshot=False)
    jobs = store.jobs()
    assert len(jobs) == 1 and set(jobs[0]["sources"]) == {"simplify-newgrad", "board-greenhouse-acme"}
    store.engine.dispose()


def test_feed_config_accepts_a_company_for_board_formats(tmp_path):
    from auto_app.ingest.runner import load_feed_config

    path = tmp_path / "feeds.yaml"
    path.write_text("sources:\n  board-lever-acme:\n    kind: repository\n    format: lever-board\n"
                    "    company: Acme\n    location: https://api.lever.co/v0/postings/acme?mode=json\n")
    assert load_feed_config(path)["board-lever-acme"]["company"] == "Acme"
    path.write_text("sources:\n  x:\n    kind: repository\n    format: lever-board\n    company: 3\n"
                    "    location: https://api.lever.co/v0/postings/acme?mode=json\n")
    with pytest.raises(ValueError, match="company"):
        load_feed_config(path)
```

```python
# tests/worker/test_boards.py
from datetime import timezone

import pytest
from sqlalchemy import insert

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.worker.boards import board_location, suggest_boards
from tests.imported_jobs import ingest, job_id_of, make_store, record


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Backend Engineer"), record("102", "Data Engineer"),
                    record("103", "ML Engineer", url="https://jobs.lever.co/beta/6ed76ce8-1111-2222-3333-444455556666")])
    with result.engine.begin() as conn:
        for title, sponsorship in (("Backend Engineer", "offers"), ("Data Engineer", "unknown"), ("ML Engineer", "no")):
            conn.execute(insert(T.job_screenings).values(
                job_id=job_id_of(result, title), rules_version="t", facts_hash="h", outcome="kept", reasons=[],
                signals={"sponsorship": sponsorship}, screened_at=utcnow()))
    yield result
    result.engine.dispose()


def test_boards_are_ranked_by_sponsoring_then_kept_jobs(store):
    boards = suggest_boards(store, configured=set(), top=10, min_jobs=1)
    assert [(b["platform"], b["board"], b["kept_jobs"], b["sponsoring_jobs"]) for b in boards] == [
        ("greenhouse", "acme", 2, 1), ("lever", "beta", 1, 0)]
    assert boards[0]["location"] == board_location("greenhouse", "acme")
    assert boards[0]["source"] == "board-greenhouse-acme" and boards[0]["company"] == "Acme"


def test_configured_boards_and_small_boards_are_marked_or_dropped(store):
    boards = suggest_boards(store, configured={board_location("greenhouse", "acme")}, top=10, min_jobs=2)
    assert [(b["board"], b["configured"]) for b in boards] == [("acme", True)]
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/ingest/test_repository_boards.py tests/worker/test_boards.py -q`
Expected: FAIL，`repository format must be one of` / `ModuleNotFoundError: auto_app.worker.boards`

- [ ] **Step 3: 实现**

```python
# src/auto_app/ingest/repository_boards.py
"""Employer job boards as repository feeds: Greenhouse, Lever and Ashby public list APIs.

Each list API returns every open job with its description, so these feeds need no enrichment. A successful
read is a complete snapshot, exactly like a repository: jobs no longer listed become `missing`. The
platform parsers are the collectors' own, so a board job reads like a collected one and merges with
repository copies of the same posting through its employer key.
"""

import json

from auto_app.core.models import RawPosting
from auto_app.ingest.models import IngestRecord, ParsedFeed
from auto_app.ingest.urls import canonical_url
from auto_app.platforms.ashby import AshbySource
from auto_app.platforms.greenhouse import GreenhouseSource
from auto_app.platforms.lever import LeverSource
from auto_app.sources.parsing import ParseError

BOARD_FORMATS = {"greenhouse-board": GreenhouseSource, "lever-board": LeverSource, "ashby-board": AshbySource}


def _rows(format: str, document) -> list:
    if format == "lever-board":
        if not isinstance(document, list):
            raise ValueError("a Lever board feed must be a JSON array")
        return document
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), list):
        raise ValueError("a board feed must be an object with a jobs array")
    return document["jobs"]


def parse_board(content: bytes, *, format: str, company: str | None = None) -> ParsedFeed:
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("board feed must contain valid JSON") from exc
    source = BOARD_FORMATS[format]()
    result, seen = ParsedFeed(), set()
    for index, row in enumerate(_rows(format, document)):
        try:
            if not isinstance(row, dict):
                raise ValueError("job must be an object")
            if format == "ashby-board" and row.get("isListed") is False:
                continue  # not on the public board; a direct link may still work, but it isn't listed here
            job = source.to_job(row)
            posting = source.parse(RawPosting(source=format, source_job_id=job.id, kind="api", posting={},
                                              capture={}, card={}, record=row, company_hint=company))
            url = canonical_url(posting.url or job.url or "")
            if not url or not (posting.title or "").strip():
                raise ValueError("job needs a public URL and a title")
            if job.id in seen:
                raise ValueError(f"duplicate id {job.id!r}")
        except (ValueError, KeyError, TypeError, ParseError) as exc:
            result.issues.append({"index": index, "reason": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        seen.add(job.id)
        result.records.append(IngestRecord(
            external_id=job.id, url=url, title=posting.title.strip(), company=posting.company or company,
            locations=posting.locations, description=posting.description_text or "", posted_at=posting.posted_at,
            employment_type=posting.employment_type, active=True,
            metadata={"upstream": {"platform": source.platform, "board_job_id": job.id,
                                   "req_id": posting.req_id}}))
    return result
```

`src/auto_app/ingest/repository.py`：

```python
REPOSITORY_FORMATS = ("simplify", "speedyapply", "applyguy", "ricsign", "jobright",
                      "greenhouse-board", "lever-board", "ashby-board")


def parse_repository(content: bytes, *, format: str = "simplify", company: str | None = None) -> ParsedFeed:
    """Use explicit source format; existing configurations default to Simplify."""
    validate_repository_format(format)
    if format.endswith("-board"):
        from auto_app.ingest.repository_boards import parse_board
        return parse_board(content, format=format, company=company)
    # … 其余分支保持不变 …
```

`src/auto_app/ingest/runner.py`：
- `run_repository(store, source, location, client=None, *, format="simplify", company=None)`，把 `parse_repository(result.content, format=format)` 改为 `parse_repository(result.content, format=format, company=company)`。
- `load_feed_config` 的 repository 分支改为：

```python
        if kind == "repository":
            format = definition.get("format", "simplify")
            validate_repository_format(format)
            company = definition.get("company")
            if company is not None and (not isinstance(company, str) or not company.strip()):
                raise ValueError(f"{source}: company must be a nonempty string")
            sources[source] = {"kind": kind, "format": format, "company": company.strip() if company else None,
                               "location": resolve_location(definition.get("location"), config_path.parent)}
```

- `run_config` 里调用 `run_repository(..., format=definition["format"], company=definition.get("company"))`。

```python
# src/auto_app/worker/boards.py
"""Which employer boards are worth a feed: ranked by kept jobs that sponsor, then all kept jobs (docs/linux-worker.md §6).

Read-only. Boards come from the apply links of jobs the screening rules kept; only Greenhouse, Lever and
Ashby have public list APIs with descriptions.
"""

import re
from collections import Counter, defaultdict

from sqlalchemy import select

from auto_app.core.db import tables as T
from auto_app.enrich.targets import target_for

LIST_APIS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{board}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true",
}
FORMATS = {"greenhouse": "greenhouse-board", "lever": "lever-board", "ashby": "ashby-board"}


def board_location(platform: str, board: str) -> str:
    return LIST_APIS[platform].format(board=board)


def suggest_boards(store, *, configured: set[str], top: int = 30, min_jobs: int = 2) -> list[dict]:
    p, j, s = T.postings, T.jobs, T.job_screenings
    query = (select(p.c.apply_url, p.c.company, j.c.job_id, s.c.signals)
             .join(j, j.c.job_id == p.c.job_id).join(s, s.c.job_id == j.c.job_id)
             .where(j.c.archived_at.is_(None), s.c.outcome == "kept", p.c.apply_url.is_not(None)))
    jobs, sponsoring, companies = defaultdict(set), defaultdict(set), defaultdict(Counter)
    with store.engine.connect() as conn:
        for url, company, job_id, signals in conn.execute(query):
            target = target_for(url)
            if target is None or target.platform not in LIST_APIS:
                continue
            key = (target.platform, target.board)
            jobs[key].add(job_id)
            if (signals or {}).get("sponsorship") == "offers":
                sponsoring[key].add(job_id)
            if company:
                companies[key][company] += 1
    out = []
    for (platform, board), ids in jobs.items():
        if len(ids) < min_jobs:
            continue
        location = board_location(platform, board)
        out.append({"platform": platform, "board": board, "kept_jobs": len(ids),
                    "sponsoring_jobs": len(sponsoring[(platform, board)]),
                    "company": (companies[(platform, board)].most_common(1) or [(None, 0)])[0][0],
                    "location": location, "format": FORMATS[platform],
                    "source": re.sub(r"[^a-z0-9_-]+", "-", f"board-{platform}-{board}".lower()).strip("-"),
                    "configured": location in configured})
    out.sort(key=lambda b: (b["sponsoring_jobs"], b["kept_jobs"], b["board"]), reverse=True)
    return out[:top]


def as_yaml(boards: list[dict]) -> str:
    """feeds.yaml entries for the boards not configured yet (paste under `sources:`)."""
    lines = []
    for b in boards:
        if b["configured"]:
            continue
        lines += [f"  {b['source']}:", "    kind: repository", f"    format: {b['format']}"]
        if b["company"]:
            lines.append(f"    company: {b['company']!r}")
        lines += [f"    location: {b['location']}", ""]
    return "\n".join(lines)
```

> 注意：测试里 `[(…("greenhouse", "acme", 2, 1), ("lever", "beta", 1, 0))]` 依赖倒序排序；`suggest_boards` 用 `(sponsoring, kept, board)` 倒序，两条记录的顺序正确。

`src/auto_app/worker/cli.py` 末尾加：

```python
@app.command("boards")
def boards(db: DbOption = None,
           top: Annotated[int, typer.Option("--top", min=1, max=200, help="How many boards to list.")] = 30,
           min_jobs: Annotated[int, typer.Option("--min-jobs", min=1, help="Skip boards with fewer kept jobs.")] = 2,
           yaml_out: Annotated[bool, typer.Option("--yaml", help="Print feeds.yaml entries instead of JSON.")] = False) -> None:
    """Employer boards worth adding as feeds, ranked by kept jobs that sponsor visas. Read-only."""
    from auto_app.core.db.store import DbStore
    from auto_app.ingest.runner import load_feed_config
    from auto_app.worker.boards import as_yaml, suggest_boards

    paths = get_paths()
    feeds = paths.home / "config" / "feeds.yaml"
    configured = ({d["location"] for d in load_feed_config(feeds).values() if d.get("location")}
                  if feeds.exists() else set())
    store = DbStore(db or paths.db, paths, migrate=False)
    try:
        found = suggest_boards(store, configured=configured, top=top, min_jobs=min_jobs)
    finally:
        store.engine.dispose()
    typer.echo(as_yaml(found) if yaml_out else json.dumps(found, ensure_ascii=False, indent=2))
```

- [ ] **Step 4: 运行，确认通过；再跑 ingest 全部测试**

Run: `uv run pytest tests/ingest tests/worker tests/test_cli_structure.py -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准时）**

```bash
git add src/auto_app/ingest src/auto_app/worker tests
git commit -m "feat(ingest): Greenhouse, Lever and Ashby boards as feeds, with board suggestions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: IMAP 邮箱（Gmail 应用专用密码等）

**Files:**
- Create: `src/auto_app/ingest/imap.py`
- Modify: `src/auto_app/ingest/mailbox.py`（`validate_mailbox_config`、`run_mailbox`、`_page`、`_message`）
- Test: `tests/ingest/test_imap.py`

**Interfaces:**
- Produces:
  - `load_imap_credentials(path: Path) -> dict`：返回 `{host, port, username, password}`；权限比 600 宽、或缺字段时抛 `ImapError`。
  - `ImapSession(credentials, *, connect=imaplib.IMAP4_SSL)`，上下文管理器，方法：
    - `select(folder) -> None`：EXAMINE，并记录 `uidvalidity`
    - `uids(config, start, end) -> list[str]`
    - `fetch(uid) -> tuple[bytes, datetime]`：用 `BODY.PEEK[]`
  - feeds.yaml 示例：

```yaml
  gmail-alerts:
    kind: mailbox
    provider: imap
    credentials: ../credentials/imap.json   # {"host": "imap.gmail.com", "username": "you@gmail.com", "password": "<app password>"}
    folder: INBOX
    senders: [jobalerts-noreply@linkedin.com, jobs-noreply@linkedin.com, noreply@jobright.ai]
    lookback_days: 14
```

- [ ] **Step 1: 写失败测试**

```python
# tests/ingest/test_imap.py
"""IMAP mailboxes: read-only, private credentials, the shared mailbox scan."""

import os
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from auto_app.ingest.imap import ImapError, ImapSession, load_imap_credentials
from tests.imported_jobs import make_store


def alert(job_id="4012345678"):
    message = EmailMessage()
    message["From"] = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
    message["Subject"] = "Software Engineer, New Grad at Acme"
    message["Date"] = "Mon, 12 Oct 2026 10:00:00 +0000"
    message.set_content(f"Software Engineer, New Grad\nAcme\nhttps://www.linkedin.com/comm/jobs/view/{job_id}/\n")
    return message.as_bytes()


class FakeConnection:
    def __init__(self, messages):
        self.messages, self.calls = messages, []

    def login(self, user, password):
        self.calls.append(("login", user))
        return "OK", [b"ok"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [b"777"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(self.messages).encode()]
        uid, items = args
        stamp = b'INTERNALDATE "12-Oct-2026 10:00:00 +0000"'
        return "OK", [(b"1 (UID " + uid.encode() + b" " + stamp + b" BODY[] {9}", self.messages[uid]), b")"]

    def logout(self):
        self.calls.append(("logout",))


def credentials(tmp_path, mode=0o600):
    path = tmp_path / "imap.json"
    path.write_text('{"host": "imap.example.com", "username": "me@example.com", "password": "app-pass"}')
    os.chmod(path, mode)
    return path


def test_credentials_must_be_private_and_complete(tmp_path):
    assert load_imap_credentials(credentials(tmp_path))["port"] == 993
    with pytest.raises(ImapError, match="chmod 600"):
        load_imap_credentials(credentials(tmp_path, 0o644))


def test_session_is_read_only_and_never_marks_seen(tmp_path):
    fake = FakeConnection({"5": alert()})
    with ImapSession(load_imap_credentials(credentials(tmp_path)), connect=lambda *a, **k: fake) as session:
        session.select("INBOX")
        uids = session.uids({"folder": "INBOX", "senders": ["a@x.com", "b@y.com"], "gmail_raw": None},
                            datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc))
        content, received = session.fetch(uids[0])
    assert uids == ["5"] and content.startswith(b"From:") and received == datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
    assert ("select", '"INBOX"', True) in fake.calls
    search = next(c for c in fake.calls if c[:2] == ("uid", "SEARCH"))
    assert search[2:] == ("SINCE", "01-Oct-2026", "BEFORE", "13-Oct-2026", "OR", "FROM", '"a@x.com"', "FROM", '"b@y.com"')
    fetch = next(c for c in fake.calls if c[:2] == ("uid", "FETCH"))
    assert fetch[3] == "(INTERNALDATE BODY.PEEK[])"
    assert not any("STORE" in str(c) for c in fake.calls)


def test_login_failures_never_include_the_password(tmp_path):
    class Refuses(FakeConnection):
        def login(self, user, password):
            import imaplib
            raise imaplib.IMAP4.error(f"bad password {password}")

    with pytest.raises(ImapError) as failure:
        with ImapSession(load_imap_credentials(credentials(tmp_path)), connect=lambda *a, **k: Refuses({})):
            pass
    assert "app-pass" not in str(failure.value)


def test_an_imap_source_imports_alert_links_once(tmp_path):
    from auto_app.ingest.mailbox import run_mailbox

    store = make_store(tmp_path)
    definition = {"provider": "imap", "credentials": str(credentials(tmp_path)), "folder": "INBOX",
                  "senders": ["jobalerts-noreply@linkedin.com"]}
    fake = FakeConnection({"5": alert(), "6": alert("4012345679")})
    with ImapSession(load_imap_credentials(credentials(tmp_path)), connect=lambda *a, **k: fake) as session:
        session.select("INBOX")
        first = run_mailbox(store, "gmail-alerts", definition, client=session,
                            now=datetime(2026, 10, 12, 12, tzinfo=timezone.utc))
        again = run_mailbox(store, "gmail-alerts", definition, client=session,
                            now=datetime(2026, 10, 12, 13, tzinfo=timezone.utc))
    assert first["status"] == "ok" and first["messages_processed"] == 2 and first["created"] == 2
    assert again["created"] == 0
    store.engine.dispose()
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/ingest/test_imap.py -q`
Expected: FAIL，`ModuleNotFoundError: auto_app.ingest.imap`

- [ ] **Step 3: 实现**

```python
# src/auto_app/ingest/imap.py
"""Read-only IMAP access for mailbox sources (docs/mailbox-linux-setup.md, "IMAP").

EXAMINE (a read-only SELECT) and BODY.PEEK[] leave every message unread and in its folder. The password
comes from a private credentials file (mode 600) and is never logged or included in an error.
"""

import imaplib
import json
import re
import ssl
from datetime import datetime, timedelta
from pathlib import Path


class ImapError(ValueError):
    pass


def load_imap_credentials(path: Path) -> dict:
    path = Path(path)
    try:
        if path.stat().st_mode & 0o077:
            raise ImapError(f"{path.name} must be readable only by its owner (chmod 600)")
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        if isinstance(exc, ImapError):
            raise
        raise ImapError(f"Cannot read IMAP credentials {path.name}") from None
    if not isinstance(data, dict):
        raise ImapError("IMAP credentials must be a JSON object")
    host, port = data.get("host"), data.get("port", 993)
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", host or ""):
        raise ImapError("IMAP credentials need a host name")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ImapError("IMAP port must be an integer")
    for key in ("username", "password"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ImapError(f"IMAP credentials need a {key}")
    return {"host": host, "port": port, "username": data["username"], "password": data["password"]}


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')


class ImapSession:
    def __init__(self, credentials: dict, *, connect=imaplib.IMAP4_SSL):
        self.credentials, self._connect = credentials, connect
        self.conn, self.uidvalidity, self._uids = None, None, {}

    def __enter__(self):
        host = self.credentials["host"]
        try:
            self.conn = self._connect(host, self.credentials["port"], ssl_context=ssl.create_default_context(),
                                      timeout=30)
            self.conn.login(self.credentials["username"], self.credentials["password"])
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"IMAP login to {host} failed ({type(exc).__name__})") from None
        return self

    def __exit__(self, *exc):
        try:
            if self.conn is not None:
                self.conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def select(self, folder: str) -> None:
        typ, _ = self.conn.select(_quote(folder), readonly=True)  # EXAMINE: nothing can be changed
        if typ != "OK":
            raise ImapError(f"cannot open folder {folder!r}")
        _, data = self.conn.response("UIDVALIDITY")
        self.uidvalidity = (data[0].decode() if data and data[0] else "0")

    def uids(self, config: dict, start: datetime, end: datetime) -> list[str]:
        key = (config["folder"], start.date(), end.date())
        if key not in self._uids:
            criteria = ["SINCE", start.strftime("%d-%b-%Y"), "BEFORE", (end + timedelta(days=1)).strftime("%d-%b-%Y")]
            senders = config.get("senders") or []
            if senders:
                criteria += ["OR"] * (len(senders) - 1)
                for sender in senders:
                    criteria += ["FROM", _quote(sender)]
            if config.get("gmail_raw"):
                criteria += ["X-GM-RAW", _quote(config["gmail_raw"])]
            typ, data = self.conn.uid("SEARCH", *criteria)
            if typ != "OK":
                raise ImapError("IMAP search failed")
            self._uids[key] = sorted((data[0] or b"").decode().split(), key=int)
        return self._uids[key]

    def fetch(self, uid: str) -> tuple[bytes, datetime]:
        typ, data = self.conn.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[])")  # PEEK: never sets \Seen
        part = next((item for item in data or [] if isinstance(item, tuple)), None)
        if typ != "OK" or part is None or not part[1]:
            raise ImapError("IMAP returned no message")
        stamp = INTERNALDATE.search(part[0])
        if stamp is None:
            raise ImapError("IMAP returned no received date")
        return part[1], datetime.strptime(stamp.group(1).decode(), "%d-%b-%Y %H:%M:%S %z")
```

修改 `src/auto_app/ingest/mailbox.py`：

1. `validate_mailbox_config`：
   - provider 校验改为 `if provider not in {"gmail", "outlook", "imap"}: raise ValueError("mailbox provider must be gmail, outlook or imap")`。
   - `string_key, list_key` 改为：

```python
    string_key, list_key = {"gmail": ("query", "label_ids"), "outlook": ("folder", "senders"),
                            "imap": ("gmail_raw", "senders")}[provider]
```

   - page_size 上限：`500 if provider in ("gmail", "imap") else 1000`。
   - 在 `return result` 前加：

```python
    if provider == "imap":
        folder = definition.get("folder", "INBOX")
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError("imap folder must be a nonempty string")
        result["folder"] = folder.strip()
```

2. `run_mailbox` 开头：在 `if client is None:` 之前加：

```python
    if config["provider"] == "imap" and client is None:
        from auto_app.ingest.imap import ImapSession, load_imap_credentials

        with ImapSession(load_imap_credentials(Path(config["credentials"]))) as session:
            session.select(config["folder"])
            return run_mailbox(store, source, config, client=session, now=now)
```

   把 `token = access_token(...)` 和 `headers` 两行改为：

```python
    if provider == "imap":
        headers = {}
    else:
        token = access_token(path, provider=provider, client=client)
        headers = {"Authorization": f"Bearer {token}"}
```

3. `_page` 开头加 imap 分支：

```python
    if config["provider"] == "imap":
        uids = client.uids(config, _date(checkpoint["window_start"]), _date(checkpoint["window_end"]))
        offset = int(cursor or 0)
        page = uids[offset:offset + config["page_size"]]
        nxt = str(offset + config["page_size"]) if offset + config["page_size"] < len(uids) else None
        return [{"id": f"{client.uidvalidity}:{uid}"} for uid in page], nxt
```

4. `_message` 的 `try` 块开头加：

```python
        if provider == "imap":
            content, received = client.fetch(message["id"].split(":", 1)[1])
        elif provider == "gmail":
```

   （原来的 `if provider == "gmail":` 改为上面的 `elif`；`except` 里再加 `ImapError`：`from auto_app.ingest.imap import ImapError` 放在函数开头，捕获列表加上 `ImapError`。）

- [ ] **Step 4: 运行，确认通过；再跑邮件相关测试**

Run: `uv run pytest tests/ingest -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准时）**

```bash
git add src/auto_app/ingest/imap.py src/auto_app/ingest/mailbox.py tests/ingest/test_imap.py
git commit -m "feat(ingest): read-only IMAP mailbox provider

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 带 token 认证的公网 MCP 入口

**Files:**
- Create: `src/auto_app/agent/auth.py`、`deploy/auto-app-agent-public.service`
- Modify: `src/auto_app/agent/mcp_server.py`（`create_mcp_server(..., auth=None)`）、`src/auto_app/agent/cli.py`（`--auth-token-file`、`--public-url`）
- Test: `tests/agent/test_public_auth.py`

**Interfaces:**
- Produces:
  - `load_token(path: Path) -> str`：至少 32 个字符；文件权限必须是 600。
  - `StaticTokenVerifier(token: str)`，方法 `async verify_token(token) -> AccessToken | None`。
  - `PublicAuth(token: str, public_url: str)`（frozen dataclass），属性：
    - `host`：公网 URL 的 netloc
    - `settings`：`AuthSettings`
    - `transport_security(port) -> TransportSecuritySettings`
  - `create_mcp_server(db_path, preferences_path=None, preparation_dir=None, *, auth: PublicAuth | None = None, control_tools=False)`（`control_tools` 在 Task 6 用）。
  - CLI：`auto-app-agent serve ... --transport streamable-http --port 8769 --auth-token-file runtime/credentials/mcp-token --public-url https://<host>`。两个参数必须同时给。

- [ ] **Step 1: 写失败测试**

```python
# tests/agent/test_public_auth.py
"""The public MCP listener: a static bearer token, and only the public host or loopback in Host."""

import json
import os

import pytest
from starlette.testclient import TestClient

from tests.screening_db import screening_db  # noqa: F401 — the shared fixture

TOKEN = "t" * 40
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def app(db):
    from auto_app.agent.auth import PublicAuth
    from auto_app.agent.mcp_server import create_mcp_server

    auth = PublicAuth(TOKEN, "https://testserver")
    server = create_mcp_server(db, auth=auth)
    return server.streamable_http_app(json_response=True, transport_security=auth.transport_security(8769))


def test_requests_without_the_token_are_rejected(screening_db):  # noqa: F811
    with TestClient(app(screening_db)) as client:
        assert client.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        wrong = client.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": "Bearer " + "x" * 40})
        assert wrong.status_code == 401
        right = client.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {TOKEN}"})
        assert right.status_code == 200, right.text


def test_unknown_hosts_are_rejected_even_with_the_token(screening_db):  # noqa: F811
    with TestClient(app(screening_db), base_url="https://evil.example") as client:
        response = client.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {TOKEN}"})
        assert response.status_code in (400, 403, 421)


def test_token_files_must_be_private_and_long(tmp_path):
    from auto_app.agent.auth import load_token

    path = tmp_path / "mcp-token"
    path.write_text(TOKEN + "\n")
    os.chmod(path, 0o600)
    assert load_token(path) == TOKEN
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        load_token(path)
    path.write_text("short")
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="32"):
        load_token(path)


def test_the_cli_requires_both_auth_options(tmp_path):
    from auto_app.agent.cli import main

    assert main(["serve", "--db", str(tmp_path / "x.db"), "--transport", "streamable-http",
                 "--auth-token-file", str(tmp_path / "t")]) == 1
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/agent/test_public_auth.py -q`
Expected: FAIL，`ModuleNotFoundError: auto_app.agent.auth`

- [ ] **Step 3: 实现**

```python
# src/auto_app/agent/auth.py
"""The public MCP listener's single static bearer token (docs/remote-mcp.md).

The listener still binds to loopback; a tunnel (Tailscale Funnel or a Cloudflare named tunnel) carries
public traffic to it. Only the configured public host and loopback are accepted in Host headers.
"""

import hmac
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

SCOPE = "jobs"


def load_token(path: Path) -> str:
    path = Path(path)
    try:
        if path.stat().st_mode & 0o077:
            raise ValueError(f"{path.name} must be readable only by its owner (chmod 600)")
        token = path.read_text().strip()
    except OSError:
        raise ValueError(f"Cannot read the MCP token file {path.name}") from None
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("the MCP token must be at least 32 characters without spaces")
    return token


class StaticTokenVerifier:
    def __init__(self, token: str):
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token):
            return AccessToken(token=token, client_id="owner", scopes=[SCOPE])
        return None


@dataclass(frozen=True)
class PublicAuth:
    token: str
    public_url: str

    def __post_init__(self):
        u = urlsplit(self.public_url)
        if u.scheme != "https" or not u.hostname or u.path not in ("", "/") or u.query or u.username:
            raise ValueError("--public-url must be https://<host> without a path")

    @property
    def host(self) -> str:
        return urlsplit(self.public_url).netloc

    @property
    def settings(self) -> AuthSettings:
        base = self.public_url.rstrip("/")
        return AuthSettings(issuer_url=base, resource_server_url=f"{base}/mcp", required_scopes=[SCOPE],
                            validate_token_resource=False)

    def verifier(self) -> StaticTokenVerifier:
        return StaticTokenVerifier(self.token)

    def transport_security(self, port: int) -> TransportSecuritySettings:
        return TransportSecuritySettings(
            allowed_hosts=[self.host, f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=[f"https://{self.host}"])
```

`src/auto_app/agent/mcp_server.py`：
- `create_mcp_server` 签名加 `*, auth=None, control_tools: bool = False`。
- 构造 `MCPServer(...)` 时，在 `instructions=...` 之后加：

```python
        **({"token_verifier": auth.verifier(), "auth": auth.settings} if auth is not None else {}),
```

`src/auto_app/agent/cli.py`：
- `serve` 子命令加：

```python
    serve.add_argument("--auth-token-file", type=Path,
                       help="Require this bearer token (file mode 600); for the public listener only.")
    serve.add_argument("--public-url", help="The tunnel's https://<host>; required with --auth-token-file.")
```

- `main()` 里，在 `AgentService(...).context()` 之前加：

```python
        auth = None
        if args.auth_token_file or args.public_url:
            if not (args.auth_token_file and args.public_url) or args.transport != "streamable-http":
                raise ValueError("--auth-token-file and --public-url go together, with --transport streamable-http")
            from auto_app.agent.auth import PublicAuth, load_token

            auth = PublicAuth(load_token(args.auth_token_file), args.public_url)
```

- `create_mcp_server(...)` 调用加 `auth=auth`；streamable-http 分支改为：

```python
            extra = {"transport_security": auth.transport_security(args.port)} if auth else {}
            server.run(transport="streamable-http", host=args.host, port=args.port, json_response=True, **extra)
```

```ini
# deploy/auto-app-agent-public.service
[Unit]
Description=Job Search MCP tools (token-protected, for the public tunnel)
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/Desktop/job-search/auto-application
ExecStart=%h/Desktop/job-search/auto-application/.venv/bin/auto-app-agent serve --db %h/Desktop/job-search/runtime/data/auto_app.db --preferences %h/Desktop/job-search/runtime/config/preferences.json --transport streamable-http --host 127.0.0.1 --port 8769 --auth-token-file %h/Desktop/job-search/runtime/credentials/mcp-token --public-url https://REPLACE-WITH-TUNNEL-HOST --control-tools
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

（`--control-tools` 在 Task 6 实现；Task 6 完成前先删掉这个参数。）

- [ ] **Step 4: 运行，确认通过；再跑 agent 测试**

Run: `uv run pytest tests/agent -q`
Expected: PASS。如果 `test_requests_without_the_token_are_rejected` 的第三个断言失败，并且报错是 protocolVersion 不被接受，就把 `INIT` 里的版本改成 SDK 的 `mcp_types.LATEST_PROTOCOL_VERSION`（如果它存在），或者改成服务端报错信息里列出的版本。

- [ ] **Step 5:「用户」开隧道并实测 Grok（spike，决定认证方式）**

1. 在 Ubuntu 上生成 token：`umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/Desktop/job-search/runtime/credentials/mcp-token`
2. 选一种隧道：
   - **Tailscale Funnel**（不需要域名）：在 Ubuntu 上安装 tailscale 并登录，然后运行 `tailscale funnel --bg 8769`，记下 `https://<机器名>.<tailnet>.ts.net`。
   - **Cloudflare named tunnel**（需要托管在 Cloudflare 的域名）：`cloudflared tunnel create job-search-mcp`，ingress 指向 `http://127.0.0.1:8769`，主机名比如 `mcp.<你的域名>`。
3. 把公网 URL 填进 `deploy/auto-app-agent-public.service`，安装并启动这个服务。
4. 在 grok.com/connectors 新建 Custom connector：URL 填 `https://<host>/mcp`，认证选 API key / Bearer，填 token。
5. 在 Grok 里让它调用 `get_job_context`。

**结果分支**：
- 能调用 → 这一步完成，把实际操作步骤写进 `docs/remote-mcp.md`。
- Grok 只接受 OAuth → 停下来，告诉用户。需要另写一个"最小单用户 OAuth 授权服务"的计划（MCP SDK 的 `auth_server_provider` 接口），不在本计划范围内。Dot 和用户自己的 agent 不受影响，可以继续用 Bearer token。

- [ ] **Step 6: Commit（仅在用户批准时）**

```bash
git add src/auto_app/agent deploy/auto-app-agent-public.service tests/agent/test_public_auth.py
git commit -m "feat(agent): token-protected public MCP listener behind a tunnel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: worker 暂停/恢复（独立控制文件）

**Files:**
- Create: `src/auto_app/worker/control.py`
- Modify: `src/auto_app/worker/loop.py`（每次循环先读控制文件）
- Test: `tests/worker/test_control.py`

**Interfaces:**
- Produces:
  - `CONTROL = ".worker-control.json"`
  - `read_control(db_path) -> dict`：返回 `{"paused": bool, "paused_until": str | None, "reason": str | None, "updated_at": str | None}`
  - `write_control(db_path, *, paused: bool, paused_until: datetime | None = None, reason: str | None = None, now=None) -> dict`
  - `is_paused(control: dict, now: datetime) -> bool`
  - 暂停期间 `loop.watch` 把状态写成 `status="paused"`、不运行新的一轮，恢复后按原来的 `next_run_at` 继续。

- [ ] **Step 1: 写失败测试**

```python
# tests/worker/test_control.py
from datetime import datetime, timedelta, timezone

from auto_app.worker import control, loop


def test_pause_until_expires_by_itself(tmp_path):
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    control.write_control(db, paused=True, paused_until=now + timedelta(hours=2), reason="busy week", now=now)
    saved = control.read_control(db)
    assert saved["reason"] == "busy week"
    assert control.is_paused(saved, now) and not control.is_paused(saved, now + timedelta(hours=3))
    control.write_control(db, paused=False, now=now)
    assert not control.is_paused(control.read_control(db), now)


def test_a_paused_worker_waits_and_resumes(tmp_path):
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    control.write_control(db, paused=True)
    calls, sleeps = [], []
    ticks = iter([False, False, False])  # two paused ticks, then the round; afterwards next(ticks, True) stops

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            control.write_control(db, paused=False)

    loop.watch(db, lambda st: calls.append(1) or {"status": "ok"}, once=False, clock=lambda: 1000.0,
               sleep=sleep, should_stop=lambda: next(ticks, True) if calls else next(ticks))
    assert sleeps[:2] == [30, 30] and calls == [1]
    assert loop.status(db)["status"] == "ok"


def test_status_reports_paused_while_paused(tmp_path):
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    control.write_control(db, paused=True)
    stops = iter([False, True])
    loop.watch(db, lambda st: {"status": "ok"}, clock=lambda: 1000.0, sleep=lambda s: None,
               should_stop=lambda: next(stops))
    assert loop.status(db)["status"] == "paused"
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/worker/test_control.py -q`
Expected: FAIL，`ImportError: cannot import name 'control'`

- [ ] **Step 3: 实现**

```python
# src/auto_app/worker/control.py
"""Pause/resume requests for the worker (docs/linux-worker.md §4).

A separate file from the worker's state: the worker only reads it, so a pause written by a chat tool or the
Discord bot is never overwritten by the running loop.
"""

from datetime import datetime, timezone
from pathlib import Path

from auto_app.ingest.scheduler import _read_state, _write_state

CONTROL = ".worker-control.json"
DEFAULT = {"paused": False, "paused_until": None, "reason": None, "updated_at": None}


def read_control(db_path: Path) -> dict:
    state = _read_state(Path(db_path), CONTROL)
    return {key: state.get(key, value) for key, value in DEFAULT.items()}


def write_control(db_path: Path, *, paused: bool, paused_until: datetime | None = None, reason: str | None = None,
                  now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    value = {"paused": bool(paused), "paused_until": paused_until.isoformat() if paused and paused_until else None,
             "reason": (reason or None) if paused else None, "updated_at": now.isoformat()}
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    _write_state(Path(db_path), value, CONTROL)
    return value


def is_paused(control: dict, now: datetime) -> bool:
    if not control.get("paused"):
        return False
    until = control.get("paused_until")
    return until is None or datetime.fromisoformat(until) > now
```

`src/auto_app/worker/loop.py`：
- import 加 `from datetime import datetime, timezone` 和 `from auto_app.worker.control import is_paused, read_control`。
- 在 `while not should_stop():` 循环体的最开头加：

```python
            if is_paused(read_control(db_path), datetime.fromtimestamp(clock(), timezone.utc)):
                if state.get("status") != "paused":
                    state["status"] = "paused"
                    _write_state(db_path, state, STATE)
                sleep(30)
                continue
```

- [ ] **Step 4: 运行，确认通过；再跑 worker 测试**

Run: `uv run pytest tests/worker -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准时）**

```bash
git add src/auto_app/worker/control.py src/auto_app/worker/loop.py tests/worker/test_control.py
git commit -m "feat(worker): pause and resume through a separate control file

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: 控制服务和 MCP 控制工具

**Files:**
- Create: `src/auto_app/agent/control.py`
- Modify: `src/auto_app/screen/rules.py`（从 `load_rules` 拆出 `rules_from_dict`）、`src/auto_app/agent/mcp_server.py`（`control_tools=True` 时注册工具）、`src/auto_app/agent/cli.py`（`--control-tools`）
- Test: `tests/agent/test_control.py`

**Interfaces:**
- Consumes: Task 5 的 `read_control` / `write_control`；P0 的 `load_facts`、`screen`、`pending`、`loop.status`、`scheduler.status`；现有的 `Review`。
- Produces:
  - `rules_from_dict(data: dict, *, where: str = "screening rules") -> ScreeningRules`（`load_rules` 改为调用它）
  - `ControlService(db_path)`，方法：
    - `pipeline_status() -> dict`：返回 `{ingest, worker, control}`。ingest 只给状态字段和每个来源的 status/error，不给 mailbox checkpoint。
    - `pause(until: str | None, reason: str | None) -> dict`
    - `resume() -> dict`
    - `screening_rules() -> dict`：返回 `{rules, revision}`
    - `propose_screening_change(changes: dict) -> dict`：返回 `{change_id, base_revision, diff, preview: {kept_before, kept_after, newly_excluded, newly_kept, examples}}`
    - `apply_screening_change(change_id: str, expected_revision: str) -> dict`：返回 `{revision}`
    - `record_filter_feedback(job_id: int, filter: str, should_recommend: bool, reason: str | None) -> dict`
    - `digest(limit: int = 20) -> dict`：返回 `{jobs, more}`
  - MCP 工具（只在 `--control-tools` 时注册）：`get_pipeline_status`、`pause_pipeline`、`resume_pipeline`、`get_screening_rules`、`propose_screening_change`、`apply_screening_change`、`record_filter_feedback`、`get_digest`。

- [ ] **Step 1: 写失败测试**

```python
# tests/agent/test_control.py
"""Control tools: status, pause, previewed rule changes applied only with the matching revision, feedback."""

import json

import pytest
from mcp import Client
from sqlalchemy import insert

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import screen_jobs
from tests.imported_jobs import ingest, job_id_of, make_store, record


@pytest.fixture
def db(tmp_path):
    store = make_store(tmp_path)
    # "Account Executive" is excluded only as non-CS ("Product Manager" would also hit the senior rule via "manager").
    ingest(store, [record("101", "Software Engineer, New Grad"), record("102", "Account Executive")])
    screen_jobs(store, ScreeningRules())
    store.engine.dispose()
    return tmp_path / "data" / "auto_app.db"


def service(db):
    from auto_app.agent.control import ControlService

    return ControlService(db)


def test_pause_resume_and_status(db):
    control = service(db)
    paused = control.pause("2099-01-01T00:00:00+00:00", "vacation")
    assert paused["paused"] is True and control.pipeline_status()["control"]["reason"] == "vacation"
    assert control.resume()["paused"] is False
    status = control.pipeline_status()
    assert set(status) == {"ingest", "worker", "control"} and "checkpoint" not in json.dumps(status)


def test_rule_changes_are_previewed_then_applied_only_at_the_same_revision(db):
    control = service(db)
    before = control.screening_rules()
    proposal = control.propose_screening_change({"exclude_non_cs": False})
    assert proposal["diff"] == {"exclude_non_cs": [True, False]}
    assert proposal["preview"]["newly_kept"] == 1 and proposal["preview"]["examples"]["newly_kept"][0]["title"] == "Account Executive"
    with pytest.raises(ValueError, match="revision"):
        control.apply_screening_change(proposal["change_id"], "not-the-revision")
    applied = control.apply_screening_change(proposal["change_id"], before["revision"])
    assert applied["revision"] != before["revision"]
    assert control.screening_rules()["rules"]["exclude_non_cs"] is False
    with pytest.raises(ValueError, match="change"):
        control.apply_screening_change(proposal["change_id"], applied["revision"])  # used once


def test_invalid_rule_changes_are_rejected_before_any_preview(db):
    with pytest.raises(ValueError, match="unknown keys"):
        service(db).propose_screening_change({"exclude_everything": True})


def test_filter_feedback_needs_a_verdict_of_that_filter(db):
    control = service(db)
    store = make_store(db.parent.parent)
    job = job_id_of(store, "Software Engineer, New Grad")
    with store.engine.begin() as conn:
        conn.execute(insert(T.filter_versions).values(filter_version="v1", filter="ng", command="x", created_at=utcnow()))
        conn.execute(insert(T.judgments).values(job_id=job, filter_version="v1", filter="ng", status="ok",
                                                recommend=True, reason="fits", created_at=utcnow()))
    store.engine.dispose()
    result = control.record_filter_feedback(job, "ng", False, "not CS enough")
    assert result["label"] is False
    with pytest.raises(ValueError, match="didn't judge"):
        control.record_filter_feedback(job, "other", True, None)


async def test_control_tools_are_registered_only_when_asked(db):
    from auto_app.agent.mcp_server import create_mcp_server

    async with Client(create_mcp_server(db)) as client:
        assert "pause_pipeline" not in {t.name for t in (await client.list_tools()).tools}
    async with Client(create_mcp_server(db, control_tools=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"get_pipeline_status", "pause_pipeline", "resume_pipeline", "get_screening_rules",
                "propose_screening_change", "apply_screening_change", "record_filter_feedback", "get_digest"} <= names
        status = await client.call_tool("get_pipeline_status", {})
        assert not status.is_error
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/agent/test_control.py -q`
Expected: FAIL，`ModuleNotFoundError: auto_app.agent.control`

- [ ] **Step 3: 实现**

`src/auto_app/screen/rules.py`：把 `load_rules` 拆成两部分：

```python
def load_rules(path: Path | None) -> ScreeningRules:
    """$AUTO_APP_HOME/config/screening.json; a missing file means the defaults."""
    if path is None or not Path(path).exists():
        return ScreeningRules()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read screening rules {path}: {exc}") from exc
    return rules_from_dict(data, where=f"screening rules {path}")


def rules_from_dict(data, *, where: str = "screening rules") -> ScreeningRules:
    if not isinstance(data, dict):
        raise ValueError(f"Cannot read {where}: expected an object")
    # … 原 load_rules 里从 `unknown = set(data) - …` 到 `return ScreeningRules(**values)` 的校验逻辑原样移到这里，
    #     报错信息里的 f"Cannot read screening rules {path}: …" 改为 f"Cannot read {where}: …" …
```

```python
# src/auto_app/agent/control.py
"""Pipeline control for chat tools and the Discord bot (docs/remote-mcp.md, "Control tools").

Writes are narrow and checked here, not in prompts: pausing writes the worker's control file; rule changes
are proposed with a preview, then applied only with the same change id and the unchanged file revision.
"""

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

INGEST_FIELDS = ("status", "running", "last_run_at", "last_finished_at", "next_run_at", "consecutive_failures")


class ControlError(ValueError):
    pass


class ControlService:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.home = self.db_path.parent.parent if self.db_path.parent.name == "data" else self.db_path.parent
        self.rules_path = self.home / "config" / "screening.json"
        self.proposals = self.home / "config" / ".screening-proposals"

    def _store(self):
        from auto_app.core.db.store import DbStore
        from auto_app.settings import Paths

        return DbStore(self.db_path, Paths(home=self.home), migrate=False)

    # ---- status and pause -------------------------------------------------------------------

    def pipeline_status(self) -> dict:
        from auto_app.ingest import scheduler
        from auto_app.worker import loop
        from auto_app.worker.control import read_control

        ingest = scheduler.status(self.db_path)
        sources = ((ingest.get("last_report") or {}).get("sources") or {})
        return {"ingest": {**{k: ingest.get(k) for k in INGEST_FIELDS},
                           "sources": {name: {"status": r.get("status"), "error": r.get("error")}
                                       for name, r in sources.items()}},
                "worker": {k: v for k, v in loop.status(self.db_path).items() if k != "alerts"},
                "control": read_control(self.db_path)}

    def pause(self, until: str | None = None, reason: str | None = None) -> dict:
        from auto_app.worker.control import write_control

        when = None
        if until:
            when = datetime.fromisoformat(until)
            when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            if when <= datetime.now(timezone.utc):
                raise ControlError("until must be in the future")
        return write_control(self.db_path, paused=True, paused_until=when, reason=(reason or "")[:200] or None)

    def resume(self) -> dict:
        from auto_app.worker.control import write_control

        return write_control(self.db_path, paused=False)

    # ---- screening rules ---------------------------------------------------------------------

    def _revision(self) -> str:
        return (hashlib.sha256(self.rules_path.read_bytes()).hexdigest()[:16]
                if self.rules_path.exists() else "default")

    def screening_rules(self) -> dict:
        from auto_app.screen.rules import load_rules

        return {"rules": asdict(load_rules(self.rules_path)), "revision": self._revision()}

    def propose_screening_change(self, changes: dict) -> dict:
        from auto_app.screen.rules import rules_from_dict, screen
        from auto_app.screen.store import load_facts

        if not isinstance(changes, dict) or not changes:
            raise ControlError("changes must be a nonempty object")
        current = self.screening_rules()
        merged = {**current["rules"], **changes}
        proposed = rules_from_dict({k: list(v) if isinstance(v, tuple) else v for k, v in merged.items()},
                                   where="the proposed screening rules")
        before = rules_from_dict({k: list(v) if isinstance(v, tuple) else v for k, v in current["rules"].items()})
        store = self._store()
        try:
            facts = load_facts(store)
        finally:
            store.engine.dispose()
        newly_excluded, newly_kept, kept_before, kept_after = [], [], 0, 0
        for fact in facts:
            old, new = screen(fact, before).outcome, screen(fact, proposed).outcome
            kept_before += old == "kept"
            kept_after += new == "kept"
            if old != new:
                (newly_kept if new == "kept" else newly_excluded).append(
                    {"job_id": fact.job_id, "title": " / ".join(fact.titles)})
        change_id = uuid4().hex
        self.proposals.mkdir(parents=True, exist_ok=True)
        (self.proposals / f"{change_id}.json").write_text(json.dumps(
            {"base_revision": current["revision"], "rules": asdict(proposed),
             "created_at": datetime.now(timezone.utc).isoformat()}))
        diff = {key: [current["rules"][key], value] for key, value in asdict(proposed).items()
                if current["rules"][key] != value}
        return {"change_id": change_id, "base_revision": current["revision"], "diff": diff,
                "preview": {"kept_before": kept_before, "kept_after": kept_after,
                            "newly_excluded": len(newly_excluded), "newly_kept": len(newly_kept),
                            "examples": {"newly_excluded": newly_excluded[:10], "newly_kept": newly_kept[:10]}}}

    def apply_screening_change(self, change_id: str, expected_revision: str) -> dict:
        if not isinstance(change_id, str) or not change_id.isalnum():
            raise ControlError("unknown change id")
        path = self.proposals / f"{change_id}.json"
        if not path.exists():
            raise ControlError("unknown or already applied change id")
        proposal = json.loads(path.read_text())
        if expected_revision != proposal["base_revision"] or self._revision() != proposal["base_revision"]:
            raise ControlError("the rules file changed since this proposal (revision mismatch); propose again")
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.rules_path.with_name(self.rules_path.name + ".tmp")
        temporary.write_text(json.dumps(proposal["rules"], indent=2) + "\n")
        os.replace(temporary, self.rules_path)
        path.unlink()
        return {"revision": self._revision(), "rules": proposal["rules"],
                "note": "the worker applies the new rules at its next round"}

    # ---- feedback and digest -----------------------------------------------------------------

    def record_filter_feedback(self, job_id: int, filter: str, should_recommend: bool,
                               reason: str | None = None) -> dict:
        from auto_app.select.review import Review

        store = self._store()
        try:
            latest = {row["filter"]: row for row in store.judgment_rows(job_id=job_id) if row["status"] != "error"}
            if filter not in latest:
                raise ControlError(f"{filter} didn't judge job #{job_id}")
            review = Review(store, [SimpleNamespace(name=name, version=row["filter_version"])
                                    for name, row in latest.items()])
            review.label(job_id, filter, bool(should_recommend), reason)
            return {"job_id": job_id, "filter": filter, "label": bool(should_recommend)}
        finally:
            store.engine.dispose()

    def digest(self, limit: int = 20) -> dict:
        from auto_app.worker import loop
        from auto_app.worker.digest import pending

        since = loop.status(self.db_path).get("notify_since")
        store = self._store()
        try:
            jobs, more = pending(store, max(1, min(limit, 50)),
                                 datetime.fromisoformat(since) if since else None)
        finally:
            store.engine.dispose()
        keep = ("job_id", "title", "company", "locations", "posted_at", "sponsorship", "recommended", "reason",
                "title_only", "link")
        return {"jobs": [{k: j.get(k) for k in keep} for j in jobs], "more": more}
```

`src/auto_app/agent/mcp_server.py`：在 `return server` 之前加：

```python
    if control_tools:
        from auto_app.agent.control import ControlError, ControlService

        control = ControlService(db_path)
        write = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                open_world_hint=False)

        def control_call(operation, *args, **kwargs):
            try:
                return operation(*args, **kwargs)
            except (ControlError, ValueError) as exc:
                raise ToolError(str(exc)) from exc

        @server.tool(annotations=read_annotations)
        def get_pipeline_status() -> dict[str, Any]:
            """Feed worker and screening worker: last/next run, failures, per-source status, pause state."""
            return control_call(control.pipeline_status)

        @server.tool(annotations=write)
        def pause_pipeline(until: str | None = None, reason: str | None = None) -> dict[str, Any]:
            """Pause the screening/notification worker, until an ISO time or until resumed. Feeds keep importing."""
            return control_call(control.pause, until, reason)

        @server.tool(annotations=write)
        def resume_pipeline() -> dict[str, Any]:
            """Resume a paused worker; it continues at its saved schedule."""
            return control_call(control.resume)

        @server.tool(annotations=read_annotations)
        def get_screening_rules() -> dict[str, Any]:
            """The deterministic screening rules in effect and their revision."""
            return control_call(control.screening_rules)

        @server.tool(annotations=write)
        def propose_screening_change(changes: dict[str, Any]) -> dict[str, Any]:
            """Preview a rule change: the diff and how many active jobs become kept/excluded. Writes nothing.

            Show the preview to the user; apply only after they confirm, with apply_screening_change."""
            return control_call(control.propose_screening_change, changes)

        @server.tool(annotations=write)
        def apply_screening_change(change_id: str, expected_revision: str) -> dict[str, Any]:
            """Apply a confirmed proposal; refused if the rules changed since it was proposed."""
            return control_call(control.apply_screening_change, change_id, expected_revision)

        @server.tool(annotations=write)
        def record_filter_feedback(job_id: Annotated[int, Field(strict=True, ge=1)], filter: str,
                                   should_recommend: bool, reason: str | None = None) -> dict[str, Any]:
            """Record the user's label for one filter's verdict on a job (training feedback, not a decision)."""
            return control_call(control.record_filter_feedback, job_id, filter, should_recommend, reason)

        @server.tool(annotations=read_annotations)
        def get_digest(limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20) -> dict[str, Any]:
            """The same new-job digest Discord receives (sponsorship first), without marking anything notified."""
            return control_call(control.digest, limit)
```

`src/auto_app/agent/cli.py`：`serve` 加 `serve.add_argument("--control-tools", action="store_true", help="Also expose pause/resume, rule changes, feedback and digest tools.")`，并把 `control_tools=args.control_tools` 传给 `create_mcp_server`。

- [ ] **Step 4: 运行，确认通过；再跑 agent 和 screen 测试**

Run: `uv run pytest tests/agent tests/screen -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准时）**

```bash
git add src/auto_app/agent src/auto_app/screen/rules.py tests/agent/test_control.py
git commit -m "feat(agent): pipeline control, previewed rule changes, feedback and digest tools

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Discord bot（按钮 + 斜杠命令，不含 AI）

**Files:**
- Modify: `pyproject.toml`、`uv.lock`（`uv add "discord.py>=2.4,<3"`）
- Modify: `src/auto_app/worker/digest.py`（拆出 `batches`、`mark_notified`，`send_digest` 改为调用它们）
- Create: `src/auto_app/bot/__init__.py`、`src/auto_app/bot/actions.py`、`src/auto_app/bot/app.py`、`src/auto_app/bot/cli.py`、`deploy/auto-app-bot.service`
- Modify: `src/auto_app/cli.py`（注册 `bot`）、`tests/test_cli_structure.py`（`"bot": ["run"]`）
- Test: `tests/worker/test_digest.py`（batches）、`tests/bot/__init__.py`、`tests/bot/test_actions.py`

**Interfaces:**
- Produces:
  - `digest.batches(store, *, limit, since, now) -> tuple[list[list[dict]], int]`
  - `digest.mark_notified(store, jobs: list[dict], now) -> None`
  - `bot.actions`：
    - `ACTIONS = {"want": "want_to_apply", "skip": "not_applied", "applied": "applied"}`
    - `custom_id(job_id, action) -> str`、`parse_custom_id(value) -> tuple[int, str] | None`
    - `BotSettings.from_env() -> BotSettings`，字段：`token`、`owner_id`、`guild_id`、`channel_id`、`db_path`
    - `record_action(db_path, user_id, owner_id, job_id, action) -> str`：返回给用户看的消息
    - `jobs_summary(db_path, *, sponsorship=None, query=None, limit=10) -> list[dict]`
  - 环境变量（`runtime/credentials/bot.env`，权限 600）：`AUTO_APP_DISCORD_BOT_TOKEN`、`AUTO_APP_DISCORD_OWNER_ID`、`AUTO_APP_DISCORD_GUILD_ID`、`AUTO_APP_DISCORD_CHANNEL_ID`
  - CLI：`auto-app bot run --db PATH`

bot 模式下，worker 用 `--steps screen,enrich` 运行，不推送。webhook 只保留给告警。bot 每 5 分钟推送一次摘要，`notify_since` 读取 worker 状态里的值。

- [ ] **Step 1: 加依赖**

Run: `uv add "discord.py>=2.4,<3"`
Expected：`pyproject.toml` 和 `uv.lock` 更新；`uv run python -c "import discord; print(discord.__version__)"` 输出 2.x。

- [ ] **Step 2: 写失败测试**

追加到 `tests/worker/test_digest.py`：

```python
def test_batches_split_by_five_and_mark_only_what_is_passed(store):
    from auto_app.worker.digest import batches, mark_notified

    screened(store, [(f"Engineer {i}", "offers", True) for i in range(7)])
    plan, more = batches(store, limit=30, since=None, now=NOW)
    assert [len(b) for b in plan] == [5, 2] and more == 0
    mark_notified(store, plan[0], NOW)
    assert len(notified(store)) == 5
```

```python
# tests/bot/test_actions.py
"""The bot's decisions: only the owner, only known actions, the same service as chat tools."""

import pytest

from auto_app.bot.actions import ACTIONS, BotSettings, custom_id, parse_custom_id, record_action
from tests.imported_jobs import ingest, job_id_of, make_store, record

OWNER = 1234567890


@pytest.fixture
def db(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Software Engineer, New Grad")])
    job = job_id_of(store, "Software Engineer, New Grad")
    store.engine.dispose()
    return tmp_path / "data" / "auto_app.db", job


def test_custom_ids_round_trip_and_reject_anything_else():
    assert parse_custom_id(custom_id(41, "want")) == (41, "want")
    for bad in ("job:41:delete", "job:x:want", "other:41:want", "job:41:want:extra"):
        assert parse_custom_id(bad) is None


def test_only_the_owner_can_record_decisions(db):
    path, job = db
    refused = record_action(path, 999, OWNER, job, "want")
    assert "only" in refused.lower()
    store = make_store(path.parent.parent)
    assert store.job(job)["decision"] is None
    store.engine.dispose()
    assert "want_to_apply" in record_action(path, OWNER, OWNER, job, "want")
    store = make_store(path.parent.parent)
    assert store.job(job)["decision"] == "want_to_apply"
    store.engine.dispose()


def test_settings_require_every_value(monkeypatch, tmp_path):
    for key in ("AUTO_APP_DISCORD_BOT_TOKEN", "AUTO_APP_DISCORD_OWNER_ID", "AUTO_APP_DISCORD_GUILD_ID",
                "AUTO_APP_DISCORD_CHANNEL_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="AUTO_APP_DISCORD_BOT_TOKEN"):
        BotSettings.from_env(tmp_path / "x.db")
    monkeypatch.setenv("AUTO_APP_DISCORD_BOT_TOKEN", "abc.def.ghi")
    monkeypatch.setenv("AUTO_APP_DISCORD_OWNER_ID", "123")
    monkeypatch.setenv("AUTO_APP_DISCORD_GUILD_ID", "456")
    monkeypatch.setenv("AUTO_APP_DISCORD_CHANNEL_ID", "not-a-number")
    with pytest.raises(ValueError, match="CHANNEL_ID"):
        BotSettings.from_env(tmp_path / "x.db")


def test_every_action_maps_to_a_manual_status():
    assert set(ACTIONS.values()) == {"want_to_apply", "not_applied", "applied"}


def test_the_discord_button_uses_the_same_custom_id():
    from auto_app.bot.app import JobButton

    button = JobButton(41, "applied")
    assert button.item.custom_id == "job:41:applied"
```

- [ ] **Step 3: 运行，确认失败**

Run: `uv run pytest tests/bot tests/worker/test_digest.py -q`
Expected: FAIL，`ModuleNotFoundError: auto_app.bot` / `cannot import name 'batches'`

- [ ] **Step 4: 实现**

`src/auto_app/worker/digest.py`：把 `send_digest` 里的分批逻辑拆出来：

```python
def batches(store, *, limit: int, since: datetime | None, now: datetime) -> tuple[list[list[dict]], int]:
    """Pending jobs in Discord-sized messages (≤ MAX_EMBEDS, one embed per URL per message)."""
    jobs, more = pending(store, limit, since, now)
    out, i = [], 0
    while i < len(jobs):
        batch, links = [], set()
        for job in jobs[i:i + MAX_EMBEDS]:
            if job.get("link") and job["link"] in links:
                break  # Discord shows only the first embed per URL in one message
            batch.append(job)
            links.add(job.get("link"))
        out.append(batch)
        i += len(batch)
    return out, more


def mark_notified(store, jobs: list[dict], now: datetime) -> None:
    store.set_notified([job["job_id"] for job in jobs], now)


def send_digest(store, discord, *, limit: int = 30, since: datetime | None = None,
                now: datetime | None = None) -> dict:
    now = now or utcnow()
    plan, more = batches(store, limit=limit, since=since, now=now)
    total = sum(len(b) for b in plan)
    sent = 0
    for index, batch in enumerate(plan):
        header = f"**{total + more} new job(s)** — visa sponsorship first" if index == 0 else None
        try:
            discord.post(header, [embed(job) for job in batch])
        except DiscordError as exc:
            return {"sent": sent, "waiting": total - sent + more, "error": redact_webhook_secrets(str(exc))}
        mark_notified(store, batch, now)
        sent += len(batch)
    if more:
        try:
            discord.post(f"…and {more} more — they come in the next digest, or ask in chat.")
        except DiscordError:
            pass
    return {"sent": sent, "waiting": more}
```

```python
# src/auto_app/bot/__init__.py
"""The Discord bot: digests with decision buttons, and slash commands — no model calls (docs/discord-bot.md)."""
```

```python
# src/auto_app/bot/actions.py
"""What the bot's buttons and commands do, without Discord: testable, and the same services as chat tools."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

ACTIONS = {"want": "want_to_apply", "skip": "not_applied", "applied": "applied"}
LABELS = {"want": "Want to apply", "skip": "Skip", "applied": "Applied"}
CUSTOM_ID = re.compile(r"job:([0-9]{1,12}):(want|skip|applied)")
ENV = ("AUTO_APP_DISCORD_BOT_TOKEN", "AUTO_APP_DISCORD_OWNER_ID", "AUTO_APP_DISCORD_GUILD_ID",
       "AUTO_APP_DISCORD_CHANNEL_ID")


def custom_id(job_id: int, action: str) -> str:
    return f"job:{int(job_id)}:{action}"


def parse_custom_id(value: str) -> tuple[int, str] | None:
    match = CUSTOM_ID.fullmatch(value or "")
    return (int(match[1]), match[2]) if match else None


@dataclass(frozen=True)
class BotSettings:
    token: str
    owner_id: int
    guild_id: int
    channel_id: int
    db_path: Path

    @classmethod
    def from_env(cls, db_path: Path) -> "BotSettings":
        values = {}
        for key in ENV:
            value = os.environ.get(key, "").strip()
            if not value:
                raise ValueError(f"{key} is not set (put it in runtime/credentials/bot.env)")
            values[key] = value
        ids = {}
        for key in ENV[1:]:
            if not values[key].isdecimal():
                raise ValueError(f"{key} must be a Discord numeric id")
            ids[key] = int(values[key])
        return cls(token=values[ENV[0]], owner_id=ids[ENV[1]], guild_id=ids[ENV[2]], channel_id=ids[ENV[3]],
                   db_path=Path(db_path))


def record_action(db_path: Path, user_id: int, owner_id: int, job_id: int, action: str) -> str:
    """The owner's decision for one job, through the same service the chat tools use."""
    if user_id != owner_id:
        return "Only the owner of this job search can record decisions."
    if action not in ACTIONS:
        return "Unknown action."
    from auto_app.agent.service import AgentError, AgentService

    try:
        result = AgentService(db_path).record_application_status(job_id, ACTIONS[action],
                                                                 reason="via Discord")
    except (AgentError, ValueError) as exc:
        return f"Not recorded: {exc}"
    return f"Job #{job_id}: {ACTIONS[action]}" + ("" if result["changed"] else " (already)")


def jobs_summary(db_path: Path, *, sponsorship: str | None = None, query: str | None = None,
                 limit: int = 10) -> list[dict]:
    from auto_app.query.service import QueryService

    with QueryService(db_path) as service:
        page = service.jobs(query=query, sponsorship=sponsorship, limit=max(1, min(limit, 10)))
    return [{"job_id": j["job_id"], "title": j.get("title"), "company": j.get("company"),
             "state": j.get("state"), "url": j.get("canonical_url"),
             "sponsorship": (j.get("screening") or {}).get("signals", {}).get("sponsorship", "unknown")}
            for j in page["items"]]
```

```python
# src/auto_app/bot/app.py
"""discord.py wiring: persistent decision buttons, guild slash commands, and the digest loop.

Every database call runs in a worker thread; the bot never blocks its event loop on SQLite.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import tasks

from auto_app.bot.actions import ACTIONS, LABELS, BotSettings, custom_id, jobs_summary, record_action
from auto_app.core.models import utcnow

log = logging.getLogger(__name__)
STYLES = {"want": discord.ButtonStyle.success, "skip": discord.ButtonStyle.secondary,
          "applied": discord.ButtonStyle.primary}
SETTINGS: BotSettings | None = None  # set by run(); DynamicItem callbacks need it


class JobButton(discord.ui.DynamicItem[discord.ui.Button], template=r"job:(?P<job>[0-9]{1,12}):(?P<action>want|skip|applied)"):
    def __init__(self, job_id: int, action: str, row: int | None = None):
        super().__init__(discord.ui.Button(label=LABELS[action], style=STYLES[action],
                                           custom_id=custom_id(job_id, action), row=row))
        self.job_id, self.action = job_id, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["job"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        message = await asyncio.to_thread(record_action, SETTINGS.db_path, interaction.user.id, SETTINGS.owner_id,
                                          self.job_id, self.action)
        await interaction.response.send_message(message, ephemeral=True)


class JobBot(discord.Client):
    def __init__(self, settings: BotSettings):
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.guild = discord.Object(id=settings.guild_id)
        _commands(self)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(JobButton)
        await self.tree.sync(guild=self.guild)
        self.digest.start()

    @tasks.loop(minutes=5)
    async def digest(self) -> None:
        try:
            await self.post_digest()
        except Exception:  # keep the loop alive; the next tick retries
            log.exception("digest failed")

    async def post_digest(self) -> None:
        from datetime import datetime

        from auto_app.core.db.store import DbStore
        from auto_app.settings import get_paths
        from auto_app.worker import loop
        from auto_app.worker.digest import batches, embed, mark_notified

        since = loop.status(self.settings.db_path).get("notify_since")
        if not since:
            return  # the worker hasn't run yet: no cutoff, so nothing is announced
        now = utcnow()

        def plan():
            store = DbStore(self.settings.db_path, get_paths(), migrate=False)
            try:
                return batches(store, limit=30, since=datetime.fromisoformat(since), now=now)
            finally:
                store.engine.dispose()

        def mark(batch):
            store = DbStore(self.settings.db_path, get_paths(), migrate=False)
            try:
                mark_notified(store, batch, now)
            finally:
                store.engine.dispose()

        plan_batches, more = await asyncio.to_thread(plan)
        if not plan_batches:
            return
        channel = self.get_channel(self.settings.channel_id) or await self.fetch_channel(self.settings.channel_id)
        total = sum(len(b) for b in plan_batches) + more
        for index, batch in enumerate(plan_batches):
            view = discord.ui.View(timeout=None)
            for row, job in enumerate(batch):
                for action in ACTIONS:
                    view.add_item(JobButton(job["job_id"], action, row=row))
            await channel.send(content=f"**{total} new job(s)** — visa sponsorship first" if index == 0 else None,
                               embeds=[discord.Embed.from_dict(embed(job)) for job in batch], view=view,
                               allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(mark, batch)


def _owner(interaction: discord.Interaction, settings: BotSettings) -> bool:
    return interaction.user.id == settings.owner_id


def _commands(bot: JobBot) -> None:
    settings, guild = bot.settings, bot.guild

    @bot.tree.command(name="jobs", description="Open jobs (rules kept), optionally by sponsorship or keyword",
                      guild=guild)
    @app_commands.choices(sponsorship=[app_commands.Choice(name=v, value=v) for v in ("offers", "unknown", "no")])
    async def jobs(interaction: discord.Interaction, sponsorship: app_commands.Choice[str] | None = None,
                   query: str | None = None) -> None:
        if not _owner(interaction, settings):
            return await interaction.response.send_message("Owner only.", ephemeral=True)
        rows = await asyncio.to_thread(jobs_summary, settings.db_path,
                                       sponsorship=sponsorship.value if sponsorship else None, query=query)
        text = "\n".join(f"#{r['job_id']} {r['title']} — {r['company']} · {r['sponsorship']} · {r['state']}"
                         for r in rows) or "No matching jobs."
        await interaction.response.send_message(text[:1900], ephemeral=True)

    @bot.tree.command(name="status", description="Feed and worker status", guild=guild)
    async def status(interaction: discord.Interaction) -> None:
        if not _owner(interaction, settings):
            return await interaction.response.send_message("Owner only.", ephemeral=True)
        from auto_app.agent.control import ControlService

        s = await asyncio.to_thread(ControlService(settings.db_path).pipeline_status)
        text = (f"ingest: {s['ingest']['status']} (next {s['ingest']['next_run_at']})\n"
                f"worker: {s['worker'].get('status')} (next {s['worker'].get('next_run_at')}, "
                f"failures {s['worker'].get('consecutive_failures')})\n"
                f"paused: {s['control']['paused']} {s['control'].get('paused_until') or ''}")
        await interaction.response.send_message(text, ephemeral=True)

    @bot.tree.command(name="pause", description="Pause the worker for N hours (0 = until resumed)", guild=guild)
    async def pause(interaction: discord.Interaction, hours: app_commands.Range[int, 0, 720] = 0) -> None:
        if not _owner(interaction, settings):
            return await interaction.response.send_message("Owner only.", ephemeral=True)
        from datetime import timedelta

        from auto_app.agent.control import ControlService

        until = (utcnow() + timedelta(hours=hours)).isoformat() if hours else None
        result = await asyncio.to_thread(ControlService(settings.db_path).pause, until, "via Discord")
        await interaction.response.send_message(f"Paused until {result['paused_until'] or 'resumed'}.", ephemeral=True)

    @bot.tree.command(name="resume", description="Resume the worker", guild=guild)
    async def resume(interaction: discord.Interaction) -> None:
        if not _owner(interaction, settings):
            return await interaction.response.send_message("Owner only.", ephemeral=True)
        from auto_app.agent.control import ControlService

        await asyncio.to_thread(ControlService(settings.db_path).resume)
        await interaction.response.send_message("Resumed.", ephemeral=True)


def run(settings: BotSettings) -> None:
    global SETTINGS
    SETTINGS = settings
    JobBot(settings).run(settings.token, log_handler=None)
```

```python
# src/auto_app/bot/cli.py
"""`auto-app bot run`: the Discord bot (docs/discord-bot.md)."""

from pathlib import Path
from typing import Annotated

import typer

from auto_app.settings import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False, help="The Discord bot: digests with buttons, slash commands.")


@app.command("run")
def run(db: Annotated[Path | None, typer.Option("--db", help="Database file; defaults to AUTO_APP_HOME/data/auto_app.db.")] = None) -> None:
    """Run the bot until stopped (systemd runs this)."""
    from auto_app.bot.actions import BotSettings

    try:
        settings = BotSettings.from_env(db or get_paths().db)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2)
    from auto_app.bot.app import run as run_bot

    run_bot(settings)
```

`src/auto_app/cli.py`：import `from auto_app.bot.cli import app as bot_app`，并 `app.add_typer(bot_app, name="bot")`。`main()` 回调里不加载 `.env` 的判断改为 `if ctx.invoked_subcommand not in ("worker", "bot"):`。

```ini
# deploy/auto-app-bot.service
[Unit]
Description=Job Search Discord bot (digests with buttons, slash commands)
Wants=network-online.target
After=network-online.target auto-app-worker.service

[Service]
Type=simple
WorkingDirectory=%h/Desktop/job-search/auto-application
Environment=AUTO_APP_HOME=%h/Desktop/job-search/runtime
EnvironmentFile=%h/Desktop/job-search/runtime/credentials/bot.env
ExecStart=%h/Desktop/job-search/auto-application/.venv/bin/auto-app bot run --db %h/Desktop/job-search/runtime/data/auto_app.db
Restart=on-failure
RestartSec=30
UMask=0077

[Install]
WantedBy=default.target
```

- [ ] **Step 5: 运行，确认通过；再跑全量测试**

Run: `uv run pytest tests/bot tests/worker tests/test_cli_structure.py -q`，然后 `uv run pytest -q`
Expected: PASS

- [ ] **Step 6:「用户」创建 bot 并实测**

1. 在 Discord Developer Portal 新建 Application → Bot，复制 token。OAuth2 URL Generator 勾选 `bot` 和 `applications.commands` scope，权限勾选 Send Messages、Embed Links，用生成的链接把 bot 邀请进自己的服务器。
2. 打开 Discord 的开发者模式，复制自己的用户 ID、服务器 ID、推送频道 ID。
3. 在 Ubuntu 上：

```bash
umask 077
cat > ~/Desktop/job-search/runtime/credentials/bot.env <<'EOF'
AUTO_APP_DISCORD_BOT_TOKEN=<token>
AUTO_APP_DISCORD_OWNER_ID=<你的用户 ID>
AUTO_APP_DISCORD_GUILD_ID=<服务器 ID>
AUTO_APP_DISCORD_CHANNEL_ID=<频道 ID>
EOF
```

4. worker 服务改为 `--steps screen,enrich`，安装并启动 `auto-app-bot.service`。
5. 验收：在 Discord 里依次运行 `/status`、`/jobs sponsorship:offers`；点击推送消息上的"Want to apply"，然后在 Dot 里查同一个岗位，状态应为 `want_to_apply`；用另一个账号点同一个按钮，应被拒绝。

- [ ] **Step 7: Commit（仅在用户批准时）**

```bash
git add pyproject.toml uv.lock src/auto_app/bot src/auto_app/worker/digest.py src/auto_app/cli.py deploy/auto-app-bot.service tests
git commit -m "feat(bot): Discord bot with decision buttons and slash commands

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: 文档、部署和验收

**Files:**
- Create: `docs/remote-mcp.md`、`docs/discord-bot.md`、`docs/updates/2026-10-12-p1-coverage-and-control.md`
- Modify: `docs/linux-worker.md`（加 §6 Boards、§4 的暂停）、`docs/mailbox-linux-setup.md`（IMAP 一节）、`docs/updates/README.md`、`docs/step1-collect.md`（board feeds、IMAP）、`docs/step4-run.md`（bot）、`README.md`、`config/feeds.example.yaml`（board 和 imap 示例，`enabled: false`）、`scripts/deploy_linux.sh`（服务列表加 `auto-app-agent-public.service`、`auto-app-bot.service`）

- [ ] **Step 1: 写文档**

`docs/remote-mcp.md` 必须包含：
- 两个 MCP 实例的区别（loopback 无认证给 OpenAI tunnel，token 认证给公网）。
- token 的生成和轮换（换文件内容后 `systemctl --user restart auto-app-agent-public`）。
- Tailscale Funnel 和 Cloudflare named tunnel 两种方式的命令。
- Grok 连接步骤和 Task 4 Step 5 的实测结果。
- 控制工具清单，以及"propose → 给用户看预演 → 用户确认 → apply"的约定。

`docs/discord-bot.md` 必须包含：创建 bot、bot.env、`/jobs`、`/status`、`/pause`、`/resume`、按钮含义、"只有 owner 能操作"、bot 模式下 worker 用 `--steps screen,enrich`。

`docs/linux-worker.md` §6 "Employer boards" 必须包含：`auto-app worker boards --yaml`、三种 format、`company` 字段、"Greenhouse URL 必须带 `?content=true`"、加入 feeds.yaml 后由 ingest worker 每 30 分钟拉取。

`docs/mailbox-linux-setup.md` 的 IMAP 一节必须包含：Gmail 先开两步验证，再创建应用专用密码；`imap.json` 的格式和 `chmod 600`；推荐的 LinkedIn/Jobright 发件人；只读保证（EXAMINE + BODY.PEEK）。

README：在 Linux worker 一节后面加 "Employer boards"、"IMAP"、"Public MCP and control tools"、"Discord bot" 四小段，每段 3–5 行加一个命令示例，并链接到上面的文档。命令表加 `auto-app worker boards`、`auto-app bot run`。

- [ ] **Step 2: 检查 `--help` 和 README 一致**

Run: `uv run auto-app worker boards --help && uv run auto-app bot --help && uv run auto-app-agent serve --help`
Expected：选项和文档一致

- [ ] **Step 3:「用户」部署并验收**（spec §5 的 P1.1–P1.5）

1. `scripts/deploy_linux.sh home-ubuntu`
2. 在 Ubuntu 上运行 `auto-app worker boards --yaml --top 30`，把结果给用户确认后，挑选要加的条目追加到 `runtime/config/feeds.yaml`。下一轮 ingest 后，`auto-app-query context` 里应出现这些 `board-*` 来源。
3. 配置 IMAP（Task 3 的 feeds 示例）；等一轮后看 `ingest status`，应有 `messages_processed > 0`。
4. 公网 MCP 和 Grok（Task 4 Step 5）。
5. Discord bot（Task 7 Step 6）。
6. 验收标准：
   - 至少 30 家公司的招聘板每天进库，并与仓库里的同一岗位正确去重；
   - 一封 LinkedIn 提醒邮件里的岗位 30 分钟内进库，且邮件仍是未读；
   - Grok 能调用 `get_job_context`，不带 token 返回 401；
   - 在 Discord 点"想投"后，Dot 里查到的状态一致。

   结果记录到 `docs/updates/2026-10-12-p1-coverage-and-control.md`。

- [ ] **Step 4: Commit（仅在用户批准时）**

```bash
git add docs README.md config/feeds.example.yaml scripts/deploy_linux.sh
git commit -m "docs: boards, IMAP, public MCP, control tools and Discord bot

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## 自检记录

- **spec 覆盖**：
  - P1.1 → Task 2。没有沿用"每家公司一个插件"的方案，改为直接读招聘板的列表 API：不需要浏览器，也不需要写 Python 插件，自带 JD。Workday 不在其中，因为它的列表 API 没有 JD，Workday 岗位继续靠 P0 的单条补全。
  - P1.2 → Task 3；P1.3 → Task 4（Grok 的认证方式在 Step 5 实测后决定）；P1.4 → Task 5、6；P1.5 → Task 7。
  - P0 审查结果 → Task 1。
- **类型一致**：以下签名在定义处和使用处一致：
  - `PublicAuth(token, public_url)`、`create_mcp_server(..., auth=None, control_tools=False)`
  - `read_control` / `write_control` / `is_paused`
  - `ControlService` 的各方法
  - `batches(store, *, limit, since, now)`、`mark_notified(store, jobs, now)`
  - `record_action(db_path, user_id, owner_id, job_id, action)`
- **Review Focus**：五条都有对应测试，分别在 Task 2、3、4、5、7。
