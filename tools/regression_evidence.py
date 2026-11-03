"""Synthetic-only unittest runner retaining failed fixtures before cleanup."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import unittest


def write_json(path, value):
    with path.open('w') as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, report, **kwargs):
        super().__init__(*args, **kwargs)
        self.report, self.roots, self.failed = report, [], False
        self.sequence = 0

    def register(self, path):
        path = Path(path).resolve()
        self.roots.append(path)
        with (self.report / 'fixture-paths.jsonl').open('a') as output:
            output.write(json.dumps({'test': self.test_id, 'path': str(path)}) + '\n')
            output.flush()
            os.fsync(output.fileno())

    def startTest(self, test):
        super().startTest(test)
        self.test_id, self.roots, self.failed = test.id(), [], False
        self.sequence += 1
        self.pending = self.report / ('fixture-%04d' % self.sequence)
        with (self.report / 'test-order.jsonl').open('a') as output:
            output.write(json.dumps({'index': self.sequence, 'test': self.test_id}) + '\n')
            output.flush()
            os.fsync(output.fileno())

    def snapshot(self, root):
        if root not in self.roots or not root.is_dir():
            return
        self.pending.mkdir(mode=0o700, exist_ok=True)
        target = self.pending / ('snapshot-%04d' % len(list(self.pending.iterdir())))
        target.mkdir(mode=0o700)
        manifest = {'test': self.test_id, 'root': str(root), 'files': {},
                    'consistency': 'per-SQLite backup, not a cross-database transaction'}
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
            for name in files:
                file = Path(base) / name
                relative = str(file.relative_to(root))
                try:
                    info = file.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if name.endswith(('-wal', '-shm', '-journal')):
                        continue
                    if info.st_size > 16 * 1024 * 1024:
                        raise ValueError('fixture file exceeds 16 MiB evidence limit')
                    dest = target / relative
                    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if name.endswith('.db'):
                        deadline = time.monotonic() + 2

                        def progress(*_args):
                            if time.monotonic() >= deadline:
                                raise TimeoutError('synthetic database backup busy')

                        source = sqlite3.connect(file.as_uri() + '?mode=ro', uri=True, timeout=.1)
                        backup = sqlite3.connect(dest)
                        try:
                            source.backup(backup, pages=64, progress=progress, sleep=.01)
                            integrity = backup.execute('PRAGMA integrity_check').fetchone()[0]
                            if integrity != 'ok':
                                raise ValueError('synthetic backup integrity failure')
                        finally:
                            backup.close()
                            source.close()
                    else:
                        shutil.copyfile(file, dest, follow_symlinks=False)
                    dest.chmod(0o600)
                    with dest.open('rb') as saved:
                        os.fsync(saved.fileno())
                    manifest['files'][relative] = {'sha256': hashlib.sha256(dest.read_bytes()).hexdigest()}
                except Exception as error:
                    manifest['files'][relative] = {'error_type': type(error).__name__}
        write_json(target / 'manifest.json', manifest)

    def addFailure(self, test, error):
        self.failed = True
        for root in self.roots:
            self.snapshot(root)
        super().addFailure(test, error)

    def addError(self, test, error):
        self.failed = True
        for root in self.roots:
            self.snapshot(root)
        super().addError(test, error)

    def addSubTest(self, test, subtest, error):
        if error is not None:
            self.failed = True
            for root in self.roots:
                self.snapshot(root)
        super().addSubTest(test, subtest, error)

    def stopTest(self, test):
        if not self.failed and self.pending.exists():
            # Only this runner's successful synthetic evidence, never failures.
            shutil.rmtree(self.pending)
        super().stopTest(test)


def run(report, modules):
    os.umask(0o077)
    report.mkdir(mode=0o700, parents=True, exist_ok=False)
    state = {}
    original_mkdtemp, original_cleanup = tempfile.mkdtemp, tempfile.TemporaryDirectory.cleanup

    def mkdtemp(*args, **kwargs):
        path = original_mkdtemp(*args, **kwargs)
        state['result'].register(path)
        return path

    def cleanup(directory):
        # Preserve evidence even when a with-block exits before an assertion.
        state['result'].snapshot(Path(directory.name).resolve())
        return original_cleanup(directory)

    def factory(*args, **kwargs):
        state['result'] = EvidenceResult(*args, report=report, **kwargs)
        return state['result']

    suite = (unittest.defaultTestLoader.discover('.', pattern='test_*.py') if modules == ['discover'] else
             unittest.defaultTestLoader.loadTestsFromNames(modules))
    tempfile.mkdtemp, tempfile.TemporaryDirectory.cleanup = mkdtemp, cleanup
    try:
        result = unittest.TextTestRunner(resultclass=factory).run(suite)
        write_json(report / 'summary.json', {'tests': result.testsRun, 'failures': len(result.failures),
                                            'errors': len(result.errors), 'skipped': len(result.skipped)})
        return 0 if result.wasSuccessful() and not result.skipped else 1
    finally:
        tempfile.mkdtemp, tempfile.TemporaryDirectory.cleanup = original_mkdtemp, original_cleanup


def preserve_interrupted(report):
    """Called by the parent before its isolated scratch tree is removed."""
    registry = report / 'fixture-paths.jsonl'
    if not registry.exists():
        return
    result = EvidenceResult(io.StringIO(), True, 1, report=report)
    result.pending = report / 'interrupted'
    rows = [json.loads(line) for line in registry.read_text().splitlines()]
    result.roots = [Path(row['path']) for row in rows]
    for row in rows:
        result.test_id = row['test']
        result.snapshot(Path(row['path']))


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path.cwd()))
    sys.exit(run(Path(sys.argv[1]), sys.argv[2:]))
