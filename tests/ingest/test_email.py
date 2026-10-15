from email.message import EmailMessage
from urllib.parse import quote

import pytest

from auto_app.ingest.email import parse_email
from auto_app.ingest.urls import canonical_url


def make_email(*, plain=None, html=None, message_id="<recommendation-1@example.test>",
               subject="Recommended jobs for you", date="Fri, 09 Oct 2026 10:30:00 -0700"):
    message = EmailMessage()
    message["Subject"] = subject
    message["Date"] = date
    if message_id:
        message["Message-ID"] = message_id
    message.set_content(plain or "Your recommended jobs are below.")
    if html is not None:
        message.add_alternative(html, subtype="html")
    return message.as_bytes()


def test_html_extracts_multiple_jobs_and_keeps_observed_title_and_header_context():
    content = make_email(html='''
        <a href="https://www.linkedin.com/comm/jobs/view/12345?trk=email">Backend <b>Engineer</b></a>
        <a href="https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555?utm_source=email">Platform Engineer</a>
        <a href="https://example.test/unsubscribe">Unsubscribe</a>
    ''')
    parsed = parse_email(content)
    assert parsed.issues == []
    assert [(record.url, record.title) for record in parsed.records] == [
        ("https://www.linkedin.com/jobs/view/12345", "Backend Engineer"),
        ("https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555", "Platform Engineer"),
    ]
    record = parsed.records[0]
    assert record.company is None
    assert record.description == ""
    assert record.posted_at is None
    assert record.metadata["subject"] == "Recommended jobs for you"
    assert record.metadata["message_id"] == "<recommendation-1@example.test>"
    assert record.metadata["observed_at"] == "2026-10-09T10:30:00-07:00"
    assert record.metadata["date"]
    assert record.metadata["original_url"] == "https://www.linkedin.com/comm/jobs/view/12345?trk=email"


def test_multipart_repeated_links_collapse_and_specific_anchor_supplies_missing_title():
    direct = "https://www.linkedin.com/jobs/view/12345"
    redirect = "https://click.example.test/link?url=" + quote(direct + "?trk=mail", safe="")
    parsed = parse_email(make_email(plain=direct, html=f'''
        <a href="{redirect}">Apply now</a>
        <a href="{direct}?utm_campaign=jobs">Software Engineer</a>
        <a href="{direct}">View job</a>
    '''))
    assert len(parsed.records) == 1
    record = parsed.records[0]
    assert record.url == direct
    assert record.title == "Software Engineer"
    assert record.metadata["original_url"] == redirect
    assert redirect in record.metadata["original_urls"]
    assert direct + "?utm_campaign=jobs" in record.metadata["original_urls"]


@pytest.mark.parametrize("label", [
    "Apply", "Apply now", "Apply to this job", "View job", "See opening", "Job details",
    "View & apply", "Learn more", "https://jobright.ai/jobs/info/abc123",
])
def test_generic_button_and_subject_are_not_claimed_as_job_title(label):
    parsed = parse_email(make_email(
        subject="Acme is hiring a Senior Software Engineer!",
        html=f'<a href="https://jobright.ai/jobs/info/abc123">{label}</a>',
    ))
    assert len(parsed.records) == 1
    assert parsed.records[0].title is None
    assert parsed.records[0].description == ""


def test_plaintext_extracts_job_links_without_guessing_surrounding_title():
    parsed = parse_email(make_email(plain='''
        Jobs for you:
        An unstructured line which is not a confirmed title
        <https://simplify.jobs/p/11111111-2222-3333-4444-555555555555/Software-Engineer?utm_source=email>
        https://acme.example/careers/backend-engineer?jobId=42&utm_source=email.
        https://boards.greenhouse.io/acme/jobs/9876?gh_src=newsletter
        https://example.test/preferences
    '''))
    assert [record.url for record in parsed.records] == [
        "https://simplify.jobs/p/11111111-2222-3333-4444-555555555555/Software-Engineer",
        "https://acme.example/careers/backend-engineer?jobId=42",
        "https://boards.greenhouse.io/acme/jobs/9876",
    ]
    assert all(record.title is None for record in parsed.records)


def test_html_without_jobs_falls_back_to_plain_text():
    parsed = parse_email(make_email(
        plain="https://jobs.ashbyhq.com/acme/11111111-2222-3333-4444-555555555555",
        html='<a href="https://example.test">Read newsletter</a>',
    ))
    assert len(parsed.records) == 1
    assert parsed.records[0].url.endswith("/acme/11111111-2222-3333-4444-555555555555")


def test_external_ids_are_stable_per_message_and_url_with_content_digest_fallback():
    content = make_email(plain="https://www.linkedin.com/jobs/view/12345", message_id=None)
    first = parse_email(content).records[0]
    assert first.external_id == parse_email(content).records[0].external_id
    assert first.metadata["message_id"] is None
    changed = parse_email(make_email(plain="https://www.linkedin.com/jobs/view/12345", message_id="<different@example.test>")).records[0]
    assert changed.external_id != first.external_id
    repeated_message = parse_email(make_email(html='''
        <a href="https://www.linkedin.com/jobs/view/12345?trk=mail">View job</a>
        <a href="https://www.linkedin.com/jobs/view/98765">View job</a>
    ''')).records
    same_message = parse_email(make_email(plain="https://www.linkedin.com/jobs/view/12345")).records[0]
    assert repeated_message[0].external_id == same_message.external_id
    assert repeated_message[0].external_id != repeated_message[1].external_id


def test_no_job_mail_does_not_import_navigation_promotions_or_attachments():
    message = EmailMessage()
    message.set_content("Your weekly newsletter")
    message.add_alternative('''
        <a href="https://www.linkedin.com/jobs/">Find jobs</a>
        <a href="https://www.linkedin.com/jobs/search/?keywords=engineer">Browse jobs</a>
        <a href="https://jobright.ai/jobs">Recommended jobs</a>
        <a href="https://simplify.jobs/">Simplify</a>
        <a href="https://jobs.lever.co/acme">All positions</a>
        <a href="https://acme.example/careers/settings">Manage alerts</a>
        <a href="https://acme.example/jobs/premium">Upgrade to premium</a>
        <a href="mailto:jobs@example.test">Contact</a>
    ''', subtype="html")
    message.add_attachment(b"https://www.linkedin.com/jobs/view/12345", maintype="text", subtype="plain", filename="notes.txt")
    assert parse_email(message.as_bytes()).records == []


@pytest.mark.parametrize(("url", "expected"), [
    ("https://www.linkedin.com/comm/jobs/view/12345?trk=mail&trackingId=x", "https://www.linkedin.com/jobs/view/12345"),
    ("https://linkedin.com/jobs/view/software-engineer-at-acme-12345?refId=abc", "https://www.linkedin.com/jobs/view/12345"),
    ("https://ACME.example/jobs/role/?utm_source=mail&jobId=7&gh_jid=9&department=eng", "https://acme.example/jobs/role?department=eng&gh_jid=9&jobId=7"),
    ("https://click.example.test/?target=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F12345%3Ftrk%3Dmail", "https://www.linkedin.com/jobs/view/12345"),
    ("https://click.example.test/?joburl=https%253A%252F%252Fjobright.ai%252Fjobs%252Finfo%252Fabc123", "https://jobright.ai/jobs/info/abc123"),
    ("https://acme.example/jobs/role?url=resume&source=internal&job_id=3", "https://acme.example/jobs/role?job_id=3&source=internal&url=resume"),
    ("https://acme.example/jobs/role?refId=R1&eid=99&utm_source=mail", "https://acme.example/jobs/role?eid=99&refId=R1"),
    ("mailto:jobs@example.test", None),
    ("javascript:alert(1)", None),
    ("/jobs/12345", None),
    ("https://[broken/jobs/1", None),
])
def test_canonical_url_strips_only_tracking_and_preserves_job_identity(url, expected):
    assert canonical_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Seattle/Engineer_R1234",
    "https://jobright.ai/jobs/info/abc123",
    "https://jobs.ashbyhq.com/acme/11111111-2222-3333-4444-555555555555/application",
    "https://careers.acme.example/jobs/12345",
    "https://acme.example/careers?gh_jid=12345",
    "https://www.google.com/about/careers/applications/jobs/results/123456-software-engineer",
    "https://www.tesla.com/careers/search/job/software-engineer-123456",
])
def test_recognizes_specific_job_urls(url):
    parsed = parse_email(make_email(plain=url))
    assert len(parsed.records) == 1


@pytest.mark.parametrize("url", [
    "https://www.google.com/about/careers/applications/jobs/results",
    "https://www.google.com/about/careers/applications/jobs/results?search=engineer",
    "https://www.tesla.com/careers/search",
    "https://www.tesla.com/careers/search/job",
    "https://www.tesla.com/careers/search/benefits",
    "https://acme.example/careers/about",
    "https://acme.example/jobs/search/engineer",
])
def test_specific_career_routes_do_not_admit_navigation(url):
    assert parse_email(make_email(plain=url)).records == []


def test_bad_date_does_not_become_job_posting_date_or_drop_job():
    raw = b"Subject: Jobs\nDate: not a date\nContent-Type: text/plain; charset=utf-8\n\nhttps://www.linkedin.com/jobs/view/12345\n"
    record = parse_email(raw).records[0]
    assert record.posted_at is None
    assert record.metadata["observed_at"] is None
