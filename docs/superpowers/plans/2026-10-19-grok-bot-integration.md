# Grok Bot 接入（第一版）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Grok Bot 通过本机执行接入求职流水线：服务器上的 `jobq` 命令和低权限 grokbot 账户，Mac 上的 apply-kit 材料包，以及 jd-resume-tailor 的取 JD、发布、投递后写 message。

**Architecture:** auto-app 新增一个轻量 CLI（`auto_app.jobq`），只封装现有的 digest、查询、决定和 handoff 服务；服务器上一个固定路径的包装脚本是 grokbot（经 sudo）和 Claude Code（经 ssh）共用的唯一入口。grokbot 的隔离靠 Linux 账户、sudo 单命令和 nftables 出口限制，root 步骤做成用户自己运行的脚本。jd-resume-tailor 新增标准库脚本 `scripts/apply_kit.py`，通过 ssh 调 `jobq`，把材料包写到 Mac 的 apply-kit。

**Tech Stack:** Python 3.12 + uv + pytest + argparse（auto-app）；Python ≥3.11 标准库 + unittest（jd-resume-tailor）；bash、sudoers、nftables、systemd、Xvfb、x11vnc（Ubuntu 26.04）。

**Spec:** `docs/superpowers/specs/2026-10-19-grok-bot-integration-design.md`（中文；执行者两份都要读）。

## Global Constraints

- 两个仓库都**不提交、不推送**，除非用户明确要求（auto-app 的 CLAUDE.md）。每个任务以"检查点"结束，改动保持未提交。
- Claude 永远不点 Submit，不替用户提交申请。部署到服务器（`scripts/deploy_linux.sh`）前必须先得到用户当次同意。
- 服务器 runtime 不加 `profile/`、`resumes/`、回答库；不开公网端口；8769 公网监听保持停用。
- 命令名 `jobq`（`jobs` 是 shell 内置命令）。console script 名 `auto-app-jobq`，模块 `auto_app.jobq.cli`。
- 认领时的 agent 名固定为 `grok-bot`。
- `jobq` 不接受 `--db`、`--preferences` 或任何文件路径参数；stdout 只输出一个 JSON 对象；退出码 0 成功、1 业务错误（`{"status": "error", "error": "…"}`）、2 参数或配置错误。
- 服务器路径：基础目录 `/home/postedism/Desktop/job-search`；包装脚本 `/home/postedism/Desktop/job-search/auto-application/deploy/jobq`；runtime `/home/postedism/Desktop/job-search/runtime`。
- Mac 路径：apply-kit `~/Desktop/Work/Search job/apply-kit`（路径里有空格）。
- handoff 额度：每天 20 个、同一公司 30 天 3 个（默认值）；租约 120 分钟（`runtime/config/handoff.json`：`{"lease_minutes": 120}`）。
- 签证规则：`offers`、`no`、`unknown` 都保留；`citizenship_required`（clearance、美国公民、U.S. person）排除。
- grokbot：home 权限 750、锁定密码、不属于任何附加组、开启 linger；sudo 只放行包装脚本；网络只放行 127.0.0.53/127.0.0.54 的 DNS 和公网 IPv4，拒绝本机地址、私有/CGNAT/链路本地 IPv4 和全部 IPv6；VNC 只监听 127.0.0.1:5990。
- 简历必须 1 页；每份开放题回答为英文、不超过 100 词。
- 需要 root 的操作做成脚本，由用户在服务器上用 sudo 运行；Claude 不输入、不读取任何密码或 token。

## Review Focus

- reason 或 note 里带换行或控制字符：应以退出码 2 拒绝，而不是把多行文本写进决定历史。→ Task 1 `test_rejects_paths_bad_text_and_unknown_values`。
- runtime 里没有数据库（新机器、路径错）：应返回 JSON 错误，且绝不创建空数据库。→ Task 1 `test_missing_home_or_database_is_a_json_error`。
- worker 还没写出 `notify_since`：`new` 应返回空列表，而不是报错。→ Task 1 `test_new_is_empty_before_the_worker_has_a_notify_cutoff`。
- ssh 失败（服务器关机、tailnet 断开）：`fetch` 应报错，且不在输出目录留下半成品文件。→ Task 4 `test_fetch_leaves_no_files_when_the_server_call_fails`。
- apply-kit 路径带空格、`index.json` 损坏：应正常处理空格路径；遇到损坏的索引时报错，不覆盖它，也不写材料包。→ Task 4 的测试都在名为 `apply kit` 的临时目录里运行，外加 `test_publish_rejects_a_corrupt_index_without_overwriting_it`。

---

### Task 1: `jobq` 命令（auto-app）

**Files:**
- Create: `src/auto_app/jobq/__init__.py`（只有一行模块说明）
- Create: `src/auto_app/jobq/cli.py`
- Modify: `pyproject.toml` 的 `[project.scripts]`：加 `auto-app-jobq = "auto_app.jobq.cli:main"`
- Test: `tests/jobq/__init__.py`、`tests/jobq/test_cli.py`

**Interfaces:**
- Consumes：`ControlService(db).digest(limit)`、`QueryService(db).jobs(...)` / `.job(id)`、`AgentService(db).record_application_status(id, status, reason=...)`、`HandoffService(db).queue(limit)` / `.claim(id, agent)` / `.packet(token)` / `.record_attempt(token, outcome, note, None)` / `.release(token)` / `.record_account(site, email, created_by_agent, note)`、`DbStore(db, Paths(home=home), migrate=False).job(id)` / `.set_notified(ids)`。preferences 一律传 `None`（这些服务会读 runtime 下的 `config/preferences.json`）。
- Produces：`main(argv: list[str] | None = None) -> int`；常量 `AGENT = "grok-bot"`。子命令与输出：

| 子命令 | 参数 | 输出 |
|---|---|---|
| `new` | `--limit` 1–50，默认 20 | `{"jobs": [...], "more": n}`（digest 原样） |
| `seen` | `ID...`，1–200 个 | `{"marked": [...], "unknown": [...]}`，按输入顺序去重 |
| `search` | `--query --company --source`（文本 ≤200）、`--state`（STATES 中除 archived 外）、`--sponsorship {offers,no,unknown}`、`--limit` 1–200 默认 50、`--cursor`（≤500） | `QueryService.jobs` 原样，`archived=False`、`include_excluded=False` |
| `show` | `ID` | `QueryService.job` 原样；不存在时报错 `job #ID not found` |
| `want` / `skip` | `ID --reason TEXT`（必填） | `{"job_id": ID, "status": "want_to_apply" 或 "not_applied", "changed": bool}` |
| `queue` | `--limit` 1–100，默认 20 | handoff 原样 |
| `claim` | `ID` | handoff 原样（agent 用 `AGENT`） |
| `packet` / `release` | `TOKEN` | handoff 原样 |
| `record` | `TOKEN {submitted,needs_user,blocked,failed,skipped} [--note TEXT]` | handoff 原样，evidence 固定为 `None` |
| `account` | `SITE EMAIL [--existing] [--note TEXT]` | handoff 原样，`created_by_agent = not existing` |

- 校验：ID 为 1–9223372036854775807 的整数；文本去掉首尾空白后非空、≤500 字符、不含 ord < 32 或 127 的字符；TOKEN 匹配 `^[A-Za-z0-9_-]{16,128}$`；所有校验失败都走 argparse 的 `parser.error`（退出码 2）。
- 启动顺序：先解析参数；`AUTO_APP_HOME` 未设置时输出 JSON 错误并返回 2；`$AUTO_APP_HOME/data/auto_app.db` 不是普通文件时输出 JSON 错误并返回 1。两项检查都在调用任何服务之前完成，所以不会建库。捕获 `QueryError`、`AgentError`、`HandoffError`、`ControlError`、`ValueError`，返回 1。服务在各自分支里延迟导入。JSON 用 `ensure_ascii=False, default=str`。

- [ ] **Step 1: 写测试 `tests/jobq/test_cli.py`**

```python
@pytest.fixture
def home(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer", sponsorship="Offers Sponsorship"),
                   record("102", "Data Engineer", sponsorship="Does Not Offer Sponsorship"),
                   record("103", "ML Engineer", company="Beta",
                          url="https://jobs.lever.co/beta/6ed76ce8-1111-2222-3333-444455556666"),
                   record("104", "Cleared Software Engineer", sponsorship="U.S. Citizenship is Required")])
    screen_jobs(store, ScreeningRules())
    now = utcnow()
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).values(first_seen_at=now - timedelta(hours=3)))
    titles = ("Backend Engineer", "Data Engineer", "ML Engineer", "Cleared Software Engineer")
    ids = {t: job_id_of(store, t) for t in titles}
    store.engine.dispose()
    _write_state(tmp_path / "data" / "auto_app.db", {"notify_since": (now - timedelta(hours=4)).isoformat()}, loop.STATE)
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    return tmp_path, ids


def run(capsys, *args):
    code = main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_new_shows_kept_jobs_sponsors_first_and_hides_clearance_jobs(home, capsys):
    _, ids = home
    code, out = run(capsys, "new")
    assert code == 0 and out["more"] == 0
    assert out["jobs"][0]["job_id"] == ids["Backend Engineer"]
    assert {j["job_id"] for j in out["jobs"]} == {ids["Backend Engineer"], ids["Data Engineer"], ids["ML Engineer"]}
    assert run(capsys, "new")[1] == out  # reading never marks anything


def test_new_is_empty_before_the_worker_has_a_notify_cutoff(home, capsys):
    root, _ = home
    (root / "data" / ("auto_app.db" + loop.STATE)).unlink()
    assert run(capsys, "new") == (0, {"jobs": [], "more": 0})


def test_seen_marks_listed_jobs_and_reports_unknown_ids(home, capsys):
    _, ids = home
    code, out = run(capsys, "seen", str(ids["Backend Engineer"]), "999999")
    assert code == 0 and out == {"marked": [ids["Backend Engineer"]], "unknown": [999999]}
    assert ids["Backend Engineer"] not in {j["job_id"] for j in run(capsys, "new")[1]["jobs"]}


def test_search_and_show_expose_the_visa_signal(home, capsys):
    _, ids = home
    assert [i["job_id"] for i in run(capsys, "search", "--sponsorship", "offers")[1]["items"]] == [ids["Backend Engineer"]]
    assert ids["Cleared Software Engineer"] not in {i["job_id"] for i in run(capsys, "search")[1]["items"]}
    code, job = run(capsys, "show", str(ids["Data Engineer"]))
    assert code == 0 and job["screening"]["signals"]["sponsorship"] == "no" and job["events"] == []
    assert run(capsys, "show", "999999") == (1, {"status": "error", "error": "job #999999 not found"})


def test_want_and_skip_need_a_reason_and_feed_the_queue(home, capsys):
    _, ids = home
    with pytest.raises(SystemExit) as exc:
        main(["want", str(ids["ML Engineer"])])
    assert exc.value.code == 2
    assert run(capsys, "want", str(ids["ML Engineer"]), "--reason", "confirmed by me")[1] == \
        {"job_id": ids["ML Engineer"], "status": "want_to_apply", "changed": True}
    assert run(capsys, "skip", str(ids["Data Engineer"]), "--reason", "not interested")[1]["status"] == "not_applied"
    assert [i["job_id"] for i in run(capsys, "queue")[1]["items"]] == [ids["ML Engineer"]]


def test_claim_packet_record_round_trip_uses_the_fixed_agent(home, capsys):
    root, ids = home
    run(capsys, "want", str(ids["ML Engineer"]), "--reason", "confirmed by me")
    claim = run(capsys, "claim", str(ids["ML Engineer"]))[1]
    packet = run(capsys, "packet", claim["token"])[1]
    assert packet["job"]["job_id"] == ids["ML Engineer"]
    assert (packet["profile"], packet["answers"], packet["resumes"]) == ({}, {}, [])
    store = make_store(root)
    with store.engine.connect() as conn:
        assert conn.execute(select(T.handoff_claims.c.agent)).scalar() == "grok-bot"
    store.engine.dispose()
    code, out = run(capsys, "record", claim["token"], "needs_user", "--note", "email verification")
    assert code == 0 and out["status"] == "needs_input"
    assert run(capsys, "record", claim["token"], "failed")[0] == 1  # the claim was consumed


def test_release_and_account_metadata(home, capsys):
    _, ids = home
    run(capsys, "want", str(ids["ML Engineer"]), "--reason", "confirmed by me")
    token = run(capsys, "claim", str(ids["ML Engineer"]))[1]["token"]
    assert run(capsys, "release", token)[1] == {"job_id": ids["ML Engineer"], "released": True}
    assert run(capsys, "account", "acme.example.com", "alex@example.com", "--existing")[1]["created_by_agent"] is False


@pytest.mark.parametrize("args", [
    ["--db", "x.db", "new"], ["new", "--db", "x.db"], ["show", "1", "--preferences", "p.json"],
    ["new", "--limit", "51"], ["seen"], ["want", "1", "--reason", "line one\nline two"],
    ["want", "1", "--reason", "   "], ["packet", "not a token!"], ["record", "x" * 20, "maybe"],
    ["search", "--sponsorship", "citizenship_required"], ["seen", *[str(i) for i in range(1, 202)]]])
def test_rejects_paths_bad_text_and_unknown_values(home, args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2


def test_missing_home_or_database_is_a_json_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AUTO_APP_HOME", raising=False)
    code, out = run(capsys, "queue")
    assert code == 2 and "AUTO_APP_HOME" in out["error"]
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    code, out = run(capsys, "show", "1")
    assert code == 1 and out["status"] == "error" and "database" in out["error"]
    assert not (tmp_path / "data" / "auto_app.db").exists()
```

导入：`json`、`timedelta`、`pytest`、`select`、`update`（sqlalchemy）、`auto_app.core.db.tables as T`、`utcnow`、`_write_state`（`auto_app.ingest.scheduler`）、`loop`（`auto_app.worker`）、`ScreeningRules`、`screen_jobs`、`main`（`auto_app.jobq.cli`）、`tests.imported_jobs` 的 `ingest, job_id_of, make_store, record`。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/jobq/test_cli.py`
Expected: FAIL，`ModuleNotFoundError: No module named 'auto_app.jobq'`

- [ ] **Step 3: 实现 `src/auto_app/jobq/cli.py` 和 `__init__.py`，并在 `pyproject.toml` 注册 console script**

按上面的 Interfaces 实现：argparse 子命令、类型函数 `positive_int`、`bounded(low, high)`、`plain_text(limit)`、`token`；`_database() -> Path` 做两项启动检查；每个子命令对应一个 `_cmd_<name>(args, db, home) -> dict`。`seen` 用 `DbStore(..., migrate=False)`，先用 `store.job(id)` 区分已知和未知 ID，再对已知 ID 调 `set_notified`，最后 `store.engine.dispose()`。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/jobq/test_cli.py && uv sync && uv run auto-app-jobq --help`
Expected: 全部 PASS；`--help` 列出 12 个子命令。

- [ ] **Step 5: 检查点**

Run: `uv run pytest -q tests/handoff tests/agent tests/jobq`
Expected: 全部 PASS。改动保持未提交。

---

### Task 2: 服务器包装脚本 `deploy/jobq` 和文档（auto-app）

**Files:**
- Create: `deploy/jobq`（可执行，`chmod 755`）
- Test: `tests/jobq/test_wrapper.py`
- Create: `docs/grok-bot.md`（中文，面向用户：三台机器的角色、`jobq` 命令表、Claude Code 的 ssh 用法、grokbot 隔离边界和剩下的可见性、apply-kit 位置）
- Create: `docs/updates/2026-10-19-grok-bot-integration.md`；Modify: `docs/updates/README.md`（表格加一行，状态写"本地实现；部署和实机验收待做"）
- Modify: `README.md`（在 Remote MCP 一节后加一小节"Grok Bot（本机执行）"：`deploy/jobq` 和 `auto-app-jobq` 的用法，与 `--help` 一致）；`docs/agent-handoff.md`（加一句：`jobq` 以固定 agent 名 `grok-bot` 暴露 queue/claim/packet/record/release/account）；`docs/step4-run.md`（状态表加一行"Grok Bot 本机执行：本地实现，部署和实机验收待做"，并链接 `docs/grok-bot.md`）

**Interfaces:**
- Consumes：Task 1 的 `auto_app.jobq.cli`。
- Produces：服务器路径 `/home/postedism/Desktop/job-search/auto-application/deploy/jobq`，供 Task 3 的 sudoers 和 Task 4 的 `apply_kit.json` 使用。用法：`deploy/jobq <子命令> [参数]`，输出与退出码同 Task 1。

脚本全文（安全边界，按原样写）：

```bash
#!/bin/bash
# The only command grokbot may run as postedism (via sudo), and the one Claude Code calls over ssh.
# It fixes the runtime home and starts Python in isolated mode; the caller's environment is not trusted.
set -euo pipefail
repo="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
runtime="$(dirname "$repo")/runtime"
cd "$runtime"
export AUTO_APP_HOME="$runtime"
exec "$repo/.venv/bin/python" -I -m auto_app.jobq.cli "$@"
```

- [ ] **Step 1: 写测试 `tests/jobq/test_wrapper.py`**

```python
from tests.jobq.test_cli import home  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def layout(tmp_path, runtime):
    base = tmp_path / "job search base"
    deploy = base / "auto-application" / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy2(ROOT / "deploy" / "jobq", deploy / "jobq")
    shim = base / "auto-application" / ".venv" / "bin" / "python"
    shim.parent.mkdir(parents=True)
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    (base / "runtime").symlink_to(runtime)
    return deploy / "jobq"


def test_wrapper_forces_the_runtime_home_and_isolated_python(home, tmp_path):  # noqa: F811
    runtime, ids = home
    evil = tmp_path / "evil" / "auto_app"
    evil.mkdir(parents=True)
    (evil / "__init__.py").write_text("raise SystemExit('shadowed')\n")
    env = {"PATH": os.environ["PATH"], "AUTO_APP_HOME": str(tmp_path / "elsewhere"),
           "PYTHONPATH": str(evil.parent)}
    result = subprocess.run([str(layout(tmp_path, runtime)), "new"], env=env, capture_output=True,
                            text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert {j["job_id"] for j in json.loads(result.stdout)["jobs"]} == \
        {ids["Backend Engineer"], ids["Data Engineer"], ids["ML Engineer"]}


def test_wrapper_is_executable_and_keeps_usage_errors(home, tmp_path):  # noqa: F811
    runtime, _ = home
    assert os.stat(ROOT / "deploy" / "jobq").st_mode & stat.S_IXUSR
    result = subprocess.run([str(layout(tmp_path, runtime)), "new", "--db", "x"],
                            env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert result.returncode == 2
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/jobq/test_wrapper.py`
Expected: FAIL（`deploy/jobq` 不存在）

- [ ] **Step 3: 按上面的全文创建 `deploy/jobq` 并 `chmod 755`**

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/jobq`
Expected: 全部 PASS

- [ ] **Step 5: 写文档**

`docs/grok-bot.md` 的命令表与 Task 1 的表一致；明确写出：Mac 上 Claude Code 用 `ssh home-ubuntu /home/postedism/Desktop/job-search/auto-application/deploy/jobq <子命令>`；grokbot 用 `jobq <子命令>`；`needs_user`/`blocked` 后的 `requeue` 只能由用户执行 `AUTO_APP_HOME=… auto-app handoff requeue`。

- [ ] **Step 6: 检查点**

Run: `uv run auto-app-jobq --help` 与 README 新小节逐项对照；`uv run pytest -q tests/jobq tests/ops`
Expected: 一致；全部 PASS。改动保持未提交。

---

### Task 3: grokbot 账户的安装与检查脚本（auto-app `deploy/grokbot/`）

**Files（全部新建）:**
- `deploy/grokbot/grokbot-jobq.sudoers`
- `deploy/grokbot/jobq-client`（安装为 `/usr/local/bin/jobq`）
- `deploy/grokbot/egress.nft`（安装为 `/etc/grokbot/egress.nft`）
- `deploy/grokbot/grokbot-egress.service`（系统单元）
- `deploy/grokbot/user/grokbot-display.service`、`deploy/grokbot/user/grokbot-app.service`、`deploy/grokbot/user/grokbot-vnc.service`（grokbot 的用户单元）
- `deploy/grokbot/setup.sh`（root 运行，`setup.sh base` 与 `setup.sh app` 两步，可重复执行）
- `deploy/grokbot/check.sh`（root 运行的验收探测）
- Test: `tests/ops/test_grokbot_files.py`

（放在子目录里，`tests/ops/test_alert.py` 只检查 `deploy/*.service`，不会要求这些单元带 OnFailure。）

**Interfaces:**
- Consumes：Task 2 的包装脚本路径。
- Produces：服务器上的 `/usr/local/bin/jobq`、用户 `grokbot`、`grokbot-egress.service`、grokbot 的三个用户单元、`/usr/local/libexec/grokbot-app`；`check.sh` 每项输出一行 `PASS <名称>`、`FAIL <名称>` 或 `REPORT <名称>: <内容>`，有 FAIL 时退出码 1。

固定内容（安全边界，按原样写）：

`grokbot-jobq.sudoers`：
```
Defaults:grokbot env_reset, !requiretty
grokbot ALL=(postedism) NOPASSWD: /home/postedism/Desktop/job-search/auto-application/deploy/jobq
```

`jobq-client`：
```sh
#!/bin/sh
exec /usr/bin/sudo -n -u postedism /home/postedism/Desktop/job-search/auto-application/deploy/jobq "$@"
```

`egress.nft`（用正向匹配跳转，避免影响没有 socket 的内核包）：
```
table inet grokbot_egress
delete table inet grokbot_egress
table inet grokbot_egress {
	chain output {
		type filter hook output priority filter; policy accept;
		meta skuid "grokbot" jump from_grokbot
	}
	chain from_grokbot {
		ct state established,related accept
		ip daddr { 127.0.0.53, 127.0.0.54 } meta l4proto { tcp, udp } th dport 53 accept
		oifname "lo" reject with icmpx admin-prohibited
		ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 } reject with icmpx admin-prohibited
		meta nfproto ipv6 reject with icmpx admin-prohibited
	}
}
```

单元要点：
- `grokbot-egress.service`：`Type=oneshot`、`RemainAfterExit=yes`、`ExecStart=/usr/sbin/nft -f /etc/grokbot/egress.nft`、`ExecStop=/usr/sbin/nft delete table inet grokbot_egress`、`After=nftables.service`、`WantedBy=multi-user.target`。
- `setup.sh base` 写入 `/etc/systemd/system/user@<grokbot 的 UID>.service.d/10-grokbot-egress.conf`，内容为 `[Unit]`、`Requires=grokbot-egress.service`、`After=grokbot-egress.service`（防火墙没加载，grokbot 的用户服务就不启动）。
- `grokbot-display.service`：`ExecStart=/usr/bin/Xvfb :90 -screen 0 1440x900x24 -nolisten tcp`。
- `grokbot-app.service`：`Requires`/`After=grokbot-display.service`、`Environment=DISPLAY=:90`、`ExecStart=/usr/local/libexec/grokbot-app`、`Restart=on-failure`、`RestartSec=10`。
- `grokbot-vnc.service`：`ExecStart=/usr/bin/x11vnc -display :90 -localhost -rfbport 5990 -rfbauth %h/.vnc/passwd -forever -shared`。
- 三个用户单元都写 `WantedBy=default.target`。

`setup.sh base`（root；任何一步失败就退出，并打印失败原因）：
1. 检查 root、用户 postedism 存在、包装脚本存在且可执行、包装脚本和它所在的目录对 group 和 others 都不可写。
2. 没有 grokbot 就 `useradd --create-home --user-group --shell /bin/bash grokbot`；然后 `passwd -l grokbot`、`usermod -G "" grokbot`、`chmod 750 /home/grokbot`、`loginctl enable-linger grokbot`。
3. 先用 `visudo -cf` 检查 sudoers 文件，再 `install -m 0440` 到 `/etc/sudoers.d/grokbot-jobq`；`install -m 0755 jobq-client /usr/local/bin/jobq`。
4. 安装 `/etc/grokbot/egress.nft` 并先用 `nft -c -f` 检查语法，再装系统单元和 user@ drop-in；`systemctl daemon-reload`；`systemctl enable --now grokbot-egress.service`。
5. `apt-get install -y xvfb x11vnc`。
6. 打印下一步：去 Grok Bot 官网下载 Linux .deb，`sudo apt install ./<文件>.deb`，然后运行 `setup.sh app`。

`setup.sh app`（root；在用户装好 .deb 之后运行）：
1. 从 `/usr/share/applications/` 里 Name 含 "Grok Bot" 的 .desktop 文件读出 `Exec=` 的可执行文件（去掉 `%U` 等占位符）；找不到就报错退出。
2. 写 `/usr/local/libexec/grokbot-app`（root 所有，755），内容为 `exec "<可执行文件>" --password-store=basic "$@"`。
3. 如果 `/etc/apparmor.d/` 里没有任何配置引用这个可执行文件，就写 `/etc/apparmor.d/grokbot-electron`（`abi <abi/4.0>,`、`include <tunables/global>`、`profile grokbot-electron "<可执行文件>" flags=(unconfined) { userns, include if exists <local/grokbot-electron> }`），再 `apparmor_parser -r`。
4. 把三个用户单元装到 `/home/grokbot/.config/systemd/user/`（属主 grokbot）。用随机密码（`openssl rand -base64 12`）生成 `/home/grokbot/.vnc/passwd`（`x11vnc -storepasswd`，权限 600），密码只在终端打印这一次。然后 `systemctl --user --machine=grokbot@ daemon-reload` 和 `enable --now grokbot-display grokbot-app grokbot-vnc`。
5. 打印 VNC 连接方法：在 Mac 上运行 `ssh -N -L 5990:127.0.0.1:5990 home-ubuntu`，再打开 `vnc://127.0.0.1:5990`。

`check.sh`（root；用 `runuser -u grokbot --` 以 grokbot 身份执行探测；TCP 用 `timeout 3 bash -c 'exec 3<>/dev/tcp/HOST/PORT'`）：
- PASS 项：`jobq new --limit 1` 退出码 0 且 JSON 含 `jobs`；`jobq queue` 退出码 0；`jobq new --db x` 退出码 2；`ls /home/postedism` 失败；读 runtime 数据库失败；`sudo -n -l` 只有一条 NOPASSWD 规则且是包装脚本；`sudo -n -u postedism id` 失败；`sudo -n id` 失败；docker 和 lxd 套接字都不可写；连接 127.0.0.1:8767、127.0.0.1:8768、本机局域网 IP（`ip -4 route get 1.1.1.1` 里的 src）的 6379 和 1433、Mac 的 tailnet IP 100.104.170.71:22、[::1]:6379 全部失败；`curl -sS -o /dev/null -w '%{http_code}' https://www.cloudflare.com/cdn-cgi/trace` 得到 200；`grokbot-egress.service` 处于 active。
- REPORT 项（不算失败）：postedism 进程命令行里匹配 `token|secret|password|api[_-]?key` 的行数（只输出数量）；grokbot 能否运行 `tailscale status`；`lpstat -r` 的结果；`/tmp` 下 postedism 所有、others 可读的文件数量。

- [ ] **Step 1: 写测试 `tests/ops/test_grokbot_files.py`**

```python
G = Path(__file__).resolve().parents[2] / "deploy" / "grokbot"
WRAPPER = "/home/postedism/Desktop/job-search/auto-application/deploy/jobq"


def test_sudoers_allows_only_the_jobq_wrapper_as_postedism():
    rules = [l for l in (G / "grokbot-jobq.sudoers").read_text().splitlines() if l and not l.startswith(("#", "Defaults"))]
    assert rules == [f"grokbot ALL=(postedism) NOPASSWD: {WRAPPER}"]
    assert WRAPPER in (G / "jobq-client").read_text() and "sudo -n -u postedism" in (G / "jobq-client").read_text()


def test_egress_rules_order_dns_before_the_loopback_reject_and_refuse_ipv6():
    text = (G / "egress.nft").read_text()
    assert text.index("delete table inet grokbot_egress") < text.index("chain output")
    assert 'meta skuid "grokbot" jump from_grokbot' in text
    chain = text[text.index("chain from_grokbot"):]
    order = ["ct state established,related accept", "127.0.0.53, 127.0.0.54", 'oifname "lo" reject',
             "100.64.0.0/10", "meta nfproto ipv6 reject"]
    assert [chain.index(s) for s in order] == sorted(chain.index(s) for s in order)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
        assert net in chain


def test_user_manager_requires_the_egress_unit_and_app_runs_hidden():
    setup = (G / "setup.sh").read_text()
    assert "Requires=grokbot-egress.service" in setup and "After=grokbot-egress.service" in setup
    assert "--password-store=basic" in setup and 'usermod -G ""' in setup and "visudo -cf" in setup
    assert "-nolisten tcp" in (G / "user" / "grokbot-display.service").read_text()
    vnc = (G / "user" / "grokbot-vnc.service").read_text()
    assert "-localhost" in vnc and "-rfbport 5990" in vnc and "-rfbauth" in vnc
    assert "DISPLAY=:90" in (G / "user" / "grokbot-app.service").read_text()


def test_check_script_probes_every_boundary():
    check = (G / "check.sh").read_text()
    for probe in ("8767", "8768", "6379", "1433", "100.104.170.71", "sudo -n -u postedism", "--db", "REPORT"):
        assert probe in check


@pytest.mark.parametrize("script", ["setup.sh", "check.sh", "jobq-client"])
def test_scripts_parse(script):
    assert subprocess.run(["bash", "-n", str(G / script)]).returncode == 0
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/ops/test_grokbot_files.py`
Expected: FAIL（文件不存在）

- [ ] **Step 3: 按上面的固定内容和步骤写出 `deploy/grokbot/` 下的全部文件；脚本加执行权限**

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/ops`
Expected: 全部 PASS

- [ ] **Step 5: 在 `docs/grok-bot.md` 加"服务器安装"一节**：两步 setup、装 .deb、VNC 登录、Grok Bot 里两台电脑的执行设置（Ubuntu 始终允许，Mac 每次询问）、Auto-review 的 Ask first 规则、`check.sh` 怎么看结果。

- [ ] **Step 6: 检查点**

Run: `uv run pytest -q tests/ops tests/jobq`
Expected: 全部 PASS。改动保持未提交。

---

### Task 4: `scripts/apply_kit.py` 和 apply-kit README（jd-resume-tailor + Mac）

**Files:**
- Create: `scripts/apply_kit.py`（只用标准库）
- Create: `apply_kit.json`（技能根目录，不放在 `data/` 下）：
  ```json
  {"ssh_host": "home-ubuntu",
   "jobq": "/home/postedism/Desktop/job-search/auto-application/deploy/jobq",
   "apply_kit_dir": "~/Desktop/Work/Search job/apply-kit"}
  ```
- Test: `tests/test_apply_kit.py`（unittest）
- Create（仓库外）: `~/Desktop/Work/Search job/apply-kit/README.md`

**Interfaces:**
- Consumes：`jobq show ID`（字段：`title`、`company`、`locations`、`canonical_url`、`posting_details[].apply_url`、`parsed.description_text`、`screening.signals.sponsorship`、`events[]` 中 `kind == "applied"` 的 `occurred_at`）、`jobq queue --limit 100`（`items[].job_id/title/company`）、`jobq search --state applied --limit 200`（`items[].job_id`）。build 报告字段：`status`、`build_id`、`pdf_sha256`、`page_count`。
- Produces（Task 5 的 SKILL.md 使用）：
  - `class ApplyKitError(Exception)`
  - `@dataclass(frozen=True) class Config: ssh_host: str; jobq: str; apply_kit_dir: Path`；`load_config(path: Path) -> Config`（展开 `~`）
  - `class Jobq: __init__(self, ssh_host: str, jobq: str, run=subprocess.run)`；`call(self, *args: str) -> dict`：运行 `["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", ssh_host, shlex.join([jobq, *args])]`，超时 120 秒；退出码非 0、stdout 不是 JSON、或 JSON 里 `status == "error"` 时抛 `ApplyKitError`（带服务器给的错误信息）。
  - `fetch(cfg: Config, jobq: Jobq, job_id: int, out: Path) -> dict`：写 `out/job.json`（`job_id, title, company, apply_url, locations, sponsorship, applied_at, fetched_at`）；有 JD 原文时写 `out/jd.txt`；返回 `{"job_id", "out", "jd": bool, "apply_url", "applied_at"}`。`apply_url` 取 `canonical_url`，没有时取第一个 `posting_details[].apply_url`。服务器调用成功之前不写任何文件。
  - `publish(cfg: Config, out: Path, replace: bool = False) -> dict`：校验通过后，在 `jobs/` 下先写临时目录再改名，生成 `jobs/<job_id>/`（含 `job.json`，有的话加 `jd.txt`，以及 `resume.pdf`、`cover_letter.pdf`、`answers.md`）；包内 `job.json` 字段为 `job_id, company, title, apply_url, locations, sponsorship, published_at, resume_build_id, cover_letter_build_id, files: {文件名: {"sha256", "bytes"}}, message: null`；原子更新 `jobs/index.json`：`{"jobs": [{"job_id", "company", "title", "published_at", "has_message"}]}`，按 job_id 排序。返回 `{"job_id", "package", "files"}`。
    - 校验：resume 和 cover letter 的 build 报告都是 `status == "success"`，含 `build_id`，`pdf_sha256` 与 PDF 一致；resume 的 `page_count == 1`；`answers.md` 非空；`job.json` 含整数 `job_id`；`index.json` 存在时必须是合法 JSON。材料包已存在且没加 `replace` 时报错 "already published"。
    - 不复制 `message.txt`。
  - `add_message(cfg: Config, jobq: Jobq, out: Path) -> dict`：要求 `out/message.txt` 非空、材料包存在、`jobq show` 里有 applied 记录。把 message 原子写入材料包；包内 `job.json` 加上 `message: {"added_at", "applied_at"}` 和这个文件的哈希；`index.json` 里该职位的 `has_message` 设为 true。
  - `todo(cfg: Config, jobq: Jobq) -> dict`：`{"needs_package": [{"job_id", "title", "company"}], "needs_message": [{"job_id", "title", "company", "applied_at"}]}`。
  - CLI：`python scripts/apply_kit.py [--config PATH] {todo | fetch ID --out DIR | publish DIR [--replace] | add-message DIR}`。默认配置文件是技能根目录下的 `apply_kit.json`。成功打印 JSON；出错打印 `{"status": "error", "error": …}`，退出码 1。

- [ ] **Step 1: 写测试 `tests/test_apply_kit.py`**

固定数据与假对象：
```python
JOB = {"job_id": 42, "title": "Software Engineer", "company": "Acme", "locations": ["Seattle, WA"],
       "canonical_url": "https://jobs.example.com/42", "parsed": {"description_text": "Build things."},
       "screening": {"signals": {"sponsorship": "offers"}}, "events": [], "posting_details": []}
APPLIED = {**JOB, "events": [{"kind": "applied", "occurred_at": "2026-10-20T17:00:00+00:00"}]}


class FakeJobq:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def call(self, *args):
        self.calls.append(args)
        value = self.responses[args]
        if isinstance(value, Exception):
            raise value
        return value


def built(out, name, pdf=b"%PDF-1.4 fixture", status="success", pages=1, build_id="b1", sha=None):
    (out / f"{name}.pdf").write_bytes(pdf)
    report = {"status": status, "build_id": build_id, "page_count": pages,
              "pdf_sha256": sha or hashlib.sha256(pdf).hexdigest()}
    (out / f"{name}.build.json").write_text(json.dumps(report))
```

每个测试在 `tempfile.TemporaryDirectory()` 下建 `apply kit/`（名字带空格）作为 `apply_kit_dir`，另建一个输出目录 `out/`。测试与断言：
- `test_fetch_writes_jd_and_job_json`：`fetch(cfg, FakeJobq({("show", "42"): JOB}), 42, out)` → `jd.txt == "Build things."`；`job.json` 的 `apply_url == "https://jobs.example.com/42"`、`sponsorship == "offers"`、`applied_at is None`；返回值 `jd is True`。
- `test_fetch_without_description_writes_no_jd_and_returns_the_link`：description 为空 → 没有 `jd.txt`；返回值 `jd is False`、`apply_url` 正确。
- `test_fetch_records_the_applied_date`：用 `APPLIED` → `job.json["applied_at"] == "2026-10-20T17:00:00+00:00"`。
- `test_fetch_leaves_no_files_when_the_server_call_fails`：返回值是 `ApplyKitError("ssh failed")` → 抛 `ApplyKitError`，`out` 里没有任何文件。
- `test_publish_copies_files_with_hashes_and_updates_the_index`：先 fetch，再 `built(out, "resume")`、`built(out, "cover_letter", build_id="c1")`，写 `answers.md` → 材料包正好含 5 个文件（`job.json`、`jd.txt`、`resume.pdf`、`cover_letter.pdf`、`answers.md`）；包内 `job.json` 的 `files["resume.pdf"]["sha256"]` 与文件一致，`resume_build_id == "b1"`，`message is None`；`index.json == {"jobs": [{"job_id": 42, "company": "Acme", "title": "Software Engineer", "published_at": <同包内>, "has_message": False}]}`。
- `test_publish_refuses_bad_builds_and_missing_files`：用 subTest 逐个检查：resume `status="failed"`；`sha="0"*64`；`pages=2`；删除 `answers.md`；删除 `cover_letter.build.json`；报告里去掉 `build_id`。每种情况都抛 `ApplyKitError`，且 `jobs/42` 不存在。
- `test_publish_needs_replace_to_overwrite`：第二次 publish 抛 "already published"；`replace=True` 后成功，resume 哈希更新为新 PDF 的哈希；`jobs/` 下没有残留的临时目录。
- `test_publish_rejects_a_corrupt_index_without_overwriting_it`：`index.json` 内容为 `"{broken"` → 抛错；`index.json` 内容不变；`jobs/42` 不存在。
- `test_add_message_requires_an_applied_record`：`show` 返回 `JOB` → 抛错，错误信息含 "applied"。
- `test_add_message_copies_the_message_and_marks_the_index`：`show` 返回 `APPLIED` → 包里有 `message.txt`；`job.json["message"]["applied_at"]` 是投递日期；index 里 `has_message is True`。
- `test_todo_lists_jobs_needing_packages_and_messages`：queue 里有 42、43；index 里有 42 和 44（都是 `has_message: False`）；`search --state applied` 返回 44；`show 44` 有 applied 记录 → `needs_package` 只有 43，`needs_message` 只有 44（带 `applied_at`）。
- `test_jobq_call_builds_the_ssh_command_and_raises_on_errors`：用假 `run` 记录 argv，返回 `subprocess.CompletedProcess(argv, 0, stdout='{"items": []}', stderr="")` → argv 为 `["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "home-ubuntu", "/srv/jobq queue --limit 100"]`；退出码 1 且 stdout 为 `{"status":"error","error":"x"}` → 抛 `ApplyKitError("x")`；stdout 不是 JSON → 抛 `ApplyKitError`。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m unittest tests.test_apply_kit -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'scripts.apply_kit'`）

- [ ] **Step 3: 按 Interfaces 实现 `scripts/apply_kit.py`，并创建 `apply_kit.json`**

原子写法：同目录 `tempfile.mkstemp` / `mkdtemp` 加 `os.replace`。replace 时先把旧包改名为 `.old-<job_id>-<随机>`，把新包改名到位，再删除旧包。

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m unittest discover -s tests`
Expected: 原有 63 个加上新增测试，全部 OK

- [ ] **Step 5: 写 `~/Desktop/Work/Search job/apply-kit/README.md`**：目录结构（同设计 §6.3）；每个文件的含义；Ubuntu 上用 Shell 跑 `jobq …`，Mac 上只用 Read 和 CopyToBox；投递前核对 `job.json` 的 job_id 和每个 PDF 的 sha256；message 在投递之后才出现；签证三类规则见 `answers.md` 使用规则第 6 条；`jobq` 子命令一览。

- [ ] **Step 6: 检查点**

Run: `.venv/bin/python scripts/apply_kit.py --help`
Expected: 列出 4 个子命令。改动保持未提交。

---

### Task 5: jd-resume-tailor 的 SKILL.md 和 README.md

**Files:**
- Modify: `SKILL.md`（第一阶段、生成子 agent 模板、第八阶段、新增"投递后写 message"、新增最后的"发布给 Grok"、"处理队列"、边缘情况表、文件参考表）
- Modify: `README.md`（Usage 加 `apply_kit.py` 四个子命令）

**Interfaces:**
- Consumes：Task 4 的 CLI：`scripts/apply_kit.py todo | fetch ID --out DIR | publish DIR [--replace] | add-message DIR`。

要写进 SKILL.md 的决定（措辞由执行者定，事实不能变）：
1. 第一阶段 JD 来源新增"auto-app 职位编号"：运行 `.venv/bin/python scripts/apply_kit.py fetch <编号> --out output/<公司_岗位>_<编号>`；有 `jd.txt` 就继续；没有时用返回的 `apply_url` 按 URL 方式取 JD，并存成 `jd.txt`。
2. 生成子 agent 模板的产出加一项 `answers.md`：英文回答三道题（为什么选这家公司或这个岗位；自我介绍；一个与 JD 最相关的项目或技术难题），每道不超过 100 词，只用这份简历和 cover letter 里的事实。返回列表加一项"answers.md 的三道题"。修改循环里与 cover letter 一起更新。
3. 第八阶段：从 auto-app 来的职位在生成时跳过 InMail，在总结里写"投递后再写 message"。粘贴 JD 的旧用法不变。
4. 新增"投递后写 message"：`apply_kit.py todo` 的 `needs_message` 列出职位后，新建一个生成子 agent，给它 `jd.txt`、`analysis.md`、方向 track、当前的 `resume.tex` 和 `cover_letter.tex`，以及 `todo` 返回的 `applied_at`；按 `references/linkedin-message-guide.md` 写已投递版本，投递时间用这个日期，只引用这份简历上的经历；存为 `output/<目录>/message.txt` 并打印到屏幕；然后运行 `apply_kit.py add-message output/<目录>`。
5. 新增最后一步"发布给 Grok"：用户确认最终版本后运行 `apply_kit.py publish output/<目录>`；没有用户确认不发布；没有职位编号时跳过；需要重发时加 `--replace`。
6. "处理队列"：先运行 `apply_kit.py todo`，对 `needs_package` 里的每个职位走完整流程，每个 JD 新建一个生成子 agent（沿用现有规则）。
7. 文件参考表加 `scripts/apply_kit.py`、`apply_kit.json`、`answers.md`、`message.txt`。

- [ ] **Step 1: 按上面 7 点修改 SKILL.md，在 README.md 的 Usage 里加四个子命令的示例**

- [ ] **Step 2: 检查**

Run: `grep -c "apply_kit.py" SKILL.md && .venv/bin/python -m unittest discover -s tests && .venv/bin/python scripts/resume_view.py tracks > /dev/null`
Expected: 计数 ≥ 5；测试全部 OK；`tracks` 正常退出。

- [ ] **Step 3: 检查点**：通读第一到八阶段和新加的两节，确认粘贴 JD 和 URL 的旧路径没有被改坏。改动保持未提交。

---

### Task 6: 部署与服务器安装（需要用户参与）

**Files:**
- Modify: `docs/updates/2026-10-19-grok-bot-integration.md`（记录本任务的实际结果）

**Interfaces:**
- Consumes：Task 1–3 的全部产物；Task 4 的 `apply_kit.json` 路径。

- [ ] **Step 1: 本地全量测试**

Run: `uv run pytest -q`（auto-app，约 9 分钟，用后台任务运行）
Expected: 全部 PASS（基线 1,912 个，加上新增的）

- [ ] **Step 2: 征得用户同意后部署**

Run: `scripts/deploy_linux.sh`（rsync 模式；会停止并重启服务、做部署前备份、执行迁移）
Expected: 脚本成功结束；`ssh home-ubuntu bash -s` 里 `systemctl --user is-active auto-app-agent auto-app-ingest auto-app-preparation auto-app-worker job-search-tunnel` 全部 active。

- [ ] **Step 3: 写入租约配置**

Run: `ssh home-ubuntu bash -s`，执行 `test -e ~/Desktop/job-search/runtime/config/handoff.json || printf '{"lease_minutes": 120}\n' > ~/Desktop/job-search/runtime/config/handoff.json`
Expected: 文件内容为 `{"lease_minutes": 120}`（文件已存在时先给用户看，不覆盖）。

- [ ] **Step 4: 以 postedism 身份验证包装脚本**

Run: `ssh home-ubuntu /home/postedism/Desktop/job-search/auto-application/deploy/jobq new --limit 1` 和 `… queue`；再运行 `ssh home-ubuntu systemd-analyze verify ~/Desktop/job-search/auto-application/deploy/grokbot/grokbot-egress.service`
Expected: 两次 jobq 都输出 JSON、退出码 0；`systemd-analyze verify` 没有报错。

- [ ] **Step 5: 用户在服务器上运行 `sudo bash ~/Desktop/job-search/auto-application/deploy/grokbot/setup.sh base`**
Expected: 脚本打印下一步说明，退出码 0。

- [ ] **Step 6: 用户下载并安装 Grok Bot 的 Linux .deb，再运行 `sudo bash …/setup.sh app`**
Expected: 打印一次 VNC 密码；`sudo systemctl --user --machine=grokbot@ is-active grokbot-display grokbot-app grokbot-vnc` 全部 active。

- [ ] **Step 7: 用户通过 VNC 完成 Grok Bot 设置**：登录 Cursor 账号；这台电脑的本机执行设为"始终允许"；Mac 上的 Grok Bot 设为"每次询问"；在 Auto-review 加 Ask first 规则"提交申请表之前问我"。

- [ ] **Step 8: 用户运行 `sudo bash …/deploy/grokbot/check.sh`，把输出给 Claude 看**
Expected: 没有 FAIL；Claude 把 REPORT 项转述给用户。如果有 FAIL，按 superpowers:systematic-debugging 排查。

- [ ] **Step 9: 重启后保持登录**：用户执行 `sudo systemctl restart user@<grokbot 的 UID>.service` → 通过 VNC 确认 Grok Bot 仍然登录。
Expected: 仍然登录。如果登录状态丢失，按设计 §11 停下来问用户是否改用真实图形会话。

- [ ] **Step 10: 在 Grok 对话里让它在 home-ubuntu 上运行 `jobq new --limit 3`**
Expected: Grok 返回 JSON 里的职位。

- [ ] **Step 11: 把以上结果（通过项、REPORT 内容、遇到的问题）写进 `docs/updates/2026-10-19-grok-bot-integration.md`，并更新索引表的状态列**

---

### Task 7: 端到端验收（用户在场）

**Files:**
- Modify: `docs/updates/2026-10-19-grok-bot-integration.md`（端到端结果）

- [ ] **Step 1: Grok 列候选**：用户在 Grok 对话里让它运行 `jobq new`，挑出候选给用户看，并对展示过的职位执行 `jobq seen`。
Expected: `jobq new` 不再返回这些职位。

- [ ] **Step 2: 用户确认一个职位后，Grok 运行 `jobq want <编号> --reason "…"`**
Expected: Claude 用 `ssh home-ubuntu …/deploy/jobq queue` 能看到这个职位。

- [ ] **Step 3: Claude Code 生成材料**：在 jd-resume-tailor 里运行 `apply_kit.py todo` → `fetch` → 按 SKILL.md 生成 → 用户确认 → `publish`。
Expected: `apply-kit/jobs/<编号>/` 下有 `job.json`、`resume.pdf`、`cover_letter.pdf`、`answers.md`（有 JD 时还有 `jd.txt`）；`index.json` 里有这个职位。

- [ ] **Step 4: Grok 投递到 Submit 前**：`jobq claim` → `jobq packet` → 在 Mac 上 Read `answers.md` 和材料包的 `job.json` → CopyToBox 两个 PDF → 在云电脑上核对 sha256 → 填表 → 停在 Submit 前问用户。
Expected: Mac 上每个操作都弹出批准；哈希一致；Grok 停在 Submit 前。

- [ ] **Step 5: 用户决定后，Grok 运行 `jobq record <token> <结果> --note "…"`**
Expected: `jobq show <编号>` 显示对应状态；如果是 submitted，时间线里有 applied 记录。

- [ ] **Step 6: 如果已提交，Claude Code 写 message**：`apply_kit.py todo` 列出这个职位 → 按 SKILL.md 写 message → `add-message`。
Expected: 材料包里有 `message.txt`；index 里 `has_message` 为 true。

- [ ] **Step 7: 把端到端结果写进 updates 记录，更新索引状态；向用户汇报，并询问是否提交两个仓库的改动**
