"""P0 approval and credential isolation. Only temporary synthetic state."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.check_server_runtime import CHECKS, ROOT, gate_passed, locked_packages, private_credential, run_probe
import worker


def report():
    return {'stage': 'P0', 'checks': dict.fromkeys(CHECKS, True),
            'real_model_turns': 2, 'production_changed': False, 'mode': 'real',
            'platform': 'linux', 'node': 'v24.22.2', 'dsh': '0.1.1-rc.2'}


class GateTests(unittest.TestCase):
    def test_complete_real_linux_evidence_required(self):
        self.assertTrue(gate_passed(report()))
        for key in CHECKS:
            for value in (False, None, 1, 'true'):
                data = report()
                data['checks'][key] = value
                self.assertFalse(gate_passed(data), (key, value))

    def test_synthetic_missing_extra_and_wrong_types_never_approve(self):
        for key, value in (('real_model_turns', 0), ('real_model_turns', True),
                           ('real_model_turns', 3), ('production_changed', 0),
                           ('production_changed', True), ('stage', 'P1'), ('mode', 'synthetic'),
                           ('platform', 'darwin'), ('node', 'v26.3.0'), ('dsh', 'latest')):
            data = report()
            data[key] = value
            self.assertFalse(gate_passed(data))
        for value in (None, {}, [], {'checks': True}):
            self.assertFalse(gate_passed(value))
        data = report()
        data['checks'].pop('real_api')
        self.assertFalse(gate_passed(data))
        data = report()
        data['checks']['extra'] = True
        self.assertFalse(gate_passed(data))


class PrivateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-p0-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.credential = self.root / 'document'
        self.credential.write_text('synthetic credential, not a real key')
        self.credential.chmod(0o600)

    def test_credential_only_owned_private_regular_bounded_file(self):
        self.assertEqual(private_credential(self.credential), self.credential.read_bytes())
        self.credential.chmod(0o644)
        with self.assertRaises(ValueError):
            private_credential(self.credential)
        self.credential.chmod(0o600)
        link = self.root / 'link'
        link.symlink_to(self.credential)
        with self.assertRaises(OSError):
            private_credential(link)
        with self.assertRaises(ValueError):
            private_credential(self.root)
        self.credential.write_bytes(b'x' * 65537)
        with self.assertRaises(ValueError):
            private_credential(self.credential)

    def invoke(self, result=None, **kwargs):
        with mock.patch('tools.check_server_runtime.subprocess.run') as run:
            run.return_value = result or subprocess.CompletedProcess([], 0, json.dumps(report()), '')
            value = run_probe('/bin/sh', self.root, self.root / 'report', **kwargs)
            return value, run.call_args

    def test_child_environment_cannot_inherit_production_or_auth(self):
        with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'synthetic-private',
                                         'AGENTBRIDGE_SECRET': 'synthetic-other', 'SSH_AUTH_SOCK': '/fake'}):
            value, call = self.invoke()
        self.assertTrue(value['gate_passed'])
        env = call.kwargs['env']
        self.assertEqual(set(env), {'PATH', 'HOME', 'TMPDIR', 'AGENTBRIDGE_BASE',
                                   'AGENTBRIDGE_AGENT_MESSAGES', 'AGENTBRIDGE_AGENT_TASKS',
                                   'AGENTBRIDGE_AGENT_LEARNING', 'AGENTBRIDGE_AGENT_NODE', 'AGENTBRIDGE_AGENT_PACKAGES'})
        self.assertEqual(env['HOME'], str(self.root / 'report/home'))
        self.assertEqual(call.kwargs['cwd'], Path(env['HOME']))
        self.assertEqual(call.kwargs['timeout'], 330)

    def test_explicit_credential_is_private_copy_and_removed_after_use(self):
        def child(*args, **kwargs):
            file = Path(kwargs['env']['AGENTBRIDGE_AGENT_CREDENTIALS'])
            self.assertEqual(file.read_bytes(), self.credential.read_bytes())
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(kwargs['env']['AGENTBRIDGE_AGENT_CREDENTIAL_REF'], 'SERVER_KEY')
            return subprocess.CompletedProcess([], 0, json.dumps(report()), '')
        with mock.patch('tools.check_server_runtime.subprocess.run', side_effect=child):
            run_probe('/bin/sh', self.root, self.root / 'report', self.credential, 'SERVER_KEY')
        self.assertFalse((self.root / 'report/home/credentials.yaml').exists())
        self.assertTrue(self.credential.exists())

    def test_failed_child_cannot_expose_credential_and_copy_removed(self):
        result = subprocess.CompletedProcess([], 1, 'synthetic-private', 'synthetic-private')
        with self.assertRaises(RuntimeError) as caught:
            self.invoke(result, credential=self.credential, credential_ref='SERVER_KEY')
        self.assertNotIn('synthetic-private', str(caught.exception))
        self.assertFalse((self.root / 'report/home/credentials.yaml').exists())
        self.assertTrue(self.credential.exists())

    def test_report_must_be_outside_repository_even_via_symlink(self):
        link = self.root / 'source'
        link.symlink_to(ROOT, target_is_directory=True)
        for directory in (ROOT / 'ignored-report', link / 'ignored-report'):
            with self.assertRaises(ValueError):
                run_probe('/bin/sh', self.root, directory)

    def test_existing_report_and_implicit_credentials_rejected(self):
        for credential, ref in ((self.credential, None), (None, 'KEY'), (self.credential, 'bad ref')):
            with self.assertRaises(ValueError):
                run_probe('/bin/sh', self.root, self.root / 'report', credential, ref)
        for node, packages in (('relative', self.root), ('/bin/sh', 'relative')):
            with self.assertRaises(ValueError):
                run_probe(node, packages, self.root / 'report')
        with self.assertRaises(FileExistsError):
            run_probe('/bin/sh', self.root, self.root)


class LockTests(unittest.TestCase):
    def test_lock_is_minimal_fixed_and_public_registry_only(self):
        manifest = json.loads((ROOT / 'server/assistant/package.json').read_text())
        lock = json.loads((ROOT / 'server/assistant/package-lock.json').read_text())
        self.assertEqual(lock['packages']['']['dependencies'], manifest['dependencies'])
        self.assertEqual(manifest['engines']['node'], '24.22.2')
        self.assertEqual(len(manifest['dependencies']), 11)
        for name, entry in lock['packages'].items():
            if not name:
                continue
            self.assertTrue(entry['resolved'].startswith('https://registry.npmjs.org/'))
            self.assertRegex(entry['integrity'], '^sha512-')
            self.assertNotIn('electron', name)
            self.assertNotIn('dsh-tool-bash', name)
            if name.startswith('node_modules/@deepseek-ai/dsh-'):
                self.assertEqual(entry['version'], '0.1.1-rc.2')

    def test_installed_package_drift_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages = root / 'node_modules/@deepseek-ai'
            packages.mkdir(parents=True)
            lock = json.loads((ROOT / 'server/assistant/package-lock.json').read_text())
            for name, entry in lock['packages'].items():
                if name and not entry.get('optional'):
                    file = root / name / 'package.json'
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(json.dumps({'version': entry['version']}))
            self.assertTrue(locked_packages(packages))
            (packages / 'dsh-agent/package.json').write_text('{"version":"unapproved"}')
            self.assertFalse(locked_packages(packages))


class LinuxSupervisorTests(unittest.TestCase):
    def test_proc_members_require_matching_kernel_birth_identity_without_ps(self):
        with mock.patch.object(worker.sys, 'platform', 'linux'), \
                mock.patch.object(worker.os, 'listdir', return_value=['self', '17', '23', '31', '١']), \
                mock.patch.object(worker, '_process_identity', side_effect=lambda pid, group:
                                  'linux:boot:17:99' if pid == 17 and group == 17 else None), \
                mock.patch.object(worker.subprocess, 'check_output') as ps:
            self.assertEqual(worker._group_members(17), {17: 'linux:boot:17:99'})
            ps.assert_not_called()

    def test_unreadable_proc_denies_group_ownership(self):
        with mock.patch.object(worker.sys, 'platform', 'linux'), \
                mock.patch.object(worker.os, 'listdir', side_effect=PermissionError):
            self.assertEqual(worker._group_members(17), {})

    def test_linux_stat_process_group_and_boot_start_identity_are_bound(self):
        def proc_file(path, **kwargs):
            fields = ['S', '1', '17'] + ['0'] * 16 + ['991']
            return mock.mock_open(read_data=('17 (name with ) spaces) ' + ' '.join(fields)
                                            if path == '/proc/17/stat' else 'synthetic-boot\n'))()
        with mock.patch.object(worker.sys, 'platform', 'linux'), mock.patch('builtins.open', side_effect=proc_file):
            self.assertEqual(worker._process_identity(17, 17), 'linux:synthetic-boot:17:991')
            self.assertIsNone(worker._process_identity(17, 99))


if __name__ == '__main__':
    unittest.main()
