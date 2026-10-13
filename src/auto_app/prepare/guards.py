"""Preparation-only browser barriers, independent of model instructions."""
import re
from urllib.parse import urlsplit

READ_OPERATIONS = {'ApiJobPosting', 'ApiOrganizationFromHostedJobsPageName', 'ApiAutocompleteGeoLocation'}
BLOCKED_PATH = re.compile(r'(submit|application\.create|applicationformsubmit|register|signup)', re.I)


def request_allowed(method, url, body=None):
    """Allow public page assets and observed Ashby read queries, never mutations."""
    try:
        u = urlsplit(url)
        host = u.hostname or ''
        if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
            return False
    except ValueError:
        return False
    if BLOCKED_PATH.search(u.path):
        return False
    public_asset = host == 'ashbyhq.com' or host.endswith('.ashbyhq.com') or host in {
        'cdn.ashbyprd.com', 'fonts.googleapis.com', 'fonts.gstatic.com', 'www.gstatic.com', 'www.google.com',
        'maps.googleapis.com', 'maps.gstatic.com', 'storage.googleapis.com',
    }
    if method in ('GET', 'HEAD', 'OPTIONS'):
        return public_asset and not (u.path == '/api/non-user-graphql' and method != 'OPTIONS')
    if method != 'POST' or host != 'jobs.ashbyhq.com' or u.path != '/api/non-user-graphql':
        return False
    if not isinstance(body, dict) or body.get('operationName') not in READ_OPERATIONS:
        return False
    query = body.get('query')
    return isinstance(query, str) and bool(re.match(r'^\s*query\s+' + re.escape(body['operationName']) + r'\b', query)) \
        and not re.search(r'\b(mutation|subscription)\b', query)


INIT_GUARD = r'''(() => {
  const blocked = [];
  Object.defineProperty(window, '__preparationBlocked', {value: blocked});
  const deny = (why, event) => { blocked.push(why); if(event) { event.preventDefault(); event.stopImmediatePropagation(); } };
  document.addEventListener('submit', e => deny('form submit', e), true);
  document.addEventListener('keydown', e => {
    if(e.key === 'Enter' && e.target.closest('form')) deny('implicit Enter submit', e);
  }, true);
  document.addEventListener('click', e => {
    const b=e.target.closest('button,input[type=submit],input[type=image],[role=button]');
    if(!b) return;
    const text=(b.innerText || b.value || b.getAttribute('aria-label') || '').trim();
    if((b.form && (b.type === 'submit' || b.type === 'image')) || /submit|send application|complete application|finish application|sign.?up|register|create.*account/i.test(text)) deny('submit/account click', e);
  }, true);
  Object.defineProperty(HTMLFormElement.prototype, 'submit', {value:function(){deny('native form submit');}, configurable:false});
  Object.defineProperty(HTMLFormElement.prototype, 'requestSubmit', {value:function(){deny('native requestSubmit');}, configurable:false});
})();'''
