"""Remote isolated official-image acceptance. Never mounts production data or opens ports."""
import json
import os
from pathlib import Path
import pty
import secrets
import select
import shutil
import subprocess
import sys
import tempfile
import time


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def interactive(args, password, env=None):
    master, slave = pty.openpty()
    proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)
    captured = b''
    sent = 0
    deadline = time.monotonic() + 180
    try:
        while proc.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                captured += chunk
                if sent == 0 and b'New dashboard password:' in captured:
                    os.write(master, password.encode() + b'\n')
                    sent += 1
                elif sent == 1 and b'Repeat for confirmation:' in captured:
                    os.write(master, password.encode() + b'\n')
                    sent += 1
        proc.wait(timeout=10)
        assert password.encode() not in captured, 'Synthetic password echoed by interactive input'
        if proc.returncode != 0 or sent != 2:
            # Output is only synthetic CLI diagnostics; redact input even on failure.
            diagnostic = captured.decode(errors='replace').replace(password, '[REDACTED]')
            raise AssertionError(f'Interactive CLI failed (exit={proc.returncode}, prompts={sent}): {diagnostic[-2500:]}')
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)


def main():
    script = Path(sys.argv[1]).resolve()
    root = Path(tempfile.mkdtemp(prefix='agentbridge-credential-acceptance-', dir='/var/tmp'))
    name = 'agentbridge-credential-test-' + secrets.token_hex(4)
    image = run(['sudo', 'docker', 'inspect', '-f', '{{.Config.Image}}', 'agentbridge-astrbot'], text=True).stdout.strip()
    data = root / 'astrbot/data'
    data.mkdir(parents=True)
    controls = root / 'docker_control'
    controls.mkdir()
    target = controls / script.name
    shutil.copy2(script, target)
    password1 = 'Ab9-' + secrets.token_urlsafe(20)
    password2 = 'Cd8-' + secrets.token_urlsafe(20)
    started = False
    try:
        interactive(['sudo', 'docker', 'run', '--rm', '-it', '--network', 'none', '--entrypoint', 'astrbot',
                     '-v', f'{data}:/AstrBot/data', '-w', '/AstrBot', image, 'password', '--username', 'synthetic'], password1)
        run(['sudo', 'chmod', '600', str(data / 'cmd_config.json')])
        # Data created by the root CLI must remain unreadable to this ubuntu caller.
        assert not os.access(data / 'cmd_config.json', os.R_OK)
        run(['sudo', 'docker', 'run', '-d', '--name', name, '--network', 'none',
             '-v', f'{data}:/AstrBot/data', image])
        started = True
        probe = ('import json,urllib.request; d=json.load(urllib.request.urlopen('
                 '"http://127.0.0.1:6185/api/auth/setup-status",timeout=2)); assert d["status"]=="ok"')
        deadline = time.monotonic() + 90
        while subprocess.run(['sudo', 'docker', 'exec', name, 'python', '-c', probe], capture_output=True).returncode:
            assert time.monotonic() < deadline, 'Isolated dashboard startup timed out'
            time.sleep(1)
        interactive(['bash', str(target), 'synthetic'], password2,
                    env={**os.environ, 'AGENTBRIDGE_ASTRBOT_CONTAINER': name})
        login = '''import json,sys,urllib.request,urllib.error
payload=json.load(sys.stdin)
req=urllib.request.Request("http://127.0.0.1:6185/api/auth/login", data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})
try:
    with urllib.request.urlopen(req,timeout=5) as response: d=json.load(response)
except urllib.error.HTTPError as exc:
    assert exc.code in (400,401,403), "Unexpected authentication HTTP error"
    d={"status":"error"}
print(json.dumps({"ok":d.get("status")=="ok","token_present":bool((d.get("data") or {}).get("token"))}))
'''
        result = run(['sudo', 'docker', 'exec', '-i', name, 'python', '-c', login],
                     input=json.dumps({'username': 'synthetic', 'password': password2}).encode())
        accepted = json.loads(result.stdout)
        assert accepted['ok'] and accepted['token_present'], 'Synthetic login rejected'
        old_result = run(['sudo', 'docker', 'exec', '-i', name, 'python', '-c', login],
                         input=json.dumps({'username': 'synthetic', 'password': password1}).encode())
        assert not json.loads(old_result.stdout)['ok'], 'Old synthetic password still accepted'
        print(json.dumps({'official_cli': True, 'no_password_echo': True, 'root_0600_read': True,
                          'running_state_restored': True, 'synthetic_login': True,
                          'old_password_rejected': True, 'public_ports': 0}))
    finally:
        if started:
            run(['sudo', 'docker', 'rm', '-f', name])
        # This tree contains only test-generated data and was never a production bind mount.
        run(['sudo', 'rm', '-rf', str(root)])


if __name__ == '__main__':
    main()
