"""Isolated background lanes and actual native approvals. No production data."""
import concurrent.futures
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from unittest import mock

from agent_actions import prepare, execute
from agent_runtime import AgentError, AgentRuntime
from agent_tasks import TaskRuntime
from agent_task_executor import NativeTaskExecutor
import worker
import shim
import test_agent_runtime as chat_tests


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='abt-', dir='/private/tmp'))).resolve()
        self.workspace = self.root / 'work'
        self.workspace.mkdir()
        self.release = threading.Event()
        self.release.set()
        self.stack.callback(self.release.set)
        self.calls = []
        self.runtime = self.make()
        self.subject = dict(sender='synthetic-owner', channel='synthetic-private', conversation=uuid.uuid4().hex,
                            project='p', cwd=str(self.workspace), session=uuid.uuid4().hex, attempt=uuid.uuid4().hex)

    def runner(self, task, cancel):
        self.calls.append(task)
        while not self.release.wait(.01):
            if cancel.is_set():
                raise AgentError('CANCELLED', 'synthetic executor stopped')
        return {'completion': 'verified', 'evidence': 'synthetic-only'}

    def make(self, runner=None, recover=lambda _row: None):
        value = TaskRuntime(self.root / 'a/t', runner or self.runner, lambda _name: str(self.workspace), enabled=True, recover=recover)
        self.stack.callback(value.close)
        return value

    def submit(self, delivery='synthetic-call', arguments=None, subject=None, tool='create_text'):
        return self.runtime.submit(subject or self.subject, delivery, tool, arguments or {'path': 'synthetic.txt', 'text': 'synthetic-data'})

    def wait(self, identifier, state='verified'):
        deadline = time.monotonic() + 12
        value = None
        while time.monotonic() < deadline:
            value = self.runtime.inspect(self.subject['sender'], self.subject['channel'], self.subject['conversation'], 'p', identifier)
            if value['state'] == state:
                return value
            time.sleep(.015)
        self.fail('task state did not reach ' + state + ': ' + str(value))

    def cancel(self, identifier, delivery='synthetic-cancel'):
        return self.runtime.cancel(self.subject['sender'], self.subject['channel'], self.subject['conversation'], 'p', identifier, delivery)

    def test_concurrent_duplicate_dispatch_is_one_task_and_one_execution(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.submit(), range(20)))
        self.assertEqual(len({row['task_id'] for row in rows}), 1)
        self.wait(rows[0]['task_id'])
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(AgentError):
            self.submit(arguments={'path': 'different.txt', 'text': 'synthetic'})

    def test_source_conversation_and_project_isolation(self):
        row = self.submit()
        self.wait(row['task_id'])
        values = [self.subject['sender'], self.subject['channel'], self.subject['conversation'], 'p']
        for index in range(4):
            foreign = values[:]
            foreign[index] = 'foreign'
            with self.assertRaises(AgentError):
                self.runtime.inspect(*foreign, row['task_id'])
        self.assertEqual(self.runtime.events('foreign', self.subject['channel'])['events'], [])
        self.assertEqual(self.runtime.events(self.subject['sender'], self.subject['channel'], 'different-conversation')['events'], [])

    def test_task_waiting_does_not_block_chat_lane(self):
        self.release.clear()
        task = self.submit()
        self.wait(task['task_id'], 'running')
        chat = AgentRuntime(self.root / 'chat', lambda *_: 'chat stayed responsive', lambda _: str(self.workspace))
        try:
            message = chat.submit('synthetic-owner', 'synthetic-private', 'chat-1', 'hello', 'p')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                value = chat.control('synthetic-owner', 'synthetic-private', 'agent.status', 'p', message['message_id'])
                if value['state'] == 'done':
                    break
                time.sleep(.01)
            self.assertEqual(value['reply'], 'chat stayed responsive')
            self.assertEqual(self.wait(task['task_id'], 'running')['state'], 'running')
        finally:
            chat.close()
            self.release.set()

    def test_queued_and_running_cancellation_do_not_replay(self):
        self.release.clear()
        first = self.submit()
        self.wait(first['task_id'], 'running')
        second = self.submit('second')
        self.assertEqual(self.cancel(second['task_id'])['state'], 'cancelled')
        with self.assertRaises(AgentError):
            self.cancel(first['task_id'])
        self.cancel(first['task_id'], 'cancel-first')
        self.wait(first['task_id'], 'cancelled')
        self.release.set()
        self.assertEqual(len(self.calls), 1)

    def test_unknown_action_is_not_retried_and_fences_legacy_execution(self):
        def broken(*_):
            raise OSError('synthetic connection lost')
        self.runtime.runner = broken
        row = self.submit()
        self.wait(row['task_id'], 'uncertain')
        with self.assertRaises(AgentError):
            self.submit('new-attempt')
        with self.assertRaises(AgentError), self.runtime.execution_fence(threading.Event()):
            self.fail('unknown fence allowed another executor')
        self.assertEqual(self.submit()['state'], 'uncertain')

    def test_global_fence_serializes_legacy_and_new_actions(self):
        with self.runtime.execution_fence(threading.Event()):
            row = self.submit()
            self.wait(row['task_id'], 'running')
            self.assertEqual(self.calls, [])
        self.wait(row['task_id'])
        self.assertEqual(len(self.calls), 1)

    def test_legacy_fifo_precedes_global_fence_without_reverse_arrival_deadlock(self):
        scheduler = worker.Scheduler(2, 8)
        gate = scheduler.resource_gate('synthetic-resource')
        tickets = [gate.enqueue(index) for index in (1, 2)]
        cancels = [threading.Event(), threading.Event()]
        app = types.SimpleNamespace(agent_tasks=self.runtime, sched=scheduler, _admit_lock=threading.RLock(),
                                    _cancellations={'one': cancels[0], 'two': cancels[1]}, db=mock.Mock(), boot_id='synthetic',
                                    _check_quarantine=mock.Mock())
        app.db.execute.return_value.rowcount = 1
        snapshots = [dict(job_id=name, attempt_id=name, lock_key='synthetic-resource', ticket=tickets[index],
                          cwd=str(self.workspace), project='p', task='synthetic', kind='read_only')
                     for index, name in enumerate(('one', 'two'))]
        second_at_gate = threading.Event()
        entered = gate.enter
        def enter(ticket, cancelled):
            if ticket is tickets[1]:
                second_at_gate.set()
            return entered(ticket, cancelled)
        completed, errors = [], []
        def run(snapshot):
            try:
                worker._run_claimed(app, snapshot)
            except Exception as error:
                errors.append(type(error).__name__)
        threads = [threading.Thread(target=run, args=(snapshot,), daemon=True) for snapshot in snapshots]
        with mock.patch.object(gate, 'enter', side_effect=enter), \
                mock.patch.object(worker, 'build_context', return_value=''), \
                mock.patch.object(worker, 'effective_kind', return_value=('read_only', False)), \
                mock.patch.object(worker, 'build_cmd', return_value=(['synthetic'], 'read-only', None)), \
                mock.patch.object(worker, 'run_task', return_value={'group_stopped': True}), \
                mock.patch.object(worker, '_finalize', side_effect=lambda _app, snap, *_: completed.append(snap['job_id'])):
            scheduler.reserve()
            scheduler.reserve()
            threads[1].start()
            try:
                self.assertTrue(second_at_gate.wait(2))
                threads[0].start()
                for thread in threads:
                    thread.join(3)
                self.assertFalse(any(thread.is_alive() for thread in threads), 'FIFO/global-fence lock inversion')
                self.assertEqual(completed, ['one', 'two'])
                self.assertEqual(errors, [])
            finally:
                for cancel in cancels:
                    cancel.set()
                for thread in threads:
                    if thread.ident:
                        thread.join(3)

    def test_replaced_project_denied_before_execution(self):
        with self.runtime.execution_fence(threading.Event()):
            row = self.submit()
            self.wait(row['task_id'], 'running')
            self.workspace.rename(self.root / 'old-work')
            self.workspace.mkdir()
        self.wait(row['task_id'], 'failed')
        self.assertEqual(self.calls, [])

    def test_terminal_event_is_durable_and_unique(self):
        row = self.submit()
        self.wait(row['task_id'])
        before = self.runtime.events(self.subject['sender'], self.subject['channel'])['events']
        self.assertEqual([event['kind'] for event in before], ['accepted', 'terminal'])
        self.runtime.close()
        self.runtime = self.make()
        self.assertEqual(self.runtime.events(self.subject['sender'], self.subject['channel'])['events'], before)
        self.assertEqual(len(self.calls), 1)

    def test_crash_running_task_is_uncertain_without_auto_execute(self):
        self.runtime.close()
        # Simulate the already-durable launch intent without launching a process.
        with self.runtime.db() as db:
            info = self.workspace.stat()
            db.execute('INSERT INTO tasks(id,origin,delivery,sender,channel,conversation,project,cwd,device,inode,session,tool,'
                       'arguments,digest,state,result,created,exec_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('interrupted', 'origin', 'delivery', self.subject['sender'], self.subject['channel'], self.subject['conversation'],
                        'p', str(self.workspace), info.st_dev, info.st_ino, 'native-session', 'create_text', '{}', 'digest', 'running', '', time.time(), 'starting'))
        self.runtime = self.make()
        self.wait('interrupted', 'uncertain')
        self.assertEqual(self.calls, [])

    def test_capability_is_purpose_bound_and_cannot_forge_source(self):
        cap = self.runtime.issue('chat', self.subject)
        payload = dict(capability=cap['capability'], op='submit', call_id='fixed-call', tool='create_text',
                       arguments={'path': 'synthetic.txt', 'text': 'synthetic'})
        result = self.runtime.rpc(payload)
        self.wait(result['task_id'])
        for altered in ({**payload, 'sender': 'foreign'}, {**payload, 'op': 'consume'}, {**payload, 'capability': 'forged'}):
            with self.assertRaises(AgentError):
                self.runtime.rpc(altered)
        self.runtime.revoke_capability(cap['capability'])
        with self.assertRaises(AgentError):
            self.runtime.rpc(payload)

    def test_cancelled_and_expired_chat_capability_cannot_enqueue(self):
        cancel = threading.Event()
        cap = self.runtime.issue('chat', self.subject, cancel=cancel)
        cancel.set()
        request = dict(capability=cap['capability'], op='submit', call_id='x', tool='create_text',
                       arguments={'path': 'new.txt', 'text': 'synthetic'})
        with self.assertRaises(AgentError):
            self.runtime.rpc(request)
        cap = self.runtime.issue('chat', self.subject, ttl=-1)
        request['capability'] = cap['capability']
        with self.assertRaises(AgentError):
            self.runtime.rpc(request)

    def test_private_socket_denies_unknown_body_without_recording_task(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(self.runtime.socket_path)
            connection.sendall(b'{"capability":"forged","op":"submit"}\n')
            value = json.loads(connection.recv(65536))
        self.assertFalse(value['ok'])
        self.assertEqual(self.runtime.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.runtime.socket_path).stat().st_mode & 0o777, 0o600)

    def test_default_off_is_enforced(self):
        self.runtime.enabled = False
        with self.assertRaises(AgentError):
            self.submit()

    def test_project_canary_allowlist_denies_other_projects_and_empty_scope(self):
        for projects in (frozenset(), frozenset({'canary-only'})):
            self.runtime.allowed_projects = projects
            self.assertFalse(self.runtime.can_submit('p'))
            with self.assertRaises(AgentError):
                self.submit()
            with self.assertRaises(AgentError):
                self.runtime.issue('chat', self.subject)
        self.runtime.allowed_projects = frozenset({'p'})
        self.wait(self.submit()['task_id'])

    def test_revoked_project_scope_rejects_issued_capability(self):
        cap = self.runtime.issue('chat', self.subject)
        self.runtime.allowed_projects = frozenset({'other'})
        with self.assertRaises(AgentError):
            self.runtime.rpc(dict(capability=cap['capability'], op='submit', call_id='fixed-call', tool='create_text',
                                  arguments={'path': 'synthetic.txt', 'text': 'synthetic'}))
        self.assertEqual(self.calls, [])

    def test_queued_project_revocation_prevents_action_body(self):
        with self.runtime.execution_fence(threading.Event()):
            row = self.submit()
            self.wait(row['task_id'], 'running')
            self.runtime.allowed_projects = frozenset()
        self.wait(row['task_id'], 'failed')
        self.assertEqual(self.calls, [])

    def test_invalid_result_fails_closed_without_killing_lane(self):
        self.runtime.runner = lambda *_: {'completion': 'verified', 'bad': object()}
        row = self.submit()
        value = self.wait(row['task_id'], 'uncertain')
        self.assertIsNone(value['result'])
        self.assertTrue(self.runtime.thread.is_alive())
        with self.assertRaises(AgentError), self.runtime.execution_fence(threading.Event()):
            self.fail('invalid receipt escaped quarantine')

    def test_external_quarantine_blocks_new_executor(self):
        self.runtime.check_external = mock.Mock(side_effect=AgentError('TASK_UNCERTAIN', 'synthetic legacy quarantine'))
        row = self.submit()
        self.wait(row['task_id'], 'failed')
        self.assertEqual(self.calls, [])

    def test_close_is_idempotent_and_removes_only_owned_socket(self):
        path = Path(self.runtime.socket_path)
        self.assertTrue(path.exists())
        self.runtime.close()
        self.runtime.close()
        self.assertFalse(path.exists())
        self.assertFalse(self.runtime.thread.is_alive())
        self.assertFalse(self.runtime.server_thread.is_alive())

    def test_event_cursor_starts_at_oldest_not_latest_and_never_skips(self):
        row = self.submit()
        self.wait(row['task_id'])
        with self.runtime.db() as db:
            for index in range(130):
                db.execute('INSERT INTO task_events(id,task,kind,sender,channel,conversation,project,body,created) '
                           'VALUES (?,?,?,?,?,?,?,?,?)', (str(index), str(index), 'terminal', self.subject['sender'],
                            self.subject['channel'], self.subject['conversation'], 'p', '{}', time.time()))
        first = self.runtime.events(self.subject['sender'], self.subject['channel'], self.subject['conversation'], 'p')['events']
        second = self.runtime.events(self.subject['sender'], self.subject['channel'], self.subject['conversation'], 'p', first[-1]['seq'])['events']
        self.assertEqual([event['seq'] for event in first + second], list(range(1, 133)))

    def test_chat_event_cursor_advances_only_after_completed_turn(self):
        task = self.submit()
        self.wait(task['task_id'])
        turns = []
        def run_chat(turn, _cancel):
            turns.append(turn)
            return 'synthetic complete'
        chat = AgentRuntime(self.root / 'chat', run_chat, lambda _: str(self.workspace), tasks=self.runtime)
        self.stack.callback(chat.close)
        first = chat.submit(self.subject['sender'], self.subject['channel'], 'one', 'hi', 'p')
        # Bind synthetic task events to the actual trusted native conversation.
        with self.runtime.db() as db:
            db.execute('UPDATE task_events SET conversation=?', (first['session_id'],))
        def done(mid):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                value = chat.control(self.subject['sender'], self.subject['channel'], 'agent.status', 'p', mid)
                if value['state'] == 'done':
                    return
                time.sleep(.01)
            self.fail('chat did not finish')
        done(first['message_id'])
        # A second turn drains anything that raced the first snapshot.
        second = chat.submit(self.subject['sender'], self.subject['channel'], 'two', 'hi', 'p')
        done(second['message_id'])
        third = chat.submit(self.subject['sender'], self.subject['channel'], 'three', 'hi', 'p')
        done(third['message_id'])
        self.assertEqual(sum(len(turn['task_events']) for turn in turns), 2)
        self.assertEqual(turns[-1]['task_events'], [])


class ActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aba-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'work'
        self.workspace.mkdir()
        # The real helper runs in its own process and fchdir binds the command
        # to the approved directory descriptor. In-process unit calls must not
        # leave discovery in a deleted temporary working directory.
        directory = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, directory)
        self.addCleanup(os.fchdir, directory)

    def action(self, tool, arguments):
        actual = prepare(str(self.workspace), tool, arguments)
        return execute(dict(root=str(self.workspace), tool=tool, arguments=actual, private_prefixes=[], home=str(self.root)))

    def test_create_new_text_verified_after_fsync(self):
        result = self.action('create_text', {'path': 'new.txt', 'text': 'synthetic text'})
        self.assertEqual(result['completion'], 'verified')
        self.assertEqual(result['sha256'], hashlib.sha256((self.workspace / 'new.txt').read_bytes()).hexdigest())
        self.assertEqual((self.workspace / 'new.txt').stat().st_mode & 0o777, 0o600)

    def test_make_directory_without_overwriting_existing_paths(self):
        self.assertEqual(self.action('make_directory', {'path': 'new'})['completion'], 'verified')
        with self.assertRaises(ValueError):
            self.action('make_directory', {'path': 'new'})

    def test_target_created_after_approval_is_never_overwritten(self):
        actual = prepare(str(self.workspace), 'create_text', {'path': 'new.txt', 'text': 'synthetic new'})
        (self.workspace / 'new.txt').write_text('synthetic concurrent original')
        with self.assertRaises(ValueError):
            execute(dict(root=str(self.workspace), tool='create_text', arguments=actual, private_prefixes=[]))
        self.assertEqual((self.workspace / 'new.txt').read_text(), 'synthetic concurrent original')

    def test_parent_symlinks_and_private_or_outside_targets_denied(self):
        (self.workspace / 'link').symlink_to(self.root)
        for value in ('../outside.txt', '.env', '.config/a', 'link/outside.txt'):
            with self.subTest(path=value), self.assertRaises((ValueError, OSError)):
                self.action('create_text', {'path': value, 'text': 'synthetic'})

    def test_parent_replacement_requires_new_approval(self):
        child = self.workspace / 'child'
        child.mkdir()
        actual = prepare(str(self.workspace), 'create_text', {'path': 'child/new.txt', 'text': 'synthetic'})
        child.rename(self.workspace / 'prior-child')
        child.mkdir()
        with self.assertRaises(ValueError):
            execute(dict(root=str(self.workspace), tool='create_text', arguments=actual, private_prefixes=[]))
        self.assertFalse((child / 'new.txt').exists())

    def test_no_shell_sudo_interpreter_or_unregistered_command(self):
        for program in ('/bin/sh', '/usr/bin/sudo', '/usr/bin/python3', '/usr/bin/env', 'git'):
            with self.subTest(program=program), self.assertRaises(ValueError):
                self.action('run_command', {'program': program, 'argv': '[]'})

    def test_allowed_command_has_process_evidence_not_goal_success(self):
        value = self.action('run_command', {'program': '/bin/echo', 'argv': '["synthetic-command"]'})
        self.assertEqual(value['completion'], 'process-ended')
        self.assertEqual(value['exit_code'], 0)
        self.assertEqual(value['stdout'], 'synthetic-command\n')

    def test_git_configuration_cannot_launch_fsmonitor_or_other_commands(self):
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.workspace)], check=True, capture_output=True)
        sentinel = self.workspace / 'not-executed'
        subprocess.run(['/usr/bin/git', '-C', str(self.workspace), 'config', 'core.fsmonitor',
                        'touch ' + str(sentinel)], check=True, capture_output=True)
        value = self.action('run_command', {'program': '/usr/bin/git', 'argv': '["status","--short"]'})
        self.assertEqual(value['exit_code'], 0)
        self.assertFalse(sentinel.exists())
        for args in ('["-c","alias.escape=!touch pwned","escape"]', '["diff","--ext-diff"]', '["push"]'):
            with self.subTest(argv=args), self.assertRaises(ValueError):
                self.action('run_command', {'program': '/usr/bin/git', 'argv': args})


class NativeTaskTests(unittest.TestCase):
    def setUp(self):
        node, packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE'), os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not node or not packages:
            self.skipTest('explicit native test runtime required')
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {'AGENTBRIDGE_AGENT_NODE': node, 'AGENTBRIDGE_AGENT_PACKAGES': packages}))

    def native(self):
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='abx-', dir='/private/tmp'))).resolve()
        workspace = root / 'work'
        workspace.mkdir()
        executor = NativeTaskExecutor(root / 'a/t/native', worker.run_task, worker._executor_identity, lambda _: 'dead')
        runtime = TaskRuntime(root / 'a/t', executor, lambda _: str(workspace), enabled=True, recover=executor.recover)
        self.stack.callback(runtime.close)
        subject = dict(sender='synthetic-owner', channel='synthetic-private', conversation='original',
                       session=uuid.uuid4().hex, project='p', cwd=str(workspace), attempt=uuid.uuid4().hex)
        task = runtime.submit(subject, 'native-call', 'create_text', {'path': 'result.txt', 'text': 'synthetic-native-result'})
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            values = runtime.approvals.inspect(subject['sender'], subject['channel'], 'original', 'p')['approvals']
            if values:
                return runtime, workspace, executor, subject, task, values[0]
            time.sleep(.02)
        self.fail('native approval not published')

    def terminal(self, runtime, subject, task, expected):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            value = runtime.inspect(subject['sender'], subject['channel'], 'original', 'p', task['task_id'])
            if value['state'] in ('verified', 'denied', 'cancelled', 'uncertain', 'failed'):
                self.assertEqual(value['state'], expected)
                return value
            time.sleep(.02)
        self.fail('native task not terminal')

    def test_pending_cancel_never_enters_body_and_records_stopped_group(self):
        runtime, workspace, _executor, subject, task, approval = self.native()
        runtime.cancel(subject['sender'], subject['channel'], 'original', 'p', task['task_id'], 'cancel-native')
        self.terminal(runtime, subject, task, 'cancelled')
        self.assertFalse((workspace / 'result.txt').exists())
        self.assertEqual(runtime.approvals.inspect(subject['sender'], subject['channel'], 'original', 'p', approval['approval_id'])['state'], 'revoked')

    def test_expired_approval_does_not_run_body(self):
        runtime, workspace, _executor, subject, task, approval = self.native()
        runtime.approvals.clock = lambda: approval['expires'] + 1
        value = self.terminal(runtime, subject, task, 'denied')
        self.assertEqual(value['result']['reason'], 'approval_expired')
        self.assertEqual(value['result']['approval_state'], 'expired')
        self.assertEqual(value['result']['approval_id'], approval['approval_id'])
        self.assertEqual(value['result']['evidence'], 'tool_body_not_entered')
        events = runtime.events(subject['sender'], subject['channel'], 'original', 'p')['events']
        self.assertEqual(events[-1]['body']['result'], value['result'])
        self.assertFalse((workspace / 'result.txt').exists())

    def test_expiry_at_consume_preserves_reason_and_never_enters_body(self):
        runtime, workspace, _executor, subject, task, approval = self.native()
        consume = runtime.approvals.consume

        def expire(attempt, request, grant):
            runtime.approvals.clock = lambda: approval['expires'] + 1
            return consume(attempt, request, grant)

        with mock.patch.object(runtime.approvals, 'consume', side_effect=expire) as called:
            runtime.approvals.decide(subject['sender'], subject['channel'], 'original', 'p',
                                     approval['approval_id'], 'approve-before-expiry', True)
            value = self.terminal(runtime, subject, task, 'denied')
            called.assert_called_once()
        self.assertEqual(value['result']['reason'], 'approval_expired')
        self.assertEqual(value['result']['approval_state'], 'expired')
        self.assertFalse((workspace / 'result.txt').exists())

    def test_expiry_before_native_guard_preserves_reason_without_consuming(self):
        runtime, workspace, _executor, subject, task, approval = self.native()
        request = runtime.approvals.request

        def expire_before_return(attempt, envelope):
            value = request(attempt, envelope)
            if value['state'] == 'approved':
                # Synthetic expiry between the host response and native guard.
                with runtime.approvals.db() as db:
                    db.execute('UPDATE approvals SET expires=? WHERE id=?',
                               (time.time() - 1, approval['approval_id']))
                value['grant']['expiresAt'] = (time.time() - 1) * 1000
            return value

        with mock.patch.object(runtime.approvals, 'request', side_effect=expire_before_return), \
                mock.patch.object(runtime.approvals, 'consume', wraps=runtime.approvals.consume) as consumed:
            runtime.approvals.decide(subject['sender'], subject['channel'], 'original', 'p',
                                     approval['approval_id'], 'approve-before-guard-expiry', True)
            value = self.terminal(runtime, subject, task, 'denied')
            consumed.assert_not_called()
        self.assertEqual(value['result']['reason'], 'approval_expired')
        self.assertEqual(value['result']['approval_state'], 'expired')
        self.assertFalse((workspace / 'result.txt').exists())

    def test_denial_reason_survives_receipt_recovery_without_rerun(self):
        runtime, workspace, executor, subject, task, approval = self.native()
        runtime.approvals.decide(subject['sender'], subject['channel'], 'original', 'p',
                                 approval['approval_id'], 'explicit-denial', False)
        before = self.terminal(runtime, subject, task, 'denied')
        runtime.close()
        with runtime.db() as db:
            db.execute("UPDATE tasks SET state='running',result='' WHERE id=?", (task['task_id'],))
            db.execute("DELETE FROM task_events WHERE kind='terminal'")
        executor.run_task = mock.Mock(side_effect=AssertionError('denied actions must not rerun'))
        restored = TaskRuntime(runtime.root, executor, lambda _: str(workspace), enabled=True, recover=executor.recover)
        self.stack.callback(restored.close)
        after = self.terminal(restored, subject, task, 'denied')
        self.assertEqual(before['result'], after['result'])
        self.assertEqual(after['result']['reason'], 'user_denied')
        self.assertFalse((workspace / 'result.txt').exists())
        executor.run_task.assert_not_called()
        # Older private receipts remain valid, but no reason is invented for them.
        receipt_path = executor.root / 'receipts' / (task['task_id'] + '.json')
        old = json.loads(receipt_path.read_text())
        old['result'] = {'completion': 'denied', 'evidence': 'tool_body_not_entered'}
        receipt_path.write_text(json.dumps(old))
        with restored.db() as db:
            row = dict(db.execute('SELECT * FROM tasks WHERE id=?', (task['task_id'],)).fetchone())
        self.assertEqual(executor.receipt(row), old['result'])

    def test_revoked_connection_denies_action_and_chat_remains_responsive(self):
        runtime, workspace, _executor, subject, task, _approval = self.native()
        chat = AgentRuntime(workspace.parent / 'chat', lambda *_: 'synthetic responsive', lambda _: str(workspace))
        self.stack.callback(chat.close)
        message = chat.submit(subject['sender'], subject['channel'], 'chat-during-approval', 'hello', 'p')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = chat.control(subject['sender'], subject['channel'], 'agent.status', 'p', message['message_id'])
            if value['state'] == 'done':
                break
            time.sleep(.01)
        self.assertEqual(value['reply'], 'synthetic responsive')
        self.assertFalse((workspace / 'result.txt').exists())
        with runtime.lock:
            runtime.capabilities.clear()
        value = self.terminal(runtime, subject, task, 'denied')
        self.assertEqual(value['result']['reason'], 'approval_unavailable')
        self.assertFalse((workspace / 'result.txt').exists())

    def test_forged_request_and_changed_actual_parameters_cannot_consume_approval(self):
        runtime, workspace, _executor, subject, task, approval = self.native()
        with runtime.approvals.db() as db:
            request = json.loads(db.execute('SELECT request FROM approvals WHERE id=?', (approval['approval_id'],)).fetchone()[0])
        broker = next((token for token, cap in runtime.capabilities.items() if cap['kind'] == 'task'))
        runtime.approvals.decide(subject['sender'], subject['channel'], 'original', 'p', approval['approval_id'], 'synthetic-approved', True)
        for changed in ({**request, 'subject': {**request['subject'], 'channel': 'foreign'}},
                        {**request, 'arguments': {**request['arguments'], 'path': 'other.txt'}}):
            with self.assertRaises(AgentError):
                runtime.rpc({'capability': broker, 'op': 'consume', 'request': changed, 'grant': approval['grant']})
        self.terminal(runtime, subject, task, 'verified')
        self.assertFalse((workspace / 'other.txt').exists())

    def test_restart_recovers_receipt_without_reexecuting_and_rejects_old_capability(self):
        runtime, workspace, executor, subject, task, approval = self.native()
        broker = runtime.issue('chat', subject)
        runtime.approvals.decide(subject['sender'], subject['channel'], 'original', 'p', approval['approval_id'], 'synthetic-approved', True)
        before = self.terminal(runtime, subject, task, 'verified')
        runtime.close()
        # Simulate a crash after the native receipt fsync but before terminal DB commit.
        with runtime.db() as db:
            db.execute("UPDATE tasks SET state='running',result='' WHERE id=?", (task['task_id'],))
            db.execute("DELETE FROM task_events WHERE kind='terminal'")
        executor.run_task = mock.Mock(side_effect=AssertionError('recovery must never reexecute'))
        restored = TaskRuntime(runtime.root, executor, lambda _: str(workspace), enabled=True, recover=executor.recover)
        self.stack.callback(restored.close)
        after = self.terminal(restored, subject, task, 'verified')
        self.assertEqual(before['result'], after['result'])
        self.assertEqual((workspace / 'result.txt').read_text(), 'synthetic-native-result')
        with self.assertRaises(AgentError):
            restored.rpc({'capability': broker['capability'], 'op': 'submit', 'call_id': 'old', 'tool': 'make_directory', 'arguments': {'path': 'old'}})
        executor.run_task.assert_not_called()

    def test_real_native_action_body_enters_only_after_persistent_confirmation(self):
        node, packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE'), os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not node or not packages:
            self.skipTest('explicit native test runtime required')
        with mock.patch.dict(os.environ, {'AGENTBRIDGE_AGENT_NODE': node, 'AGENTBRIDGE_AGENT_PACKAGES': packages}):
            for allow in (False, True):
                with self.subTest(allow=allow), contextlib.ExitStack() as stack:
                    root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='abn-', dir='/private/tmp'))).resolve()
                    workspace = root / 'work'
                    workspace.mkdir()
                    executor = NativeTaskExecutor(root / 'a/t/native', worker.run_task, worker._executor_identity,
                                                  lambda _row: 'dead')
                    runtime = TaskRuntime(root / 'a/t', executor, lambda _name: str(workspace), enabled=True, recover=executor.recover)
                    stack.callback(runtime.close)
                    subject = dict(sender='synthetic-owner', channel='synthetic-private', conversation='original',
                                   session=uuid.uuid4().hex, project='p', cwd=str(workspace), attempt=uuid.uuid4().hex)
                    task = runtime.submit(subject, 'native-call', 'create_text', {'path': 'result.txt', 'text': 'synthetic-native-result'})
                    deadline = time.monotonic() + 12
                    pending = None
                    while time.monotonic() < deadline:
                        values = runtime.approvals.inspect(subject['sender'], subject['channel'], 'original', 'p')['approvals']
                        if values:
                            pending = values[0]
                            break
                        time.sleep(.02)
                    self.assertIsNotNone(pending)
                    self.assertFalse((workspace / 'result.txt').exists(), 'tool body ran before approval')
                    runtime.approvals.decide(subject['sender'], subject['channel'], 'original', 'p', pending['approval_id'], 'approval-message', allow)
                    state = None
                    while time.monotonic() < deadline:
                        result = runtime.inspect(subject['sender'], subject['channel'], 'original', 'p', task['task_id'])
                        state = result['state']
                        if state in ('verified', 'denied', 'uncertain'):
                            break
                        time.sleep(.02)
                    self.assertEqual(state, 'verified' if allow else 'denied')
                    self.assertEqual((workspace / 'result.txt').exists(), allow)
                    if allow:
                        self.assertEqual((workspace / 'result.txt').read_text(), 'synthetic-native-result')
                        self.assertEqual(result['result']['evidence'], 'created_new_file_fsynced')
                    else:
                        self.assertEqual(result['result']['reason'], 'user_denied')
                        self.assertEqual(result['result']['approval_state'], 'denied')
                        self.assertEqual(result['result']['approval_id'], pending['approval_id'])
                    events = runtime.events(subject['sender'], subject['channel'], 'original', 'p')['events']
                    self.assertEqual([event['kind'] for event in events], ['accepted', 'approval', 'terminal'])
                    self.assertEqual(events[-1]['body']['result'], result['result'])


class TaskHTTPTests(unittest.TestCase):
    start_server = chat_tests.AgentHTTPTests.start_server
    wait_idle = chat_tests.AgentHTTPTests.wait_idle
    control = chat_tests.AgentHTTPTests.control
    message = chat_tests.AgentHTTPTests.message

    def setUp(self):
        chat_tests.AgentHTTPTests.setUp(self)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='abh-', dir='/private/tmp'))).resolve()
        self.runtime_tasks = TaskRuntime(directory / 'tasks', lambda *_: {'completion': 'verified', 'evidence': 'synthetic'},
                                         lambda name: self.app.get_project(name)['dir'], enabled=True)
        self.stack.callback(self.runtime_tasks.close)
        self.app.agent_tasks = self.runtime_tasks
        self.stack.enter_context(mock.patch.object(worker, 'AGENT_TASKS', True))
        status, row = self.message({'delivery_id': 'synthetic-chat', 'text': 'hello'})
        self.assertEqual(status, 202)
        self.conversation = row['session_id']

    def subject(self):
        return dict(sender='owner', channel='smoke-channel', conversation=self.conversation, project='default',
                    session=self.conversation, attempt=uuid.uuid4().hex, cwd=os.path.realpath(self.app.get_project('default')['dir']))

    def test_event_and_status_routes_reject_spoofing_and_extra_fields(self):
        subject = self.subject()
        row = self.runtime_tasks.submit(subject, 'task-call', 'create_text', {'path': 'synthetic-new.txt', 'text': 'synthetic'})
        status, result = self.control({'op': 'agent.tasks', 'task_id': row['task_id']})
        self.assertEqual(status, 200)
        self.assertEqual(result['task_id'], row['task_id'])
        self.assertFalse(self.control({'op': 'agent.tasks', 'task_id': row['task_id']}, **{'X-AgentBridge-Sender': 'foreign'})[1].get('ok'))
        self.assertEqual(self.control({'op': 'agent.tasks', 'sender': 'owner'})[0], 400)
        status, events = self.control({'op': 'agent.events', 'after': '0'})
        self.assertTrue(events['ok'])
        self.assertTrue(events['events'])
        self.assertFalse(self.control({'op': 'agent.events', 'after': '-1'})[1].get('ok'))

    def test_new_conversation_cannot_authorize_old_task_or_access_its_status(self):
        subject = self.subject()
        row = self.runtime_tasks.submit(subject, 'task-call', 'create_text', {'path': 'synthetic-new.txt', 'text': 'synthetic'})
        self.control({'op': 'agent.new', 'delivery_id': 'new-conversation'})
        status, value = self.control({'op': 'agent.tasks', 'task_id': row['task_id']})
        self.assertFalse(value['ok'])

    def test_task_controls_fail_closed_when_flag_off(self):
        with mock.patch.object(worker, 'AGENT_TASKS', False):
            for operation in ('agent.tasks', 'agent.approvals', 'agent.events'):
                self.assertFalse(self.control({'op': operation})[1].get('ok'))

    def test_http_confirmation_reaches_native_guard_and_only_original_action(self):
        node, packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE'), os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not node or not packages:
            self.skipTest('explicit native test runtime required')
        self.runtime_tasks.close()
        self.stack.enter_context(mock.patch.dict(os.environ, {'AGENTBRIDGE_AGENT_NODE': node, 'AGENTBRIDGE_AGENT_PACKAGES': packages}))
        executor = NativeTaskExecutor(self.runtime_tasks.root / 'native', worker.run_task, worker._executor_identity, lambda _: 'dead')
        self.runtime_tasks = TaskRuntime(self.runtime_tasks.root, executor, lambda name: self.app.get_project(name)['dir'],
                                         enabled=True, recover=executor.recover)
        self.stack.callback(self.runtime_tasks.close)
        self.app.agent_tasks = self.runtime_tasks
        subject = self.subject()
        row = self.runtime_tasks.submit(subject, 'http-native-call', 'create_text', {'path': 'native-http.txt', 'text': 'synthetic-http-result'})
        deadline = time.monotonic() + 12
        approval = None
        while time.monotonic() < deadline:
            status, values = self.control({'op': 'agent.approvals'})
            self.assertEqual(status, 200)
            if values['approvals']:
                approval = values['approvals'][0]
                break
            time.sleep(.02)
        self.assertIsNotNone(approval)
        self.assertNotIn('grant', approval)
        self.assertFalse((Path(subject['cwd']) / 'native-http.txt').exists())
        status, result = self.control({'op': 'agent.approve', 'approval_id': approval['approval_id'], 'delivery_id': 'http-confirm'})
        self.assertEqual(status, 200)
        self.assertNotIn('grant', result)
        self.assertEqual(result['state'], 'approved')
        while time.monotonic() < deadline:
            status, result = self.control({'op': 'agent.tasks', 'task_id': row['task_id']})
            if result['state'] == 'verified':
                break
            time.sleep(.02)
        self.assertEqual(result['state'], 'verified')
        self.assertEqual((Path(subject['cwd']) / 'native-http.txt').read_text(), 'synthetic-http-result')
        status, repeated = self.control({'op': 'agent.approve', 'approval_id': approval['approval_id'], 'delivery_id': 'http-confirm'})
        self.assertEqual(status, 200)
        self.assertTrue(repeated['reused'])
        self.assertNotIn('grant', repeated)
        self.assertFalse(self.control({'op': 'agent.approve', 'approval_id': approval['approval_id'], 'delivery_id': 'new-confirm'})[1].get('ok'))


class TaskPluginTests(unittest.TestCase):
    def setUp(self):
        chat_tests.PluginAgentTests.setUp(self)
        self.stack.enter_context(mock.patch.object(self.plugin, 'AGENT_TASKS', True))
        self.event_data = {'id': 'synthetic-event', 'seq': 1, 'sender': 'owner', 'channel': self.event.unified_msg_origin,
                           'conversation': 'synthetic-original-session', 'project': 'p', 'task': 'synthetic-task',
                           'kind': 'terminal', 'body': {'state': 'verified', 'result': {'evidence': 'synthetic-only'}}}
        with self.main._agent_db() as db:
            db.execute('INSERT INTO agent_task_sources VALUES (?,?,0)', ('owner', self.event.unified_msg_origin))

    def tick(self):
        import asyncio
        asyncio.run(self.main._agent_task_tick())

    def test_background_event_claims_once_and_keeps_origin_label(self):
        self.main._control = mock.AsyncMock(return_value=(200, {'ok': True, 'events': [self.event_data]}))
        self.tick()
        self.tick()
        self.assertEqual(self.main._send_notification.await_count, 1)
        text = self.main._send_notification.await_args.args[1]
        self.assertIn('synthetic-task', text)
        self.assertIn('原会话 syntheti', text)

    def test_timeout_send_is_unknown_and_not_retried(self):
        self.main._control = mock.AsyncMock(return_value=(200, {'ok': True, 'events': [self.event_data]}))
        self.main._send_notification = mock.AsyncMock(side_effect=TimeoutError)
        self.tick()
        self.tick()
        self.assertEqual(self.main._send_notification.await_count, 1)
        with self.main._agent_db() as db:
            self.assertEqual(db.execute('SELECT state FROM agent_task_outbox').fetchone()[0], 'unknown')

    def test_foreign_event_never_advanced_or_sent(self):
        self.main._control = mock.AsyncMock(return_value=(200, {'ok': True, 'events': [{**self.event_data, 'sender': 'foreign'}]}))
        self.tick()
        self.main._send_notification.assert_not_awaited()
        with self.main._agent_db() as db:
            self.assertEqual(db.execute('SELECT cursor FROM agent_task_sources').fetchone()[0], 0)

    def test_event_record_and_cursor_transaction_roll_back_together(self):
        # A malformed second event aborts the complete batch, including its cursor.
        second = {**self.event_data, 'id': 'second', 'seq': 2}
        del second['kind']
        self.main._control = mock.AsyncMock(return_value=(200, {'ok': True, 'events': [self.event_data, second]}))
        with self.assertRaises(KeyError):
            self.tick()
        with self.main._agent_db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM agent_task_outbox').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT cursor FROM agent_task_sources').fetchone()[0], 0)
        self.main._control = mock.AsyncMock(return_value=(200, {'ok': True, 'events': [self.event_data]}))
        self.tick()
        self.assertEqual(self.main._send_notification.await_count, 1)

    def test_restart_sending_state_becomes_unknown_without_resending(self):
        with self.main._agent_db() as db:
            db.execute('INSERT INTO agent_task_outbox VALUES (?,?,?,?,?)', (self.event_data['id'], 'owner',
                       self.event.unified_msg_origin, json.dumps(self.event_data), 'sending'))
        restarted = self.plugin.Main(self.main.context)
        restarted._send_notification = mock.AsyncMock(return_value=True)
        restarted._control = mock.AsyncMock(return_value=(200, {'ok': True, 'events': [self.event_data]}))
        import asyncio
        async def restart():
            with mock.patch.object(self.plugin, 'NOTIFICATIONS_ENABLED', False):
                await restarted.initialize()
                try:
                    await restarted._agent_task_tick()
                finally:
                    await restarted.terminate()
        asyncio.run(restart())
        restarted._send_notification.assert_not_awaited()
        with restarted._agent_db() as db:
            self.assertEqual(db.execute('SELECT state FROM agent_task_outbox').fetchone()[0], 'unknown')

    def test_approval_displays_digest_actual_summary_and_bound_id(self):
        event = {**self.event_data, 'kind': 'approval', 'body': {'approval_id': 'synthetic-approval', 'digest': 'synthetic-digest',
                  'tool': 'create_text', 'arguments': {'path': 'new.txt', 'text': 'synthetic content', 'binding': 'private snapshot'}}}
        text = self.main._task_event_text(event)
        self.assertIn('/agent approve synthetic-approval', text)
        self.assertIn('/agent deny synthetic-approval', text)
        self.assertIn('/agent approve synthetic-approval', text.splitlines())
        self.assertIn('/agent deny synthetic-approval', text.splitlines())
        self.assertNotIn('同意：/agent', text)
        self.assertIn(hashlib.sha256(b'synthetic content').hexdigest(), text)
        self.assertNotIn('private snapshot', text)

    def test_denial_notifications_distinguish_expiry_user_denial_and_unknown(self):
        for reason, expected in [('user_denied', '用户主动拒绝'), ('approval_expired', '审批已过期'),
                                 ('approval_revoked', '审批已撤销'), ('approval_unavailable', '审批通道不可用'),
                                 ('approval_not_consumed', '授权未消费'),
                                 ('boundary_denied', '执行边界未放行'), (None, '工具未获授权')]:
            with self.subTest(reason=reason):
                result = {'completion': 'denied', 'evidence': 'tool_body_not_entered'}
                if reason:
                    result['reason'] = reason
                event = {**self.event_data, 'body': {'state': 'denied', 'result': result}}
                text = self.main._task_event_text(event)
                self.assertIn(expected, text)
                if reason != 'user_denied':
                    self.assertNotIn('用户主动拒绝', text)
                self.assertNotIn('自动拒绝', text)


class TaskNativeChatTests(unittest.TestCase):
    def test_denial_reasons_and_unknown_old_receipt_persist_once_in_original_session(self):
        node, packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE'), os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not node or not packages:
            self.skipTest('explicit native runtime required')
        with tempfile.TemporaryDirectory(prefix='abr-', dir='/private/tmp') as scratch:
            root = Path(scratch).resolve()
            workspace = root / 'work'
            workspace.mkdir()
            sid = uuid.uuid4().hex
            events = []
            for index, reason in enumerate(('user_denied', 'approval_expired', None), 1):
                result = {'completion': 'denied', 'evidence': 'tool_body_not_entered'}
                if reason:
                    result['reason'] = reason
                events.append(dict(id=f'synthetic-denial-{index}', seq=index, sender='owner', channel='private',
                                   conversation=sid, project='p', task=f'synthetic-task-{index}', kind='terminal',
                                   body={'state': 'denied', 'result': result}))
            for turn in range(2):
                mid = uuid.uuid4().hex
                subject = dict(sender='owner', channel='private', conversation=sid, session=sid,
                               project='p', cwd=str(workspace), attempt=mid)
                request = dict(id=mid, session=sid, root=str(root / 'a/native'), subject=subject, resume=turn > 0,
                               text='synthetic denial recall', synthetic=True, task_events=events)
                file = root / 'request.json'
                file.write_text(json.dumps(request))
                result = subprocess.run([node, str(Path(__file__).with_name('agent_driver.mjs')), str(file)],
                                        capture_output=True, text=True, timeout=15,
                                        env={'HOME': str(root), 'PATH': '/usr/bin:/bin', 'ELECTRON_RUN_AS_NODE': '1',
                                             'AGENTBRIDGE_AGENT_PACKAGES': packages})
                self.assertEqual(result.returncode, 0, result.stderr)
                evidence = json.loads(json.loads(result.stdout)['reply'])['evidence']
                self.assertEqual([json.loads(item[0]['text'].split('\n', 1)[1]) for item in evidence], events)
            file = next((root / 'a/native/sessions').glob('*/' + sid + '/session.jsonl'))
            records = [json.loads(line) for line in file.read_text().splitlines()]
            for event in events:
                self.assertEqual(sum(record.get('type') == 'user/message'
                                     and record['data']['id'] == 'bridge-task-event:' + event['id']
                                     for record in records), 1)

    def test_crash_after_native_event_flush_resumes_without_duplicate_evidence(self):
        node, packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE'), os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not node or not packages:
            self.skipTest('explicit native runtime required')
        with tempfile.TemporaryDirectory(prefix='abf-', dir='/private/tmp') as scratch:
            root = Path(scratch).resolve()
            workspace = root / 'work'
            workspace.mkdir()
            source = root / 'source'
            source.mkdir()
            for name in ('agent_driver.mjs', 'agent_persona.mjs', 'agent_rpc.mjs', 'native_boundary.mjs'):
                shutil.copyfile(Path(__file__).with_name(name), source / name)
            driver = source / 'agent_driver.mjs'
            original = driver.read_text()
            checkpoint = 'assert.equal(await ctx.sessions.flush(handle.agent.session), true);'
            self.assertEqual(original.count(checkpoint), 2)
            driver.write_text(original.replace(checkpoint, checkpoint + '\n  process.exit(77);', 1))
            sid = uuid.uuid4().hex
            event = dict(id='synthetic-crash-event', seq=1, sender='owner', channel='private', conversation=sid,
                         project='p', task='synthetic-task', kind='terminal', body={'state': 'verified'})
            for index in range(2):
                mid = uuid.uuid4().hex
                subject = dict(sender='owner', channel='private', conversation=sid, session=sid,
                               project='p', cwd=str(workspace), attempt=mid)
                request = dict(id=mid, session=sid, root=str(root / 'a/native'), subject=subject, resume=index > 0,
                               text='synthetic recall after crash', synthetic=True, task_events=[event])
                file = root / 'request.json'
                file.write_text(json.dumps(request))
                result = subprocess.run([node, str(driver), str(file)], capture_output=True, text=True, timeout=15,
                                        env={'HOME': str(root), 'PATH': '/usr/bin:/bin', 'ELECTRON_RUN_AS_NODE': '1',
                                             'AGENTBRIDGE_AGENT_PACKAGES': packages})
                self.assertEqual(result.returncode, 77 if index == 0 else 0, result.stderr)
                driver.write_text(original)
            reply = json.loads(json.loads(result.stdout)['reply'])
            self.assertEqual(len(reply['evidence']), 1)
            file = next((root / 'a/native/sessions').glob('*/' + sid + '/session.jsonl'))
            messages = [json.loads(line) for line in file.read_text().splitlines()]
            self.assertEqual(sum(record.get('type') == 'user/message' and record['data']['id'] == 'bridge-task-event:' + event['id']
                                 for record in messages), 1)

    def test_frozen_task_submission_returns_while_background_lane_waits(self):
        node, packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE'), os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not node or not packages:
            self.skipTest('explicit native runtime required')
        with contextlib.ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='abc-', dir='/private/tmp'))).resolve()
            workspace = root / 'work'
            workspace.mkdir()
            release = threading.Event()

            def runner(task, cancel):
                while not release.wait(.02):
                    if cancel.is_set():
                        raise AgentError('CANCELLED', 'synthetic stopped')
                return {'completion': 'verified'}

            tasks = TaskRuntime(root / 'a/t', runner, lambda _: str(workspace), enabled=True)
            stack.callback(tasks.close)
            mid, sid = uuid.uuid4().hex, uuid.uuid4().hex
            subject = dict(sender='synthetic-owner', channel='synthetic-private', conversation=sid, session=sid,
                           project='p', cwd=str(workspace), attempt=mid)
            broker = tasks.issue('chat', subject)
            request = {'id': mid, 'session': sid, 'root': str(root / 'a/native'), 'subject': subject, 'resume': False,
                       'text': 'synthetic submission', 'synthetic': True, 'broker': broker, 'python': sys.executable,
                       'syntheticTool': {'name': 'submit_task', 'arguments': {'operation': 'create_text',
                         'arguments': json.dumps({'path': 'new.txt', 'text': 'synthetic'})}}}
            file = root / 'request.json'
            file.write_text(json.dumps(request))
            result = subprocess.run([node, str(Path(__file__).with_name('agent_driver.mjs')), str(file)], capture_output=True,
                                    text=True, timeout=15, env={'HOME': str(root), 'PATH': '/usr/bin:/bin',
                                     'ELECTRON_RUN_AS_NODE': '1', 'AGENTBRIDGE_AGENT_PACKAGES': packages})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('task_id', result.stdout)
            self.assertFalse((workspace / 'new.txt').exists())
            self.assertEqual(tasks.inspect(subject['sender'], subject['channel'], sid, 'p')['tasks'][0]['state'], 'running')
            release.set()

    def test_result_event_persists_once_in_original_native_session(self):
        node, packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE'), os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not node or not packages:
            self.skipTest('explicit native runtime required')
        with tempfile.TemporaryDirectory(prefix='abe-', dir='/private/tmp') as scratch:
            root = Path(scratch).resolve()
            workspace = root / 'work'
            workspace.mkdir()
            sid = uuid.uuid4().hex
            event = dict(id='synthetic-result-event', seq=1, sender='synthetic-owner', channel='synthetic-private',
                         conversation=sid, project='p', task='synthetic-task', kind='terminal',
                         body={'state': 'verified', 'result': {'path': 'synthetic-location', 'evidence': 'synthetic-only'}})
            for index in range(4):
                mid = uuid.uuid4().hex
                subject = dict(sender='synthetic-owner', channel='synthetic-private', conversation=sid, session=sid,
                               project='p', cwd=str(workspace), attempt=mid)
                request = dict(id=mid, session=sid, root=str(root / 'a/native'), subject=subject, resume=index>0,
                               text='synthetic event recall', synthetic=True,
                               task_events=[event if index<2 else ({**event, 'channel': 'foreign'} if index == 2
                                            else {**event, 'body': {'state': 'verified', 'result': {'path': 'forged-location'}}})])
                file = root / (mid+'.json')
                file.write_text(json.dumps(request))
                result = subprocess.run([node, str(Path(__file__).with_name('agent_driver.mjs')), str(file)], capture_output=True,
                                        text=True, timeout=15, env={'HOME': str(root), 'PATH': '/usr/bin:/bin',
                                         'ELECTRON_RUN_AS_NODE': '1', 'AGENTBRIDGE_AGENT_PACKAGES': packages})
                self.assertEqual(result.returncode, 0 if index<2 else 1, result.stderr)
                if index < 2:
                    reply = json.loads(json.loads(result.stdout)['reply'])
                    self.assertEqual(len(reply['evidence']), 1)
                    self.assertIn('synthetic-location', reply['evidence'][0][0]['text'])
            file = next((root / 'a/native/sessions').glob('*/'+sid+'/session.jsonl'))
            records = [json.loads(line) for line in file.read_text().splitlines()]
            results = [record for record in records if record.get('type') == 'user/message'
                       and record['data']['source'].get('plugin') == 'agentbridge-task-events']
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['data']['id'], 'bridge-task-event:' + event['id'])
            self.assertEqual(json.loads(results[0]['data']['content'][0]['text'].split('\n', 1)[1]), event)


if __name__ == '__main__':
    unittest.main()
