# P3：投递后跟踪 + 长期运行 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让系统能跟踪每份申请投出去之后的进展（OA、面试、拒信、offer），每周在 Discord 发一次汇总；同时把运维补齐：每晚自动备份并能验证恢复、服务挂掉时 Discord 告警、磁盘快满时告警、从 rsync 快照部署改为 git 部署。全程不调用任何模型。

**Architecture:**
- 新增 `application_events`（每条申请的时间线）和 `mail_updates`（邮件识别出的状态更新）两张表，逻辑放在 `src/auto_app/track/`。
- 状态事件有三个来源：
  - 手动：CLI、MCP、Discord；
  - 系统自动：标记为 `applied` 时自动写一条 applied 事件，P2 agent 回写 submitted 时也是；
  - 邮件：mailbox 来源设 `purpose: updates` 后，邮件不再按"岗位提醒"解析，而是用确定性规则分类，再按公司名匹配到已投岗位。匹配不上的进入"待确认"列表，由用户指定。
- 运维代码放在 `src/auto_app/ops/`，提供 `backup`、`verify`、`prune`、`alert` 四个命令，用 systemd timer 和 `OnFailure=` 钩子驱动。
- 部署脚本新增 git 模式：push 到 Ubuntu 上的 bare repo，在 work tree 里 checkout 指定提交。停服务、备份、迁移、恢复服务的逻辑保持不变。

**Tech Stack:** Python 3.12、SQLAlchemy Core + Alembic、`email`（标准库）、zstandard（已有依赖）、Typer、mcp 2.3、systemd timer、git、pytest。

**Spec:** [2026-10-12-roadmap-p0-p3.md](2026-10-12-roadmap-p0-p3.md) §7。前置条件：P1 Task 3（IMAP）、P2（handoff 回写）。

## Global Constraints

- 不调用模型或 agent。邮件分类只用确定性规则；分不出来就不分，匹配不上就进待确认列表，不猜。
- 邮件只读（沿用 P1 IMAP 的 EXAMINE + BODY.PEEK，以及 Gmail/Outlook API 的 GET）。
- 邮件正文**不入库**：`mail_updates` 只存发件人、主题、分类结果和匹配结果。
- 备份文件权限 600；备份目录在 `$AUTO_APP_HOME/backups/`，部署脚本的 rsync 已经排除了它。
- git 部署要求工作区干净（没有未提交的改动）。**目前所有 P0/P1 的改动都没有提交**；要用 git 模式，用户得先自己决定提交。
- 数据库结构只通过 Alembic 迁移修改；新迁移的 `down_revision = '7c3d9e1f4a20'`。
- 文档规则和提交规则同 P1（commit 只在用户批准时执行，结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`）。

## Review Focus

1. **一封拒信被重复识别**（重新扫描同一封邮件时）：期望只生成一条事件。→ Task 2 `test_the_same_message_is_recorded_once`。
2. **同一公司有多个已投岗位**：期望不随便挑一个，而是先按主题里的职位名匹配；还匹配不上就进待确认列表。→ Task 2 `test_ambiguous_companies_go_to_the_review_list`。
3. **备份时数据库正在写入**：期望用 SQLite online backup 拿到一致的快照，而且备份能被 QueryService 打开。→ Task 4 `test_backup_is_consistent_and_verifiable`。
4. **保留策略误删最新的备份**：期望最新的一份永远保留，每天最新的一份按天保留，每周最新的一份按周保留。→ Task 4 `test_prune_keeps_the_newest_daily_and_weekly_copies`。
5. **git 部署时工作区不干净**：期望脚本拒绝部署，远端什么都不改。→ Task 6 `test_git_mode_refuses_a_dirty_tree`。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `migrations/versions/8d4e0f2a5b31_tracking.py`、`src/auto_app/core/db/tables.py` | `application_events`、`mail_updates` |
| `src/auto_app/track/__init__.py`、`events.py` | 事件记录、查询、周汇总 |
| `src/auto_app/track/updates.py` | 邮件分类和匹配 |
| `src/auto_app/track/cli.py` | `auto-app events …` |
| `src/auto_app/agent/service.py`、`src/auto_app/handoff/service.py` | 标为 applied 时自动写事件 |
| `src/auto_app/query/service.py`、`src/auto_app/agent/mcp_server.py` | `get_job` 带时间线；新增事件和待确认相关的 MCP 工具 |
| `src/auto_app/ingest/mailbox.py` | `purpose: updates` 分支 |
| `src/auto_app/worker/round.py`、`src/auto_app/worker/digest.py` | `summary`、`health` 两个步骤 |
| `src/auto_app/ops/__init__.py`、`backup.py`、`alert.py`、`cli.py` | 备份、验证、清理、告警 |
| `deploy/auto-app-backup.service`、`deploy/auto-app-backup.timer`、`deploy/auto-app-alert@.service`，以及所有 `deploy/*.service` | systemd |
| `scripts/deploy_linux.sh`、`scripts/pull_backups.sh` | git 部署；从 Mac 拉取异机备份 |
| `docs/operations.md`、`docs/application-tracking.md`、`docs/updates/…`、`README.md` | 文档 |

---

### Task 1: 申请时间线（application_events）

**Files:**
- Create: `migrations/versions/8d4e0f2a5b31_tracking.py`、`src/auto_app/track/__init__.py`、`src/auto_app/track/events.py`
- Modify: `src/auto_app/core/db/tables.py`、`src/auto_app/agent/service.py`（`record_application_status(..., event_source="manual")`：标为 applied 时写事件）、`src/auto_app/handoff/service.py`（传 `event_source="agent"`）、`src/auto_app/query/service.py`（可选表，`job()` 返回 `events`）、`src/auto_app/agent/mcp_server.py`（`record_application_event` 工具）
- Test: `tests/track/__init__.py`（空文件）、`tests/track/test_events.py`

**Interfaces:**
- Produces:
  - `KINDS = ("applied", "assessment", "interview", "rejected", "offer", "withdrawn")`
  - `SOURCES = ("manual", "agent", "email", "system")`
  - `record_event(store, job_id, kind, *, occurred_at=None, source="manual", note=None, mail_update_id=None) -> int`：同一岗位、同一 kind、同一来源，24 小时内的重复记录返回已有的 event_id。
  - `events_for(store, job_id) -> list[dict]`
  - `weekly_summary(store, since: datetime) -> dict`：返回 `{"by_kind": {...}, "applied_total": n, "active_pipeline": {...}}`
  - MCP：`record_application_event(job_id, kind, occurred_at=None, note=None)`
  - `get_job` 的返回里多一个 `events` 字段。

- [ ] **Step 1: 写失败测试**

```python
# tests/track/test_events.py
from datetime import datetime, timedelta, timezone

import pytest

from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Backend Engineer")])
    yield result
    result.engine.dispose()


def test_events_are_recorded_once_and_listed_in_order(store):
    from auto_app.track.events import events_for, record_event

    job = job_id_of(store, "Backend Engineer")
    first = record_event(store, job, "assessment", occurred_at=NOW, source="email", note="HackerRank link")
    again = record_event(store, job, "assessment", occurred_at=NOW + timedelta(hours=2), source="email")
    record_event(store, job, "interview", occurred_at=NOW + timedelta(days=3))
    assert first == again
    assert [e["kind"] for e in events_for(store, job)] == ["assessment", "interview"]
    with pytest.raises(ValueError, match="kind"):
        record_event(store, job, "ghosted")


def test_marking_applied_writes_an_applied_event(store, tmp_path):
    from auto_app.agent.service import AgentService
    from auto_app.track.events import events_for

    job = job_id_of(store, "Backend Engineer")
    AgentService(tmp_path / "data" / "auto_app.db").record_application_status(job, "applied", "applied on site")
    assert [(e["kind"], e["source"]) for e in events_for(store, job)] == [("applied", "manual")]


def test_get_job_includes_the_timeline(store, tmp_path):
    from auto_app.query.service import QueryService
    from auto_app.track.events import record_event

    job = job_id_of(store, "Backend Engineer")
    record_event(store, job, "rejected", occurred_at=NOW, source="email")
    with QueryService(tmp_path / "data" / "auto_app.db") as query:
        assert [e["kind"] for e in query.job(job)["events"]] == ["rejected"]


def test_weekly_summary_counts_recent_events(store):
    from auto_app.track.events import record_event, weekly_summary

    job = job_id_of(store, "Backend Engineer")
    record_event(store, job, "applied", occurred_at=NOW - timedelta(days=10))
    record_event(store, job, "interview", occurred_at=NOW - timedelta(days=1))
    summary = weekly_summary(store, NOW - timedelta(days=7))
    assert summary["by_kind"] == {"interview": 1} and summary["active_pipeline"] == {"interview": 1}
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/track/test_events.py -q`
Expected: FAIL，`ModuleNotFoundError: auto_app.track`

- [ ] **Step 3: 实现**

迁移文件 `migrations/versions/8d4e0f2a5b31_tracking.py`（`revision = '8d4e0f2a5b31'`，`down_revision = '7c3d9e1f4a20'`）：

```python
def upgrade():
    op.create_table(
        'mail_updates',
        sa.Column('update_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('message_key', sa.String(), nullable=False, unique=True),
        sa.Column('received_at', sa.DateTime(timezone=True)),
        sa.Column('sender', sa.Text()),
        sa.Column('subject', sa.Text()),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('job_id', sa.Integer()),
        sa.Column('status', sa.String(), nullable=False),  # matched | unmatched | dismissed
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'application_events',
        sa.Column('event_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.Integer(), nullable=False, index=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('note', sa.Text()),
        sa.Column('mail_update_id', sa.Integer()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table('application_events')
    op.drop_table('mail_updates')
```

`tables.py` 末尾追加对应的两个 `Table` 定义，列和迁移一致，并加注释：`# job_id: no FK — the timeline outlives the job (TTL)`。

```python
# src/auto_app/track/__init__.py
"""What happened after applying: the timeline, mail updates and the weekly summary (docs/application-tracking.md)."""
```

```python
# src/auto_app/track/events.py
"""One application's timeline. Sources: you (CLI, chat, Discord), your agent, recognized mail, the system."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, insert, select

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow

KINDS = ("applied", "assessment", "interview", "rejected", "offer", "withdrawn")
SOURCES = ("manual", "agent", "email", "system")
TERMINAL = ("rejected", "offer", "withdrawn")
SAME_WITHIN = timedelta(hours=24)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def record_event(store, job_id: int, kind: str, *, occurred_at: datetime | None = None, source: str = "manual",
                 note: str | None = None, mail_update_id: int | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of: {', '.join(SOURCES)}")
    occurred_at = _aware(occurred_at) or utcnow()
    e = T.application_events
    with store.engine.begin() as conn:
        existing = conn.execute(select(e.c.event_id).where(
            e.c.job_id == job_id, e.c.kind == kind, e.c.source == source,
            e.c.occurred_at >= occurred_at - SAME_WITHIN, e.c.occurred_at <= occurred_at + SAME_WITHIN)).scalar()
        if existing is not None:
            return existing
        return conn.execute(insert(e).values(job_id=job_id, kind=kind, occurred_at=occurred_at, source=source,
                                             note=(note or None) and note[:500], mail_update_id=mail_update_id,
                                             created_at=utcnow())).inserted_primary_key[0]


def events_for(store, job_id: int) -> list[dict]:
    e = T.application_events
    with store.engine.connect() as conn:
        return [{**dict(r), "occurred_at": _aware(r["occurred_at"]).isoformat()}
                for r in conn.execute(select(e.c.event_id, e.c.kind, e.c.occurred_at, e.c.source, e.c.note)
                                      .where(e.c.job_id == job_id).order_by(e.c.occurred_at, e.c.event_id)).mappings()]


def weekly_summary(store, since: datetime) -> dict:
    e = T.application_events
    with store.engine.connect() as conn:
        by_kind = dict(conn.execute(select(e.c.kind, func.count()).where(e.c.occurred_at >= since)
                                    .group_by(e.c.kind)).all())
        applied = conn.execute(select(func.count(func.distinct(e.c.job_id))).where(e.c.kind == "applied")).scalar()
        latest = {}
        for job_id, kind, at in conn.execute(select(e.c.job_id, e.c.kind, e.c.occurred_at)
                                             .order_by(e.c.occurred_at, e.c.event_id)):
            latest[job_id] = kind
    pipeline = {}
    for kind in latest.values():
        if kind not in ("applied", *TERMINAL):
            pipeline[kind] = pipeline.get(kind, 0) + 1
    return {"by_kind": by_kind, "applied_total": applied, "active_pipeline": pipeline}
```

`src/auto_app/agent/service.py`：`record_application_status` 签名加 `event_source: str = "manual"`；在 `changed = True` 之后、事务结束之前，如果 `status == "applied" and not same_status`，就调用：

```python
                        from auto_app.track.events import record_event

                        record_event(store, job_id, "applied", source=event_source, note=reason)
```

（`record_event` 自己开连接；它在 `store.transaction` 之外执行也可以。如果在同一事务里因为 SQLite 锁冲突失败，就把这段调用移到 `with store.transaction(...)` 块之后、`finally` 之前执行。）

`src/auto_app/handoff/service.py`：`record_attempt` 调用 `record_application_status(...)` 时加上 `event_source="agent"`。

`src/auto_app/query/service.py`：
- `__init__` 里检测可选表：`self.events_available = {"job_id", "kind", "occurred_at", "source", "note"} <= {row["name"] for row in self._conn.execute('PRAGMA table_info("application_events")')}`。
- `job()` 返回前加：

```python
            if self.events_available:
                item["events"] = [_dates(r) for r in self._conn.execute(
                    "SELECT kind, occurred_at, source, note FROM application_events WHERE job_id=? "
                    "ORDER BY occurred_at, event_id", (job_id,))]
```

`src/auto_app/agent/mcp_server.py`：在基础工具里（和 `record_application_status` 并列）加：

```python
    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                             open_world_hint=False))
    def record_application_event(job_id: Annotated[int, Field(strict=True, ge=1)],
                                 kind: Literal["applied", "assessment", "interview", "rejected", "offer", "withdrawn"],
                                 occurred_at: str | None = None, note: str | None = None) -> dict[str, Any]:
        """Add what the user reports happened after applying (an OA, an interview, a rejection, an offer)."""
        return _call(service.record_event, job_id, kind, occurred_at, note)
```

并在 `AgentService` 里加一个薄方法 `record_event(job_id, kind, occurred_at=None, note=None)`：打开 `DbStore(migrate=False)`，解析 `occurred_at`（ISO 字符串，没有时区就按 UTC），调用 `track.events.record_event(..., source="manual")`，返回 `{"event_id", "events": events_for(...)}`；`ValueError` 转为 `AgentError`。

- [ ] **Step 4: 运行，确认通过；再跑 agent、query、handoff**

Run: `uv run pytest tests/track tests/agent tests/query tests/handoff -q`
Expected: PASS（`tests/agent/test_mcp.py` 里断言工具集合相等的那个测试要把 `record_application_event` 加进期望集合）

- [ ] **Step 5: Commit（仅在用户批准时）**

```bash
git add migrations src/auto_app/core/db/tables.py src/auto_app/track src/auto_app/agent src/auto_app/handoff src/auto_app/query tests
git commit -m "feat(track): application timeline with automatic applied events

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 邮件识别申请状态（`purpose: updates`）

**Files:**
- Create: `src/auto_app/track/updates.py`
- Modify: `src/auto_app/ingest/mailbox.py`（`validate_mailbox_config` 读取 `purpose`；`_import_message` 按 purpose 分流）、`src/auto_app/agent/control.py` 和 `mcp_server.py`（`list_unmatched_updates`、`assign_update`、`dismiss_update`，挂在 `--control-tools` 下）
- Test: `tests/track/test_updates.py`

**Interfaces:**
- Produces:
  - `classify(subject: str, body: str) -> str | None`：优先级 offer > rejected > interview > assessment > applied。
  - `record_update(store, source: str, message_key: str, content: bytes, received: datetime) -> dict`：返回 `{"kind", "status", "job_id"}`；分不出类时返回 `{"kind": None}`，不写库。
  - `unmatched(store) -> list[dict]`、`assign(store, update_id, job_id) -> dict`、`dismiss(store, update_id) -> dict`
  - feeds.yaml 示例：

```yaml
  application-mail:
    kind: mailbox
    provider: imap
    purpose: updates          # recognize OA / interview / rejection / offer mail instead of job alerts
    credentials: ../credentials/imap.json
    folder: INBOX
    gmail_raw: 'category:primary newer_than:30d -from:linkedin.com'
    lookback_days: 30
```

- [ ] **Step 1: 写失败测试**

```python
# tests/track/test_updates.py
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from auto_app.track.updates import assign, classify, record_update, unmatched
from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)


def mail(sender, subject, body):
    m = EmailMessage()
    m["From"], m["Subject"] = sender, subject
    m.set_content(body)
    return m.as_bytes()


@pytest.mark.parametrize("subject, body, kind", [
    ("Your application to Acme", "Unfortunately, we have decided to move forward with other candidates.", "rejected"),
    ("Acme Online Assessment", "Please complete the HackerRank challenge within 7 days.", "assessment"),
    ("Next steps with Acme", "We'd like to schedule a phone screen. Please share your availability.", "interview"),
    ("Offer", "We are pleased to offer you the position of Software Engineer.", "offer"),
    ("Thank you for applying to Acme", "We have received your application.", "applied"),
    ("Acme newsletter", "Our latest product news.", None),
])
def test_classification_rules(subject, body, kind):
    assert classify(subject, body) == kind


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Backend Engineer"), record("102", "Data Engineer"),
                    record("103", "ML Engineer", company="Beta",
                           url="https://jobs.lever.co/beta/6ed76ce8-1111-2222-3333-444455556666")])
    for title in ("Backend Engineer", "Data Engineer", "ML Engineer"):
        result.mark_job(job_id_of(result, title), "applied")
    yield result
    result.engine.dispose()


def test_a_unique_company_match_records_an_event(store):
    from auto_app.track.events import events_for

    result = record_update(store, "application-mail", "imap:1:5",
                           mail("Beta Recruiting <talent@beta.com>", "Your application", "Unfortunately …"), NOW)
    assert result["status"] == "matched" and result["job_id"] == job_id_of(store, "ML Engineer")
    assert [e["kind"] for e in events_for(store, result["job_id"])] == ["rejected"]


def test_the_same_message_is_recorded_once(store):
    content = mail("Beta Recruiting <talent@beta.com>", "Your application", "Unfortunately …")
    record_update(store, "application-mail", "imap:1:5", content, NOW)
    again = record_update(store, "application-mail", "imap:1:5", content, NOW)
    assert again["status"] == "duplicate"


def test_ambiguous_companies_go_to_the_review_list(store):
    by_title = record_update(store, "application-mail", "imap:1:6",
                             mail("Acme <jobs@acme.com>", "Acme Data Engineer: next steps", "schedule a call"), NOW)
    assert by_title["job_id"] == job_id_of(store, "Data Engineer")
    unclear = record_update(store, "application-mail", "imap:1:7",
                            mail("Acme <jobs@acme.com>", "Your Acme application", "Unfortunately …"), NOW)
    assert unclear["status"] == "unmatched"
    pending = unmatched(store)
    assert [u["update_id"] for u in pending] == [unclear["update_id"]]
    assert assign(store, unclear["update_id"], job_id_of(store, "Backend Engineer"))["status"] == "matched"
    assert unmatched(store) == []
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/track/test_updates.py -q`
Expected: FAIL，`ModuleNotFoundError: auto_app.track.updates`

- [ ] **Step 3: 实现**

```python
# src/auto_app/track/updates.py
"""Recognize application updates in mail, without a model (docs/application-tracking.md).

Deterministic phrases classify a message (offer > rejected > interview > assessment > applied). A message is
matched to an applied job by company name in the sender/subject/opening text — and, when that company has
several applied jobs, by job-title words in the subject. Anything else waits in the review list. Message
bodies are never stored: only sender, subject, the kind and the match.
"""

import re
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from sqlalchemy import insert, select, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.track.events import record_event

RULES = [
    ("offer", r"pleased to offer|offer letter|extend(?:ing)?\s+(?:you\s+)?(?:an|the)\s+offer|job offer"),
    ("rejected", r"unfortunately|not\s+(?:to\s+)?(?:be\s+)?mov(?:e|ing)\s+forward|decided to (?:pursue|move forward with) other|"
                 r"position has been filled|no longer (?:being )?consider|regret to inform"),
    ("interview", r"\binterview|phone screen|schedule (?:a |some )?(?:time|call|chat)|your availability|"
                  r"next steps? in (?:the|our) (?:process|hiring)"),
    ("assessment", r"\bassessment|coding challenge|hackerrank|codesignal|codility|\bkarat\b|take[- ]home|online test"),
    ("applied", r"thank(?:s| you) for (?:applying|your application|your interest)|application (?:was |has been )?"
                r"(?:received|submitted)|we(?:'ve| have) received your application"),
]
COMPILED = [(kind, re.compile(pattern, re.I)) for kind, pattern in RULES]


def classify(subject: str, body: str) -> str | None:
    text = f"{subject}\n{(body or '')[:3000]}"
    for kind, pattern in COMPILED:
        if pattern.search(text):
            return kind
    return None


def _parts(content: bytes) -> tuple[str, str, str]:
    message = BytesParser(policy=policy.default).parsebytes(content)
    name, address = parseaddr(str(message.get("From", "")))
    part = message.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part is not None else ""
    return f"{name} <{address}>".strip(), str(message.get("Subject", "")), re.sub(r"<[^>]+>", " ", body)


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _match(store, sender: str, subject: str, body: str) -> int | None:
    j, p = T.jobs, T.postings
    with store.engine.connect() as conn:
        rows = conn.execute(select(j.c.job_id, j.c.company_norm, j.c.title_norm)
                            .where(j.c.decision == "applied")).all()
    haystack = _words(f"{sender} {subject} {body[:2000]}")
    companies = {}
    for job_id, company, title in rows:
        tokens = _words(company)
        if tokens and tokens <= haystack:
            companies.setdefault(company, []).append((job_id, title))
    if len(companies) != 1:
        return None
    jobs = next(iter(companies.values()))
    if len(jobs) == 1:
        return jobs[0][0]
    subject_words = _words(subject)
    titled = [job_id for job_id, title in jobs if _words(title) and _words(title) - {"engineer"} <= subject_words]
    return titled[0] if len(titled) == 1 else None


def record_update(store, source: str, message_key: str, content: bytes, received: datetime) -> dict:
    sender, subject, body = _parts(content)
    kind = classify(subject, body)
    if kind is None:
        return {"kind": None}
    u = T.mail_updates
    with store.engine.connect() as conn:
        if conn.execute(select(u.c.update_id).where(u.c.message_key == message_key)).first():
            return {"kind": kind, "status": "duplicate"}
    job_id = _match(store, sender, subject, body)
    status = "matched" if job_id else "unmatched"
    with store.engine.begin() as conn:
        update_id = conn.execute(insert(u).values(source=source, message_key=message_key, received_at=received,
                                                  sender=sender[:300], subject=subject[:300], kind=kind,
                                                  job_id=job_id, status=status, created_at=utcnow())
                                 ).inserted_primary_key[0]
    if job_id:
        record_event(store, job_id, kind, occurred_at=received, source="email", note=subject[:200],
                     mail_update_id=update_id)
    return {"kind": kind, "status": status, "job_id": job_id, "update_id": update_id}


def unmatched(store) -> list[dict]:
    u = T.mail_updates
    with store.engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(u.c.update_id, u.c.received_at, u.c.sender, u.c.subject, u.c.kind)
                                              .where(u.c.status == "unmatched").order_by(u.c.update_id)).mappings()]


def assign(store, update_id: int, job_id: int) -> dict:
    u = T.mail_updates
    with store.engine.begin() as conn:
        row = conn.execute(select(u).where(u.c.update_id == update_id, u.c.status == "unmatched")).mappings().first()
        if row is None:
            raise ValueError(f"update #{update_id} is not waiting for review")
        conn.execute(update(u).where(u.c.update_id == update_id).values(job_id=job_id, status="matched"))
    record_event(store, job_id, row["kind"], occurred_at=row["received_at"], source="email",
                 note=(row["subject"] or "")[:200], mail_update_id=update_id)
    return {"update_id": update_id, "job_id": job_id, "status": "matched"}


def dismiss(store, update_id: int) -> dict:
    u = T.mail_updates
    with store.engine.begin() as conn:
        changed = conn.execute(update(u).where(u.c.update_id == update_id, u.c.status == "unmatched")
                               .values(status="dismissed")).rowcount
    if not changed:
        raise ValueError(f"update #{update_id} is not waiting for review")
    return {"update_id": update_id, "status": "dismissed"}
```

修改 `src/auto_app/ingest/mailbox.py`：

- `validate_mailbox_config`：`return` 之前加

```python
    purpose = definition.get("purpose", "jobs")
    if purpose not in ("jobs", "updates"):
        raise ValueError("mailbox purpose must be jobs or updates")
    result["purpose"] = purpose
```

- `_import_message` 开头，在 `parsed = parse_email(content)` 之前加：

```python
    if config.get("purpose") == "updates":
        from auto_app.track.updates import record_update

        record_update(store, source, f"{provider}:{account}:{message_id}", content, received)
        _receipt(store, source, provider, account, message_id, received, now, 0)
        return {}
```

修改 `src/auto_app/agent/control.py`：加三个方法，都是打开 `self._store()` 后调用对应函数：`unmatched_updates()`、`assign_update(update_id, job_id)`、`dismiss_update(update_id)`。在 `mcp_server.py` 的 control 区块里注册三个工具：`list_unmatched_updates`（只读）、`assign_update`、`dismiss_update`（写）。

- [ ] **Step 4: 运行，确认通过；再跑邮件相关测试**

Run: `uv run pytest tests/track tests/ingest tests/agent -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准时）**

```bash
git add src/auto_app/track/updates.py src/auto_app/ingest/mailbox.py src/auto_app/agent tests/track/test_updates.py
git commit -m "feat(track): recognize application updates in mail, with a review list

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 每周汇总和磁盘检查（worker 新步骤）

**Files:**
- Modify: `src/auto_app/worker/round.py`（`STEPS = ("screen", "enrich", "notify", "summary", "health")`、`WorkerOptions.min_free_gb = 2`）、`src/auto_app/worker/digest.py`（`summary_text`）、`src/auto_app/worker/cli.py`（`--steps` 的帮助文字）
- Create: `src/auto_app/track/cli.py`（`auto-app events add|list|unmatched|assign|dismiss`），并在 `src/auto_app/cli.py` 里注册；`tests/test_cli_structure.py` 加 `"events": ["add", "list", "unmatched", "assign", "dismiss"]`，`worker` 的 TREE 不变
- Test: `tests/worker/test_summary_health.py`、`tests/track/test_cli.py`

**Interfaces:**
- Produces:
  - `summary` 步骤：距离 `state["summary_at"]` 超过 7 天、且配置了 Discord 时，发一条周汇总，内容包括本周各类事件数、累计投递数、进行中的流程，以及 P2 队列长度（如果有）。发送成功后才更新 `summary_at`。
  - `health` 步骤：数据库所在磁盘的剩余空间小于 `min_free_gb` 时，状态为 `error`，交给现有的告警去重机制发 Discord。
  - `summary_text(summary: dict, queue_size: int | None) -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/worker/test_summary_health.py
from datetime import datetime, timedelta, timezone

from auto_app.worker.round import WorkerOptions, run_round
from tests.imported_jobs import ingest, make_store, record
from tests.worker.test_digest import FakeDiscord

NOW = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)


def test_the_summary_goes_out_weekly(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer")])
    discord, state = FakeDiscord(), {}
    opts = WorkerOptions(screening_path=tmp_path / "s.json", steps=("summary",))
    run_round(store, opts, fetcher=None, discord=discord, state=state, now=NOW)
    assert discord.posts and discord.posts[0][0].startswith("📊 Weekly")
    run_round(store, opts, fetcher=None, discord=discord, state=state, now=NOW + timedelta(days=1))
    assert len(discord.posts) == 1
    run_round(store, opts, fetcher=None, discord=discord, state=state, now=NOW + timedelta(days=8))
    assert len(discord.posts) == 2
    store.engine.dispose()


def test_low_disk_space_is_an_error(tmp_path, monkeypatch):
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(100 * 2**30, 99 * 2**30, 1 * 2**30))
    store = make_store(tmp_path)
    report = run_round(store, WorkerOptions(screening_path=tmp_path / "s.json", steps=("health",)), fetcher=None,
                       discord=None, state={}, now=NOW)
    assert report["steps"]["health"]["status"] == "error" and "1.0 GB free" in report["steps"]["health"]["error"]
    store.engine.dispose()
```

```python
# tests/track/test_cli.py
import json

from typer.testing import CliRunner

from auto_app.cli import app
from tests.imported_jobs import ingest, job_id_of, make_store, record


def test_events_add_and_list(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer")])
    job = job_id_of(store, "Backend Engineer")
    store.engine.dispose()
    env = {"AUTO_APP_HOME": str(tmp_path)}
    added = CliRunner().invoke(app, ["events", "add", str(job), "interview", "--note", "phone screen"], env=env)
    assert added.exit_code == 0, added.output
    listed = CliRunner().invoke(app, ["events", "list", str(job)], env=env)
    assert [e["kind"] for e in json.loads(listed.output)] == ["interview"]
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/worker/test_summary_health.py tests/track/test_cli.py -q`
Expected: FAIL（`steps must contain …`；`No such command 'events'`）

- [ ] **Step 3: 实现**

`src/auto_app/worker/round.py`：
- `STEPS = ("screen", "enrich", "notify", "summary", "health")`；`WorkerOptions` 加 `min_free_gb: float = 2.0`；`__post_init__` 的报错信息同步更新。
- 在 notify 之后加：

```python
    if "summary" in opts.steps:
        step("summary", lambda: _summary(store, discord, state, now))
    if "health" in opts.steps:
        step("health", lambda: _health(store, opts))
```

- 新增两个函数：

```python
SUMMARY_EVERY = timedelta(days=7)


def _summary(store, discord, state: dict, now: datetime) -> dict:
    from auto_app.track.events import weekly_summary
    from auto_app.worker.digest import summary_text

    last = state.get("summary_at")
    if discord is None:
        return {"status": "skipped", "reason": "no Discord webhook configured"}
    if last and now - datetime.fromisoformat(last) < SUMMARY_EVERY:
        return {"status": "skipped", "reason": "not due"}
    queue = None
    try:
        from auto_app.handoff.service import HandoffService

        queue = len(HandoffService(store.db_path).queue(limit=100, now=now)["items"])
    except Exception:  # P2 not deployed or its config is invalid: the summary goes out without it
        queue = None
    discord.post(summary_text(weekly_summary(store, now - SUMMARY_EVERY), queue))
    state["summary_at"] = now.isoformat()
    return {"status": "ok", "sent": True}


def _health(store, opts: WorkerOptions) -> dict:
    import shutil

    free = shutil.disk_usage(Path(store.db_path).parent).free / 2**30
    if free < opts.min_free_gb:
        return {"status": "error", "error": f"only {free:.1f} GB free on the database disk"}
    return {"status": "ok", "free_gb": round(free, 1)}
```

（`from datetime import datetime, timedelta`。`Discord.post` 抛出的 `DiscordError` 会被 `step()` 捕获成 error，此时 `summary_at` 不会更新，下一轮会重试。）

`src/auto_app/worker/digest.py` 末尾加：

```python
def summary_text(summary: dict, queue_size: int | None) -> str:
    kinds = summary.get("by_kind") or {}
    week = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items())) or "no new events"
    pipeline = ", ".join(f"{kind} {count}" for kind, count in sorted((summary.get("active_pipeline") or {}).items()))
    lines = [f"📊 Weekly: {week}", f"Applied so far: {summary.get('applied_total', 0)}"]
    if pipeline:
        lines.append(f"In progress: {pipeline}")
    if queue_size is not None:
        lines.append(f"Waiting in the application queue: {queue_size}")
    return "\n".join(lines)
```

```python
# src/auto_app/track/cli.py
"""`auto-app events`: an application's timeline, and mail updates waiting for your review. JSON out."""

import json
from datetime import datetime, timezone
from typing import Annotated

import typer

from auto_app.settings import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Application timeline and mail updates to review.")


def _store():
    from auto_app.core.db.store import DbStore

    paths = get_paths()
    return DbStore(paths.db, paths)


def _run(operation):
    store = _store()
    try:
        typer.echo(json.dumps(operation(store), ensure_ascii=False, indent=2, default=str))
    except ValueError as exc:
        typer.echo(json.dumps({"status": "error", "error": str(exc)}))
        raise typer.Exit(1)
    finally:
        store.engine.dispose()


@app.command("add")
def add(job: int, kind: str, at: Annotated[str | None, typer.Option("--at", help="ISO time; default now.")] = None,
        note: Annotated[str | None, typer.Option("--note")] = None) -> None:
    """Add an event: applied, assessment, interview, rejected, offer or withdrawn."""
    from auto_app.track.events import events_for, record_event

    when = datetime.fromisoformat(at) if at else None
    if when and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    _run(lambda store: (record_event(store, job, kind, occurred_at=when, note=note), events_for(store, job))[1])


@app.command("list")
def list_(job: int) -> None:
    """One job's timeline."""
    from auto_app.track.events import events_for

    _run(lambda store: events_for(store, job))


@app.command("unmatched")
def unmatched_() -> None:
    """Mail updates that matched no applied job (or several)."""
    from auto_app.track.updates import unmatched

    _run(unmatched)


@app.command("assign")
def assign_(update: int, job: int) -> None:
    """Attach a mail update to a job; its event is recorded."""
    from auto_app.track.updates import assign

    _run(lambda store: assign(store, update, job))


@app.command("dismiss")
def dismiss_(update: int) -> None:
    """Drop a mail update that isn't about an application."""
    from auto_app.track.updates import dismiss

    _run(lambda store: dismiss(store, update))
```

在 `src/auto_app/cli.py` 里注册：`app.add_typer(events_app, name="events")`。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/worker tests/track tests/test_cli_structure.py -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准时）**

```bash
git add src/auto_app/worker src/auto_app/track/cli.py src/auto_app/cli.py tests
git commit -m "feat(worker): weekly Discord summary, disk check and events CLI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 每晚备份、验证和保留策略

**Files:**
- Create: `src/auto_app/ops/__init__.py`、`src/auto_app/ops/backup.py`、`src/auto_app/ops/cli.py`、`deploy/auto-app-backup.service`、`deploy/auto-app-backup.timer`、`scripts/pull_backups.sh`
- Modify: `src/auto_app/cli.py`（注册 `ops`）、`tests/test_cli_structure.py`（`"ops": ["backup", "verify", "prune", "alert"]`）
- Test: `tests/ops/__init__.py`（空文件）、`tests/ops/test_backup.py`

**Interfaces:**
- Produces:
  - `backup(db_path: Path, dest: Path, *, now=None) -> dict`：返回 `{"path", "sha256", "bytes", "quick_check": "ok"}`。同时向 `dest/manifest.jsonl` 追加一行。
  - `verify(path: Path) -> dict`：解压到临时文件，用 `QueryService` 打开，返回 `{"ok": True, "jobs": n, "schema_version": …}`。
  - `prune(dest: Path, *, keep_daily=14, keep_weekly=8, now=None) -> list[str]`：返回删掉的文件。
  - 文件名格式：`auto_app-YYYYmmddTHHMMSSZ.db.zst`，权限 600。

- [ ] **Step 1: 写失败测试**

```python
# tests/ops/test_backup.py
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from tests.imported_jobs import ingest, make_store, record

NOW = datetime(2026, 10, 20, 3, 30, tzinfo=timezone.utc)


def test_backup_is_consistent_and_verifiable(tmp_path):
    from auto_app.ops.backup import backup, verify

    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer")])
    writer = sqlite3.connect(tmp_path / "data" / "auto_app.db")
    writer.execute("BEGIN IMMEDIATE")  # a concurrent writer holds a transaction during the backup
    writer.execute("UPDATE jobs SET notified_at=NULL")
    try:
        saved = backup(tmp_path / "data" / "auto_app.db", tmp_path / "backups", now=NOW)
    finally:
        writer.rollback()
        writer.close()
    assert saved["quick_check"] == "ok" and oct(os.stat(saved["path"]).st_mode & 0o777) == "0o600"
    assert verify(saved["path"])["jobs"] == 1
    assert (tmp_path / "backups" / "manifest.jsonl").read_text().count(saved["sha256"]) == 1
    store.engine.dispose()


def test_prune_keeps_the_newest_daily_and_weekly_copies(tmp_path):
    from auto_app.ops.backup import prune

    dest = tmp_path / "backups"
    dest.mkdir()
    names = []
    for days in range(0, 120):
        for hour in (3, 15):
            stamp = (NOW - timedelta(days=days)).replace(hour=hour)
            name = f"auto_app-{stamp:%Y%m%dT%H%M%SZ}.db.zst"
            (dest / name).write_bytes(b"x")
            names.append(name)
    removed = prune(dest, keep_daily=14, keep_weekly=8, now=NOW)
    kept = sorted(p.name for p in dest.glob("auto_app-*.db.zst"))
    assert f"auto_app-{NOW.replace(hour=15):%Y%m%dT%H%M%SZ}.db.zst" in kept  # the newest
    assert len([k for k in kept if k >= f"auto_app-{NOW - timedelta(days=13):%Y%m%d}"]) == 14  # one per day
    assert 14 < len(kept) <= 14 + 8 and len(removed) == len(names) - len(kept)
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/ops -q`
Expected: FAIL，`ModuleNotFoundError: auto_app.ops`

- [ ] **Step 3: 实现**

```python
# src/auto_app/ops/__init__.py
"""Operations on the Linux host: backups, restore checks, retention and failure alerts (docs/operations.md)."""
```

```python
# src/auto_app/ops/backup.py
"""Consistent SQLite backups with the online backup API, compressed, checked and pruned (docs/operations.md §1)."""

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import zstandard

PATTERN = "auto_app-*.db.zst"


def backup(db_path: Path, dest: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    target = dest / f"auto_app-{now:%Y%m%dT%H%M%SZ}.db.zst"
    with tempfile.TemporaryDirectory(dir=dest) as work:
        copy = Path(work) / "copy.db"
        source = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
        destination = sqlite3.connect(copy)
        try:
            source.backup(destination)  # a consistent snapshot even while another connection writes
            check = destination.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            destination.close()
            source.close()
        if check != "ok":
            raise RuntimeError(f"backup failed its integrity check: {check}")
        data = zstandard.ZstdCompressor(level=10).compress(copy.read_bytes())
    target.write_bytes(data)
    os.chmod(target, 0o600)
    digest = hashlib.sha256(data).hexdigest()
    entry = {"path": str(target), "sha256": digest, "bytes": len(data), "created_at": now.isoformat(),
             "quick_check": check}
    with (dest / "manifest.jsonl").open("a") as manifest:
        manifest.write(json.dumps(entry) + "\n")
    os.chmod(dest / "manifest.jsonl", 0o600)
    return entry


def verify(path: Path) -> dict:
    from auto_app.query.service import QueryService

    with tempfile.TemporaryDirectory() as work:
        copy = Path(work) / "data" / "auto_app.db"
        copy.parent.mkdir()
        copy.write_bytes(zstandard.ZstdDecompressor().decompress(Path(path).read_bytes()))
        with QueryService(copy) as query:
            context = query.context()
    return {"ok": True, "jobs": context["counts"]["total"], "schema_version": context["schema_version"]}


def _stamp(path: Path) -> datetime:
    return datetime.strptime(path.name, "auto_app-%Y%m%dT%H%M%SZ.db.zst").replace(tzinfo=timezone.utc)


def prune(dest: Path, *, keep_daily: int = 14, keep_weekly: int = 8, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    files = sorted(dest.glob(PATTERN), key=_stamp, reverse=True)
    keep, days, weeks = set(), set(), set()
    for path in files:
        at = _stamp(path)
        day, week = at.date(), at.isocalendar()[:2]
        if not keep:
            keep.add(path)  # always the newest
        if day not in days and len(days) < keep_daily and now - at < timedelta(days=keep_daily):
            days.add(day)
            keep.add(path)
        elif week not in weeks and len(weeks) < keep_weekly and day not in days:
            weeks.add(week)
            keep.add(path)
    removed = []
    for path in files:
        if path not in keep:
            path.unlink()
            removed.append(path.name)
    return removed
```

> 注：`prune` 的规则是：先保留最近 14 天里每天最新的一份；更早的部分，再保留每个 ISO 周最新的一份，最多 8 周；最新的一份无论如何都保留。

```python
# src/auto_app/ops/cli.py
"""`auto-app ops`: backups, restore checks, retention, and service-failure alerts (docs/operations.md)."""

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from auto_app.settings import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Backups, restore checks and failure alerts.")


@app.command("backup")
def backup_(dest: Annotated[Path | None, typer.Option("--dest", help="Default: AUTO_APP_HOME/backups.")] = None,
            keep_daily: Annotated[int, typer.Option("--keep-daily", min=1)] = 14,
            keep_weekly: Annotated[int, typer.Option("--keep-weekly", min=0)] = 8) -> None:
    """Back up the database now, verify the copy, then apply the retention policy."""
    from auto_app.ops.backup import backup, prune, verify

    paths = get_paths()
    target = dest or paths.home / "backups"
    entry = backup(paths.db, target)
    entry["verify"] = verify(Path(entry["path"]))
    entry["pruned"] = prune(target, keep_daily=keep_daily, keep_weekly=keep_weekly)
    typer.echo(json.dumps(entry, indent=2))


@app.command("verify")
def verify_(path: Path) -> None:
    """Open a backup like a restore would, and report its job count and schema."""
    from auto_app.ops.backup import verify

    typer.echo(json.dumps(verify(path), indent=2))


@app.command("prune")
def prune_(dest: Annotated[Path | None, typer.Option("--dest")] = None,
           keep_daily: Annotated[int, typer.Option("--keep-daily", min=1)] = 14,
           keep_weekly: Annotated[int, typer.Option("--keep-weekly", min=0)] = 8) -> None:
    """Apply the retention policy only."""
    from auto_app.ops.backup import prune

    typer.echo(json.dumps(prune(dest or get_paths().home / "backups", keep_daily=keep_daily,
                                keep_weekly=keep_weekly), indent=2))


@app.command("alert")
def alert(unit: Annotated[str, typer.Option("--unit", help="The systemd unit that failed.")]) -> None:
    """Post a service-failure alert to Discord (systemd OnFailure= runs this)."""
    from auto_app.ops.alert import send_failure

    raise typer.Exit(send_failure(unit, os.environ))
```

```ini
# deploy/auto-app-backup.service
[Unit]
Description=Job Search nightly database backup
OnFailure=auto-app-alert@%n.service

[Service]
Type=oneshot
WorkingDirectory=%h/Desktop/job-search/auto-application
Environment=AUTO_APP_HOME=%h/Desktop/job-search/runtime
ExecStart=%h/Desktop/job-search/auto-application/.venv/bin/auto-app ops backup
UMask=0077
```

```ini
# deploy/auto-app-backup.timer
[Unit]
Description=Nightly job search backup

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
```

```bash
#!/usr/bin/env bash
# scripts/pull_backups.sh — run on the Mac: copy the host's backups off-host (docs/operations.md §2).
set -euo pipefail
HOST="${1:-home-ubuntu}"
DEST="${DEST:-$HOME/Backups/job-search}"
mkdir -p "$DEST"
chmod 700 "$DEST"
rsync -a --chmod=F600 "$HOST:Desktop/job-search/runtime/backups/" "$DEST/"
ls -1t "$DEST"/auto_app-*.db.zst | head -3
```

在 `src/auto_app/cli.py` 里注册 `ops`。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/ops tests/test_cli_structure.py -q`
Expected: PASS（`alert` 命令在 Task 5 实现；在那之前 `tests/test_cli_structure.py` 里先不列 `alert`，等 Task 5 再加回去）

- [ ] **Step 5: Commit（仅在用户批准时）**

```bash
git add src/auto_app/ops src/auto_app/cli.py deploy/auto-app-backup.* scripts/pull_backups.sh tests
git commit -m "feat(ops): nightly verified backups with retention

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: 服务故障告警

**Files:**
- Create: `src/auto_app/ops/alert.py`、`deploy/auto-app-alert@.service`
- Modify: 所有 `deploy/*.service`，在 `[Unit]` 里加 `OnFailure=auto-app-alert@%n.service`（alert 模板本身除外）
- Test: `tests/ops/test_alert.py`

**Interfaces:**
- Produces: `send_failure(unit: str, environ: Mapping[str, str], discord_factory=Discord) -> int`。返回 0 表示已发送，1 表示没有 webhook 或发送失败。消息里只有单元名、时间，以及一条查看日志的命令；不带日志内容，避免把敏感信息带出去。

- [ ] **Step 1: 写失败测试**

```python
# tests/ops/test_alert.py
from auto_app.ops.alert import send_failure


class Poster:
    sent = []

    def __init__(self, url):
        self.url = url

    def post(self, content=None, embeds=None):
        Poster.sent.append(content)

    def close(self):
        pass


def test_failures_are_posted_without_log_contents():
    Poster.sent = []
    code = send_failure("auto-app-worker.service", {"AUTO_APP_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/a"},
                        discord_factory=Poster)
    assert code == 0 and "auto-app-worker.service" in Poster.sent[0] and "journalctl --user -u" in Poster.sent[0]


def test_no_webhook_means_a_nonzero_exit():
    assert send_failure("x.service", {}, discord_factory=Poster) == 1


def test_unit_names_are_validated():
    Poster.sent = []
    assert send_failure("x; rm -rf /", {"AUTO_APP_DISCORD_WEBHOOK_URL": "u"}, discord_factory=Poster) == 1
    assert Poster.sent == []
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/ops/test_alert.py -q`
Expected: FAIL，`ModuleNotFoundError: auto_app.ops.alert`

- [ ] **Step 3: 实现**

```python
# src/auto_app/ops/alert.py
"""systemd OnFailure= → one Discord message naming the unit; no log contents (docs/operations.md §3)."""

import re
from collections.abc import Mapping
from datetime import datetime, timezone

from auto_app.core.discord import WEBHOOK_ENV, Discord, DiscordError

UNIT = re.compile(r"[A-Za-z0-9@._-]{1,120}\.(?:service|timer)")


def send_failure(unit: str, environ: Mapping[str, str], discord_factory=Discord) -> int:
    if not UNIT.fullmatch(unit or "") or not environ.get(WEBHOOK_ENV):
        return 1
    try:
        discord = discord_factory(environ[WEBHOOK_ENV])
        try:
            discord.post(f"🚨 {unit} failed at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC — "
                         f"`journalctl --user -u {unit} -n 50` on the host")
        finally:
            discord.close()
    except DiscordError:
        return 1
    return 0
```

```ini
# deploy/auto-app-alert@.service
[Unit]
Description=Job Search failure alert for %i

[Service]
Type=oneshot
WorkingDirectory=%h/Desktop/job-search/auto-application
EnvironmentFile=%h/Desktop/job-search/runtime/credentials/worker.env
ExecStart=%h/Desktop/job-search/auto-application/.venv/bin/auto-app ops alert --unit %i
```

在 `deploy/auto-app-worker.service`、`auto-app-agent.service`、`auto-app-agent-public.service`、`auto-app-bot.service`、`auto-app-preparation.service`、`job-search-tunnel.service`（以及 ingest 服务的生成模板 `src/auto_app/ingest/schedule_cli.py` 里的 `build_systemd_unit`）的 `[Unit]` 段加一行：

```ini
OnFailure=auto-app-alert@%n.service
```

`tests/test_cli_structure.py` 的 `ops` 加回 `alert`。`tests/ingest` 里如果有断言 systemd 单元全文的测试，同步更新期望文本。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest tests/ops tests/ingest tests/test_cli_structure.py -q`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准时）**

```bash
git add src/auto_app/ops/alert.py src/auto_app/ingest/schedule_cli.py deploy tests
git commit -m "feat(ops): Discord alert when a service fails

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: git 部署模式

**Files:**
- Modify: `scripts/deploy_linux.sh`（新增 `DEPLOY_MODE=git`；默认仍是 rsync，等用户确认后再改默认值）
- Test: `tests/worker/test_deploy.py`（在现有的 deployment fixture 上新增用例）

**Interfaces:**
- Produces：
  - `DEPLOY_MODE=git scripts/deploy_linux.sh home-ubuntu [REF]`：要求工作区干净；把 `REF`（默认 `HEAD`）push 到 `$BASE/repo.git` 的 `refs/heads/deploy`。
  - 远端依次执行：stop → 备份 → `git --git-dir=$BASE/repo.git --work-tree=$BASE/auto-application checkout -f deploy` → `clean -fd`（不带 `-x`，所以 `.venv` 这类被 ignore 的目录保留）→ 写 `runtime/.deployed`（内容：commit 和时间）→ `uv sync` → 迁移 → 恢复服务。
  - 一次性初始化（用户执行）：`ssh home-ubuntu git init --bare Desktop/job-search/repo.git`。

- [ ] **Step 1: 写失败测试**

在 `tests/worker/test_deploy.py` 末尾加。沿用该文件已有的 `deployment` fixture，它会伪造 ssh/systemctl/uv 并记录调用：

```python
def test_git_mode_refuses_a_dirty_tree(deployment):
    result = deployment.run(env={"DEPLOY_MODE": "git"}, dirty=True)
    assert result.returncode != 0 and "uncommitted" in result.stderr
    assert not deployment.remote_calls()  # nothing was stopped, backed up or pushed


def test_git_mode_pushes_the_ref_and_records_the_deployed_commit(deployment):
    result = deployment.run(env={"DEPLOY_MODE": "git"})
    assert result.returncode == 0, result.stderr
    calls = deployment.calls()
    assert any(c[:2] == ["git", "push"] and "refs/heads/deploy" in c[-1] for c in calls)
    assert deployment.remote_file("runtime/.deployed").split()[0] == deployment.head()
    order = deployment.phase_order()
    assert order.index("backup") < order.index("checkout") < order.index("migrate") < order.index("start")
```

> 执行者先读 `tests/worker/test_deploy.py` 里 `deployment` fixture 的实现。如果 fixture 还没有 `dirty`、`remote_calls`、`remote_file`、`head`、`phase_order` 这些辅助方法，就在 fixture 里补上：
> - 在临时目录里 `git init`，提交一次，作为干净的工作区；
> - `dirty=True` 时写一个未提交的文件；
> - 伪造的 `ssh` 用本地 `bash -s` 执行远端脚本，把 `$HOME` 指向临时远端目录；
> - 伪造的 `git push` 直接推到临时目录里的 bare repo。
>
> 断言里的阶段名（backup、checkout、migrate、start）要和脚本里的 `echo "phase: …"` 输出对应。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/worker/test_deploy.py -q`
Expected: 新增的两个测试 FAIL

- [ ] **Step 3: 实现**

在 `scripts/deploy_linux.sh` 里：
1. 参数解析之后加：

```bash
DEPLOY_MODE="${DEPLOY_MODE:-rsync}"
DEPLOY_REF="${2:-HEAD}"
if [[ "$DEPLOY_MODE" == git ]]; then
  if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    echo "Refusing to deploy: the working tree has uncommitted changes (commit or stash them first)." >&2
    exit 2
  fi
  DEPLOY_COMMIT="$(git rev-parse --verify "$DEPLOY_REF^{commit}")"
elif [[ "$DEPLOY_MODE" != rsync ]]; then
  echo "DEPLOY_MODE must be rsync or git" >&2
  exit 2
fi
```

2. 在 prepare 和备份完成之后，用 `if [[ "$DEPLOY_MODE" == git ]]; then … else <原 rsync 块> fi` 把同步代码的那一段包起来。git 分支：

```bash
  echo "phase: push"
  git push --force "$DEPLOY_HOST:$DEPLOY_BASE/repo.git" "$DEPLOY_COMMIT:refs/heads/deploy"
  ssh "$DEPLOY_HOST" bash -s -- "$DEPLOY_BASE" "$DEPLOY_COMMIT" <<'REMOTE'
set -euo pipefail
DEPLOY_BASE="$1"; DEPLOY_COMMIT="$2"
[[ "$DEPLOY_BASE" = /* ]] || DEPLOY_BASE="$HOME/$DEPLOY_BASE"
echo "phase: checkout"
git --git-dir="$DEPLOY_BASE/repo.git" --work-tree="$DEPLOY_BASE/auto-application" checkout -f deploy
git --git-dir="$DEPLOY_BASE/repo.git" --work-tree="$DEPLOY_BASE/auto-application" clean -fd
test "$(git --git-dir="$DEPLOY_BASE/repo.git" rev-parse deploy)" = "$DEPLOY_COMMIT"
printf '%s %s\n' "$DEPLOY_COMMIT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEPLOY_BASE/runtime/.deployed"
REMOTE
```

3. 原有的阶段前加上对应的 `echo "phase: backup"`、`echo "phase: migrate"`、`echo "phase: start"`，供测试检查执行顺序。

- [ ] **Step 4: 运行，确认通过；`bash -n` 检查语法**

Run: `uv run pytest tests/worker/test_deploy.py -q && bash -n scripts/deploy_linux.sh`
Expected: PASS

- [ ] **Step 5: Commit（仅在用户批准时）**

```bash
git add scripts/deploy_linux.sh tests/worker/test_deploy.py
git commit -m "feat(deploy): git mode — push a committed ref, record what is deployed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: 文档、部署和验收

**Files:**
- Create: `docs/operations.md`、`docs/application-tracking.md`、`docs/updates/2026-10-12-p3-tracking-and-operations.md`
- Modify: `docs/updates/README.md`、`docs/step3-apply.md`（时间线）、`docs/step4-run.md`、`README.md`、`config/feeds.example.yaml`（`application-mail` 示例，`enabled: false`）

- [ ] **Step 1: 写文档**

`docs/operations.md`：
- §1 备份：timer 的安装命令、保留策略、`ops verify` 的用法、恢复步骤（stop 所有服务 → 解压覆盖 `auto_app.db` → `ops verify` → start）。
- §2 异机备份：`scripts/pull_backups.sh`，以及怎么用 Mac 上的 launchd 每天运行它（给出 plist 示例，由用户自己安装）。
- §3 故障告警：`OnFailure=` 的原理，以及怎么手动测试（`systemctl --user start auto-app-alert@test.service`）。
- §4 磁盘告警的阈值。
- §5 git 部署：初始化 bare repo、`DEPLOY_MODE=git`、`runtime/.deployed`，以及从 rsync 模式迁过来的步骤（先提交，再部署一次）。

`docs/application-tracking.md`：
- 事件的种类和来源；
- 标为 applied 时会自动写事件；
- 邮件分类规则表；
- 匹配逻辑，以及待确认列表（`auto-app events unmatched/assign/dismiss`，MCP 的 `list_unmatched_updates`）；
- 周汇总的内容；
- 隐私：邮件正文不入库。

README：加 "Tracking after you apply" 和 "Operations" 两小段，并在命令表里加 `auto-app events …`、`auto-app ops …`。

- [ ] **Step 2:「用户」部署并验收**

1. 如果要用 git 模式：先提交并初始化 bare repo（Task 6）。否则继续用 rsync 部署。
2. 部署（会自动迁移到 `8d4e0f2a5b31`），然后安装备份 timer：

```bash
cp deploy/auto-app-backup.* deploy/auto-app-alert@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now auto-app-backup.timer
```

3. 手动跑一次 `auto-app ops backup`，确认 `verify.ok`；在 Mac 上运行 `scripts/pull_backups.sh`。
4. 用 `systemctl --user start auto-app-alert@test.service` 测试告警，Discord 应收到一条消息。
5. 在 feeds.yaml 加 `application-mail`（`purpose: updates`）；等一轮后运行 `auto-app events unmatched`，检查识别结果，用 `assign` / `dismiss` 处理。
6. 验收标准：
   - 每晚有一份新备份，`verify` 能打开；
   - 停掉 worker（`systemctl --user kill auto-app-worker`）时 Discord 收到告警；
   - 一封真实的拒信能被识别，并且匹配到正确的岗位，或者进入待确认列表；
   - 周一收到周汇总。

   结果记录到 `docs/updates/2026-10-12-p3-tracking-and-operations.md`。

- [ ] **Step 3: Commit（仅在用户批准时）**

```bash
git add docs README.md config/feeds.example.yaml
git commit -m "docs: application tracking and operations

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## 自检记录

- **spec 覆盖**：
  - spec §7 的投递后跟踪 → Task 1、2、3；
  - 备份 → Task 4；健康告警 → Task 3（磁盘）、Task 5（服务）；git 部署 → Task 6；
  - "收敛 README" → Task 7。
- **类型一致**：以下签名在定义处和使用处一致：
  - `record_event(store, job_id, kind, *, occurred_at, source, note, mail_update_id)`
  - `record_update(store, source, message_key, content, received)`
  - `backup` / `verify` / `prune`
  - `send_failure(unit, environ, discord_factory)`
  - `WorkerOptions` 新增的 `min_free_gb`
- **Review Focus**：五条都有对应测试，分别在 Task 2（两条）、Task 4（两条）、Task 6（一条）。
