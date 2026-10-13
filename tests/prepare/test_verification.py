"""Readback distinguishes valid committed answers from present-looking values."""

from pathlib import Path

import pytest


def test_required_account_field_is_retained_as_an_action_blocker():
    from auto_app.prepare.verification import field_check

    check = field_check(label="Password", role="textbox", required=True, expected=None,
                        actual="must-not-be-returned", source=None, account=True)
    assert check["status"] == "unanswered"
    assert check["blocker"] == "account_required"
    assert check["actual"] == ""


@pytest.mark.parametrize("label, blocked", [("Account username", True), ("Describe your Account Executive experience", False)])
def test_account_label_detection_does_not_confuse_sales_experience_with_credentials(label, blocked):
    from auto_app.prepare.verification import field_check

    check = field_check(label=label, role="textbox", required=True, expected=None, actual="", source=None)
    assert (check.get("blocker") == "account_required") is blocked


def test_invalid_known_value_never_matches_even_when_text_is_equal():
    from auto_app.prepare.verification import field_check

    check = field_check(label="Email", role="textbox", required=True,
                        expected="alex.sandbox@example.invalid", actual="alex.sandbox@example.invalid",
                        source="fictional_candidate.email", invalid=True)
    assert check["status"] == "mismatch"
    assert check["invalid"] is True


@pytest.mark.parametrize("upload_verified, expected_status", [(False, "mismatch"), (True, "matched")])
def test_resume_filename_requires_successful_upload_evidence(upload_verified, expected_status):
    from auto_app.prepare.verification import field_check

    check = field_check(label="Resume", role="file", required=True, expected="fictional-sales-resume.pdf",
                        actual="fictional-sales-resume.pdf", source="fictional_candidate.resume",
                        upload_verified=upload_verified)
    assert check["status"] == expected_status
    assert check["upload_verified"] is upload_verified


def test_successful_upload_does_not_hide_wrong_attachment():
    from auto_app.prepare.verification import field_check

    check = field_check(label="Resume", role="file", required=True, expected="fictional-sales-resume.pdf",
                        actual="old-resume.pdf", source="fictional_candidate.resume", upload_verified=True)
    assert check["status"] == "mismatch"


def test_typed_combobox_query_does_not_count_as_committed_selection():
    from auto_app.prepare.verification import field_check

    check = field_check(label="Working location", role="combobox", required=True,
                        expected="Seattle, WA, United States", actual="Seattle, WA, United States",
                        source="fictional_candidate.location", selection_confirmed=False)
    assert check["status"] == "missing"
    assert check["selection_confirmed"] is False


@pytest.fixture
async def session(tmp_path):
    from playwright.async_api import async_playwright
    from auto_app.apply.profile import Me, Profile
    from auto_app.prepare.candidate import fictional_candidate
    from auto_app.prepare.session import PreparationSession

    candidate = fictional_candidate()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        try:
            yield PreparationSession(
                page=page, mode="start", app={},
                me=Me(Profile.model_validate(candidate["profile"]), {}, Path(candidate["resume_path"])),
                matcher=None, time_limit_s=60, log_path=tmp_path / "actions.jsonl",
                get_password=lambda *args: None, has_password=lambda *args: False,
            )
        finally:
            await browser.close()


async def test_dom_required_password_stays_visible_without_leaking_value(session):
    from auto_app.prepare.verification import verify_fields

    await session.page.set_content('<label for="pwd">Password</label><input id="pwd" type="password" required value="secret-test">')
    checks, missing = await verify_fields(session)
    assert len(checks) == 1
    assert checks[0]["blocker"] == "account_required"
    assert checks[0]["status"] == "unanswered"
    assert missing[0]["blocker"] == "account_required"
    assert "secret-test" not in str((checks, missing))


@pytest.mark.parametrize("attributes", ['pattern=".*@example.com"', 'aria-invalid="true"'])
async def test_dom_validity_and_aria_invalid_override_equal_values(session, attributes):
    from auto_app.prepare.verification import verify_fields

    await session.page.set_content('<label for="email">Email</label><input id="email" type="email" required '
                                   f'{attributes} value="alex.sandbox@example.invalid">')
    checks, missing = await verify_fields(session)
    assert checks[0]["status"] == "mismatch"
    assert checks[0]["invalid"] is True
    assert missing[0]["label"] == "Email"


async def test_dom_location_query_requires_committed_selection(session):
    from auto_app.prepare.verification import verify_fields

    await session.page.set_content('''<label for="location">Working location</label><div class="select-control">
        <input id="location" role="combobox" aria-required="true" aria-expanded="false"
        value="Seattle, WA, United States"></div>''')
    checks, missing = await verify_fields(session)
    assert checks[0]["status"] == "missing"
    assert checks[0]["selection_confirmed"] is False
    assert missing
    await session.page.locator('#location').evaluate("e => {e.value=''; const v=document.createElement('div'); v.className='select-singleValue'; v.textContent='Seattle, WA, United States'; e.parentElement.appendChild(v);}")
    checks, missing = await verify_fields(session)
    assert checks[0]["status"] == "matched"
    assert checks[0]["selection_confirmed"] is True
    assert not missing


async def test_dom_file_default_is_unverified_until_upload_acknowledged(session):
    from auto_app.prepare.verification import verify_fields

    await session.page.set_content('<label for="resume">Resume</label><input id="resume" type="file" required>')
    await session.page.locator('#resume').set_input_files(session.me.cv)
    checks, missing = await verify_fields(session)
    assert checks[0]["status"] == "mismatch"
    assert missing
    session.upload_verified = True
    checks, missing = await verify_fields(session)
    assert checks[0]["status"] == "matched"
    assert not missing
