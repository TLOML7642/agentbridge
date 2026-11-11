from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_diagnostics import Diagnostics, failure_metadata


class DiagnosticTests(unittest.TestCase):
    def test_failure_details_are_typed_allowlist_not_redacted_raw_error_text(self):
        private = 'synthetic-secret-message-and-path'
        details = {'stage': 'turn', 'model_code': 'SERVER', 'http_status': 500, 'turn_end': 'error',
                   'side_effect_attempted': False, 'inbox_pending': False, 'group_stopped': True,
                   'exit_code': 1, 'source_version': 'c' * 40, 'message': private, 'stack': private,
                   'stderr': private, 'api_key': private, 'error_type': private}
        sanitized = failure_metadata(details)
        self.assertEqual(sanitized['http_status'], 500)
        self.assertNotIn('error_type', sanitized)
        self.assertNotIn(private, json.dumps(sanitized))
        with tempfile.TemporaryDirectory() as root:
            diag = Diagnostics(root)
            diag.emit('terminal', 'a' * 32, 'MODEL_FAILED', state='failed', details=details)
            event = json.loads(next(diag.root.glob('*.jsonl')).read_text())
            self.assertEqual(event['details'], sanitized)
            self.assertEqual(event['code'], 'MODEL_FAILED')
        for value in ({'http_status': True, 'exit_code': True, 'milliseconds': float('nan'),
                       'model_code': private, 'stage': private, 'source_version': private}, None, private, []):
            self.assertEqual(failure_metadata(value), {})

    def test_health_metadata_remains_available_when_diagnostic_directory_is_unreadable(self):
        with tempfile.TemporaryDirectory() as root:
            diag = Diagnostics(root)
            with mock.patch.object(Path, 'iterdir', side_effect=PermissionError('synthetic private detail')), \
                    mock.patch.object(Path, 'glob', side_effect=PermissionError('synthetic private detail')), \
                    mock.patch('sys.stderr') as stderr:
                status = diag.maintain()
            self.assertTrue(status['write_failed'])
            self.assertIsNone(status['bytes'])
            stderr.write.assert_called_with('AGENTBRIDGE_DIAGNOSTICS_WRITE_FAILED\n')

    def test_metadata_is_allowlisted_and_never_contains_exception_text(self):
        with tempfile.TemporaryDirectory() as root:
            diag = Diagnostics(Path(root) / 'diagnostics')
            diag.emit('rpc', 'a' * 32, 'TIMEOUT', 'expired', ValueError('synthetic secret capability chat'), 1.25)
            diag.emit('untrusted stage', 'synthetic secret', 'synthetic secret', 'synthetic secret')
            diag.emit('executor', 'b' * 32, 'OK', process={'stdout': 'synthetic secret', 'stderr': 'synthetic secret',
                      'exit_code': 0, 'group_stopped': True, 'cancelled': False, 'timed_out': False})
            content = next(diag.root.glob('*.jsonl')).read_text()
            self.assertNotIn('synthetic secret', content)
            first, second, third = map(json.loads, content.splitlines())
            self.assertEqual(first['exception_type'], 'ValueError')
            self.assertEqual(first['correlation'], 'a' * 32)
            self.assertEqual(second['code'], 'UNKNOWN')
            self.assertNotIn('correlation', second)
            self.assertEqual(third['process'], {'exit_code': 0, 'group_stopped': True, 'cancelled': False, 'timed_out': False})
            self.assertEqual(next(diag.root.glob('*.jsonl')).stat().st_mode & 0o777, 0o600)

    def test_thirty_day_retention_preserves_boundary_and_unrelated_evidence(self):
        now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as root:
            diag = Diagnostics(root, clock=lambda: now)
            for name in ('diagnostic-20261003.jsonl', 'diagnostic-20261004.jsonl', 'failure-evidence.json',
                         'diagnostic-99999999.jsonl'):
                (Path(root) / name).write_text('{}')
            diag.maintain()
            self.assertFalse((Path(root) / 'diagnostic-20261003.jsonl').exists())
            self.assertTrue((Path(root) / 'diagnostic-20261004.jsonl').exists())
            self.assertTrue((Path(root) / 'failure-evidence.json').exists())
            self.assertTrue((Path(root) / 'diagnostic-99999999.jsonl').exists())

    def test_capacity_warning_does_not_shorten_retention(self):
        with tempfile.TemporaryDirectory() as root:
            diag = Diagnostics(root)
            name = datetime.now(timezone.utc).strftime('diagnostic-%Y%m%d.jsonl')
            file = Path(root) / name
            with file.open('wb') as output:
                output.truncate(200 * 1024 * 1024)
            file.chmod(0o600)
            with mock.patch('sys.stderr') as stderr:
                diag.emit('executor', 'a' * 32, 'START')
            self.assertTrue(diag.status()['capacity_warning'])
            self.assertGreaterEqual(file.stat().st_size, 200 * 1024 * 1024)
            stderr.write.assert_called_with('AGENTBRIDGE_DIAGNOSTICS_CAPACITY_WARNING\n')

    def test_symlink_is_not_followed_or_rotated_and_failure_does_not_change_task(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'outside'
            file.write_text('synthetic protected')
            diag = Diagnostics(Path(root) / 'diagnostics')
            name = datetime.now(timezone.utc).strftime('diagnostic-%Y%m%d.jsonl')
            (diag.root / name).symlink_to(file)
            with mock.patch('sys.stderr'):
                diag.emit('rpc', 'a' * 32, 'OK')
            self.assertTrue(diag.status()['write_failed'])
            self.assertEqual(file.read_text(), 'synthetic protected')
