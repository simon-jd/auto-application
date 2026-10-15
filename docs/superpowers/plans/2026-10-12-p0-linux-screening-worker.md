# P0：Linux worker（规则筛选 → JD 补全 → Discord 推送）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Linux 上已经收集了一万多个岗位，这个计划让它们真正流到用户面前：先用确定性规则筛一遍，再用雇主的公开 API 补全 JD，补全后再筛一遍，最后把新岗位按签证担保排序推送到 Discord。全程不调用任何模型。

**Architecture:** 新增一个独立的 `auto-app worker` 进程（systemd 用户服务，每 15 分钟跑一轮），和现有的 `auto-app-ingest.service` 共用同一个 SQLite 数据库。新增两张表：`enrichments` 按申请 URL 缓存 JD 补全结果；`job_screenings` 保存每个活跃岗位当前的规则结论。规则结论只是一个"视图"：查询接口和 MCP 默认隐藏被排除的岗位，但从不删除岗位，也不改 `step2`/`decision`。用户自己运行的 `auto-app filter run` 会跳过被规则排除的岗位，省下模型额度。

**Tech Stack:** Python 3.12、uv、SQLAlchemy Core + Alembic、httpx、Typer、pytest（`asyncio_mode = "auto"`）、Discord incoming webhook。

**Spec:** [2026-10-12-roadmap-p0-p3.md](2026-10-12-roadmap-p0-p3.md) §1–§4、§8。

## Global Constraints

- Python 3.12，依赖用 `uv`；不新增第三方依赖（httpx、SQLAlchemy、Typer 都已在 `pyproject.toml` 里）。
- **worker 不调用任何模型或 AI agent**：不用 Claude CLI、Agent SDK，不读 API key，不写筛选器提示词。LLM 筛选和 agent 由用户按额度自己安排（spec §8 #11）。
- 数据库结构只通过 Alembic 迁移修改；新迁移的 `down_revision = 'c2f14e8a903b'`（当前唯一的 head）。
- `src/auto_app/query/` 必须保持只用标准库、只读：`scripts/job_view.py` 用 `python3 -S` 运行它。查询路径不能 import SQLAlchemy，也不能建表或迁移。
- 规则排除是可逆的视图：不删除岗位，不修改 `jobs.step2`、`jobs.decision`、`reviews`、`feedback`。
- 不提交申请，不调用 `apply` 相关代码。
- 密钥不进仓库，也不能被打印、写进日志或出现在报错里：Discord webhook URL 只从环境变量 `AUTO_APP_DISCORD_WEBHOOK_URL` 读取。
- 对外请求的限制：
  - 只访问 Greenhouse/Lever/Ashby/Workday 的公开 JSON API。
  - 同一个 host 两次请求间隔 ≥1 秒；每轮最多补全 200 次请求。
  - `User-Agent: auto-app/1 (personal job search; one request per job)`；不跟随重定向。
- 目标画像（spec §1）：new grad / entry level、泛 CS、不要实习、签证担保优先但不是必须。明确要求公民身份/安全许可/ITAR 的岗位排除（spec §8 #1，待用户确认）。
- 文档规则（CLAUDE.md）：每个功能都要更新 `docs/updates/` 条目和索引、对应的 step 文档、`README.md`。
- 提交规则（CLAUDE.md 写着 "Commit or push only when the user asks."）：每个任务末尾的 commit 步骤**只有在用户批准本计划的提交时才执行**；否则只跑测试，改动留在工作区。提交信息结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 测试命令：`uv run pytest -q`；单个文件：`uv run pytest tests/<path>.py -q`。

## Review Focus

下面五种情况是 spec 隐含的、单个任务正常路径测试不一定覆盖的，也是最可能伤到使用者的。每条对应的测试已经加进负责它的任务里：

1. **仓库 feed 重新导入会把已补全的 JD 清空**。上游某一行变化时，`IngestService` 会用空描述重写这个 posting，状态变回 `pending`。期望：下一轮从 `enrichments` 缓存恢复描述，不再发请求。→ Task 5 `test_reimport_restores_description_from_cache_without_a_request`。
2. **Discord 推送中途失败**。期望：只有 Discord 确认收到的那几批岗位被标记 `notified_at`，其余下一轮重发，不丢、不重复。→ Task 8 `test_a_failed_post_marks_only_the_delivered_jobs`。
3. **ingest worker 正在写库（`database is locked`）**。期望：当前步骤报错，后面的步骤照常运行，下一轮重试。→ Task 9 `test_a_locked_database_in_one_step_does_not_stop_the_others`。
4. **容易误判的标题**。期望："Internal Tools Engineer" 不算实习，"Site Reliability Engineer" 不算非 CS，"Software Engineer - V&V" 不算高级职位，"New Grad (Summer 2027 start)" 不算实习。→ Task 2 `test_entry_level_cs_and_ambiguous_titles_are_kept`。
5. **首次部署把存量岗位一次性刷进 Discord**。期望：第一轮记下 `notify_since`，只推之后新出现的岗位；没有 JD 的岗位先等补全。→ Task 9 `test_first_round_does_not_push_the_backlog`、Task 8 `test_title_only_jobs_wait_for_enrichment`。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `migrations/versions/5b1e0c7d2a91_enrichment_and_screening.py` | 新建 `enrichments`、`job_screenings` 两张表 |
| `src/auto_app/core/db/tables.py` | 两张表的 SQLAlchemy 定义 |
| `src/auto_app/screen/rules.py` | 纯函数规则：`JobFacts` + `ScreeningRules` → `Screening` |
| `src/auto_app/screen/store.py` | 从数据库读事实、写结论：`screen_jobs`、`excluded_job_ids`、`chunks` |
| `src/auto_app/enrich/targets.py` | 申请 URL → 平台 API 地址（`target_for`） |
| `src/auto_app/enrich/fetch.py` | 限速的 HTTP 读取，复用平台解析器（`Fetcher`） |
| `src/auto_app/enrich/service.py` | 选候选、查缓存、写回 posting、退避重试（`enrich_jobs`） |
| `src/auto_app/select/engine.py` | `plan()` 跳过被规则排除的岗位（用户自己运行 `filter run` 时生效） |
| `src/auto_app/query/service.py`、`query/cli.py`、`query/api.py`、`agent/mcp_server.py` | 默认隐藏规则排除的岗位，返回原因和信号，支持 `sponsorship` 过滤，context 返回统计 |
| `src/auto_app/core/discord.py` | Discord webhook 发送（429 重试，不 @ 任何人） |
| `src/auto_app/worker/digest.py` | 推送内容和排序、标记已通知、告警去重 |
| `src/auto_app/worker/round.py` | 一轮：screen → enrich → rescreen → notify，各步骤互相隔离 |
| `src/auto_app/worker/loop.py` | 持久化的循环和状态文件（复用 `ingest/scheduler.py` 的锁和状态读写） |
| `src/auto_app/worker/cli.py` | `auto-app worker run / watch / status` |
| `src/auto_app/ingest/scheduler.py` | 状态文件读写函数加 `suffix` 参数（默认值不变） |
| `deploy/auto-app-worker.service` | systemd 用户服务模板 |
| `config/screening.example.json` | 规则文件模板 |
| `scripts/deploy_linux.sh` | rsync 部署，部署前备份数据库 |
| `docs/linux-worker.md`、`docs/updates/2026-10-12-linux-worker.md`、`docs/updates/README.md`、`docs/step4-run.md`、`README.md` | 文档 |
| `tests/imported_jobs.py` | screen / enrich / worker 测试共用的导入岗位数据 |

---

### Task 1: 新表 `enrichments` 和 `job_screenings`

**Files:**
- Create: `migrations/versions/5b1e0c7d2a91_enrichment_and_screening.py`
- Modify: `src/auto_app/core/db/tables.py`（文件末尾追加）
- Test: `tests/core/test_worker_tables.py`

**Interfaces:**
- Produces:
  - `T.enrichments`，列：`url`（主键）、`platform`、`status`、`attempts`、`next_attempt_at`、`fetched_at`、`data`（JSON）、`error`、`updated_at`。
  - `T.job_screenings`，列：`job_id`（主键）、`rules_version`、`facts_hash`、`outcome`、`reasons`（JSON）、`signals`（JSON）、`screened_at`；索引 `ix_job_screenings_outcome`。

- [ ] **Step 1: 写失败测试**

```python
# tests/core/test_worker_tables.py
"""The Linux worker's tables exist after migrating a new database."""

from sqlalchemy import inspect

from auto_app.core.db.store import DbStore
from auto_app.settings import Paths


def test_enrichment_and_screening_tables_after_migration(tmp_path):
    store = DbStore(tmp_path / "data" / "auto_app.db", Paths(home=tmp_path))
    try:
        insp = inspect(store.engine)
        assert {c["name"] for c in insp.get_columns("enrichments")} == {
            "url", "platform", "status", "attempts", "next_attempt_at", "fetched_at", "data", "error", "updated_at"}
        assert {c["name"] for c in insp.get_columns("job_screenings")} == {
            "job_id", "rules_version", "facts_hash", "outcome", "reasons", "signals", "screened_at"}
        assert any(ix["column_names"] == ["outcome"] for ix in insp.get_indexes("job_screenings"))
    finally:
        store.engine.dispose()
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/core/test_worker_tables.py -q`
Expected: FAIL，`NoSuchTableError: enrichments`

- [ ] **Step 3: 写迁移和表定义**

```python
# migrations/versions/5b1e0c7d2a91_enrichment_and_screening.py
"""Linux worker: JD enrichment cache and deterministic screening verdicts.

Revision ID: 5b1e0c7d2a91
Revises: c2f14e8a903b
"""

from alembic import op
import sqlalchemy as sa

revision = '5b1e0c7d2a91'
down_revision = 'c2f14e8a903b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'enrichments',
        sa.Column('url', sa.Text(), primary_key=True),
        sa.Column('platform', sa.String()),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True)),
        sa.Column('fetched_at', sa.DateTime(timezone=True)),
        sa.Column('data', sa.JSON()),
        sa.Column('error', sa.Text()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'job_screenings',
        sa.Column('job_id', sa.Integer(), primary_key=True),
        sa.Column('rules_version', sa.String(), nullable=False),
        sa.Column('facts_hash', sa.String(), nullable=False),
        sa.Column('outcome', sa.String(), nullable=False),
        sa.Column('reasons', sa.JSON(), nullable=False),
        sa.Column('signals', sa.JSON(), nullable=False),
        sa.Column('screened_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_job_screenings_outcome', 'job_screenings', ['outcome'])


def downgrade():
    op.drop_index('ix_job_screenings_outcome', table_name='job_screenings')
    op.drop_table('job_screenings')
    op.drop_table('enrichments')
```

在 `src/auto_app/core/db/tables.py` 末尾追加：

```python
# ---- Linux worker: JD enrichment and deterministic screening (docs/linux-worker.md) --------------

enrichments = Table(  # one read of an imported job's official record, by the posting's canonical apply URL
    "enrichments", metadata,
    Column("url", Text, primary_key=True),
    Column("platform", String),  # greenhouse | lever | ashby | workday; NULL when no API is known for the URL
    Column("status", String, nullable=False),  # ok | failed | unsupported | gone
    Column("attempts", Integer, nullable=False, default=0),  # failed reads so far
    Column("next_attempt_at", Ts),  # failed only: when a later round may try again
    Column("fetched_at", Ts),
    Column("data", JSON),  # ok: the CommonPosting fields taken from the employer's record
    Column("error", Text),
    Column("updated_at", Ts, nullable=False),
)

job_screenings = Table(  # the rules' current verdict per active job: a view, never a decision
    "job_screenings", metadata,
    Column("job_id", Integer, primary_key=True),  # no FK: rows of deleted/archived jobs are dropped by the next pass
    Column("rules_version", String, nullable=False),
    Column("facts_hash", String, nullable=False),  # screened again when the job's facts change
    Column("outcome", String, nullable=False, index=True),  # kept | excluded
    Column("reasons", JSON, nullable=False),  # e.g. ["internship", "experience_required:5"]
    Column("signals", JSON, nullable=False),  # {"role", "sponsorship", "years_required", "new_grad_title", …}
    Column("screened_at", Ts, nullable=False),
)
```

- [ ] **Step 4: 运行，确认通过；再跑现有数据库测试**

Run: `uv run pytest tests/core -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准提交时）**

```bash
git add migrations/versions/5b1e0c7d2a91_enrichment_and_screening.py src/auto_app/core/db/tables.py tests/core/test_worker_tables.py
git commit -m "feat(db): add enrichment cache and screening verdict tables

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 规则筛选（纯函数）

**Files:**
- Create: `src/auto_app/screen/__init__.py`、`src/auto_app/screen/rules.py`、`config/screening.example.json`
- Modify: `.gitignore`（加一行 `config/screening.json`）
- Test: `tests/screen/__init__.py`（空文件）、`tests/screen/test_rules.py`

**Interfaces:**
- Produces:
  - `JobFacts(job_id: int, titles: tuple[str, ...], employment_types: tuple[str, ...] = (), sources: tuple[str, ...] = (), description: str = "", sponsorship_labels: tuple[str, ...] = (), categories: tuple[str, ...] = (), closed_at_source: bool = False)`，属性 `digest -> str`。
  - `ScreeningRules(exclude_internships=True, exclude_senior=True, exclude_non_cs=True, exclude_citizenship_required=True, max_years_required: int | None = 3, extra_cs_terms: tuple[str, ...] = (), extra_non_cs_terms: tuple[str, ...] = ())`，属性 `version -> str`。
  - `Screening(outcome: str, reasons: tuple[str, ...], signals: dict)`。`signals` 的键：
    - `role`：`cs | non_cs | unknown`
    - `sponsorship`：`offers | no | unknown | citizenship_required`
    - `years_required`：`int | None`
    - `new_grad_title`、`has_description`
  - `screen(facts: JobFacts, rules: ScreeningRules) -> Screening`
  - `years_required(description: str) -> int | None`
  - `load_rules(path: Path | None) -> ScreeningRules`：文件不存在时返回默认值；键名或值不合法时抛 `ValueError`。

- [ ] **Step 1: 写失败测试**

```python
# tests/screen/test_rules.py
"""Deterministic screening: only explicit evidence excludes; sponsorship is a signal."""

import json

import pytest

from auto_app.screen.rules import JobFacts, ScreeningRules, load_rules, screen, years_required

RULES = ScreeningRules()


def facts(title, **kw):
    return JobFacts(job_id=1, titles=(title,), **kw)


@pytest.mark.parametrize("title", [
    "Software Engineer, New Grad", "Software Engineer I", "Data Scientist - Entry Level",
    "Site Reliability Engineer", "Internal Tools Engineer", "Machine Learning Engineer (2027 New Grad)",
    "Software Engineer II", "Associate Software Engineer", "Software Engineer - V&V",
    "Software Engineer, New Grad (Summer 2027 start)", "Field Applications Engineer",
])
def test_entry_level_cs_and_ambiguous_titles_are_kept(title):
    assert screen(facts(title), RULES).outcome == "kept", title


@pytest.mark.parametrize("title, reason", [
    ("Software Engineer Intern", "internship"),
    ("SWE Co-op (Spring)", "internship"),
    ("Summer 2027 Software Engineering Internship", "internship"),
    ("Software Engineer (Summer 2027)", "internship"),
    ("Senior Software Engineer", "senior_title"),
    ("Sr. Data Engineer", "senior_title"),
    ("Staff ML Engineer", "senior_title"),
    ("Software Engineer III", "senior_title"),
    ("Engineering Manager, Payments", "senior_title"),
    ("Mechanical Engineer", "not_cs_role"),
    ("Account Executive", "not_cs_role"),
    ("Product Manager", "not_cs_role"),
    ("ASIC Design Engineer", "not_cs_role"),
    ("Process Engineer", "not_cs_role"),
])
def test_explicit_title_evidence_excludes(title, reason):
    result = screen(facts(title), RULES)
    assert result.outcome == "excluded" and reason in result.reasons, (title, result.reasons)


def test_internship_from_employment_type_and_intern_only_feeds():
    assert "internship" in screen(facts("Software Engineer", employment_types=("internship",)), RULES).reasons
    assert "internship" in screen(facts("Software Engineer", sources=("simplify-internships",)), RULES).reasons
    mixed = facts("Software Engineer", sources=("simplify-internships", "simplify-newgrad"))
    assert screen(mixed, RULES).outcome == "kept"


def test_repository_categories_decide_generic_titles():
    assert screen(facts("Engineer", categories=("Hardware",)), RULES).reasons == ("not_cs_role",)
    kept = screen(facts("Engineer", categories=("Software",)), RULES)
    assert kept.outcome == "kept" and kept.signals["role"] == "cs"
    assert screen(facts("Embedded Software Engineer", categories=("Hardware",)), RULES).outcome == "kept"


@pytest.mark.parametrize("labels, description, expected", [
    (("Offers Sponsorship",), "", "offers"),
    (("Does Not Offer Sponsorship",), "", "no"),
    ((), "Visa sponsorship is available for this role.", "offers"),
    ((), "We are unable to sponsor visas for this position.", "no"),
    ((), "Candidates must be authorized to work in the US without sponsorship.", "no"),
    (("Other",), "Build backend services.", "unknown"),
])
def test_sponsorship_is_a_signal_not_an_exclusion(labels, description, expected):
    result = screen(facts("Software Engineer", sponsorship_labels=labels, description=description), RULES)
    assert result.outcome == "kept" and result.signals["sponsorship"] == expected


@pytest.mark.parametrize("labels, description", [
    (("U.S. Citizenship is Required",), ""),
    ((), "Applicants must be a U.S. citizen."),
    ((), "Requires an active Secret clearance."),
    ((), "Must have the ability to obtain a security clearance."),
    ((), "This position requires access to export-controlled data; candidates must be U.S. persons."),
])
def test_citizenship_or_clearance_requirements_exclude(labels, description):
    result = screen(facts("Software Engineer", sponsorship_labels=labels, description=description), RULES)
    assert result.reasons == ("citizenship_required",)
    assert result.signals["sponsorship"] == "citizenship_required"


@pytest.mark.parametrize("description, years, excluded", [
    ("Requires 5+ years of professional experience.", 5, True),
    ("3+ years of experience preferred.", None, False),
    ("0-2 years of experience with Python.", 0, False),
    ("Bachelor's degree and 2 years of relevant work experience.", 2, False),
    ("4 years of experience in lieu of a degree. 1+ years of software development experience.", 1, False),
])
def test_stated_experience_requirements(description, years, excluded):
    assert years_required(description) == years
    result = screen(facts("Software Engineer", description=description), RULES)
    assert (result.outcome == "excluded") is excluded
    assert result.signals["years_required"] == years


def test_new_grad_titles_are_not_excluded_by_experience_text():
    result = screen(facts("New Grad Software Engineer", description="5+ years of experience required."), RULES)
    assert result.outcome == "kept" and result.signals["years_required"] == 5


def test_closed_at_source_excludes():
    assert screen(facts("Software Engineer", closed_at_source=True), RULES).reasons == ("closed_at_source",)


def test_rules_can_be_turned_off_and_change_the_version(tmp_path):
    relaxed = ScreeningRules(exclude_internships=False, max_years_required=None)
    assert screen(facts("Software Engineer Intern"), relaxed).outcome == "kept"
    assert relaxed.version != RULES.version
    path = tmp_path / "screening.json"
    path.write_text(json.dumps({"exclude_internships": False, "max_years_required": None,
                                "extra_non_cs_terms": ["support engineer"]}))
    loaded = load_rules(path)
    assert loaded == ScreeningRules(exclude_internships=False, max_years_required=None,
                                    extra_non_cs_terms=("support engineer",))
    assert screen(facts("Support Engineer"), loaded).reasons == ("not_cs_role",)


def test_missing_rules_file_means_defaults_and_bad_files_fail_loudly(tmp_path):
    assert load_rules(tmp_path / "missing.json") == ScreeningRules()
    assert load_rules(None) == ScreeningRules()
    bad = tmp_path / "bad.json"
    bad.write_text('{"exclude_interns": true}')
    with pytest.raises(ValueError, match="unknown keys"):
        load_rules(bad)
    bad.write_text('{"max_years_required": "three"}')
    with pytest.raises(ValueError, match="max_years_required"):
        load_rules(bad)
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/screen/test_rules.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'auto_app.screen'`

- [ ] **Step 3: 实现**

```python
# src/auto_app/screen/__init__.py
"""Deterministic job screening for the Linux worker (docs/linux-worker.md §2)."""
```

```python
# src/auto_app/screen/rules.py
"""Deterministic screening for new-grad / entry-level CS jobs (docs/linux-worker.md §2).

A pure function of a job's saved facts and the rules file: no network, no model, no database.
Only explicit evidence excludes; anything uncertain stays "kept" for the user to judge.
Visa sponsorship is a signal for ordering, never an exclusion. The worker stores the verdict as a
view (job_screenings) — never as a decision.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

RULES_VERSION = "1"  # bump when a pattern below changes meaning: every job is screened again

INTERN = re.compile(r"\b(?:intern|interns|internship|internships|co-?op|apprentice|apprenticeship)\b", re.I)
SEASON = re.compile(r"\b(?:summer|fall|autumn|winter|spring)\s*'?(?:20)?\d\d\b", re.I)
NEW_GRAD = re.compile(r"\b(?:new\s*grads?|new\s*graduates?|graduate|entry[\s-]*level|early[\s-]*career|university|"
                      r"college|campus|junior|jr)\b", re.I)
SENIOR = re.compile(
    r"\b(?:senior|sr|staff|principal|lead|manager|director|head\s+of|vp|vice\s+president|distinguished|fellow|"
    r"architect)\b"
    r"|(?<=\s)(?:iii|iv|v|vi)\b(?=\s*(?:$|[,()\-–/|]))"  # "Engineer III", not "V&V"
    r"|\blevel\s*[3-9]\b", re.I)
CS_ROLE = re.compile(
    r"\b(?:software|swe|sde|sdet|developer|programmer|full[\s-]*stack|front[\s-]*end|back[\s-]*end|web|mobile|ios|"
    r"android|data\s+(?:engineer|engineering|scientist|science|analyst|analytics|platform|infrastructure)|"
    r"analytics\s+engineer|business\s+intelligence|machine\s+learning|ml|mle|ai|artificial\s+intelligence|"
    r"deep\s+learning|llm|nlp|natural\s+language|computer\s+vision|research\s+(?:engineer|scientist)|"
    r"applied\s+scientist|cloud|devops|sre|site\s+reliability|platform|infrastructure|security|cybersecurity|cyber|"
    r"appsec|embedded|firmware|compiler|kernel|distributed|database|"
    r"quant(?:itative)?\s+(?:developer|researcher|engineer|trader|analyst)|game|graphics|blockchain|automation|"
    r"test\s+engineer|qa|quality\s+assurance|computer|it|systems?\s+engineer|solutions?\s+engineer|"
    r"forward[\s-]+deployed)\b", re.I)
NON_CS_ROLE = re.compile(
    r"\b(?:mechanical|civil|structural|chemical|manufacturing|industrial|hvac|construction|electrical|"
    r"power\s+systems|rf|radio\s+frequency|analog|mixed[\s-]*signal|asic|rtl|physical\s+design|pcb|optical|"
    r"photonics|materials|nuclear|aerospace|propulsion|thermal|process\s+engineer|technician|product\s+manager|"
    r"program\s+manager|project\s+manager|product\s+owner|designer|sales|account\s+executive|"
    r"business\s+development|marketing|recruiter|recruiting|human\s+resources|accountant|accounting|"
    r"financial\s+analyst|audit|tax|legal|paralegal|attorney|nurse|clinical|pharmacist|biologist|chemist)\b", re.I)
CS_CATEGORIES = {"software", "software engineering", "ai/ml/data", "data science, ai & machine learning", "quant"}
NON_CS_CATEGORIES = {"hardware", "hardware engineering", "product", "product management"}
LABELS = {"offers sponsorship": "offers", "does not offer sponsorship": "no",
          "u.s. citizenship is required": "citizenship_required"}
_US = r"(?:u\.?\s?s\.?|united\s+states)"
CITIZENSHIP = re.compile(
    rf"{_US}\s+citizen(?:ship)?\s+(?:is\s+)?required|"
    rf"must\s+(?:be|hold)\s+(?:a\s+)?{_US}\s+citizen|"
    rf"{_US}\s+citizens?\s+only|"
    r"(?:secret|top\s+secret|ts/sci)\s+(?:security\s+)?clearance\s+(?:is\s+)?required|"
    r"(?:active|current)\s+(?:secret|top\s+secret|ts/sci)\s+(?:security\s+)?clearance|"
    r"(?:ability|able|eligible)\s+to\s+obtain\s+(?:and\s+maintain\s+)?(?:an?\s+)?(?:active\s+)?"
    rf"(?:{_US}\s+)?(?:government\s+|dod\s+)?(?:security\s+)?clearance|"
    r"\bu\.?\s?s\.?\s+persons?\b", re.I)
NO_SPONSOR = re.compile(
    r"\b(?:(?:not|unable\s+to|cannot|can't|won't)\s+(?:\w+\s+){0,3}sponsor|"
    r"no\s+(?:\w+\s+){0,2}sponsorship|"
    r"without\s+(?:\w+\s+){0,5}sponsorship|"
    r"sponsorship\s+(?:is\s+)?(?:not\s+available|not\s+offered|not\s+provided|unavailable))", re.I)
OFFERS_SPONSOR = re.compile(
    r"\b(?:(?:will|can|able\s+to|happy\s+to|glad\s+to)\s+(?:\w+\s+){0,2}sponsor|"
    r"sponsorship\s+(?:is\s+)?(?:available|offered|provided)|"
    r"offers?\s+(?:\w+\s+){0,2}sponsorship)", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)(?:\s+of)?"
                   r"(?:\s+[\w/-]+){0,4}?\s+experience", re.I)
SOFT = re.compile(r"prefer|nice\s+to\s+have|bonus|a\s+plus|desired|ideally|in\s+lieu|instead\s+of|equivalent", re.I)
SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")


@dataclass(frozen=True)
class JobFacts:
    job_id: int
    titles: tuple[str, ...]
    employment_types: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    description: str = ""
    sponsorship_labels: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    closed_at_source: bool = False  # every apply link's employer API says the job is gone

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ScreeningRules:
    exclude_internships: bool = True
    exclude_senior: bool = True
    exclude_non_cs: bool = True
    exclude_citizenship_required: bool = True
    max_years_required: int | None = 3  # a stated requirement of this many years or more excludes; None = off
    extra_cs_terms: tuple[str, ...] = ()  # more words that make a title a CS role
    extra_non_cs_terms: tuple[str, ...] = ()  # more words that make a title clearly not a CS role

    @property
    def version(self) -> str:
        data = json.dumps({"v": RULES_VERSION, **asdict(self)}, sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Screening:
    outcome: str  # kept | excluded
    reasons: tuple[str, ...]
    signals: dict


def years_required(description: str) -> int | None:
    """The smallest years-of-experience figure stated as a requirement (not "preferred"); None if none."""
    found = []
    for sentence in SENTENCE.split(description or ""):
        if SOFT.search(sentence):
            continue
        found += [int(m.group(1)) for m in YEARS.finditer(sentence)]
    return min(found) if found else None


def screen(facts: JobFacts, rules: ScreeningRules) -> Screening:
    title = " / ".join(t for t in facts.titles if t)
    new_grad = bool(NEW_GRAD.search(title))
    reasons = []
    if rules.exclude_internships and _internship(facts, title, new_grad):
        reasons.append("internship")
    if rules.exclude_senior and not new_grad and SENIOR.search(title):
        reasons.append("senior_title")
    role = _role(facts, title, rules)
    if rules.exclude_non_cs and role == "non_cs":
        reasons.append("not_cs_role")
    sponsorship = _sponsorship(facts)
    if rules.exclude_citizenship_required and sponsorship == "citizenship_required":
        reasons.append("citizenship_required")
    years = years_required(facts.description)
    if rules.max_years_required is not None and not new_grad and years is not None \
            and years >= rules.max_years_required:
        reasons.append(f"experience_required:{years}")
    if facts.closed_at_source:
        reasons.append("closed_at_source")
    signals = {"role": role, "sponsorship": sponsorship, "years_required": years, "new_grad_title": new_grad,
               "has_description": bool(facts.description.strip())}
    return Screening("excluded" if reasons else "kept", tuple(reasons), signals)


def _internship(facts: JobFacts, title: str, new_grad: bool) -> bool:
    if "internship" in facts.employment_types or INTERN.search(title):
        return True
    if SEASON.search(title) and not new_grad:  # "Software Engineer (Summer 2027)"
        return True
    return bool(facts.sources) and all("intern" in source for source in facts.sources)


def _terms(terms: tuple[str, ...]) -> re.Pattern | None:
    return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b", re.I) if terms else None


def _role(facts: JobFacts, title: str, rules: ScreeningRules) -> str:
    categories = {c.strip().lower() for c in facts.categories if c}
    extra_cs, extra_non_cs = _terms(rules.extra_cs_terms), _terms(rules.extra_non_cs_terms)
    if CS_ROLE.search(title) or (extra_cs and extra_cs.search(title)) or categories & CS_CATEGORIES:
        return "cs"
    if NON_CS_ROLE.search(title) or (extra_non_cs and extra_non_cs.search(title)) or categories & NON_CS_CATEGORIES:
        return "non_cs"
    return "unknown"


def _sponsorship(facts: JobFacts) -> str:
    labels = {LABELS.get(label.strip().lower()) for label in facts.sponsorship_labels} - {None}
    text = facts.description or ""
    if "citizenship_required" in labels or CITIZENSHIP.search(text):
        return "citizenship_required"
    if NO_SPONSOR.search(text):  # the posting's own words win over a repository label
        return "no"
    if "offers" in labels or OFFERS_SPONSOR.search(text):
        return "offers"
    return "no" if "no" in labels else "unknown"


def load_rules(path: Path | None) -> ScreeningRules:
    """$AUTO_APP_HOME/config/screening.json; a missing file means the defaults."""
    if path is None or not Path(path).exists():
        return ScreeningRules()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read screening rules {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Cannot read screening rules {path}: expected an object")
    unknown = set(data) - {f.name for f in fields(ScreeningRules)}
    if unknown:
        raise ValueError(f"Cannot read screening rules {path}: unknown keys {sorted(unknown)}")
    values = {}
    for key, value in data.items():
        if key.startswith("extra_"):
            if not isinstance(value, list) or any(not isinstance(t, str) or not t.strip() for t in value):
                raise ValueError(f"Cannot read screening rules {path}: {key} must be a list of nonempty strings")
            values[key] = tuple(t.strip() for t in value)
        elif key == "max_years_required":
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"Cannot read screening rules {path}: max_years_required must be a positive "
                                 f"integer or null")
            values[key] = value
        elif type(value) is not bool:
            raise ValueError(f"Cannot read screening rules {path}: {key} must be true or false")
        else:
            values[key] = value
    return ScreeningRules(**values)
```

`config/screening.example.json`（文件内容就是下面这个 JSON 对象）：

```json
{
  "exclude_internships": true,
  "exclude_senior": true,
  "exclude_non_cs": true,
  "exclude_citizenship_required": true,
  "max_years_required": 3,
  "extra_cs_terms": [],
  "extra_non_cs_terms": []
}
```

在 `.gitignore` 里 `config/preferences.json` 那一行之后加：

```
config/screening.json
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/screen/test_rules.py -q`
Expected: PASS（全部参数化用例）

- [ ] **Step 5: Commit（仅在用户批准提交时）**

```bash
git add src/auto_app/screen tests/screen config/screening.example.json .gitignore
git commit -m "feat(screen): deterministic new-grad CS screening rules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 规则结论写入数据库

**Files:**
- Create: `src/auto_app/screen/store.py`、`tests/imported_jobs.py`
- Test: `tests/screen/test_screen_store.py`

**Interfaces:**
- Consumes: Task 1 的 `T.job_screenings`、`T.enrichments`；Task 2 的 `JobFacts`、`ScreeningRules`、`screen`。
- Produces:
  - `chunks(items, size=500) -> Iterator[list]`
  - `load_facts(store) -> list[JobFacts]`：所有活跃岗位，按 job_id 排序。
  - `screen_jobs(store, rules: ScreeningRules, *, now: datetime | None = None) -> dict`，返回键：`rules_version`、`jobs`、`screened`、`unchanged`、`removed`、`kept`、`excluded`、`reasons`（排除原因计数；`experience_required:5` 这类按冒号前缀合并）。
  - `excluded_job_ids(store) -> set[int]`
  - 测试工具 `tests/imported_jobs.py`：`make_store(tmp_path)`、`record(external_id, title, *, sponsorship="Other", category="Software", **fields)`、`ingest(store, records, source="simplify-newgrad")`、`job_id_of(store, title)`、`FakeFetcher`、`ok(description, **fields)`。其中 `FakeFetcher` 和 `ok` 到 Task 5 才用，这里一并写好。

- [ ] **Step 1: 写测试工具和失败测试**

```python
# tests/imported_jobs.py
"""Imported-job test data shared by the Linux worker's tests (screen, enrich, worker)."""

from auto_app.core.db.store import DbStore
from auto_app.ingest.models import IngestRecord, ParsedFeed
from auto_app.ingest.service import IngestService
from auto_app.settings import Paths


def make_store(tmp_path):
    return DbStore(tmp_path / "data" / "auto_app.db", Paths(home=tmp_path))


def record(external_id, title, *, sponsorship="Other", category="Software", **fields):
    values = dict(external_id=external_id, url=f"https://job-boards.greenhouse.io/acme/jobs/{external_id}",
                  title=title, company="Acme", locations=["Boston, MA"], active=True,
                  metadata={"upstream": {"sponsorship": sponsorship, "category": category}})
    values.update(fields)
    return IngestRecord(**values)


def ingest(store, records, source="simplify-newgrad"):
    return IngestService(store).ingest(source, ParsedFeed(records), kind="repository", origin="fixture.json",
                                       revision="r1", complete_snapshot=True)


def job_id_of(store, title):
    return next(j["job_id"] for j in store.jobs() if j["title"] == title)


class FakeFetcher:
    """Answers every fetch with `result` (a FetchResult, or a function of the target); records the API URLs."""

    def __init__(self, result):
        self.result, self.calls = result, []

    def fetch(self, target):
        self.calls.append(target.api_url)
        return self.result(target) if callable(self.result) else self.result


def ok(description, **fields):
    from auto_app.core.models import CommonPosting
    from auto_app.enrich.fetch import FetchResult

    return FetchResult("ok", posting=CommonPosting(source="enrichment", source_job_id="x",
                                                   title=fields.pop("title", "Software Engineer"),
                                                   description_text=description, **fields))
```

```python
# tests/screen/test_screen_store.py
"""Screening verdicts are stored per active job and recomputed only when facts or rules change."""

import pytest
from sqlalchemy import insert, select, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import excluded_job_ids, load_facts, screen_jobs
from tests.imported_jobs import ingest, job_id_of, make_store, record


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Software Engineer, New Grad", sponsorship="Offers Sponsorship"),
                    record("102", "Software Engineer Intern"),
                    record("103", "Software Engineer", sponsorship="U.S. Citizenship is Required")])
    yield result
    result.engine.dispose()


def verdicts(store):
    with store.engine.connect() as conn:
        return {r.job_id: (r.outcome, tuple(r.reasons), r.signals)
                for r in conn.execute(select(T.job_screenings))}


def test_verdicts_reasons_and_signals_are_saved(store):
    report = screen_jobs(store, ScreeningRules())
    assert (report["screened"], report["kept"], report["excluded"]) == (3, 1, 2)
    assert report["reasons"] == {"internship": 1, "citizenship_required": 1}
    saved = verdicts(store)
    ng, intern, citizen = (job_id_of(store, t) for t in
                           ("Software Engineer, New Grad", "Software Engineer Intern", "Software Engineer"))
    assert saved[ng][0] == "kept" and saved[ng][2]["sponsorship"] == "offers"
    assert saved[intern][:2] == ("excluded", ("internship",))
    assert saved[citizen][:2] == ("excluded", ("citizenship_required",))
    assert excluded_job_ids(store) == {intern, citizen}


def test_unchanged_jobs_are_not_rescreened_until_the_rules_change(store):
    screen_jobs(store, ScreeningRules())
    again = screen_jobs(store, ScreeningRules())
    assert (again["screened"], again["unchanged"]) == (0, 3)
    relaxed = screen_jobs(store, ScreeningRules(exclude_internships=False))
    assert relaxed["screened"] == 3
    assert verdicts(store)[job_id_of(store, "Software Engineer Intern")][0] == "kept"


def test_archived_jobs_lose_their_verdict(store):
    screen_jobs(store, ScreeningRules())
    intern = job_id_of(store, "Software Engineer Intern")
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == intern).values(archived_at=utcnow()))
    report = screen_jobs(store, ScreeningRules())
    assert report["removed"] == 1 and intern not in verdicts(store)


def test_facts_come_from_postings_upstream_rows_and_enrichment_results(store):
    ng = job_id_of(store, "Software Engineer, New Grad")
    fact = next(f for f in load_facts(store) if f.job_id == ng)
    assert fact.titles == ("Software Engineer, New Grad",)
    assert fact.sources == ("simplify-newgrad",)
    assert fact.sponsorship_labels == ("Offers Sponsorship",) and fact.categories == ("Software",)
    assert fact.closed_at_source is False
    with store.engine.begin() as conn:
        apply_url = conn.execute(select(T.postings.c.apply_url).where(T.postings.c.job_id == ng)).scalar()
        conn.execute(insert(T.enrichments).values(url=apply_url, platform="greenhouse", status="gone",
                                                  attempts=0, updated_at=utcnow()))
    screen_jobs(store, ScreeningRules())
    assert verdicts(store)[ng][:2] == ("excluded", ("closed_at_source",))
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/screen/test_screen_store.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'auto_app.screen.store'`

- [ ] **Step 3: 实现**

```python
# src/auto_app/screen/store.py
"""Keep each active job's screening verdict current (docs/linux-worker.md §2).

A job is screened again only when its facts or the rules changed, so a round over ten thousand unchanged
jobs is a few reads and no writes. Rows for jobs that were archived or deleted are dropped.
"""

from collections import Counter
from datetime import datetime

from sqlalchemy import delete, func, insert, select

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.screen.rules import JobFacts, ScreeningRules, screen

PLACEHOLDER_TITLE = "[Title pending]"  # IngestService's title for imports without one


def chunks(items, size: int = 500):
    """Lists of at most `size` items: keeps IN (…) under SQLite's bound-parameter limit."""
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def load_facts(store) -> list[JobFacts]:
    j, p, e, n = T.jobs, T.postings, T.ingest_entries, T.enrichments
    active = j.c.archived_at.is_(None)
    with store.engine.connect() as conn:
        postings = conn.execute(
            select(p.c.job_id, p.c.posting_id, p.c.source, p.c.title, p.c.parsed, p.c.apply_url,
                   j.c.primary_posting_id)
            .join(j, j.c.job_id == p.c.job_id).where(active).order_by(p.c.posting_id)).mappings().all()
        upstream = conn.execute(
            select(p.c.job_id, e.c.payload).join(p, p.c.posting_id == e.c.posting_id)
            .join(j, j.c.job_id == p.c.job_id).where(active)).all()
        gone = set(conn.execute(select(n.c.url).where(n.c.status == "gone")).scalars())
    acc: dict[int, dict] = {}
    for r in postings:
        a = acc.setdefault(r["job_id"], {"titles": set(), "employment": set(), "sources": set(), "primary": "",
                                         "longest": "", "labels": set(), "categories": set(), "urls": set()})
        parsed = r["parsed"] or {}
        a["titles"].add((r["title"] or parsed.get("title") or "").strip())
        if parsed.get("employment_type") not in (None, "", "unknown"):
            a["employment"].add(parsed["employment_type"])
        a["sources"].add(r["source"])
        description = parsed.get("description_text") or ""
        if r["posting_id"] == r["primary_posting_id"]:
            a["primary"] = description
        if len(description) > len(a["longest"]):
            a["longest"] = description
        if r["apply_url"]:
            a["urls"].add(r["apply_url"])
    for job_id, payload in upstream:
        a = acc.get(job_id)
        row = ((payload or {}).get("metadata") or {}).get("upstream")
        if a is None or not isinstance(row, dict):
            continue
        for key, target in (("sponsorship", "labels"), ("category", "categories")):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                a[target].add(value.strip())
    return [JobFacts(job_id=job_id, titles=tuple(sorted(a["titles"] - {"", PLACEHOLDER_TITLE})),
                     employment_types=tuple(sorted(a["employment"])), sources=tuple(sorted(a["sources"])),
                     description=a["primary"] or a["longest"], sponsorship_labels=tuple(sorted(a["labels"])),
                     categories=tuple(sorted(a["categories"])),
                     closed_at_source=bool(a["urls"]) and a["urls"] <= gone)
            for job_id, a in sorted(acc.items())]


def screen_jobs(store, rules: ScreeningRules, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    s = T.job_screenings
    facts = load_facts(store)
    version = rules.version
    with store.engine.connect() as conn:
        saved = {r.job_id: (r.rules_version, r.facts_hash)
                 for r in conn.execute(select(s.c.job_id, s.c.rules_version, s.c.facts_hash))}
    rows, unchanged = [], 0
    for fact in facts:
        digest = fact.digest
        if saved.get(fact.job_id) == (version, digest):
            unchanged += 1
            continue
        result = screen(fact, rules)
        rows.append({"job_id": fact.job_id, "rules_version": version, "facts_hash": digest,
                     "outcome": result.outcome, "reasons": list(result.reasons), "signals": result.signals,
                     "screened_at": now})
    active = {fact.job_id for fact in facts}
    stale = [job_id for job_id in saved if job_id not in active]
    with store.engine.begin() as conn:
        for part in chunks(stale):
            conn.execute(delete(s).where(s.c.job_id.in_(part)))
        for part in chunks(rows):
            conn.execute(delete(s).where(s.c.job_id.in_([r["job_id"] for r in part])))
            conn.execute(insert(s), part)
    report = {"rules_version": version, "jobs": len(facts), "screened": len(rows), "unchanged": unchanged,
              "removed": len(stale), "kept": 0, "excluded": 0, "reasons": {}}
    with store.engine.connect() as conn:
        for outcome, count in conn.execute(select(s.c.outcome, func.count()).group_by(s.c.outcome)):
            report[outcome] = count
        reasons = Counter(reason.split(":", 1)[0]
                          for (values,) in conn.execute(select(s.c.reasons).where(s.c.outcome == "excluded"))
                          for reason in values or [])
    report["reasons"] = dict(reasons.most_common())
    return report


def excluded_job_ids(store) -> set[int]:
    s = T.job_screenings
    with store.engine.connect() as conn:
        return set(conn.execute(select(s.c.job_id).where(s.c.outcome == "excluded")).scalars())
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/screen -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准提交时）**

```bash
git add src/auto_app/screen/store.py tests/imported_jobs.py tests/screen/test_screen_store.py
git commit -m "feat(screen): store screening verdicts for active jobs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 申请链接 → 平台 API，以及限速读取

**Files:**
- Create: `src/auto_app/enrich/__init__.py`、`src/auto_app/enrich/targets.py`、`src/auto_app/enrich/fetch.py`
- Test: `tests/enrich/__init__.py`（空文件）、`tests/enrich/test_targets.py`、`tests/enrich/test_fetch.py`

**Interfaces:**
- Consumes: 现有的 `GreenhouseSource/LeverSource/AshbySource/WorkdaySource.parse(raw: RawPosting) -> CommonPosting`、`auto_app.sources.common.urls.UUID`、`workday_job_id`。
- Produces:
  - `Target(platform: str, board: str, job_id: str, api_url: str)`（frozen dataclass）
  - `target_for(url: str | None) -> Target | None`
  - `FetchResult(status: str, posting: CommonPosting | None = None, error: str | None = None)`；`status` ∈ `ok | gone | failed`
  - `Fetcher(client: httpx.Client | None = None, *, min_interval: float = 1.0, clock=time.monotonic, sleep=time.sleep)`，方法：`fetch(target) -> FetchResult`、`close()`

- [ ] **Step 1: 写失败测试**

```python
# tests/enrich/test_targets.py
import pytest

from auto_app.enrich.targets import Target, target_for

UUID = "6ed76ce8-1111-2222-3333-444455556666"


@pytest.mark.parametrize("url, expected", [
    ("https://job-boards.greenhouse.io/acme/jobs/4012345",
     Target("greenhouse", "acme", "4012345", "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345")),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345",
     Target("greenhouse", "acme", "4012345", "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345")),
    (f"https://jobs.lever.co/acme/{UUID}/apply",
     Target("lever", "acme", UUID, f"https://api.lever.co/v0/postings/acme/{UUID}")),
    (f"https://jobs.eu.lever.co/acme/{UUID}",
     Target("lever", "acme", UUID, f"https://api.eu.lever.co/v0/postings/acme/{UUID}")),
    (f"https://jobs.ashbyhq.com/Acme/{UUID}/application",
     Target("ashby", "acme", UUID, "https://api.ashbyhq.com/posting-api/job-board/Acme")),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-TX-Austin/"
     "Software-Engineer_JR2026343/apply",
     Target("workday", "nvidia/nvidiaexternalcareersite", "JR2026343",
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/US-TX-Austin/"
            "Software-Engineer_JR2026343")),
])
def test_supported_platform_links(url, expected):
    assert target_for(url) == expected


@pytest.mark.parametrize("url", [
    None, "", "https://acme.com/careers?gh_jid=4012345", "https://careers.oracle.com/jobs/123",
    "https://wd3.myworkdaysite.com/recruiting/acme/External/job/X_R1", "https://jobs.lever.co/acme/not-a-uuid",
    "https://job-boards.greenhouse.io/acme", "https://acme.wd1.myworkdayjobs.com/External",
])
def test_everything_else_has_no_target(url):
    assert target_for(url) is None
```

```python
# tests/enrich/test_fetch.py
"""Fetcher: one paced request per job, the platform's own parser, 404 = gone."""

import httpx

from auto_app.enrich.fetch import Fetcher
from auto_app.enrich.targets import target_for

UUID = "91eed659-1111-2222-3333-444455556666"
OTHER = "aaaaaaaa-1111-2222-3333-444455556666"
GREENHOUSE = {"id": 4012345, "title": "Software Engineer, New Grad", "updated_at": "2026-10-01T00:00:00Z",
              "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4012345",
              "location": {"name": "Boston, MA"}, "content": "&lt;p&gt;Build APIs in Python.&lt;/p&gt;"}
WORKDAY = {"jobPostingInfo": {"title": "Software Engineer", "jobDescription": "<p>Write C++.</p>",
                              "location": "US, TX, Austin", "timeType": "Full time", "startDate": "2026-10-01",
                              "jobReqId": "JR2026343"},
           "hiringOrganization": {"name": "NVIDIA"}}
ASHBY = {"jobs": [{"id": UUID, "title": "Backend Engineer", "location": "New York", "jobUrl": "https://x",
                   "descriptionPlain": "Own services.", "employmentType": "FullTime", "isListed": True},
                  {"id": OTHER, "title": "Data Engineer", "location": "Remote", "jobUrl": "https://y",
                   "descriptionPlain": "Own pipelines.", "employmentType": "FullTime", "isListed": True}]}
WD_URL = ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-TX-Austin/"
          "Software-Engineer_JR2026343")


class Clock:
    def __init__(self):
        self.t, self.sleeps = 100.0, []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.t += seconds


def fetcher(handler, clock=None):
    clock = clock or Clock()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Fetcher(client, clock=clock, sleep=clock.sleep)


def test_greenhouse_and_workday_records_parse_with_the_collectors_parsers():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=GREENHOUSE if "greenhouse" in request.url.host else WORKDAY)

    f = fetcher(handler)
    gh = f.fetch(target_for("https://job-boards.greenhouse.io/acme/jobs/4012345"))
    wd = f.fetch(target_for(WD_URL))
    assert gh.status == "ok" and "Build APIs in Python." in gh.posting.description_text
    assert wd.status == "ok" and "Write C++." in wd.posting.description_text
    assert wd.posting.employment_type == "full_time" and wd.posting.req_id == "JR2026343"
    assert seen[1].endswith("/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/US-TX-Austin/Software-Engineer_JR2026343")


def test_an_ashby_board_is_read_once_per_round_and_missing_jobs_are_gone():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=ASHBY)

    f = fetcher(handler)
    first = f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{UUID}"))
    second = f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{OTHER}/application"))
    missing = f.fetch(target_for("https://jobs.ashbyhq.com/acme/bbbbbbbb-1111-2222-3333-444455556666"))
    assert (first.status, second.status, missing.status) == ("ok", "ok", "gone")
    assert first.posting.description_text == "Own services." and len(calls) == 1


def test_404_is_gone_and_server_errors_are_failures():
    f = fetcher(lambda request: httpx.Response(404 if request.url.path.endswith("/1") else 500))
    gone = f.fetch(target_for("https://job-boards.greenhouse.io/acme/jobs/1"))
    failed = f.fetch(target_for("https://job-boards.greenhouse.io/acme/jobs/2"))
    assert gone.status == "gone" and gone.posting is None
    assert failed.status == "failed" and "HTTPStatusError" in failed.error


def test_requests_to_one_host_are_spaced_by_the_minimum_interval():
    clock = Clock()
    f = fetcher(lambda request: httpx.Response(200, json=GREENHOUSE), clock)
    f.fetch(target_for("https://job-boards.greenhouse.io/acme/jobs/1"))
    f.fetch(target_for("https://job-boards.greenhouse.io/acme/jobs/2"))
    assert clock.sleeps == [1.0]
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/enrich -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'auto_app.enrich'`

- [ ] **Step 3: 实现**

```python
# src/auto_app/enrich/__init__.py
"""JD enrichment for imported jobs: their employers' public job APIs (docs/linux-worker.md §1)."""
```

```python
# src/auto_app/enrich/targets.py
"""Where an imported job's official record can be read: its platform's public job API (docs/linux-worker.md §1).

    https://job-boards.greenhouse.io/acme/jobs/123                  → boards-api.greenhouse.io/v1/boards/acme/jobs/123
    https://boards.greenhouse.io/embed/job_app?for=acme&token=123   → the same
    https://jobs.lever.co/acme/<uuid>                               → api.lever.co/v0/postings/acme/<uuid>
    https://jobs.ashbyhq.com/acme/<uuid>                            → the board's posting API (read once per round)
    https://acme.wd5.myworkdayjobs.com/en-US/Site/job/City/Title_R1 → …/wday/cxs/acme/Site/job/City/Title_R1
Anything else — company career sites, Greenhouse embedded on a company domain, iCIMS, Oracle … — has no
target: the job stays title-only.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from auto_app.sources.common.urls import UUID, workday_job_id

LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
LEVER_API = {"jobs.lever.co": "api.lever.co", "jobs.eu.lever.co": "api.eu.lever.co"}


@dataclass(frozen=True)
class Target:
    platform: str  # greenhouse | lever | ashby | workday
    board: str  # lowercase; Workday: "<tenant>/<site>"
    job_id: str
    api_url: str


def target_for(url: str | None) -> Target | None:
    if not url:
        return None
    u = urlparse(url)
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if host in GREENHOUSE_HOSTS:
        query = parse_qs(u.query)
        if parts[:2] == ["embed", "job_app"]:
            board, job = (query.get("for") or [None])[0], (query.get("token") or [None])[0]
        elif len(parts) >= 3 and parts[1] == "jobs":
            board, job = parts[0], parts[2]
        else:
            return None
        if not board or not job or not job.isdigit():
            return None
        return Target("greenhouse", board.lower(), job,
                      f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}")
    if host in LEVER_API and len(parts) >= 2 and UUID.match(parts[1]):
        board, job = parts[0].lower(), parts[1].lower()
        return Target("lever", board, job, f"https://{LEVER_API[host]}/v0/postings/{board}/{job}")
    if host == "jobs.ashbyhq.com" and len(parts) >= 2 and UUID.match(parts[1]):
        return Target("ashby", parts[0].lower(), parts[1].lower(),
                      f"https://api.ashbyhq.com/posting-api/job-board/{parts[0]}")
    if host.endswith(".myworkdayjobs.com"):
        tenant = host.split(".")[0]
        parts = [p for p in parts if not LOCALE.match(p)]
        while parts and parts[-1].lower() in {"apply", "application", "applymanually"}:
            parts.pop()
        if len(parts) >= 3 and parts[1] == "job":
            site = parts[0]
            return Target("workday", f"{tenant}/{site.lower()}", workday_job_id(parts[-1]),
                          f"https://{host}/wday/cxs/{tenant}/{site}/" + "/".join(parts[1:]))
    return None
```

```python
# src/auto_app/enrich/fetch.py
"""Read one official job record per target over plain HTTP, politely (docs/linux-worker.md §1).

Parsing reuses each platform's collector parser, so an enriched job reads exactly like a collected one.
No browser, no cookies, no redirects, no retries inside a round: a failed read is retried by a later round.
"""

import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from auto_app.core.models import CommonPosting, RawPosting
from auto_app.enrich.targets import Target
from auto_app.platforms.ashby import AshbySource
from auto_app.platforms.greenhouse import GreenhouseSource
from auto_app.platforms.lever import LeverSource
from auto_app.platforms.workday import WorkdaySource
from auto_app.sources.parsing import ParseError

USER_AGENT = "auto-app/1 (personal job search; one request per job)"
PARSERS = {"greenhouse": GreenhouseSource, "lever": LeverSource, "ashby": AshbySource, "workday": WorkdaySource}
GONE = (404, 410)


@dataclass
class FetchResult:
    status: str  # ok | gone | failed
    posting: CommonPosting | None = None
    error: str | None = None


class Fetcher:
    def __init__(self, client: httpx.Client | None = None, *, min_interval: float = 1.0,
                 clock=time.monotonic, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=20, follow_redirects=False,
                                             headers={"User-Agent": USER_AGENT})
        self.min_interval, self.clock, self.sleep = min_interval, clock, sleep
        self._last: dict[str, float] = {}  # host → when its last request ended
        self._boards: dict[str, dict | None] = {}  # Ashby board → {job id: record}; None = the board is gone

    def close(self) -> None:
        self.client.close()

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        host = urlparse(url).hostname or ""
        if host in self._last:
            wait = self._last[host] + self.min_interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        try:
            return self.client.get(url, params=params)
        finally:
            self._last[host] = self.clock()

    def fetch(self, target: Target) -> FetchResult:
        try:
            if target.platform == "ashby":
                record = self._ashby_record(target)
                if record is None:
                    return FetchResult("gone", error="not on the employer's board")
                raw = _raw(target, record=record)
            else:
                response = self._get(target.api_url)
                if response.status_code in GONE:
                    return FetchResult("gone", error=f"HTTP {response.status_code}")
                response.raise_for_status()
                data = response.json()
                raw = _raw(target, detail=data) if target.platform == "workday" else _raw(target, record=data)
            return FetchResult("ok", posting=PARSERS[target.platform]().parse(raw))
        except (httpx.HTTPError, ValueError, ParseError, KeyError, TypeError) as exc:
            return FetchResult("failed", error=f"{type(exc).__name__}: {exc}"[:300])

    def _ashby_record(self, target: Target) -> dict | None:
        if target.board not in self._boards:
            response = self._get(target.api_url, {"includeCompensation": "true"})
            if response.status_code in GONE:
                self._boards[target.board] = None
            else:
                response.raise_for_status()
                self._boards[target.board] = {str(j["id"]).lower(): j for j in response.json().get("jobs", [])
                                              if j.get("id")}
        jobs = self._boards[target.board]
        return None if jobs is None else jobs.get(target.job_id)


def _raw(target: Target, *, record: dict | None = None, detail: dict | None = None) -> RawPosting:
    return RawPosting(source="enrichment", source_job_id=target.job_id, kind="api", posting={}, capture={},
                      card={}, record=record or {}, detail=detail)
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/enrich -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准提交时）**

```bash
git add src/auto_app/enrich tests/enrich
git commit -m "feat(enrich): resolve ATS links and fetch official job records

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: JD 补全写回数据库（缓存 + 退避）

**Files:**
- Create: `src/auto_app/enrich/service.py`
- Test: `tests/enrich/test_service.py`

**Interfaces:**
- Consumes: Task 1 的表；Task 3 的 `chunks`；Task 4 的 `target_for`、`FetchResult`；现有的 `normalize_posting`、`CompanyAliases`、`load_items`、`refresh_job_projection`、`default_registry`。
- Produces: `enrich_jobs(store, fetcher, *, limit: int = 200, now: datetime | None = None) -> dict`。返回键：`candidates`、`fetched`、`ok`、`gone`、`failed`、`unsupported`、`from_cache`、`waiting`、`skipped_limit`、`jobs_enriched`。`fetcher` 只需要提供 `fetch(target) -> FetchResult`。

行为规则（写进函数的 docstring）：

- **候选**：`parser='ingest-v1'`、`enrichment_status='pending'`、有 `apply_url`、所属岗位活跃、且没有被规则排除的 posting，按 `posted_at` 从新到旧。
- **按缓存状态处理**：
  - `ok`：直接写回，不发请求，不计入 `limit`。
  - `unsupported` / `gone`：跳过。
  - `failed` 且还没到 `next_attempt_at`：计入 `waiting`。
  - 其他情况才发请求，计入 `limit`。
- **失败退避**：第 n 次失败后等 `min(24h, 30min × 2^(n-1))`。
- **雇主返回空描述**：记为 `unsupported`（"employer record has no description"），以后不再请求。
- **写回 posting**：
  - 必写 `description_text`，有则写 `description_html`。
  - `employment_type`、`workplace_type`、`salary`、`req_id`、`locations`、`title` 只在原值为空或 unknown 时填。
  - 设置 `extras.ingestion.enrichment_status = "complete"` 和 `extras.enrichment = {platform, url, fetched_at}`。
  - 重新 normalize（描述的 hash 参与去重）。
  - 最后刷新受影响岗位的 projection。

- [ ] **Step 1: 写失败测试**

```python
# tests/enrich/test_service.py
"""Enrichment fills imported jobs' descriptions, caches by URL, backs off failures, never re-asks for gone jobs."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.enrich.fetch import FetchResult
from auto_app.enrich.service import enrich_jobs
from auto_app.process.normalize import pending_enrichment
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import excluded_job_ids, screen_jobs
from tests.imported_jobs import FakeFetcher, ingest, job_id_of, make_store, ok, record

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/101"


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Software Engineer, New Grad")])
    yield result
    result.engine.dispose()


def cache_row(store):
    with store.engine.connect() as conn:
        return conn.execute(select(T.enrichments)).mappings().one()


def test_enrichment_fills_the_description_and_unblocks_selection(store):
    job = job_id_of(store, "Software Engineer, New Grad")
    assert pending_enrichment(store.job_inputs(job)[0])
    fetcher = FakeFetcher(ok("Build APIs in Python.", employment_type="full_time"))
    report = enrich_jobs(store, fetcher, limit=10, now=NOW)
    assert (report["ok"], report["jobs_enriched"], report["fetched"]) == (1, 1, 1)
    assert fetcher.calls == [API]
    parsed, _ = store.job_inputs(job)
    assert parsed["description_text"] == "Build APIs in Python." and not pending_enrichment(parsed)
    assert parsed["employment_type"] == "full_time"
    assert parsed["extras"]["enrichment"]["platform"] == "greenhouse"
    assert cache_row(store)["status"] == "ok"


def test_reimport_restores_description_from_cache_without_a_request(store):
    job = job_id_of(store, "Software Engineer, New Grad")
    enrich_jobs(store, FakeFetcher(ok("Build APIs in Python.")), limit=10, now=NOW)
    ingest(store, [record("101", "Software Engineer, New Grad", locations=["Austin, TX"])])  # upstream row changed
    assert pending_enrichment(store.job_inputs(job)[0])
    never = FakeFetcher(lambda target: pytest.fail("cached result must not be fetched again"))
    report = enrich_jobs(store, never, limit=10, now=NOW)
    assert report["from_cache"] == 1 and never.calls == []
    assert store.job_inputs(job)[0]["description_text"] == "Build APIs in Python."


def test_a_gone_job_is_screened_out_and_never_requested_again(store):
    job = job_id_of(store, "Software Engineer, New Grad")
    fetcher = FakeFetcher(FetchResult("gone", error="HTTP 404"))
    assert enrich_jobs(store, fetcher, limit=10, now=NOW)["gone"] == 1
    screen_jobs(store, ScreeningRules())
    assert job in excluded_job_ids(store)
    enrich_jobs(store, fetcher, limit=10, now=NOW + timedelta(days=2))
    assert len(fetcher.calls) == 1


def test_failures_back_off_exponentially(store):
    fetcher = FakeFetcher(FetchResult("failed", error="HTTPStatusError: 500"))
    enrich_jobs(store, fetcher, limit=10, now=NOW)
    soon = enrich_jobs(store, fetcher, limit=10, now=NOW + timedelta(minutes=5))
    assert soon["waiting"] == 1 and len(fetcher.calls) == 1
    later = enrich_jobs(store, fetcher, limit=10, now=NOW + timedelta(minutes=31))
    assert later["failed"] == 1 and len(fetcher.calls) == 2
    row = cache_row(store)
    assert row["attempts"] == 2
    assert row["next_attempt_at"].replace(tzinfo=timezone.utc) == NOW + timedelta(minutes=31, hours=1)


def test_unsupported_links_and_the_request_limit(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("201", "Backend Engineer", url="https://careers.acme.com/jobs/201"),
                   record("202", "Data Engineer"), record("203", "ML Engineer"), record("204", "Web Developer")])
    fetcher = FakeFetcher(ok("Ship features."))
    report = enrich_jobs(store, fetcher, limit=2, now=NOW)
    assert (report["unsupported"], report["fetched"], report["skipped_limit"]) == (1, 2, 1)
    store.engine.dispose()


def test_screened_out_jobs_are_not_fetched_and_empty_descriptions_stop_retries(store):
    job = job_id_of(store, "Software Engineer, New Grad")
    with store.engine.begin() as conn:
        conn.execute(insert(T.job_screenings).values(job_id=job, rules_version="t", facts_hash="h",
                                                     outcome="excluded", reasons=["internship"], signals={},
                                                     screened_at=utcnow()))
    fetcher = FakeFetcher(ok("unused"))
    assert enrich_jobs(store, fetcher, limit=10, now=NOW)["candidates"] == 0 and fetcher.calls == []
    with store.engine.begin() as conn:
        conn.execute(T.job_screenings.delete())
    empty = FakeFetcher(ok(""))
    enrich_jobs(store, empty, limit=10, now=NOW)
    enrich_jobs(store, empty, limit=10, now=NOW + timedelta(days=3))
    assert len(empty.calls) == 1 and cache_row(store)["status"] == "unsupported"
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/enrich/test_service.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'auto_app.enrich.service'`

- [ ] **Step 3: 实现**

```python
# src/auto_app/enrich/service.py
"""Fill in imported jobs' missing descriptions from their employers' public job APIs (docs/linux-worker.md §1).

Candidates: postings imported without a description (`enrichment_status: pending`) whose job is active and
not excluded by screening, newest first. Results are cached per canonical apply URL in `enrichments`:
a cached success is written back without a request (a feed re-import rewrites the posting with an empty
description), `gone` and `unsupported` are never asked again, and a failure waits 30 min × 2^(n-1), at most
a day. Only real requests count against `limit`.
"""

import copy
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as upsert

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.enrich.targets import target_for
from auto_app.process.dedup import load_items
from auto_app.process.link import refresh_job_projection
from auto_app.process.normalize import CompanyAliases, normalize_posting
from auto_app.screen.store import PLACEHOLDER_TITLE, chunks
from auto_app.sources.registry import default_registry

PENDING = func.coalesce(func.json_extract(T.postings.c.parsed, "$.extras.ingestion.enrichment_status"), "") == "pending"
BACKOFF_START = timedelta(minutes=30)
BACKOFF_MAX = timedelta(hours=24)
FIELDS = {"title", "locations", "employment_type", "workplace_type", "salary", "req_id", "description_text",
          "description_html"}


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; every stored value is UTC."""
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def candidates(store) -> list[dict]:
    p, j, s = T.postings, T.jobs, T.job_screenings
    excluded = select(s.c.job_id).where(s.c.outcome == "excluded")
    query = (select(p.c.posting_id, p.c.job_id, p.c.apply_url)
             .join(j, j.c.job_id == p.c.job_id)
             .where(j.c.archived_at.is_(None), p.c.parser == "ingest-v1", PENDING, p.c.apply_url.is_not(None),
                    j.c.job_id.not_in(excluded))
             .order_by(p.c.posted_at.desc().nulls_last(), p.c.posting_id.desc()))
    with store.engine.connect() as conn:
        return [dict(r) for r in conn.execute(query).mappings()]


def _cache(store, urls) -> dict[str, dict]:
    e, out = T.enrichments, {}
    with store.engine.connect() as conn:
        for part in chunks(sorted(urls)):
            for row in conn.execute(select(e).where(e.c.url.in_(part))).mappings():
                out[row["url"]] = dict(row, next_attempt_at=_aware(row["next_attempt_at"]),
                                       fetched_at=_aware(row["fetched_at"]))
    return out


def _save(store, url: str, platform: str | None, status: str, now: datetime, *, data: dict | None = None,
          error: str | None = None, attempts: int = 0, next_attempt_at: datetime | None = None) -> dict:
    values = dict(url=url, platform=platform, status=status, attempts=attempts, next_attempt_at=next_attempt_at,
                  fetched_at=now if status in ("ok", "gone") else None, data=data, error=error, updated_at=now)
    statement = upsert(T.enrichments).values(**values)
    with store.engine.begin() as conn:
        conn.execute(statement.on_conflict_do_update(
            index_elements=["url"], set_={key: statement.excluded[key] for key in values if key != "url"}))
    return values


def _write_back(store, aliases: CompanyAliases, posting_id: int, cached: dict) -> int:
    """Copy the employer's fields into the imported posting; returns its job id."""
    data = cached["data"]
    with store.engine.begin() as conn:
        row = conn.execute(select(T.postings.c.parsed, T.postings.c.source, T.postings.c.job_id)
                           .where(T.postings.c.posting_id == posting_id)).one()
        parsed = copy.deepcopy(row.parsed or {})
        parsed["description_text"] = data["description_text"]
        if data.get("description_html"):
            parsed["description_html"] = data["description_html"]
        for key in ("employment_type", "workplace_type"):
            if parsed.get(key) in (None, "", "unknown") and data.get(key) not in (None, "", "unknown"):
                parsed[key] = data[key]
        for key in ("salary", "req_id", "locations"):
            if not parsed.get(key) and data.get(key):
                parsed[key] = data[key]
        if parsed.get("title") in (None, "", PLACEHOLDER_TITLE) and data.get("title"):
            parsed["title"] = data["title"]
        extras = dict(parsed.get("extras") or {})
        extras["ingestion"] = {**(extras.get("ingestion") or {}), "enrichment_status": "complete"}
        extras["enrichment"] = {"platform": cached["platform"], "url": cached["url"],
                                "fetched_at": cached["fetched_at"].isoformat() if cached["fetched_at"] else None}
        parsed["extras"] = extras
        norm = normalize_posting(parsed, row.source, aliases, None)
        conn.execute(update(T.postings).where(T.postings.c.posting_id == posting_id).values(
            parsed=parsed, title=parsed.get("title"), normalized=norm, company_norm=norm["company_norm"],
            title_norm=norm["title_norm"], employer_key=norm["employer_key"], req_id=norm["req_id"],
            desc_hash=norm["desc_hash"]))
        return row.job_id


def enrich_jobs(store, fetcher, *, limit: int = 200, now: datetime | None = None) -> dict:
    now = now or utcnow()
    report = dict.fromkeys(("candidates", "fetched", "ok", "gone", "failed", "unsupported", "from_cache",
                            "waiting", "skipped_limit", "jobs_enriched"), 0)
    rows = candidates(store)
    report["candidates"] = len(rows)
    if not rows:
        return report
    registry = default_registry()
    aliases = CompanyAliases(registry.companies(), store.load_aliases())
    cache = _cache(store, {r["apply_url"] for r in rows})
    touched: set[int] = set()
    for r in rows:
        url = r["apply_url"]
        cached = cache.get(url)
        if cached and cached["status"] == "ok":
            touched.add(_write_back(store, aliases, r["posting_id"], cached))
            report["from_cache"] += 1
            continue
        if cached and cached["status"] in ("unsupported", "gone"):
            continue
        if cached and cached["status"] == "failed" and cached["next_attempt_at"] and cached["next_attempt_at"] > now:
            report["waiting"] += 1
            continue
        target = target_for(url)
        if target is None:
            cache[url] = _save(store, url, None, "unsupported", now, error="no public job API for this link")
            report["unsupported"] += 1
            continue
        if report["fetched"] >= limit:
            report["skipped_limit"] += 1
            continue
        report["fetched"] += 1
        result = fetcher.fetch(target)
        if result.status == "ok" and not (result.posting.description_text or "").strip():
            cache[url] = _save(store, url, target.platform, "unsupported", now,
                               error="employer record has no description")
            report["unsupported"] += 1
        elif result.status == "ok":
            data = result.posting.model_dump(mode="json", include=FIELDS)
            cache[url] = _save(store, url, target.platform, "ok", now, data=data)
            touched.add(_write_back(store, aliases, r["posting_id"], cache[url]))
            report["ok"] += 1
        elif result.status == "gone":
            cache[url] = _save(store, url, target.platform, "gone", now, error=result.error)
            report["gone"] += 1
        else:
            attempts = (cached or {}).get("attempts", 0) + 1
            wait = min(BACKOFF_MAX, BACKOFF_START * 2 ** (attempts - 1))
            cache[url] = _save(store, url, target.platform, "failed", now, error=result.error, attempts=attempts,
                               next_attempt_at=now + wait)
            report["failed"] += 1
    if touched:
        items = load_items(store, registry, job_ids=touched)
        with store.transaction():
            refresh_job_projection(store, items, touched)
    report["jobs_enriched"] = len(touched)
    return report
```

- [ ] **Step 4: 运行，确认通过；再跑导入和去重相关测试，确认没有破坏现有行为**

Run: `uv run pytest tests/enrich tests/ingest/test_service.py tests/process -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准提交时）**

```bash
git add src/auto_app/enrich/service.py tests/enrich/test_service.py
git commit -m "feat(enrich): fill imported job descriptions with caching and backoff

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: 用户自己运行的 `filter run` 跳过被规则排除的岗位

worker 不运行 LLM 筛选。这个改动只影响用户按额度手动执行的 `auto-app filter run`：不在规则已经排除的岗位上花额度。

**Files:**
- Modify: `src/auto_app/select/engine.py`（`plan()`，新增函数 `screened_out_jobs`）
- Test: `tests/select/test_plan_screening.py`

**Interfaces:**
- Consumes: Task 3 的 `excluded_job_ids`。
- Produces: `screened_out_jobs(store) -> set[int]`（文件存储返回空集合）。没有指定 `opts.jobs` 时，`plan()` 跳过这些岗位；显式 `--job N` 照常判断。

- [ ] **Step 1: 写失败测试**

```python
# tests/select/test_plan_screening.py
"""Jobs the screening rules excluded never cost a model run, unless asked for by id."""

from sqlalchemy import insert

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.select.engine import SelectOptions, plan
from auto_app.select.filters import load_filter
from tests.select.test_select import env  # noqa: F401 — the shared fixture


def test_plan_skips_screened_out_jobs_unless_asked(env):  # noqa: F811
    db, folder, jobs = env
    with db.engine.begin() as conn:
        conn.execute(insert(T.job_screenings).values(job_id=jobs["sales"], rules_version="t", facts_hash="h",
                                                     outcome="excluded", reasons=["not_cs_role"], signals={},
                                                     screened_at=utcnow()))
    flt = load_filter(folder / "ml")
    planned = {job for job, _ in plan(db, [flt], SelectOptions())}
    assert jobs["sales"] not in planned and jobs["ml"] in planned
    assert {job for job, _ in plan(db, [flt], SelectOptions(jobs=[jobs["sales"]]))} == {jobs["sales"]}
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/select/test_plan_screening.py -q`
Expected: FAIL，断言 `jobs["sales"] not in planned` 不成立

- [ ] **Step 3: 实现**

在 `src/auto_app/select/engine.py` 的 `unjudgeable()` 后面加：

```python
def screened_out_jobs(store) -> set[int]:
    """Jobs the deterministic screening excluded (docs/linux-worker.md §2): not worth a model run."""
    if not hasattr(store, "engine"):
        return set()
    from auto_app.screen.store import excluded_job_ids

    return excluded_job_ids(store)
```

在 `plan()` 里：
1. `gave_up = unjudgeable(store)` 那行之后加 `screened_out = screened_out_jobs(store)`。
2. 循环里 `if j.get("decision") and opts.jobs is None: continue` 之后加：

```python
        if j["job_id"] in screened_out and opts.jobs is None:
            continue
```

3. 模块 docstring 第一段末尾补一句：`Jobs excluded by the screening rules (job_screenings) are skipped unless asked for by id.`

- [ ] **Step 4: 运行，确认通过；再跑整个筛选测试目录**

Run: `uv run pytest tests/select -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准提交时）**

```bash
git add src/auto_app/select/engine.py tests/select/test_plan_screening.py
git commit -m "feat(select): skip jobs excluded by screening rules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: 查询接口和 MCP：隐藏规则排除、返回原因、按担保过滤

**Files:**
- Modify: `src/auto_app/query/service.py`、`src/auto_app/query/cli.py`、`src/auto_app/query/api.py`、`src/auto_app/agent/mcp_server.py`
- Create: `tests/screening_db.py`（共享 fixture）
- Test: `tests/query/test_screening.py`、`tests/agent/test_mcp_screening.py`

**Interfaces:**
- Consumes: Task 1 的 `job_screenings` 表。这张表是可选的：旧数据库没有这张表时，一切照旧。
- Produces:
  - `QueryService.screening_available: bool`
  - `QueryService.jobs(..., include_excluded=False, sponsorship: str | None = None)`：`sponsorship` ∈ `offers|no|unknown|citizenship_required`。`include_excluded=False` 时同时隐藏国家排除和规则排除的岗位。
  - 每个岗位的 `item["screening"]`：有规则结论时为 `{excluded, reasons（国家原因 + 规则原因）, matched_locations, signals, rules_version, screened_at}`；没有结论时保持原样。
  - `context()["screening"] = {kept, excluded, unscreened, excluded_by_reason, kept_by_sponsorship}`，以及 `context()["capabilities"]["rules_screening"]`。
  - 三个入口都支持 `sponsorship`：CLI `auto-app-query jobs --sponsorship offers`；HTTP `/api/jobs?sponsorship=offers`；MCP `search_jobs(sponsorship="offers")`。

- [ ] **Step 1: 写共享 fixture 和失败测试**

```python
# tests/screening_db.py
"""A migrated database with three jobs and two screening verdicts, for query and MCP tests."""

import json
import sqlite3

import pytest

STAMP = "2026-10-09 10:00:00.000000"


@pytest.fixture
def screening_db(tmp_path):
    from auto_app.core.db.store import DbStore
    from auto_app.settings import Paths

    path = tmp_path / "data" / "auto_app.db"
    DbStore(path, Paths(home=tmp_path)).engine.dispose()
    with sqlite3.connect(path) as conn:
        for job_id, title in ((1, "Software Engineer, New Grad"), (2, "Account Executive"), (3, "Data Analyst")):
            conn.execute("""INSERT INTO jobs (job_id, company_norm, title_norm, first_seen_at, last_seen_at)
                            VALUES (?, 'acme', ?, ?, ?)""", (job_id, title.lower(), STAMP, STAMP))
            conn.execute("""INSERT INTO postings (posting_id, job_id, source, source_job_id, kind, state, purged,
                            first_seen_at, last_seen_at, title, company, parsed)
                            VALUES (?, ?, 'simplify-newgrad', ?, 'api', 'linked', 0, ?, ?, ?, 'Acme', ?)""",
                         (job_id, job_id, str(job_id), STAMP, STAMP, title, json.dumps({"title": title})))
            conn.execute("UPDATE jobs SET primary_posting_id=? WHERE job_id=?", (job_id, job_id))
        for job_id, outcome, reasons, sponsorship in ((1, "kept", [], "offers"),
                                                       (2, "excluded", ["not_cs_role"], "unknown")):
            conn.execute("""INSERT INTO job_screenings (job_id, rules_version, facts_hash, outcome, reasons, signals,
                            screened_at) VALUES (?, 'v1', 'h', ?, ?, ?, ?)""",
                         (job_id, outcome, json.dumps(reasons), json.dumps({"sponsorship": sponsorship}), STAMP))
    return path
```

```python
# tests/query/test_screening.py
"""Rule-excluded jobs are hidden by default, explained on audit, and filterable by sponsorship."""

import json
import sqlite3

import pytest

from tests.screening_db import screening_db  # noqa: F401 — the shared fixture


def service(path):
    from auto_app.query.service import QueryService

    return QueryService(path)


def ids(page):
    return [item["job_id"] for item in page["items"]]


def test_rule_excluded_jobs_are_hidden_by_default_and_explained_on_audit(screening_db):  # noqa: F811
    with service(screening_db) as query:
        assert ids(query.jobs()) == [3, 1]
        assert ids(query.jobs(include_excluded=True)) == [3, 2, 1]
        excluded = query.job(2)["screening"]
        assert excluded["excluded"] is True and excluded["reasons"] == ["not_cs_role"]
        assert excluded["rules_version"] == "v1" and excluded["signals"] == {"sponsorship": "unknown"}
        assert query.job(3)["screening"] == {"excluded": False, "reasons": [], "matched_locations": []}


def test_sponsorship_filter_and_context_counts(screening_db):  # noqa: F811
    with service(screening_db) as query:
        assert ids(query.jobs(sponsorship="offers")) == [1]
        with pytest.raises(ValueError, match="sponsorship must be one of"):
            query.jobs(sponsorship="maybe")
        context = query.context()
    assert context["capabilities"]["rules_screening"] is True
    assert context["screening"] == {"kept": 1, "excluded": 1, "unscreened": 1,
                                    "excluded_by_reason": {"not_cs_role": 1}, "kept_by_sponsorship": {"offers": 1}}


def test_databases_without_the_screening_table_still_work(screening_db):  # noqa: F811
    with sqlite3.connect(screening_db) as conn:
        conn.execute("DROP TABLE job_screenings")
    with service(screening_db) as query:
        assert ids(query.jobs()) == [3, 2, 1]
        assert query.context()["capabilities"]["rules_screening"] is False
        with pytest.raises(ValueError, match="screening"):
            query.jobs(sponsorship="offers")


def test_query_cli_passes_the_sponsorship_filter(screening_db, capsys):  # noqa: F811
    from auto_app.query.cli import main

    assert main(["--db", str(screening_db), "jobs", "--sponsorship", "offers"]) == 0
    assert ids(json.loads(capsys.readouterr().out)) == [1]
```

```python
# tests/agent/test_mcp_screening.py
"""search_jobs exposes the sponsorship signal to chat clients."""

from mcp import Client

from tests.screening_db import screening_db  # noqa: F401 — the shared fixture


async def test_search_jobs_filters_by_sponsorship(screening_db):  # noqa: F811
    from auto_app.agent.mcp_server import create_mcp_server

    async with Client(create_mcp_server(screening_db)) as client:
        page = await client.call_tool("search_jobs", {"sponsorship": "offers"})
        assert not page.is_error
        assert [job["job_id"] for job in page.structured_content["items"]] == [1]
        context = await client.call_tool("get_job_context", {})
        assert context.structured_content["screening"]["excluded"] == 1
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/query/test_screening.py tests/agent/test_mcp_screening.py -q`
Expected: FAIL：`ids(query.jobs())` 返回 `[3, 2, 1]` 而不是 `[3, 1]`；`sponsorship` 是未知参数。

- [ ] **Step 3: 实现**

修改 `src/auto_app/query/service.py`：

1. 顶部 import 加 `from collections import Counter`。
2. 常量区（`PENDING_SQL` 附近）加：

```python
SCREENING_COLUMNS = {"job_id", "rules_version", "outcome", "reasons", "signals", "screened_at"}
SPONSORSHIP = ("offers", "no", "unknown", "citizenship_required")
SCREENED_OUT_SQL = "EXISTS (SELECT 1 FROM job_screenings s WHERE s.job_id=j.job_id AND s.outcome='excluded')"
SPONSORSHIP_SQL = ("EXISTS (SELECT 1 FROM job_screenings s WHERE s.job_id=j.job_id "
                   "AND json_extract(s.signals, '$.sponsorship')=?)")
```

3. `__init__` 里 `self.ingestion_available = ...` 那段之后加：

```python
            screening_columns = {row["name"] for row in self._conn.execute('PRAGMA table_info("job_screenings")')}
            self.screening_available = SCREENING_COLUMNS <= screening_columns
```

4. `context()`：构造 `result` 之后、`return` 之前加：

```python
            result["capabilities"]["rules_screening"] = self.screening_available
            if self.screening_available:
                result["screening"] = self._screening_summary(result["counts"]["active"])
```

5. 新增两个方法（放在 `_ingestion_summary` 后面）：

```python
    def _screening_summary(self, active):
        """Current verdicts of active jobs; `unscreened` = active jobs the worker hasn't screened yet."""
        live = "FROM job_screenings s JOIN jobs j ON j.job_id=s.job_id WHERE j.archived_at IS NULL"
        counts = dict(self._conn.execute(f"SELECT s.outcome, COUNT(*) {live} GROUP BY s.outcome"))
        reasons = Counter()
        for (value,) in self._conn.execute(f"SELECT s.reasons {live} AND s.outcome='excluded'"):
            reasons.update(r.split(":", 1)[0] for r in _json(value, list, "job_screenings.reasons") or [])
        sponsorship = dict(self._conn.execute(
            f"SELECT json_extract(s.signals, '$.sponsorship'), COUNT(*) {live} AND s.outcome='kept' GROUP BY 1"))
        kept, excluded = counts.get("kept", 0), counts.get("excluded", 0)
        return {"kept": kept, "excluded": excluded, "unscreened": max(0, active - kept - excluded),
                "excluded_by_reason": dict(reasons.most_common()), "kept_by_sponsorship": sponsorship}

    def _screening_projection(self, job_ids):
        placeholders = ",".join("?" for _ in job_ids)
        result = {}
        for row in self._conn.execute(f"""SELECT job_id, rules_version, outcome, reasons, signals, screened_at
                                          FROM job_screenings WHERE job_id IN ({placeholders})""", job_ids):
            entry = _dates(row)
            entry["reasons"] = _json(entry["reasons"], list, "job_screenings.reasons") or []
            entry["signals"] = _json(entry["signals"], dict, "job_screenings.signals") or {}
            result[entry.pop("job_id")] = entry
        return result
```

6. `jobs()`：
   - 签名改为 `def jobs(self, query=None, state=None, company=None, source=None, archived=False, limit=50, cursor=None, include_excluded=False, sponsorship=None) -> dict:`。
   - 在 `state` 校验之后加：

```python
        if sponsorship is not None and sponsorship not in SPONSORSHIP:
            raise QueryError(f"sponsorship must be one of: {', '.join(SPONSORSHIP)}")
        if sponsorship is not None and not self.screening_available:
            raise QueryError("sponsorship filtering needs the worker's screening table (run `auto-app worker run`)")
```

   - `filters` 字典里加 `"sponsorship": sponsorship`。
   - 在 `if self.preferences.exclude_countries and not include_excluded:` 那段之后加：

```python
            if self.screening_available and not include_excluded:
                conditions.append(f"NOT {SCREENED_OUT_SQL}")
            if sponsorship is not None:
                conditions.append(SPONSORSHIP_SQL)
                args.append(sponsorship)
```

7. `_project()`：
   - 在 `ingestion = ...` 那行之后加 `screenings = self._screening_projection(ids) if self.screening_available else {}`。
   - 在 `item["screening"] = self.preferences.evaluate(...)` 之后加：

```python
            rule = screenings.get(jid)
            if rule is not None:
                country = item["screening"]
                item["screening"] = {**country, "excluded": country["excluded"] or rule["outcome"] == "excluded",
                                     "reasons": [*country["reasons"], *rule["reasons"]], "signals": rule["signals"],
                                     "rules_version": rule["rules_version"], "screened_at": rule["screened_at"]}
```

修改 `src/auto_app/query/cli.py`：
- `jobs.add_argument("--include-excluded", ...)` 的 help 改为 `"Audit jobs hidden by country preferences or screening rules."`，并在它后面加：

```python
    jobs.add_argument("--sponsorship", choices=["offers", "no", "unknown", "citizenship_required"],
                      help="Only jobs with this visa-sponsorship signal (from the worker's screening).")
```

- `main()` 里的 `service.jobs(...)` 调用加上 `sponsorship=args.sponsorship`。

修改 `src/auto_app/query/api.py`：`jobs()` 参数加 `sponsorship: Literal["offers", "no", "unknown", "citizenship_required"] | None = None`，并把 `sponsorship=sponsorship` 传给 `service.jobs`。

修改 `src/auto_app/agent/mcp_server.py`：
- `search_jobs` 参数加 `sponsorship: Literal["offers", "no", "unknown", "citizenship_required"] | None = None`，传给 `service.search_jobs(..., sponsorship=sponsorship)`。
- `search_jobs` 的 docstring 改为：

```python
        """Search saved jobs with stable IDs and provenance; reuse filters with the next_cursor.

        Country exclusions and screening-rule exclusions apply by default; include_excluded audits
        hidden jobs, each with screening.reasons. sponsorship filters by the visa-sponsorship signal
        (offers, no, unknown, citizenship_required). archived=false searches active jobs, true archived
        jobs, null both. Explicit state='archived' searches archived jobs regardless of that flag.
        """
```

- `get_job_context` 的 docstring 改为 `"""Get job counts, source health, country preferences, screening counts and available capabilities."""`。

- [ ] **Step 4: 运行，确认通过；再跑查询和 agent 的全部测试**

Run: `uv run pytest tests/query tests/agent -q`
Expected: PASS

另外确认标准库路径仍然可用：运行 `python3 -S scripts/job_view.py --db /tmp/does-not-exist.db context`，应该报 "Cannot open read-only database" 并返回非零，而不是 ImportError。

- [ ] **Step 5: Commit（仅在用户批准提交时）**

```bash
git add src/auto_app/query src/auto_app/agent/mcp_server.py tests/screening_db.py tests/query/test_screening.py tests/agent/test_mcp_screening.py
git commit -m "feat(query): hide rule-excluded jobs and filter by sponsorship signal

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Discord 推送与告警

**Files:**
- Create: `src/auto_app/core/discord.py`、`src/auto_app/worker/__init__.py`、`src/auto_app/worker/digest.py`
- Test: `tests/worker/__init__.py`（空文件）、`tests/worker/test_discord.py`、`tests/worker/test_digest.py`

**Interfaces:**
- Consumes: 现有的 `store.jobs(include_excluded=False)`、`store.set_notified(job_ids, when)`；Task 1 的 `job_screenings`；Task 3 的 `chunks`。
- Produces:
  - `WEBHOOK_ENV = "AUTO_APP_DISCORD_WEBHOOK_URL"`、`DiscordError(RuntimeError)`、`MAX_EMBEDS = 5`（每条消息最多 5 个 embed，保证总长度小于 6000 字符）
  - `Discord(webhook_url: str, client: httpx.Client | None = None, sleep=time.sleep)`，方法 `post(content: str | None = None, embeds: list[dict] | None = None) -> None`
  - `READY_AFTER = timedelta(hours=2)`
  - `pending(store, limit: int, since: datetime | None = None, now: datetime | None = None) -> tuple[list[dict], int]`：返回（这次要推的岗位，还剩多少没推）。
    - 推送条件：规则 `kept`；状态是 `not_evaluated` 或 `recommended`（也就是用户没做决定，也没被用户自己的筛选器判为不推荐）；未通知；`first_seen_at >= since`。
    - 没有 JD 的岗位要等 `first_seen_at` 过了 `READY_AFTER` 才推。
  - `embed(job: dict) -> dict`
  - `send_digest(store, discord, *, limit: int = 30, since: datetime | None = None, now: datetime | None = None) -> dict`：返回 `{"sent": n, "waiting": m}`，失败时多一个 `"error"`。
  - `send_alerts(discord, state: dict, report: dict, ingest: dict, now: datetime) -> list[str]`：同一问题 12 小时内只发一次；返回这次发出的告警 key。

- [ ] **Step 1: 写失败测试**

```python
# tests/worker/test_discord.py
import json

import httpx
import pytest

from auto_app.core.discord import Discord, DiscordError

URL = "https://discord.com/api/webhooks/1/secret-token"


def discord(handler, slept=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Discord(URL, client=client, sleep=(slept if slept is not None else []).append)


def test_rate_limits_are_waited_out_and_nobody_is_pinged():
    calls, slept = [], []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(429, json={"retry_after": 0.5}) if len(calls) == 1 else httpx.Response(200, json={})

    discord(handler, slept).post("hello", [{"title": "x"}])
    assert slept == [0.5] and len(calls) == 2
    assert calls[0]["allowed_mentions"] == {"parse": []} and calls[0]["embeds"] == [{"title": "x"}]


def test_rejections_raise_without_leaking_the_webhook_url():
    with pytest.raises(DiscordError) as failure:
        discord(lambda request: httpx.Response(400, text="bad embed")).post("hello")
    assert "secret-token" not in str(failure.value) and "400" in str(failure.value)


def test_only_discord_webhook_urls_are_accepted():
    with pytest.raises(DiscordError):
        Discord("https://example.com/hook")
```

```python
# tests/worker/test_digest.py
"""Digest: kept, undecided new jobs, sponsorship first; marked notified only after Discord accepted them."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select

from auto_app.core.db import tables as T
from auto_app.core.discord import DiscordError
from auto_app.core.models import utcnow
from auto_app.worker.digest import embed, pending, send_alerts, send_digest
from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


class FakeDiscord:
    def __init__(self, fail_on: int | None = None):
        self.posts, self.fail_on = [], fail_on

    def post(self, content=None, embeds=None):
        if self.fail_on is not None and len(self.posts) == self.fail_on:
            raise DiscordError("Discord rejected the message: HTTP 500")
        self.posts.append((content, embeds or []))


def screened(store, rows):
    """rows: (title, sponsorship or None for "not screened yet", has_description); screened jobs are kept."""
    ingest(store, [record(str(100 + i), title) for i, (title, _, _) in enumerate(rows)])
    ids = {title: job_id_of(store, title) for title, _, _ in rows}
    with store.engine.begin() as conn:
        for title, sponsorship, described in rows:
            if sponsorship is not None:
                conn.execute(insert(T.job_screenings).values(
                    job_id=ids[title], rules_version="t", facts_hash="h", outcome="kept", reasons=[],
                    signals={"sponsorship": sponsorship, "has_description": described}, screened_at=utcnow()))
    return ids


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    yield result
    result.engine.dispose()


def notified(store):
    with store.engine.connect() as conn:
        return set(conn.execute(select(T.jobs.c.job_id).where(T.jobs.c.notified_at.is_not(None))).scalars())


def test_sponsoring_jobs_come_first_and_handled_jobs_never(store):
    ids = screened(store, [("Backend Engineer", "no", True), ("Data Engineer", "offers", True),
                           ("ML Engineer", "unknown", True), ("Web Developer", "offers", True),
                           ("QA Engineer", "offers", True), ("SRE", "offers", True),
                           ("Unscreened Engineer", None, True)])
    store.set_job_state(ids["Web Developer"], decision="want_to_apply")  # you decided
    store.set_job_state(ids["SRE"], step2="un_recommended")  # your own filter said no
    with store.engine.begin() as conn:
        conn.execute(T.job_screenings.update().where(T.job_screenings.c.job_id == ids["QA Engineer"])
                     .values(outcome="excluded"))
    jobs, more = pending(store, limit=10)
    assert [j["title"] for j in jobs] == ["Data Engineer", "ML Engineer", "Backend Engineer"] and more == 0


def test_your_filters_recommendations_come_first_and_are_marked(store):
    ids = screened(store, [("Backend Engineer", "offers", True), ("Data Engineer", "offers", True)])
    store.set_job_state(ids["Data Engineer"], step2="recommended")
    jobs, _ = pending(store, limit=10)
    assert [j["title"] for j in jobs] == ["Data Engineer", "Backend Engineer"]
    assert embed(jobs[0])["fields"][-1]["value"].startswith("⭐ your filter recommends it")


def test_title_only_jobs_wait_for_enrichment(store):
    screened(store, [("Backend Engineer", "unknown", False)])
    assert pending(store, limit=10, now=utcnow()) == ([], 0)
    later, _ = pending(store, limit=10, now=utcnow() + timedelta(hours=3))
    assert later[0]["title_only"] and "title only" in embed(later[0])["fields"][-1]["value"]


def test_digest_posts_in_batches_and_marks_what_was_delivered(store):
    screened(store, [(f"Engineer {i}", "offers", True) for i in range(7)])
    fake = FakeDiscord()
    report = send_digest(store, fake, limit=30, now=NOW)
    assert report == {"sent": 7, "waiting": 0}
    assert [len(embeds) for _, embeds in fake.posts] == [5, 2]
    assert fake.posts[0][0].startswith("**7 new job(s)**") and fake.posts[1][0] is None
    assert fake.posts[0][1][0]["fields"][1]["value"] == "🟢 sponsors visas"
    assert fake.posts[0][1][0]["url"] == "https://job-boards.greenhouse.io/acme/jobs/100"
    assert len(notified(store)) == 7
    assert send_digest(store, fake, now=NOW) == {"sent": 0, "waiting": 0}


def test_a_failed_post_marks_only_the_delivered_jobs(store):
    screened(store, [(f"Engineer {i}", "unknown", True) for i in range(7)])
    report = send_digest(store, FakeDiscord(fail_on=1), limit=30, now=NOW)
    assert report["sent"] == 5 and report["waiting"] == 2 and "HTTP 500" in report["error"]
    assert len(notified(store)) == 5
    assert send_digest(store, FakeDiscord(), now=NOW)["sent"] == 2


def test_jobs_seen_before_since_are_not_announced(store):
    screened(store, [("Backend Engineer", "offers", True)])
    assert pending(store, limit=10, since=utcnow() + timedelta(minutes=1)) == ([], 0)


def test_the_same_problem_is_alerted_once_per_twelve_hours():
    fake, state = FakeDiscord(), {}
    report = {"steps": {"enrich": {"status": "error", "error": "OperationalError: database is locked"}}}
    ingest_state = {"consecutive_failures": 3}
    assert len(send_alerts(fake, state, report, ingest_state, NOW)) == 2
    assert send_alerts(fake, state, report, ingest_state, NOW + timedelta(hours=1)) == []
    assert len(send_alerts(fake, state, report, ingest_state, NOW + timedelta(hours=13))) == 2
    assert len(fake.posts) == 4 and fake.posts[0][0].startswith("⚠️")
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/worker/test_discord.py tests/worker/test_digest.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'auto_app.core.discord'`

- [ ] **Step 3: 实现**

```python
# src/auto_app/core/discord.py
"""Post to a Discord channel through an incoming webhook (docs/linux-worker.md §3).

One-way messages only. The webhook URL is a secret: it comes from the environment
(AUTO_APP_DISCORD_WEBHOOK_URL, from the worker's systemd EnvironmentFile), never from the repository,
and it never appears in an error message or a log line.
"""

import time

import httpx

WEBHOOK_ENV = "AUTO_APP_DISCORD_WEBHOOK_URL"
PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
MAX_EMBEDS = 5  # per message: keeps one message under Discord's 6000-character total
MAX_TRIES = 3


class DiscordError(RuntimeError):
    pass


class Discord:
    def __init__(self, webhook_url: str, client: httpx.Client | None = None, sleep=time.sleep):
        if not webhook_url.startswith(PREFIXES):
            raise DiscordError(f"{WEBHOOK_ENV} must be a Discord webhook URL (https://discord.com/api/webhooks/…)")
        self.url, self.client, self.sleep = webhook_url, client or httpx.Client(timeout=15), sleep

    def post(self, content: str | None = None, embeds: list[dict] | None = None) -> None:
        body: dict = {"allowed_mentions": {"parse": []}}  # never @everyone / @here / users
        if content:
            body["content"] = content[:2000]
        if embeds:
            body["embeds"] = embeds[:MAX_EMBEDS]
        for attempt in range(MAX_TRIES):
            try:
                response = self.client.post(self.url, params={"wait": "true"}, json=body)
            except httpx.HTTPError as exc:
                if attempt == MAX_TRIES - 1:
                    raise DiscordError(f"Discord unreachable: {type(exc).__name__}") from None
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 429:
                try:
                    retry = float(response.json().get("retry_after", 1))
                except ValueError:
                    retry = 1.0
                self.sleep(min(retry, 30))
                continue
            if response.is_success:
                return
            raise DiscordError(f"Discord rejected the message: HTTP {response.status_code} {response.text[:200]}")
        raise DiscordError("Discord kept rate-limiting the webhook")
```

```python
# src/auto_app/worker/__init__.py
"""The Linux worker: screen, enrich and tell the user on Discord — no model calls (docs/linux-worker.md)."""
```

```python
# src/auto_app/worker/digest.py
"""New jobs to Discord, visa sponsorship first; alerts for what needs the user (docs/linux-worker.md §3).

Announced: active jobs the screening rules kept, undecided and not judged out by the user's own filters
(state not_evaluated or recommended), not yet notified, first seen after `since` — the backlog that existed
when the worker started is for browsing, not pushing. A job without a description waits READY_AFTER for
enrichment, then goes out title-only. A job is marked notified (jobs.notified_at — the same mark as
`job notified`) only after Discord accepted the message that carried it; a failed post is retried next round.
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from auto_app.core.db import tables as T
from auto_app.core.discord import MAX_EMBEDS, DiscordError
from auto_app.core.models import utcnow
from auto_app.screen.store import chunks

OPEN_STATES = ("not_evaluated", "recommended")
ORDER = {"offers": 0, "unknown": 1, "no": 2}
BADGE = {"offers": "🟢 sponsors visas", "unknown": "⚪ sponsorship not stated", "no": "🟡 no sponsorship",
         "citizenship_required": "⛔ citizenship required"}
COLOR = {"offers": 0x2ECC71, "unknown": 0x95A5A6, "no": 0xF1C40F, "citizenship_required": 0xE74C3C}
READY_AFTER = timedelta(hours=2)
ALERT_EVERY = timedelta(hours=12)


def _when(value) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def pending(store, limit: int, since: datetime | None = None,
            now: datetime | None = None) -> tuple[list[dict], int]:
    """(jobs to announce now, how many more wait)."""
    now = now or utcnow()
    jobs = [j for j in store.jobs(include_excluded=False)
            if j["state"] in OPEN_STATES and not j.get("notified_at")
            and (since is None or (_when(j.get("first_seen_at")) or since) >= since)]
    s, jd, p = T.job_screenings, T.judgments, T.postings
    verdicts, reasons, links = {}, {}, {}
    with store.engine.connect() as conn:
        for part in chunks([j["job_id"] for j in jobs]):
            for r in conn.execute(select(s.c.job_id, s.c.outcome, s.c.signals).where(s.c.job_id.in_(part))):
                verdicts[r.job_id] = (r.outcome, r.signals or {})
            for r in conn.execute(select(jd.c.job_id, jd.c.reason).where(
                    jd.c.job_id.in_(part), jd.c.recommend.is_not(False), jd.c.reason.is_not(None))
                    .order_by(jd.c.created_at)):
                reasons[r.job_id] = r.reason  # the latest recommending verdict wins
            for r in conn.execute(select(p.c.job_id, p.c.apply_url).where(
                    p.c.job_id.in_(part), p.c.apply_url.is_not(None)).order_by(p.c.posting_id)):
                links.setdefault(r.job_id, r.apply_url)
    out = []
    for j in jobs:
        outcome, signals = verdicts.get(j["job_id"], (None, {}))
        if outcome != "kept":
            continue  # not screened yet, or screened out
        described = bool(signals.get("has_description"))
        if not described and now - (_when(j.get("first_seen_at")) or now) < READY_AFTER:
            continue  # enrichment may still bring the description (and the rules that need it)
        recommended = j["state"] == "recommended"
        out.append({**j, "sponsorship": signals.get("sponsorship", "unknown"),
                    "years_required": signals.get("years_required"), "title_only": not described,
                    "recommended": recommended, "reason": reasons.get(j["job_id"]) if recommended else None,
                    "link": j.get("canonical_url") or links.get(j["job_id"])})
    # Stable sorts, last key first: sponsorship, then your filters' picks, then newest.
    out.sort(key=lambda j: j.get("posted_at") or "", reverse=True)
    out.sort(key=lambda j: not j["recommended"])
    out.sort(key=lambda j: ORDER.get(j["sponsorship"], 3))
    return out[:limit], max(0, len(out) - limit)


def _where(locations: list[str]) -> str:
    if not locations:
        return "not stated"
    return locations[0] + (f" +{len(locations) - 1}" if len(locations) > 1 else "")


def embed(job: dict) -> dict:
    sponsorship = job.get("sponsorship", "unknown")
    fields = [{"name": "Where", "value": _where(job.get("locations") or [])[:100], "inline": True},
              {"name": "Visa", "value": BADGE.get(sponsorship, BADGE["unknown"]), "inline": True},
              {"name": "Posted", "value": (job.get("posted_at") or "not stated")[:10], "inline": True}]
    notes = []
    if job.get("recommended"):
        notes.append("⭐ your filter recommends it" + (f": {job['reason'][:250]}" if job.get("reason") else ""))
    if job.get("years_required"):
        notes.append(f"asks for {job['years_required']}+ years of experience")
    if job.get("title_only"):
        notes.append("title only — open the link for the description")
    if notes:
        fields.append({"name": "Notes", "value": "\n".join(notes)[:400]})
    result = {"title": f"#{job['job_id']} {job.get('title') or 'Untitled'} — {job.get('company') or '?'}"[:256],
              "color": COLOR.get(sponsorship, COLOR["unknown"]), "fields": fields,
              "footer": {"text": f"job {job['job_id']}"}}
    if job.get("link"):
        result["url"] = job["link"]
    return result


def send_digest(store, discord, *, limit: int = 30, since: datetime | None = None,
                now: datetime | None = None) -> dict:
    now = now or utcnow()
    jobs, more = pending(store, limit, since, now)
    sent = 0
    for i in range(0, len(jobs), MAX_EMBEDS):
        batch = jobs[i:i + MAX_EMBEDS]
        header = f"**{len(jobs) + more} new job(s)** — visa sponsorship first" if i == 0 else None
        try:
            discord.post(header, [embed(j) for j in batch])
        except DiscordError as exc:
            return {"sent": sent, "waiting": len(jobs) - sent + more, "error": str(exc)}
        store.set_notified([j["job_id"] for j in batch], now)
        sent += len(batch)
    if more:
        try:
            discord.post(f"…and {more} more — they come in the next digest, or ask in chat.")
        except DiscordError:
            pass  # the jobs themselves are still unmarked: nothing is lost
    return {"sent": sent, "waiting": more}


def _problems(report: dict, ingest: dict) -> dict[str, str]:
    out = {}
    for name, step in (report.get("steps") or {}).items():
        if step.get("status") == "error":
            error = step.get("error") or ""
            out[f"{name}:{error[:60]}"] = f"worker step `{name}` failed: {error[:300]}"
    if (ingest or {}).get("consecutive_failures", 0) >= 3:
        out["ingest:failing"] = (f"the feed worker failed {ingest['consecutive_failures']} rounds in a row "
                                 f"(`auto-app ingest status`)")
    return out


def send_alerts(discord, state: dict, report: dict, ingest: dict, now: datetime) -> list[str]:
    if discord is None:
        return []
    sent_at = state.setdefault("alerts", {})
    sent = []
    for key, message in _problems(report, ingest).items():
        last = _when(sent_at.get(key))
        if last and now - last < ALERT_EVERY:
            continue
        try:
            discord.post(f"⚠️ {message}"[:2000])
        except DiscordError:
            break
        sent_at[key] = now.isoformat()
        sent.append(key)
    for key in [k for k, v in sent_at.items() if now - _when(v) > 2 * ALERT_EVERY]:
        del sent_at[key]  # keep the state file small
    return sent
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/worker/test_discord.py tests/worker/test_digest.py -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准提交时）**

```bash
git add src/auto_app/core/discord.py src/auto_app/worker/__init__.py src/auto_app/worker/digest.py tests/worker
git commit -m "feat(worker): Discord digest of new screened jobs and deduplicated alerts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: worker 的一轮、循环和 CLI

**Files:**
- Create: `src/auto_app/worker/round.py`、`src/auto_app/worker/loop.py`、`src/auto_app/worker/cli.py`、`deploy/auto-app-worker.service`
- Modify:
  - `src/auto_app/ingest/scheduler.py`：`_read_state`、`_write_state`、`_running` 加 `suffix` 参数
  - `src/auto_app/cli.py`：注册 `worker`
  - `tests/test_cli_structure.py`：`TREE` 加 `"worker": ["run", "watch", "status"]`
- Test: `tests/worker/test_round.py`、`tests/worker/test_loop.py`、`tests/worker/test_cli.py`

**Interfaces:**
- Consumes: Task 3 的 `screen_jobs`、Task 2 的 `load_rules`；Task 4 的 `Fetcher`；Task 5 的 `enrich_jobs`；Task 8 的 `send_digest`、`send_alerts`、`Discord`、`WEBHOOK_ENV`；现有的 `scheduler.status`。
- Produces:
  - `STEPS = ("screen", "enrich", "notify")`
  - `WorkerOptions(screening_path: Path, enrich_limit=200, digest_limit=30, steps=STEPS)`
  - `run_round(store, opts, *, fetcher, discord, state: dict, now=None) -> dict`
    - 返回 `{"status": "ok" | "partial", "started_at", "steps": {name: {"status": "ok" | "error" | "skipped", …}}}`。
    - `state` 原地更新：首轮写入 `notify_since`。
  - `loop.watch(db_path, round_fn, *, interval_seconds=900, once=False, clock=time.time, sleep=time.sleep, should_stop=None, emit=None) -> dict`；`loop.status(db_path) -> dict`
  - CLI：
    - `auto-app worker run [--steps screen,enrich,notify] [--enrich-limit N] [--notify-since ISO]`
    - `auto-app worker watch --interval 900`
    - `auto-app worker status`

- [ ] **Step 1: 写失败测试**

```python
# tests/worker/test_round.py
"""One worker round: the steps in order, each isolated; no backlog flood."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from auto_app.core.models import utcnow
from auto_app.worker.round import WorkerOptions, run_round
from tests.imported_jobs import FakeFetcher, ingest, make_store, ok, record
from tests.worker.test_digest import FakeDiscord

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
LONG_AGO = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Software Engineer, New Grad"), record("102", "Software Engineer Intern")])
    yield result
    result.engine.dispose()


def options(tmp_path, **kw):
    return WorkerOptions(screening_path=tmp_path / "config" / "screening.json", **kw)


def test_a_round_screens_enriches_and_notifies(store, tmp_path):
    fetcher = FakeFetcher(ok("Build APIs in Python. Visa sponsorship is available."))
    discord = FakeDiscord()
    report = run_round(store, options(tmp_path), fetcher=fetcher, discord=discord,
                       state={"notify_since": LONG_AGO}, now=NOW)
    assert list(report["steps"]) == ["screen", "enrich", "rescreen", "notify"]
    assert report["status"] == "ok"
    assert report["steps"]["screen"]["excluded"] == 1  # the internship
    assert len(fetcher.calls) == 1  # the internship is never fetched
    assert report["steps"]["notify"]["sent"] == 1
    first = discord.posts[0][1][0]
    assert first["title"].endswith("Software Engineer, New Grad — Acme")
    assert first["fields"][1]["value"] == "🟢 sponsors visas"  # from the enriched description


def test_a_locked_database_in_one_step_does_not_stop_the_others(store, tmp_path, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("auto_app.screen.store.screen_jobs", locked)
    report = run_round(store, options(tmp_path), fetcher=FakeFetcher(ok("Build APIs.")), discord=FakeDiscord(),
                       state={}, now=NOW)
    assert report["status"] == "partial"
    assert report["steps"]["screen"]["status"] == "error"
    assert "database is locked" in report["steps"]["screen"]["error"]
    assert {"enrich", "notify"} <= set(report["steps"])


def test_first_round_does_not_push_the_backlog(store, tmp_path):
    later = utcnow() + timedelta(hours=3)  # past the title-only wait
    discord, state = FakeDiscord(), {}
    run_round(store, options(tmp_path, steps=("screen", "notify")), fetcher=None, discord=discord,
              state=state, now=later)
    assert discord.posts == [] and state["notify_since"] == later.isoformat()
    state["notify_since"] = LONG_AGO
    report = run_round(store, options(tmp_path, steps=("notify",)), fetcher=None, discord=discord,
                       state=state, now=later)
    assert report["steps"]["notify"]["sent"] == 1  # the new-grad job; the internship is screened out


def test_without_a_webhook_notify_is_skipped(store, tmp_path):
    report = run_round(store, options(tmp_path, steps=("screen", "notify")), fetcher=None, discord=None,
                       state={}, now=NOW)
    assert report["steps"]["notify"]["status"] == "skipped" and report["status"] == "ok"
```

```python
# tests/worker/test_loop.py
from auto_app.worker import loop


def test_watch_once_persists_state_and_status_reads_it(tmp_path):
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    rounds = []
    state = loop.watch(db, lambda st: rounds.append(dict(st)) or {"status": "ok"}, once=True, clock=lambda: 1000.0)
    assert len(rounds) == 1 and state["status"] == "ok" and state["consecutive_failures"] == 0
    saved = loop.status(db)
    assert saved["status"] == "ok" and saved["running"] is False and saved["last_report"] == {"status": "ok"}


def test_a_restart_waits_for_the_saved_next_round_and_failures_count(tmp_path):
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    loop.watch(db, lambda st: {"status": "partial"}, once=True, clock=lambda: 1000.0, interval_seconds=900)
    calls, sleeps, stops = [], [], iter([False, True])
    loop.watch(db, lambda st: calls.append(1) or {"status": "ok"}, clock=lambda: 1100.0, sleep=sleeps.append,
               should_stop=lambda: next(stops))
    assert calls == [] and sleeps == [30]
    assert loop.status(db)["consecutive_failures"] == 1


def test_status_before_any_round(tmp_path):
    assert loop.status(tmp_path / "data" / "auto_app.db")["status"] == "not_started"
```

```python
# tests/worker/test_cli.py
import json

from typer.testing import CliRunner

from auto_app.cli import app


def test_worker_run_screen_only_reports_json(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    monkeypatch.delenv("AUTO_APP_DISCORD_WEBHOOK_URL", raising=False)
    db = tmp_path / "data" / "auto_app.db"
    result = CliRunner().invoke(app, ["worker", "run", "--db", str(db), "--steps", "screen"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output.strip().splitlines()[-1])
    assert report["status"] == "ok" and list(report["steps"]) == ["screen"]
    status = CliRunner().invoke(app, ["worker", "status", "--db", str(db)])
    assert json.loads(status.output)["status"] == "ok"


def test_unknown_steps_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["worker", "run", "--db", str(tmp_path / "x.db"), "--steps", "screen,judge"])
    assert result.exit_code != 0 and "judge" in result.output
```

在 `tests/test_cli_structure.py` 的 `TREE` 里加一行：`"worker": ["run", "watch", "status"],`

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/worker tests/test_cli_structure.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'auto_app.worker.round'`

- [ ] **Step 3: 实现**

修改 `src/auto_app/ingest/scheduler.py`：给三个函数加 `suffix` 参数。默认值保持原来的文件名，现有调用不变：

```python
STATE_SUFFIX = ".ingest-state.json"
WATCH_LOCK_SUFFIX = ".ingest-watch.lock"


def _read_state(db_path: Path, suffix: str = STATE_SUFFIX) -> dict:
    path = _path(db_path, suffix)
    # … 函数体其余部分不变 …


def _running(db_path: Path, suffix: str = WATCH_LOCK_SUFFIX) -> bool:
    try:
        with _path(db_path, suffix).open("rb") as handle:
    # … 函数体其余部分不变 …


def _write_state(db_path: Path, state: dict, suffix: str = STATE_SUFFIX) -> None:
    path = _path(db_path, suffix)
    # … 函数体其余部分不变 …
```

另外，`watch()` 里的 `_lock(_path(db_path, ".ingest-watch.lock"), …)` 改成 `_lock(_path(db_path, WATCH_LOCK_SUFFIX), …)`。

```python
# src/auto_app/worker/round.py
"""One round of the Linux worker (docs/linux-worker.md): screen → enrich → screen again → Discord.

No model or agent is called here: model filtering is the user's own `auto-app filter run`, when their
quota allows. Steps are isolated: a failing step is reported and the next ones still run, so a Discord
outage never stops enrichment. Nothing here applies to a job.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from auto_app.core.models import utcnow

STEPS = ("screen", "enrich", "notify")


@dataclass
class WorkerOptions:
    screening_path: Path
    enrich_limit: int = 200
    digest_limit: int = 30
    steps: tuple[str, ...] = STEPS


def run_round(store, opts: WorkerOptions, *, fetcher, discord, state: dict, now: datetime | None = None) -> dict:
    """fetcher: enrich.fetch.Fetcher or None (no enrich step); discord: core.discord.Discord or None.
    `state` is the worker's saved state, updated in place."""
    from auto_app.enrich.service import enrich_jobs
    from auto_app.screen import store as screening
    from auto_app.screen.rules import load_rules

    now = now or utcnow()
    state.setdefault("notify_since", now.isoformat())  # the first round: today's backlog is not pushed
    report = {"status": "ok", "started_at": now.isoformat(), "steps": {}}

    def step(name, work):
        try:
            result = work()
        except Exception as exc:  # reported; the next steps still run
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:400]}
        report["steps"][name] = result
        return result

    def screen():
        return {"status": "ok", **screening.screen_jobs(store, load_rules(opts.screening_path), now=now)}

    if "screen" in opts.steps:
        step("screen", screen)
    if "enrich" in opts.steps and fetcher is not None:
        enriched = step("enrich", lambda: {"status": "ok",
                                           **enrich_jobs(store, fetcher, limit=opts.enrich_limit, now=now)})
        if "screen" in opts.steps and enriched.get("jobs_enriched"):  # new descriptions: their facts changed
            step("rescreen", screen)
    if "notify" in opts.steps:
        step("notify", lambda: _notify(store, discord, opts, state, now))
    if any(s["status"] == "error" for s in report["steps"].values()):
        report["status"] = "partial"
    return report


def _notify(store, discord, opts: WorkerOptions, state: dict, now: datetime) -> dict:
    from auto_app.worker.digest import send_digest

    if discord is None:
        return {"status": "skipped", "reason": "no Discord webhook configured (AUTO_APP_DISCORD_WEBHOOK_URL)"}
    result = send_digest(store, discord, limit=opts.digest_limit,
                         since=datetime.fromisoformat(state["notify_since"]), now=now)
    return {"status": "error" if result.get("error") else "ok", **result}
```

```python
# src/auto_app/worker/loop.py
"""The worker's durable loop: one round now, then one every interval (docs/linux-worker.md §4).

Same mechanics as the feed worker (ingest/scheduler.py): a process lock beside the database, a JSON state
file written atomically, and a restart that waits for the saved next-run time.
"""

import os
import time
from pathlib import Path

from auto_app.ingest.scheduler import (_due, _lock, _path, _read_state, _running, _stamp, _write_state,
                                       validate_interval)

STATE = ".worker-state.json"
LOCK = ".worker.lock"


def status(db_path: Path) -> dict:
    """Read the state file and lock without opening the database."""
    try:
        return {**_read_state(Path(db_path), STATE), "running": _running(Path(db_path), LOCK)}
    except (OSError, ValueError) as exc:
        return {"status": "error", "running": False, "error": str(exc)}


def watch(db_path: Path, round_fn, *, interval_seconds: float = 900, once: bool = False, clock=time.time,
          sleep=time.sleep, should_stop=None, emit=None) -> dict:
    """round_fn(state) -> report; `state` is persisted after every round (round_fn may update it)."""
    validate_interval(interval_seconds)
    should_stop = should_stop or (lambda: False)
    db_path = Path(db_path).expanduser().resolve()
    with _lock(_path(db_path, LOCK), "worker"):
        state = _read_state(db_path, STATE)
        due = 0 if once else _due(state.get("next_run_at"))
        while not should_stop():
            remaining = due - clock()
            if remaining > 0:
                sleep(min(remaining, 30))
                continue
            state.update(status="running", last_run_at=_stamp(clock()), pid=os.getpid())
            _write_state(db_path, state, STATE)
            interrupted = False
            try:
                report = round_fn(state)
            except KeyboardInterrupt:
                interrupted, report = True, {"status": "interrupted"}
            except Exception as exc:
                report = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:400]}
            finished = clock()
            due = finished + interval_seconds
            failures = 0 if report["status"] == "ok" else state.get("consecutive_failures", 0) + 1
            state.update(status=report["status"], last_finished_at=_stamp(finished), next_run_at=_stamp(due),
                         consecutive_failures=failures, last_report=report)
            _write_state(db_path, state, STATE)
            if emit is not None:
                emit(report)
            if once or interrupted:
                break
    return state
```

```python
# src/auto_app/worker/cli.py
"""`auto-app worker`: screen, enrich and tell you on Discord — on the Linux host (docs/linux-worker.md)."""

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from auto_app.settings import get_paths
from auto_app.worker.round import STEPS, WorkerOptions

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="The Linux worker: screen jobs, fetch missing descriptions, tell you on Discord.")
DbOption = Annotated[Path | None, typer.Option("--db", help="Database file; defaults to AUTO_APP_HOME/data/auto_app.db.")]
StepsOption = Annotated[str, typer.Option("--steps", help=f"Comma-separated steps: {', '.join(STEPS)}.")]
EnrichOption = Annotated[int, typer.Option("--enrich-limit", min=0, help="Most employer API requests per round.")]
SinceOption = Annotated[str | None, typer.Option(
    "--notify-since", help="Announce jobs first seen since this ISO time (default: the first round).")]


def _json(report: dict) -> None:
    typer.echo(json.dumps(report, ensure_ascii=False))


def _options(steps: str, enrich_limit: int) -> WorkerOptions:
    chosen = tuple(s.strip() for s in steps.split(",") if s.strip())
    unknown = sorted(set(chosen) - set(STEPS))
    if unknown:
        raise typer.BadParameter(f"unknown steps {unknown}; choose from {', '.join(STEPS)}", param_hint="--steps")
    return WorkerOptions(screening_path=get_paths().home / "config" / "screening.json", enrich_limit=enrich_limit,
                         steps=chosen)


def _since(value: str | None) -> str | None:
    if value is None:
        return None
    when = datetime.fromisoformat(value)
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).isoformat()


def _round_fn(db_path: Path, opts: WorkerOptions, notify_since: str | None):
    def run(state: dict) -> dict:
        from auto_app.core.db.store import DbStore
        from auto_app.core.discord import WEBHOOK_ENV, Discord, DiscordError
        from auto_app.core.models import utcnow
        from auto_app.enrich.fetch import Fetcher
        from auto_app.ingest import scheduler
        from auto_app.worker.digest import send_alerts
        from auto_app.worker.round import run_round

        if notify_since:
            state["notify_since"] = notify_since
        store = DbStore(db_path, get_paths())
        fetcher = Fetcher() if "enrich" in opts.steps else None
        discord, discord_error = None, None
        if os.environ.get(WEBHOOK_ENV):
            try:
                discord = Discord(os.environ[WEBHOOK_ENV])
            except DiscordError as exc:
                discord_error = str(exc)
        try:
            now = utcnow()
            report = run_round(store, opts, fetcher=fetcher, discord=discord, state=state, now=now)
            if discord_error:
                report["discord_error"] = discord_error
            report["alerts"] = send_alerts(discord, state, report, scheduler.status(db_path), now)
            return report
        finally:
            if fetcher is not None:
                fetcher.close()
            store.engine.dispose()
    return run


def _watch(db: Path | None, opts: WorkerOptions, *, interval: int, once: bool, notify_since: str | None) -> dict:
    from auto_app.worker import loop

    db_path = db or get_paths().db
    stopped = False

    def request_stop(*_):
        nonlocal stopped
        stopped = True

    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        return loop.watch(db_path, _round_fn(db_path, opts, _since(notify_since)), interval_seconds=interval,
                          once=once, should_stop=lambda: stopped,
                          sleep=lambda seconds: time.sleep(min(seconds, 0.25)), emit=_json)
    finally:
        signal.signal(signal.SIGTERM, previous)


@app.command("run")
def run(db: DbOption = None, steps: StepsOption = ",".join(STEPS), enrich_limit: EnrichOption = 200,
        notify_since: SinceOption = None) -> None:
    """One round now: screen → enrich → screen again → Discord. Prints the round's JSON report."""
    try:
        result = _watch(db, _options(steps, enrich_limit), interval=900, once=True, notify_since=notify_since)
    except (OSError, ValueError) as exc:
        _json({"status": "error", "error": str(exc)})
        raise typer.Exit(1)
    if result["status"] != "ok":
        raise typer.Exit(1)


@app.command("watch")
def watch(db: DbOption = None, steps: StepsOption = ",".join(STEPS), enrich_limit: EnrichOption = 200,
          notify_since: SinceOption = None,
          interval: Annotated[int, typer.Option("--interval", min=60, help="Seconds between rounds.")] = 900) -> None:
    """Rounds forever (systemd runs this): one now, then one every --interval seconds."""
    try:
        _watch(db, _options(steps, enrich_limit), interval=interval, once=False, notify_since=notify_since)
    except (OSError, ValueError) as exc:
        _json({"status": "error", "error": str(exc)})
        raise typer.Exit(1)


@app.command("status")
def status(db: DbOption = None) -> None:
    """Last/next round, its report, failures and whether a worker is running — without opening the database."""
    from auto_app.worker import loop

    report = loop.status(db or get_paths().db)
    _json(report)
    if report["status"] == "error" and "error" in report:
        raise typer.Exit(1)
```

修改 `src/auto_app/cli.py`：
- 在 `from auto_app.ingest.cli import app as ingest_app` 下面加 `from auto_app.worker.cli import app as worker_app`。
- 在 `app.add_typer(ingest_app, name="ingest")` 下面加 `app.add_typer(worker_app, name="worker")`。

```ini
# deploy/auto-app-worker.service
[Unit]
Description=Job Search worker (screen, enrich, Discord)
Wants=network-online.target
After=network-online.target auto-app-ingest.service

[Service]
Type=simple
WorkingDirectory=%h/Desktop/job-search/auto-application
Environment=AUTO_APP_HOME=%h/Desktop/job-search/runtime
EnvironmentFile=%h/Desktop/job-search/runtime/credentials/worker.env
ExecStart=%h/Desktop/job-search/auto-application/.venv/bin/auto-app worker watch --db %h/Desktop/job-search/runtime/data/auto_app.db --interval 900
Restart=on-failure
RestartSec=30
KillMode=control-group
TimeoutStopSec=120
UMask=0077

[Install]
WantedBy=default.target
```

- [ ] **Step 4: 运行，确认通过；再跑完整测试**

Run: `uv run pytest tests/worker tests/test_cli_structure.py tests/ingest/test_scheduler.py -q`，然后 `uv run pytest -q`
Expected: 全部 PASS（基线 1,035 个测试，加上本计划新增的测试）

- [ ] **Step 5: Commit（仅在用户批准提交时）**

```bash
git add src/auto_app/worker src/auto_app/ingest/scheduler.py src/auto_app/cli.py deploy/auto-app-worker.service tests/worker tests/test_cli_structure.py
git commit -m "feat(worker): screen/enrich/notify rounds with a durable loop and CLI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: 文档

**Files:**
- Create: `docs/linux-worker.md`、`docs/updates/2026-10-12-linux-worker.md`
- Modify:
  - `docs/updates/README.md`：索引加一行
  - `docs/step4-run.md`：末尾加一节，指向 linux-worker.md
  - `README.md`：在 "Gmail, Outlook and recurring ingestion" 一节后面加 "Linux worker" 一节，并更新命令表

- [ ] **Step 1: 写 `docs/linux-worker.md`**

用英文写，风格和 `docs/dot-setup.md` 一致。各章节必须包含的内容：

- **Overview**：
  - 一轮 = screen → enrich → rescreen → notify；每 15 分钟一轮；和 ingest worker 共用数据库。
  - **不调用任何模型或 agent**：LLM 筛选是用户按额度手动运行的 `auto-app filter run`，它会跳过被规则排除的岗位和还没有 JD 的岗位。
  - 不提交任何申请。
- **§1 Enrichment**：
  - 支持的四种链接（照抄 `enrich/targets.py` 的 docstring 表）。
  - 缓存状态 `ok / failed / unsupported / gone` 的含义。
  - 退避：30 分钟 × 2^(n-1)，上限 24 小时。
  - 限速：每轮最多 200 个请求，同一 host 间隔 ≥1 秒；User-Agent。
- **§2 Screening**：
  - `$AUTO_APP_HOME/config/screening.json` 的每个键和默认值（照抄 `config/screening.example.json`）。
  - 每个排除原因的含义：`internship`、`senior_title`、`not_cs_role`、`citizenship_required`、`experience_required:N`、`closed_at_source`。
  - 签证担保信号的四个取值。
  - "排除是视图：改了规则，下一轮生效"。
  - 如何审计：`auto-app-query jobs --include-excluded`，以及每个岗位的 `screening.reasons`。
- **§3 Discord**：
  - 怎么建 webhook：服务器设置 → 整合 → Webhooks → 新建 → 复制 URL。
  - 在 `worker.env` 里写 `AUTO_APP_DISCORD_WEBHOOK_URL=…`，文件权限 600。
  - 哪些岗位会推送：规则保留、状态是 not_evaluated/recommended、未通知、首次出现时间晚于 `notify_since`。
  - 推送顺序：有担保 → 未写明 → 无担保；同一组里你的筛选器推荐的（⭐）在前，然后新的在前。
  - 没有 JD 的岗位等 2 小时，之后以"仅标题"推送。
  - 首轮记下 `notify_since`，之前的存量不推；可以用 `--notify-since` 修改。
  - 告警种类，以及"同一问题 12 小时一次"。
- **§4 Loop and state**：
  - `<db>.worker-state.json` 的各个键：`status`、`last_run_at`、`next_run_at`、`consecutive_failures`、`last_report`、`notify_since`、`alerts`。
  - `auto-app worker status`。
  - 重启后会等到保存的下次运行时间。
- **§5 Deployment**：
  - `scripts/deploy_linux.sh`（见 Task 11）。
  - `deploy/auto-app-worker.service` 的安装命令。
  - 部署前会自动备份数据库。

- [ ] **Step 2: 写其他文档**

`docs/updates/2026-10-12-linux-worker.md`：按现有 updates 条目的格式写（标题、**Step:** 1, 2, 4、**Status:**、做了什么、怎么验证、剩余工作），状态写 "implemented; deployment pending"。

`docs/updates/README.md`：在索引表末尾加一行，指向这个条目。

`docs/step4-run.md`：末尾加一节 `## 15. Linux worker (2026-10-12)`，用两三句话说明它和 `serve` 的关系：`serve` 是 macOS 上带浏览器采集和模型筛选的循环；Linux worker 不采集、不调用模型，只处理 ingest worker 导入的岗位。最后链接到 `linux-worker.md`。

`README.md`：在 "Gmail, Outlook and recurring ingestion" 一节后面加：

````markdown
## Linux worker: screening, JD enrichment and Discord

`auto-app worker` runs on the Linux host next to the feed worker and calls no model. Every 15 minutes it
screens every active job with deterministic rules (`config/screening.json`: internships, senior titles,
non-CS roles, citizenship/clearance requirements, stated experience ≥ 3 years, closed postings), fetches
missing descriptions from Greenhouse, Lever, Ashby and Workday public APIs, screens again, and posts new
kept jobs to Discord — visa sponsorship first, your own filters' recommendations marked ⭐. Exclusions are a
reversible view; nothing is deleted or decided, and nothing is ever applied to. `auto-app filter run`, which
you run when your model quota allows, skips rule-excluded jobs.

```bash
cp config/screening.example.json config/screening.json
uv run auto-app worker run --steps screen               # one round of only the rules
uv run auto-app worker run --enrich-limit 20
uv run auto-app worker status
uv run auto-app-query jobs --sponsorship offers
```

`auto-app-query jobs`, the HTTP API and the MCP `search_jobs` tool hide rule-excluded jobs by default
(`--include-excluded` / `include_excluded=true` audits them, with `screening.reasons`) and accept a
`sponsorship` filter. Setup, Discord and deployment: [docs/linux-worker.md](docs/linux-worker.md).
````

并在 README 的 "Command reference" 命令表里加上 `auto-app worker  run · watch · status`。

- [ ] **Step 3: 检查 README 和 `--help` 一致**

Run: `uv run auto-app worker --help && uv run auto-app worker run --help`
Expected: `--help` 里的选项和 README 写的一致（`--steps`、`--enrich-limit`、`--notify-since`、`--db`）

- [ ] **Step 4: Commit（仅在用户批准提交时）**

```bash
git add docs README.md
git commit -m "docs: Linux worker setup, screening rules and Discord digest

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: 部署到 home-ubuntu 并验收（需要用户参与）

**Files:**
- Create: `scripts/deploy_linux.sh`

这个任务会修改生产环境。**每个标了「用户」的步骤都由用户本人执行，或经用户明确批准后执行。** 涉及密钥的步骤（Discord webhook）只能由用户自己操作；执行者不得读取或打印 `worker.env`。

- [ ] **Step 1: 写部署脚本**

```bash
#!/usr/bin/env bash
# Sync this checkout to the Linux host, back up the live database, install dependencies and restart the
# job-search services (docs/linux-worker.md §5). Secrets, data and private config never leave the Mac's
# git-ignored paths: they live only in the host's runtime directory.
set -euo pipefail
HOST="${1:-home-ubuntu}"
BASE="${BASE:-Desktop/job-search}"
cd "$(dirname "$0")/.."
rsync -az --delete \
  --exclude '.git/' --exclude '.venv/' --exclude '__pycache__/' --exclude 'node_modules/' --exclude '.pytest_cache/' \
  --exclude 'reports/' --exclude '.env' --exclude 'data/' --exclude 'profile/' --exclude 'filters/' \
  --exclude 'credentials/' --exclude 'browser-profiles/' --exclude 'config/searches.yaml' \
  --exclude 'config/apply.yaml' --exclude 'config/serve.yaml' --exclude 'config/feeds.yaml' \
  --exclude 'config/preferences.json' --exclude 'config/screening.json' \
  ./ "$HOST:$BASE/auto-application/"
ssh "$HOST" bash -s -- "$BASE" <<'REMOTE'
set -euo pipefail
BASE="$1"
cd "$HOME/$BASE/auto-application"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
"$UV" sync --frozen --python 3.12
python3 - "$HOME/$BASE/runtime/data/auto_app.db" <<'PY'
import sqlite3, sys, time
from pathlib import Path
db = Path(sys.argv[1])
out = db.with_name(f"auto_app.{time.strftime('%Y%m%d-%H%M%S')}.pre-deploy.db")
src, dst = sqlite3.connect(f"file:{db}?mode=ro", uri=True), sqlite3.connect(out)
src.backup(dst)
dst.close(); src.close()
out.chmod(0o600)
print("backup:", out)
PY
systemctl --user daemon-reload
systemctl --user restart auto-app-ingest.service auto-app-agent.service
if systemctl --user is-enabled --quiet auto-app-worker.service 2>/dev/null; then
  systemctl --user restart auto-app-worker.service
fi
systemctl --user --no-pager --lines=0 status auto-app-ingest.service auto-app-agent.service
REMOTE
```

然后 `chmod +x scripts/deploy_linux.sh`。

- [ ] **Step 2:「用户」确认 spec §8 的决定**，尤其是：
  - #1：公民身份要求排除
  - #2–#4：PM、硬件、数据分析
  - #7：停用实习 feed
  - #8：仅标题岗位等 2 小时

  有改动就先改 Task 2 的默认值或正则（同时补测试），再继续。

- [ ] **Step 3: 部署代码并备份**

Run（在 Mac 上）：`scripts/deploy_linux.sh home-ubuntu`
Expected：输出 `backup: …/auto_app.<时间>.pre-deploy.db`，两个服务都是 active。新代码第一次打开数据库时会自动迁移到 `5b1e0c7d2a91`；这份备份就是回滚点。

- [ ] **Step 4:「用户」准备 Linux 上的运行时文件**

```bash
ssh home-ubuntu
cd ~/Desktop/job-search
cp auto-application/config/screening.example.json runtime/config/screening.json
```

停用 4 个实习 feed：在 `runtime/config/feeds.yaml` 里，把 `simplify-internships`、`speedyapply-swe-internships`、`speedyapply-ai-internships`、`jobright-swe-internships` 设为 `enabled: false`。修改前先把 diff 给用户看。

用户自己建 Discord webhook，然后在 Ubuntu 上运行：

```bash
umask 077
cat > ~/Desktop/job-search/runtime/credentials/worker.env <<'EOF'
AUTO_APP_DISCORD_WEBHOOK_URL=<粘贴 webhook URL>
EOF
```

- [ ] **Step 5: 分步试运行**

```bash
cd ~/Desktop/job-search/auto-application
export AUTO_APP_HOME=~/Desktop/job-search/runtime
DB=$AUTO_APP_HOME/data/auto_app.db
.venv/bin/auto-app worker run --db "$DB" --steps screen
.venv/bin/auto-app worker run --db "$DB" --steps screen,enrich --enrich-limit 20
set -a; . $AUTO_APP_HOME/credentials/worker.env; set +a
.venv/bin/auto-app worker run --db "$DB" --enrich-limit 20
python3 -S scripts/job_view.py --db "$DB" context | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['screening'], indent=2))"
```

Expected：
- 第一条命令：`steps.screen.excluded` 有几千个，`reasons` 里 `internship` 最多；`kept` 估计在 2,000–4,000 之间。
- 第二条命令：`steps.enrich.ok` 接近 20，`failed` 很少。
- 第三条命令：`steps.notify` 为 `ok`，`sent` = 0。首轮不推存量，所以 Discord 里没有消息是正常的。
- `context.screening` 的数字和第一条命令的结果一致。

- [ ] **Step 6:「用户」抽查规则排除**

```bash
python3 -S scripts/job_view.py --db "$DB" jobs --include-excluded --limit 200 \
  | python3 -c "import json,sys,random; xs=[j for j in json.load(sys.stdin)['items'] if j['screening']['excluded']]; random.seed(1); [print(j['job_id'], j['title'], '|', j['company'], '|', j['screening']['reasons']) for j in random.sample(xs, min(30, len(xs)))]"
```

用户看这 30 条。误排除超过 2 条，就调整规则：改 `screening.json`，或者回到 Task 2 改正则并加测试。然后重跑 Step 5 的第一条命令。

- [ ] **Step 7: 安装服务**

```bash
cp ~/Desktop/job-search/auto-application/deploy/auto-app-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now auto-app-worker.service
systemctl --user status auto-app-worker.service --no-pager
journalctl --user -u auto-app-worker.service -n 20 --no-pager
```

- [ ] **Step 8: 验收（spec §4）**。24 小时后检查以下各项，结果记录到 `docs/updates/2026-10-12-linux-worker.md`：

1. `auto-app worker status`：`consecutive_failures` = 0，`last_report.steps` 里有 screen、enrich、notify。
2. 四个支持平台的保留岗位里，已补全 JD 的比例 ≥90%（用 `context` 加一条 SQL 统计）。
3. 新岗位在 15 分钟内出现在 Discord（仅标题的最迟 2 小时 15 分钟），格式符合 Task 8。
4. 在 Dot 里问"有哪些提供签证担保的新岗位"，确认它调用了 `search_jobs(sponsorship="offers")` 并返回结果。Dot 用的 MCP 服务是 `auto-app-agent.service`，部署脚本已经重启过它。如果 ChatGPT 里的工具列表没有显示新参数，在 ChatGPT 插件设置里刷新连接。
5. 抽查的 30 个被排除岗位里，误排除 ≤2 个。

- [ ] **Step 9: Commit（仅在用户批准提交时）**

```bash
git add scripts/deploy_linux.sh docs/updates/2026-10-12-linux-worker.md
git commit -m "chore(deploy): rsync deploy with pre-deploy database backup

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## 自检记录

- **spec 覆盖**：
  - §4 的四个步骤：screen → Task 2/3；enrich → Task 4/5；notify → Task 8/9。
  - "用户自己运行的 filter run 跳过被排除岗位" → Task 6。
  - 查询/MCP 的 `sponsorship` 参数和默认隐藏 → Task 7。
  - 停用实习 feed、部署前备份 → Task 11；验收 → Task 11 Step 8。
  - §8 的决定：默认值写在 Task 2 的正则和 Task 8 的 `READY_AFTER` 里，Task 11 Step 2 让用户确认。
  - §8 #11（不调用模型）：Global Constraints 和 Task 9 的 `STEPS` 里没有 judge；`test_unknown_steps_are_rejected` 用 `judge` 作为非法步骤来验证。
- **类型一致**：以下签名在定义处和使用处一致：
  - `FetchResult(status, posting, error)`、`Target(platform, board, job_id, api_url)`
  - `ScreeningRules` / `JobFacts` / `Screening` 的字段
  - `WorkerOptions(screening_path, enrich_limit, digest_limit, steps)`
  - `run_round(store, opts, *, fetcher, discord, state, now)`
  - `pending(store, limit, since, now)`、`send_digest(store, discord, *, limit, since, now)`
- **Review Focus**：五条都有对应测试，分别在 Task 5、8、9、2、9/8。
- **时间相关的测试**：判断"是否在 `since` 之后"的测试都用真实的 `utcnow()` 加偏移量，不用写死的日期，避免岗位的真实 `first_seen_at` 晚于写死的日期导致误判。
