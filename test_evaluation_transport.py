"""Synthetic transport diagnostics; never contact a real provider."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.evaluation_transport import TransportTrace


class TransportTraceTests(unittest.TestCase):
    def test_new_requests_have_distinct_correlation_ids(self):
        first, second = TransportTrace(), TransportTrace()
        self.assertNotEqual(first.data['request_id'], second.data['request_id'])
        self.assertEqual(len(first.data['request_id']), 32)

    def test_success_counts_and_timing(self):
        clock = mock.Mock(side_effect=[10, 12.3456])
        trace = TransportTrace(clock)
        trace.headers(200, 'upstream-canary')
        trace.received(100)
        trace.stage('downstream_write')
        trace.sent(90)
        trace.sse()
        trace.sse(terminal=True)
        result = trace.finish()
        self.assertEqual(result['http_status'], 200)
        self.assertEqual(result['upstream_bytes'], 100)
        self.assertEqual(result['downstream_bytes'], 90)
        self.assertEqual(result['sse_events'], 2)
        self.assertTrue(result['terminal_seen'])
        self.assertEqual(result['stage'], 'complete')
        self.assertEqual(result['elapsed_seconds'], 2.346)

    def test_timeout_retains_failure_stage_not_error_text(self):
        trace = TransportTrace()
        trace.headers(200)
        trace.received(5)
        result = trace.finish(TimeoutError('secret-key and response body'))
        self.assertEqual(result['exception_type'], 'TimeoutError')
        self.assertEqual(result['stage'], 'upstream_read')
        self.assertNotIn('secret', json.dumps(result))
        self.assertFalse(result['terminal_seen'])

    def test_client_disconnect_is_not_upstream_failure(self):
        trace = TransportTrace()
        trace.headers(200)
        trace.stage('downstream_write')
        result = trace.finish(BrokenPipeError('private response'))
        self.assertEqual(result['stage'], 'downstream_write')
        self.assertEqual(result['exception_type'], 'BrokenPipeError')

    def test_request_header_is_hashed_never_logged(self):
        trace = TransportTrace()
        value = 'private-header\r\nsecret'
        trace.headers(429, value)
        result = trace.finish(OSError(value))
        self.assertEqual(result['upstream_request_id_sha256'],
                         hashlib.sha256(value.encode()).hexdigest())
        self.assertNotIn(value, json.dumps(result))
        self.assertEqual(result['http_status'], 429)

    def test_parse_failure_is_separate_from_io(self):
        trace = TransportTrace()
        trace.stage('stream_parse')
        result = trace.finish(ValueError('private malformed SSE body'))
        self.assertEqual(result['stage'], 'stream_parse')
        self.assertEqual(result['exception_type'], 'ValueError')
        self.assertNotIn('malformed', json.dumps(result))

    def test_evidence_collision_is_not_a_provider_failure(self):
        trace = TransportTrace()
        trace.headers(200)
        trace.stage('evidence_open')
        result = trace.finish(FileExistsError('private evidence path'))
        self.assertEqual(result['stage'], 'evidence_open')
        self.assertEqual(result['exception_type'], 'FileExistsError')
        self.assertNotIn('private evidence', json.dumps(result))

    def test_inflight_requests_do_not_share_evidence_filenames(self):
        traces = [TransportTrace() for _ in range(30)]
        with tempfile.TemporaryDirectory() as directory:
            # Both title and streaming requests may be in flight before either
            # upstream returns. Evidence IDs must be fixed on request arrival.
            def write(trace):
                with trace.open_evidence(directory) as out:
                    out.write(b'synthetic')
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(write, reversed(traces)))
            self.assertEqual(len(list(Path(directory).iterdir())), len(traces))

    def test_existing_evidence_is_never_overwritten(self):
        trace = TransportTrace()
        with tempfile.TemporaryDirectory() as directory:
            with trace.open_evidence(directory) as out:
                out.write(b'original failure')
            with self.assertRaises(FileExistsError):
                trace.open_evidence(directory)
            self.assertEqual(next(Path(directory).iterdir()).read_bytes(), b'original failure')

    def test_unknown_stages_fail_closed(self):
        trace = TransportTrace()
        with self.assertRaises(ValueError):
            trace.stage('body-with-secret')


if __name__ == '__main__':
    unittest.main()
