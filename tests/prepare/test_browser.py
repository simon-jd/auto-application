"""Real browser preparation tests: known answers, honest readback and no submission."""
import hashlib
from pathlib import Path

import pytest

URL = 'https://jobs.ashbyhq.com/test-only/11111111-1111-4111-8111-111111111111/application'
FORM = '''<!doctype html><html><head><title>TEST ONLY Sales</title></head><body>
<h1>TEST ONLY Sales Development Representative</h1>
<form action="/api/application.submit" method="post">
<label for="name">Legal Name*</label><input id="name" required>
<label for="email">Email*</label><input id="email" type="email" required>
<label for="phone">Phone Number*</label><input id="phone" type="tel" required>
<label for="resume">Resume*</label><input id="resume" type="file" required>
<label for="li">LinkedIn Profile</label><input id="li">
{extra}
<button type="submit">Submit Application</button></form></body></html>'''

@pytest.fixture
def run(tmp_path):
    from auto_app.prepare.candidate import fictional_candidate
    import shutil
    candidate = fictional_candidate()
    cv = tmp_path / 'fictional-sales-resume.pdf'
    shutil.copyfile(candidate['resume_path'], cv)
    candidate['resume_path'] = str(cv)
    candidate['profile']['cv'] = str(cv)
    return {'run_id': 'a' * 32, 'apply_url': URL, 'title': 'TEST ONLY Sales', 'company': 'Sandbox',
            'candidate': candidate}


def fixture_route(html, requests):
    async def setup(context):
        async def handle(request):
            requests.append((request.method, request.url))
            return {'content_type': 'text/html', 'body': html}
        return handle
    return setup


@pytest.fixture
def acknowledged_upload(monkeypatch):
    """Isolate browser/readback behavior after a simulated server acknowledgement.

    This is not an upload protocol test. test_network.py separately exercises
    exact request matching, multipart bytes, target binding and acknowledgements.
    """
    from auto_app.prepare import browser

    class SimulatedAcknowledgedUploadNetwork:
        resume_uploaded = True
        resume_attached = True

        def __init__(self, candidate, target):
            pass

        async def bind_session(self, session):
            pass

        def allow_body(self, body):
            return False

        def upload_allowed(self, request):
            return False

        def evidence(self):
            return {"simulated_upload_acknowledgement": True, "resume_uploaded": True, "resume_attached": True}

    monkeypatch.setattr(browser, 'PreparationNetwork', SimulatedAcknowledgedUploadNetwork)


async def test_simulated_upload_ack_allows_readback_ready_without_submit(run, tmp_path, acknowledged_upload):
    from auto_app.prepare.browser import fill_ashby
    requests = []
    result = await fill_ashby(run, tmp_path / 'artifacts', browser_channel='chrome',
                             route=fixture_route(FORM.format(extra=''), requests))
    assert result['status'] == 'ready_for_review'
    checks = {c['label']: c for c in result['field_checks']}
    assert checks['Legal Name']['actual'] == 'Alex TEST ONLY Sandbox FICTIONAL'
    assert checks['Email']['actual'] == 'alex.sandbox@example.invalid'
    assert checks['Resume']['actual'] == 'fictional-sales-resume.pdf'
    assert all(c['status'] == 'matched' for c in checks.values())
    assert result['missing_questions'] == []
    assert result['submission_allowed'] is False
    assert result['browser_session_retained'] is False
    assert result['upload_evidence']['simulated_upload_acknowledgement'] is True
    assert Path(tmp_path / 'artifacts' / result['screenshot']).is_file()
    assert all(method == 'GET' for method, _ in requests)


async def test_filename_without_server_upload_ack_does_not_report_ready(run, tmp_path):
    from auto_app.prepare.browser import fill_ashby

    result = await fill_ashby(run, tmp_path / 'artifacts', browser_channel='chrome',
                             route=fixture_route(FORM.format(extra=''), []))
    assert result['status'] == 'needs_input'
    resume = next(c for c in result['field_checks'] if c['label'] == 'Resume')
    assert resume['actual'] == 'fictional-sales-resume.pdf'
    assert resume['status'] == 'mismatch'
    assert resume['upload_verified'] is False
    assert any(q['label'] == 'Resume' for q in result['missing_questions'])


async def test_unknown_required_question_is_returned_without_guess(run, tmp_path):
    from auto_app.prepare.browser import fill_ashby
    extra = '<label for="sp">Will you require sponsorship?*</label><select id="sp" required><option value=""></option><option>Yes</option><option>No</option></select>'
    result = await fill_ashby(run, tmp_path / 'artifacts', browser_channel='chrome',
                             route=fixture_route(FORM.format(extra=extra), []))
    assert result['status'] == 'needs_input'
    q = next(q for q in result['missing_questions'] if 'sponsorship' in q['label'])
    assert q['options'] == ['Yes', 'No']
    assert not q.get('actual')
    assert next(c for c in result['field_checks'] if 'sponsorship' in c['label'])['expected'] is None


async def test_readback_detects_page_replacing_known_answer(run, tmp_path):
    from auto_app.prepare.browser import fill_ashby
    html = FORM.format(extra='').replace('id="email"', 'id="email" oninput="if(this.value)this.value=\'wrong@example.invalid\'"')
    result = await fill_ashby(run, tmp_path / 'artifacts', browser_channel='chrome', route=fixture_route(html, []))
    assert result['status'] == 'needs_input'
    email = next(c for c in result['field_checks'] if c['label'] == 'Email')
    assert email['actual'] == 'wrong@example.invalid'
    assert email['status'] == 'mismatch'


async def test_required_password_is_action_needed_even_with_known_fields_filled(run, tmp_path, acknowledged_upload):
    from auto_app.prepare.browser import fill_ashby

    extra = '<label for="password">Password*</label><input id="password" type="password" required>'
    result = await fill_ashby(run, tmp_path / 'artifacts', browser_channel='chrome',
                             route=fixture_route(FORM.format(extra=extra), []))
    assert result['status'] == 'action_needed'
    password = next(c for c in result['field_checks'] if c['label'] == 'Password')
    assert password['actual'] == '' and password['expected'] is None
    assert password['blocker'] == 'account_required'
    assert any(q.get('blocker') == 'account_required' for q in result['missing_questions'])


async def test_disabled_submit_does_not_report_ready(run, tmp_path, acknowledged_upload):
    from auto_app.prepare.browser import fill_ashby

    html = FORM.format(extra='').replace('<button type="submit">', '<button type="submit" disabled>')
    result = await fill_ashby(run, tmp_path / 'artifacts', browser_channel='chrome', route=fixture_route(html, []))
    assert result['status'] == 'action_needed'
    assert 'disabled' in result['note'].lower()


@pytest.mark.parametrize('attributes', ['pattern=".*@example.com"', 'aria-invalid="true"'])
async def test_invalid_equal_email_value_does_not_report_ready(run, tmp_path, acknowledged_upload, attributes):
    from auto_app.prepare.browser import fill_ashby

    html = FORM.format(extra='').replace('id="email"', f'id="email" {attributes}')
    result = await fill_ashby(run, tmp_path / 'artifacts', browser_channel='chrome', route=fixture_route(html, []))
    assert result['status'] == 'needs_input'
    email = next(c for c in result['field_checks'] if c['label'] == 'Email')
    assert email['actual'] == email['expected'] == 'alex.sandbox@example.invalid'
    assert email['invalid'] is True and email['status'] == 'mismatch'


async def test_empty_job_page_is_not_ready(run, tmp_path):
    from auto_app.prepare.browser import fill_ashby
    result = await fill_ashby(run, tmp_path / 'artifacts', browser_channel='chrome',
                             route=fixture_route('<h1>This job is no longer available</h1>', []))
    assert result['status'] == 'action_needed'
    assert result['field_checks'] == []


async def test_model_cannot_submit_via_click_enter_or_native_form(run, tmp_path):
    from auto_app.prepare.browser import fill_ashby
    requests = []
    async def hostile_driver(session):
        await session.snapshot()
        submit = next(ref for ref, it in session.items.items() if it.get('name') == 'Submit Application')
        assert 'refused' in await session.click(submit)
        assert 'refused' in await session.press('Enter')
        # Native form submission bypasses submit listeners. The network barrier must still deny it.
        await session.page.evaluate("document.querySelector('form').submit(); fetch('/api/application.submit', {method:'POST'}).catch(()=>{});")
        await session.page.wait_for_timeout(100)
    result = await fill_ashby(run, tmp_path / 'artifacts', driver=hostile_driver, browser_channel='chrome',
                             route=fixture_route(FORM.format(extra=''), requests))
    assert not any(method == 'POST' for method, _ in requests)
    assert result['guard']['blocked_requests']
    assert result['status'] != 'ready_for_review'


async def test_changed_resume_is_rejected_before_browser(run, tmp_path):
    from auto_app.prepare.browser import fill_ashby
    Path(run['candidate']['resume_path']).write_bytes(b'wrong file')
    result = await fill_ashby(run, tmp_path / 'artifacts', browser_channel='chrome')
    assert result['status'] == 'failed'
    assert 'resume' in result['note'].lower()


def test_network_policy_blocks_submissions_disguised_as_queries():
    from auto_app.prepare.guards import request_allowed
    assert not request_allowed('POST', 'https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting',
                               {'operationName': 'ApiJobPosting', 'query': 'mutation ApiJobPosting { applicationFormSubmit }'})
    assert not request_allowed('POST', 'https://jobs.ashbyhq.com/api/application.submit', {})
    assert not request_allowed('GET', 'http://127.0.0.1/internal', None)


def test_actual_ashby_cdn_and_read_queries_load_but_other_posts_do_not():
    from auto_app.prepare.guards import request_allowed
    assert request_allowed('GET', 'https://cdn.ashbyprd.com/frontend_non_user/release/.vite/manifest.json')
    assert request_allowed('POST', 'https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting',
                           {'operationName': 'ApiJobPosting', 'query': 'query ApiJobPosting { jobPosting { title } }'})
    assert not request_allowed('POST', 'https://cdn.ashbyprd.com/submit', {})


def test_graphql_get_cannot_bypass_post_operation_allowlist():
    from auto_app.prepare.guards import request_allowed
    assert not request_allowed('GET', 'https://jobs.ashbyhq.com/api/non-user-graphql?operationName=ApiSubmitApplication&query=mutation%20X')

async def test_redirect_hop_is_blocked_before_browser_can_follow(run, tmp_path):
    from auto_app.prepare.browser import fill_ashby
    requests = []
    async def setup(context):
        async def handle(request):
            requests.append(request.url)
            return {'status': 302, 'headers': {'location': 'http://127.0.0.1:9/private'}, 'body': ''}
        return handle
    result = await fill_ashby(run, tmp_path / 'artifacts', route=setup, browser_channel='chrome')
    assert result['status'] != 'ready_for_review'
    assert requests == [URL]
    assert 'redirect blocked' in result['guard']['blocked_actions']


@pytest.mark.parametrize('model_status', ['needs_input', 'ready'])
async def test_model_questions_missing_from_dom_remain_needs_input(run, tmp_path, acknowledged_upload, model_status):
    from auto_app.prepare.browser import fill_ashby
    async def driver(session):
        await session.ask_user('Work authorization', 'Are you authorized to work?', ['Yes', 'No'])
        await session.done(model_status, 'Work authorization needs your answer')
    extra = '<p>Work authorization (required)</p><button type="button">Yes</button><button type="button">No</button>'
    result = await fill_ashby(run, tmp_path / 'artifacts', driver=driver, browser_channel='chrome',
                             route=fixture_route(FORM.format(extra=extra), []))
    assert result['status'] == 'needs_input'
    question = next(q for q in result['missing_questions'] if q['label'] == 'Work authorization')
    assert question['options'] == ['Yes', 'No'] and question['required'] is True
    assert result['submission_allowed'] is False


async def test_model_needs_input_without_dom_question_is_not_ready(run, tmp_path, acknowledged_upload):
    from auto_app.prepare.browser import fill_ashby
    async def driver(session):
        await session.done('needs_input', 'A required custom control needs review')
    result = await fill_ashby(run, tmp_path / 'artifacts', driver=driver, browser_channel='chrome',
                             route=fixture_route(FORM.format(extra=''), []))
    assert result['status'] == 'needs_input'
    assert 'custom control' in result['note']
