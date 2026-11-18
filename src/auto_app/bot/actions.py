"""Pure bot configuration and database actions, separate from Discord transport."""

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

ACTIONS = {"want": "want_to_apply", "skip": "not_applied", "applied": "applied"}
BUTTONS = (*ACTIONS, "bad")  # "bad": shouldn't recommend, which asks for a reason first (record_shouldnt)
LABELS = {"want": "Want to apply", "skip": "Skip", "applied": "Applied", "bad": "Shouldn't recommend"}
CUSTOM_ID = re.compile(r"job:([1-9][0-9]{0,11}):(want|skip|applied|bad)")
PROPOSAL_ID = re.compile(r"screen:([a-f0-9]{32}):(apply|discard)")
SHOULDNT = "shouldn't recommend: "  # the decision's reason starts with this, then the user's words
EXAMPLES = 5  # jobs listed per side of a proposal's preview
TOKEN = re.compile(r"[A-Za-z0-9_.-]+")
ENV = ("AUTO_APP_DISCORD_BOT_TOKEN", "AUTO_APP_DISCORD_OWNER_ID", "AUTO_APP_DISCORD_GUILD_ID",
       "AUTO_APP_DISCORD_CHANNEL_ID")
# Optional: the channel for Claude's questions to the owner (without it they go to the main channel).
OPTIONAL_ENV = ("AUTO_APP_DISCORD_QUESTIONS_CHANNEL_ID",)
ASK_ID = re.compile(r"ask:([1-9][0-9]{0,11}):(opt[0-3]|reply|skip)")


def custom_id(job_id: int, action: str) -> str:
    if type(job_id) is not int or not 0 < job_id < 10**12 or action not in BUTTONS:
        raise ValueError("Invalid job button")
    return f"job:{job_id}:{action}"


def parse_custom_id(value: str) -> tuple[int, str] | None:
    match = CUSTOM_ID.fullmatch(value) if isinstance(value, str) else None
    return (int(match[1]), match[2]) if match else None


def proposal_custom_id(change_id: str, action: str) -> str:
    value = f"screen:{change_id}:{action}"
    if not isinstance(change_id, str) or not PROPOSAL_ID.fullmatch(value):
        raise ValueError("Invalid proposal button")
    return value


def parse_proposal_id(value: str) -> tuple[str, str] | None:
    match = PROPOSAL_ID.fullmatch(value) if isinstance(value, str) else None
    return (match[1], match[2]) if match else None


@dataclass(frozen=True)
class BotSettings:
    token: str = field(repr=False)
    owner_id: int
    guild_id: int
    channel_id: int
    db_path: Path
    questions_channel_id: int | None = None  # AUTO_APP_DISCORD_QUESTIONS_CHANNEL_ID (optional)

    @classmethod
    def _from_values(cls, values: dict[str, str], db_path: Path) -> "BotSettings":
        for key in ENV:
            if not values.get(key):
                raise ValueError(f"{key} is missing from bot credentials")
        if not TOKEN.fullmatch(values[ENV[0]]):
            raise ValueError("AUTO_APP_DISCORD_BOT_TOKEN has invalid format")
        ids = []
        for key in ENV[1:]:
            value = values[key]
            if not value.isascii() or not value.isdecimal() or int(value) <= 0:
                raise ValueError(f"{key} must be a positive Discord numeric id")
            ids.append(int(value))
        questions = values.get(OPTIONAL_ENV[0])
        if questions is not None and (not questions.isascii() or not questions.isdecimal() or int(questions) <= 0):
            raise ValueError(f"{OPTIONAL_ENV[0]} must be a positive Discord numeric id")
        return cls(values[ENV[0]], *ids, Path(db_path), int(questions) if questions else None)

    @classmethod
    def from_env(cls, db_path: Path) -> "BotSettings":
        """Support tests and explicit callers; production startup requires from_file."""
        values = {key: os.environ.get(key, "").strip() for key in ENV}
        values.update({key: os.environ[key].strip() for key in OPTIONAL_ENV if os.environ.get(key, "").strip()})
        return cls._from_values(values, db_path)

    @classmethod
    def from_file(cls, path: Path, db_path: Path) -> "BotSettings":
        path = Path(path).expanduser()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise ValueError("bot credential file is missing or unreadable") from None
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise ValueError("bot credential file must be a regular file with mode 0600")
            raw = os.read(fd, 8193)
        except OSError:
            raise ValueError("bot credential file is unreadable") from None
        finally:
            os.close(fd)
        if len(raw) > 8192:
            raise ValueError("bot credential file is too large")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("bot credential file is not valid UTF-8") from None
        values = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or key not in (*ENV, *OPTIONAL_ENV) or key in values or not value:
                raise ValueError("bot credential file contains an invalid entry")
            values[key] = value
        return cls._from_values(values, db_path)


def record_action(db_path: Path, user_id: int, owner_id: int, job_id: int, action: str) -> str:
    """Write one owner's manual decision through the shared AgentService."""
    if user_id != owner_id:
        return "Only the owner of this job search can record decisions."
    if action not in ACTIONS:
        return "Unknown action."
    from auto_app.agent.service import AgentError, AgentService

    try:
        result = AgentService(db_path).record_application_status(job_id, ACTIONS[action], reason="via Discord")
    except (AgentError, ValueError):
        return "Not recorded: this job cannot be changed. Check its current status."
    return f"Job #{job_id}: {ACTIONS[action]}" + ("" if result["changed"] else " (already)")


def record_shouldnt(db_path: Path, user_id: int, owner_id: int, job_id: int, reason: str) -> str:
    """The owner's "shouldn't recommend": the job becomes not_applied, and the reason waits for the daily summary
    that turns reasons into a screening-rule proposal (auto_app.screen.feedback)."""
    if user_id != owner_id:
        return "Only the owner of this job search can record decisions."
    from auto_app.agent.service import AgentError, AgentService
    from auto_app.screen.feedback import MAX_REASON, FeedbackError, ScreeningFeedback

    text = reason.strip() if isinstance(reason, str) else ""
    if not text or len(text) > MAX_REASON:
        return f"Not recorded: the reason must be 1 to {MAX_REASON} characters."
    try:
        AgentService(db_path).record_application_status(job_id, "not_applied", reason=SHOULDNT + text)
    except (AgentError, ValueError):
        return "Not recorded: this job cannot be changed. Check its current status."
    try:
        ScreeningFeedback(db_path).record(job_id, text)
    except (FeedbackError, ValueError):
        return f"Job #{job_id}: not_applied, but the reason was not saved; send it again or tell Claude."
    return f"Job #{job_id}: not_applied. The reason waits for the next screening-rule summary."


def proposal_message(db_path: Path, change_id: str) -> str | None:
    """The text of a screening proposal for Discord: why, what changes, what the preview found, which reasons it
    answers. None when the proposal is gone or expired."""
    from auto_app.agent.control import ControlService
    from auto_app.screen.feedback import ScreeningFeedback

    proposal = ControlService(db_path).screening_proposal(change_id)
    if proposal is None:
        return None
    lines = ["**Screening rule proposal** (Apply changes the rules; it expires 24 hours after it was made)"]
    if proposal.get("note"):
        lines.append(proposal["note"])
    for key, (before, after) in (proposal.get("diff") or {}).items():
        if isinstance(before, list) and isinstance(after, list):
            added = [t for t in after if t not in before]
            removed = [t for t in before if t not in after]
            change = ", ".join([*(f"+{t}" for t in added), *(f"-{t}" for t in removed)])
        else:
            change = f"{before} → {after}"
        lines.append(f"• `{key}`: {change}")
    preview = proposal.get("preview") or {}
    lines.append(f"Kept jobs: {preview.get('kept_before')} → {preview.get('kept_after')} "
                 f"(newly excluded {preview.get('newly_excluded')}, newly kept {preview.get('newly_kept')})")
    examples = preview.get("examples") or {}
    for side, label in (("newly_excluded", "Newly excluded"), ("newly_kept", "Newly kept")):
        rows = (examples.get(side) or [])[:EXAMPLES]
        if rows:
            lines.append(f"{label}: " + "; ".join(f"#{r.get('job_id')} {r.get('title')}" for r in rows))
    reasons = ScreeningFeedback(db_path).attached(change_id)
    if reasons:
        lines.append("Your reasons:")
        lines += [f"• #{r['job_id']} {r.get('title') or ''}: {r['reason']}" for r in reasons[:10]]
    return "\n".join(lines)


@dataclass(frozen=True)
class ProposalActionResult:
    message: str
    remove_buttons: bool = False


def proposal_action(db_path: Path, user_id: int, owner_id: int, change_id: str, action: str) -> ProposalActionResult:
    """Resolve the reasons after an owner decision, and indicate when the controls are finished."""
    if user_id != owner_id:
        return ProposalActionResult("Only the owner of this job search can change the screening rules.")
    if action not in ("apply", "discard"):
        return ProposalActionResult("Unknown action.")
    from auto_app.agent.control import ControlError, ScreeningProposalUnavailable
    from auto_app.screen.feedback import ScreeningFeedback

    feedback = ScreeningFeedback(db_path)
    control = feedback.control
    try:
        if action == "apply":
            control.apply_screening_proposal(change_id)
            resolution = "applied"
            message = "Applied. The worker screens with the new rules from its next round."
        else:
            control.discard_screening_change(change_id)
            resolution = "discarded"
            message = "Discarded. The rules stay as they are."
    except ScreeningProposalUnavailable:
        feedback.release_proposal(change_id)
        return ProposalActionResult(
            "This proposal expired or is gone. Unresolved reasons go back to the next summary.", remove_buttons=True
        )
    except ControlError as exc:
        operation = "applied" if action == "apply" else "discarded"
        return ProposalActionResult(f"Not {operation}: {exc}")

    feedback.resolve(change_id, resolution)
    return ProposalActionResult(message, remove_buttons=True)


def closure_message(items: list[dict]) -> str:
    """Reasons the daily summary found no rule change for, with its note."""
    lines = ["**Shouldn't-recommend reasons with no rule change**"]
    lines += [f"• #{i['job_id']} {i.get('title') or ''}: {i['reason']} — {i.get('resolution_note') or ''}"
              for i in items]
    return "\n".join(lines)


def jobs_summary(db_path: Path, *, sponsorship: str | None = None, query: str | None = None,
                 company: str | None = None, limit: int = 10) -> list[dict]:
    """Find open, visible jobs with the same country and screening rules as chat."""
    from auto_app.query.service import QueryService

    wanted, cursor = [], None
    limit = max(1, min(limit, 10))
    with QueryService(db_path) as service:
        while len(wanted) < limit:
            page = service.jobs(query=query, company=company, sponsorship=sponsorship, limit=100, cursor=cursor)
            for job in page["items"]:
                if job["state"] in ("not_evaluated", "recommended"):
                    wanted.append(_brief(job))
                    if len(wanted) == limit:
                        break
            cursor = page["next_cursor"]
            if not cursor:
                break
    return wanted


def job_summary(db_path: Path, job_id: int) -> dict | None:
    from auto_app.query.service import QueryService

    with QueryService(db_path) as service:
        job = service.job(job_id)
    return _brief(job) if job else None


def _brief(job: dict) -> dict:
    return {"job_id": job["job_id"], "title": job.get("title"), "company": job.get("company"),
            "state": job.get("state"), "url": job.get("canonical_url"),
            "sponsorship": ((job.get("screening") or {}).get("signals") or {}).get("sponsorship", "unknown")}


# ---- Claude's questions for the owner (auto_app.handoff.asks) ---------------------------------------------------

def ask_custom_id(question_id: int, action: str) -> str:
    value = f"ask:{question_id}:{action}"
    if type(question_id) is not int or not ASK_ID.fullmatch(value):
        raise ValueError("Invalid question button")
    return value


def parse_ask_id(value: str) -> tuple[int, str] | None:
    match = ASK_ID.fullmatch(value) if isinstance(value, str) else None
    return (int(match[1]), match[2]) if match else None


def ask_embed(item: dict) -> dict:
    """One question as a Discord embed: the job, the question, why it is asked and Claude's recommendation."""
    if item.get("job_id") is not None:
        title = f"#{item['job_id']} {item.get('company') or '?'} — {item.get('title') or 'Untitled'}"
    else:
        title = "需要你决定"
    parts = [f"**{item['question']}**"]
    if item.get("context"):
        parts.append(item["context"])
    options = item.get("options") or []
    if item.get("recommended") is not None and item["recommended"] < len(options):
        parts.append(f"Claude 推荐：{options[item['recommended']]}")
    parts.append("点下面的按钮回答，或点「其他回复」用你自己的话回答" + ("；「不投」直接记为不投。" if item.get("job_id") else "。"))
    embed = {"title": title[:256], "description": "\n\n".join(parts)[:4000],
             "footer": {"text": f"问题 #{item['question_id']} · {item.get('asked_by') or 'claude'}"}}
    if item.get("link"):
        embed["url"] = item["link"]
    return embed


@dataclass(frozen=True)
class AskActionResult:
    message: str
    remove_buttons: bool = False


def ask_action(db_path: Path, user_id: int, owner_id: int, question_id: int, action: str,
               text: str | None = None) -> AskActionResult:
    """The owner's answer to a question: an option (optN), their own words (reply) or not applying (skip)."""
    if user_id != owner_id:
        return AskActionResult("Only the owner of this job search can answer.")
    from auto_app.handoff.asks import AskError, OwnerQuestions

    questions = OwnerQuestions(db_path)
    try:
        item = questions.get(question_id)
        if action == "skip":
            questions.skip(question_id)
            return AskActionResult(f"#{item['job_id']} 已记为不投。", True)
        if action == "reply":
            answer = text or ""
        elif action.startswith("opt") and int(action[3:]) < len(item["options"]):
            answer = item["options"][int(action[3:])]
        else:
            return AskActionResult("这个按钮已经不对应任何选项。")
        questions.answer(question_id, answer)
    except (AskError, ValueError) as exc:
        return AskActionResult(f"没有记下：{exc}")
    return AskActionResult(f"已记下你的回复：{answer}。Claude 马上接着处理，做完会在这条消息下回复。", True)
