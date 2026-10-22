from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select

from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)

def mail(sender, subject, body):
    m = EmailMessage()
    m["From"], m["Subject"] = sender, subject
    m.set_content(body)
    return m.as_bytes()

@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Backend Engineer"), record("102", "Data Engineer"),
                   record("103", "ML Engineer", company="Beta", url="https://jobs.lever.co/beta/test")])
    for title in ("Backend Engineer", "Data Engineer", "ML Engineer"):
        result.mark_job(job_id_of(result, title), "applied", occurred_at=NOW - timedelta(days=1))
    yield result
    result.engine.dispose()

@pytest.mark.parametrize("subject,body,kind", [
    ("Your application to Acme", "Unfortunately, we have decided to move forward with other candidates.", "rejected"),
    ("Acme Online Assessment", "Please complete the HackerRank challenge within 7 days.", "assessment"),
    ("Next steps with Acme", "We'd like to schedule a phone screen. Please share your availability.", "interview"),
    ("Offer", "We are pleased to offer you the position of Software Engineer.", "offer"),
    ("Thank you for applying to Acme", "We have received your application.", "applied"),
    ("Acme newsletter", "Our latest product news.", None),
    ("Interview tips", "Prepare for your interview with these tips and share your availability.", None),
    ("Recommended jobs", "New job offer, HackerRank assessment and interview opportunities.", None),
    ("Application update", "Unfortunately, our website is temporarily unavailable.", None),
    ("Application update", "We are not pleased to offer you this position.", None),
    ("Your application", "We are not scheduling an interview at this time.", None),
    ("Your application", "We have not received your application.", None),
])
def test_classification_rules(subject, body, kind):
    from auto_app.track.updates import classify
    assert classify(subject, body) == kind


def test_unique_match_duplicate_and_no_body_persistence(store):
    from auto_app.core.db import tables as T
    from auto_app.track.events import events_for
    from auto_app.track.updates import record_update
    content = mail("Beta Recruiting <talent@beta.com>", "Your application", "We will not be moving forward with your application. PRIVATE-BODY")
    result = record_update(store, "mail", "imap:1:5", content, NOW)
    assert result["status"] == "matched" and result["job_id"] == job_id_of(store, "ML Engineer")
    assert [e["kind"] for e in events_for(store, result["job_id"])] == ["applied", "rejected"]
    assert record_update(store, "mail", "imap:1:5", content, NOW)["status"] == "duplicate"
    with store.engine.connect() as conn:
        rows = list(conn.execute(select(T.mail_updates)).mappings())
        assert len(rows) == 1 and "PRIVATE-BODY" not in str(rows)


def test_ambiguous_title_boundaries_and_review_assignment(store):
    from auto_app.track.updates import assign, record_update, unmatched
    by_title = record_update(store, "mail", "1", mail("Acme <jobs@acme.com>",
        "Acme Data Engineer: next steps", "We would like to schedule a phone screen."), NOW)
    assert by_title["job_id"] == job_id_of(store, "Data Engineer")
    unclear = record_update(store, "mail", "2", mail("Acme <jobs@acme.com>",
        "Your Acme application", "We have decided to move forward with other candidates."), NOW)
    assert unclear["status"] == "unmatched"
    assert [u["update_id"] for u in unmatched(store)] == [unclear["update_id"]]
    assert assign(store, unclear["update_id"], job_id_of(store, "Backend Engineer"))["status"] == "matched"
    assert unmatched(store) == []
    with pytest.raises(ValueError):
        assign(store, unclear["update_id"], job_id_of(store, "ML Engineer"))


def test_no_guessing_company_or_unapplied_job(store):
    from auto_app.track.updates import record_update
    content = mail("Other <jobs@alphabetacorp.com>", "Your application", "We have decided to move forward with other candidates.")
    assert record_update(store, "mail", "1", content, NOW)["status"] == "unmatched"
    store.mark_job(job_id_of(store, "ML Engineer"), "want_to_apply")
    content = mail("Beta <talent@beta.com>", "Your application", "We have decided to move forward with other candidates.")
    assert record_update(store, "mail", "2", content, NOW)["status"] == "unmatched"


def test_assignment_validates_job_and_event_failure_rolls_back(store, monkeypatch):
    from auto_app.track import updates
    content = mail("Unknown <jobs@unknown.com>", "Your application", "We have decided to move forward with other candidates.")
    result = updates.record_update(store, "mail", "1", content, NOW)
    with pytest.raises(ValueError):
        updates.assign(store, result["update_id"], 99999)
    def fail(*args, **kwargs):
        raise RuntimeError("event failure")
    monkeypatch.setattr(updates, "record_event", fail)
    with pytest.raises(RuntimeError):
        updates.assign(store, result["update_id"], job_id_of(store, "Backend Engineer"))
    assert updates.unmatched(store)[0]["update_id"] == result["update_id"]


def test_match_event_failure_leaves_no_update(store, monkeypatch):
    from auto_app.core.db import tables as T
    from auto_app.track import updates
    def fail(*args, **kwargs):
        raise RuntimeError("event failure")
    monkeypatch.setattr(updates, "record_event", fail)
    with pytest.raises(RuntimeError):
        updates.record_update(store, "mail", "1", mail("Beta <jobs@beta.com>", "Your application",
            "We have decided to move forward with other candidates."), NOW)
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(T.mail_updates)).scalar() == 0


def test_dismiss_and_strict_inputs(store):
    from auto_app.track.updates import dismiss, record_update, unmatched
    result = record_update(store, "mail", "1", mail("Unknown <jobs@unknown.com>", "Your application",
        "We have decided to move forward with other candidates."), NOW)
    for invalid in (True, "1", 0, -1):
        with pytest.raises(ValueError):
            dismiss(store, invalid)
    assert dismiss(store, result["update_id"])["status"] == "dismissed"
    assert unmatched(store) == []
    with pytest.raises(ValueError):
        dismiss(store, result["update_id"])


@pytest.mark.parametrize("subject,body", [
    ("Your application", "We will not invite you to an interview at this time."),
    ("Your application", "We won't be inviting you to an interview."),
    ("Special offer", "We are pleased to offer you a 20% discount on our webinar."),
    ("Your application", "No action needed today.\n> We would like to invite you to an interview."),
])
def test_negations_promotions_and_quoted_status_are_not_new_updates(subject, body):
    from auto_app.track.updates import classify
    assert classify(subject, body) is None


def test_company_in_quoted_history_is_not_matching_evidence(store):
    from auto_app.track.updates import record_update
    content = mail("Other <jobs@other.com>", "Your application", "We have decided to move forward with other candidates.\n> Previous employer: Beta")
    assert record_update(store, "mail", "quote-company", content, NOW)["status"] == "unmatched"


def test_html_quoted_invitation_is_not_a_new_status(store):
    from auto_app.track.updates import record_update
    message = EmailMessage()
    message["From"], message["Subject"] = "Beta <jobs@beta.com>", "Your application"
    message.set_content('<p>No new updates.</p><blockquote>We would like to invite you to an interview.</blockquote>', subtype="html")
    assert record_update(store, "mail", "html-quote", message.as_bytes(), NOW) == {"kind": None}


@pytest.mark.parametrize("body", [
    "Your offer letter is not ready yet. We still need to decide whether to offer you the role.",
    "If you pass the remaining interview, we would be pleased to offer you the position.",
    "Please confirm your interview has been cancelled.",
    "Please confirm your interview is not going ahead.",
])
def test_conditional_offers_and_cancelled_interviews_are_not_outcomes(store, body):
    from auto_app.track.updates import record_update
    from auto_app.track.events import events_for
    result = record_update(store, "mail", body, mail("Beta <jobs@beta.com>", "Your application", body), NOW)
    assert result == {"kind": None}
    assert [e["kind"] for e in events_for(store, job_id_of(store, "ML Engineer"))] == ["applied"]


@pytest.mark.parametrize("body,html", [
    ("No change to your application today.\nFrom: Beta Recruiting\nSent: Monday\nSubject: Interview\nWe would like to invite you to an interview.", False),
    ("No change today.\n-----Original Message-----\nWe would like to invite you to an interview.", False),
    ('<p>No change today.</p><div id="divRplyFwdMsg">From: Beta Recruiting<br>Sent: Monday<br>Subject: Interview</div><p>We would like to invite you to an interview.</p>', True),
    ('<p>No change today.</p><div class="gmail_quote">We would like to invite you to an interview.</div>', True),
])
def test_outlook_reply_and_html_history_do_not_create_status(store, body, html):
    from auto_app.track.updates import record_update
    message = EmailMessage()
    message["From"], message["Subject"] = "Beta <jobs@beta.com>", "Your application"
    message.set_content(body, subtype="html" if html else "plain")
    assert record_update(store, "mail", body, message.as_bytes(), NOW) == {"kind": None}


def test_generic_company_word_requires_explicit_employer_evidence(store):
    from auto_app.track.updates import record_update
    ingest(store, [record("104", "Engineer", company="Next", url="https://jobs.lever.co/next/test")])
    next_job = job_id_of(store, "Engineer")
    store.mark_job(next_job, "applied")
    body = "We would like to schedule a phone screen."
    assert record_update(store, "mail", "generic-next", mail("Unrelated Company <hr@unrelated.example>", "Next steps", body), NOW)["status"] == "unmatched"
    assert record_update(store, "mail", "employer-next", mail("Next Recruiting <hr@next.example>", "Next steps", body), NOW)["job_id"] == next_job


@pytest.mark.parametrize("subject", ["Ready for next steps", "Invitation to next steps", "Continuing with next steps", "Joining next stage", "Application for next steps"])
def test_generic_sentence_context_is_not_employer_identity(store, subject):
    from auto_app.track.updates import record_update
    from auto_app.track.events import events_for
    ingest(store, [record("104", "Engineer", company="Next", url="https://jobs.lever.co/next/test")])
    next_job = job_id_of(store, "Engineer")
    store.mark_job(next_job, "applied")
    result = record_update(store, "mail", subject, mail("Unrelated Company <hr@unrelated.example>", subject,
        "We would like to schedule a phone screen."), NOW)
    assert result["status"] == "unmatched"
    assert [e["kind"] for e in events_for(store, next_job)] == ["applied"]


def test_generic_sender_prose_is_not_an_exact_employer_identity(store):
    from auto_app.track.updates import record_update
    ingest(store, [record("104", "Engineer", company="Next", url="https://jobs.lever.co/next/test")])
    store.mark_job(job_id_of(store, "Engineer"), "applied")
    result = record_update(store, "mail", "sender-prose", mail("Unrelated Next Steps Team <hr@unrelated.example>", "Invitation",
        "We would like to schedule a phone screen."), NOW)
    assert result["status"] == "unmatched"


def test_explicit_organization_attribution_can_identify_a_common_name(store):
    from auto_app.track.updates import record_update
    ingest(store, [record("104", "Engineer", company="Next", url="https://jobs.lever.co/next/test")])
    next_job = job_id_of(store, "Engineer")
    store.mark_job(next_job, "applied")
    result = record_update(store, "mail", "organization-field", mail("Hiring Platform <hr@platform.example>", "Invitation",
        "Employer: Next\nWe would like to schedule a phone screen."), NOW)
    assert result["status"] == "matched" and result["job_id"] == next_job


@pytest.mark.parametrize("sender", [
    "Beta Corporation <noreply@beta.com>", "The Beta Recruiting Team <talent@beta.com>",
    '"Beta, Inc. Talent Acquisition" <talent@beta.com>', "Beta via Greenhouse <no-reply@greenhouse.io>",
    "Beta Hiring Team <jobs@beta.com>",
])
def test_ordinary_employer_sender_names_match(store, sender):
    from auto_app.track.updates import record_update
    result = record_update(store, "mail", sender, mail(sender, "Your application",
        "We have decided to move forward with other candidates."), NOW)
    assert result["job_id"] == job_id_of(store, "ML Engineer")


def test_distinct_interview_emails_on_one_day_are_separate_events(store):
    from datetime import timedelta
    from auto_app.track.events import events_for
    from auto_app.track.updates import record_update
    body = "We would like to schedule a phone screen."
    for key, hours in (("first", 0), ("second", 5)):
        record_update(store, "mail", key, mail("Beta <jobs@beta.com>", "Next steps", body), NOW + timedelta(hours=hours))
    record_update(store, "mail", "second", mail("Beta <jobs@beta.com>", "Next steps", body), NOW + timedelta(hours=5))
    assert [e["kind"] for e in events_for(store, job_id_of(store, "ML Engineer"))] == ["applied", "interview", "interview"]


@pytest.mark.parametrize("body", [
    "Please note that this does not mean your application is rejected.",
    "It is not true that your application was rejected.",
    "It is not the case that your application was rejected.",
    "The claim that your application was rejected is not true.",
    "We cannot confirm that your application has been rejected.",
    "We have not said that your application was rejected.",
    "We have not stated that your application was rejected.",
    "We did not report that your application has been rejected.",
    "If your application is rejected, you will receive a separate notice.",
    "Your application is rejected only if you fail the remaining checks.",
    "Unless you complete the assessment, we will not be moving forward with your application.",
    "We will not be moving forward with your application if you withdraw it.",
    "We regret to inform you that if the background check fails, you will not be selected.",
])
def test_negated_or_conditional_rejection_preserves_pipeline(store, body):
    from auto_app.track.events import events_for, record_event, weekly_summary
    from auto_app.track.updates import record_update

    job_id = job_id_of(store, "ML Engineer")
    record_event(store, job_id, "interview", occurred_at=NOW)
    before = events_for(store, job_id)
    result = record_update(store, "mail", body, mail("Beta Recruiting <hr@beta.example>", "Application update", body), NOW)
    assert result == {"kind": None}
    assert events_for(store, job_id) == before
    assert weekly_summary(store, NOW)["active_pipeline"] == {"interview": 1}


@pytest.mark.parametrize("body", [
    "We will not be moving forward with your application.",
    "We won't move forward with your application.",
    "We are not moving forward with your application.",
    "This was not an easy decision, but we have decided to pursue other candidates.",
    "We regret to inform you that you were not selected for the position.",
    "Your application was rejected; if you have questions, please contact us.",
    "We confirm that your application has been rejected.",
    "We cannot provide detailed feedback, but we confirm that your application was rejected.",
])
def test_actual_rejections_keep_their_intrinsic_negative_wording(body):
    from auto_app.track.updates import classify

    assert classify("Application update", body) == "rejected"


@pytest.mark.parametrize("body,kind", [
    ("We regret to inform you that, while your skills may be a fit for other roles, you were not selected for this position.", "rejected"),
    ("We regret to inform you that you were not selected for this position.", "rejected"),
    ("You might hear that we have decided to move forward with other candidates.", None),
    ("We regret to inform you that if the position is filled, you will not be selected.", None),
])
def test_polite_modal_inside_a_rejection_does_not_hide_it(body, kind):
    from auto_app.track.updates import classify
    assert classify("Your application", body) == kind
