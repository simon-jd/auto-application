"""Exact observed draft/upload requests; all other writes fail closed."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_app.prepare.candidate import fictional_candidate


TARGET = 'https://jobs.ashbyhq.com/ramp/83eec8f4-c37d-43c9-860b-27223eee3643/application'
QUERIES = json.loads((Path(__file__).resolve().parents[2] / 'src/auto_app/prepare/fixtures/ashby-preparation-queries.json').read_text())
UPLOAD_URL = 'https://ashbyhq-infra-prd-main-app-uploaded-files-us-east-1.s3.us-east-1.amazonaws.com/'
SIGNED = {'acl': 'private', 'bucket': 'ashbyhq-infra-prd-main-app-uploaded-files-us-east-1',
          'X-Amz-Algorithm': 'AWS4-HMAC-SHA256', 'X-Amz-Credential': 'fictional', 'X-Amz-Date': '20261011T000000Z',
          'X-Amz-Security-Token': 'fictional', 'key': 'fictional/file.pdf', 'Policy': 'fictional', 'X-Amz-Signature': 'fictional'}


def create_body(candidate):
    return {'operationName': 'ApiCreateFileUploadHandle', 'query': QUERIES['ApiCreateFileUploadHandle'],
            'variables': {'fileUploadContext': 'NonUserFormEngine', 'organizationHostedJobsPageName': 'ramp',
                          'filename': 'fictional-sales-resume.pdf', 'contentType': 'application/pdf',
                          'contentLength': len(Path(candidate['resume_path']).read_bytes())}}


def set_body(path='phone-path', value='+12025550107'):
    return {'operationName': 'ApiSetFormValue', 'query': QUERIES['ApiSetFormValue'],
            'variables': {'organizationHostedJobsPageName': 'ramp', 'formRenderIdentifier': '369314da-221d-4391-931f-7ce3739f9b60',
                          'path': path, 'value': value,
                          'formDefinitionIdentifier': json.dumps({'kind': 'CompositeFormDefinitionId-JobPostingApplicationFormV2',
                            'formDefinitionId': '6b20358a-c9c9-4a19-9507-6d89038d27cf',
                            'jobPostingId': '83eec8f4-c37d-43c9-860b-27223eee3643', 'jobBoardSuperType': 'External'})}}


class Element:
    def __init__(self, path, kind='text'):
        self.path, self.kind = path, kind

    async def evaluate(self, expression):
        return {'path': self.path, 'type': self.kind}


class Session:
    """Browser-boundary fixture: values are provided by the trusted session mapper."""
    def __init__(self):
        self.items = {'phone': {'role': 'textbox'}, 'resume': {'role': 'file'},
                      'password': {'role': 'textbox'}, 'location': {'role': 'combobox'}}
        self.elements = {'phone': Element('phone-path'), 'resume': Element('_systemfield_resume', 'file'),
                         'password': Element('password-path', 'password'), 'location': Element('location-path')}

    async def snapshot(self):
        return ''

    def _label(self, ref):
        return ref

    def expected(self, label, role='text'):
        return {'phone': ('+12025550107', 'fictional_candidate.phone'),
                'resume': ('fictional-sales-resume.pdf', 'fictional_candidate.resume'),
                'password': ('+12025550107', 'fictional_candidate.phone'),
                'location': ('Seattle, WA, United States', 'fictional_candidate.location')}[label]

    async def _el(self, ref):
        return self.elements[ref]


@pytest.fixture
async def network():
    from auto_app.prepare.network import PreparationNetwork
    network = PreparationNetwork(fictional_candidate(), TARGET)
    await network.bind_session(Session())
    return network


def upload_response(url=UPLOAD_URL, fields=None):
    return {'data': {'fileUploadHandle': {'__typename': 'FileUploadHandle', 'handle': 'fictional-upload-handle',
                                        'url': url, 'fields': SIGNED if fields is None else fields}}}


def multipart_request(candidate, *, fields=None, content=None, url=UPLOAD_URL, copies=1, mime='application/pdf'):
    fields = SIGNED if fields is None else fields
    content = Path(candidate['resume_path']).read_bytes() if content is None else content
    boundary = 'fictional-test-boundary'
    pieces = []
    for key, value in fields.items():
        pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for _ in range(copies):
        pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fictional-sales-resume.pdf"\r\nContent-Type: {mime}\r\n\r\n'.encode() + content + b'\r\n')
    pieces.append(f'--{boundary}--\r\n'.encode())
    return SimpleNamespace(method='POST', url=url, headers={'content-type': f'multipart/form-data; boundary={boundary}'},
                           post_data_buffer=b''.join(pieces))


async def test_exact_target_answer_and_upload_context_allowed(network):
    assert network.allow_body(set_body())
    assert network.allow_body(create_body(fictional_candidate()))
    for path in ('unknown', 'password-path', 'location-path'):
        assert not network.allow_body(set_body(path))
    assert not network.allow_body(set_body(value='a real phone'))


@pytest.mark.parametrize('variant', ['alias', 'trailing', 'operation', 'org', 'job', 'render', 'extra', 'context', 'length', 'mime'])
async def test_changed_query_target_and_upload_metadata_fail_closed(network, variant):
    body = create_body(fictional_candidate()) if variant in {'context', 'length', 'mime'} else set_body()
    if variant == 'alias': body['query'] = body['query'].replace('setFormValue(', 'submit: submitApplication(')
    if variant == 'trailing': body['query'] += '\nmutation Evil { submitApplication }'
    if variant == 'operation': body['operationName'] = 'SubmitApplication'
    if variant == 'org': body['variables']['organizationHostedJobsPageName'] = 'another-employer'
    if variant == 'job':
        target = json.loads(body['variables']['formDefinitionIdentifier'])
        target['jobPostingId'] = '11111111-1111-4111-8111-111111111111'
        body['variables']['formDefinitionIdentifier'] = json.dumps(target)
    if variant == 'render': body['variables']['formRenderIdentifier'] = 'not-a-uuid'
    if variant == 'extra': body['variables']['submit'] = True
    if variant == 'context': body['variables']['fileUploadContext'] = 'UserProfile'
    if variant == 'length': body['variables']['contentLength'] += 1
    if variant == 'mime': body['variables']['contentType'] = 'text/plain'
    assert not network.allow_body(body)


async def test_upload_needs_registered_response_exact_multipart_and_success_before_resume(network):
    candidate = fictional_candidate()
    request = multipart_request(candidate)
    assert not network.upload_allowed(request)
    assert not network.resume_uploaded
    assert network.observe_json(create_body(candidate), upload_response())
    assert not network.allow_body(attach_body())
    assert network.upload_allowed(request)
    assert not network.resume_uploaded
    assert not network.upload_allowed(request)  # no ambiguous concurrent upload to the same URL
    network.observe_upload(request.url, 204)
    assert network.resume_uploaded
    assert network.allow_body(attach_body())
    assert not network.allow_body(attach_body(handle='unknown-handle'))
    report = json.dumps(network.evidence())
    for secret in (SIGNED['X-Amz-Credential'], SIGNED['key'], UPLOAD_URL, 'fictional-upload-handle'):
        assert secret not in report


@pytest.mark.parametrize('variant', ['bytes', 'field', 'extra', 'missing', 'url', 'duplicate', 'mime'])
async def test_wrong_file_signed_fields_or_url_are_blocked(network, variant):
    candidate = fictional_candidate()
    assert network.observe_json(create_body(candidate), upload_response())
    options = {}
    if variant == 'bytes': options['content'] = b'%PDF a different resume'
    if variant in {'field', 'extra', 'missing'}:
        fields = dict(SIGNED)
        if variant == 'field': fields['key'] = 'another-file'
        if variant == 'extra': fields['extra'] = 'not-signed'
        if variant == 'missing': del fields['Policy']
        options['fields'] = fields
    if variant == 'url': options['url'] = UPLOAD_URL + '?changed=1'
    if variant == 'duplicate': options['copies'] = 2
    if variant == 'mime': options['mime'] = 'application/octet-stream'
    assert not network.upload_allowed(multipart_request(candidate, **options))
    network.observe_upload(UPLOAD_URL, 204)
    assert not network.resume_uploaded


@pytest.mark.parametrize('url', ['https://127.0.0.1/', 'https://storage.googleapis.com/', 'http://ashbyhq-infra-prd-main-app-uploaded-files-us-east-1.s3.us-east-1.amazonaws.com/', UPLOAD_URL+'x', UPLOAD_URL+'?signed=x'])
async def test_unobserved_upload_destination_rejected(network, url):
    assert not network.observe_json(create_body(fictional_candidate()), upload_response(url))


async def test_failed_upload_never_marks_resume_uploaded(network):
    candidate = fictional_candidate()
    assert network.observe_json(create_body(candidate), upload_response())
    assert network.upload_allowed(multipart_request(candidate))
    network.observe_upload(UPLOAD_URL, 403)
    assert not network.resume_uploaded
    assert not network.allow_body(attach_body())


async def test_resume_attachment_needs_returned_matching_file_entry(network):
    candidate = fictional_candidate()
    assert network.observe_json(create_body(candidate), upload_response())
    assert network.upload_allowed(multipart_request(candidate))
    network.observe_upload(UPLOAD_URL, 204)
    request = attach_body()
    assert not network.observe_json(request, {'data': {'setFormValueToFile': {}}})
    assert not network.resume_attached
    result = {'id': request['variables']['formRenderIdentifier'], 'errorMessages': [], 'formErrors': [],
              'sourceFormDefinitionId': request['variables']['formDefinitionIdentifier'],
              'sections': [{'fieldEntries': [{'id': request['variables']['formRenderIdentifier']+'__systemfield_resume',
                    'field': {'path': '_systemfield_resume'}, 'fieldValue':
                    {'__typename': 'File', 'id': 'stored-fictional-file', 'filename': 'fictional-sales-resume.pdf'}}]}]}
    wrong = copy.deepcopy(result)
    wrong['sections'][0]['fieldEntries'][0]['fieldValue']['filename'] = 'other-resume.pdf'
    assert not network.observe_json(request, {'data': {'setFormValueToFile': wrong}})
    assert not network.resume_attached
    assert network.observe_json(request, {'data': {'setFormValueToFile': result}})
    assert network.resume_attached


async def test_account_field_is_never_bound(network):
    session = Session()
    session.items['phone']['account_field'] = True
    await network.bind_session(session)
    assert not network.allow_body(set_body())


async def test_encoded_multipart_cannot_claim_exact_pdf_bytes(network):
    import base64
    candidate = fictional_candidate()
    assert network.observe_json(create_body(candidate), upload_response())
    request = multipart_request(candidate, content=base64.b64encode(Path(candidate['resume_path']).read_bytes()))
    request.post_data_buffer = request.post_data_buffer.replace(
        b'Content-Type: application/pdf\r\n', b'Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n')
    assert not network.upload_allowed(request)


async def test_observed_seattle_location_needs_binding_exact_value_and_ack(network):
    session = Session()
    session.elements['location'] = Element('_systemfield_location')
    value = {'text': 'Seattle, Washington, United States', 'providerLocationId': 'united_states/washington/seattle'}
    body = set_body('_systemfield_location', value)
    assert not network.allow_body(body)
    await network.bind_session(session)
    assert network.allow_body(body)
    assert not network.allow_body(set_body('_systemfield_location', {**value, 'providerLocationId': 'united_states/new_york/new_york'}))
    assert not network.location_verified
    assert not network.observe_json(body, {'data': {'setFormValue': {}}})
    result = {'id': body['variables']['formRenderIdentifier'], 'errorMessages': [], 'formErrors': [],
              'sourceFormDefinitionId': body['variables']['formDefinitionIdentifier'],
              'sections': [{'fieldEntries': [{'id': body['variables']['formRenderIdentifier']+'__systemfield_location',
                    'field': {'path': '_systemfield_location'}, 'fieldValue':
                    {'__typename': 'JSONBox', 'value': value}}]}]}
    wrong = copy.deepcopy(result)
    wrong['sections'][0]['fieldEntries'][0]['fieldValue']['value']['text'] = 'New York'
    assert not network.observe_json(body, {'data': {'setFormValue': wrong}})
    assert not network.location_verified
    assert network.observe_json(body, {'data': {'setFormValue': result}})
    assert network.location_verified
    assert network.evidence()['location_verified'] is True


async def test_cdp_omitted_file_is_replaced_by_fixed_pdf_in_outbound_payload(network):
    from email import policy
    from email.parser import BytesParser
    candidate = fictional_candidate()
    assert network.observe_json(create_body(candidate), upload_response())
    request = multipart_request(candidate, content=b'', fields={**SIGNED, 'Content-Type': 'application/pdf'})
    assert network.upload_allowed(request)
    payload = network.upload_payload(request.url)
    assert payload != request.post_data_buffer
    parsed = BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\n\r\n' + payload)
    files = [part.get_payload(decode=True) for part in parsed.iter_parts() if part.get_filename() is not None]
    assert len(files) == 1
    assert hashlib.sha256(files[0]).hexdigest() == '37d6949cbb0f4490397180fe65394179cef230d0fbf590c7072d9cff844ed5ed'
    assert files[0] == Path(candidate['resume_path']).read_bytes()
    assert not network.resume_uploaded
    network.observe_upload(request.url, 204)
    assert network.resume_uploaded
    with pytest.raises(ValueError):
        network.upload_payload(request.url)


async def test_multipart_content_type_form_field_must_be_exact_pdf(network):
    candidate = fictional_candidate()
    assert network.observe_json(create_body(candidate), upload_response())
    request = multipart_request(candidate, content=b'', fields={**SIGNED, 'Content-Type': 'text/plain'})
    assert not network.upload_allowed(request)
    with pytest.raises(ValueError):
        network.upload_payload(request.url)


def attach_body(handle='fictional-upload-handle'):
    body = set_body('_systemfield_resume')
    body['operationName'] = 'ApiSetFormValueToFile'
    body['query'] = QUERIES['ApiSetFormValueToFile']
    del body['variables']['value']
    body['variables']['fileHandle'] = handle
    return body


async def test_observed_attach_mutation_and_prefixed_field_identity(network):
    candidate = fictional_candidate()
    assert network.observe_json(create_body(candidate), upload_response())
    assert network.upload_allowed(multipart_request(candidate))
    network.observe_upload(UPLOAD_URL, 204)
    body = attach_body()
    assert network.allow_body(body)
    assert not network.allow_body(set_body('_systemfield_resume', 'fictional-upload-handle'))
    result = {'id': body['variables']['formRenderIdentifier'], 'errorMessages': [], 'formErrors': [],
              'sourceFormDefinitionId': body['variables']['formDefinitionIdentifier'],
              'sections': [{'fieldEntries': [{'id': body['variables']['formRenderIdentifier']+'__systemfield_resume',
                    'field': {'path': '_systemfield_resume'}, 'fieldValue':
                    {'__typename': 'File', 'id': 'stored-fictional-file', 'filename': 'fictional-sales-resume.pdf'}}]}]}
    assert network.observe_json(body, {'data': {'setFormValueToFile': result}})
    assert network.resume_attached


async def test_payroll_location_alias_binds_only_actual_location_path(network):
    class PayrollSession(Session):
        def _label(self, ref):
            return 'Where do you plan on working from (for payroll tax purposes)?' if ref == 'location' else ref

        def expected(self, label, role='text'):
            if label.startswith('Where do you plan'):
                return 'Seattle, WA, United States', 'fictional_candidate.where do you plan on working from for payroll tax purposes'
            return super().expected(label, role)

    session = PayrollSession()
    session.elements['location'] = Element('_systemfield_location')
    await network.bind_session(session)
    assert network.allow_body(set_body('_systemfield_location', {
        'text': 'Seattle, Washington, United States', 'providerLocationId': 'united_states/washington/seattle'}))


@pytest.mark.parametrize('variant', ['alias', 'handle', 'path', 'job', 'org', 'extra'])
async def test_attach_is_limited_to_uploaded_handle_and_same_resume_target(network, variant):
    candidate = fictional_candidate()
    assert network.observe_json(create_body(candidate), upload_response())
    assert network.upload_allowed(multipart_request(candidate))
    network.observe_upload(UPLOAD_URL, 204)
    body = attach_body()
    if variant == 'alias': body['query'] = body['query'].replace('setFormValueToFile(', 'submit: submitApplication(')
    if variant == 'handle': body['variables']['fileHandle'] = 'not-uploaded'
    if variant == 'path': body['variables']['path'] = 'phone-path'
    if variant == 'job':
        target = json.loads(body['variables']['formDefinitionIdentifier'])
        target['jobPostingId'] = '11111111-1111-4111-8111-111111111111'
        body['variables']['formDefinitionIdentifier'] = json.dumps(target)
    if variant == 'org': body['variables']['organizationHostedJobsPageName'] = 'other'
    if variant == 'extra': body['variables']['submit'] = True
    assert not network.allow_body(body)
