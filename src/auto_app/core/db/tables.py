"""Database schema (docs/step1-collect.md §8.1). Changes go through Alembic migrations (migrations/)."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})

Ts = DateTime(timezone=True)

batches = Table(
    "batches", metadata,
    Column("batch_id", String, primary_key=True),
    Column("started_at", Ts, nullable=False),
    Column("finished_at", Ts),
    Column("interrupted", Boolean, nullable=False, default=False),
    Column("config_hash", String),
    Column("data", JSON, nullable=False),  # options + per-source summaries, as shown by `stats`
)

search_runs = Table(
    "search_runs", metadata,
    Column("search_run_id", Integer, primary_key=True, autoincrement=True),
    Column("batch_id", String, ForeignKey("batches.batch_id"), nullable=False, index=True),
    Column("search_name", String),
    Column("source", String, nullable=False),
    Column("query", JSON),
    Column("search_url", Text),
    Column("started_at", Ts),
    Column("finished_at", Ts),
    Column("status", String),
    Column("stop_reason", String),
    Column("pages_loaded", Integer),
    Column("outcomes", JSON),
)

search_results = Table(
    "search_results", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("search_run_id", Integer, ForeignKey("search_runs.search_run_id"), nullable=False, index=True),
    Column("batch_id", String, nullable=False),
    Column("search_name", String),
    Column("source", String, nullable=False),
    Column("source_job_id", String, nullable=False),
    Column("page", Integer),
    Column("rank", Integer),
    Column("is_promoted", Boolean),
    Column("title", Text),
    Column("url", Text),
    Column("card", JSON),  # the card as recorded (lines, attrs …)
    Column("card_html", LargeBinary),  # zstd
    Column("outcome", String, nullable=False),
    Column("outcome_detail", Text),
    Column("seen_at", Ts, nullable=False),
    Index("ix_search_results_source_job", "source", "source_job_id"),
    Index("ix_search_results_source_outcome_seen", "source", "outcome", "seen_at"),
)

source_health = Table(
    "source_health", metadata,
    Column("source", String, primary_key=True),
    Column("status", String, nullable=False, default="ok"),
    Column("data", JSON, nullable=False),  # cooldown_until, last_block_at, last_block_reason, last_ok_at, last_run_at
)

postings = Table(
    "postings", metadata,
    Column("posting_id", Integer, primary_key=True, autoincrement=True),
    Column("source", String, nullable=False),
    Column("source_job_id", String, nullable=False),
    Column("source_url", Text),
    Column("kind", String, nullable=False),  # browser | api
    Column("platform", String),
    Column("board", Text),
    Column("batch_id", String),
    Column("first_search_run_id", Integer),
    # lifecycle: fetched → parsed | parse_failed → normalized → (C8c) l1_done → linked
    Column("state", String, nullable=False, default="fetched"),
    Column("parser", String),
    Column("parsed_at", String),
    Column("parse_error", Text),
    Column("first_seen_at", Ts, nullable=False),
    Column("last_seen_at", Ts, nullable=False),
    # parse output: the full CommonPosting + a few columns for queries
    Column("parsed", JSON),
    Column("title", Text),
    Column("company", Text),
    Column("posted_at", Ts),
    Column("apply_url", Text),
    Column("apply_kind", String),
    # normalize output: the full record + indexed keys (kept forever, for Level 1a/2 and tombstones)
    Column("normalized", JSON),
    Column("company_norm", String, index=True),
    Column("title_norm", Text),
    Column("employer_key", String, index=True),
    Column("req_id", String),
    Column("desc_hash", String, index=True),
    # dedup (C8c)
    Column("duplicate_of", Integer, ForeignKey("postings.posting_id")),
    Column("job_id", Integer, ForeignKey("jobs.job_id"), index=True),
    Column("match_rule", String),
    Column("match_confidence", Float),
    Column("purged", Boolean, nullable=False, default=False),
    UniqueConstraint("source", "source_job_id"),  # Level 1a
    Index("ix_postings_source_state", "source", "state"),
)

posting_raw = Table(  # heavy, purged ~7 days after a successful parse (C9)
    "posting_raw", metadata,
    Column("posting_id", Integer, ForeignKey("postings.posting_id", ondelete="CASCADE"), primary_key=True),
    Column("fetched_at", Ts),
    Column("capture", JSON),  # final URL, status, ready, apply link/kind, meta
    Column("card", JSON),
    Column("page_text", LargeBinary),  # zstd
    Column("detail_html", LargeBinary),  # zstd
    Column("payloads", LargeBinary),  # zstd JSON
    Column("embedded", LargeBinary),  # zstd JSON
    Column("record", LargeBinary),  # zstd JSON (API sources)
    Column("detail", LargeBinary),  # zstd JSON (API sources, e.g. Workday)
    Column("screenshot_path", Text),
)

# ---- C8c (created now, filled by dedup later) -------------------------------------------------

jobs = Table(
    "jobs", metadata,
    Column("job_id", Integer, primary_key=True, autoincrement=True),
    Column("primary_posting_id", Integer, ForeignKey("postings.posting_id", use_alter=True)),
    Column("company_norm", String, index=True),
    Column("title_norm", Text),
    Column("canonical_url", Text),
    # state (docs/README.md §3): Step 2's verdict and your decision; NULL = not yet
    Column("step2", String),  # recommended | un_recommended
    Column("step2_at", Ts),
    Column("decision", String),  # want_to_apply | applied | not_applied
    Column("decided_at", Ts),
    Column("first_seen_at", Ts),
    Column("last_seen_at", Ts),  # latest last-seen of any posting (refreshed by dedup and purge)
    Column("expires_at", Ts),  # last_seen_at + the TTL of the job's state (jobs.ttl_days)
    # archived (step1 §9.2): a record kept as training data — never merged with active jobs
    Column("archived_at", Ts, index=True),
    Column("archive", JSON),
    Column("archive_description", LargeBinary),  # zstd; applied jobs only (judged jobs also keep their primary posting's parsed fields)
    Column("duplicate_of", Integer),  # archived only: the earlier archived job this one duplicates (step2 §7.8)
    Column("warnings", JSON),  # e.g. [{"kind": "repost", "job_id": 12, "text": "looks like job #12, …"}]
    Column("notified_at", Ts),  # Step 4: when you were told about it (the manager's digest — `job notified`)
    sqlite_autoincrement=True,  # retained timelines must never attach to a recycled job ID
)

job_locations = Table(
    "job_locations", metadata,
    Column("job_id", Integer, ForeignKey("jobs.job_id"), primary_key=True),
    Column("location_norm", String, primary_key=True),
    Column("workplace_type", String),
    Column("apply_url", Text),
    Column("posting_id", Integer, ForeignKey("postings.posting_id")),
)

job_keys = Table(  # kept while the job exists, archived jobs included (repost warnings)
    "job_keys", metadata,
    Column("key_type", String, primary_key=True),
    Column("key_value", String, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.job_id"), primary_key=True),
)

ingest_entries = Table(
    "ingest_entries", metadata,
    Column("source", String, primary_key=True),
    Column("external_id", String, primary_key=True),
    Column("posting_id", Integer, ForeignKey("postings.posting_id", ondelete="SET NULL"), index=True),
    Column("kind", String, nullable=False),
    Column("origin", Text, nullable=False),
    Column("revision", String),
    Column("content_hash", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String, nullable=False),  # active | unknown | inactive | missing (source evidence only)
    Column("first_seen_at", Ts, nullable=False),
    Column("last_seen_at", Ts, nullable=False),
    Column("last_imported_at", Ts, nullable=False),
    Column("needs_reassessment", Boolean, nullable=False, default=False),
)

ingest_changes = Table(
    "ingest_changes", metadata,
    Column("change_id", Integer, primary_key=True, autoincrement=True),
    Column("source", String, nullable=False),
    Column("external_id", String, nullable=False),
    Column("posting_id", Integer, ForeignKey("postings.posting_id", ondelete="SET NULL")),
    Column("event", String, nullable=False),
    Column("revision", String),
    Column("content_hash", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("observed_at", Ts, nullable=False),
    Column("created_at", Ts, nullable=False),
    Index("ix_ingest_changes_source_external_id", "source", "external_id"),
)

company_aliases = Table(
    "company_aliases", metadata,
    Column("alias_norm", String, primary_key=True),
    Column("company_norm", String, nullable=False),
    Column("origin", String),  # learned | config
)

# ---- Step 2 (docs/step2-select.md §9) ----------------------------------------------------------

filter_versions = Table(  # what each filter version ran: the exact command, files and settings
    "filter_versions", metadata,
    Column("filter_version", String, primary_key=True),
    Column("filter", String, nullable=False, index=True),
    Column("command", Text, nullable=False),
    Column("files", JSON),  # {relative path: text}
    Column("settings", JSON),  # filter.yaml's versioned settings
    Column("created_at", Ts, nullable=False),
)

judgments = Table(  # one agent run of one filter version on one job
    "judgments", metadata,
    Column("job_id", Integer, ForeignKey("jobs.job_id"), primary_key=True),
    Column("filter_version", String, ForeignKey("filter_versions.filter_version"), primary_key=True),
    Column("filter", String, nullable=False, index=True),
    Column("model", String),
    Column("status", String, nullable=False),  # ok | unparsed | error
    Column("answer", Text),  # the raw final answer
    Column("recommend", Boolean),  # parsed output: fit and no dealbreaker; NULL = couldn't read (or error)
    Column("fit", Boolean),  # the job meets your requirements (older answers: = recommend)
    Column("dealbreaker", Boolean),  # a deal-breaker applies (NULL: not given — treated as no)
    Column("reason", Text),
    Column("parser", String),  # "convention" or "parser.py:<hash>"
    Column("parse_error", Text),
    Column("error", Text),  # the run's error, when status = error
    Column("trace", LargeBinary),  # zstd JSON: every model turn and tool call
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("tool_calls", Integer),
    Column("cost_usd", Float),  # NULL when the model has no price configured
    Column("created_at", Ts, nullable=False),
    Column("parsed_at", Ts),
)

reviews = Table(  # your decisions in review = training labels (S3)
    "reviews", metadata,
    Column("review_id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", Integer),  # no FK: labels outlive the job (TTL)
    Column("decision", String, nullable=False),  # want_to_apply | not_applied (review), applied (Step 3)
    Column("reason", Text),
    Column("filters", JSON),  # the filters that judged it, with their verdicts
    Column("job_text", Text),  # title + company + description, kept for training
    Column("created_at", Ts, nullable=False),
)

feedback = Table(  # your label of one job for one filter (good / bad) = training data for its preference model
    "feedback", metadata,
    Column("job_id", Integer, primary_key=True),  # no FK: labels outlive the job (TTL)
    Column("filter", String, primary_key=True),
    Column("filter_version", String),  # the version whose verdict you rated
    Column("verdict", Boolean),  # what the filter said: recommend (True) / no (False) / couldn't read (NULL)
    Column("correct", Boolean),  # S3: the verdict was right — now derived from label (NULL: no verdict)
    Column("label", Boolean),  # S4: your label — good (True) / bad (False) job for this filter (step2 §7.1)
    Column("origin", String, nullable=False, default="explicit"),  # explicit (g/b) | implied (by your decision)
    Column("reason", Text),
    Column("job_text", Text),  # title + company + description, kept for training
    Column("created_at", Ts, nullable=False),
)

audit_draws = Table(  # one random sample of a filter's "no" jobs (step2 §7.4)
    "audit_draws", metadata,
    Column("draw_id", Integer, primary_key=True, autoincrement=True),
    Column("filter", String, nullable=False, index=True),
    Column("filter_version", String),  # the filter's current version when drawn (for reference)
    Column("pool_size", Integer, nullable=False),  # how many unlabeled "no" jobs it was drawn from
    Column("size", Integer, nullable=False),
    Column("drawn_at", Ts, nullable=False),
)

audit_items = Table(  # the jobs in a draw; labeled once a feedback row exists for (job, filter)
    "audit_items", metadata,
    Column("draw_id", Integer, ForeignKey("audit_draws.draw_id"), primary_key=True),
    Column("job_id", Integer, primary_key=True),  # no FK: kept with the labels
    Column("filter", String, nullable=False, index=True),
)

applications = Table(  # Step 3: one application per queued job (docs/step3-apply.md §8)
    "applications", metadata,
    Column("application_id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", Integer, nullable=False, index=True),  # no FK: the record outlives the job (TTL)
    Column("platform", String, nullable=False),  # greenhouse | lever | ashby | workday | linkedin_easy_apply | other
    Column("apply_url", Text),
    Column("status", String, nullable=False, index=True),  # queued | handoff | needs_input | action_needed | ready |
                                                           # submitting | submitted | failed | dropped
    Column("chain", JSON, nullable=False),  # the applier chain from the policy, e.g. ["greenhouse", "simplify", "manual"]
    Column("chain_pos", Integer, nullable=False, default=0),  # the applier now in charge
    Column("applier", String),  # its name (or the one that submitted)
    Column("note", Text),
    Column("created_at", Ts, nullable=False),
    Column("updated_at", Ts, nullable=False),
    Column("submitted_at", Ts),
    Column("confirmation_path", Text),
)

attempts = Table(  # every applier's try on an application, with its outcome (§3)
    "attempts", metadata,
    Column("attempt_id", Integer, primary_key=True, autoincrement=True),
    Column("application_id", Integer, ForeignKey("applications.application_id"), nullable=False, index=True),
    Column("applier", String, nullable=False),
    Column("pass", String, nullable=False),  # prepare | submit | handoff
    Column("outcome", String, nullable=False),  # ready | needs_input | action_needed | unsupported | failed | submitted | handoff
    Column("reason", Text),
    Column("screenshot_path", Text),
    Column("started_at", Ts, nullable=False),
    Column("ended_at", Ts),
)

judge_tries = Table(  # Step 4: a filter's failed tries on a job; after N, the job is unjudgeable for it (§2.3)
    "judge_tries", metadata,
    Column("job_id", Integer, primary_key=True),
    Column("filter", String, primary_key=True),
    Column("tries", Integer, nullable=False, default=0),
    Column("last_error", Text),
    Column("unjudgeable_at", Ts),  # set: skipped by `filter run` until you retry it (`filter run --job N`)
    Column("updated_at", Ts, nullable=False),
)

mail_messages = Table(
    "mail_messages", metadata,
    Column("source", String, primary_key=True),
    Column("provider", String, primary_key=True),
    Column("account_key", String, primary_key=True),
    Column("message_id", String, primary_key=True),
    Column("received_at", Ts),  # unavailable if a listed message was deleted before retrieval
    Column("imported_at", Ts, nullable=False),
    Column("job_links", Integer, nullable=False),
)

accounts = Table(  # Step 3: the site accounts the agent created or signed in to (docs/step3-agent.md §6)
    "accounts", metadata,
    Column("site", String, primary_key=True),  # the registrable site, e.g. myworkdayjobs.com's tenant host
    Column("email", Text, nullable=False),
    Column("created_by_agent", Boolean, nullable=False, default=False),  # created by the agent (else: signed in to)
    Column("created_at", Ts, nullable=False),
    Column("last_used_at", Ts, nullable=False),
    Column("application_id", Integer),  # the application it was first used for
    Column("note", Text),  # e.g. "email verification pending"
)

questions = Table(  # Step 3: each application's questions, with what was filled in and why (§4, §8)
    "questions", metadata,
    Column("application_id", Integer, ForeignKey("applications.application_id"), primary_key=True),
    Column("question_id", String, primary_key=True),  # the form's field name
    Column("ord", Integer, nullable=False),  # the order on the form
    Column("label", Text),
    Column("type", String),  # text | textarea | file | select | multiselect
    Column("kind", String),  # standard | known | sensitive | free_text
    Column("required", Boolean),
    Column("options", JSON),
    Column("answer", JSON),
    Column("source", String),  # profile | bank:<key> | claude | draft | you | skipped
    Column("confidence", Float),
    Column("seen", Boolean),  # drafts: shown to you in review?
    Column("note", Text),
)


# ---- Linux worker: JD enrichment and deterministic screening (docs/linux-worker.md) --------------

enrichments = Table(  # one read of an imported job's official record, by the posting's canonical apply URL
    "enrichments", metadata,
    Column("url", Text, primary_key=True),
    Column("platform", String),  # greenhouse | lever | ashby | workday | microsoft | tiktok | jobright |
    # smartrecruiters | workable | amazon; NULL when no adapter knew the URL (read again once one does)
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

# ---- Agent handoff ---------------------------------------------------------------------------

handoff_claims = Table(
    'handoff_claims', metadata,
    Column('job_id', Integer, primary_key=True),
    Column('agent', String, nullable=False),
    Column('token_hash', String, nullable=False, unique=True),
    Column('claimed_at', Ts, nullable=False),
    Column('lease_until', Ts, nullable=False),
)

screening_feedback = Table(  # the user's "shouldn't recommend" from Discord: reasons behind screening-rule proposals
    "screening_feedback", metadata,
    Column("feedback_id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", Integer, nullable=False, index=True),  # no FK: the reason outlives the job (TTL)
    Column("reason", Text, nullable=False),
    Column("title", Text),
    Column("company", Text),
    Column("signals", JSON),  # the screening verdict (outcome, reasons, signals) when the user pressed the button
    Column("created_at", Ts, nullable=False),
    Column("proposal_id", String),  # the open screening proposal made from it (ControlService change id)
    Column("resolved_at", Ts),
    Column("resolution", String),  # applied | discarded | no_change
    Column("resolution_note", Text),  # why no rule change (no_change)
    Column("posted_at", Ts),  # when a no_change note went to Discord
)

handoff_questions = Table(  # open questions an agent found on a form and handed back for answers
    'handoff_questions', metadata,
    Column('question_set_id', Integer, primary_key=True, autoincrement=True),
    Column('job_id', Integer, nullable=False, index=True),  # no FK: like applications, it outlives the job
    Column('application_id', Integer, nullable=False),
    Column('agent', String, nullable=False),
    Column('questions', JSON, nullable=False),  # [{"text", "required", "limit", "page"}]
    Column('submitted_at', Ts, nullable=False),
    Column('answered_at', Ts),  # set when the owner marks the answers published (answers-ready)
)

cheatsheet_requests = Table(  # the user's general answers, passed on by an agent, for Claude to add to the cheat sheet
    "cheatsheet_requests", metadata,
    Column("request_id", Integer, primary_key=True, autoincrement=True),
    Column("text", Text, nullable=False),  # the user's words, verbatim
    Column("agent", String, nullable=False),
    Column("submitted_at", Ts, nullable=False),
    Column("done_at", Ts),  # set by the owner's side (Claude) once it edited the cheat sheet or declined
    Column("outcome", String),  # applied | declined
    Column("note", Text),  # what changed, or why not
)

owner_questions = Table(  # what Claude needs the owner to decide, posted to Discord and answered there
    "owner_questions", metadata,
    Column("question_id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", Integer, index=True),  # no FK; null for a question about no single job
    Column("asked_by", String, nullable=False),
    Column("question", Text, nullable=False),
    Column("context", Text),  # why it is asked: the JD's words, the conflict, the recommendation
    Column("options", JSON, nullable=False),  # up to 4 short answers offered as buttons
    Column("recommended", Integer),  # index into options, or null
    Column("created_at", Ts, nullable=False),
    Column("posted_at", Ts),
    Column("message_id", String),  # the Discord message, for the reply once handled
    Column("answer", Text),
    Column("answered_at", Ts),
    Column("outcome", String),  # answered | skipped (the owner chose not to apply)
    Column("handled_at", Ts),  # Claude acted on the answer
    Column("handled_note", Text),
    Column("reported_at", Ts),  # the bot posted handled_note under the question
)

board_proposals = Table(  # unconnected employer boards offered to the owner on Discord, weekly
    "board_proposals", metadata,
    Column("proposal_id", Integer, primary_key=True, autoincrement=True),
    Column("platform", String, nullable=False),  # greenhouse | ashby | lever
    Column("board", String, nullable=False),
    Column("company", Text),
    Column("location", Text, nullable=False),  # the board's list API, as a feed's location
    Column("kept_jobs", Integer, nullable=False),
    Column("applied", Integer, nullable=False),
    Column("wanted", Integer, nullable=False),
    Column("skipped", Integer, nullable=False),
    Column("examples", JSON, nullable=False),  # a few kept job titles on the board
    Column("created_at", Ts, nullable=False),
    Column("posted_at", Ts),
    Column("message_id", String),
    Column("decision", String),  # added | skipped
    Column("decided_at", Ts),
    Column("feed", String),  # the feed name added to feeds.yaml
)

resume_artifacts = Table(
    'resume_artifacts', metadata,
    Column('artifact_id', Integer, primary_key=True, autoincrement=True),
    Column('job_id', Integer, nullable=False, index=True),
    Column('path', Text, nullable=False),
    Column('sha256', String, nullable=False),
    Column('bytes', Integer, nullable=False),
    Column('note', Text),
    Column('created_at', Ts, nullable=False),
)

# job_id: no FK — the timeline outlives the job (TTL).
mail_updates = Table(
    "mail_updates", metadata,
    Column("update_id", Integer, primary_key=True, autoincrement=True),
    Column("source", String, nullable=False),
    Column("message_key", String, nullable=False, unique=True),
    Column("received_at", Ts),
    Column("sender", Text),
    Column("subject", Text),
    Column("kind", String, nullable=False),
    Column("job_id", Integer),
    Column("status", String, nullable=False),
    Column("created_at", Ts, nullable=False),
)

application_events = Table(
    "application_events", metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", Integer, nullable=False, index=True),
    Column("kind", String, nullable=False),
    Column("occurred_at", Ts, nullable=False),
    Column("source", String, nullable=False),
    Column("note", Text),
    Column("mail_update_id", Integer, unique=True),
    Column("created_at", Ts, nullable=False),
)
