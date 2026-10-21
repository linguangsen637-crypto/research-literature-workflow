"""Small, stdlib-only primitives. Source documents never become instructions."""
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone


class SafetyError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def checked(path, root, must_exist=False):
    """Reject traversal and every existing symlink component, including ancestors."""
    path, root = Path(path).absolute(), Path(root).absolute()
    if '..' in path.parts or '..' in root.parts:
        raise SafetyError('path traversal')
    if not path.is_relative_to(root):
        raise SafetyError('path outside allowed root')
    for candidate in (root, path):
        for part in [*reversed(candidate.parents), candidate]:
            if part.is_symlink():
                raise SafetyError('symlink component rejected')
    if must_exist and not path.exists():
        raise SafetyError('required path does not exist')
    if path.resolve() != path or root.resolve() != root:
        raise SafetyError('non-canonical path')
    return path


def atomic(path, data, create_only=False, **kwargs):
    from .securefs import write
    return write(path, data, create_only=create_only, **kwargs)


def pdf_info(path):
    p = Path(path)
    h = hashlib.sha256()
    with p.open('rb') as stream:
        header = stream.read(5)
        h.update(header)
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return {'path': str(p.resolve()), 'bytes': p.stat().st_size,
            'header_valid': header == b'%PDF-', 'sha256': h.hexdigest(),
            'content_match': 'Unverified', 'parse_integrity': 'Unverified'}


def cache_key(identity, pdf_hash, parser_version, analysis_config):
    return digest(canonical({'identity': identity, 'pdf_sha256': pdf_hash,
                             'parser_version': parser_version,
                             'analysis_config': analysis_config}))
