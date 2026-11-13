"""Synthetic lightweight service and preflight lifecycle. No production state."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import urllib.error
import urllib.request
from unittest import mock

from agent_runtime import AgentError
from agent_tasks import TaskRuntime
from agent_approvals import fingerprint
import executor_service
import worker
import shim
import test_computer


class Preflight(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='abpf-', dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'project/demo').mkdir(parents=True)
        (self.root / 'file.txt').write_text('synthetic')
        self.calls = []
        self.runtime = self.make()
        self.addCleanup(self.runtime.close)
        self.subject = dict(sender='owner', channel='private', conversation='original', project='assistant',
                            cwd=str(self.root), attempt=uuid.uuid4().hex, session=uuid.uuid4().hex)

    def make(self):
        def runner(task, cancel):
            self.calls.append(task)
            request = dict(version=1, epoch='native', execution='native', subject=task['subject'], tool=task['tool'],
                           arguments=json.loads(task['arguments']), callId=task['id'], rootCallId=task['id'], nested=False)
            request['digest'] = fingerprint(request)
            payload = dict(capability=task['broker']['capability'], op='approval', request=request)
            answer = self.runtime.rpc(payload)
            consumed = self.runtime.rpc({**payload, 'op': 'consume', 'grant': answer['grant']})
            if not consumed['consumed']:
                return {'completion': 'denied'}
            self.assertFalse(self.runtime.rpc({**payload, 'op': 'consume', 'grant': answer['grant']})['consumed'])
            return {'completion': 'verified', 'evidence': 'synthetic-only'}
        return TaskRuntime(self.root / '.runtime/tasks', runner, lambda _: str(self.root / 'project/demo'),
                           enabled=True, allowed_projects=(), computer_root=str(self.root), preflight=True)

    def wait(self, identifier, state):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            value = self.runtime.inspect('owner', 'private', 'original', 'assistant', identifier)
            if value['state'] == state:
                return value
            time.sleep(.01)
        self.fail(str(value))

    def submit(self, tool='computer_trash_file', args=None):
        return self.runtime.submit(self.subject, uuid.uuid4().hex, tool, args or {'path': 'file.txt'})

    def approve(self, identifier, remember=False):
        self.wait(identifier, 'awaiting_confirmation')
        values = self.runtime.approvals.inspect('owner', 'private', 'original', 'assistant')['approvals']
        approval = next(value for value in values if value['subject']['attempt'] == identifier)
        return self.runtime.decide('owner', 'private', 'original', 'assistant', approval['approval_id'],
                                   uuid.uuid4().hex, True, remember)

    def test_no_tool_before_decision_and_native_grant_is_still_single_use(self):
        row = self.submit()
        self.wait(row['task_id'], 'awaiting_confirmation')
        self.assertEqual(self.calls, [])
        self.approve(row['task_id'])
        self.wait(row['task_id'], 'verified')
        self.assertEqual(len(self.calls), 1)

    def test_real_native_guard_after_preflight_moves_only_synthetic_file(self):
        if sys.platform != 'darwin':
            self.skipTest('macOS native action acceptance is separate from Linux policy tests')
        from agent_task_executor import NativeTaskExecutor
        from process_supervisor import run_task, _executor_identity
        executor = NativeTaskExecutor(self.runtime.root / 'native', run_task, _executor_identity, lambda _: 'dead')
        self.runtime.runner = executor
        with mock.patch.dict(os.environ, {
                'AGENTBRIDGE_AGENT_NODE': os.environ['AGENTBRIDGE_TEST_NATIVE_NODE'],
                'AGENTBRIDGE_AGENT_PACKAGES': os.environ['AGENTBRIDGE_TEST_NATIVE_PACKAGES']}):
            row = self.submit()
            self.wait(row['task_id'], 'awaiting_confirmation')
            self.assertFalse((executor.root / (row['task_id'] + '.request.json')).exists())
            self.approve(row['task_id'])
            result = self.wait(row['task_id'], 'verified')
        self.assertFalse((self.root / 'file.txt').exists())
        self.assertEqual(Path(result['result']['trash_path']).read_text(), 'synthetic')
        journal = executor.root / 'action-evidence' / (row['task_id'] + '.jsonl')
        self.assertEqual([json.loads(line)['stage'] for line in journal.read_text().splitlines()], ['trash-intent', 'trash-verified'])

    def test_waiting_does_not_block_safe_task_and_cancel_never_runs(self):
        pending = self.submit()
        self.wait(pending['task_id'], 'awaiting_confirmation')
        safe = self.submit('computer_open_project', {'project': 'demo'})
        self.wait(safe['task_id'], 'verified')
        self.runtime.cancel('owner', 'private', 'original', 'assistant', pending['task_id'], 'cancel')
        self.wait(pending['task_id'], 'cancelled')
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(pending['task_id'], self.runtime.cancels)

    def test_changed_target_fails_before_tool_and_restart_does_not_replay(self):
        row = self.submit()
        self.wait(row['task_id'], 'awaiting_confirmation')
        (self.root / 'file.txt').write_text('changed')
        self.approve(row['task_id'])
        self.wait(row['task_id'], 'failed')
        self.assertEqual(self.calls, [])
        row = self.submit()
        self.wait(row['task_id'], 'awaiting_confirmation')
        self.runtime.close()
        self.runtime = self.make()
        self.addCleanup(self.runtime.close)
        self.wait(row['task_id'], 'cancelled')
        self.assertEqual(self.calls, [])

    def test_parent_substitution_and_unknown_after_partial_effect_never_replay(self):
        directory = self.root / 'files'
        directory.mkdir()
        (directory / 'one').write_text('synthetic one')
        row = self.submit(args={'path': 'files/one'})
        self.wait(row['task_id'], 'awaiting_confirmation')
        directory.rename(self.root / 'moved')
        directory.symlink_to(self.root / 'moved', target_is_directory=True)
        self.approve(row['task_id'])
        self.wait(row['task_id'], 'failed')
        self.assertEqual(self.calls, [])
        directory.unlink()
        (self.root / 'moved').rename(directory)
        count = []
        def partial(_task, _cancel):
            count.append(1)
            (directory / 'one').rename(directory / 'effect')
            raise OSError('synthetic crash after effect')
        self.runtime.runner = partial
        row = self.submit(args={'path': 'files/one'})
        self.approve(row['task_id'])
        self.wait(row['task_id'], 'uncertain')
        with self.assertRaises(AgentError):
            self.submit('computer_open_project', {'project': 'demo'})
        self.runtime.close()
        self.runtime = self.make()
        self.addCleanup(self.runtime.close)
        self.wait(row['task_id'], 'uncertain')
        self.assertEqual(count, [1])
        self.assertEqual((directory / 'effect').read_text(), 'synthetic one')

    def configuration(self):
        directory = self.root / 'project/demo/.idea/runConfigurations'
        directory.mkdir(parents=True)
        file = directory / 'demo.xml'
        file.write_text('<component><configuration name="demo" type="PythonConfigurationType">'
                        '<option name="SCRIPT_NAME" value="$PROJECT_DIR$/main.py"/>'
                        '<option name="SDK_HOME" value="' + sys.executable + '"/></configuration></component>')
        (self.root / 'project/demo/main.py').write_text('print("synthetic")')
        return file

    def test_persistent_run_grant_survives_restart_and_code_change_but_not_config_change(self):
        file = self.configuration()
        args = {'project': 'demo', 'configuration': 'demo'}
        first = self.submit('computer_run_project', args)
        self.approve(first['task_id'], remember=True)
        self.wait(first['task_id'], 'verified')
        grants = self.runtime.permissions('owner', 'private')['permissions']
        self.assertEqual(len(grants), 1)
        self.assertEqual(self.runtime.permissions('foreign', 'private')['permissions'], [])
        self.runtime.close()
        self.runtime = self.make()
        self.addCleanup(self.runtime.close)
        (self.root / 'project/demo/main.py').write_text('print("daily change")')
        second = self.submit('computer_run_project', args)
        self.wait(second['task_id'], 'verified')
        file.write_text(file.read_text().replace('</configuration>', '<option name="ENV" value="changed"/></configuration>'))
        changed = self.submit('computer_run_project', args)
        self.wait(changed['task_id'], 'awaiting_confirmation')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.runtime.permissions('owner', 'private')['permissions'], [])
        self.runtime.cancel('owner', 'private', 'original', 'assistant', changed['task_id'], 'cancel-changed')
        with self.assertRaises(AgentError):
            self.runtime.permissions('owner', 'foreign', grants[0]['permission_id'])
        self.runtime.permissions('owner', 'private', grants[0]['permission_id'])
        self.assertEqual(self.runtime.permissions('owner', 'private')['permissions'], [])


class Service(unittest.TestCase):
    def test_legacy_job_queries_preserve_wire_shape_and_do_not_write_old_database(self):
        with tempfile.TemporaryDirectory(prefix='absvc-', dir='/private/tmp') as name:
            root = Path(name).resolve()
            home = root / 'home'
            home.mkdir()
            database = worker.DB(str(root / 'agentbridge.db'))
            database.execute("INSERT INTO jobs(job_id,task,status,created_at,result) VALUES ('legacy','synthetic','done',1,'synthetic result')")
            database.execute("INSERT INTO aliases(alias,job_id,created_at) VALUES ('alias','legacy',1)")
            database.close()
            before = (root / 'agentbridge.db').read_bytes()
            with mock.patch.dict(os.environ, {'AGENTBRIDGE_WORKER_ROLE': 'executor_only'}):
                app = executor_service.App(root, str(home), runner=lambda *_: None)
            try:
                value = app.get_job('alias')
                self.assertEqual(value['job_id'], 'legacy')
                self.assertEqual(value['status'], 'done')
                self.assertEqual(value['message'], 'synthetic result')
                self.assertEqual(value['requested_id'], 'alias')
                self.assertIsNone(app.get_job('unknown'))
            finally:
                app.close()
            self.assertEqual((root / 'agentbridge.db').read_bytes(), before)
    def test_standalone_does_not_import_worker_or_start_chat_or_scheduler(self):
        with tempfile.TemporaryDirectory(prefix='absvc-', dir='/private/tmp') as name:
            root = Path(name).resolve()
            home = root / 'home'
            home.mkdir()
            code = ('import sys; from executor_service import App; '
                    'a=App(sys.argv[1],sys.argv[2]); '
                    'assert "worker" not in sys.modules; assert "agent_runtime" not in sys.modules; '
                    'assert "agent_character" not in sys.modules; assert a.agent is None; '
                    'assert not hasattr(a,"sched"); a.close()')
            result = subprocess.run([sys.executable, '-B', '-c', code, str(root / 'runtime'), str(home)],
                                    env={**os.environ, 'AGENTBRIDGE_WORKER_ROLE': 'executor_only'},
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_old_paths_and_python_entry_points_cannot_bypass_disabled_role(self):
        for op in ('confirm', 'force', 'project.add', 'project.rm', 'use'):
            with self.assertRaises(worker.AgentBridgeError) as raised:
                worker.do_control(type('App', (), {'role': 'executor_only'})(), {'op': op})
            self.assertEqual(raised.exception.code, 'LEGACY_DISABLED')
        with self.assertRaises(worker.AgentBridgeError):
            worker.do_execute(type('App', (), {'role': 'executor_only'})(), {'task': 'run arbitrary shell'})
        with mock.patch.object(shim, 'ASSISTANT', 'http://127.0.0.1:9999'), mock.patch.object(shim.urllib.request, 'urlopen') as network:
            self.assertEqual(shim._accept_worker('arbitrary', {})['code'], 'LEGACY_DISABLED')
            network.assert_not_called()
        app = type('App', (), {'role': 'executor_only', 'boot_id': 'synthetic'})()
        http = executor_service.server(app, 'synthetic', {'owner'}, {'private'}, 0)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            for route in ('/execute', '/execute/', '/control', '/agent/messages', '/project/add'):
                request = urllib.request.Request('http://127.0.0.1:' + str(http.server_port) + route, data=b'{}',
                                                 headers={'X-AgentBridge-Secret': 'synthetic'})
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(raised.exception.code, 410)
                self.assertEqual(json.load(raised.exception)['code'], 'LEGACY_DISABLED')
        finally:
            http.shutdown()
            http.server_close()
            thread.join()
