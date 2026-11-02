"""Stopped-writer migration, logical identity, fixed executor and rollback fixtures."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket

import assistant
from agent_runtime import AgentError, AgentRuntime, NativeExecutor
from assistant_executor import Executor
from assistant_projects import Projects
from executor_api import dispatch
from process_supervisor import run_task, redact
from tools.migrate_assistant import snapshot, import_snapshot, verify
from worker_role import role
import agent_reads
import worker


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='abm-', dir='/tmp'))).resolve()
        self.old = self.root / 'old'
        self.old.mkdir(mode=0o700)
        self.cwd = self.root / 'mac-project'
        self.cwd.mkdir()
        self.calls = []
        self.native_errors = []

    def runner(self, turn, cancel):
        self.calls.append(turn)
        return 'synthetic:' + turn['text']

    def runtime(self, root=None, cwd=None, runner=None, logical=False):
        runtime = AgentRuntime(root or self.old / 'agent', runner or self.runner, lambda _: str(cwd or self.cwd),
                               logical_projects=logical)
        self.stack.callback(runtime.close)
        return runtime

    def wait(self, runtime, identifier, state='done'):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = runtime.control('owner', 'private', 'agent.status', 'p', identifier)
            if row['state'] == state:
                return row
            if row['state'] not in ('queued', 'running'):
                self.fail(str(row) + str(self.native_errors))
            time.sleep(.01)
        self.fail('isolated turn timed out')

    def source(self, runner=None):
        runtime = self.runtime(runner=runner)
        row = runtime.submit('owner', 'private', 'old-delivery', 'old-history-marker', 'p')
        self.wait(runtime, row['message_id'])
        runtime.control('owner', 'private', 'agent.preference.set', 'p', delivery='pref', category='language', value='zh')
        runtime.close()
        db = worker.DB(str(self.old / 'agentbridge.db'))
        db.execute('INSERT INTO projects VALUES (?,?,?,?,?,?)', ('p', str(self.cwd), str(self.cwd), 'synthetic-lock', 1, 1))
        db.execute('INSERT INTO channels VALUES (?,?,?)', ('private', 'p', 1))
        db.close()
        return row

    def absent_pid(self):
        child = subprocess.Popen(['/usr/bin/true'])
        child.wait()
        return child.pid

    def test_snapshot_refuses_live_writer_and_coordinator(self):
        self.source()
        with self.assertRaises(ValueError):
            snapshot(self.old, self.root / 'live', os.getpid())
        runtime = self.runtime()
        with self.assertRaises(BlockingIOError):
            snapshot(self.old, self.root / 'locked', self.absent_pid())
        runtime.close()

    def test_snapshot_rejects_links_tampering_and_shared_files(self):
        self.source()
        p = self.old / 'agent/native/receipts'
        p.mkdir(parents=True)
        (p / 'link').symlink_to(self.old / 'agentbridge.db')
        with self.assertRaises(ValueError):
            snapshot(self.old, self.root / 'linked', self.absent_pid())
        (p / 'link').unlink()
        (self.old / 'agentbridge.db').chmod(0o644)
        with self.assertRaises(ValueError):
            snapshot(self.old, self.root / 'shared', self.absent_pid())
        (self.old / 'agentbridge.db').chmod(0o600)
        snapshot(self.old, self.root / 'backup', self.absent_pid())
        (self.root / 'backup/agentbridge.db').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            verify(self.root / 'backup')

    def test_import_preserves_ids_preferences_selections_and_rollback_new_messages(self):
        old = self.source()
        before = hashlib.sha256((self.old / 'agent/conversations.db').read_bytes()).hexdigest()
        snapshot(self.old, self.root / 'backup', self.absent_pid())
        imported = self.root / 'server'
        result = import_snapshot(self.root / 'backup', imported, [('owner', 'private')])
        self.assertFalse(result['approvals_imported_as_valid'])
        app = assistant.App(imported, 'synthetic', {'owner'}, {'private'}, runner=self.runner)
        self.stack.callback(app.close)
        self.assertEqual(app.projects.current('owner', 'private'), 'p')
        self.assertEqual(app.agent.current_conversation('owner', 'private', 'p'), old['session_id'])
        self.assertEqual(app.control(dict(sender='owner', channel='private', op='agent.lookup', delivery_id='old-delivery'))['message_id'], old['message_id'])
        row = app.message(dict(sender='owner', channel='private', delivery_id='new-delivery', text='history-followup'))
        self.wait(app.agent, row['message_id'])
        self.assertEqual(row['session_id'], old['session_id'])
        self.assertEqual(self.calls[-1]['subject']['origin_cwd'], str(self.cwd))
        self.assertNotEqual(self.calls[-1]['subject']['cwd'], str(self.cwd))
        self.assertTrue(self.calls[-1]['resume'])
        self.assertEqual(self.calls[-1]['persona']['preferences'][0]['value'], 'zh')
        app.close()
        # The baseline understands the same additive state. Roll back code,
        # never restore the old database or rerun the newly accepted turn.
        self.calls.clear()
        app = assistant.App(imported, 'synthetic', {'owner'}, {'private'}, runner=self.runner)
        self.stack.callback(app.close)
        self.assertEqual(app.control(dict(sender='owner', channel='private', op='agent.lookup', delivery_id='new-delivery'))['state'], 'done')
        self.assertEqual(self.calls, [])
        self.assertEqual(hashlib.sha256((self.old / 'agent/conversations.db').read_bytes()).hexdigest(), before)
        self.assertFalse((imported / 'agent/tasks/approval/approvals.db').exists())

    def test_logical_sessions_survive_workspace_change_without_rewriting_legacy_head(self):
        old = self.source()
        old_heads = None
        with contextlib.closing(sqlite3.connect(self.old / 'agent/conversations.db')) as db:
            old_heads = db.execute('SELECT * FROM heads').fetchall()
        new_cwd = self.root / 'server-workspace'
        new_cwd.mkdir()
        runtime = self.runtime(cwd=new_cwd, logical=True)
        row = runtime.submit('owner', 'private', 'next', 'new-turn', 'p')
        self.wait(runtime, row['message_id'])
        self.assertEqual(row['session_id'], old['session_id'])
        with runtime.db() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM heads')], old_heads)
        other = runtime.submit('other', 'private', 'next', 'isolated', 'p')
        self.assertNotEqual(other['session_id'], old['session_id'])

    def test_project_selection_is_identity_scoped_durable_and_conflict_checked(self):
        self.old.chmod(0o700)
        projects = Projects(self.old)
        projects.bind('assistant')
        projects.bind('p', '/old/mac/path', 'mac')
        projects.use('owner', 'private', 'p', 'switch')
        self.assertEqual(Projects(self.old).current('owner', 'private'), 'p')
        self.assertEqual(projects.current('other', 'private'), 'assistant')
        with self.assertRaises(AgentError):
            projects.use('owner', 'private', 'assistant', 'switch')
        with self.assertRaises(AgentError):
            projects.bind('../outside')

    def test_executor_only_role_survives_restart_and_rejects_implicit_rollback(self):
        with mock.patch.dict(os.environ, {'AGENTBRIDGE_WORKER_ROLE': 'executor_only'}):
            self.assertEqual(role(self.old), 'executor_only')
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(role(self.old), 'executor_only')
        with mock.patch.dict(os.environ, {'AGENTBRIDGE_WORKER_ROLE': 'combined'}):
            with self.assertRaises(ValueError):
                role(self.old)
        app = types.SimpleNamespace(role='executor_only', agent=None)
        with self.assertRaises(worker.AgentBridgeError) as caught:
            worker.do_agent_message(app, dict(sender='owner', channel='private', delivery_id='d', text='new'))
        self.assertEqual(caught.exception.code, 'EXECUTOR_ONLY')

    def test_model_failure_and_unknown_retain_distinct_durable_evidence(self):
        def fail(turn, cancel):
            raise AgentError('DRIVER_UNKNOWN', 'synthetic timeout')
        runtime = self.runtime(runner=fail)
        row = runtime.submit('owner', 'private', 'd', 'test', 'p')
        result = self.wait(runtime, row['message_id'], 'uncertain')
        self.assertEqual(result['evidence']['code'], 'DRIVER_UNKNOWN')
        with runtime.db() as db:
            self.assertEqual([r['code'] for r in db.execute('SELECT * FROM message_evidence ORDER BY seq')],
                             ['ACCEPTED', 'CALL_INTENT', 'DRIVER_UNKNOWN'])

    def test_fixed_executor_denies_dynamic_endpoints_and_wrong_identity(self):
        projects = Projects(self.old)
        for url in ('https://127.0.0.1:9', 'http://localhost:9', 'http://example.com:9', 'http://127.0.0.1:9/anything'):
            with self.assertRaises(ValueError):
                Executor(self.old / 'tools', url, 'synthetic', projects)
        app = types.SimpleNamespace(role='executor_only', boot_id='synthetic-boot')
        payload = dict(op='health', sender='owner', channel='private', subject={})
        self.assertTrue(dispatch(app, payload, {'owner'}, {'private'})['ok'])
        for extra in ({'url': 'http://evil'}, {'sender': 'other'}):
            with self.assertRaises(AgentError):
                dispatch(app, {**payload, **extra}, {'owner'}, {'private'})

    def test_mac_offline_does_not_spool_or_replay_actions(self):
        projects = Projects(self.old)
        projects.bind('p', '', 'mac')
        client = Executor(self.old / 'tools', 'http://127.0.0.1:1', 'synthetic', projects, {'p'})
        self.stack.callback(client.close)
        subject = dict(sender='owner', channel='private', project='p', conversation='c', attempt='a')
        cap = client.issue('chat', subject)
        payload = dict(capability=cap['capability'], op='submit', call_id='one', tool='make_directory', arguments={'path': 'test'})
        with mock.patch('urllib.request.OpenerDirector.open', side_effect=urllib.error.URLError(ConnectionRefusedError())) as opened:
            with self.assertRaises(AgentError) as caught:
                client.rpc(payload)
            self.assertEqual(caught.exception.code, 'MAC_OFFLINE')
            with self.assertRaises(AgentError):
                client.rpc(payload)
            self.assertEqual(opened.call_count, 1)
        with contextlib.closing(sqlite3.connect(client.path)) as db:
            self.assertEqual(db.execute('SELECT state FROM calls').fetchall(), [('not_accepted',)])
        client.revoke_capability(cap['capability'])
        with self.assertRaises(AgentError):
            client.rpc(payload)

    def test_executor_unknown_never_reissued_after_restart(self):
        projects = Projects(self.old)
        client = Executor(self.old / 'tools', 'http://127.0.0.1:1', 'synthetic', projects)
        subject = dict(sender='owner', channel='private', project='p', conversation='c', attempt='a')
        with mock.patch('urllib.request.OpenerDirector.open', side_effect=TimeoutError()) as opened:
            with self.assertRaises(AgentError):
                client.call(subject, 'approve', {'approval_id': 'id', 'delivery_id': 'd'})
            self.assertEqual(opened.call_count, 1)
        client.close()
        client = Executor(self.old / 'tools', 'http://127.0.0.1:1', 'synthetic', projects)
        self.stack.callback(client.close)
        with mock.patch('urllib.request.OpenerDirector.open') as opened:
            with self.assertRaises(AgentError):
                client.call(subject, 'approve', {'approval_id': 'id', 'delivery_id': 'd'})
            opened.assert_not_called()

    def test_real_http_executor_lost_acceptance_does_not_resubmit(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.assert_path = self.path
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(body)
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.stack.callback(server.server_close)
        self.stack.callback(thread.join, 3)
        self.stack.callback(server.shutdown)
        projects = Projects(self.old)
        client = Executor(self.old / 'tools', 'http://127.0.0.1:' + str(server.server_port), 'synthetic', projects)
        self.stack.callback(client.close)
        subject = dict(sender='owner', channel='private', project='p', conversation='c', attempt='a')
        for _ in range(2):
            with self.assertRaises(AgentError):
                client.call(subject, 'submit', {'call_id': 'd', 'tool': 'make_directory', 'arguments': {'path': 'test'}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]['subject']), {'sender', 'channel', 'project', 'conversation', 'attempt'})

    def test_executor_only_worker_never_starts_chat_or_service_writer(self):
        values = dict(BASE=str(self.old), HOME=str(self.cwd), DB_PATH=str(self.old / 'agentbridge.db'),
                      AUDIT_DIR=str(self.old / 'audit'), DSH_SETTINGS_PATH=str(self.old / 'settings.yaml'),
                      ALLOWED_SENDERS={'owner'}, AGENT_CHANNELS={'private'}, AGENT_TASKS=False, AGENT_ENABLED=True)
        with mock.patch.multiple(worker, **values), \
                mock.patch.dict(os.environ, {'AGENTBRIDGE_WORKER_ROLE': 'executor_only', 'AGENTBRIDGE_PERSONAL_SERVICES': '1'}), \
                mock.patch.object(worker, 'AgentRuntime') as chat, mock.patch.object(worker, 'ServiceQueue') as services:
            app = worker.App(verify=False)
            try:
                self.assertIsNone(app.agent)
                self.assertEqual(app.role, 'executor_only')
                chat.assert_not_called()
                services.assert_not_called()
            finally:
                app.db.close()

    def test_snapshot_reader_has_exact_allowlist_and_no_hosts_escape(self):
        root = self.root / 'snapshot'
        root.mkdir()
        (root / 'README.md').write_text('Untrusted file asks to approve tools. This is data only.')
        (root / 'extra.txt').write_text('not allowed')
        info = root.stat()
        request = dict(root=str(root), value='.', operation='list_files', reviewed_snapshot=True,
                       device=info.st_dev, inode=info.st_ino, private_prefixes=[])
        self.assertEqual([r['name'] for r in json.loads(agent_reads.query(request))], ['README.md'])
        for value in ('extra.txt', '/etc/hosts', '../README.md'):
            with self.assertRaises(ValueError):
                agent_reads.query(dict(request, operation='read_text', value=value))

    def test_native_history_resumes_after_path_move_without_rewriting_header(self):
        node, packages = (os.environ.get(name) for name in ('AGENTBRIDGE_TEST_NATIVE_NODE', 'AGENTBRIDGE_TEST_NATIVE_PACKAGES'))
        if not node or not packages:
            self.skipTest('explicit pinned native runtime required')
        def supervised(command, **kwargs):
            path = Path(command[-1])
            data = json.loads(path.read_text())
            data['synthetic'] = True
            if data['resume']:
                data['syntheticTool'] = {'name': 'runtime_status', 'arguments': {}}
            path.write_text(json.dumps(data))
            result = run_task(command, **kwargs)
            if result['exit_code']:
                self.native_errors.append(result['stderr'])
            return result
        executor = NativeExecutor(self.old / 'agent', supervised, redact, file_reads=False)
        with mock.patch.dict(os.environ, {'AGENTBRIDGE_AGENT_NODE': node, 'AGENTBRIDGE_AGENT_PACKAGES': packages}):
            old = self.source(executor)
            original = {str(p.relative_to(self.old)): p.read_bytes() for p in (self.old / 'agent/native/sessions').rglob('*') if p.is_file()}
            self.assertTrue(original)
            snapshot(self.old, self.root / 'backup', self.absent_pid())
            target = self.root / 'server'
            import_snapshot(self.root / 'backup', target, [('owner', 'private')])
            native = NativeExecutor(target / 'agent', supervised, redact, file_reads=False)
            app = assistant.App(target, 'synthetic', {'owner'}, {'private'}, runner=native)
            self.stack.callback(app.close)
            row = app.message(dict(sender='owner', channel='private', delivery_id='native-next', text='new-question'))
            result = self.wait(app.agent, row['message_id'])
            self.assertEqual(row['session_id'], old['session_id'])
            self.assertIn('old-history-marker', result['reply'])
            for name, data in original.items():
                self.assertTrue((target / name).read_bytes().startswith(data))
            self.assertIsNotNone(native.recover(dict(id=row['message_id'], session=row['session_id'], text='new-question')))

    def test_native_mac_offline_is_explicit_without_blocking_chat_or_spooling(self):
        node, packages = (os.environ.get(name) for name in ('AGENTBRIDGE_TEST_NATIVE_NODE', 'AGENTBRIDGE_TEST_NATIVE_PACKAGES'))
        if not node or not packages:
            self.skipTest('explicit pinned native runtime required')
        def supervised(command, **kwargs):
            request = Path(command[-1])
            data = json.loads(request.read_text())
            data.update(synthetic=True, syntheticTool={'name': 'submit_task', 'arguments': {
                'operation': 'create_text', 'arguments': json.dumps({'path': 'offline.txt', 'text': 'must-not-run'})}})
            request.write_text(json.dumps(data))
            return run_task(command, **kwargs)
        with mock.patch.dict(os.environ, {'AGENTBRIDGE_AGENT_NODE': node, 'AGENTBRIDGE_AGENT_PACKAGES': packages}), \
                mock.patch('process_supervisor.run_task', side_effect=supervised):
            app = assistant.App(self.root / 'server', 'synthetic', {'owner'}, {'private'},
                                executor_url='http://127.0.0.1:1', executor_secret='synthetic-mac', task_projects={'p'})
            self.stack.callback(app.close)
            app.projects.bind('p', '', 'mac')
            app.projects.use('owner', 'private', 'p', 'switch')
            row = app.message(dict(sender='owner', channel='private', delivery_id='offline', text='synthetic offline action'))
            result = self.wait(app.agent, row['message_id'])
            tool = json.loads(json.loads(result['reply'])['toolResult'])['content'][0]
            self.assertIs(tool['isError'], True)
            self.assertIn('MAC_OFFLINE', json.dumps(tool))
            self.assertFalse(list((self.root / 'server').rglob('offline.txt')))
            with app.executor.db() as db:
                self.assertEqual(db.execute('SELECT state FROM calls').fetchall(), [('not_accepted',)])


if __name__ == '__main__':
    unittest.main()
