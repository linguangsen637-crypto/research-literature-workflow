"""Fail-closed single writer with reviewed plans and protected personal notes."""
from contextlib import contextmanager
from dataclasses import dataclass, asdict
import fcntl
import json
import os
from pathlib import Path
import re
import uuid
import yaml
from .core import SafetyError, atomic, canonical, checked, digest, now
from .securefs import directory, attached, read as secure_read, unlink as secure_unlink

END = '\n<!-- research-os:managed:end -->\n'


def identity(item):
    library = item['library']
    return f"{library['type']}:{library['id']}:{item['key']}"


def paper_filename(title):
    """Keep original titles; minimally replace forbidden filename characters."""
    if not isinstance(title, str) or not title.strip():
        raise SafetyError('paper title is required')
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', title).rstrip(' .')
    if filename in ('', '.', '..') or filename.startswith('.'):
        raise SafetyError('paper title cannot form a safe visible filename')
    if re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', filename):
        filename = '_' + filename
    if len((filename + '.md').encode('utf-8')) > 240:
        raise SafetyError('paper title exceeds filename limit; explicit naming review required')
    return filename + '.md'


def note(item):
    data, lib = item['data'], item['library']
    citekey = data.get('citationKey', '')
    fields = {
        'title': data['title'],
        'authors': [c.get('name') or ' '.join(filter(None, [c.get('firstName'), c.get('lastName')]))
                    for c in data.get('creators', []) if c.get('creatorType') == 'author'],
        'year': int(re.search(r'\b(19|20)\d{2}\b', data.get('date', '')).group())
                if re.search(r'\b(19|20)\d{2}\b', data.get('date', '')) else None,
        'venue': data.get('publicationTitle') or data.get('proceedingsTitle', ''),
        'doi': data.get('DOI', ''), 'arxiv_id': '',
        'zotero_library_type': lib['type'], 'zotero_library_id': str(lib['id']),
        'zotero_key': item['key'], 'citekey': citekey,
        'zotero_link': (f"zotero://select/library/items/{item['key']}" if lib['type'] == 'user'
                        else f"zotero://select/groups/{lib['id']}/items/{item['key']}"),
        'status': 'unread', 'importance': 'normal',
        'tags': [t['tag'] for t in data.get('tags', [])],
        'metadata_verification': 'zotero-snapshot-not-external-verification',
    }
    safe_name = paper_filename(data['title'])
    if safe_name != data['title'] + '.md':
        fields['filename_normalization'] = {'original': data['title'] + '.md', 'safe': safe_name}
    # JSON is a YAML subset; serializing strings prevents frontmatter injection.
    front = '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in fields.items())
    title = ' '.join(data['title'].splitlines())
    sections = ['1. 问题与贡献', '2. 方法与关键公式', '3. 数据与实验条件',
                '4. 结果与证据边界', '5. 局限与待核查问题', '6. 研究启发与关联']
    body = '\n\n'.join('## ' + heading + '\n\nUnverified — 尚未分析。' for heading in sections)
    return '---\n' + front + '\n---\n\n# ' + title + '\n\n' + body + END


def managed_hash(text):
    if text.count(END) != 1:
        raise SafetyError('missing or ambiguous machine boundary')
    return digest(text.split(END)[0] + END)


def stable_identity(text):
    if not text.startswith('---\n'):
        return None
    try:
        metadata = yaml.safe_load(text.split('\n---\n', 1)[0][4:])
        if not isinstance(metadata, dict): return None
        if metadata.get('research_os_id'):
            return 'document:' + str(metadata['research_os_id'])
        return f"{metadata['zotero_library_type']}:{metadata['zotero_library_id']}:{metadata['zotero_key']}"
    except (KeyError, TypeError, yaml.YAMLError):
        return None


@dataclass(frozen=True)
class Plan:
    identity: str
    relative_path: str
    before_hash: str | None
    content: str
    metadata_hash: str
    item_version: int
    citekey: str
    root: str
    backup_root: str | None
    state_root: str
    api_origin: str | None
    server_id: str | None
    pdf_sha256: str | None = None

    @property
    def approval_hash(self):
        return digest(canonical(asdict(self)))


class SafeWriter:
    def __init__(self, root, state, backup=None, enabled=False, api_origin=None, server_id=None,
                 mode='create_only'):
        self.root = checked(root, root)
        self.state = checked(state, state)
        self.backup = checked(backup, backup) if backup else None
        self.enabled = enabled
        self.api_origin, self.server_id = api_origin, server_id
        if mode not in ('create_only', 'reviewed_update'):
            raise SafetyError('unsupported writer mode')
        self.mode = mode
        # Production factory additionally disallows backup inside the entire Vault/workspace.
        if self.backup and (self.backup.is_relative_to(self.root) or self.root.is_relative_to(self.backup)
                            or self.backup.is_relative_to(self.state) or self.state.is_relative_to(self.backup)):
            raise SafetyError('backup must be independent of notes and registry')

    def statepath(self, ident, suffix='.json'):
        return checked(self.state / (digest(ident) + suffix), self.state)

    def entry(self, ident):
        path = self.statepath(ident)
        raw = secure_read(path)
        entry = json.loads(raw) if raw is not None else None
        if entry and (entry.get('api_origin') != self.api_origin or entry.get('server_id') != self.server_id):
            raise SafetyError('registry API version partition changed')
        return entry

    def locate(self, ident, default):
        candidates = []
        if self.root.exists():
            for directory, dirs, files in os.walk(self.root, followlinks=False):
                for name in dirs + files:
                    checked(Path(directory) / name, self.root)
                for name in files:
                    if name.endswith('.md'):
                        path = Path(directory) / name
                        raw = secure_read(path)
                        if raw is not None and stable_identity(raw.decode()) == ident:
                            candidates.append(path)
        if len(candidates) > 1:
            raise SafetyError('duplicate stable identity; do not choose automatically')
        if candidates:
            return candidates[0]
        entry = self.entry(ident)
        if entry:
            raise SafetyError('registered note missing; do not recreate automatically')
        path = checked(self.root / default, self.root)
        if path.suffix != '.md':
            raise SafetyError('only Markdown is allowed')
        if path.exists():
            raise SafetyError('filename collision with unowned note')
        return path

    def plan(self, item):
        ident = identity(item)
        path = self.locate(ident, paper_filename(item['data']['title']))
        raw = secure_read(path)
        old = raw.decode() if raw is not None else None
        entry = self.entry(ident)
        if old is not None and not entry:
            raise SafetyError('existing note has no trusted baseline; adoption requires review')
        if old is not None and managed_hash(old) != entry['managed_hash']:
            raise SafetyError('machine-managed content was edited')
        tail = old.split(END)[1] if old is not None else '\n## My Notes\n\n'
        marker = '\n<!-- research-os:analysis:start -->\n'
        retained = ''
        if old is not None and marker in old.split(END)[0]:
            retained = marker + old.split(END)[0].split(marker, 1)[1]
        content = note(item).removesuffix(END) + retained + END + tail
        managed_hash(content)
        return Plan(ident, str(path.relative_to(self.root)), digest(old) if old is not None else None,
                    content, digest(canonical(item['data'])), item['version'], item['data'].get('citationKey', ''),
                    str(self.root), str(self.backup) if self.backup else None, str(self.state),
                    self.api_origin, self.server_id)

    def document_plan(self, document_id, relative_path, title, body):
        if not re.fullmatch(r'[a-z0-9-]{1,100}', document_id):
            raise SafetyError('invalid knowledge document ID')
        if Path(relative_path).parts[0] not in ('Topics', 'Methods', 'Research'):
            raise SafetyError('knowledge documents require a designated research subdirectory')
        ident = 'document:' + document_id
        path = self.locate(ident, relative_path)
        old = secure_read(path)
        entry = self.entry(ident)
        if old is not None:
            if not entry or managed_hash(old.decode()) != entry['managed_hash']:
                raise SafetyError('knowledge document baseline conflict')
            user = old.decode().split(END)[1]
        else:
            user = '\n## My Notes\n\n'
        if END.strip() in body or '\n<!-- research-os:analysis:start -->\n' in body:
            raise SafetyError('reserved delimiter in knowledge document')
        front = '---\nresearch_os_id: ' + json.dumps(document_id) + '\ntitle: ' + json.dumps(title, ensure_ascii=False) + '\n---\n'
        content = front + '\n# ' + ' '.join(title.splitlines()) + '\n\n' + body + END + user
        return Plan(ident, str(path.relative_to(self.root)), digest(old) if old is not None else None,
                    content, digest(canonical({'title': title, 'body': body})), None, '',
                    str(self.root), str(self.backup) if self.backup else None, str(self.state), self.api_origin, self.server_id)

    @contextmanager
    def lock(self):
        checked(self.state, self.state)
        with directory(self.state, create=True) as parent:
            fd = os.open('.writer.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SafetyError('another writer holds the lock') from None
                attached(self.state, parent)
                yield
            finally:
                os.close(fd)

    def _current_hash(self, path):
        checked(path, self.root)
        raw = secure_read(path)
        return digest(raw) if raw is not None else None

    def event(self, status, ident, **details):
        path = checked(self.state / 'events' / (uuid.uuid4().hex + '.json'), self.state)
        atomic(path, canonical({'time': now(), 'status': status, 'identity': ident,
                               'tokens': None, 'token_status': 'Unavailable', **details}).encode(), create_only=True)

    def apply(self, plan, approved_hash, after_note=None):
        try:
            return self._apply(plan, approved_hash, after_note)
        except Exception as error:
            if self.enabled:
                self.event('failed', plan.identity, error_type=type(error).__name__)
            raise

    def _apply(self, plan, approved_hash, after_note=None):
        """Only accepts an explicit preview hash. Test hook simulates a process interruption."""
        if not self.enabled or not self.backup:
            raise SafetyError('writes disabled or independent backup is unconfigured')
        if approved_hash != plan.approval_hash:
            raise SafetyError('approval does not match preview')
        if (plan.root, plan.backup_root, plan.state_root, plan.api_origin, plan.server_id) != (
                str(self.root), str(self.backup) if self.backup else None, str(self.state),
                self.api_origin, self.server_id):
            raise SafetyError('preview environment changed')
        if stable_identity(plan.content) != plan.identity:
            raise SafetyError('preview identity mismatch')
        target = checked(self.root / plan.relative_path, self.root)
        if target.suffix != '.md':
            raise SafetyError('only Markdown is allowed')
        with self.lock():
            journal_path = self.statepath(plan.identity, '.pending.json')
            if journal_path.exists():
                raise SafetyError('pending transaction requires recovery')
            if self._current_hash(target) != plan.before_hash:
                raise SafetyError('concurrent modification since preview')
            if plan.before_hash == digest(plan.content):
                entry = self.entry(plan.identity)
                if not entry:
                    raise SafetyError('missing registry baseline')
                entry.update(note_path=plan.relative_path, metadata_hash=plan.metadata_hash,
                             zotero_item_version=plan.item_version, last_successful_sync=now(),
                             pdf_sha256=plan.pdf_sha256)
                atomic(self.statepath(plan.identity), canonical(entry).encode())
                self.event('unchanged', plan.identity, note_path=plan.relative_path)
                return {'status': 'unchanged', 'path': str(target)}
            if plan.before_hash is not None and self.mode != 'reviewed_update':
                raise SafetyError('existing note requires reviewed_update with a quiescent editor')
            transaction_id = uuid.uuid4().hex
            backup_path = None
            if target.exists():
                checked(self.backup, self.backup)
                backup_path = checked(self.backup / (digest(plan.identity) + '-' + uuid.uuid4().hex + '.bak'), self.backup)
                original = secure_read(target)
                atomic(backup_path, original, create_only=True)
                if digest(secure_read(backup_path)) != plan.before_hash:
                    raise SafetyError('backup verification failed')
                manifest = {'identity': plan.identity, 'original_path': str(target),
                            'note_root': str(self.root), 'sha256': plan.before_hash,
                            'transaction_id': transaction_id, 'created_at': now(),
                            'api_origin': self.api_origin, 'server_id': self.server_id}
                atomic(Path(str(backup_path) + '.manifest.json'), canonical(manifest).encode(), create_only=True)
            entry = {'identity': plan.identity, 'note_path': plan.relative_path,
                     'api_origin': self.api_origin, 'server_id': self.server_id,
                     'citekey': plan.citekey, 'zotero_item_version': plan.item_version,
                     'pdf_sha256': plan.pdf_sha256, 'metadata_hash': plan.metadata_hash,
                     'note_hash': digest(plan.content), 'managed_hash': managed_hash(plan.content),
                     'last_successful_sync': now(),
                     'sync_status': 'success' if plan.metadata_hash else 'restored_requires_revalidation'}
            journal = {'before_hash': plan.before_hash, 'entry': entry,
                       'backup_path': str(backup_path) if backup_path else None,
                       'transaction_id': transaction_id, 'note_root': str(self.root)}
            atomic(journal_path, canonical(journal).encode())
            self.event('prepared', plan.identity, transaction_id=transaction_id)
            checked(target, self.root)
            if self._current_hash(target) != plan.before_hash:
                raise SafetyError('concurrent modification before replacement')
            atomic(target, plan.content.encode(), create_only=plan.before_hash is None, expected=plan.before_hash)
            if after_note:
                after_note()
            if self._current_hash(target) != entry['note_hash']:
                raise SafetyError('post-write verification failed')
            atomic(self.statepath(plan.identity), canonical(entry).encode())
            self.event('committed', plan.identity, transaction_id=transaction_id, note_path=plan.relative_path)
            secure_unlink(journal_path)
            return {'status': 'success', 'path': str(target), 'backup': journal['backup_path']}

    def recover(self, ident):
        if not self.enabled:
            raise SafetyError('writes disabled')
        with self.lock():
            pending = self.statepath(ident, '.pending.json')
            if not pending.exists():
                return 'nothing-to-recover'
            journal = json.loads(secure_read(pending))
            if journal.get('note_root') != str(self.root):
                raise SafetyError('journal root changed')
            entry = journal['entry']
            if entry['identity'] != ident:
                raise SafetyError('journal identity mismatch')
            if entry.get('api_origin') != self.api_origin or entry.get('server_id') != self.server_id:
                raise SafetyError('journal API partition changed')
            target = checked(self.root / entry['note_path'], self.root)
            current = self._current_hash(target)
            if current == entry['note_hash']:
                atomic(self.statepath(ident), canonical(entry).encode())
                self.event('recovery_finalized', ident, transaction_id=journal['transaction_id'])
                secure_unlink(pending)
                return 'finalized'
            if current == journal['before_hash']:
                self.event('recovery_aborted', ident, transaction_id=journal['transaction_id'])
                secure_unlink(pending)
                return 'aborted-before-write'
            raise SafetyError('recovery conflict; manual review required')

    def restore(self, ident, backup_path, expected_current_hash):
        """Explicit rollback only; never overwrites a changed note."""
        if not self.enabled or not self.backup:
            raise SafetyError('restore disabled')
        backup = checked(backup_path, self.backup, must_exist=True)
        with self.lock():
            if self.statepath(ident, '.pending.json').exists():
                raise SafetyError('recover pending transaction first')
            entry = self.entry(ident)
            if not entry:
                raise SafetyError('missing registry entry')
            target = self.locate(ident, entry['note_path'])
            manifest_path = checked(Path(str(backup) + '.manifest.json'), self.backup, must_exist=True)
            manifest = json.loads(secure_read(manifest_path))
            data = secure_read(backup)
            if (manifest.get('identity') != ident or manifest.get('sha256') != digest(data)
                    or manifest.get('note_root') != str(self.root)
                    or manifest.get('api_origin') != self.api_origin or manifest.get('server_id') != self.server_id):
                raise SafetyError('backup manifest verification failed')
            if stable_identity(data.decode()) != ident:
                raise SafetyError('backup belongs to another note')
            if self._current_hash(target) != expected_current_hash:
                raise SafetyError('restore conflict')
            # Restore uses the same journaled path as ordinary writes.
            restored = yaml.safe_load(data.decode().split('\n---\n', 1)[0][4:])
            plan = Plan(ident, str(target.relative_to(self.root)), expected_current_hash,
                        data.decode(), None, None, restored.get('citekey', ''), str(self.root),
                        str(self.backup), str(self.state), self.api_origin, self.server_id)
        result = self.apply(plan, plan.approval_hash)
        # Metadata was rolled back too; its exact Zotero baseline needs revalidation.
        return {**result, 'metadata_baseline': 'requires-revalidation'}
