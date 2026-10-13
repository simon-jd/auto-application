"""Preparation driver configuration and terminal evidence, without remote Claude."""

import asyncio
from pathlib import Path

import claude_agent_sdk
import pytest

from auto_app.apply.profile import Answer, Me, Profile
from auto_app.prepare.candidate import fictional_candidate
from auto_app.prepare.session import PreparationSession


@pytest.fixture
def session(tmp_path):
    candidate = fictional_candidate()
    me = Me(Profile.model_validate(candidate['profile']),
            {k: Answer.model_validate(v) for k, v in candidate['answers'].items()},
            Path(candidate['resume_path']))
    return PreparationSession(page=None, mode='start', app={
        'title': 'TEST ONLY role', 'company': 'Sandbox',
        'apply_url': 'https://jobs.ashbyhq.com/example/11111111-1111-4111-8111-111111111111/application',
    }, me=me, log_path=tmp_path / 'artifacts' / 'actions.jsonl',
       get_password=lambda *args: None, has_password=lambda *args: False)


def result(**kwargs):
    return claude_agent_sdk.ResultMessage(subtype='success', duration_ms=20, duration_api_ms=15,
        is_error=kwargs.pop('is_error', False), num_turns=1, session_id='fixture', **kwargs)


async def test_driver_exposes_only_preparation_tools_and_rejects_submitted(session, monkeypatch):
    from auto_app.prepare.driver import claude_prepare_driver

    created = []
    original_create = claude_agent_sdk.create_sdk_mcp_server

    def capture_server(name, **kwargs):
        created.append({tool.name: tool for tool in kwargs['tools']})
        return original_create(name, **kwargs)

    async def query(*, prompt, options):
        expected_names = {'snapshot', 'click', 'type', 'select', 'check', 'upload_cv', 'press', 'scroll',
                          'wait', 'screenshot', 'answer_for', 'ask_user', 'done'}
        assert set(created[-1]) == expected_names
        assert set(options.allowed_tools) == {f'mcp__form__{name}' for name in expected_names}
        assert options.tools == [] and options.setting_sources == []
        assert options.model == 'sonnet'
        assert options.env == {'ANTHROPIC_API_KEY': ''}
        assert options.cwd == str(session.log_path.parent)
        assert str(session.app['apply_url']) in prompt
        assert options.system_prompt != __import__('auto_app.apply.agent.driver', fromlist=['SYSTEM']).SYSTEM
        done = created[-1]['done']
        assert set(done.input_schema['properties']['status']['enum']) == {
            'ready', 'needs_input', 'action_needed', 'unsupported'}
        rejected = await done.handler({'status': 'submitted'})
        assert rejected.get('isError') is True
        assert session.result.status != 'submitted'
        await done.handler({'status': 'ready', 'note': 'fictional controls checked'})
        yield result(usage={'input_tokens': 3, 'cache_creation_input_tokens': 4, 'cache_read_input_tokens': 5,
                            'output_tokens': 6}, total_cost_usd=0.01)

    monkeypatch.setattr(claude_agent_sdk, 'create_sdk_mcp_server', capture_server)
    monkeypatch.setattr(claude_agent_sdk, 'query', query)
    await claude_prepare_driver(session)
    assert session.result.status == 'ready'
    assert session.tokens == {'input': 12, 'output': 6, 'cost_usd': 0.01}


async def test_sdk_failure_overrides_an_earlier_model_ready_claim(session, monkeypatch):
    from auto_app.prepare.driver import claude_prepare_driver

    async def query(**kwargs):
        await session.done('ready')
        raise RuntimeError('fixture SDK unavailable')
        yield

    monkeypatch.setattr(claude_agent_sdk, 'query', query)
    await claude_prepare_driver(session)
    assert session.result.status == 'action_needed'
    assert 'fixture SDK unavailable' in session.result.note


async def test_error_result_is_not_reported_as_ready(session, monkeypatch):
    from auto_app.prepare.driver import claude_prepare_driver

    async def query(**kwargs):
        await session.done('ready')
        yield result(is_error=True, errors=['fixture authentication failed'], usage={'output_tokens': 2})

    monkeypatch.setattr(claude_agent_sdk, 'query', query)
    await claude_prepare_driver(session)
    assert session.result.status == 'action_needed'
    assert 'fixture authentication failed' in session.result.note
    assert session.tokens['output'] == 2


async def test_stream_without_a_done_call_requires_action(session, monkeypatch):
    from auto_app.prepare.driver import claude_prepare_driver

    async def query(**kwargs):
        yield result()

    monkeypatch.setattr(claude_agent_sdk, 'query', query)
    await claude_prepare_driver(session)
    assert session.result.status == 'action_needed'


async def test_old_ready_status_cannot_replace_this_runs_done_call(session, monkeypatch):
    from auto_app.prepare.driver import claude_prepare_driver

    await session.done('ready', 'a prior invocation')

    async def query(**kwargs):
        yield result()

    monkeypatch.setattr(claude_agent_sdk, 'query', query)
    await claude_prepare_driver(session)
    assert session.result.status == 'action_needed'


async def test_timeout_is_honest_and_cancels_the_sdk_stream(session, monkeypatch):
    from auto_app.prepare.driver import claude_prepare_driver

    cancelled = asyncio.Event()
    session.time_limit_s = 0.02

    async def query(**kwargs):
        try:
            await asyncio.sleep(10)
            yield result()
        finally:
            cancelled.set()

    monkeypatch.setattr(claude_agent_sdk, 'query', query)
    await claude_prepare_driver(session)
    assert cancelled.is_set()
    assert session.result.status == 'action_needed'
    assert 'time' in session.result.note.lower()
