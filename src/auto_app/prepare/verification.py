"""Read actual control values, independently of model/tool success claims."""
import re

from auto_app.apply.questions import norm

FIELD_ROLES = {'textbox', 'textarea', 'select', 'combobox', 'listbox', 'checkbox', 'radio', 'switch', 'file'}


READBACK_JS = r'''e => {
  const clean = text => (text || '').replace(/\s+/g, ' ').trim();
  const ariaInvalid = e.getAttribute('aria-invalid');
  const password = e.type === 'password';
  const account = password || /^(?:username|current-password|new-password)$/.test(e.autocomplete || '');
  const result = {
    actual: '', account, selection_confirmed: true,
    invalid: Boolean((e.validity && !e.validity.valid) ||
      (ariaInvalid && ariaInvalid.toLowerCase() !== 'false')),
    validation_message: password ? '' : (e.validationMessage || '').slice(0, 300)
  };
  // Do not return even a partially populated credential value to the caller.
  if (account) return result;
  if (e.type === 'file') result.actual = e.files?.[0]?.name || '';
  else if (e.type === 'checkbox' || e.type === 'radio') result.actual = e.checked ? 'Yes' : 'No';
  else if (e.matches('[role=checkbox], [role=radio], [role=switch]'))
    result.actual = e.getAttribute('aria-checked') === 'true' ? 'Yes' : 'No';
  else if (e.tagName === 'SELECT')
    result.actual = [...e.selectedOptions].filter(o => o.value).map(o => clean(o.textContent)).join(', ');
  else if (e.getAttribute('role') === 'combobox') {
    // Input.value may only be a pending autocomplete search. Confirm semantic
    // selection evidence instead of accepting the typed query as the answer.
    const control = e.closest('[class*=control]') || e.parentElement;
    const displayed = control?.querySelector('[class*="singleValue"], [class*="single-value"], [data-selected-value]');
    const controlledIds = ((e.getAttribute('aria-controls') || '') + ' ' +
                          (e.getAttribute('aria-owns') || '')).trim().split(/\s+/);
    const controlled = controlledIds.map(id => document.getElementById(id)).filter(Boolean);
    const selected = controlled.flatMap(root => [...root.querySelectorAll('[role=option][aria-selected=true]')]);
    const selectedText = selected.length === 1 && e.getAttribute('aria-expanded') !== 'true'
      ? clean(selected[0].textContent) : '';
    result.actual = clean(displayed?.getAttribute('data-selected-value') || displayed?.textContent ||
                          e.getAttribute('aria-valuetext') || selectedText);
    result.selection_confirmed = Boolean(result.actual);
    result.typed_value = typeof e.value === 'string' ? e.value : '';
  } else result.actual = e.value ?? e.textContent ?? '';
  return result;
}'''


def equivalent(expected, actual, label):
    if 'phone' in norm(label):
        return ''.join(c for c in str(expected) if c.isdigit()) == ''.join(c for c in str(actual) if c.isdigit())
    if 'location' in norm(label) or 'payroll tax purposes' in norm(label):
        actual = str(actual).replace('Seattle, Washington, United States', 'Seattle, WA, United States')
    return str(expected).strip() == str(actual).strip()


def field_check(*, label, role, required, expected, actual, source, options=None,
                invalid=False, account=False, upload_verified=False, selection_confirmed=True):
    """Classify observed evidence; matching text cannot override invalid controls."""
    account = account or bool(re.search(r'\b(?:password|username)\b', label, re.I)) or bool(
        re.fullmatch(r'(?:create (?:an? )?)?account(?: name| id| email)?', norm(label)))
    if account:
        actual, expected, source = '', None, None
    empty = actual in ('', None) or (role in ('checkbox', 'switch') and actual == 'No' and expected is None)
    if account:
        status = 'unanswered' if required else 'optional_empty'
    elif invalid:
        status = 'unanswered' if expected is None and required else ('missing' if empty else 'mismatch')
    elif role == 'combobox' and not selection_confirmed:
        status = 'missing' if expected is not None else ('unanswered' if required else 'optional_empty')
    elif expected is not None:
        status = 'matched' if equivalent(expected, actual, label) else ('missing' if empty else 'mismatch')
        if role == 'file' and status == 'matched' and upload_verified is not True:
            status = 'mismatch'
    elif required:
        status = 'unanswered'
    else:
        status = 'optional_empty' if empty else 'unanswered'
    check = {'label': label, 'required': required, 'expected': expected, 'actual': actual,
             'source': source, 'status': status, 'options': options or [], 'invalid': bool(invalid)}
    if account and required:
        check['blocker'] = 'account_required'
    if role == 'file':
        check['upload_verified'] = upload_verified is True
    if role == 'combobox':
        check['selection_confirmed'] = bool(selection_confirmed)
    return check


async def verify_fields(session):
    await session.snapshot()
    checks, missing, groups = [], [], set()
    for ref, item in list(session.items.items()):
        role = item.get('role')
        if role not in FIELD_ROLES or item.get('disabled'):
            continue
        label = session._label(ref)
        required = bool(item.get('required'))
        if not label and not required:
            continue
        label = label or 'Unlabelled required field'
        expected, source = session.expected(label, role)
        state = {}
        invalid = bool(item.get('invalid'))
        if item.get('group') and role == 'radio':
            group = item['group']
            if group in groups:
                continue
            groups.add(group)
            options = [it for it in session.items.values() if it.get('group') == group and it.get('role') == 'radio']
            actual = next((it['name'] for it in options if it.get('checked')), '')
            required = any(it.get('required') for it in options)
            choices = [it['name'] for it in options]
            for option_ref, option in session.items.items():
                if option.get('group') == group and option.get('role') == 'radio':
                    el = await session._el(option_ref)
                    readback = await el.evaluate(READBACK_JS)
                    invalid |= bool(option.get('invalid') or readback['invalid'])
        else:
            el = await session._el(ref)
            state = await el.evaluate(READBACK_JS)
            if (role == 'combobox' and getattr(session, 'location_verified', False)
                    and expected == 'Seattle, WA, United States'
                    and getattr(session, '_selected_options', {}).get(expected) == state.get('typed_value')
                    and state.get('typed_value') == 'Seattle, Washington, United States'
                    and await el.get_attribute('aria-expanded') == 'false'):
                state['actual'] = state['typed_value']
                state['selection_confirmed'] = True
            actual = state['actual']
            invalid |= state['invalid']
            choices = item.get('options') or []
        check = field_check(label=label, role=role, required=required, expected=expected, actual=actual, source=source,
                            options=choices, invalid=invalid, account=bool(state.get('account') or item.get('account_field') or item.get('password')),
                            upload_verified=bool(expected is not None and role == 'file' and getattr(session, 'upload_verified', False)),
                            selection_confirmed=state.get('selection_confirmed', True))
        if role == 'combobox' and state.get('typed_value'):
            check['typed_value'] = state['typed_value']
        if state.get('validation_message'):
            check['validation_message'] = state['validation_message']
        checks.append(check)
        if check['status'] in ('missing', 'mismatch') or (required and check['status'] == 'unanswered'):
            if check.get('blocker'):
                reason = 'Account or credential step requires action; preparation cannot create or access accounts'
            elif invalid:
                reason = 'The form reports this control as invalid'
            elif role == 'file' and check.get('upload_verified') is False:
                reason = 'The resume upload has not been acknowledged'
            elif role == 'combobox' and check.get('selection_confirmed') is False:
                reason = 'Autocomplete text is not a confirmed selection'
            else:
                reason = 'No approved answer' if expected is None else 'Expected value did not match the form'
            missing.append({**check, 'reason': reason})
    return checks, missing
