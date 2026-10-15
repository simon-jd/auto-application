"""Packets carry what an agent needs to apply — never account secrets; resumes stay inside resumes/."""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from auto_app.core.db import tables as T
from tests.handoff.test_service import NOW, env, service  # noqa: F401 — shared fixture
from tests.imported_jobs import make_store

PROFILE = """name: {first: Alex, last: Doe}
email: alex@example.com
cv: profile/cv.pdf
accounts:
  email: alex@example.com
  password: "Change-me-1!"
  passwords: {nvidia.wd5.myworkdayjobs.com: "secret"}
"""


def write_profile(home):
    (home / "profile").mkdir()
    (home / "profile" / "profile.yaml").write_text(PROFILE)
    (home / "profile" / "answers.yaml").write_text('requires_sponsorship: {question: "Sponsorship?", answer: "Yes"}\n')


def test_the_packet_never_contains_account_secrets(env):  # noqa: F811
    home, ids = env
    write_profile(home)
    s = service(home)
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    packet = s.packet(token, now=NOW)
    text = json.dumps(packet)
    assert "Change-me-1!" not in text and "secret" not in text and "accounts" not in packet["profile"]
    assert packet["profile"]["email"] == "alex@example.com" and packet["answers"]["requires_sponsorship"]["answer"] == "Yes"
    assert packet["job"]["title"] == "Backend Engineer" and packet["job"]["apply_url"].startswith("https://")


def test_accounts_are_recorded_without_passwords(env):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, _ = env
    s = service(home)
    s.record_account("acme.wd5.myworkdayjobs.com", "alex@example.com", True, "verified by email")
    with pytest.raises(HandoffError, match="password"):
        s.record_account("beta.wd1.myworkdayjobs.com", "alex@example.com", True, None, password="x")
    store = make_store(home)
    with store.engine.connect() as conn:
        rows = conn.execute(select(T.accounts)).mappings().all()
    store.engine.dispose()
    assert [r["site"] for r in rows] == ["acme.wd5.myworkdayjobs.com"] and rows[0]["created_by_agent"] is True


def test_resume_paths_must_stay_inside_the_resumes_folder(env, tmp_path):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, ids = env
    (home / "resumes").mkdir()
    good = home / "resumes" / "acme-backend.pdf"
    good.write_bytes(b"%PDF-1.7 fake")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.7")
    (home / "resumes" / "link.pdf").symlink_to(outside)
    s = service(home)
    saved = s.register_resume(ids["Backend Engineer"], str(good), "tailored for backend")
    assert saved["bytes"] == 13 and len(saved["sha256"]) == 64
    for bad in (str(outside), str(home / "resumes" / ".." / "data" / "x.pdf"), str(home / "resumes" / "link.pdf")):
        with pytest.raises(HandoffError, match="resumes"):
            s.register_resume(ids["Backend Engineer"], bad)
    (home / "resumes" / "notes.txt").write_text("x")
    with pytest.raises(HandoffError, match="pdf"):
        s.register_resume(ids["Backend Engineer"], str(home / "resumes" / "notes.txt"))
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    assert [r["path"] for r in s.packet(token, now=NOW)["resumes"]] == [str(good.resolve())]


def test_packet_allowlists_profile_fields_and_scrubs_nested_answers(env):
    home, ids = env
    write_profile(home)
    (home / 'profile' / 'profile.yaml').write_text(PROFILE + '\nunknown: hidden-value\nlinks: {github: "https://github.com/alex", secret: "hidden-nested"}\n')
    (home / 'profile' / 'answers.yaml').write_text('work: {question: "Authorized?", answer: "Yes", token: "hidden-answer"}\n')
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    packet = s.packet(token, now=NOW)
    output = json.dumps(packet)
    assert not any(secret in output for secret in ('hidden-value', 'hidden-nested', 'hidden-answer'))
    assert packet['answers']['work']['answer'] == 'Yes'


def test_packet_refuses_changed_artifacts_and_symlinked_profile(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    write_profile(home)
    (home / 'resumes').mkdir()
    resume = home / 'resumes' / 'resume.pdf'
    resume.write_bytes(b'%PDF-one')
    s = service(home)
    s.register_resume(ids['Backend Engineer'], str(resume))
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    resume.write_bytes(b'%PDF-two')
    with pytest.raises(HandoffError, match='changed'):
        s.packet(token, now=NOW)
    resume.write_bytes(b'%PDF-one')
    outside = home / 'outside.yaml'
    outside.write_text('email: outside@example.com')
    (home / 'profile' / 'profile.yaml').unlink()
    (home / 'profile' / 'profile.yaml').symlink_to(outside)
    with pytest.raises(HandoffError, match='profile'):
        s.packet(token, now=NOW)


def test_accounts_refuse_unknown_sensitive_fields_without_echoing_values(env):
    from auto_app.handoff.service import HandoffError

    home, _ = env
    for extra in ({'secret': 'hidden'}, {'token': 'hidden'}, {'unexpected': {'password': 'hidden'}}):
        with pytest.raises(HandoffError) as exc:
            service(home).record_account('x.com', 'alex@example.com', **extra)
        assert 'hidden' not in str(exc.value) and 'unexpected' not in str(exc.value)


def test_large_artifacts_and_symlinked_parent_are_rejected_before_read(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    (home / 'resumes').mkdir()
    huge = home / 'resumes' / 'huge.pdf'
    with huge.open('wb') as handle:
        handle.truncate(11 * 1024 * 1024)
    with pytest.raises(HandoffError, match='10 MB'):
        service(home).register_resume(ids['Backend Engineer'], str(huge))
    outside = home / 'elsewhere'
    outside.mkdir()
    (outside / 'resume.pdf').write_bytes(b'%PDF-test')
    (home / 'resumes' / 'parent').symlink_to(outside, target_is_directory=True)
    with pytest.raises(HandoffError, match='resumes'):
        service(home).register_resume(ids['Backend Engineer'], str(home / 'resumes' / 'parent' / 'resume.pdf'))


def test_nested_secrets_in_allowlisted_profile_values_are_scrubbed(env):
    home, ids = env
    write_profile(home)
    (home / 'profile' / 'profile.yaml').write_text(PROFILE + '\neducation: [{school: {name: "School", password: "nested-hidden"}}]\n')
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    assert 'nested-hidden' not in json.dumps(s.packet(token, now=NOW))


def test_account_upsert_reports_persisted_creation_state(env):
    home, _ = env
    s = service(home)
    s.record_account('acme.example.com', 'alex@example.com', True)
    assert s.record_account('acme.example.com', 'alex@example.com', False)['created_by_agent'] is True


def test_malformed_profile_never_echoes_sensitive_yaml_values(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    write_profile(home)
    (home / 'profile' / 'profile.yaml').write_text('email: [sensitive-private-value')
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    with pytest.raises(HandoffError) as exc:
        s.packet(token, now=NOW)
    assert 'sensitive-private-value' not in str(exc.value)


def test_evidence_cannot_escape_or_follow_symlinks(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    (home / 'evidence').mkdir()
    outside = home / 'outside.png'
    outside.write_bytes(b'png')
    (home / 'evidence' / 'link.png').symlink_to(outside)
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    for path in (outside, home / 'evidence' / 'link.png'):
        with pytest.raises(HandoffError, match='evidence'):
            s.record_attempt(token, 'submitted', evidence=str(path), now=NOW)
    good = home / 'evidence' / 'confirmation.png'
    good.write_bytes(b'png')
    assert s.record_attempt(token, 'submitted', evidence=str(good), now=NOW)['status'] == 'submitted'


def test_packet_refuses_symlinked_registered_artifact_even_with_same_bytes(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    (home / 'resumes').mkdir()
    resume = home / 'resumes' / 'one.pdf'
    resume.write_bytes(b'%PDF-one')
    s = service(home)
    s.register_resume(ids['Backend Engineer'], str(resume))
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    outside = home / 'outside.pdf'
    outside.write_bytes(resume.read_bytes())
    resume.unlink()
    resume.symlink_to(outside)
    with pytest.raises(HandoffError, match='resumes'):
        s.packet(token, now=NOW)


def test_reregistered_changed_resume_uses_latest_registration_and_keeps_history(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    (home / 'resumes').mkdir()
    resume = home / 'resumes' / 'updated.pdf'
    resume.write_bytes(b'%PDF-original')
    s = service(home)
    old = s.register_resume(ids['Backend Engineer'], str(resume), 'original')
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    resume.write_bytes(b'%PDF-new-version')
    with pytest.raises(HandoffError, match='changed'):
        s.packet(token, now=NOW)
    current = s.register_resume(ids['Backend Engineer'], str(resume), 'updated')
    packet = s.packet(token, now=NOW)
    assert packet['resumes'] == [{
        'artifact_id': current['artifact_id'], 'path': str(resume), 'sha256': current['sha256'],
        'bytes': current['bytes'], 'note': 'updated'}]
    store = make_store(home)
    with store.engine.connect() as conn:
        rows = conn.execute(select(T.resume_artifacts).order_by(T.resume_artifacts.c.artifact_id)).mappings().all()
    store.engine.dispose()
    assert [row['artifact_id'] for row in rows] == [old['artifact_id'], current['artifact_id']]
    assert rows[0]['sha256'] == old['sha256']
    resume.write_bytes(b'%PDF-changed-again-without-registration')
    with pytest.raises(HandoffError, match='changed'):
        s.packet(token, now=NOW)
