"""Versioned Zotero changes, never SQLite writes, deletion or emulated merge."""
import json
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid
from .core import SafetyError, atomic, canonical, digest, now
from .zotero import Zotero
from .metadata import candidates


def equivalent(field, actual, expected):
    if field == 'tags':
        normalize = lambda tags: sorted((t['tag'], t.get('type', 0)) for t in tags or [])
        return normalize(actual) == normalize(expected)
    return actual == expected


def zotero_record(resolved, collection):
    f = resolved['fields']
    kind = {'proceedings-article': 'conferencePaper', 'journal-article': 'journalArticle',
            'preprint': 'preprint'}.get(f['publication_type'])
    if not kind:
        raise SafetyError('unsupported publication type; map it explicitly before import')
    record = {'itemType': kind, 'title': f['title'], 'creators': [], 'date': str(f['year'] or ''),
              'url': f.get('url') or '', 'abstractNote': f.get('abstract') or '',
              'collections': [collection], 'tags': [], 'extra': ''}
    for author in f['authors']:
        if author.get('family'):
            record['creators'].append({'creatorType': 'author', 'lastName': author['family'],
                                       'firstName': author.get('given', '')})
        else:
            record['creators'].append({'creatorType': 'author', 'name': author['name']})
    if kind == 'preprint':
        record.update(repository='arXiv', archiveID=f['arxiv_id'])
        record['extra'] = f"arXiv: {f['arxiv_id']}v{f['arxiv_version']}"
        if f.get('doi'):
            record['extra'] += '\nRelated published DOI: ' + f['doi']
    else:
        record['DOI'] = f.get('doi') or ''
        record['publicationTitle' if kind == 'journalArticle' else 'proceedingsTitle'] = f.get('venue') or ''
    return record


def import_plan(client, resolved):
    target = client.collection()
    existing = candidates(resolved, client.listing('items/top'))
    body = zotero_record(resolved, target['key'])
    return {'action': 'review-existing' if existing else 'create-item', 'candidates': existing,
            'api_origin': client.base, 'server_id': client.server_id,
            'library': client.config['library'], 'collection': target['key'],
            'record': body, 'sources': resolved['sources'], 'created_at': now(),
            'write_token': uuid.uuid4().hex}


def patch_plan(client, key, proposed, remove_tags=()):
    item = client.paper(key)
    allowed = {'title', 'creators', 'date', 'DOI', 'url', 'abstractNote',
               'publicationTitle', 'proceedingsTitle', 'extra', 'tags'}
    if not set(proposed) <= allowed:
        raise SafetyError('patch contains fields outside allowlist')
    changes = dict(proposed)
    if 'tags' in changes:
        current = {t['tag']: t for t in item['data'].get('tags', [])}
        for name in remove_tags:
            current.pop(name, None)
        for tag in changes['tags']:
            current.setdefault(tag['tag'], tag)
        changes['tags'] = list(current.values())
    changes = {k: v for k, v in changes.items() if item['data'].get(k) != v}
    return {'action': 'patch-item', 'key': key, 'version': item['version'], 'changes': changes,
            'before': {k: item['data'].get(k) for k in changes},
            'api_origin': client.base, 'server_id': client.server_id, 'library': client.config['library'],
            'collection': client.collection()['key'], 'created_at': now()}


class ZoteroWriter:
    def __init__(self, client, state, backup, authorize=False):
        self.client, self.state, self.backup = client, Path(state), Path(backup)
        self.authorize = authorize
        self.key = os.environ.get('ZOTERO_LOCAL_API_KEY')
        self.remembered = bool(self.key)

    def request(self, method, route, value=None, headers=None, raw=False):
        if not self.key:
            if not self.authorize:
                raise SafetyError('Zotero write key missing; use --authorize for the official dialog')
            req = urllib.request.Request(self.client.base + 'local/authorize',
                data=canonical({'appName': 'Research Workflow'}).encode(), method='POST',
                headers={'Content-Type': 'application/json', 'Zotero-Server-ID': self.client.server_id})
            try:
                with self.client.opener.open(req, timeout=55) as response:
                    grant = json.load(response)
            except (urllib.error.URLError, TimeoutError):
                raise SafetyError('Zotero write authorization denied, unavailable or timed out') from None
            self.key, self.remembered = grant['key'], grant.get('remember', False)
        req = urllib.request.Request(self.client.base + route,
            # Zotero 10 applies attachment fields in input order: linkMode must precede filename.
            data=value if raw else json.dumps(value, ensure_ascii=False).encode(), method=method,
            headers={'Content-Type': 'application/json', 'Zotero-Server-ID': self.client.server_id,
                     'Zotero-API-Key': self.key, **(headers or {})})
        try:
            with self.client.opener.open(req, timeout=45) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            if error.code == 401: self.key = None
            raise SafetyError(f'Zotero write failed HTTP {error.code}; do not blind retry') from None
        except urllib.error.URLError:
            raise SafetyError('Zotero write outcome uncertain; reconcile before retry') from None
        finally:
            if not self.remembered:
                self.key = None

    @contextmanager
    def lock(self):
        from .securefs import directory
        with directory(self.state, create=True) as parent:
            fd = os.open('.zotero-write.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise SafetyError('another Zotero writer is active') from None
                yield
            finally: os.close(fd)

    def apply(self, plan, approval):
        with self.lock(): return self._apply(plan, approval)

    def _apply(self, plan, approval):
        if digest(canonical(plan)) != approval:
            raise SafetyError('approval hash differs from complete change preview')
        c = self.client
        if (plan['api_origin'], plan['server_id'], plan['library'], plan['collection']) != (
                c.base, c.server_id, c.config['library'], c.collection()['key']):
            raise SafetyError('change plan context mismatch')
        job = self.state / (approval + '.json')
        if job.exists():
            state = json.loads(job.read_text())
            if state['status'] == 'success':
                current = c.paper(state['item_key'])
                if plan['action'] == 'patch-item' and any(not equivalent(k, current['data'].get(k), v)
                                                         for k, v in plan['changes'].items()):
                    raise SafetyError('previously successful item changed; generate a new preview')
                return state
            raise SafetyError('prior write needs reconciliation; never repeat uncertain creation')
        state = {'status': 'prepared', 'action': plan['action'], 'approval_hash': approval,
                 'server_id': c.server_id, 'started_at': now()}
        if plan['action'] == 'patch-item':
            current = c.paper(plan['key'])
            if current['version'] != plan['version']:
                raise SafetyError('Zotero item version changed since preview')
            if not plan['changes']:
                return {**state, 'status': 'unchanged'}
            atomic(self.backup / ('zotero-' + approval + '.json'), canonical(current).encode(), create_only=True)
        elif plan['action'] == 'create-item':
            if plan['record'].get('collections') != [plan['collection']]:
                raise SafetyError('creation must target only the approved Collection')
            if plan['candidates']:
                raise SafetyError('duplicate candidates prohibit automatic creation')
            # Recheck against fresh library state, including entries outside the Collection.
            for existing in c.listing('items/top'):
                d = existing['data']; new = plan['record']
                if (d.get('DOI') and d['DOI'].casefold() == new.get('DOI', '').casefold()) or (
                        d.get('title', '').casefold() == new['title'].casefold()):
                    raise SafetyError('new duplicate candidate appeared since preview')
        else:
            raise SafetyError('unsupported write action; deletion and merge are excluded')
        atomic(job, canonical(state).encode(), create_only=True)
        try:
            if plan['action'] == 'patch-item':
                self.request('PATCH', c.prefix + 'items/' + plan['key'], plan['changes'],
                             {'If-Unmodified-Since-Version': str(plan['version'])})
                saved = c.paper(plan['key'])
                if any(not equivalent(k, saved['data'].get(k), v) for k, v in plan['changes'].items()):
                    raise SafetyError('Zotero post-write metadata verification failed')
                state['item_key'] = plan['key']
            else:
                response = self.request('POST', c.prefix + 'items', [plan['record']],
                                        {'Zotero-Write-Token': plan['write_token']})
                if response.get('failed') or '0' not in response.get('successful', {}):
                    raise SafetyError('Zotero rejected item creation; inspect transaction')
                state['item_key'] = response['successful']['0']['key']
                saved = c.paper(state['item_key'])
                if saved['data']['title'] != plan['record']['title']:
                    raise SafetyError('created item verification failed')
            state.update(status='success', finished_at=now())
            atomic(job, canonical(state).encode())
            return state
        except Exception:
            state.update(status='needs_reconciliation', finished_at=now())
            atomic(job, canonical(state).encode())
            raise

    def reconcile(self, plan):
        approval = digest(canonical(plan))
        job = self.state / (approval + '.json')
        state = json.loads(job.read_text())
        if state['server_id'] != self.client.server_id:
            raise SafetyError('transaction Server ID changed')
        key = plan.get('key') if plan['action'] == 'patch-item' else state.get('item_key')
        if not key:
            raise SafetyError('creation result key unknown; manual reconciliation required')
        current = self.client.paper(key)
        expected = plan['changes'] if plan['action'] == 'patch-item' else plan['record']
        if any(not equivalent(k, current['data'].get(k), v) for k, v in expected.items()):
            raise SafetyError('Zotero state differs from intended write; do not retry automatically')
        state.update(status='success', item_key=key, reconciled_at=now(), verification='fresh-API-readback')
        atomic(job, canonical(state).encode())
        return state

    def attach(self, parent, pdf_path):
        with self.lock(): return self._attach(parent, pdf_path)

    def _attach(self, parent, pdf_path):
        """Stored PDF upload using supported three-phase local upload protocol."""
        from .core import pdf_info
        import hashlib
        c = self.client
        c.paper(parent)  # Confirms membership, identity and parent item type.
        path = Path(pdf_path)
        info = pdf_info(path)
        if not info['header_valid'] or info['bytes'] > 200 * 1024 * 1024:
            raise SafetyError('invalid/oversized PDF')
        data = path.read_bytes(); md5 = hashlib.md5(data).hexdigest()
        task = self.state / ('attachment-' + digest(parent + info['sha256']) + '.json')
        if task.exists():
            old = json.loads(task.read_text())
            if old['status'] == 'success':
                stored = c.get(c.prefix + 'items/' + old['attachment_key'])
                if stored['data'].get('parentItem') != parent or stored['data'].get('md5') != md5:
                    raise SafetyError('previously uploaded attachment changed; review before retry')
                return old
            if old['status'] != 'rejected':
                raise SafetyError('attachment transaction requires reconciliation')
        children = c.get(c.prefix + f'items/{parent}/children')
        if any(x['data'].get('contentType') == 'application/pdf' for x in children):
            raise SafetyError('parent already has PDF; review versions rather than add automatically')
        state = {'status': 'prepared', 'parent': parent, 'sha256': info['sha256']}
        atomic(task, canonical(state).encode(), create_only=not task.exists())
        try:
            result = self.request('POST', c.prefix + 'items', [{'itemType': 'attachment',
                'parentItem': parent, 'linkMode': 'imported_file', 'title': 'Original PDF',
                'contentType': 'application/pdf', 'filename': path.name, 'tags': []}],
                {'Zotero-Write-Token': uuid.uuid4().hex})
            if result.get('failed') or '0' not in result.get('successful', {}):
                state['rejection'] = result.get('failed', {})
                state['status'] = 'rejected'
                raise SafetyError('attachment metadata creation failed')
            key = result['successful']['0']['key']; state['attachment_key'] = key
            atomic(task, canonical(state).encode())
            route = c.prefix + f'items/{key}/file'
            params = {'md5': md5, 'filename': path.name, 'filesize': len(data),
                      'mtime': int(path.stat().st_mtime * 1000)}
            headers = {'If-None-Match': '*', 'Content-Type': 'application/x-www-form-urlencoded'}
            upload = self.request('POST', route, urllib.parse.urlencode(params).encode(), headers, raw=True)
            if not upload.get('exists'):
                url = urllib.parse.urljoin(c.base, upload['url'])
                if not url.startswith(c.base + 'local/uploads/'):
                    raise SafetyError('upload URL outside Zotero loopback endpoint')
                req = urllib.request.Request(url, data=data, method='POST',
                    headers={'Content-Type': upload.get('contentType', 'application/octet-stream')})
                with c.opener.open(req, timeout=45) as response:
                    if response.status != 201: raise SafetyError('attachment binary upload failed')
                self.request('POST', route, urllib.parse.urlencode({'upload': upload['uploadKey']}).encode(), headers, raw=True)
            stored = c.get(c.prefix + f'items/{key}')
            if stored['data'].get('md5') != md5:
                raise SafetyError('attachment readback hash mismatch')
            state.update(status='success', finished_at=now())
            atomic(task, canonical(state).encode()); return state
        except Exception:
            if state['status'] != 'rejected': state['status'] = 'needs_reconciliation'
            atomic(task, canonical(state).encode()); raise
