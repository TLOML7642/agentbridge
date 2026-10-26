"""Narrow server-side release operations. Run as root on the existing server."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path('/opt/agentbridge')
BACKUP = Path('/var/backups/agentbridge/20261026-s1-s5')
STAGE = Path('/var/tmp/agentbridge-s1-s5-validation/release')
MAPPING = {
    'shim.py': ROOT / 'shim/shim.py',
    'main.py': ROOT / 'astrbot/data/plugins/agentbridge_mac/main.py',
    **{name: ROOT / 'docker_control' / name for name in (
        'agentbridge_set_credentials.sh', 'agentbridge_set_password.sh', 'agentbridge_reset_password.sh')},
}
EXPECTED = {
    str(ROOT / 'shim/shim.py'): 'ee72af60b66e7194a9b999ff0d5382448428dd03f14a455636e4135ad283fec0',
    str(ROOT / 'astrbot/data/plugins/agentbridge_mac/main.py'): '3f6cff7e780b370d7aa0894e9a512c87f5354ea3e449f7cee3e86b6e9a0c4f8f',
    str(ROOT / 'docker_control/agentbridge_set_credentials.sh'): '4804518b9f7e1a9c1294bf2db1d9b081e038fa8d7b199fae804aaab37d30007e',
    str(ROOT / 'compose.yaml'): 'df5862cd71170d6f00aac8e046861e432e966ae0a1cf00437215301bf22ca246',
}


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def request(url, token, data=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None,
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=150) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)


def runtime():
    rows = json.loads(run(['docker', 'inspect', 'agentbridge-astrbot', 'agentbridge-shim']))
    env = dict(value.split('=', 1) for row in reversed(rows) for value in row['Config']['Env'] if '=' in value)
    cfg = json.loads((ROOT / 'astrbot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
    sender = env['AGENTBRIDGE_ALLOWED_SENDERS'].split(',')[0].strip()
    channel = cfg['platform_settings']['id_whitelist'][0]
    return rows, env, {'X-AgentBridge-Sender': sender, 'X-AgentBridge-Channel': channel}


def prepare():
    for path, expected in EXPECTED.items():
        assert digest(path) == expected, 'Baseline changed: ' + path
    BACKUP.mkdir(parents=True, mode=0o700)
    os.chmod(BACKUP.parent, 0o700)
    paths = [*MAPPING.values(), ROOT / '.env', ROOT / 'compose.yaml',
             ROOT / 'astrbot/data/cmd_config.json', Path('/etc/ssh/sshd_config'),
             *Path('/etc/ssh/sshd_config.d').glob('*.conf')]
    manifest = {}
    for path in paths:
        if not path.exists():
            manifest[str(path)] = {'exists': False}
            continue
        dest = BACKUP / str(path).lstrip('/')
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, dest)
        dest.chmod(0o600)
        st = path.stat()
        manifest[str(path)] = {'exists': True, 'sha256': digest(dest), 'mode': st.st_mode & 0o777,
                               'uid': st.st_uid, 'gid': st.st_gid}
    (BACKUP / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    (BACKUP / 'manifest.json').chmod(0o600)
    (BACKUP / 'prepared-at').write_text(str(time.time()))
    print(json.dumps({'backup_files': len(manifest), 'private_backup': str(BACKUP), 'baseline_verified': True}))


def apply():
    manifest = json.loads((BACKUP / 'manifest.json').read_text())
    assert run(['docker', 'inspect', '-f', '{{.State.Running}}', 'agentbridge-astrbot']) == 'false'
    for path, expected in EXPECTED.items():
        assert digest(path) == expected, 'Concurrent change: ' + path
    staged = json.loads((STAGE / 'sha256.json').read_text())
    for name, path in MAPPING.items():
        assert digest(STAGE / name) == staged[name]
        old = manifest[str(path)]
        shutil.copyfile(STAGE / name, path)
        path.chmod(old['mode'] if old['exists'] else 0o755)
        if old['exists']:
            os.chown(path, old['uid'], old['gid'])
        assert digest(path) == staged[name]
    print(json.dumps({'installed': list(MAPPING), 'checksums_verified': True}))


def ssh_apply():
    path = Path('/etc/ssh/sshd_config')
    expected = json.loads((BACKUP / 'manifest.json').read_text())[str(path)]['sha256']
    assert digest(path) == expected, 'Concurrent SSH configuration change'
    extra = Path('/etc/ssh/agentbridge-tunnel.conf')
    assert not extra.exists()
    extra.write_text('Match User agentbridge-tunnel\n'
                     '    AllowTcpForwarding remote\n'
                     '    PermitListen 127.0.0.1:9000\n'
                     '    PermitOpen none\n'
                     '    AllowStreamLocalForwarding no\n'
                     '    PermitTTY no\n'
                     '    X11Forwarding no\n'
                     '    AllowAgentForwarding no\n'
                     '    ClientAliveInterval 30\n'
                     '    ClientAliveCountMax 3\n')
    extra.chmod(0o644)
    with path.open('a') as f:
        f.write('\n# Dedicated reverse tunnel account; no changes to management users.\n'
                'Include /etc/ssh/agentbridge-tunnel.conf\n')
    try:
        run(['/usr/sbin/sshd', '-t'])
        effective = run(['/usr/sbin/sshd', '-T', '-C', 'user=agentbridge-tunnel,host=localhost,addr=127.0.0.1'])
        for directive in ('allowtcpforwarding remote', 'permitlisten 127.0.0.1:9000', 'permitopen none',
                          'allowstreamlocalforwarding no', 'permittty no', 'x11forwarding no', 'allowagentforwarding no'):
            assert directive in effective.splitlines(), directive
        admin = run(['/usr/sbin/sshd', '-T', '-C', 'user=ubuntu,host=localhost,addr=127.0.0.1'])
        assert 'allowtcpforwarding yes' in admin.splitlines() and 'permittty yes' in admin.splitlines()
        run(['systemctl', 'reload', 'ssh'])
    except BaseException:
        shutil.copyfile(BACKUP / 'etc/ssh/sshd_config', path)
        extra.unlink()
        run(['/usr/sbin/sshd', '-t'])
        run(['systemctl', 'reload', 'ssh'])
        raise
    print(json.dumps({'sshd_test': True, 'tunnel_only_restrictions': True, 'management_unchanged': True}))


def probe():
    rows, env, headers = runtime()
    status, _ = request('http://127.0.0.1:8787/health', '')
    assert status == 401
    assert request('http://127.0.0.1:8787/health', env['SHIM_TOKEN'])[0] == 200
    base = 'http://127.0.0.1:8787/v1/agentbridge/control'
    assert request(base, env['SHIM_TOKEN'], {'op': 'status'}, headers)[0] == 200
    assert request(base, env['SHIM_TOKEN'], {'op': 'status'}, {**headers, 'X-AgentBridge-Sender': 'synthetic-denied'})[0] == 403
    assert request(base, env['SHIM_TOKEN'], {'op': 'status'}, {})[0] == 403
    assert request(base, env['SHIM_TOKEN'], {'op': 'execute'}, headers)[0] == 400
    worker_req = urllib.request.Request('http://127.0.0.1:9000/health', headers={'X-AgentBridge-Secret': env['AGENTBRIDGE_SECRET']})
    with urllib.request.urlopen(worker_req, timeout=5) as response:
        assert json.load(response)['ok']
    result = {'time': time.time(), 'health_auth': True, 'allowlist_rejection': True,
              'control_readonly': True, 'worker_tunnel': True,
              'containers': [{key: row['State'][key] for key in ('Status', 'OOMKilled', 'ExitCode')} |
                             {'name': row['Name'], 'restarts': row['RestartCount']} for row in rows]}
    for row in rows:
        assert row['State']['Running'] and not row['State']['OOMKilled']
    result['resources'] = run(['docker', 'stats', '--no-stream', '--format', '{{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.PIDs}}',
                                'agentbridge-astrbot', 'agentbridge-shim']).splitlines()
    result['errors_since_release'] = {}
    since = (BACKUP / 'prepared-at').read_text() if (BACKUP / 'prepared-at').exists() else rows[0]['State']['StartedAt']
    for name in ('agentbridge-astrbot', 'agentbridge-shim'):
        logs = subprocess.run(['docker', 'logs', '--since', since, name], capture_output=True, text=True)
        result['errors_since_release'][name] = len(re.findall(r'\b(?:ERROR|CRITICAL|Traceback)\b', logs.stdout + logs.stderr))
    assert not any(result['errors_since_release'].values()), 'New server errors require review'
    print(json.dumps(result))


def readonly():
    _, env, headers = runtime()
    headers['X-AgentBridge-Job-Id'] = 'release-20261026-readonly-acceptance'
    headers['X-AgentBridge-Id-Stable'] = '1'
    payload = {'model': 'agentbridge', 'messages': [{'role': 'user', 'content':
        '只读核验：报告 macOS 系统类型和 /etc/hosts 的行数。仅查询，只返回上述摘要。'}]}
    status, result = request('http://127.0.0.1:8787/v1/chat/completions', env['SHIM_TOKEN'], payload, headers)
    xb = result.get('x_agentbridge') or {}
    assert status == 200 and xb.get('status') == 'done', 'Readonly task did not complete'
    again = request('http://127.0.0.1:8787/v1/chat/completions', env['SHIM_TOKEN'], payload, headers)[1]
    assert again['x_agentbridge']['reused']
    print(json.dumps({'readonly_task_done': True, 'idempotent_retry': True, 'job_id': xb['job_id'],
                      'phone_wechat_acceptance': 'not_performed'}))


if __name__ == '__main__':
    assert os.geteuid() == 0
    {'prepare': prepare, 'apply': apply, 'ssh-apply': ssh_apply, 'probe': probe, 'readonly': readonly}[sys.argv[1]]()
