"""Descriptor-relative reads against temporary synthetic files only."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_reads import PAGE_BYTES, SOURCE_FILES, query


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

    def query(self, value='hello.txt', operation='read_text', **range_args):
        return json.loads(query(dict(root=str(self.workspace), value=value, operation=operation,
                                     device=self.identity.st_dev, inode=self.identity.st_ino,
                                     private_prefixes=[str(self.private)], **range_args)))

    def page(self, **kwargs):
        return self.query(operation='read_text_range', **kwargs)

    def test_range_chinese_crlf_cr_and_empty_snapshot(self):
        for data, lines in [(b'', []), ('中文\r\n尾行\r第三\n无尾换行'.encode(),
                                        ['中文\r\n', '尾行\r', '第三\n', '无尾换行'])]:
            with self.subTest(data=data):
                (self.workspace / 'hello.txt').write_bytes(data)
                first = self.page(max_lines=2)
                self.assertEqual(first['text'], ''.join(lines[:2]))
                self.assertEqual(first['start_line'], 1)
                self.assertEqual(first['end_line'], min(2, len(lines)))
                self.assertEqual(first['total_lines'], len(lines))
                self.assertEqual(first['eof'], len(lines) <= 2)
                self.assertNotIn('sha256', first)
                self.assertEqual(first['fragment_sha256'], hashlib.sha256(first['text'].encode()).hexdigest())
                if len(lines) > 2:
                    second = self.page(start_line=first['next_line'], max_lines=2,
                                       expected_version=first['file_version'])
                    self.assertEqual(second['file_version'], first['file_version'])
                    self.assertEqual(second['text'], ''.join(lines[2:]))
                    self.assertTrue(second['eof'])
                    self.assertIsNone(second['next_line'])

    def test_range_defaults_limits_and_unread_remainder(self):
        (self.workspace / 'hello.txt').write_text('a\n' * 201)
        first = self.page()
        self.assertEqual((first['end_line'], first['next_line'], first['eof']), (100, 101, False))
        maximum = self.page(max_lines=200)
        self.assertEqual((maximum['end_line'], maximum['next_line']), (200, 201))
        last = self.page(start_line=201, expected_version=maximum['file_version'])
        self.assertEqual(last['text'], 'a\n')
        self.assertTrue(last['eof'])
        for kwargs in ({'start_line': 0}, {'start_line': -1}, {'start_line': True}, {'start_line': 1.5},
                       {'max_lines': 0}, {'max_lines': 201}, {'max_lines': True}, {'max_lines': '2'},
                       {'start_line': 2}, {'expected_version': 'not-a-version'},
                       {'start_line': 202, 'expected_version': first['file_version']}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.page(**kwargs)

    def test_range_response_limit_counts_serialized_utf8_and_never_splits_lines(self):
        data = ('中文\\"\t' * 200 + '\r\n').encode() * 40
        (self.workspace / 'hello.txt').write_bytes(data)
        pages, recovered, args = [], b'', {}
        while True:
            raw = query(dict(root=str(self.workspace), value='hello.txt', operation='read_text_range',
                             device=self.identity.st_dev, inode=self.identity.st_ino,
                             private_prefixes=[], **args))
            self.assertLessEqual(len(raw.encode()), PAGE_BYTES)
            page = json.loads(raw)
            pages.append(page)
            recovered += page['text'].encode()
            self.assertTrue(page['text'].endswith('\r\n'))
            if page['eof']:
                break
            args = dict(start_line=page['next_line'], expected_version=page['file_version'])
        self.assertGreater(len(pages), 1)
        self.assertEqual(recovered, data)

    def test_range_exact_response_boundary(self):
        target = self.workspace / 'hello.txt'
        size = 16000
        for _ in range(4):
            target.write_bytes(b'x' * size)
            page = self.page()
            difference = PAGE_BYTES - len(json.dumps(page, ensure_ascii=False).encode())
            if difference == 0:
                break
            size += difference
        self.assertEqual(len(json.dumps(page, ensure_ascii=False).encode()), PAGE_BYTES)
        target.write_bytes(b'x' * (size + 1))
        with self.assertRaisesRegex(ValueError, 'single line'):
            self.page()

    def test_range_rejects_long_line_binary_invalid_utf8_and_large_file(self):
        for data in (b'a' * PAGE_BYTES, b'\t' * 9000, b'\0', b'good\n\xff', b'a\n' * 524289):
            (self.workspace / 'hello.txt').write_bytes(data)
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                self.page(max_lines=1)
        # A long later line is refused on its page, not silently omitted.
        (self.workspace / 'hello.txt').write_bytes(b'ok\n' + b'a' * PAGE_BYTES)
        first = self.page()
        self.assertEqual(first['text'], 'ok\n')
        self.assertFalse(first['eof'])
        with self.assertRaisesRegex(ValueError, 'single line'):
            self.page(start_line=2, expected_version=first['file_version'])

    def test_range_accepts_exact_one_mib_and_checks_growth_after_stat(self):
        file = self.workspace / 'hello.txt'
        file.write_bytes(b'a\n' * 524288)
        self.assertEqual(self.page()['total_lines'], 524288)
        file.write_bytes(b'a\n' * 524289)
        original = os.fstat

        def stale_size(descriptor):
            info = original(descriptor)
            if info.st_size > 1048576:
                fields = list(info)
                fields[6] = 0
                return os.stat_result(fields)
            return info

        with mock.patch('agent_reads.os.fstat', side_effect=stale_size), self.assertRaises(ValueError):
            self.page()

    def test_range_version_rejects_edits_replacement_and_cross_file_tokens(self):
        file = self.workspace / 'hello.txt'
        file.write_bytes(b'a\nb\n')
        version = self.page(max_lines=1)['file_version']
        (self.workspace / 'other.txt').write_bytes(file.read_bytes())
        with self.assertRaisesRegex(ValueError, 'changed between pages'):
            self.query('other.txt', 'read_text_range', start_line=2, expected_version=version)
        before = file.stat()
        file.write_bytes(b'c\nd\n')
        os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'changed between pages'):
            self.page(start_line=2, expected_version=version)
        version = self.page()['file_version']
        file.rename(self.workspace / 'old.txt')
        file.write_bytes(b'c\nd\n')
        with self.assertRaisesRegex(ValueError, 'changed between pages'):
            self.page(start_line=2, expected_version=version)

    def test_range_detects_mutation_during_read(self):
        file = self.workspace / 'hello.txt'
        original = os.fstat
        calls = 0

        def mutate(descriptor):
            nonlocal calls
            info = original(descriptor)
            calls += 1
            if calls == 2:
                file.write_text('changed during bounded read')
            return info

        with mock.patch('agent_reads.os.fstat', side_effect=mutate), self.assertRaisesRegex(ValueError, 'changed during'):
            self.page()

    def test_range_reuses_private_symlink_project_and_traversal_guards(self):
        (self.workspace / 'link').symlink_to(self.workspace / 'hello.txt')
        (self.workspace / 'parent').symlink_to(self.private, target_is_directory=True)
        for value in ('link', 'parent/message.txt', 'agent/message.txt', '../outside', '.env', '/etc/hosts'):
            with self.subTest(value=value), self.assertRaises((ValueError, OSError)):
                self.query(value, 'read_text_range')
        self.workspace.rename(self.root / 'old')
        self.workspace.mkdir()
        with self.assertRaises(ValueError):
            self.page()

    def test_source_pilot_is_exact_allowlist_for_reads_and_listings(self):
        (self.workspace / 'README.md').write_text('synthetic source\n')
        server = self.workspace / 'server/plugins/agentbridge_mac'
        server.mkdir(parents=True)
        (server / 'main.py').write_text('# synthetic code')
        (server / 'config.yaml').write_text('synthetic private marker')
        for name in ('runtime.yaml', 'chat.jsonl', 'raw.log', 'agentbridge.db', 'unreviewed.py'):
            (self.workspace / name).write_text('synthetic private marker')
        with mock.patch('agent_reads.SOURCE_PROJECT', str(self.workspace)):
            self.assertEqual([row['name'] for row in self.query('.', 'list_files')], ['README.md', 'server'])
            self.assertEqual(self.query('README.md', 'read_text_range')['text'], 'synthetic source\n')
            self.assertEqual(self.query('server/plugins/agentbridge_mac/main.py')['text'], '# synthetic code')
            for name in ('hello.txt', 'runtime.yaml', 'chat.jsonl', 'raw.log', 'agentbridge.db', 'unreviewed.py',
                         'server/plugins/agentbridge_mac/config.yaml', '/etc/hosts'):
                for operation in ('read_text', 'read_text_range'):
                    with self.subTest(name=name, operation=operation), self.assertRaises(ValueError):
                        self.query(name, operation)
            (self.workspace / 'README.md').unlink()
            (self.workspace / 'README.md').symlink_to(self.workspace / 'hello.txt')
            with self.assertRaises(OSError):
                self.query('README.md', 'read_text_range')
        self.assertNotIn('runtime.yaml', SOURCE_FILES)

    def test_regular_text_and_listing(self):
        self.assertEqual(self.query()['text'], 'synthetic readable text')
        self.assertEqual(len(self.query()['sha256']), 64)
        rows = self.query('.', 'list_files')
        self.assertEqual([row['name'] for row in rows], ['hello.txt'])

    def test_metadata_matches_actual_utf8_bytes_without_newline_translation(self):
        for text in ('部署测试青竹', '部署测试青竹\n', 'a\r\nb\r', '', 'ASCII', '汉😀e\u0301', '\ufeff字'):
            with self.subTest(text=text):
                data = text.encode('utf-8')
                (self.workspace / 'hello.txt').write_bytes(data)
                self.assertEqual(self.query(), dict(text=text, sha256=hashlib.sha256(data).hexdigest(),
                                                   bytes=len(data), encoding='utf-8', unicode_codepoints=len(text),
                                                   ends_with_newline=text.endswith(('\n', '\r'))))

    def test_byte_count_is_not_stale_stat_size(self):
        original_stat = os.fstat

        def stale_size(descriptor):
            fields = list(original_stat(descriptor))
            fields[6] = 0
            return os.stat_result(fields)

        with mock.patch('agent_reads.os.fstat', side_effect=stale_size):
            self.assertEqual(self.query()['bytes'], len(b'synthetic readable text'))

    def test_invalid_utf8_is_not_reported_as_measured_text(self):
        (self.workspace / 'hello.txt').write_bytes(b'\xff')
        with self.assertRaises(UnicodeDecodeError):
            self.query()

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

    def test_hidden_auth_and_backup_directories_are_never_readable(self):
        for directory in ('.config/gh', '.local/share/agentbridge-releases', '.docker', '.kube'):
            path = self.workspace / directory
            path.mkdir(parents=True)
            (path / 'config.json').write_text('synthetic private marker')
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                self.query(directory + '/config.json')
        self.assertEqual([row['name'] for row in self.query('.', 'list_files')], ['hello.txt'])

    def test_home_library_is_outside_the_reviewed_project_scope(self):
        library = self.workspace / 'Library'
        library.mkdir()
        (library / 'config.json').write_text('synthetic private marker')
        with mock.patch('agent_reads.os.path.expanduser', return_value=str(library)), self.assertRaises(ValueError):
            self.query('Library/config.json')

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
