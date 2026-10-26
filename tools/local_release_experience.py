"""Operate only the existing worker launchd job; preserve live SQLite on rollback."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys

HOME = Path.home()
ROOT = HOME / 'agentbridge'
BACKUP = HOME / '.local/share/agentbridge-releases/20261026-stability-experience'
PLIST = HOME / 'Library/LaunchAgents/com.agentbridge.worker.plist'
LABEL = f'gui/{os.getuid()}/com.agentbridge.worker'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def quiescent():
    with sqlite3.connect('file:' + str(ROOT / 'agentbridge.db') + '?mode=ro', uri=True) as db:
        count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        assert count == 0, 'Worker has active jobs; do not restart or change cancellation policy'


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


def baseline():
    meta = json.loads((BACKUP / 'pre-maintenance-local.json').read_text())
    old = (BACKUP / PLIST.name).read_bytes()
    assert digest(old) == meta['launchd_sha256'], 'Launchd rollback baseline changed'
    config = plistlib.loads(old)
    enabled = plistlib.loads(old)
    enabled.setdefault('EnvironmentVariables', {})['AGENTBRIDGE_RUNNING_CANCEL'] = '1'
    assert plistlib.loads(PLIST.read_bytes()) in (config, enabled), 'Concurrent launchd configuration edit'
    return meta, old, enabled


def restart():
    quiescent()
    baseline()
    run('launchctl', 'kickstart', '-k', LABEL)
    print(json.dumps({'existing_worker_restarted': True}))


def configure_cancel(enabled):
    quiescent()
    meta, old, config = baseline()
    before = PLIST.read_bytes()
    run('launchctl', 'bootout', LABEL)
    try:
        PLIST.write_bytes(plistlib.dumps(config) if enabled else old)
        PLIST.chmod(meta['launchd_mode'])
        run('launchctl', 'bootstrap', f'gui/{os.getuid()}', str(PLIST))
    except BaseException:
        PLIST.write_bytes(before)
        PLIST.chmod(meta['launchd_mode'])
        run('launchctl', 'bootstrap', f'gui/{os.getuid()}', str(PLIST))
        raise
    print(json.dumps({'running_cancel_enabled': enabled, 'same_launchd_label': True}))


def rollback():
    quiescent()
    meta, old_plist, _ = baseline()
    before = json.loads((BACKUP / 'manifest.json').read_text())
    source = BACKUP / 'worker.py'
    assert digest(source.read_bytes()) == before['worker.py']['sha256']
    current = digest((ROOT / 'worker.py').read_bytes())
    assert current in (meta['worker_source_sha256'], before['worker.py']['sha256']), 'Concurrent worker source edit'
    run('launchctl', 'bootout', LABEL)
    (ROOT / 'worker.py').write_bytes(source.read_bytes())
    (ROOT / 'worker.py').chmod(before['worker.py']['mode'])
    PLIST.write_bytes(old_plist)
    PLIST.chmod(meta['launchd_mode'])
    run('launchctl', 'bootstrap', f'gui/{os.getuid()}', str(PLIST))
    print(json.dumps({'worker_source_rolled_back': True, 'database_restored': False}))


if __name__ == '__main__':
    {'restart': restart, 'enable-cancel': lambda: configure_cancel(True),
     'disable-cancel': lambda: configure_cancel(False), 'rollback': rollback}[sys.argv[1]]()
