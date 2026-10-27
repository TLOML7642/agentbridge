"""Descriptor-relative reads against temporary synthetic files only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_reads import query


class ReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='agent-read-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.identity = self.workspace.stat()
        self.private = self.workspace / 'agent'
        self.private.mkdir()
        (self.workspace / 'hello.txt').write_text('synthetic readable text')
        (self.private / 'message.txt').write_text('synthetic private marker')

    def query(self, value='hello.txt', operation='read_text'):
        return json.loads(query(dict(root=str(self.workspace), value=value, operation=operation,
                                     device=self.identity.st_dev, inode=self.identity.st_ino,
                                     private_prefixes=[str(self.private)])))

    def test_regular_text_and_listing(self):
        self.assertEqual(self.query()['text'], 'synthetic readable text')
        self.assertEqual(len(self.query()['sha256']), 64)
        rows = self.query('.', 'list_files')
        self.assertEqual([row['name'] for row in rows], ['hello.txt'])

    def test_scope_traversal_and_private_directories_are_denied(self):
        for value in ('../outside.txt', '/private/etc/passwd', 'agent/message.txt'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.query(value)

    def test_credential_names_and_binary_or_large_files_are_denied(self):
        for name, data in [('.env', b'synthetic'), ('api-secret.txt', b'synthetic'), ('id_ed25519', b'synthetic'),
                           ('cache.db', b'synthetic'), ('binary', b'\0'), ('large', b'x' * 16385)]:
            (self.workspace / name).write_bytes(data)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.query(name)

    def test_leaf_and_parent_symlinks_are_not_followed(self):
        (self.workspace / 'link').symlink_to(self.workspace / 'hello.txt')
        (self.workspace / 'parent').symlink_to(self.private, target_is_directory=True)
        for value in ('link', 'parent/message.txt'):
            with self.subTest(value=value), self.assertRaises(OSError):
                self.query(value)
        self.assertEqual([row['name'] for row in self.query('.', 'list_files')], ['hello.txt'])

    def test_project_identity_is_rechecked_on_open(self):
        self.workspace.rename(self.root / 'old-workspace')
        self.workspace.mkdir()
        (self.workspace / 'hello.txt').write_text('not the bound project')
        with self.assertRaises(ValueError):
            self.query()

    def test_parent_replaced_after_open_cannot_redirect_leaf_read(self):
        child = self.workspace / 'child'
        child.mkdir()
        (child / 'message.txt').write_text('synthetic original')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'message.txt').write_text('synthetic private marker')
        original_open = os.open

        def replace_after_open(file, flags, *args, **kwargs):
            descriptor = original_open(file, flags, *args, **kwargs)
            if file == 'child':
                child.rename(self.workspace / 'old-child')
                child.symlink_to(outside, target_is_directory=True)
            return descriptor

        with mock.patch('agent_reads.os.open', side_effect=replace_after_open):
            self.assertEqual(self.query('child/message.txt')['text'], 'synthetic original')

    def test_special_files_are_rejected_without_blocking(self):
        os.mkfifo(self.workspace / 'pipe')
        with self.assertRaises(ValueError):
            self.query('pipe')

    def test_growth_after_stat_still_has_hard_read_limit(self):
        (self.workspace / 'growing').write_bytes(b'x' * 16385)
        original_stat = os.fstat

        def stale_size(descriptor):
            value = original_stat(descriptor)
            if value.st_size == 16385:
                fields = list(value)
                fields[6] = 0
                return os.stat_result(fields)
            return value

        with mock.patch('agent_reads.os.fstat', side_effect=stale_size), self.assertRaises(ValueError):
            self.query('growing')


if __name__ == '__main__':
    unittest.main()
