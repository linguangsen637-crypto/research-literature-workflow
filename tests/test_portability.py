"""Configuration boundaries and actual execution from a relocated workspace."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from research_os import settings
from research_os.core import SafetyError
from research_os.writer import SafeWriter, note, paper_filename
from research_os.maintenance import search_notes
from research_os.zotero import Zotero
from research_os.metadata import configured_hosts, safe_url
from test_safety import item


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.vault = self.base / 'Knowledge'
        self.vault.mkdir()
        self.cfg = {'vault': {'path': str(self.vault)}, 'research': {'root': 'Literature'},
                    'backup': {'path': str(self.base / 'Backup')}}

    def test_different_projects_need_only_configuration(self):
        for relative in ('Literature', 'History/Reading', 'Materials'):
            cfg = copy.deepcopy(self.cfg)
            cfg['research']['root'] = relative
            _, root, _ = settings.obsidian_paths(cfg)
            self.assertEqual(root, self.vault / relative)

    def test_reject_unsafe_note_roots(self):
        for relative in ('', '.', '..', '../outside', '/tmp', '.obsidian', 'Literature/.obsidian'):
            with self.subTest(relative=relative), self.assertRaises(SafetyError):
                settings.obsidian_paths({**self.cfg, 'research': {'root': relative}})

    def test_reject_dependent_backups(self):
        for path in (self.vault, self.vault / 'Backup', settings.WORKSPACE / 'Backup'):
            with self.subTest(path=path), self.assertRaises(SafetyError):
                settings.obsidian_paths({**self.cfg, 'backup': {'path': str(path)}})

    def test_reject_symlink_note_directory(self):
        (self.vault / 'Literature').symlink_to(self.base / 'Elsewhere')
        with self.assertRaises(SafetyError):
            settings.obsidian_paths(self.cfg)

    def test_source_access_is_explicit(self):
        source = self.base / 'Sources'
        source.mkdir()
        pdf = source / 'paper.pdf'
        pdf.write_bytes(b'%PDF-fixture')
        with patch.object(settings, 'config', return_value={'sources': {'pdf_roots': []}}):
            with self.assertRaises(SafetyError):
                settings.approved_pdf_path(pdf)
        with patch.object(settings, 'config', return_value={'sources': {'pdf_roots': [str(source)]}}):
            self.assertEqual(settings.approved_pdf_path(pdf), pdf)

    def test_source_symlink_cannot_escape_allowlist(self):
        source = self.base / 'Sources'
        source.mkdir()
        outside = self.base / 'private.pdf'
        outside.write_bytes(b'%PDF-fixture')
        (source / 'paper.pdf').symlink_to(outside)
        with patch.object(settings, 'config', return_value={'sources': {'pdf_roots': [str(source)]}}):
            with self.assertRaises(SafetyError):
                settings.approved_pdf_path(source / 'paper.pdf')

    def test_paper_uses_original_title_in_root(self):
        writer = SafeWriter(self.vault / 'Literature', self.base / 'State')
        plan = writer.plan(item(title='Evidence and Interpretation: A Study'))
        self.assertEqual(plan.relative_path, 'Evidence and Interpretation_ A Study.md')
        self.assertIn('# Evidence and Interpretation: A Study\n', plan.content)
        self.assertIn('filename_normalization:', plan.content)

    def test_unicode_title_and_separator(self):
        self.assertEqual(paper_filename('社会研究／方法'), '社会研究／方法.md')
        self.assertEqual(paper_filename('Theory/Practice'), 'Theory_Practice.md')
        with self.assertRaises(SafetyError):
            paper_filename('')

    def test_same_title_different_identity_is_not_overwritten(self):
        writer = SafeWriter(self.vault / 'Literature', self.base / 'State',
                            self.base / 'Backup', enabled=True)
        first = writer.plan(item())
        writer.apply(first, first.approval_hash)
        second = item()
        second['key'] = 'BCDEFGHJ'
        with self.assertRaises(SafetyError):
            writer.plan(second)

    def test_title_lookup_before_registry_exists(self):
        root = self.vault / 'Literature'
        root.mkdir()
        (root / 'Example.md').write_text(note(item()))
        hits = [{'key': 'ABCDEFGH', 'title': 'Example'}]
        result = search_notes(root, self.base / 'State', 'Example', hits, {'type': 'user', 'id': 42})
        self.assertEqual(result[0]['path'], str(root / 'Example.md'))

    def test_placeholder_zotero_identity_never_connects(self):
        with self.assertRaises(SafetyError):
            Zotero({'api': {'base_url': 'http://127.0.0.1:23119/api/',
                            'expected_server_id': 'REPLACE_WITH_LOCAL_SERVER_ID'},
                    'library': {'type': 'user', 'id': 0}})

    def test_extra_source_host_requires_explicit_allowlist(self):
        address = [(2, 1, 6, '', ('93.184.216.34', 443))]
        with patch('research_os.metadata.socket.getaddrinfo', return_value=address):
            with self.assertRaises(SafetyError):
                safe_url('https://publisher.example/paper.pdf', hosts={'arxiv.org'})
            self.assertEqual(safe_url('https://publisher.example/paper.pdf', hosts={'publisher.example'}),
                             'https://publisher.example/paper.pdf')

    def test_allowlisted_host_cannot_resolve_to_private_network(self):
        address = [(2, 1, 6, '', ('127.0.0.1', 443))]
        with patch('research_os.metadata.socket.getaddrinfo', return_value=address):
            with self.assertRaises(SafetyError):
                safe_url('https://publisher.example/paper.pdf', hosts={'publisher.example'})

    def test_relocated_cli_and_idempotent_initialization(self):
        source = Path(__file__).resolve().parents[1]
        workspace = self.base / 'Research Workspace'
        for directory in ('scripts', 'templates', 'config'):
            shutil.copytree(source / directory, workspace / directory,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        command = [sys.executable, '-B', str(workspace / 'scripts/init_workspace.py')]
        subprocess.run(command, cwd=workspace, check=True, capture_output=True)
        config_path = workspace / 'config/obsidian.yaml'
        config_path.write_text(yaml.safe_dump({**self.cfg, 'sync': {'writes_enabled': False}}))
        original = config_path.read_bytes()
        subprocess.run(command, cwd=workspace, check=True, capture_output=True)
        self.assertEqual(config_path.read_bytes(), original)
        result = subprocess.run([sys.executable, '-B', str(workspace / 'scripts/research.py'),
                                 '--no-log', 'audit'], cwd=workspace, check=True, capture_output=True, text=True)
        report = json.loads(result.stdout)
        self.assertEqual(report['result']['workspace'], str(workspace))
        self.assertFalse(report['result']['writes_enabled'])
        self.assertEqual(list(self.vault.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
