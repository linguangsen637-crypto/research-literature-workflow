#!/usr/bin/env python3
"""Read-only commands; logs are written only inside the fixed literature workspace."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import uuid
import yaml
from research_os.core import SafetyError, atomic, canonical, checked, digest, now, pdf_info
from research_os.zotero import Zotero, duplicate_candidates
from research_os.writer import SafeWriter

from research_os.settings import WORKSPACE, config, obsidian_paths


def local_audit():
    cfg = config('obsidian')
    vault, root, backup = obsidian_paths(cfg)
    pdfs = [pdf_info(checked(p, WORKSPACE)) for p in sorted(WORKSPACE.glob('*.pdf'))]
    return {'workspace': str(WORKSPACE), 'cwd_unchanged': Path.cwd().resolve() == WORKSPACE,
            'vault': str(vault), 'research_root': str(root), 'root_exists': root.exists(),
            'writes_enabled': cfg['sync']['writes_enabled'], 'backup_configured': bool(cfg['backup']['path']),
            'backup_path': str(backup) if backup else None,
            'backup_exists': backup.exists() if backup else False,
            'backup_writable_current_sandbox': os.access(backup, os.W_OK) if backup else False,
            'skills_discovery': 'not-probed; source skills are in workflows/skills',
            'pdfs': pdfs}


def execute(args):
    if args.command == 'audit':
        report = local_audit()
        if args.zotero:
            client = Zotero(config('zotero'))
            collection = client.collection()
            items = client.items()
            report['zotero'] = {'collection_key': collection['key'], 'library': collection['library'],
                'papers': sum(x['data']['itemType'] not in ('note', 'attachment') for x in items),
                'attachments': sum(x['data']['itemType'] == 'attachment' for x in items),
                'notes': sum(x['data']['itemType'] == 'note' for x in items),
                'server_id': client.server_id, 'write_access': 'not-tested'}
        return report
    if args.command == 'collector-plan':
        client = Zotero(config('zotero'))
        matches = duplicate_candidates(args.doi, client.items())
        return {'mode': 'dry-run', 'candidates': matches,
                'metadata_resolution': 'not-implemented', 'pdf_verification': 'not-requested',
                'action': 'review-existing' if matches else 'resolve-metadata-before-import',
                'zotero_changes': [], 'limitations': ['DOI input only in MVP', 'No automatic merge or import']}
    if args.command == 'note-preview':
        client = Zotero(config('zotero'))
        item = client.paper(args.item_key)
        cfg = config('obsidian')
        _, root, _ = obsidian_paths(cfg)
        writer = SafeWriter(root, WORKSPACE / 'state/sync-registry',
                            api_origin=client.base, server_id=client.server_id)
        plan = writer.plan(item)
        return {'mode': 'preview-only', 'plan': asdict(plan), 'preview_hash': plan.approval_hash,
                'api_origin': client.base, 'server_id': client.server_id,
                'apply_available': False, 'modified_notes': []}
    raise SafetyError('unknown command')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-log', action='store_true', help='Do not persist a task report')
    commands = parser.add_subparsers(dest='command', required=True)
    audit = commands.add_parser('audit')
    audit.add_argument('--zotero', action='store_true')
    collector = commands.add_parser('collector-plan')
    collector.add_argument('--doi', required=True)
    preview = commands.add_parser('note-preview')
    preview.add_argument('--item-key', required=True)
    args = parser.parse_args()
    if Path.cwd().resolve() != WORKSPACE:
        parser.error('run from workspace: ' + str(WORKSPACE))
    report = {'task_id': uuid.uuid4().hex, 'workflow': args.command, 'skill': {
        'audit': 'research-librarian', 'collector-plan': 'paper-collector',
        'note-preview': 'obsidian-writer'}[args.command], 'model': None,
        'start_time': now(), 'status': 'running', 'modified_files': [],
        'zotero_changes': [], 'obsidian_changes': [], 'errors': [], 'cache_hits': 0,
        'tokens': {k: None for k in ['input', 'output', 'cached', 'total']},
        'token_status': 'Unavailable', 'llm_calls': 0}
    code = 0
    try:
        report['result'] = execute(args)
        report['status'] = 'success'
    except (SafetyError, OSError, ValueError, KeyError, yaml.YAMLError) as error:
        report['status'] = 'failed'
        # Do not print arbitrary URL/config/error payloads that may carry credentials.
        report['errors'] = [str(error) if isinstance(error, SafetyError) else type(error).__name__]
        code = 1
    report['end_time'] = now()
    if not args.no_log:
        path = checked(WORKSPACE / 'logs' / (report['task_id'] + '.json'), WORKSPACE)
        report['modified_files'].append(str(path))
        atomic(path, canonical(report).encode(), create_only=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
