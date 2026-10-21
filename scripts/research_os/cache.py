"""Explicit cache partitions; no claim that a cached snapshot is externally verified."""
import json
from pathlib import Path
from .core import atomic, cache_key, canonical, checked, digest, SafetyError


class Cache:
    def __init__(self, root):
        self.root = checked(root, root)

    def path(self, identity, pdf_hash, parser_version, analysis_config):
        return checked(self.root / (cache_key(identity, pdf_hash, parser_version, analysis_config) + '.json'), self.root)

    def put(self, identity, pdf_hash, parser_version, analysis_config, payload):
        path = self.path(identity, pdf_hash, parser_version, analysis_config)
        envelope = {'payload': payload, 'sha256': digest(canonical(payload))}
        atomic(path, canonical(envelope).encode())
        return path

    def get(self, identity, pdf_hash, parser_version, analysis_config):
        path = self.path(identity, pdf_hash, parser_version, analysis_config)
        if not path.exists():
            return None
        envelope = json.loads(path.read_text())
        if digest(canonical(envelope['payload'])) != envelope['sha256']:
            raise SafetyError('cache integrity mismatch')
        return envelope['payload']
