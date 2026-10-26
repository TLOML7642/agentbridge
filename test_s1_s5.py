"""Positive control-plane and delivery regressions. Synthetic backends only."""
import asyncio
import concurrent.futures
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

import worker


def load_plugin():
    names = ('astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.core',
             'astrbot.core.star', 'astrbot.core.star.filter',
             'astrbot.core.star.filter.command', 'aiohttp')
    modules = {name: types.ModuleType(name) for name in names}
    modules['astrbot.api'].star = types.SimpleNamespace(Star=object, Context=object)
    modules['astrbot.api.event'].AstrMessageEvent = object
    modules['astrbot.api.event'].filter = types.SimpleNamespace(
        command=lambda *a, **k: lambda f: f, regex=lambda *a, **k: lambda f: f)
    modules['astrbot.core.star.filter.command'].GreedyStr = str
    spec = importlib.util.spec_from_file_location(
        'plugin_under_test', Path(__file__).parent / 'server/plugins/agentbridge_mac/main.py')
    plugin = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, modules):
        spec.loader.exec_module(plugin)
    return plugin


plugin = load_plugin()


class Event:
    unified_msg_origin = 'instance:FriendMessage:owner'

    def __init__(self, mid='delivery-1'):
        self.message_obj = types.SimpleNamespace(
            message_id=mid or 'adapter-random', raw_message={'message_id': mid}, self_id='bot-1')

    def get_sender_id(self):
        return 'owner'

    def plain_result(self, text):
        return text


async def collect(gen):
    return [value async for value in gen]


class TestPluginDelivery(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.object(plugin, 'DATA_DIR', self.root, create=True))
        self.main = plugin.Main(None)

    def test_stable_delivery_and_instance_namespace(self):
        first, reused = self.main._delivery(Event(), 'task', 'p')
        self.assertEqual(first, self.main._delivery(Event(), 'task', 'other')[0])
        other = Event()
        other.unified_msg_origin = 'other:FriendMessage:owner'
        self.assertNotEqual(first, self.main._delivery(other, 'task', 'p')[0])

    def test_fallback_persists_project_bound_and_sixty_second_boundary(self):
        with mock.patch.object(plugin.time, 'time', return_value=100):
            first, reused = self.main._delivery(Event(None), 'full task', 'p')
            self.assertFalse(reused)
        with mock.patch.object(plugin.time, 'time', return_value=159.999):
            self.assertEqual((first, True), plugin.Main(None)._delivery(Event(None), 'full task', 'p'))
            self.assertNotEqual(first, self.main._delivery(Event(None), 'full task', 'q')[0])
        with mock.patch.object(plugin.time, 'time', return_value=160):
            self.assertNotEqual(first, self.main._delivery(Event(None), 'full task', 'p')[0])
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        db = self.root / 'deliveries.db'
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b'full task', db.read_bytes())

    def test_explicit_new_number_is_idempotent(self):
        first = self.main._delivery(Event('a'), 'task', 'p', '101')[0]
        self.assertEqual(first, self.main._delivery(Event('b'), 'task', 'p', '101')[0])
        self.assertNotEqual(first, self.main._delivery(Event('b'), 'task', 'p', '102')[0])

    def test_fallback_concurrent_retries_persist_exactly_one_id(self):
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            ids = list(pool.map(lambda _: self.main._delivery(Event(None), 'inspect', 'p')[0], range(24)))
        self.assertEqual(len(set(ids)), 1)

    def test_persistence_failure_never_dispatches(self):
        async def control(*args):
            return 200, {'ok': True, 'current': 'p'}
        with mock.patch.object(self.main, '_control', side_effect=control), \
                mock.patch.object(plugin.sqlite3, 'connect', side_effect=OSError('synthetic-disk-full')), \
                mock.patch.object(self.main, '_call', new=mock.AsyncMock()) as execute:
            reply = asyncio.run(collect(self.main.mac(Event(None), 'inspect')))
            execute.assert_not_called()
            self.assertIn('未下发执行', reply[0])

    def test_plugin_restart_and_lost_response_keep_delivery_id(self):
        ids = []
        async def control(*args):
            return 200, {'ok': True, 'current': 'p'}
        async def execute(event, task, **kwargs):
            ids.append(kwargs['job_id'])
            raise OSError('synthetic-response-loss')
        for _ in range(2):
            main = plugin.Main(None)
            with mock.patch.object(main, '_control', side_effect=control), \
                    mock.patch.object(main, '_call', side_effect=execute):
                asyncio.run(collect(main.mac(Event(None), 'inspect')))
        self.assertEqual(ids[0], ids[1])

    def test_control_commands_never_execute_model(self):
        async def control(event, payload):
            return 200, {'ok': True, 'current': 'p', 'message': 'ok'}
        with mock.patch.object(self.main, '_control', side_effect=control) as ctl, \
                mock.patch.object(self.main, '_call', new=mock.AsyncMock()) as execute:
            for command in ('status', 'status job', 'force job', 'confirm token123',
                            'project ls', 'project add p /tmp', 'project rm p', 'use p',
                            'force', 'project nonsense'):
                asyncio.run(collect(self.main.mac(Event(), command)))
            execute.assert_not_called()
            self.assertGreaterEqual(ctl.call_count, 8)

    def test_confirmation_survives_reload_and_failures(self):
        for code in ('QUEUE_FULL', 'AUTH_FAIL', 'ACCEPT_UNKNOWN', 'ACCEPTED'):
            with mock.patch.object(self.main, '_control', new=mock.AsyncMock(
                    return_value=(200, {'code': code, 'message': code}))) as call:
                asyncio.run(self.main._confirm(Event(), 'token123'))
                self.assertEqual(call.call_args.args[1]['token'], 'token123')
                self.assertTrue(call.call_args.args[1]['delivery_id'])
            self.main = plugin.Main(None)


class TestWorkerControl(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key, value in (('HOME', str(self.root)), ('DB_PATH', str(self.root / 'jobs.db')),
                           ('AUDIT_DIR', str(self.root / 'audit')),
                           ('DSH_SETTINGS_PATH', str(self.root / 'settings.yaml')),
                           ('ALLOWED_SENDERS', {'owner'}), ('QUEUE_MAX', 8)):
            self.stack.enter_context(mock.patch.object(worker, key, value))
        self.app = worker.App(verify=False)
        self.stack.callback(self.app.db.close)
        self.executions = []

        def result(*args, **kwargs):
            self.executions.append(args)
            return {'stdout': 'synthetic-ok', 'stderr': '', 'exit_code': 0,
                    'timed_out': False, 'peak_rss_mb': 1}
        self.stack.enter_context(mock.patch.object(worker, 'run_task', side_effect=result))

    def execute(self, jid='job', task='inspect', **extra):
        return worker.do_execute(self.app, dict(task=task, sender='owner', channel='chat',
                                               job_id=jid, **extra))

    def control(self, op, **extra):
        return worker.do_control(self.app, dict(op=op, sender='owner', channel='chat', **extra))

    def wait_idle(self):
        import time
        deadline = time.monotonic() + 3
        while self.app.sched.snapshot()['running'] and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_status_does_not_add_jobs_and_denies_cross_source_alias(self):
        self.execute()
        count = len(self.app.db.query('SELECT * FROM jobs'))
        self.assertEqual(self.control('status', job_id='job')['status'], 'done')
        self.assertEqual(self.control('status')['recent'][0]['job_id'], 'job')
        self.assertEqual(len(self.app.db.query('SELECT * FROM jobs')), count)
        self.app.db.execute('INSERT INTO aliases VALUES (?,?,?)', ('alias', 'job', 1))
        for extra in ({'sender': ''}, {'sender': 'intruder'}, {'channel': ''}, {'channel': 'other'}):
            with self.assertRaises(worker.AgentBridgeError):
                worker.do_control(self.app, dict(op='status', job_id='alias', sender='owner',
                                                 channel='chat') | extra)

    def test_multiple_pending_wrong_token_queue_full_and_reload(self):
        for name in ('a', 'b'):
            path = self.root / name
            path.mkdir()
            self.app.add_project(name, str(path))
            self.execute(name, 'rm -rf synthetic', project=name)
        self.execute('ordinary', 'inspect', project='default')
        tokens = {r['job_id']: r['token'] for r in self.app.db.query('SELECT * FROM pending')}
        with self.assertRaises(worker.AgentBridgeError):
            self.control('confirm', token='wrong-token', delivery_id='wrong')
        self.assertEqual(len(self.app.db.query('SELECT * FROM pending')), 2)
        with mock.patch.object(self.app.sched, 'reserve', side_effect=worker.AgentBridgeError('QUEUE_FULL', 'full')):
            with self.assertRaises(worker.AgentBridgeError):
                self.control('confirm', token=tokens['a'], delivery_id='ca')
        for name in ('a', 'b'):
            self.control('confirm', token=tokens[name], delivery_id='c' + name)
        self.wait_idle()
        for name in ('a', 'b'):
            self.assertEqual(self.control('status', job_id='c' + name)['status'], 'done')
            self.control('confirm', token=tokens[name], delivery_id='c' + name)
        self.assertEqual(len(self.executions), 3)

    def test_force_replay_after_second_interruption_never_reauthorizes(self):
        self.execute()
        self.app.db.execute("UPDATE jobs SET status='uncertain' WHERE job_id='job'")
        self.control('force', job_id='job', delivery_id='force-message')
        self.wait_idle()
        self.app.db.execute("UPDATE jobs SET status='uncertain' WHERE job_id='job'")
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: self.control('force', job_id='job', delivery_id='force-message'), range(12)))
        self.assertEqual(len(self.executions), 2)
        self.assertEqual(self.control('status', job_id='job')['status'], 'uncertain')

    def test_concurrent_confirmation_executes_once_and_persists_alias(self):
        self.execute('sensitive', 'rm -rf synthetic')
        token = self.app.db.one('SELECT token FROM pending')['token']
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda _: self.control('confirm', token=token, delivery_id='same-confirm'), range(24)))
        self.wait_idle()
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.control('status', job_id='same-confirm')['status'], 'done')

    def test_project_operations_replay_does_not_undo_newer_switch(self):
        for name in ('a', 'b'):
            path = self.root / name
            path.mkdir()
            self.control('project.add', name=name, dir=str(path), delivery_id='add-' + name)
        self.control('use', project='a', delivery_id='use-a')
        self.control('use', project='b', delivery_id='use-b')
        self.control('use', project='a', delivery_id='use-a')
        self.assertEqual(self.control('status')['current'], 'b')
        self.control('project.rm', name='a', delivery_id='rm-a')
        self.assertTrue((self.root / 'a').is_dir())

    def test_mutations_and_confirmation_reject_missing_identity(self):
        for op in ('status', 'confirm', 'force', 'project.add', 'project.rm', 'project.list', 'use'):
            for field in ('sender', 'channel'):
                with self.assertRaises(worker.AgentBridgeError):
                    worker.do_control(self.app, dict(op=op, sender='owner', channel='chat',
                                                     delivery_id='d', token='x', job_id='j') | {field: ''})
        with self.assertRaises(worker.AgentBridgeError):
            self.control('arbitrary', url='http://example.invalid')

    def test_crash_after_authorization_before_response_receipt_is_fail_closed(self):
        self.execute()
        self.app.db.execute("UPDATE jobs SET status='uncertain' WHERE job_id='job'")
        original = self.app.db.execute

        def fail_receipt(sql, args=()):
            if sql.startswith('UPDATE control_receipts'):
                raise RuntimeError('synthetic-crash-before-response')
            return original(sql, args)

        with mock.patch.object(self.app.db, 'execute', side_effect=fail_receipt):
            with self.assertRaises(RuntimeError):
                self.control('force', job_id='job', delivery_id='crash-force')
        self.wait_idle()
        self.app.db.execute("UPDATE jobs SET status='uncertain' WHERE job_id='job'")
        self.app.db.close()
        self.app = worker.App(verify=False)
        self.stack.callback(self.app.db.close)
        self.assertTrue(self.control('force', job_id='job', delivery_id='crash-force')['reused'])
        self.assertEqual(len(self.executions), 2)

    def test_pending_and_confirmation_alias_survive_worker_reopen(self):
        self.execute('sensitive', 'rm -rf synthetic')
        token = self.app.db.one('SELECT token FROM pending')['token']
        self.app.db.close()
        self.app = worker.App(verify=False)
        self.stack.callback(self.app.db.close)
        self.control('confirm', token=token, delivery_id='reopen-confirm')
        self.wait_idle()
        self.app.db.close()
        self.app = worker.App(verify=False)
        self.stack.callback(self.app.db.close)
        self.assertTrue(self.control('confirm', token=token, delivery_id='reopen-confirm')['reused'])
        self.assertEqual(len(self.executions), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
