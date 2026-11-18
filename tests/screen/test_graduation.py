"""Screening: a job that starts (or wants graduates) before the user's graduation is excluded, on explicit dates only."""

import pytest

from auto_app.screen.rules import JobFacts, ScreeningRules, rules_from_dict, screen

GRAD = ScreeningRules(graduation="2027-05")
DESC_TITLE = "Software Engineer"


def verdict(title=DESC_TITLE, description="", rules=GRAD):
    return screen(JobFacts(job_id=1, titles=(title,), description=description), rules)


@pytest.mark.parametrize("title,description,evidence", [
    ("Software Engineer, New Grad (2026)", "", "2026-12"),
    ("2026 New Grad Software Engineer", "", "2026-12"),
    ("Software Engineer - Class of 2026", "", "2026-12"),
    ("University Graduate Software Engineer, 2026 Start", "", "2026-12"),
    (DESC_TITLE, "Start date: January 2027.", "2027-01"),
    (DESC_TITLE, "This role is starting summer 2026 in our Seattle office.", "2026-08"),
    (DESC_TITLE, "You are graduating between December 2025 and June 2026.", "2026-06"),
    (DESC_TITLE, "Candidates must graduate by August 2026.", "2026-08"),
    (DESC_TITLE, "The program has a Winter 2026 start.", "2027-02"),
    (DESC_TITLE, "Open to the Class of 2026.", "2026-12"),
    (DESC_TITLE, "Expected graduation date of December 2025 through June 2026.", "2026-06"),
    (DESC_TITLE, "You must graduate no later than June 2026.", "2026-06"),
    (DESC_TITLE, "The role starts in January 2027 in New York.", "2027-01"),
])
def test_explicit_earlier_dates_exclude(title, description, evidence):
    result = verdict(title, description)
    assert result.outcome == "excluded" and f"start_before_graduation:{evidence}" in result.reasons


@pytest.mark.parametrize("title,description", [
    ("Software Engineer, New Grad (2027)", ""),
    (DESC_TITLE, "Spring 2027 start."),  # spring ends in May: not before a May graduation
    (DESC_TITLE, "You can start as early as January 2026."),
    (DESC_TITLE, "We welcome candidates graduating in 2026 or later."),
    (DESC_TITLE, "We started in 2015 as a two-person team."),
    (DESC_TITLE, "Since starting in 2019, the team has grown to 40 engineers."),
    (DESC_TITLE, "Preferred qualifications:\nGraduating by June 2026."),
    (DESC_TITLE, "Start dates may vary; some hires may start in 2026."),
    (DESC_TITLE, "The starting salary for 2026 is $120,000."),
    (DESC_TITLE, "Start date: January 2026. Start date for the second cohort: Summer 2027."),  # conflicting: keep
    (DESC_TITLE, "Startup experience in 2026 markets is a plus."),
    # review 2026-11-18: recruiting timelines, postings, pay, benefits, laws, history, negation, programs, titles
    ("Software Engineer, New Grad", "We will begin reviewing applications in October 2026."),
    ("Software Engineer, New Grad", "Interviews begin in November 2026."),
    (DESC_TITLE, "Applications open starting September 2026."),
    (DESC_TITLE, "Posting start date: 11/18/2026"),
    (DESC_TITLE, "Job Posting Start Date: 2026-11-10"),
    (DESC_TITLE, "Beginning January 1, 2025, Illinois law requires employers to post pay ranges."),
    (DESC_TITLE, "Pay range effective beginning January 1, 2026."),
    (DESC_TITLE, "Beginning in 2026, all employees are eligible for the new parental leave."),
    (DESC_TITLE, "We offer a 401(k) match beginning in 2026."),
    (DESC_TITLE, "Since starting in 2025, our team has grown to 40 engineers."),
    (DESC_TITLE, "Candidates who graduated in 2025 or 2026 are not eligible."),
    (DESC_TITLE, "Our graduate program, launched in 2025, has trained 200 engineers."),
    ("University Graduate - SWE (Req 2025-04567)", ""),
    ("Early Career SWE - 2026 Hiring Event", ""),
])
def test_uncertain_or_later_dates_keep(title, description):
    result = verdict(title, description)
    assert not any(r.startswith("start_before_graduation") for r in result.reasons), result.reasons


def test_the_rule_is_off_without_a_graduation():
    result = verdict("2026 New Grad Software Engineer", rules=ScreeningRules())
    assert not any(r.startswith("start_before_graduation") for r in result.reasons)


@pytest.mark.parametrize("value", ["2027-13", "May 2027", "2027-5", 2027, "1999-05"])
def test_graduation_must_be_a_year_and_month(value):
    with pytest.raises(ValueError, match="graduation"):
        rules_from_dict({"graduation": value})


def test_graduation_reads_and_changes_the_version():
    assert rules_from_dict({"graduation": "2027-05"}).graduation == "2027-05"
    assert rules_from_dict({"graduation": None}).graduation is None
    assert GRAD.version != ScreeningRules().version
