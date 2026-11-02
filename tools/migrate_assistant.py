"""Private stopped-writer snapshots and additive assistant imports, never deployment."""
import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DATABASES = ('agentbridge.db', 'agent/conversations.db', 'agent/services/services.db',
             'agent/tasks/tasks.db', 'agent/tasks/approval/approvals.db')
TREES = ('agent/native/sessions', 'agent/native/receipts', 'agent/personas', 'agent/tasks/native/receipts')
LOCKS = ('agent/coordinator.lock', 'agent/tasks/approval/approval-authority.lock')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def private(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or path.is_symlink() or any(p.is_symlink() for p in path.parents)):
        raise ValueError('source must be an owned private regular file without symlink parents')


def sync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def connect(path, writable=False):
    db = sqlite3.connect(path if writable else path.as_uri() + '?mode=ro', uri=not writable)
    try:
        with db:
            yield db
    finally:
        db.close()


def quiescent(root):
    checks = ((DATABASES[0], 'jobs', 'status', ('queued', 'running', 'uncertain')),
              (DATABASES[1], 'messages', 'state', ('queued', 'running')),
              (DATABASES[3], 'tasks', 'state', ('queued', 'running', 'uncertain')))
    for name, table, column, states in checks:
        path = root / name
        if not path.exists():
            continue
        with connect(path) as db:
            slots = ','.join('?' for _ in states)
            if db.execute(f'SELECT count(*) FROM {table} WHERE {column} IN ({slots})', states).fetchone()[0]:
                raise ValueError('source has active or unresolved executions; reconcile first')


def snapshot(source, destination, writer_pid, configs=()):
    source, destination = Path(source), Path(destination)
    if not source.is_absolute() or source.is_symlink() or not destination.is_absolute():
        raise ValueError('explicit absolute non-symlink source and destination required')
    if destination == ROOT or ROOT in destination.parents or source == destination or source in destination.parents:
        raise ValueError('backup must be private and outside source/repository')
    if type(writer_pid) is not int or writer_pid <= 1:
        raise ValueError('stopped writer PID required')
    try:
        os.kill(writer_pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise ValueError('writer PID still exists; no snapshot')
    os.umask(0o077)
    with ExitStack() as stack:
        for name in LOCKS:
            path = source / name
            if path.exists():
                private(path)
                lease = stack.enter_context(open(path, 'rb'))
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        quiescent(source)
        files = [source / name for name in DATABASES if (source / name).exists()]
        for name in TREES:
            directory = source / name
            if directory.is_symlink():
                raise ValueError('linked source tree')
            if directory.exists():
                for path in directory.rglob('*'):
                    if path.is_symlink():
                        raise ValueError('linked source history')
                    if path.is_file():
                        files.append(path)
        for path in files:
            private(path)
        before = {str(p.relative_to(source)): digest(p) for p in files}
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for path in files:
            name = str(path.relative_to(source))
            target = destination / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if name in DATABASES:
                with connect(path) as old, connect(target, writable=True) as new:
                    old.backup(new)
                    if new.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise ValueError('SQLite integrity failed')
            else:
                shutil.copyfile(path, target)
            target.chmod(0o600)
            sync(target)
        for index, path in enumerate(configs):
            path = Path(path)
            private(path)
            target = destination / 'private-config' / str(index)
            target.parent.mkdir(mode=0o700, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o600)
            sync(target)
        if before != {str(p.relative_to(source)): digest(p) for p in files}:
            raise ValueError('source changed during stopped-writer snapshot')
        inventory = {str(p.relative_to(destination)): digest(p) for p in destination.rglob('*') if p.is_file()}
        manifest = {'format': 1, 'created': time.time(), 'writer_pid_absent': writer_pid,
                    'source_files': before, 'files': inventory, 'source_overwritten': False,
                    'approval_policy': 'archived only; no valid authorization imported',
                    'reminders': 'remain on original server; not migrated or scheduled here'}
        output = destination / 'manifest.json'
        output.write_text(json.dumps(manifest, indent=2) + '\n')
        output.chmod(0o600)
        sync(output)
        for directory in sorted((p for p in destination.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            sync(directory)
        sync(destination)
        return manifest


def verify(snapshot_root):
    root = Path(snapshot_root)
    private(root / 'manifest.json')
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('unknown snapshot format')
    for name, expected in manifest['files'].items():
        path = root / name
        if not name or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('unsafe manifest path')
        private(path)
        if digest(path) != expected:
            raise ValueError('snapshot checksum mismatch')
    return manifest


def import_snapshot(snapshot_root, destination, sender_channels, source_projects=()):
    """Import only into a fresh rehearsal root. Preserve all native bytes and IDs."""
    sys.path.insert(0, str(ROOT))
    from assistant_projects import Projects
    source, target = Path(snapshot_root), Path(destination)
    manifest = verify(source)
    if not target.is_absolute() or target == ROOT or ROOT in target.parents:
        raise ValueError('private absolute destination required')
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in manifest['files']:
        if name == 'agent/conversations.db' or name == 'agent/services/services.db' or any(
                name.startswith(tree + '/') for tree in TREES[:3]):
            output = target / name
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source / name, output)
            output.chmod(0o600)
    # Archived task/approval databases deliberately stay out of the live root.
    # Existing task executor authority remains on Mac and revokes on restart.
    projects = Projects(target)
    projects.bind('assistant')
    with connect(source / 'agentbridge.db') as db:
        for name, cwd in db.execute('SELECT name,dir FROM projects'):
            projects.bind(name, cwd, 'source_snapshot' if name in source_projects else 'mac')
        selections = dict(db.execute('SELECT channel,current_project FROM channels'))
    for sender, channel in sender_channels:
        if channel in selections:
            projects.use(sender, channel, selections[channel], 'migration-project-selection')
    with connect(target / 'agent/conversations.db', writable=True) as db:
        # Unresolved service intents are retained for reconciliation, never replayed.
        db.execute('PRAGMA integrity_check')
        heads = db.execute('SELECT sender,channel,project,count(DISTINCT session) FROM heads GROUP BY sender,channel,project').fetchall()
        if any(row[3] != 1 for row in heads):
            raise ValueError('ambiguous native conversation heads; explicit reconciliation required')
    if (target / 'agent/services/services.db').exists():
        with connect(target / 'agent/services/services.db', writable=True) as db:
            db.execute("UPDATE service_requests SET state='unknown' WHERE state='pending'")
    result = {'format': 1, 'imported_files': sum(1 for p in target.rglob('*') if p.is_file()),
              'original_history_rewritten': False, 'approvals_imported_as_valid': False,
              'unaccepted_messages_replayed': False, 'production_switched': False}
    (target / 'migration-result.json').write_text(json.dumps(result, indent=2) + '\n')
    sync(target)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--stopped-writer-pid', required=True, type=int)
    parser.add_argument('--config', action='append', default=[], type=Path)
    args = parser.parse_args()
    result = snapshot(args.source, args.destination, args.stopped_writer_pid, args.config)
    print(json.dumps({'snapshot_files': len(result['files']), 'verified': verify(args.destination) == result,
                      'source_overwritten': False, 'production_switched': False}))


if __name__ == '__main__':
    main()
