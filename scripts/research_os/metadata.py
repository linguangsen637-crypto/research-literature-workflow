"""Deterministic, source-attributed metadata resolution; no Zotero mutation."""
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from .core import SafetyError, atomic, canonical, digest, now
from .zotero import doi

HOSTS = {'api.crossref.org', 'arxiv.org', 'export.arxiv.org'}
RESOLVER_VERSION = 'crossref-arxiv-v1'
ARXIV = re.compile(r'(?P<base>\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v(?P<version>\d+))?$', re.I)


def arxiv_id(value):
    value = re.sub(r'^arxiv:\s*', '', value.strip(), flags=re.I)
    value = re.sub(r'^https?://(?:export\.)?arxiv.org/(?:abs|pdf)/', '', value)
    value = value.removesuffix('.pdf')
    match = ARXIV.fullmatch(value)
    if not match:
        raise SafetyError('invalid arXiv identifier')
    return match.group('base'), int(match.group('version')) if match.group('version') else None


def configured_hosts():
    from .settings import WORKSPACE, config
    extra = config('research').get('sources', {}).get('https_hosts', []) if (WORKSPACE / 'config/research.yaml').exists() else []
    if not isinstance(extra, list) or any(not isinstance(host, str) or
            not re.fullmatch(r'[a-z0-9]+(?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9]+(?:[-a-z0-9]*[a-z0-9])?)+', host)
            for host in extra):
        raise SafetyError('sources.https_hosts requires explicit lowercase hostnames')
    return HOSTS | set(extra)


def safe_url(url, hosts=None):
    hosts = configured_hosts() if hosts is None else hosts
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in hosts or parsed.username
            or parsed.password or parsed.port not in (None, 443)):
        raise SafetyError('source URL is outside the approved HTTPS host allowlist')
    # Validate public destinations even when a standard environment proxy is used.
    for answer in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(answer[4][0]).is_global:
            raise SafetyError('source resolves to a non-public address')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, max_bytes=8 * 1024 * 1024):
    safe_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(), Redirect())
    req = urllib.request.Request(url, headers={'User-Agent': 'Research-Workflow/0.3 (personal research)',
                                              'Accept': 'application/json,application/atom+xml,application/pdf'})
    for attempt in range(3):
        try:
            with opener.open(req, timeout=30) as response:
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise SafetyError('source exceeds configured size limit')
                return data
        except urllib.error.HTTPError as error:
            if error.code in (429, 503) and attempt < 2:
                try:
                    delay = min(10, max(1, int(error.headers.get('Retry-After', '3'))))
                except ValueError:
                    delay = 3
                time.sleep(delay)
                continue
            raise SafetyError(f'metadata/PDF request failed: HTTP {error.code}') from None
        except urllib.error.URLError:
            raise SafetyError('metadata/PDF source is unreachable') from None


def resolve(value, cache_root=None, force=False):
    try:
        identifier = doi(value)
        kind = 'doi'
    except SafetyError:
        base, version = arxiv_id(value)
        identifier, kind = base + (f'v{version}' if version else ''), 'arxiv'
    cache = Path(cache_root).absolute() / (digest(RESOLVER_VERSION + ':' + kind + ':' + identifier) + '.json') if cache_root else None
    if cache and cache.exists() and not force:
        saved = json.loads(cache.read_text())
        age = time.time() - cache.stat().st_mtime
        if age < 86400 and 'payload' in saved:
            payload = saved['payload']
            if (saved.get('sha256') != digest(canonical(payload)) or
                    digest(saved.get('source_raw', '')) != payload['sources'][0]['sha256']):
                raise SafetyError('metadata cache integrity mismatch')
            return {**payload, 'cache_hit': True}
    if kind == 'doi':
        url = 'https://api.crossref.org/works/' + urllib.parse.quote(identifier, safe='')
        raw = fetch(url)
        m = json.loads(raw)['message']
        if doi(m['DOI']) != identifier:
            raise SafetyError('resolved DOI differs from requested identity')
        date = next((m[k]['date-parts'][0] for k in ['published', 'published-print', 'published-online', 'issued']
                     if m.get(k, {}).get('date-parts')), [])
        fields = {'title': (m.get('title') or [''])[0], 'authors': m.get('author', []),
                  'year': date[0] if date else None, 'venue': (m.get('container-title') or [''])[0],
                  'doi': identifier, 'arxiv_id': None, 'arxiv_version': None,
                  'publication_type': m.get('type'), 'url': m.get('URL'),
                  'abstract': m.get('abstract', '')}
        pdf_urls = [x['URL'] for x in m.get('link', []) if x.get('content-type') == 'application/pdf'
                    and urllib.parse.urlsplit(x.get('URL', '')).hostname in HOSTS
                    and x['URL'].startswith('https://')]
    else:
        url = 'https://export.arxiv.org/api/query?id_list=' + urllib.parse.quote(identifier, safe='')
        raw = fetch(url)
        ns = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
        entry = ET.fromstring(raw).find('a:entry', ns)
        if entry is None:
            raise SafetyError('arXiv record missing')
        def txt(tag):
            return ' '.join((entry.findtext(tag, '', ns)).split())
        returned_base, returned_version = arxiv_id(txt('a:id'))
        if returned_base != base or (version is not None and returned_version != version):
            raise SafetyError('arXiv identity/version mismatch')
        fields = {'title': txt('a:title'), 'authors': [{'name': ' '.join((x.findtext('a:name', '', ns)).split())}
                  for x in entry.findall('a:author', ns)], 'year': int(txt('a:published')[:4]),
                  'venue': '', 'doi': txt('x:doi') or None, 'arxiv_id': base,
                  'arxiv_version': returned_version, 'publication_type': 'preprint',
                  'url': txt('a:id'), 'abstract': txt('a:summary'), 'journal_reference': txt('x:journal_ref')}
        pdf_urls = ['https://arxiv.org/pdf/' + returned_base + f'v{returned_version}']
    if not fields['title'] or not fields['authors']:
        raise SafetyError('source lacks title or authors; do not import')
    result = {'input': value, 'identity_type': kind, 'identity': identifier, 'fields': fields,
              'pdf_urls': pdf_urls, 'sources': [{'url': url, 'retrieved_at': now(), 'sha256': digest(raw)}],
              'verification': 'source-retrieved; Zotero field conflicts require review', 'cache_hit': False}
    if cache:
        atomic(cache, canonical({'payload': result, 'sha256': digest(canonical(result)),
                                 'source_raw': raw.decode('utf-8')}).encode())
    return result


def normalized_title(title):
    return re.sub(r'[^\w]', '', title.casefold())


def candidates(resolved, items):
    fields = resolved['fields']
    matches = []
    for item in items:
        d = item['data']
        reasons = []
        if fields.get('doi') and d.get('DOI'):
            try:
                if doi(fields['doi']) == doi(d['DOI']): reasons.append('doi')
            except SafetyError:
                pass
        if fields.get('arxiv_id') and fields['arxiv_id'] in (d.get('url', '') + ' ' + d.get('extra', '')):
            reasons.append('arxiv-base-review-version')
        if normalized_title(fields['title']) == normalized_title(d.get('title', '')):
            reasons.append('normalized-title-only-not-proof')
        if reasons:
            matches.append({'key': item['key'], 'version': item['version'], 'title': d.get('title'),
                            'item_type': d.get('itemType'), 'reasons': reasons, 'action': 'review-existing'})
    return matches
