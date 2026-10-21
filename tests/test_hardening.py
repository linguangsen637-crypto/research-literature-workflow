import dataclasses
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from research_os.core import SafetyError, atomic, digest
from research_os.securefs import read, directory, attached
from research_os.writer import SafeWriter, identity
from test_safety import item


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'vault/Literature'
        self.writer = SafeWriter(self.root, self.base / 'state', self.base / 'backup', enabled=True,
                                 mode='reviewed_update', api_origin='local', server_id='test')

    def write(self, paper=None):
        plan = self.writer.plan(paper or item())
        return self.writer.apply(plan, plan.approval_hash)

    def test_create_only_rejects_update(self):
        self.writer.mode = 'create_only'
        self.write()
        with self.assertRaises(SafetyError):
            self.write(item(title='updated'))

    def test_knowledge_document_preserves_user_notes(self):
        p=self.writer.document_plan('topic-test','Topics/Test.md','Test','Machine content')
        self.writer.apply(p,p.approval_hash)
        target=self.root/'Topics/Test.md';target.write_text(target.read_text()+'Human notes\n')
        update=self.writer.document_plan('topic-test','Topics/Test.md','Test','Changed machine content')
        self.writer.apply(update,update.approval_hash)
        self.assertTrue(target.read_text().endswith('Human notes\n'))

    def test_analysis_survives_metadata_refresh(self):
        self.write()
        from research_os.writer import END
        target=self.root/'Example.md'
        plan=self.writer.plan(item())
        content=plan.content.replace(END,'\n<!-- research-os:analysis:start -->\nRetained analysis'+END)
        updated=dataclasses.replace(plan,content=content)
        self.writer.apply(updated,updated.approval_hash)
        refreshed=self.writer.plan(item(title='New metadata title'))
        self.assertIn('Retained analysis',refreshed.content)

    def test_verified_pdf_hash_reaches_registry(self):
        plan = dataclasses.replace(self.writer.plan(item()), pdf_sha256=digest(b'PDF fixture'))
        self.writer.apply(plan, plan.approval_hash)
        self.assertEqual(self.writer.entry(identity(item()))['pdf_sha256'], digest(b'PDF fixture'))

    def test_preview_cannot_be_reused_in_another_root(self):
        plan = self.writer.plan(item())
        self.writer.root = self.base / 'another-root'
        with self.assertRaisesRegex(SafetyError, 'environment'):
            self.writer.apply(plan, plan.approval_hash)

    def test_symlink_in_atomic_parent_is_rejected(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (self.base / 'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            atomic(self.base / 'link/note.md', b'bad')
        self.assertEqual(list(outside.iterdir()), [])

    def test_target_symlink_is_rejected(self):
        outside = self.base / 'original'
        outside.write_bytes(b'keep')
        target = self.base / 'link'
        target.symlink_to(outside)
        with self.assertRaises(OSError):
            atomic(target, b'overwrite')
        self.assertEqual(outside.read_bytes(), b'keep')

    def test_hardlink_is_rejected(self):
        original = self.base / 'original'
        original.write_bytes(b'keep')
        target = self.base / 'link'
        os.link(original, target)
        with self.assertRaises(SafetyError):
            atomic(target, b'overwrite')
        self.assertEqual(original.read_bytes(), b'keep')

    def test_atomic_expected_hash(self):
        target = self.base / 'file'
        atomic(target, b'new', create_only=True)
        with self.assertRaises(SafetyError):
            atomic(target, b'overwrite', expected=digest(b'old'))
        self.assertEqual(read(target), b'new')

    def test_directory_replacement_during_publication_is_detected(self):
        parent = self.base / 'parent'
        parent.mkdir()
        outside = self.base / 'outside'
        outside.mkdir()
        import research_os.securefs as fs
        original_attached = fs.attached
        def swap(path, fd):
            parent.rename(self.base / 'displaced')
            parent.symlink_to(outside, target_is_directory=True)
            original_attached(path, fd)
        with patch.object(fs, 'attached', side_effect=swap):
            with self.assertRaises(OSError):
                atomic(parent / 'test.md', b'unsafe')
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list((self.base / 'displaced').iterdir()), [])

    def test_backup_tampering_refused(self):
        self.write()
        result = self.write(item(title='New', version=2))
        backup = Path(result['backup'])
        backup.write_bytes(backup.read_bytes() + b'tampered')
        target = Path(result['path'])
        before = target.read_bytes()
        with self.assertRaisesRegex(SafetyError, 'manifest'):
            self.writer.restore(identity(item()), backup, digest(before))
        self.assertEqual(target.read_bytes(), before)

    def test_missing_manifest_refused(self):
        self.write()
        result = self.write(item(title='New'))
        Path(result['backup'] + '.manifest.json').unlink()
        with self.assertRaises(SafetyError):
            self.writer.restore(identity(item()), result['backup'], digest(Path(result['path']).read_bytes()))

    def test_events_record_commit_and_failure(self):
        self.write()
        plan = self.writer.plan(item(title='New'))
        with self.assertRaises(SafetyError):
            self.writer.apply(plan, 'wrong-approval')
        events = [json.loads(p.read_text()) for p in (self.base / 'state/events').glob('*.json')]
        self.assertIn('committed', [x['status'] for x in events])
        self.assertIn('failed', [x['status'] for x in events])


if __name__ == '__main__':
    unittest.main()
