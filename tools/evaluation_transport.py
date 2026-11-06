"""Body-free diagnostics for isolated evaluation relays, not production routing."""
import hashlib
from pathlib import Path
import time
import uuid


class TransportTrace:
    """Keep correlation and transport facts without exception text or headers."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._start = clock()
        self.data = dict(request_id=uuid.uuid4().hex, stage='upstream_connect',
                         http_status=None, upstream_request_id_sha256=None,
                         upstream_bytes=0, downstream_bytes=0, sse_events=0,
                         terminal_seen=False, exception_type=None)

    def headers(self, status, request_id=None):
        self.data['http_status'] = int(status)
        if request_id:
            self.data['upstream_request_id_sha256'] = hashlib.sha256(
                str(request_id).encode()).hexdigest()
        self.stage('upstream_read')

    def stage(self, name):
        if name not in ('upstream_connect', 'evidence_open', 'upstream_read', 'downstream_write',
                        'stream_parse', 'complete'):
            raise ValueError('unknown transport stage')
        self.data['stage'] = name

    def received(self, count):
        self.data['upstream_bytes'] += count

    def open_evidence(self, root):
        """Exclusively open a per-request file in an already-private test root."""
        self.stage('evidence_open')
        return (Path(root) / (self.data['request_id'] + '.raw')).open('xb')

    def sent(self, count):
        self.data['downstream_bytes'] += count

    def sse(self, terminal=False):
        self.data['sse_events'] += 1
        self.data['terminal_seen'] |= bool(terminal)

    def finish(self, error=None):
        if error is None:
            self.stage('complete')
        else:
            # Never retain str(error), repr(error), URLs, response bodies or keys.
            self.data['exception_type'] = type(error).__name__
        self.data['elapsed_seconds'] = round(self._clock() - self._start, 3)
        return dict(self.data)
