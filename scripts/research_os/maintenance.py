"""Bounded metadata-first retrieval and read-only library diagnostics."""
from collections import defaultdict
import json
from pathlib import Path
import re
import urllib.parse
from .core import SafetyError, canonical, checked, digest, pdf_info
from .metadata import normalized_title
from .writer import stable_identity, managed_hash, paper_filename
from .securefs import read
from .zotero import doi


def search(items, query, limit=10):
    words = re.findall(r'\w+', query.casefold())
    if not words: raise SafetyError('search requires non-empty keywords')
    ranked = []
    for item in items:
        d = item['data']
        if d['itemType'] in ('note', 'attachment'): continue
        title = d.get('title', '').casefold()
        abstract = d.get('abstractNote', '').casefold()
        tags = ' '.join(t['tag'] for t in d.get('tags', [])).casefold()
        score = sum(3 * title.count(w) + abstract.count(w) + 2 * tags.count(w) for w in words)
        if score:
            ranked.append({'key': item['key'], 'title': d['title'], 'score': score,
                           'evidence': 'metadata-only; full text not read'})
    return sorted(ranked, key=lambda x: (-x['score'], x['key']))[:min(limit, 50)]


def search_notes(root, state, query, hits, library, limit=10):
    """Read only metadata-selected paper notes and filename-selected topic/method notes."""
    root, state = Path(root), Path(state)
    words = re.findall(r'\w+', query.casefold())
    paths = set()
    for hit in hits:
        ident = f"{library['type']}:{library['id']}:{hit['key']}"
        entry = state / (digest(ident) + '.json')
        relative = json.loads(read(entry))['note_path'] if entry.exists() else paper_filename(hit['title'])
        paths.add(checked(root / relative, root))
    for folder in ['Topics', 'Methods', 'Research']:
        for path in (root / folder).glob('*.md'):
            if any(w in path.stem.casefold() for w in words): paths.add(checked(path, root))
    result=[]
    for path in sorted(paths)[:50]:
        raw = read(path)
        if raw is None: continue
        text = raw.decode(); score=sum(text.casefold().count(w) for w in words)
        if score:
            matching = next((line for line in text.splitlines() if any(w in line.casefold() for w in words)), '')
            result.append({'path':str(path), 'score':score, 'snippet':matching[:500],
                           'evidence':'local note; claims still require original source verification'})
    return sorted(result,key=lambda x:-x['score'])[:min(limit,20)]


def inspect(items, root, state, library, taxonomy, source_roots=()):
    findings, dois, titles, papers, pdf_parents = [], defaultdict(list), defaultdict(list), {}, set()
    pdf_paths = defaultdict(list)
    for x in items:
        d = x['data']
        if d['itemType'] == 'attachment' and d.get('contentType') == 'application/pdf':
            pdf_parents.add(d.get('parentItem'))
            url = x.get('links', {}).get('enclosure', {}).get('href', '')
            if url.startswith('file:///'):
                try:
                    candidate = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path))
                    approved = next((Path(r) for r in source_roots if candidate.is_relative_to(Path(r))), None)
                    if approved is None: raise SafetyError('attachment outside configured source roots')
                    path = checked(candidate, approved, True)
                    with path.open('rb') as stream: valid = stream.read(5) == b'%PDF-'
                    if not valid: findings.append({'kind':'invalid-pdf', 'key':x['key']})
                    else: pdf_paths[d.get('parentItem')].append(path)
                except (OSError, SafetyError):
                    findings.append({'kind':'unreadable-pdf', 'key':x['key']})
        if d['itemType'] not in ('attachment', 'note'):
            papers[x['key']] = x
            if d.get('DOI'):
                try: dois[doi(d['DOI'])].append(x['key'])
                except SafetyError: findings.append({'kind':'invalid-doi-format','key':x['key']})
            titles[normalized_title(d.get('title', ''))].append(x['key'])
            if not d.get('DOI') and d['itemType'] not in ('preprint', 'book'):
                findings.append({'kind': 'missing-doi', 'key': x['key']})
            for tag in d.get('tags', []):
                mapped = taxonomy.get('aliases', {}).get(tag['tag'])
                if mapped and mapped != tag['tag'] and mapped not in {t['tag'] for t in d.get('tags', [])}:
                    findings.append({'kind': 'tag-alias', 'key': x['key'], 'existing': tag['tag'], 'suggested': mapped})
    for key in sorted(set(papers) - pdf_parents): findings.append({'kind': 'missing-pdf', 'key': key})
    for label, groups in [('duplicate-doi-candidate', dois), ('duplicate-title-candidate', titles)]:
        for value, keys in groups.items():
            if value and len(keys) > 1:
                findings.append({'kind': label, 'keys': sorted(keys), 'action': 'review-in-Zotero-native-merge'})
    note_ids = defaultdict(list)
    root = Path(root)
    if root.exists():
        for path in root.rglob('*.md'):
            if '_validation' in path.relative_to(root).parts: continue
            if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                findings.append({'kind': 'symlink-note-rejected', 'path': str(path)}); continue
            raw = read(path)
            ident = stable_identity(raw.decode())
            if ident:
                note_ids[ident].append(str(path))
                prefix = f"{library['type']}:{library['id']}:"
                if ident.startswith(prefix) and ident.removeprefix(prefix) not in papers:
                    findings.append({'kind': 'note-not-in-target-collection', 'identity': ident, 'path': str(path),
                                     'action': 'review; may exist elsewhere in library, never delete'})
    for ident, paths in note_ids.items():
        if len(paths) > 1: findings.append({'kind': 'duplicate-note-identity', 'identity': ident, 'paths': paths})
    for path in Path(state).glob('*.json') if Path(state).exists() else []:
        if path.name.endswith('.pending.json'):
            findings.append({'kind': 'pending-transaction', 'path': str(path)}); continue
        entry = json.loads(read(path))
        if 'identity' not in entry: continue
        prefix = f"{library['type']}:{library['id']}:"
        item_key = entry['identity'].removeprefix(prefix)
        if entry['identity'].startswith(prefix) and item_key in papers:
            if entry.get('zotero_item_version') != papers[item_key]['version']:
                findings.append({'kind':'metadata-sync-pending', 'identity':entry['identity'],
                                 'registered_version':entry.get('zotero_item_version'),
                                 'current_version':papers[item_key]['version']})
            if entry.get('pdf_sha256') and pdf_paths[item_key]:
                if entry['pdf_sha256'] not in {pdf_info(p)['sha256'] for p in pdf_paths[item_key]}:
                    findings.append({'kind':'pdf-version-changed', 'identity':entry['identity']})
        matches = note_ids.get(entry['identity'], [])
        if len(matches) == 1:
            actual = Path(matches[0]); text = read(actual).decode()
            if str(actual.relative_to(root)) != entry['note_path']:
                findings.append({'kind': 'note-moved', 'identity': entry['identity'], 'path': str(actual)})
            try:
                if managed_hash(text) != entry['managed_hash']:
                    findings.append({'kind': 'managed-note-conflict', 'identity': entry['identity']})
            except SafetyError:
                findings.append({'kind': 'managed-boundary-invalid', 'identity': entry['identity']})
        elif not matches:
            findings.append({'kind': 'registered-note-missing', 'identity': entry['identity']})
    findings.sort(key=canonical)
    return {'papers_checked': len(papers), 'findings': findings, 'fingerprint': digest(canonical(findings)),
            'mode': 'read-only', 'repairs_applied': [], 'merge_policy': 'native Zotero, reviewed only'}


def comparison_request(question, requests, outputs):
    from .papers import validate_analysis
    if len(requests) < 2 or len(requests) != len(outputs):
        raise SafetyError('comparison requires at least two actual source/analysis pairs')
    for req, output in zip(requests, outputs): validate_analysis(output, req)
    return {'question': question, 'papers': [{'identity': r['paper_identity'], 'sources': r['sources'],
            'analysis': o['results']} for r, o in zip(requests, outputs)],
            'requirements': ['cite source_id and PDF page for every important comparison',
                'do not rank different datasets/protocols directly', 'separate facts, interpretations and suggestions'],
            'status': 'ready-for-structured-synthesis; no model call performed'}
