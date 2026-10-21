import dataclasses
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from research_os.core import SafetyError, checked, digest, pdf_info
from research_os.cache import Cache
from research_os.writer import SafeWriter, identity, note
from research_os.zotero import Zotero, duplicate_candidates


def item(title='Example', citekey='Example2026', version=1):
    return {'key': 'ABCDEFGH', 'version': version, 'library': {'type': 'user', 'id': 42},
            'data': {'title': title, 'itemType': 'conferencePaper', 'citationKey': citekey,
                     'DOI': '10.1234/example', 'date': '2026', 'creators': [], 'tags': []}}


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'vault' / 'Literature'
        self.writer = SafeWriter(self.root, self.base / 'state', self.base / 'backup', enabled=True,
                                 api_origin='local', server_id='test-instance', mode='reviewed_update')

    def write(self, paper=None, **kwargs):
        plan = self.writer.plan(paper or item())
        return self.writer.apply(plan, plan.approval_hash, **kwargs)

    def test_preview_has_no_filesystem_side_effects(self):
        self.writer.plan(item())
        self.assertEqual(list(self.base.iterdir()), [])

    def test_idempotent(self):
        self.write()
        self.assertEqual(self.write()['status'], 'unchanged')
        self.assertEqual(len(list(self.root.rglob('*.md'))), 1)

    def test_citekey_change_reuses_note(self):
        self.write()
        self.write(item(citekey='Changed2026', version=2))
        self.assertEqual(len(list(self.root.rglob('*.md'))), 1)
        self.assertIn('Changed2026', (self.root / 'Example.md').read_text())

    def test_renamed_note_is_relocated(self):
        self.write()
        moved = self.root / 'moved.md'
        (self.root / 'Example.md').rename(moved)
        result = self.write(item(title='Updated', version=2))
        self.assertEqual(result['path'], str(moved))
        self.assertEqual(len(list(self.root.rglob('*.md'))), 1)

    def test_user_notes_preserved(self):
        self.write()
        p = self.root / 'Example.md'
        p.write_text(p.read_text() + 'My formula: $x^2$\n')
        self.write(item(title='Updated', version=2))
        self.assertTrue(p.read_text().endswith('My formula: $x^2$\n'))

    def test_machine_edit_is_conflict(self):
        self.write()
        p = self.root / 'Example.md'
        p.write_text(p.read_text().replace('# Example', '# Human edit'))
        with self.assertRaises(SafetyError):
            self.writer.plan(item(title='New'))

    def test_concurrent_edit_after_preview(self):
        self.write()
        plan = self.writer.plan(item(title='New'))
        p = self.root / plan.relative_path
        p.write_text(p.read_text() + 'Human edit')
        with self.assertRaises(SafetyError):
            self.writer.apply(plan, plan.approval_hash)
        self.assertTrue(p.read_text().endswith('Human edit'))

    def test_unowned_collision(self):
        p = self.root / 'Example.md'
        p.parent.mkdir(parents=True)
        p.write_text('Unmanaged note')
        with self.assertRaises(SafetyError):
            self.writer.plan(item())

    def test_missing_registered_note_not_recreated(self):
        self.write()
        (self.root / 'Example.md').unlink()
        with self.assertRaises(SafetyError):
            self.writer.plan(item())

    def test_duplicate_identity(self):
        self.write()
        (self.root / 'duplicate.md').write_text((self.root / 'Example.md').read_text())
        with self.assertRaises(SafetyError):
            self.writer.plan(item())

    def test_traversal_and_symlink_rejected(self):
        with self.assertRaises(SafetyError):
            checked(self.root / '../outside.md', self.root)
        self.root.mkdir(parents=True)
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / 'Example.md').symlink_to(outside / 'Example.md')
        with self.assertRaises(SafetyError):
            self.writer.plan(item())
        self.assertEqual(list(outside.iterdir()), [])

    def test_disabled_writer(self):
        self.writer.enabled = False
        plan = self.writer.plan(item())
        with self.assertRaises(SafetyError):
            self.writer.apply(plan, plan.approval_hash)
        self.assertFalse(self.root.exists())

    def test_preview_hash_required(self):
        plan = self.writer.plan(item())
        with self.assertRaises(SafetyError):
            self.writer.apply(plan, 'wrong')

    def test_approval_covers_target_path(self):
        plan = self.writer.plan(item())
        tampered = dataclasses.replace(plan, relative_path='Other.md')
        with self.assertRaises(SafetyError):
            self.writer.apply(tampered, plan.approval_hash)

    def test_renamed_unchanged_note_refreshes_registry(self):
        self.write()
        moved = self.root / 'renamed.md'
        (self.root / 'Example.md').rename(moved)
        self.write()
        self.assertEqual(self.writer.entry(identity(item()))['note_path'], 'renamed.md')

    def test_failed_replacement_keeps_registry_uncommitted(self):
        plan = self.writer.plan(item())
        from research_os import writer as module
        original = module.atomic
        def fail_note(path, data, **kwargs):
            if Path(path).suffix == '.md':
                raise OSError('simulated disk error')
            return original(path, data, **kwargs)
        with patch.object(module, 'atomic', side_effect=fail_note):
            with self.assertRaises(OSError):
                self.writer.apply(plan, plan.approval_hash)
        self.assertIsNone(self.writer.entry(identity(item())))
        self.assertEqual(self.writer.recover(identity(item())), 'aborted-before-write')

    def test_logical_lock(self):
        with self.writer.lock():
            with self.assertRaises(SafetyError):
                with self.writer.lock():
                    pass

    def test_interruption_recovery(self):
        def crash():
            raise RuntimeError('simulated interruption after note replacement')
        with self.assertRaises(RuntimeError):
            self.write(after_note=crash)
        self.assertIsNone(self.writer.entry(identity(item())))
        self.assertEqual(self.writer.recover(identity(item())), 'finalized')
        self.assertEqual(self.write()['status'], 'unchanged')

    def test_recovery_refuses_external_modification(self):
        def crash():
            raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError):
            self.write(after_note=crash)
        p = self.root / 'Example.md'
        p.write_text(p.read_text() + 'external')
        with self.assertRaises(SafetyError):
            self.writer.recover(identity(item()))

    def test_backup_restore_round_trip(self):
        self.write()
        p = self.root / 'Example.md'
        original = p.read_bytes()
        updated = self.write(item(title='Updated', version=2))
        self.assertEqual(Path(updated['backup']).read_bytes(), original)
        self.writer.restore(identity(item()), updated['backup'], digest(p.read_bytes()))
        self.assertEqual(p.read_bytes(), original)
        self.assertEqual(self.writer.entry(identity(item()))['sync_status'], 'restored_requires_revalidation')

    def test_registry_partition(self):
        self.write()
        self.writer.server_id = 'different-instance'
        with self.assertRaises(SafetyError):
            self.writer.plan(item())

    def test_new_note_concurrent_create_not_overwritten(self):
        plan = self.writer.plan(item())
        p = self.root / plan.relative_path
        p.parent.mkdir(parents=True)
        p.write_text('created by another app')
        with self.assertRaises(SafetyError):
            self.writer.apply(plan, plan.approval_hash)
        self.assertEqual(p.read_text(), 'created by another app')


class CacheAndIdentityTests(unittest.TestCase):
    def test_cache_invalidates_pdf_parser_and_analysis_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp) / 'cache')
            args = ('user:42:ABCDEFGH', 'hash1', 'parser1', {'mode': 'quick'})
            cache.put(*args, {'facts': []})
            self.assertEqual(cache.get(*args), {'facts': []})
            self.assertIsNone(cache.get(args[0], 'hash2', args[2], args[3]))
            self.assertIsNone(cache.get(args[0], args[1], 'parser2', args[3]))
            self.assertIsNone(cache.get(args[0], args[1], args[2], {'mode': 'deep'}))

    def test_cache_corruption_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp) / 'cache')
            args = ('user:42:ABCDEFGH', 'hash', 'parser1', {})
            path = cache.put(*args, {'fact': 'old'})
            data = json.loads(path.read_text())
            data['payload']['fact'] = 'changed'
            path.write_text(json.dumps(data))
            with self.assertRaises(SafetyError):
                cache.get(*args)

    def test_doi_candidate_does_not_merge(self):
        result = duplicate_candidates('https://doi.org/10.1234/EXAMPLE', [item()])
        self.assertEqual(result[0]['action'], 'review_existing')

    def test_group_identity_and_link(self):
        group = item()
        group['library'] = {'type': 'group', 'id': 42}
        self.assertNotEqual(identity(group), identity(item()))
        self.assertIn('zotero://select/groups/42/items/ABCDEFGH', note(group))

    def test_empty_pdf_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'empty.pdf'
            p.touch()
            self.assertFalse(pdf_info(p)['header_valid'])

    def test_nonloopback_endpoint_rejected(self):
        with self.assertRaises(SafetyError):
            Zotero({'api': {'base_url': 'http://example.com/api/'}})

    def test_instance_precondition_failure_is_safe(self):
        import urllib.error
        client = Zotero({'api': {'base_url': 'http://127.0.0.1:23119/api/',
                                 'expected_server_id': 'old'}, 'library': {'type': 'user', 'id': 42}})
        with patch.object(client.opener, 'open', side_effect=urllib.error.HTTPError('url', 412, 'Conflict', {}, None)):
            with self.assertRaisesRegex(SafetyError, 'HTTP 412'):
                client.get()


if __name__ == '__main__':
    unittest.main()
