#!/usr/bin/env python3
"""Explicitly authorized, isolated live-directory exercise. Never enables production sync."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from research_os.core import SafetyError, atomic, canonical, checked, digest, now
from research_os.securefs import read, unlink
from research_os.writer import SafeWriter, identity

from research_os.settings import WORKSPACE, config, obsidian_paths


def configured_paths():
    _, root, backup = obsidian_paths(config('obsidian'))
    return root, backup


def fixture(title='Recovery validation A', version=1):
    # Synthetic validation data. Never sent to Zotero; isolated registry/API namespace.
    return {'key': 'TEST2345', 'version': version, 'library': {'type': 'user', 'id': 1},
            'data': {'title': title, 'itemType': 'document', 'citationKey': '',
                     'date': '', 'creators': [], 'tags': [{'tag': 'validation-only'}]}}


def paths(run):
    if len(run) != 32 or any(c not in '0123456789abcdef' for c in run):
        raise SafetyError('invalid validation run ID')
    RESEARCH, BACKUP = configured_paths()
    root = checked(RESEARCH / '_validation' / run, RESEARCH)
    backup = checked(BACKUP / 'research-workflow-validation' / run, BACKUP)
    state = checked(WORKSPACE / 'state/validation' / run, WORKSPACE)
    return root, backup, state


def writer(run):
    root, backup, state = paths(run)
    return SafeWriter(root, state, backup, enabled=True, mode='reviewed_update',
                      api_origin='synthetic-validation-only', server_id='validation-' + run)


def apply(w, paper, crash=False):
    plan = w.plan(paper)
    return w.apply(plan, plan.approval_hash, after_note=(lambda: os._exit(83)) if crash else None)


def probe(root):
    target = root / 'permission-probe.bin'
    atomic(target, b'probe-A', create_only=True)
    if read(target) != b'probe-A':
        raise SafetyError('probe create verification failed')
    atomic(target, b'probe-B', expected=digest(b'probe-A'))
    if read(target) != b'probe-B':
        raise SafetyError('probe replacement verification failed')
    unlink(target)
    if read(target) is not None:
        raise SafetyError('probe cleanup failed')
    return {'path': str(root), 'create': True, 'read': True, 'atomic_replace': True,
            'fsync': True, 'cleanup': True}


def snapshot(directory, excluded=None):
    """Fingerprint existing small local Vault/backup files; do not record contents."""
    result = {}
    for current, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()
                   and (excluded is None or Path(current) / name != excluded)]
        for name in files:
            path = Path(current) / name
            if path.is_symlink():
                result[str(path)] = 'symlink-not-followed'
            else:
                data = read(path)
                if data is not None:
                    result[str(path)] = digest(data)
    return result


def exercise(run):
    RESEARCH, BACKUP = configured_paths()
    root, backup, state = paths(run)
    report_path = WORKSPACE / 'logs' / ('live-writer-' + run + '.json')
    report = {'task_id': run, 'workflow': 'live-writer-validation', 'start_time': now(),
              'status': 'running', 'workspace': str(WORKSPACE), 'research_test_root': str(root),
              'backup_test_root': str(backup), 'state_root': str(state),
              'execution_scope': 'explicitly-approved-exec; not a persistent sandbox grant',
              'synthetic_data': True, 'zotero_changes': [], 'production_writes_enabled': False,
              'tokens': None, 'token_status': 'Unavailable', 'checks': {}}
    before_vault = snapshot(RESEARCH, root)
    before_backup = snapshot(BACKUP, backup)
    try:
        report['directory_probes'] = [probe(root), probe(backup)]
        w = writer(run)
        paper = fixture()
        ident = identity(paper)
        first = apply(w, paper)
        target = Path(first['path'])
        initial = read(target)
        report['checks']['create'] = first['status'] == 'success'
        report['checks']['idempotent'] = apply(w, paper)['status'] == 'unchanged'
        second = apply(w, fixture('Recovery validation B', 2))
        report['checks']['backup_matches_A'] = read(second['backup']) == initial
        w.restore(ident, second['backup'], digest(read(target)))
        report['checks']['restore_exact_A'] = read(target) == initial

        # A real separate process terminates without Python cleanup after note publication.
        process = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                  '--crash-child', run], timeout=30, capture_output=True)
        report['crash_child_exit_code'] = process.returncode
        if process.returncode != 83:
            raise SafetyError('crash child did not reach fault injection')
        report['checks']['pending_survives_process_exit'] = w.statepath(ident, '.pending.json').exists()
        report['checks']['crash_recovery_finalized'] = w.recover(ident) == 'finalized'
        report['checks']['registry_matches_disk'] = w.entry(ident)['note_hash'] == digest(read(target))

        # Simulated user editing touches only this run's newly-created synthetic note.
        old = read(target)
        human = old + '\nValidation user formula: $x^2$\n'.encode()
        atomic(target, human, expected=digest(old))
        apply(w, fixture('Recovery validation D', 4))
        report['checks']['user_notes_preserved'] = read(target).endswith(b'Validation user formula: $x^2$\n')
        plan = w.plan(fixture('Recovery validation E', 5))
        old = read(target)
        atomic(target, old + b'Concurrent validation edit\n', expected=digest(old))
        try:
            w.apply(plan, plan.approval_hash)
        except SafetyError:
            report['checks']['concurrent_edit_rejected'] = read(target).endswith(b'Concurrent validation edit\n')
        else:
            raise SafetyError('concurrent edit was not rejected')

        valid_backups = sorted(backup.glob('*.bak'))
        original_backup = Path(second['backup'])
        current = read(target)
        try:
            w.restore(ident, original_backup, digest(b'outdated-preview'))
        except SafetyError:
            report['checks']['restore_conflict_rejected'] = read(target) == current
        else:
            raise SafetyError('restore conflict was not rejected')

        old = read(target)
        atomic(target, old.replace(b'# Recovery validation D', b'# Validation manual machine edit'), expected=digest(old))
        try:
            w.plan(fixture('Recovery validation F', 6))
        except SafetyError:
            report['checks']['machine_edit_rejected'] = True
        else:
            raise SafetyError('machine-managed edit was not rejected')
        w.restore(ident, original_backup, digest(read(target)))
        report['checks']['final_restore_exact_A'] = read(target) == initial
        report['checks']['no_pending_transactions'] = not list(state.glob('*.pending.json'))
        report['checks']['unrelated_vault_files_unchanged'] = snapshot(RESEARCH, root) == before_vault
        report['checks']['unrelated_backup_files_unchanged'] = snapshot(BACKUP, backup) == before_backup
        report['checks']['workspace_unchanged'] = Path.cwd().resolve() == WORKSPACE
        report['checks']['production_switch_still_off'] = 'writes_enabled: false' in (WORKSPACE / 'config/obsidian.yaml').read_text()
        report['artifacts'] = {'test_note': str(target), 'final_note_sha256': digest(read(target)),
                               'backup_files': [str(p) for p in sorted(backup.glob('*.bak'))],
                               'event_count': len(list((state / 'events').glob('*.json')))}
        if not all(report['checks'].values()):
            raise SafetyError('one or more acceptance checks failed')
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = str(error) if isinstance(error, SafetyError) else type(error).__name__
        raise
    finally:
        report['end_time'] = now()
        atomic(report_path, canonical(report).encode(), create_only=True)
        print(json.dumps({'status': report['status'], 'report': str(report_path),
                          'checks': report['checks']}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-authorized-validation', action='store_true')
    parser.add_argument('--crash-child')
    args = parser.parse_args()
    if Path.cwd().resolve() != WORKSPACE:
        raise SafetyError('workspace must remain unchanged')
    if args.crash_child:
        apply(writer(args.crash_child), fixture('Recovery validation C', 3), crash=True)
    elif args.execute_authorized_validation:
        exercise(uuid.uuid4().hex)
    else:
        parser.error('requires explicit authorized-validation flag; production sync stays disabled')


if __name__ == '__main__':
    main()
