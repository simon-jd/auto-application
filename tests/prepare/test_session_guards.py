"""Exercise preparation tool boundaries against local, network-isolated DOMs."""

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from auto_app.apply.profile import Answer, Me, Profile
from auto_app.prepare.candidate import fictional_candidate
from auto_app.prepare.session import PreparationSession


@pytest.fixture
async def session():
    candidate = fictional_candidate()
    me = Me(Profile.model_validate(candidate['profile']),
            {k: Answer.model_validate(v) for k, v in candidate['answers'].items()},
            Path(candidate['resume_path']))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='chrome', headless=True)
        context = await browser.new_context(service_workers='block')
        await context.route('**/*', lambda route: route.abort())
        page = await context.new_page()
        value = PreparationSession(page=page, mode='start', app={
            'apply_url': 'https://jobs.ashbyhq.com/example/11111111-1111-4111-8111-111111111111/application',
        }, me=me, get_password=lambda *args: None, has_password=lambda *args: False)
        try:
            yield value
        finally:
            await browser.close()


async def field(session, html, label):
    await session.page.set_content(html)
    await session.snapshot()
    return next(ref for ref, item in session.items.items() if item.get('name') == label)


async def test_option_cannot_borrow_an_answer_from_a_different_field(session):
    await session.page.set_content('''
      <label for="sms">Do you consent to receive SMS messages?</label>
      <input id="sms" role="combobox" aria-controls="sms-options">
      <div id="sms-options" role="listbox"><div role="option" onclick="document.querySelector('#sms').value='No'">No</div></div>
      <label for="work">Will you require sponsorship?</label><input id="work" role="combobox" aria-controls="work-options">
      <div id="work-options" role="listbox"><div role="option" onclick="document.querySelector('#work').value='No'">No</div></div>
    ''')
    await session.snapshot()
    await session.answer_for([{'label': 'Do you consent to receive SMS messages?'}])
    sms_ref = next(ref for ref, item in session.items.items() if item.get('name') == 'Do you consent to receive SMS messages?')
    await session.click(sms_ref)
    await session.snapshot()
    other = await session.page.locator('#work-options [role=option]').get_attribute('data-aa-ref')
    assert 'refused' in await session.click(other)
    assert await session.page.locator('#work').input_value() == ''


async def test_known_custom_option_is_bound_across_snapshot_refresh(session):
    ref = await field(session, '''
      <label for="sms">Do you consent to receive SMS messages?</label>
      <input id="sms" role="combobox" aria-controls="choices">
      <div id="choices" role="listbox"><div role="option" onclick="document.querySelector('#sms').value='No'; document.querySelector('#sms').dataset.selected='No'">No</div></div>
    ''', 'Do you consent to receive SMS messages?')
    assert 'refused' not in await session.choose_option(ref, 'No')
    assert await session.page.locator('#sms').input_value() == 'No'
    assert await session.page.locator('#sms').get_attribute('data-selected') == 'No'


async def test_unknown_checkbox_cannot_be_unchecked(session):
    ref = await field(session, '<label><input type="checkbox" checked>I am authorized to work here</label>',
                      'I am authorized to work here')
    assert 'refused' in await session.check(ref, False)
    assert await session.page.locator('input').is_checked()


async def test_approved_no_can_only_uncheck_its_own_checkbox(session):
    ref = await field(session, '<label><input type="checkbox" checked>Do you consent to receive SMS messages?</label>',
                      'Do you consent to receive SMS messages?')
    assert 'refused' in await session.check(ref, True)
    assert 'refused' not in await session.check(ref, False)
    assert not await session.page.locator('input').is_checked()


async def test_select_does_not_expand_no_to_an_unapproved_longer_answer(session):
    ref = await field(session, '''<label for="sms">Do you consent to receive SMS messages?</label>
      <select id="sms"><option value=""></option><option>No, unless you share my contact details</option></select>''',
      'Do you consent to receive SMS messages?')
    assert 'refused' in await session.select(ref, 'No')
    assert await session.page.locator('select').input_value() == ''


async def test_select_preserves_an_exact_approved_choice(session):
    ref = await field(session, '''<label for="sms">Do you consent to receive SMS messages?</label>
      <select id="sms"><option value=""></option><option>No</option><option>Yes</option></select>''',
      'Do you consent to receive SMS messages?')
    assert 'refused' not in await session.select(ref, 'No')
    assert await session.page.locator('select').input_value() == 'No'


@pytest.mark.parametrize('label', ['Cover Letter', 'Autofill from resume'])
async def test_resume_upload_is_refused_for_other_attachment_purposes(session, label):
    ref = await field(session, f'<label for="upload">{label}</label><input id="upload" type="file">', label)
    assert session.expected(label, 'file') == (None, None)
    assert 'refused' in await session.upload_cv(ref)
    assert await session.page.locator('input').evaluate('e => e.files.length') == 0


async def test_actual_resume_upload_remains_available(session):
    ref = await field(session, '<label for="upload">Resume</label><input id="upload" type="file">', 'Resume')
    assert 'refused' not in await session.upload_cv(ref)
    assert await session.page.locator('input').evaluate('e => e.files[0].name') == 'fictional-sales-resume.pdf'


@pytest.mark.parametrize('html', [
    '<label for="mail">Email</label><input id="mail" type="password">',
    '<form><h1>Sign in</h1><label for="mail">Email</label><input id="mail" type="email"><input type="password" aria-label="Password"></form>',
])
async def test_password_and_account_email_fields_cannot_receive_candidate_values(session, html):
    ref = await field(session, html, 'Email')
    assert 'refused' in await session.type(ref, 'alex.sandbox@example.invalid')
    assert await session.page.locator('#mail').input_value() == ''
    assert session.items[ref].get('password') or session.items[ref].get('account_field')


async def test_required_ashby_label_and_payroll_location_are_recognized(session):
    label = 'Where do you plan on working from (for payroll tax purposes)?'
    ref = await field(session, f'''<div class="ashby-application-form-field-entry">
      <label class="_label_abc _required_abc" for="location">{label}</label><input id="location"></div>''', label)
    assert session.items[ref].get('required') is True
    assert session.expected(label)[0] == 'Seattle, WA, United States'


async def test_arrow_keys_cannot_answer_an_unknown_focused_select(session):
    await field(session, '''<label for="work">Will you require sponsorship?</label>
      <select id="work"><option value=""></option><option>Yes</option><option>No</option></select>''',
      'Will you require sponsorship?')
    await session.page.locator('select').focus()
    assert 'refused' in await session.press('ArrowDown')
    assert await session.page.locator('select').input_value() == ''

async def test_phone_required_label_does_not_make_optional_sms_radio_required(session):
    await session.page.set_content('''<div class="ashby-application-form-field-entry">
      <label class="_required_x" for="phone">Phone</label><input id="phone" required>
      <label><input type="radio" name="sms">No SMS</label></div>''')
    await session.snapshot()
    radio = next(it for it in session.items.values() if it.get('role') == 'radio')
    assert radio['required'] is False


async def test_location_option_uses_only_fixed_state_spelling_equivalence(session):
    ref = await field(session, '''<label for="loc">Working location</label>
      <input id="loc" role="combobox" aria-controls="loc-choices">
      <div id="loc-choices" role="listbox"><div role="option" onclick="document.querySelector('#loc').value=this.textContent">Seattle, Washington, United States</div></div>''', 'Working location')
    assert 'refused' not in await session.choose_option(ref, 'Seattle, WA, United States')
    assert await session.page.locator('#loc').input_value() == 'Seattle, Washington, United States'

async def test_prefill_selects_approved_no_radio_even_when_yes_comes_first(session):
    from auto_app.prepare.browser import prefill
    await session.page.set_content('''<fieldset><legend>Do you consent to receive SMS messages?</legend>
      <label><input type="radio" name="sms" value="yes" required>Yes</label>
      <label><input type="radio" name="sms" value="no">No</label></fieldset>''')
    await prefill(session)
    assert await session.page.locator('input[value=no]').is_checked()
    assert not await session.page.locator('input[value=yes]').is_checked()
