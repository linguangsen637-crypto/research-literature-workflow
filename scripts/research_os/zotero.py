"""GET-only Zotero 10 adapter. No connector writes or authorization requests."""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from .core import SafetyError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SafetyError('API redirect rejected')


class SandboxLoopbackProxy(urllib.request.ProxyHandler):
    """Honor Codex's filtering proxy even when NO_PROXY includes loopback."""
    def __init__(self, proxy):
        parsed = urllib.parse.urlsplit(proxy)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
                or not parsed.port or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise SafetyError('invalid Codex network proxy')
        self.hostport = parsed.netloc
        super().__init__({'http': proxy})

    def proxy_open(self, req, proxy, type):
        parsed = urllib.parse.urlsplit(req.full_url)
        if (parsed.scheme, parsed.hostname, parsed.port) != ('http', '127.0.0.1', 23119):
            raise SafetyError('Zotero proxy destination rejected')
        req.set_proxy(self.hostport, 'http')
        return None


def zotero_opener():
    active = os.environ.get('CODEX_NETWORK_PROXY_ACTIVE', '').lower() in ('1', 'true')
    if active:
        proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
        if not proxy:
            raise SafetyError('Codex network proxy active but HTTP proxy missing')
        handler = SandboxLoopbackProxy(proxy)
    else:
        # Direct host execution must never route Zotero to an upstream proxy.
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler, NoRedirect())


class Zotero:
    def __init__(self, config):
        self.config = config
        self.base = config['api']['base_url']
        parsed = urllib.parse.urlsplit(self.base)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or parsed.port != 23119 or parsed.path != '/api/'
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise SafetyError('only the configured loopback API is allowed')
        self.opener = zotero_opener()
        self.server_id = config['api']['expected_server_id']
        if (not self.server_id or self.server_id.startswith('REPLACE_')
                or config['library']['type'] not in ('user', 'group')
                or isinstance(config['library']['id'], bool)
                or not isinstance(config['library']['id'], int) or config['library']['id'] <= 0):
            raise SafetyError('configure the actual local server and library identity first')
        self.prefix = f"{'users' if config['library']['type'] == 'user' else 'groups'}/{int(config['library']['id'])}/"

    def get(self, route=''):
        if route.startswith('/') or '..' in route or ':' in route:
            raise SafetyError('invalid API route')
        request = urllib.request.Request(self.base + route, method='GET', headers={
            'Zotero-API-Version': '3', 'Zotero-Server-ID': self.server_id})
        try:
            with self.opener.open(request, timeout=10) as response:
                if response.headers.get('Zotero-Server-ID') != self.server_id:
                    raise SafetyError('Zotero instance changed; discard version partition')
                if response.headers.get('Zotero-API-Version') != '3':
                    raise SafetyError('unsupported Zotero API version')
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise SafetyError(f'Zotero GET rejected: HTTP {error.code}') from None
        except urllib.error.URLError as error:
            reason = error.reason
            label = type(reason).__name__
            code = getattr(reason, 'errno', None)
            raise SafetyError(f'Zotero GET unavailable: {label}, errno={code}; '
                              'run scripts/research_preflight.py') from None

    def listing(self, route):
        result, start = [], 0
        while True:
            separator = '&' if '?' in route else '?'
            page = self.get(self.prefix + route + f'{separator}limit=100&start={start}')
            if not isinstance(page, list):
                raise SafetyError('unexpected API list response')
            result.extend(page)
            if len(page) < 100:
                return result
            start += 100

    def collection(self):
        matches = [x for x in self.listing('collections')
                   if x['data']['name'] == self.config['collection']['name']]
        if len(matches) != 1:
            raise SafetyError('collection missing or ambiguous; user confirmation required')
        match = matches[0]
        if (match['key'] != self.config['collection']['key']
                or match['library']['type'] != self.config['library']['type']
                or match['library']['id'] != self.config['library']['id']):
            raise SafetyError('collection/library identity mismatch')
        return match

    def items(self):
        collection = self.collection()
        return self.listing(f"collections/{collection['key']}/items")

    def paper(self, key):
        if not re.fullmatch(r'[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}', key):
            raise SafetyError('invalid item key')
        items = self.items()
        matches = [x for x in items if x['key'] == key and
                   x['data']['itemType'] not in ('note', 'attachment')]
        if len(matches) != 1:
            raise SafetyError('paper not found in target collection')
        return matches[0]


def doi(value):
    text = value.strip().lower()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if not re.fullmatch(r'10\.\d{4,9}/\S+', text):
        raise SafetyError('invalid DOI syntax')
    return text


def duplicate_candidates(value, items):
    """DOI agreement is a candidate, never permission to merge publication versions."""
    identity = doi(value)
    return [{'key': x['key'], 'title': x['data'].get('title'),
             'reason': 'same-doi-review-version', 'action': 'review_existing'}
            for x in items if x['data'].get('DOI') and doi(x['data']['DOI']) == identity]
