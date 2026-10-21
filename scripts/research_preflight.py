#!/usr/bin/env python3
"""Current-process checks; never grants permissions or enables production sync."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from research import WORKSPACE, config, obsidian_paths
from research_os.core import SafetyError, atomic, canonical, now
from research_os.zotero import Zotero


def connection():
    c = Zotero(config('zotero'))
    collection = c.collection()
    return {'status': 'passed', 'collection_key': collection['key'],
            'library': collection['library'], 'server_id': c.server_id}


def ensure_zotero(timeout=25):
    try:
        return connection()
    except SafetyError as error:
        # Identity/HTTP failures must never launch a second app or retry another library.
        if 'Zotero GET unavailable:' not in str(error):
            raise
    running = subprocess.run(['pgrep', '-u', str(os.getuid()), '-x', 'zotero'],
                             capture_output=True).returncode == 0
    if not running:
        known = Path.home() / '.local/bin/zotero'
        executable = str(known) if known.is_file() else shutil.which('zotero')
        if not executable:
            raise SafetyError('Zotero executable missing')
        (WORKSPACE / 'logs').mkdir(exist_ok=True)
        with (WORKSPACE / 'logs/zotero-startup.log').open('ab') as log:
            subprocess.Popen([executable], stdin=subprocess.DEVNULL, stdout=log,
                             stderr=log, start_new_session=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return connection()
        except SafetyError as error:
            if 'Zotero GET unavailable:' not in str(error):
                raise
        time.sleep(1)
    raise SafetyError('Zotero API not ready after 25s; check Zotero window and local API setting')


def check(probe=False, ensure=False):
    cfg = config('obsidian')
    vault, root, backup = obsidian_paths(cfg)
    result = {'time': now(), 'cwd': str(Path.cwd()),
              'production_auto_sync_enabled': cfg['sync']['writes_enabled'],
              'proxy_active': os.environ.get('CODEX_NETWORK_PROXY_ACTIVE', ''),
              'zotero': {}, 'filesystem': [], 'errors': []}
    try:
        result['zotero'] = ensure_zotero() if ensure else connection()
    except (SafetyError, OSError) as error:
        result['errors'].append(str(error))
    run = uuid.uuid4().hex
    for parent, relative in [(root, Path('_validation') / run),
                             (backup, Path('research-workflow-validation') / run)]:
        entry = {'root': str(parent), 'writable_hint': os.access(parent, os.W_OK),
                 'write_probe': 'not-requested'}
        if probe:
            target = parent / relative / 'permission-probe.bin'
            try:
                # Same bounded, temporary probe as the authorized live recovery exercise.
                from validate_live_writer import probe as write_probe
                entry['write_probe'] = write_probe(target.parent)
            except (OSError, SafetyError) as error:
                entry['write_probe'] = 'failed: ' + type(error).__name__
                result['errors'].append('write probe failed: ' + str(parent))
        elif not entry['writable_hint'] and not ensure:
            result['errors'].append('current process lacks write access: ' + str(parent))
        result['filesystem'].append(entry)
    result['boundary_hints'] = {str(p): os.access(p, os.W_OK)
                                for p in (vault, vault / '.obsidian', backup.parent)}
    if probe and any(result['boundary_hints'].values()):
        result['errors'].append('permission boundary broader than approved roots')
    result['status'] = 'failed' if result['errors'] else 'passed'
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ensure-zotero', action='store_true', help='host launcher only; may start Zotero')
    p.add_argument('--probe-writes', action='store_true', help='isolated validation directories only')
    p.add_argument('--out', default=str(WORKSPACE / 'state/preflight-current.json'))
    a = p.parse_args()
    if Path.cwd().resolve() != WORKSPACE:
        p.error('fixed workspace required')
    from research_os.core import checked
    target = checked(a.out, WORKSPACE)
    result = check(a.probe_writes, a.ensure_zotero)
    atomic(target, canonical(result).encode())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
