"""Allowlisted operational metadata only; unrelated history is never rotated."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import time


STAGES = frozenset(('rpc', 'native_rpc', 'guard', 'executor', 'receipt', 'terminal'))
CODES = frozenset(('OK', 'DENIED', 'TIMEOUT', 'CLOSED', 'CANCELLED', 'INVALID_FRAME',
                   'AUTH_FAIL', 'DB_BUSY', 'UNAVAILABLE', 'UNKNOWN', 'START', 'RECEIPT_MISSING'))
STATES = frozenset(('pending', 'approved', 'expired', 'revoked', 'denied', 'consumed',
                    'verified', 'process-ended', 'cancelled', 'uncertain', 'failed', 'running'))


class Diagnostics:
    def __init__(self, root, clock=time.time):
        self.root, self.clock = Path(root), clock
        if self.root.is_symlink():
            raise ValueError('diagnostic directory must not be a symlink')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.last_day, self.failed = None, False

    def maintain(self):
        try:
            self._prune()
        except OSError:
            self.failed = True
            sys.stderr.write('AGENTBRIDGE_DIAGNOSTICS_WRITE_FAILED\n')
        return self.status()

    def _prune(self):
        day = int(self.clock() // 86400)
        for path in self.root.iterdir():
            match = re.fullmatch(r'diagnostic-(\d{8})\.jsonl', path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                stamp = datetime.strptime(match[1], '%Y%m%d').replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            # Keep the complete boundary day, never shorten retention for size.
            if stamp + 86400 <= self.clock() - 30 * 86400:
                path.unlink()
        self.last_day = day

    def status(self):
        try:
            size = sum(p.stat().st_size for p in self.root.glob('diagnostic-????????.jsonl')
                       if p.is_file() and not p.is_symlink())
        except OSError:
            self.failed, size = True, None
        return {'retention_days': 30, 'bytes': size, 'capacity_warning': size is not None and size >= 200 * 1024 * 1024,
                'write_failed': self.failed, 'failure_evidence': 'private manual cleanup only'}

    def emit(self, stage, correlation='', code='UNKNOWN', state=None, exception=None, milliseconds=None, process=None):
        try:
            if int(self.clock() // 86400) != self.last_day:
                self.maintain()
            event = {'time': self.clock(), 'stage': stage if stage in STAGES else 'rpc',
                     'code': code if code in CODES else 'UNKNOWN'}
            if re.fullmatch('[a-f0-9]{32}', correlation or ''):
                event['correlation'] = correlation
            if state in STATES:
                event['state'] = state
            if exception is not None:
                event['exception_type'] = type(exception).__name__ if type(exception).__name__ in (
                    'AgentError', 'OperationalError', 'TimeoutError', 'ConnectionError', 'BrokenPipeError',
                    'ValueError', 'OSError', 'RuntimeError', 'KeyError', 'AttributeError', 'TypeError',
                    'PermissionError', 'JSONDecodeError', 'UnicodeDecodeError') else 'Exception'
            if isinstance(milliseconds, (int, float)) and 0 <= milliseconds <= 3600000:
                event['milliseconds'] = round(milliseconds, 3)
            if isinstance(process, dict):
                event['process'] = {key: process[key] for key in ('timed_out', 'cancelled', 'group_stopped')
                                    if type(process.get(key)) is bool}
                if type(process.get('exit_code')) is int and -255 <= process['exit_code'] <= 255:
                    event['process']['exit_code'] = process['exit_code']
            stamp = datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y%m%d')
            fd = os.open(self.root / ('diagnostic-' + stamp + '.jsonl'),
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError('unsafe diagnostic file')
                os.write(fd, (json.dumps(event, separators=(',', ':')) + '\n').encode())
            finally:
                os.close(fd)
            if self.status()['capacity_warning']:
                sys.stderr.write('AGENTBRIDGE_DIAGNOSTICS_CAPACITY_WARNING\n')
        except Exception:
            self.failed = True
            sys.stderr.write('AGENTBRIDGE_DIAGNOSTICS_WRITE_FAILED\n')
