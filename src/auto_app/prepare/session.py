"""The existing browser tools restricted to one fictional preparation package."""
import json
import re
from pathlib import Path

from auto_app.apply.agent.session import AgentSession
from auto_app.apply.agent.snapshot import render
from auto_app.apply.questions import norm


OPTION_BELONGS_TO_CONTROL = r'''(option, control) => {
  if (!control?.isConnected || !option.isConnected || control.ownerDocument !== option.ownerDocument) return false;
  const ids = [control.getAttribute('aria-controls'), control.getAttribute('aria-owns')]
    .filter(Boolean).join(' ').split(/\s+/).filter(Boolean);
  return ids.some(id => control.ownerDocument.getElementById(id)?.contains(option));
}'''


class PreparationSession(AgentSession):
    def __post_init__(self):
        super().__post_init__()
        self.refusals = []
        self._active_combobox = None
        self._selected_options = {}

    def _refuse(self, reason):
        self.refusals.append(reason)
        return f'refused: {reason}'

    async def _auth_form(self, el):
        # Formless pages have a Document fallback, which has no innerText.
        return await el.evaluate("""e => {
          const doc = e.ownerDocument;
          const root = e.closest('form,[role=dialog],main') || doc.body || doc.documentElement;
          const fields = [...root.querySelectorAll('input:not([type=hidden]):not([type=checkbox])')];
          if (root.querySelector('input[type=password]')) return fields.length <= 5;
          const email = fields.some(i => i.type === 'email' || /mail|user/i.test(i.name + i.id + i.autocomplete));
          return fields.length <= 2 && email && /sign.?in|log.?in|create (an )?account|sign.?up|register/i.test(
              doc.title + ' ' + (root.innerText || '').slice(0, 2000));
        }""")

    async def snapshot(self):
        text = await super().snapshot()
        for ref, item in self.items.items():
            el = await self._el(ref)
            details = await el.evaluate('''e => {
              const entry = e.closest('.ashby-application-form-field-entry');
              const labels = entry ? [...entry.querySelectorAll('label')] : [];
              const primary = entry?.querySelector('input:not([type=hidden]),textarea,select,[role=combobox]');
              const required = e === primary && labels.some(label => [...label.classList].some(c => /(^|[_-])required([_-]|$)/i.test(c)));
              return {required: !!e.required || e.getAttribute('aria-required') === 'true' || required,
                      password: e.type === 'password'};
            }''')
            item['required'] = bool(item.get('required') or details['required'])
            item['password'] = details['password']
            item['account_field'] = details['password'] or await self._auth_form(el)
        # Include enriched required markers in the model's view as well as readback.
        if self.items:
            metadata = render([{'index': 0, 'url': self.page.url, 'items': list(self.items.values())}])
            return text + '\nPreparation control metadata:\n' + metadata[:30000]
        return text

    def expected(self, label, role='text'):
        label = norm(label)
        p = self.me.profile.model_dump()
        name = p['name']
        location = p.get('location') or {}
        if isinstance(location, dict):
            location = ', '.join(str(location[k]) for k in ('city', 'state', 'country') if location.get(k))
        direct = {
            'name': f"{name['first']} {name['last']}", 'full name': f"{name['first']} {name['last']}",
            'legal name': f"{name['first']} {name['last']}", 'first name': name['first'], 'last name': name['last'],
            'legal first name': name['first'], 'legal last name': name['last'],
            'email': p['email'], 'email address': p['email'], 'phone': p.get('phone'),
            'phone number': p.get('phone'), 'mobile phone': p.get('phone'),
            'linkedin': p.get('links', {}).get('linkedin'), 'linkedin profile': p.get('links', {}).get('linkedin'),
            'linkedin url': p.get('links', {}).get('linkedin'),
            'location': location, 'current location': location, 'working location': location,
            'where do you plan on working from for payroll tax purposes': location,
        }
        if role == 'file':
            if re.fullmatch(r'(?:(?:upload|attach) )?(?:your )?(?:resume|cv|curriculum vitae)(?: (?:or )?(?:resume|cv))?', label):
                return Path(self.me.cv).name, 'fictional_candidate.resume'
            return None, None
        if label in direct and direct[label]:
            return str(direct[label]), f'fictional_candidate.{label}'
        for key, answer in self.me.answers.items():
            if norm(answer.question) == label:
                value = 'Yes' if answer.answer is True else 'No' if answer.answer is False else str(answer.answer)
                return value, f'fictional_answer:{key}'
        return None, None

    async def answer_for(self, fields):
        result = []
        for f in fields:
            label = (f.get('label') or '').rstrip('* ').strip()
            value, source = self.expected(label, f.get('type', 'text'))
            if value is not None:
                self.answers[norm(label)] = (value, source)
                result.append({'label': label, 'answer': value, 'source': source})
            else:
                result.append({'label': label, 'answer': None, 'instruction': 'Leave empty; report as missing if required.'})
        return json.dumps(result)

    def _guard_value(self, ref, value):
        item = self.items.get(ref, {})
        expected, _ = self.expected(self._label(ref), item.get('role', 'text'))
        if expected is None or str(value).strip() != str(expected).strip():
            return 'refused: only the exact approved fictional answer may be entered'
        return None

    async def _field(self, ref, roles):
        item = self.items.get(ref, {})
        if item.get('role') not in roles or item.get('disabled'):
            return None, self._refuse('unsupported or disabled field control')
        el = await self._el(ref)
        if (item.get('password') or item.get('account_field')
                or await el.evaluate("e => e.type === 'password'") or await self._auth_form(el)):
            return None, self._refuse('password and account fields are unavailable in preparation')
        return el, None

    async def type(self, ref, text):
        el, refusal = await self._field(ref, {'textbox', 'textarea', 'combobox'})
        if refusal:
            return refusal
        if self.items[ref].get('role') == 'combobox':
            if why := self._guard_value(ref, text):
                return self._refuse(why.removeprefix('refused: '))
            self._active_combobox = (await el.element_handle(), str(text).strip())
        return await super().type(ref, text)

    async def select(self, ref, option):
        el, refusal = await self._field(ref, {'select', 'listbox'})
        if refusal:
            return refusal
        if why := self._guard_value(ref, option):
            return self._refuse(why.removeprefix('refused: '))
        if not await el.evaluate("e => e.tagName === 'SELECT'"):
            return self._refuse('only a native select is supported by select')
        labels = await el.evaluate("e => [...e.options].map(o => o.textContent.trim())")
        if labels.count(option) != 1:
            return self._refuse('no unique option exactly matches the approved answer')
        await el.select_option(label=option, timeout=5000)
        self._record(ref, option)
        return 'selected exact approved option'

    async def check(self, ref, on=True):
        el, refusal = await self._field(ref, {'checkbox', 'radio', 'switch'})
        if refusal:
            return refusal
        item = self.items[ref]
        if item.get('role') == 'radio':
            if not on:
                return self._refuse('radio options may only select their approved answer')
            value = item.get('name')
        else:
            value = 'Yes' if on else 'No'
        if why := self._guard_value(ref, value):
            return self._refuse(why.removeprefix('refused: '))
        # set_checked has no generic click fallback that could invoke another control.
        await el.set_checked(on, timeout=5000)
        self._record(ref, value)
        return 'checked' if on else 'unchecked'

    async def upload_cv(self, ref):
        el, refusal = await self._field(ref, {'file'})
        if refusal:
            return refusal
        expected, _ = self.expected(self._label(ref), 'file')
        if expected is None or not await el.evaluate("e => e.tagName === 'INPUT' && e.type === 'file'"):
            return self._refuse('only the actual Resume or CV file input may receive the fictional resume')
        await el.set_input_files(str(self.me.cv), timeout=5000)
        await self.page.wait_for_timeout(1500)
        self._record(ref, expected, 'fictional_candidate.resume')
        return 'fictional resume selected; upload acceptance requires final verification'

    @staticmethod
    def option_value(value):
        # One fixture-specific state spelling equivalence; never fuzzy match arbitrary answers.
        return value.replace('Seattle, Washington, United States', 'Seattle, WA, United States')

    async def choose_option(self, ref, answer):
        """Select one approved custom option within its control's declared listbox."""
        el, refusal = await self._field(ref, {'combobox'})
        if refusal:
            return refusal
        if why := self._guard_value(ref, answer):
            return self._refuse(why.removeprefix('refused: '))
        if await el.evaluate("e => e.tagName === 'INPUT'"):
            refusal = await self.type(ref, answer)
        else:
            refusal = await self.click(ref)
        if refusal.startswith('refused:'):
            return refusal
        await self.page.wait_for_timeout(800)
        await self.snapshot()
        candidates = []
        for option_ref, item in self.items.items():
            if item.get('role') == 'option' and self.option_value(item.get('name', '')) == self.option_value(answer):
                option = await self._el(option_ref)
                if await option.evaluate(OPTION_BELONGS_TO_CONTROL, self._active_combobox[0]):
                    candidates.append(option_ref)
        if len(candidates) != 1:
            return self._refuse('no unique option belongs to the approved field')
        return await self.click(candidates[0])

    async def click(self, ref):
        item = self.items.get(ref, {})
        el = await self._el(ref)
        name = item.get('name') or ''
        role = item.get('role') or ''
        if re.search(r'submit|next|continue|proceed|finish|complete|send|sign.?in|sign.?up|register|account', name, re.I):
            self.refusals.append(name)
            return 'refused: preparation cannot submit, advance steps, or use accounts'
        if role in ('checkbox', 'radio', 'switch'):
            return await self.check(ref)
        if role == 'option':
            active = self._active_combobox
            if (not active or self.option_value(name) != self.option_value(active[1])
                    or not await el.evaluate(OPTION_BELONGS_TO_CONTROL, active[0])):
                return self._refuse('option does not belong to its approved field')
        elif role == 'combobox':
            el, refusal = await self._field(ref, {'combobox'})
            if refusal:
                return refusal
            expected, _ = self.expected(self._label(ref), role)
            if expected is None:
                return self._refuse('unknown fields cannot be opened for answering')
            self._active_combobox = (await el.element_handle(), str(expected).strip())
        elif role == 'button':
            if await el.evaluate("e => e.type === 'submit' || e.type === 'image'"):
                return 'refused: a form submission control'
            if not re.fullmatch(r'application|apply|apply now', name, re.I):
                return 'refused: unsupported button in preparation mode'
        elif role == 'tab':
            if not re.fullmatch(r'application|apply|apply now', name, re.I):
                return self._refuse('only the application tab may be opened')
        else:
            return 'refused: preparation only allows application tabs and field controls'
        await el.click(timeout=5000)
        if role == 'option':
            self._selected_options[active[1]] = name
        await self.page.wait_for_timeout(300)
        return 'clicked; take a new snapshot'

    async def press(self, key):
        if key not in ('Tab', 'Escape'):
            return self._refuse('key may submit or change a field without an approved answer')
        return await super().press(key)

    async def account_info(self):
        return 'refused: account access is unavailable in fictional preparation mode'

    async def fill_secret(self, ref, what):
        return 'refused: preparation has no credentials'
