"""Permit only observed Ashby draft writes and a verified fictional PDF upload.

Signed upload fields stay in memory. Reports contain counts and booleans only;
this object never exposes credentials, upload handles or signed destinations.
"""

from email import policy
from email.parser import BytesParser
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

from auto_app.prepare.candidate import validate_run_candidate
from auto_app.prepare.urls import normalize_ashby_url


QUERY_SHA256 = {
    'ApiCreateFileUploadHandle': '512339bdc70b71ab15852e143749296bc8fcb4e36d425f1237dd1554468b7022',
    'ApiSetFormValue': '92e736d6645d95a56dc604468efe7632559b08c350fd470eaf541e39e89f18a7',
    'ApiSetFormValueToFile': 'a6798f9c5e490dcde33477f1f5c25a95672376d6959178224dedbc543df21dc2',
}
UPLOAD_HOST = 'ashbyhq-infra-prd-main-app-uploaded-files-us-east-1.s3.us-east-1.amazonaws.com'
UPLOAD_FIELDS = {'acl', 'bucket', 'X-Amz-Algorithm', 'X-Amz-Credential', 'X-Amz-Date',
                 'X-Amz-Security-Token', 'key', 'Policy', 'X-Amz-Signature'}
RESUME_FIELD = '_systemfield_resume'
LOCATION_FIELD = '_systemfield_location'
SEATTLE_LOCATION = {'text': 'Seattle, Washington, United States',
                    'providerLocationId': 'united_states/washington/seattle'}


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _upload_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == 'https' and parsed.netloc == UPLOAD_HOST and parsed.path == '/'
                and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


class PreparationNetwork:
    """State scoped to one fixed candidate and one normalized Ashby job target."""

    def __init__(self, candidate, target):
        validate_run_candidate(candidate)
        target = normalize_ashby_url(target)
        self._organization, self._job = urlsplit(target).path.strip('/').split('/')[:2]
        self._pdf = Path(candidate['resume_path']).read_bytes()
        self._sha256 = candidate['resume_sha256']
        self._filename = Path(candidate['resume_path']).name
        self._values = {}
        self._resume_bound = False
        self._location_bound = False
        self._location_acknowledged = False
        self._form_identity = None
        self._uploads = {}
        self._pending = {}
        self._uploaded = set()
        self._attached = set()

    async def bind_session(self, session):
        """Bind approved answers to actual DOM field paths before any draft write."""
        await session.snapshot()
        self._values = {}
        self._resume_bound = False
        self._location_bound = False
        conflicts = set()
        for ref, item in session.items.items():
            role = item.get('role')
            if role not in {'textbox', 'textarea', 'select', 'listbox', 'combobox', 'radio', 'checkbox', 'switch', 'file'}:
                continue
            if item.get('disabled') or item.get('account_field') or item.get('password'):
                continue
            label = session._label(ref)
            expected, source = session.expected(label, role)
            if expected is None or not source:
                continue
            try:
                element = await session._el(ref)
                field = await element.evaluate("""e => ({
                    path: e.closest('[data-field-path]')?.getAttribute('data-field-path') || null,
                    type: (e.getAttribute('type') || '').toLowerCase()
                })""")
            except Exception:
                continue
            if not isinstance(field, dict) or field.get('type') == 'password':
                continue
            path = field.get('path')
            if not isinstance(path, str) or not path or len(path) > 250:
                continue
            if path == LOCATION_FIELD:
                # The only observed provider value corresponds exactly to the
                # fixed candidate's Seattle, WA location. A trusted payroll
                # label alias may return that answer without the word location.
                if (expected == 'Seattle, WA, United States'
                        and source.startswith(('fictional_candidate.', 'fictional_answer:'))):
                    self._location_bound = True
                continue
            if 'location' in label.lower() or 'location' in source.lower():
                continue
            if role == 'file':
                if path == RESUME_FIELD and source == 'fictional_candidate.resume':
                    self._resume_bound = True
                continue
            if path in (RESUME_FIELD, LOCATION_FIELD):
                continue
            if path in self._values and self._values[path] != expected:
                conflicts.add(path)
            self._values[path] = expected
        for path in conflicts:
            self._values.pop(path, None)

    def allow_body(self, body):
        """Exact query fingerprints, exact variables, one target and approved value."""
        if not isinstance(body, dict) or set(body) != {'operationName', 'query', 'variables'}:
            return False
        operation = body.get('operationName')
        query = body.get('query')
        variables = body.get('variables')
        if (not isinstance(operation, str) or operation not in QUERY_SHA256
                or not isinstance(query, str) or sha256(query.encode()).hexdigest() != QUERY_SHA256[operation]
                or not isinstance(variables, dict)):
            return False
        if variables.get('organizationHostedJobsPageName') != self._organization:
            return False
        if operation == 'ApiCreateFileUploadHandle':
            expected = {'organizationHostedJobsPageName': self._organization,
                        'fileUploadContext': 'NonUserFormEngine', 'filename': self._filename,
                        'contentType': 'application/pdf', 'contentLength': len(self._pdf)}
            return self._resume_bound and variables == expected and type(variables.get('contentLength')) is int
        value_key = 'fileHandle' if operation == 'ApiSetFormValueToFile' else 'value'
        if set(variables) != {'organizationHostedJobsPageName', 'formRenderIdentifier', 'path', value_key,
                              'formDefinitionIdentifier'}:
            return False
        render = variables.get('formRenderIdentifier')
        if not _uuid(render) or not isinstance(variables.get('formDefinitionIdentifier'), str):
            return False
        try:
            definition = json.loads(variables['formDefinitionIdentifier'])
        except (ValueError, TypeError):
            return False
        if (not isinstance(definition, dict)
                or set(definition) != {'kind', 'formDefinitionId', 'jobPostingId', 'jobBoardSuperType'}
                or definition['kind'] != 'CompositeFormDefinitionId-JobPostingApplicationFormV2'
                or definition['jobPostingId'] != self._job
                or definition['jobBoardSuperType'] != 'External'
                or not _uuid(definition['formDefinitionId'])):
            return False
        identity = (render, definition['formDefinitionId'])
        if self._form_identity is not None and identity != self._form_identity:
            return False
        path, value = variables.get('path'), variables.get(value_key)
        if not isinstance(path, str):
            return False
        if operation == 'ApiSetFormValueToFile':
            allowed = path == RESUME_FIELD and self._resume_bound and isinstance(value, str) and value in self._uploaded
        elif path == RESUME_FIELD:
            allowed = False
        elif path == LOCATION_FIELD:
            allowed = self._location_bound and value == SEATTLE_LOCATION
        else:
            allowed = path in self._values and type(value) is type(self._values[path]) and value == self._values[path]
        if allowed:
            self._form_identity = identity
        return bool(allowed)

    def observe_json(self, body, response):
        """Register an allowed upload response or confirmed resume field assignment."""
        if not self.allow_body(body) or not isinstance(response, dict) or response.get('errors'):
            return False
        data = response.get('data')
        if not isinstance(data, dict):
            return False
        if body['operationName'] in ('ApiSetFormValue', 'ApiSetFormValueToFile'):
            path = body['variables']['path']
            result_key = 'setFormValueToFile' if body['operationName'] == 'ApiSetFormValueToFile' else 'setFormValue'
            if path not in (RESUME_FIELD, LOCATION_FIELD) or not isinstance(data.get(result_key), dict):
                return False
            result = data[result_key]
            if (result.get('errorMessages') or result.get('formErrors')
                    or result.get('id') != body['variables']['formRenderIdentifier']):
                return False
            try:
                if json.loads(result['sourceFormDefinitionId']) != json.loads(body['variables']['formDefinitionIdentifier']):
                    return False
            except (KeyError, TypeError, ValueError):
                return False
            matched = False
            sections = result.get('sections')
            if not isinstance(sections, list):
                return False
            for section in sections:
                entries = section.get('fieldEntries') if isinstance(section, dict) else None
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if (not isinstance(entry, dict) or entry.get('id') != result['id'] + '_' + path
                            or not isinstance(entry.get('field'), dict) or entry['field'].get('path') != path):
                        continue
                    value = entry.get('fieldValue')
                    if not isinstance(value, dict):
                        continue
                    if path == LOCATION_FIELD:
                        matched = value.get('__typename') == 'JSONBox' and value.get('value') == SEATTLE_LOCATION
                        continue
                    files = [value] if value.get('__typename') == 'File' else value.get('files')
                    if value.get('__typename') not in ('File', 'FileList') or not isinstance(files, list):
                        continue
                    matched = len(files) == 1 and isinstance(files[0], dict) and bool(files[0].get('id')) \
                        and files[0].get('filename') == self._filename
            if not matched:
                return False
            if path == LOCATION_FIELD:
                self._location_acknowledged = True
            else:
                self._attached.add(body['variables']['fileHandle'])
            return True
        upload = data.get('fileUploadHandle')
        if not isinstance(upload, dict):
            return False
        handle, url, fields = upload.get('handle'), upload.get('url'), upload.get('fields')
        if (not isinstance(handle, str) or not 1 <= len(handle) <= 1024
                or not _upload_url(url) or not isinstance(fields, dict) or set(fields) != UPLOAD_FIELDS
                or not all(isinstance(v, str) and 0 < len(v) <= 20000 for v in fields.values())
                or any('\r' in v or '\n' in v for v in fields.values())
                or fields['bucket'] != UPLOAD_HOST.split('.s3.')[0]
                or fields['X-Amz-Algorithm'] != 'AWS4-HMAC-SHA256'):
            return False
        record = {'url': url, 'fields': dict(fields)}
        if handle in self._uploads and self._uploads[handle] != record:
            return False
        self._uploads[handle] = record
        return True

    def upload_allowed(self, request):
        """Validate metadata and stage a rebuilt upload containing our fixed PDF.

        Chromium can omit file bytes from intercepted post_data_buffer. An empty
        file part is acceptable only because we never forward that body: callers
        must send upload_payload(url), rebuilt using the immutable package bytes.
        Nonempty browser bytes must still match the package exactly.
        """
        if request.method != 'POST' or not _upload_url(request.url) or request.url in self._pending:
            return False
        content_type = request.headers.get('content-type', '')
        raw = request.post_data_buffer
        if (not isinstance(content_type, str) or '\r' in content_type or '\n' in content_type
                or not isinstance(raw, bytes) or len(raw) > len(self._pdf) + 131072):
            return False
        try:
            message = BytesParser(policy=policy.default).parsebytes(
                b'Content-Type: ' + content_type.encode('ascii') + b'\r\nMIME-Version: 1.0\r\n\r\n' + raw)
            if (message.defects or not message.is_multipart() or message.get_content_type() != 'multipart/form-data'
                    or not message.get_boundary()
                    or not re.fullmatch(r"[A-Za-z0-9'()+_,./:=?-]{1,70}", message.get_boundary())):
                return False
            fields, files = {}, []
            for part in message.iter_parts():
                if (part.defects or part.is_multipart() or part.get_content_disposition() != 'form-data'
                        or len(part.get_all('Content-Disposition', [])) != 1
                        or part.get('Content-Transfer-Encoding') is not None):
                    return False
                name = part.get_param('name', header='content-disposition')
                if not isinstance(name, str):
                    return False
                payload = part.get_payload(decode=True)
                if not isinstance(payload, bytes):
                    return False
                filename = part.get_filename()
                if filename is not None:
                    if name != 'file' or filename != self._filename or part.get_content_type() != 'application/pdf':
                        return False
                    files.append(payload)
                else:
                    if name in fields or name not in UPLOAD_FIELDS | {'Content-Type'}:
                        return False
                    fields[name] = payload.decode('utf-8', errors='strict')
            if len(files) != 1 or (files[0] and (sha256(files[0]).hexdigest() != self._sha256 or files[0] != self._pdf)):
                return False
            if 'Content-Type' in fields and fields['Content-Type'] != 'application/pdf':
                return False
            signed = {key: value for key, value in fields.items() if key != 'Content-Type'}
            matches = [handle for handle, record in self._uploads.items()
                       if record['url'] == request.url and record['fields'] == signed and handle not in self._uploaded]
            if len(matches) != 1:
                return False
            boundary = message.get_boundary()
            pieces = []
            for name, value in fields.items():
                pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{self._filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode())
            pieces.extend([self._pdf, f'\r\n--{boundary}--\r\n'.encode()])
            outbound = b''.join(pieces)
        except (ValueError, TypeError, UnicodeError):
            return False
        self._pending[request.url] = {'handle': matches[0], 'payload': outbound}
        return True

    def upload_payload(self, url):
        """The only body that may be sent for a validated pending upload."""
        pending = self._pending.get(url)
        if pending is None:
            raise ValueError('No validated pending upload for this destination')
        return pending['payload']

    def observe_upload(self, url, status):
        pending = self._pending.pop(url, None)
        if pending is not None and status in (200, 201, 204):
            self._uploaded.add(pending['handle'])

    @property
    def resume_uploaded(self):
        return bool(self._uploaded)

    @property
    def resume_attached(self):
        return bool(self._attached & self._uploaded)

    @property
    def location_verified(self):
        return self._location_bound and self._location_acknowledged

    def evidence(self):
        return {'upload_handles': len(self._uploads), 'storage_uploads': len(self._uploaded),
                'resume_uploaded': self.resume_uploaded, 'resume_attached': self.resume_attached,
                'location_verified': self.location_verified}
