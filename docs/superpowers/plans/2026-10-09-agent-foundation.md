# Agent 查询与 Linux 运行基础 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付能立即使用的轻量只读 Agent 查询、分页职位接口，以及 Linux systemd 启动文件生成；保留所有原有行为。

**Architecture:** 在现有数据库旁增加基于 sqlite3 的 QueryService，不调用 DbStore 或迁移。独立查询入口与薄 HTTP API 共用该服务；systemd 配置只生成文本，实际安装由部署阶段完成。主数据库仍只有一份。

**Tech Stack:** Python 3.12、stdlib sqlite3/argparse、现有 FastAPI、pytest；不增加依赖。

**Spec:** `docs/reviews/2026-10-09-job-agent-proposal.md`。

## Global Constraints

- 用户已批准设计并授权逐步执行，当前执行第一批可独立使用的功能；后续批次在下方列明，不宣称整套助手已实现。
- 优先功能，保留简单扩展点；不先实现通用权限/事件/分布式任务框架。
- 不读取 `.env`、`profile/` 或真实邮箱；不采集真实网站、不申请岗位、不 commit/push。
- 查询不能创建数据库、迁移 schema、修改 TTL/通知/决定；缺库和缺必要表要清楚报错。
- 不改变现有 step2/decision 含义，返回 legacy 状态；完整机会资格在后续批次实现。
- 新代码在 `feat/job-agent-foundation`；原生 worktree 因任务目录不是 Git repo 创建失败，使用当前独立功能分支。

## Review Focus

- 只读打开缺失数据库时不创建目录或文件；DDL/DML 被连接拒绝。
- 分页必须遍历已有所有职位，新插入不能导致重复或跳过原有条目；游标绑定查询条件。
- 归档职位仍能从 archive 读取标题/公司；已知但缺 primary posting 的记录不能消失。
- query CLI 不导入 Playwright/Textual/模型或加载私密配置；JSON 只写 stdout，错误到 stderr。
- systemd 路径含空格仍正确，显式保留 AUTO_APP_HOME；生成文本不安装服务。

## Interfaces

`QueryService(db_path: Path)` is a context manager. Methods return JSON-serializable dictionaries:

```python
with QueryService(db_path) as queries:
    context = queries.context()
    page = queries.jobs(query=None, state=None, company=None, source=None,
                        archived=False, limit=50, cursor=None)
    detail = queries.job(41)  # None if absent
```

Page: `items`, `total`, `returned`, `next_cursor`, `as_of`, `snapshot_max_id`. Context: `schema_version`, `as_of`, `database`, `counts`, `sources`, `latest_batch`, `capabilities`. Do not claim a general data revision or frozen mutable-field snapshot. Cursor bounds insertion by max job ID and descending last ID; rows are current at each query. Query/JSON errors raise `QueryError`.

## Task 1: Strict read-only queries

**Files:** Create `src/auto_app/query/__init__.py`, `src/auto_app/query/service.py`, `tests/query/test_service.py`.

**Produces:** QueryService and QueryError above; no changes to existing DB models.

- [x] Write tests with a temporary DbStore created by the test fixture: missing DB, no writes/migration, context counts/health, multi-page all IDs, insert between pages, query/state/source/company filters, archived detail, missing detail.
- [x] Run `.venv/bin/pytest tests/query/test_service.py -q`; verify feature absent before implementation.
- [x] Implement a read-only URI connection and explicit SELECTs. SQL filter/page before JSON projection; batch locations/sources per page. Detail includes parsed JD, postings, judgment reasons, reviews and feedback, excluding accounts/raw traces.

```python
conn = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
conn.execute('PRAGMA query_only=ON')
```

- [x] Run focused tests; check error conditions do not mutate business data. Do not pretend absent future evidence/tasks/resume tables are populated: context capabilities explicitly false.

## Task 2: Agent script, command and query API

**Files:** Create `src/auto_app/query/cli.py`, `src/auto_app/query/api.py`, `scripts/job_view.py`, `tests/query/test_cli.py`, `tests/query/test_api.py`; modify `pyproject.toml` entry points.

**Consumes:** Task 1 exact QueryService signatures.

**Produces:** `auto-app-query --db PATH context|jobs|job ID`; equivalent script; `auto_app.query.api.create_app(db_path)`.

- [x] Test command JSON, actionable missing DB/not-found errors, no heavy imports, API context/page/detail parity.
- [x] Run the focused tests to observe missing feature.
- [x] Implement argparse CLI and thin wrappers; `--db` defaults to AUTO_APP_HOME/data/auto_app.db or repository data path without loading .env. `--json` accepted; output always JSON.

```python
def create_app(db_path):
    app = FastAPI(title='auto-app read API')
    @app.get('/api/context')
    def context():
        with QueryService(db_path) as service:
            return service.context()
    return app
```

- [x] Add GET `/api/jobs` and `/api/jobs/{job_id}` using the same service; no write routes. Provide `auto-app-query --db PATH serve --host 127.0.0.1 --port 8766`; document SSH tunnel for remote use.
- [x] Run focused tests and `--help`; do not claim MCP or remote auth delivered in this batch.

## Task 3: Linux service generation

**Files:** Create `src/auto_app/service.py`, `tests/test_service.py`; modify only serve command registration in `src/auto_app/cli.py` for `serve systemd`.

**Produces:** `build_systemd_unit(project: Path, home: Path, uv: str) -> str`; `auto-app serve systemd` prints a system unit using current `serve run`, fixed work directory and AUTO_APP_HOME. No service installation.

- [x] Test generated ExecStart, WorkingDirectory, Environment, Restart, path quoting and CLI no-write behavior.
- [x] Run focused tests to observe missing feature.
- [x] Implement the small template and CLI arguments `--project`, `--home`, `--uv`; resolve uv from PATH with explicit error if missing. Keep existing Mac install/remove unchanged.

```ini
[Service]
Type=simple
Restart=on-failure
KillMode=control-group
TimeoutStopSec=60
```

- [x] Run focused tests and existing serve/CLI structure tests. Linux deployment itself is pending host access, not reported installed.

## Task 4: Documentation and integration verification

**Files:** README.md, docs/step4-run.md, docs/updates/README.md, docs/updates/2026-10-09-agent-foundation.md.

- [x] Document executable examples and which first-batch features are actually available; keep future capability list separate.
- [x] Run `.venv/bin/pytest -q`, inspect all failures; distinguish environment failures from regressions without weakening checks.
- [x] Run query script/API against a disposable fixture DB, verify the last page and no business writes; remove only generated temporary test files.
- [x] Independent reviewer checks full diff and focused regression results; fix user-facing defects and rerun affected tests.
- [x] Record exact results below and hand off current state without commit/push.

## Subsequent independent batches

1. **Opportunity ingestion and qualification:** repository JSON and saved-mail ingestion implemented in [the second batch](2026-10-09-multisource-ingestion.md); Gmail/Outlook OAuth providers and the independent recurring feed worker implemented in [the third batch](2026-10-10-mailbox-scheduling.md), with real authorization and Linux activation pending. Remaining work includes website inputs and qualification; generic IMAP can be added when needed. LinkedIn existing adapter, Jobright/Simplify verified individually; per-track possible/unknown/excluded and complete opportunity list.
2. **Public research:** evidence storage, platform coverage and company/topic reuse, provider-based searches, cited company brief.
3. **Resume orchestration:** register actual existing generators, job packet, isolated subagent outputs, artifact tracking; user still needs to provide repository locations.
4. **Persistent automation and host integration:** durable tasks, first MCP host, proactive notification delivery, shared progress. Implement only the recovery and concurrency controls exercised by these workflows.

## Execution record

- Baseline dependency installation: first attempt failed on sandbox DNS; authorized network retry used locked dependencies.
- New tests first failed for missing features: service 29, CLI/API 7, systemd 9. First integration check: 88 passed.
- Initial full baseline: 64 failed, 306 passed, 1 skipped. The first failure was missing Chromium. After installing it under `/private/tmp`, a diagnostic confirmed macOS sandbox permission denial; browser tests were rerun with permission to launch the browser.
- Full regression: `AUTO_APP_HOME=/private/tmp/auto-app-test-home PLAYWRIGHT_BROWSERS_PATH=/private/tmp/auto-app-playwright .venv/bin/pytest -q` → **415 passed, 1 skipped in 334.46s**. Existing web smoke is skipped because `web/dist` is not built.
- Independent reviewer found two defects: target Linux paths followed local symlinks; docs incorrectly put filters under separate runtime home. Both fixed and verified by reviewer. The new path regression failed before its fix; final targeted suite → **89 passed**. It includes the extra test added after the full run had collected its tests.
- Installed console, `python -S` standalone script and HTTP adapter passed a 3-job disposable-database smoke check; all pages returned, business data unchanged. `uv sync --frozen`, command help and `git diff --check` passed.
- Local warmed synthetic timing (2,000 jobs without postings, 30 open/context/first-page/close operations): median 1.72 ms, p95 2.55 ms. No production latency guarantee.
- No changes to runtime data, config or accounts. No commit/push.
