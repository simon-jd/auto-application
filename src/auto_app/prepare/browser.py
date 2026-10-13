"""Ashby prepare-only browser executor; outputs evidence, never a submission."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from auto_app.apply.profile import Answer, Me, Profile
from auto_app.apply.questions import norm
from auto_app.core.browser_env import browser_env
from auto_app.prepare.guards import INIT_GUARD, request_allowed
from auto_app.prepare.session import PreparationSession
from auto_app.prepare.network import PreparationNetwork
from auto_app.prepare.verification import FIELD_ROLES, verify_fields


async def prefill(session):
    await session.snapshot()
    fields = [(session._label(ref), item.get('role'), item.get('name')) for ref, item in session.items.items() if item.get('role') in FIELD_ROLES]
    fields.sort(key=lambda field: field[1] == 'combobox')
    for label, role, option_name in fields:
        await session.snapshot()
        matches = [(r, it) for r, it in session.items.items() if it.get('role') == role and session._label(r) == label
                   and (role != 'radio' or it.get('name') == option_name)]
        if not matches:
            continue
        ref, item = matches[0]
        if role not in FIELD_ROLES or item.get('disabled'):
            continue
        label = session._label(ref)
        answer, source = session.expected(label, role)
        if answer is None:
            continue
        session.answers[norm(label)] = (answer, source)
        try:
            if role == 'file':
                await session.upload_cv(ref)
            elif role in ('textbox', 'textarea'):
                await session.type(ref, answer)
            elif role in ('select', 'listbox'):
                await session.select(ref, answer)
            elif role == 'radio':
                if norm(item.get('name', '')) == norm(answer):
                    await session.check(ref, True)
            elif role in ('checkbox', 'switch'):
                await session.check(ref, norm(answer) in ('yes', 'true'))
            elif role == 'combobox':
                await session.choose_option(ref, answer)
        except Exception as exc:
            # Final readback is authoritative; a failed field never aborts all other fields.
            session._log('prefill_error', {'label': label}, type(exc).__name__)


async def fill_ashby(run, artifact_dir, *, driver=None, browser_channel=None, route=None):
    from playwright.async_api import async_playwright
    from auto_app.prepare.urls import normalize_ashby_url

    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate = run['candidate']
    result = {'status': 'failed', 'test_only': True, 'candidate_id': candidate.get('candidate_id'),
              'candidate_hash': candidate.get('package_hash'), 'resume_sha256': candidate.get('resume_sha256'),
              'submission_allowed': False, 'browser_session_retained': False,
              'field_checks': [], 'missing_questions': [], 'screenshot': None, 'usage': {},
              'guard': {'blocked_requests': [], 'blocked_actions': []}}
    try:
        target = normalize_ashby_url(run['apply_url'])
        from auto_app.prepare.candidate import validate_run_candidate
        validate_run_candidate(candidate)
    except (OSError, ValueError) as exc:
        result['note'] = str(exc)
        return result
    async with async_playwright() as pw:
        browser = None
        try:
            options = {'headless': True, 'env': browser_env()}
            if browser_channel:
                browser = await pw.chromium.launch(channel=browser_channel, **options)
            else:
                try:
                    browser = await pw.chromium.launch(channel='chrome', **options)
                except Exception:
                    browser = await pw.chromium.launch(**options)
            context = await browser.new_context(viewport={'width': 1280, 'height': 1000}, service_workers='block')
            test_transport = await route(context) if route else None
            await context.add_init_script(INIT_GUARD)

            network = PreparationNetwork(candidate, target)

            async def barrier(r):
                req = r.request
                try:
                    body = req.post_data_json if req.method == 'POST' else None
                except Exception:
                    body = None
                prep_mutation = network.allow_body(body) if isinstance(body, dict) and req.url.split('?')[0] == 'https://jobs.ashbyhq.com/api/non-user-graphql' and req.method == 'POST' else False
                upload = network.upload_allowed(req)
                allowed = request_allowed(req.method, req.url, body) or prep_mutation or upload
                if req.is_navigation_request() and req.resource_type == 'document':
                    # No redirects to another employer/job, account, or third-party destination.
                    try:
                        allowed = allowed and normalize_ashby_url(req.url) == target
                    except ValueError:
                        allowed = False
                if not allowed:
                    result['guard']['blocked_requests'].append({'method': req.method,
                                                               'url': req.url.split('?')[0],
                                                               'operation': body.get('operationName') if isinstance(body, dict) else None})
                    await r.abort()
                else:
                    # Playwright does not route redirect hops. Fetch every allowed
                    # response without following redirects, then inspect before fulfil.
                    if test_transport:
                        response = await test_transport(req)
                        if 300 <= response.get('status', 200) < 400:
                            result['guard']['blocked_actions'].append('redirect blocked')
                            await r.abort()
                        else:
                            await r.fulfill(**response)
                        return
                    response = await r.fetch(max_redirects=0, **({'post_data': network.upload_payload(req.url),
                        'headers': {k: v for k, v in req.headers.items() if k.lower() != 'content-length'}} if upload else {}))
                    if 300 <= response.status < 400:
                        result['guard']['blocked_actions'].append('redirect blocked')
                        await r.abort()
                        return
                    if prep_mutation:
                        try:
                            network.observe_json(body, await response.json())
                        except (ValueError, TypeError):
                            pass
                    if upload:
                        network.observe_upload(req.url, response.status)
                    await r.fulfill(response=response)
            await context.route('**/*', barrier)
            page = await context.new_page()
            me = Me(Profile.model_validate(candidate['profile']),
                    {k: Answer.model_validate(a) for k, a in candidate['answers'].items()},
                    Path(candidate['resume_path']))
            session = PreparationSession(page=page, mode='start', app=run, me=me, matcher=None,
                                         time_limit_s=120, log_path=artifact_dir / 'actions.jsonl',
                                         get_password=lambda *a: None, has_password=lambda *a: False)
            await page.goto(target, wait_until='domcontentloaded', timeout=45000)
            try:
                await page.locator('input:not([type=hidden]),textarea,select').first.wait_for(state='attached', timeout=15000)
            except Exception:
                pass
            await session.snapshot()
            await network.bind_session(session)
            await prefill(session)
            if driver == 'claude':
                from auto_app.prepare.driver import claude_prepare_driver
                await claude_prepare_driver(session, model='sonnet')
            elif callable(driver):
                await driver(session)
            await page.wait_for_timeout(1000)
            session.upload_verified = network.resume_attached
            session.location_verified = getattr(network, 'location_verified', False)
            checks, missing = await verify_fields(session)
            missing_labels = {norm(q['label']) for q in missing}
            for question in session.asked.values():
                if norm(question.label) not in missing_labels:
                    missing.append({'label': question.label, 'required': question.required,
                                    'options': [o['label'] for o in question.options],
                                    'reason': question.note or 'The model requested an answer from the user',
                                    'source': 'ask_user'})
                    missing_labels.add(norm(question.label))
            result['upload_evidence'] = network.evidence()
            if getattr(session, 'driver_error', None):
                result['driver_error'] = session.driver_error
            result.update(field_checks=checks, missing_questions=missing, usage=session.tokens,
                          observed_url=page.url, observed_title=await page.title())
            result['guard']['blocked_actions'] += session.refusals + (await page.evaluate('window.__preparationBlocked || []'))
            try:
                await page.screenshot(path=str(artifact_dir / 'filled.png'), full_page=True, timeout=10000)
                result['screenshot'] = 'filled.png'
            except Exception:
                result['screenshot'] = None
            final_controls = [it for it in session.items.values() if it.get('role') == 'button'
                              and re.search(r'submit|send application|complete application', it.get('name', ''), re.I)]
            if any(c.get('blocker') == 'account_required' for c in checks):
                result.update(status='action_needed', note='An account or password field requires user action; preparation stopped.')
            elif not checks or not final_controls:
                result.update(status='action_needed', note='No complete supported Ashby application form was verified.')
            elif missing or session.result.status == 'needs_input':
                result.update(status='needs_input', note=session.result.note or
                              'Known fictional values were filled; required answers or value corrections remain.')
            elif any(c['status'] == 'mismatch' for c in checks):
                result.update(status='needs_input', note='A field did not match its approved value.')
            elif not any(not it.get('disabled') for it in final_controls):
                result.update(status='action_needed', note='The final form control is disabled; readiness was not verified.')
            elif result['guard']['blocked_actions']:
                result.update(status='action_needed', note='A prohibited operation was blocked; review the guard evidence.')
            else:
                result.update(status='ready_for_review', note='Fictional values read back successfully; stopped before submit. Browser closed; this is a preparation report, not a retained draft session.')
            if session.result.status in ('timeout', 'action_needed', 'unsupported'):
                result.update(status='action_needed', note=session.result.note or session.result.status)
            result['model_status'] = session.result.status if driver else 'not_used'
        except Exception as exc:
            result.update(status='failed', note=f'Preparation failed: {type(exc).__name__}: {str(exc).splitlines()[0][:220]}')
        finally:
            if browser:
                await browser.close()
    (artifact_dir / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
