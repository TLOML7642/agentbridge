"""Narrow server release operations for the explicitly authorized maintenance window.

Run as root from the private staging directory. No password, SSH policy, network,
image update, chat data deletion, or worker task execution operations are provided.
"""
from contextlib import closing
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path('/opt/agentbridge')
RELEASE_ID = os.environ.get('AGENTBRIDGE_RELEASE_ID', '20261026-stability-experience')
assert re.fullmatch(r'[a-z0-9-]+', RELEASE_ID), 'Invalid release identifier'
STAGE = Path('/var/tmp/agentbridge-stability-experience') if RELEASE_ID == '20261026-stability-experience' else Path('/var/tmp/agentbridge-' + RELEASE_ID)
BACKUP = Path('/var/backups/agentbridge') / RELEASE_ID
MAPPING = {'shim.py': ROOT / 'shim/shim.py',
           'main.py': ROOT / 'astrbot/data/plugins/agentbridge_mac/main.py'}
EXPECTED = {
    'shim.py': 'e32a8f2b003084cb9fd42e230ccf7236f546a40ffa69206bd0d77d37eeec58bb',
    'main.py': '6f8dcf4d64b0dcda5bca49fee726dffc40c08a8217697b964d7fd73828343704',
}
PROTECTED = [ROOT / '.env', ROOT / 'astrbot/data/cmd_config.json']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=90, **kwargs).stdout.strip()


def containers():
    return json.loads(run(['docker', 'inspect', 'agentbridge-astrbot', 'agentbridge-shim']))


def runtime():
    rows = containers()
    env = dict(value.split('=', 1) for row in reversed(rows)
               for value in row['Config']['Env'] if '=' in value)
    cfg = json.loads((ROOT / 'astrbot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
    actors = [s.strip() for s in env.get('AGENTBRIDGE_ALLOWED_SENDERS', '').split(',') if s.strip()]
    assert actors, 'No explicitly allowed actor for release probes'
    channels = cfg.get('platform_settings', {}).get('id_whitelist', [])
    # Maintenance probes are not WeChat messages or notification subscriptions.
    channel = channels[0] if channels else 'agentbridge-release-readonly'
    return rows, env, {'X-AgentBridge-Sender': actors[0], 'X-AgentBridge-Channel': channel}


def manifest():
    return json.loads((BACKUP / 'manifest.json').read_text())


def invariant():
    before = manifest()
    assert all(digest(Path(p)) == value for p, value in before['protected'].items()), 'Credential/config changed concurrently'
    assert {row['Name']: row['Image'] for row in containers()} == before['images'], 'Image identity changed'


def prepare():
    assert not BACKUP.exists(), 'Do not overwrite an existing rollback baseline'
    assert all(digest(MAPPING[name]) == value for name, value in EXPECTED.items()), 'Source baseline changed'
    staged = json.loads((STAGE / 'sha256.json').read_text())
    for name in MAPPING:
        assert digest(STAGE / name) == staged[name]
        compile((STAGE / name).read_text(), name, 'exec')
    rows = containers()
    assert all(row['State']['Running'] for row in rows), 'Unexpected stopped component'
    astrbot = next(row for row in rows if row['Name'] == '/agentbridge-astrbot')
    env = dict(v.split('=', 1) for v in astrbot['Config']['Env'] if '=' in v)
    assert env.get('ASTRBOT_RESET_DASHBOARD_PASSWORD', '') not in ('1', 'true', 'True'), 'Password reset flag blocks recreation'
    BACKUP.mkdir(mode=0o700, parents=True)
    files = {}
    for path in [*MAPPING.values(), ROOT / 'compose.yaml']:
        dest = BACKUP / path.name
        shutil.copy2(path, dest)
        dest.chmod(0o600)
        st = path.stat()
        files[str(path)] = {'sha256': digest(path), 'mode': st.st_mode & 0o777, 'uid': st.st_uid, 'gid': st.st_gid, 'backup': dest.name}
    record = {'files': files, 'protected': {str(p): digest(p) for p in PROTECTED},
              'images': {row['Name']: row['Image'] for row in rows}, 'prepared_at': time.time()}
    (BACKUP / 'manifest.json').write_text(json.dumps(record, indent=2))
    print(json.dumps({'prepared': True, 'backup': str(BACKUP), 'source_hashes': EXPECTED}))


def pause():
    invariant()
    run(['docker', 'stop', '--time', '30', 'agentbridge-astrbot'])
    assert not containers()[0]['State']['Running']
    # Only bridge metadata databases, never chat history. Snapshot after intake stops.
    data = ROOT / 'astrbot/data/plugin_data/agentbridge_mac'
    for name in ('deliveries.db', 'notifications.db'):
        path = data / name
        if path.exists():
            with closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as src, \
                    closing(sqlite3.connect(BACKUP / name)) as dst:
                src.backup(dst)
    print(json.dumps({'intake_paused': True}))


def install():
    invariant()
    assert not containers()[0]['State']['Running'], 'Pause intake first'
    staged = json.loads((STAGE / 'sha256.json').read_text())
    before = manifest()
    for name, target in MAPPING.items():
        old = before['files'][str(target)]
        assert digest(target) == old['sha256'], 'Concurrent source edit'
        assert digest(STAGE / name) == staged[name], 'Stage changed'
        compile((STAGE / name).read_text(), name, 'exec')
    for name, target in MAPPING.items():
        old = before['files'][str(target)]
        # Keep the inode for the existing read-only shim bind mount.
        shutil.copyfile(STAGE / name, target)
        os.chmod(target, old['mode'])
        os.chown(target, old['uid'], old['gid'])
        assert digest(target) == staged[name]
    print(json.dumps({'installed': list(MAPPING), 'credentials_unchanged': True}))


def resume():
    invariant()
    run(['docker', 'restart', 'agentbridge-shim'])
    run(['docker', 'start', 'agentbridge-astrbot'])
    print(json.dumps({'resumed_existing_containers': True}))


def request(url, token, body=None, headers=None, timeout=12):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)


def probe():
    invariant()
    rows, env, headers = runtime()
    assert all(row['State']['Running'] and not row['State']['OOMKilled'] for row in rows)
    url = 'http://127.0.0.1:8787/v1/agentbridge/control'
    checks = {}
    for op in ('diag', 'jobs', 'pending', 'status'):
        status, data = request(url, env['SHIM_TOKEN'], {'op': op}, headers)
        assert status == 200 and data.get('ok'), op + ' failed'
        if op == 'diag':
            checks['running_cancel_enabled'] = data['running_cancel_enabled']
        checks[op] = True
    assert request(url, env['SHIM_TOKEN'], {'op': 'jobs'}, {})[0] == 403
    assert request(url, env['SHIM_TOKEN'], {'op': 'execute'}, headers)[0] == 400
    assert request('http://127.0.0.1:8787/health', '')[0] == 401
    result = {'utc_epoch': time.time(), 'checks': checks, 'credentials_unchanged': True,
              'notifications_enabled': env.get('AGENTBRIDGE_NOTIFICATIONS') == '1',
              'images_unchanged': True, 'phone_acceptance': 'not_performed'}
    # Counts only: no raw application logs or business data are printed.
    result['errors'] = {}
    for row in rows:
        logs = subprocess.run(['docker', 'logs', '--since', str(manifest()['prepared_at']), row['Name']],
                              capture_output=True, text=True, timeout=10)
        text = logs.stdout + logs.stderr
        result['errors'][row['Name']] = len(re.findall(r'\b(?:ERROR|ERRO|CRITICAL|Traceback)\b', text))
    assert not any(result['errors'].values()), 'New errors require inspection'
    print(json.dumps(result))


def readonly():
    _, env, headers = runtime()
    job_id = 'release-' + RELEASE_ID + '-readonly'
    headers.update({'X-AgentBridge-Job-Id': job_id, 'X-AgentBridge-Id-Stable': '1'})
    body = {'model': 'agentbridge', 'messages': [{'role': 'user', 'content':
            '只读核验：报告 macOS 系统类型和 /etc/hosts 的行数。仅查询，只返回这两项摘要，不输出文件正文。'}]}
    base = 'http://127.0.0.1:8787'
    # Exactly one initial submission; uncertain responses are reconciled, not retried.
    try:
        request(base + '/v1/chat/completions', env['SHIM_TOKEN'], body, headers, timeout=150)
    except OSError:
        pass
    deadline = time.monotonic() + 180
    final = None
    while time.monotonic() < deadline:
        status, final = request(base + '/v1/agentbridge/control', env['SHIM_TOKEN'], {'op': 'status', 'job_id': job_id}, headers)
        if status == 200 and final.get('status') in ('done', 'failed', 'cancelled', 'uncertain'):
            break
        time.sleep(1)
    assert final and final.get('status') == 'done', 'Readonly task did not reach done'
    _, again = request(base + '/v1/chat/completions', env['SHIM_TOKEN'], body, headers, timeout=150)
    assert again['x_agentbridge']['reused'], 'Stable delivery was not reused'
    _, after = request(base + '/v1/agentbridge/control', env['SHIM_TOKEN'], {'op': 'status', 'job_id': job_id}, headers)
    assert after['attempt_id'] == final['attempt_id'] and len(after['events']) == 1
    print(json.dumps({'readonly_job': job_id, 'done': True, 'single_attempt': True,
                      'single_completion_event': True, 'reused': True, 'phone_acceptance': 'not_performed'}))


def enable_notifications():
    invariant()
    cfg = json.loads((ROOT / 'astrbot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
    assert cfg.get('platform_settings', {}).get('id_whitelist'), 'No notification context is explicitly allowed'
    path = ROOT / 'compose.yaml'
    assert digest(path) == manifest()['files'][str(path)]['sha256'], 'Concurrent compose edit'
    text = path.read_text()
    anchor = '      AGENTBRIDGE_ALLOWED_SENDERS: ${AGENTBRIDGE_ALLOWED_SENDERS:-}\n'
    assert text.count(anchor) == 2 and 'AGENTBRIDGE_NOTIFICATIONS:' not in text
    path.write_text(text.replace(anchor, anchor + '      AGENTBRIDGE_NOTIFICATIONS: ${AGENTBRIDGE_NOTIFICATIONS:-0}\n', 1))
    env = {**os.environ, 'AGENTBRIDGE_NOTIFICATIONS': '1'}
    run(['docker', 'compose', 'config', '--quiet'], cwd=ROOT, env=env)
    run(['docker', 'compose', 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'astrbot'], cwd=ROOT, env=env)
    invariant()
    print(json.dumps({'notification_switch_enabled': True, 'image_update': False}))


def rollback():
    invariant()
    assert not containers()[0]['State']['Running'], 'Pause intake and reconcile notifications before rollback'
    data = ROOT / 'astrbot/data/plugin_data/agentbridge_mac/notifications.db'
    if data.exists():
        with closing(sqlite3.connect(data)) as db, db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notifications'").fetchone():
                db.execute("UPDATE notifications SET state='unknown',reason='release_rollback' WHERE state='sending'")
    staged = json.loads((STAGE / 'sha256.json').read_text())
    before = manifest()
    for name, path in MAPPING.items():
        old = before['files'][str(path)]
        assert digest(path) in (old['sha256'], staged[name]), 'Concurrent source edit prevents rollback'
    for path, old in before['files'].items():
        backup = BACKUP / old['backup']
        assert digest(backup) == old['sha256']
        target = Path(path)
        if target.name == 'compose.yaml':
            expected = backup.read_text().replace(
                '      AGENTBRIDGE_ALLOWED_SENDERS: ${AGENTBRIDGE_ALLOWED_SENDERS:-}\n',
                '      AGENTBRIDGE_ALLOWED_SENDERS: ${AGENTBRIDGE_ALLOWED_SENDERS:-}\n      AGENTBRIDGE_NOTIFICATIONS: ${AGENTBRIDGE_NOTIFICATIONS:-0}\n', 1)
            assert target.read_text() in (backup.read_text(), expected), 'Concurrent compose edit prevents rollback'
        shutil.copyfile(backup, target)
        os.chmod(target, old['mode'])
        os.chown(target, old['uid'], old['gid'])
    # Recreate only AstrBot, same image, retained data. Never restore old SQLite.
    run(['docker', 'compose', 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'astrbot'], cwd=ROOT)
    run(['docker', 'restart', 'agentbridge-shim'])
    print(json.dumps({'source_rollback': True, 'database_restored': False}))


if __name__ == '__main__':
    os.umask(0o077)
    assert os.geteuid() == 0
    {'prepare': prepare, 'pause': pause, 'install': install, 'resume': resume, 'probe': probe,
     'enable-notifications': enable_notifications, 'rollback': rollback, 'readonly': readonly}[sys.argv[1]]()
