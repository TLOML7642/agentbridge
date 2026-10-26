"""Credential lifecycle failure injection; no production Docker or passwords."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
r=pathlib.Path(os.environ['MOCK_ROOT']); args=sys.argv[1:]
with (r/'calls').open('a') as f: f.write(json.dumps(args)+'\n')
s=json.loads((r/'state').read_text()); case=os.environ['MOCK_CASE']
if args[0]=='info': sys.exit(0)
if args[0]=='inspect':
    fmt=args[2]
    print('true' if s['running'] else 'false') if fmt=='{{.State.Running}}' else print('[]' if 'Env' in fmt else 'synthetic-image')
elif args[0]=='stop':
    if case=='stop-fail': sys.exit(1)
    s['running']=False
elif args[0]=='start':
    s['starts']+=1
    if case=='start-fail' and s['starts']==1:
        (r/'state').write_text(json.dumps(s)); sys.exit(1)
    s['running']=True
elif args[0]=='run':
    cfg=r/'astrbot/data/cmd_config.json'; d=json.loads(cfg.read_text())
    d['dashboard']['username']=args[-1]; d['dashboard']['pbkdf2_password']='synthetic-new-hash'
    cfg.write_text(json.dumps(d))
    if case=='cli-fail': sys.exit(1)
(r/'state').write_text(json.dumps(s))
'''

FAKE_SUDO = r'''#!/bin/sh
[ "$1" != -n ] || shift
printf '%s\n' "$1" >> "$MOCK_ROOT/privileged-calls"
[ "$1" != -v ] || exit 0
if [ "$1" = mktemp ]; then exec /usr/bin/mktemp -d "$MOCK_ROOT/backup.XXXXXXXX"; fi
exec "$@"
'''

FAKE_SHA = r'''#!/usr/bin/env python3
import hashlib,pathlib,sys
if sys.argv[1]=='-c':
    digest,path=pathlib.Path(sys.argv[2]).read_text().strip().split('  ',1)
    assert hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()==digest
else:
    print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()+'  '+sys.argv[1])
'''


class TestCredentials(unittest.TestCase):
    def run_case(self, case, running=True):
        with tempfile.TemporaryDirectory(prefix='agentbridge-credentials-test-') as tmp:
            root = Path(tmp)
            controls = root / 'docker_control'
            controls.mkdir()
            source = Path(__file__).parent / 'server/docker_control'
            for name in ('set_credentials', 'set_password', 'reset_password'):
                shutil.copy2(source / ('agentbridge_' + name + '.sh'), controls)
            cfg = root / 'astrbot/data/cmd_config.json'
            cfg.parent.mkdir(parents=True)
            original = json.dumps({'dashboard': {'username': 'synthetic', 'pbkdf2_password': 'old-hash',
                                                  'password_change_required': False}}).encode()
            cfg.write_bytes(original)
            cfg.chmod(0o600)
            envfile = root / '.env'
            envfile.write_text('SYNTHETIC_ENV_UNCHANGED=1\n')
            (root / 'state').write_text(json.dumps({'running': running, 'starts': 0}))
            bindir = root / 'bin'
            bindir.mkdir()
            for name, script in {'docker': FAKE_DOCKER, 'sudo': FAKE_SUDO, 'sha256sum': FAKE_SHA,
                                 'curl': '#!/bin/sh\necho \'{"status":"ok","data":{"setup_required":false}}\'\n'}.items():
                path = bindir / name
                path.write_text(script)
                path.chmod(0o700)
            result = subprocess.run(['bash', str(controls / 'agentbridge_set_credentials.sh'), 'synthetic'],
                env={**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH'],
                     'MOCK_ROOT': str(root), 'MOCK_CASE': case}, capture_output=True, text=True, timeout=12)
            calls = [json.loads(row) for row in (root / 'calls').read_text().splitlines()]
            state = json.loads((root / 'state').read_text())
            self.assertEqual(state['running'], running)
            self.assertFalse(any('shim' in ' '.join(call) or 'compose' in call for call in calls))
            self.assertEqual(envfile.read_text(), 'SYNTHETIC_ENV_UNCHANGED=1\n')
            self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('old-hash', result.stdout + result.stderr)
            self.assertNotIn('synthetic-new-hash', result.stdout + result.stderr)
            if case == 'ok':
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(cfg.read_text())['dashboard']['pbkdf2_password'], 'synthetic-new-hash')
                self.assertFalse(list(root.glob('backup.*')))
            else:
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(cfg.read_bytes(), original)
            if case == 'stop-fail':
                self.assertFalse(any(call[0] == 'run' for call in calls))
            if not running:
                self.assertFalse(any(call[0] == 'start' for call in calls))
            self.assertIn('python3', (root / 'privileged-calls').read_text())

    def test_stop_failure_aborts_before_cli(self):
        self.run_case('stop-fail')

    def test_cli_failure_restores_config_and_original_running_state(self):
        self.run_case('cli-fail')

    def test_start_failure_restores_config_and_restarts_original(self):
        self.run_case('start-fail')

    def test_success_keeps_running_and_never_touches_shim_or_env(self):
        self.run_case('ok')

    def test_originally_stopped_remains_stopped(self):
        self.run_case('ok', running=False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
