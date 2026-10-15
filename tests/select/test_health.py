"""After a filter batch: usage limits, unjudgeable jobs, broken filters (docs/step4-run.md §2.3)."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from auto_app.select.engine import SelectReport, unjudgeable
from auto_app.select.health import assess, clear, kind_of, reset_time, unjudgeable_rows
from tests.select.test_review import env  # noqa: F401 — env is a fixture

NOW = datetime(2026, 10, 6, 13, 0)


def rep(failures, succeeded=None):
    r = SelectReport()
    r.failures, r.succeeded = failures, succeeded or {}
    return r


def test_kinds_and_reset_times():
    assert kind_of("Claude AI usage limit reached|1791241200") == "usage"
    assert kind_of("API error 429: rate_limit_error") == "usage" and kind_of("Insufficient Balance (402)") == "usage"
    assert kind_of("Invalid API key · Please run /login") == "broken" and kind_of("model: claude-x not found") == "broken"
    assert kind_of("timed out after 600 s") == "job" and kind_of("couldn't fetch the page") == "job"
    assert reset_time("5-hour limit reached ∙ resets 3pm", NOW, 900) == datetime(2026, 10, 6, 15, 0)
    assert reset_time("resets at 9:30am", NOW, 900) == datetime(2026, 10, 7, 9, 30)  # past today: tomorrow
    assert reset_time("rate limited", NOW, 900) == datetime(2026, 10, 6, 13, 15)  # none given: retry interval


@pytest.mark.parametrize("error,failed_at,assessed_at,expected", [
    ("usage limit, resets 3pm", datetime(2026, 10, 6, 14, 59), datetime(2026, 10, 6, 15, 5),
     datetime(2026, 10, 6, 15)),
    ("usage limit, resets 12am", datetime(2026, 10, 6, 23, 59), datetime(2026, 10, 7, 0, 5),
     datetime(2026, 10, 7)),
    ("rate limited", datetime(2026, 10, 6, 15), datetime(2026, 10, 6, 15, 20),
     datetime(2026, 10, 6, 15, 15)),
])
def test_usage_reset_is_anchored_to_failure_not_batch_end(error, failed_at, assessed_at, expected):
    report = rep([(1, "ml", error)])
    report.failure_times = {(1, "ml"): failed_at}
    assert assess(report, assessed_at).usage_until == expected


def test_usage_uses_each_failures_time_even_if_reset_has_passed():
    report = rep([(1, "ml", "usage limit, resets 3pm"), (2, "ml", "usage limit, resets 3pm")])
    report.failure_times = {(1, "ml"): datetime(2026, 10, 6, 14, 59),
                            (2, "ml"): datetime(2026, 10, 6, 15, 1)}
    assert assess(report, datetime(2026, 10, 6, 15, 5)).usage_until == datetime(2026, 10, 7, 15)


def test_reset_uses_reported_timezone_and_returns_the_callers_timezone():
    utc = timezone.utc
    assert reset_time("usage limit, resets 3pm (America/Los_Angeles)",
                      datetime(2026, 10, 6, 20, tzinfo=utc), 900) == datetime(2026, 10, 6, 22, tzinfo=utc)
    # At 23:30 on Oct 6 in LA, midnight is Oct 7 (already Oct 7 in UTC).
    assert reset_time("usage limit, resets 12am (America/Los_Angeles)",
                      datetime(2026, 10, 7, 6, 30, tzinfo=utc), 900) == datetime(2026, 10, 7, 7, tzinfo=utc)
    # The next 03:00 follows the DST fallback and therefore uses the new offset.
    assert reset_time("usage limit, resets 3am (America/Los_Angeles)",
                      datetime(2026, 11, 1, 7, 30, tzinfo=utc), 900) == datetime(2026, 11, 1, 11, tzinfo=utc)


def test_failure_time_with_a_different_timezone_is_normalized_for_the_serve_clock():
    report = rep([(1, "ml", "usage limit, resets 3pm")])
    report.failure_times = {(1, "ml"): datetime(2026, 10, 6, 14, 59, tzinfo=ZoneInfo("America/Los_Angeles"))}
    assert assess(report, datetime(2026, 10, 6, 22, 5, tzinfo=timezone.utc)).usage_until == \
        datetime(2026, 10, 6, 22, tzinfo=timezone.utc)


@pytest.mark.parametrize("error", ["usage limit, resets 9:99am", "usage limit, resets 13pm"])
def test_invalid_reset_clock_uses_retry_interval(error):
    assert reset_time(error, NOW, 900) == datetime(2026, 10, 6, 13, 15)


def test_usage_never_counts_against_a_job(env):  # noqa: F811
    db = env[0]
    h = assess(rep([(1, "ml", "usage limit reached, resets 3pm"), (2, "ml", "usage limit reached, resets 3pm")]),
               NOW, store=db)
    assert h.usage_until == datetime(2026, 10, 6, 15, 0) and not h.broken and not h.unjudgeable
    assert unjudgeable_rows(db) == []


def test_a_broken_filter(env):  # noqa: F811
    db = env[0]
    h = assess(rep([(1, "ml", "Invalid API key · Please run /login")]), NOW, store=db)
    assert "Invalid API key" in h.broken["ml"]  # a login / key error: broken at once
    h = assess(rep([(1, "research", "timeout"), (2, "research", "timeout"), (3, "research", "timeout")]), NOW, store=db)
    assert "3 different jobs failed, none worked" in h.broken["research"]  # failing on everything: broken
    assert unjudgeable_rows(db) == []  # … and those jobs aren't blamed


def test_a_job_that_keeps_failing_becomes_unjudgeable(env):  # noqa: F811
    db, _, jobs, _ = env
    j = jobs["ml"]
    for n in range(3):
        h = assess(rep([(j, "ml", "page unreadable")], succeeded={"ml": 4}), NOW, tries_per_job=3, store=db)
    assert h.unjudgeable == [(j, "ml", "page unreadable")] and not h.broken
    assert (j, "ml") in unjudgeable(db)  # `filter run` skips it now
    clear(db, [j])  # `filter run --job N`: a fresh start
    assert (j, "ml") not in unjudgeable(db)
