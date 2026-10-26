"""Source-scoped controls, durable events and synthetic cancellation races."""
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

import worker
import test_s1_s5 as baseline
import test_bridge as bridge

Event, collect, plugin = baseline.Event, baseline.collect, baseline.plugin


class TestControls(unittest.TestCase):
    setUp = baseline.TestWorkerControl.setUp
    execute = baseline.TestWorkerControl.execute
    control = baseline.TestWorkerControl.control

    def test_short_id_scope_ambiguity_and_legacy_fail_closed(self):
        self.execute('12345678-one')
        self.execute('12345678-two')
        self.assertEqual(self.control('status', job_id='12345678-o')['job_id'], '12345678-one')
        with self.assertRaises(worker.AgentBridgeError) as error:
            self.control('status', job_id='12345678')
        self.assertEqual(error.exception.code, 'AMBIGUOUS_ID')
        self.app.db.execute("UPDATE jobs SET sender=NULL WHERE job_id='12345678-one'")
        with self.assertRaises(worker.AgentBridgeError):
            self.control('status', job_id='12345678-one')
        self.assertEqual([r['job_id'] for r in self.control('jobs')['recent']], ['12345678-two'])

    def test_batch_is_all_or_nothing_and_lists_have_no_body_or_token(self):
        self.execute('pending-job', 'rm -rf synthetic')
        pending = self.control('pending')['pending']
        self.assertGreater(pending[0]['remaining_seconds'], 0)
        self.assertNotIn('synthetic', json.dumps(pending))
        self.assertNotIn('token', json.dumps(pending))
        with self.assertRaises(worker.AgentBridgeError):
            self.control('status.batch', job_ids=['pending-job', 'missing-job'])
        with self.assertRaises(worker.AgentBridgeError):
            self.control('status.batch', job_ids=['pending-job'] * 51)
        self.assertEqual(len(self.control('status.batch', job_ids=['pending-job'])['jobs']), 1)

    def test_deny_confirm_race_revokes_token_and_executes_at_most_once(self):
        self.execute('pending-job', 'rm -rf synthetic')
        token = self.app.db.one('SELECT token FROM pending')['token']
        def act(op):
            try:
                return self.control(op, delivery_id=op, **({'token': token} if op == 'confirm' else {'job_id': 'pending-job'}))
            except worker.AgentBridgeError:
                return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(act, ['confirm', 'deny']))
        deadline = time.monotonic() + 3
        while self.app.get_job('pending-job')['status'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIn(self.app.get_job('pending-job')['status'], ('done', 'cancelled'))
        self.assertLessEqual(len(self.executions), 1)
        self.assertIsNone(self.app.db.one('SELECT * FROM pending'))

    def test_terminal_and_event_share_transaction_and_force_changes_attempt(self):
        self.execute('attempt-job')
        first = self.control('status', job_id='attempt-job')
        self.assertTrue(first['attempt_id'])
        self.assertEqual(first['events'][0]['attempt_id'], first['attempt_id'])
        self.app.db.execute("UPDATE jobs SET status='uncertain' WHERE job_id='attempt-job'")
        self.control('force', job_id='attempt-job', delivery_id='force-one')
        deadline = time.monotonic() + 3
        while self.app.get_job('attempt-job')['status'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(.01)
        second = self.control('status', job_id='attempt-job')
        self.assertEqual(second['status'], 'done')
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])
        self.assertGreaterEqual(len(second['events']), 2)
        # Replayed cancellation/force delivery cannot act on another attempt.
        self.assertEqual(self.control('force', job_id='attempt-job', delivery_id='force-one')['attempt_id'], second['attempt_id'])

    def test_terminal_event_rolls_back_with_state(self):
        self.execute('atomic-job')
        self.app.db.execute("DELETE FROM completion_events")
        self.app.db.execute("UPDATE jobs SET status='running',ended_at=NULL WHERE job_id='atomic-job'")
        with self.assertRaises(RuntimeError):
            with self.app.db.transaction() as conn:
                conn.execute("UPDATE jobs SET status='done',ended_at=? WHERE job_id='atomic-job'", (time.time(),))
                raise RuntimeError('synthetic crash before commit')
        self.assertEqual(self.app.get_job('atomic-job')['status'], 'running')
        self.assertEqual(self.app.db.query('SELECT * FROM completion_events'), [])
        self.app.db.execute("UPDATE jobs SET status='done',ended_at=? WHERE job_id='atomic-job'", (time.time(),))
        self.assertEqual(len(self.app.db.query('SELECT * FROM completion_events')), 1)

    def test_restart_restores_pre_start_cancel_and_old_receipt_cannot_cancel_force(self):
        self.execute('restart-job')
        self.app.db.execute("UPDATE jobs SET status='queued',exec_state='idle',cancel_requested_at=? WHERE job_id='restart-job'", (time.time(),))
        self.app._recover_stale_jobs()
        self.assertEqual(self.app.get_job('restart-job')['status'], 'cancelled')
        self.control('cancel', job_id='restart-job', delivery_id='old-cancel')
        self.app.db.execute("UPDATE jobs SET status='uncertain' WHERE job_id='restart-job'")
        self.control('force', job_id='restart-job', delivery_id='new-force')
        deadline = time.monotonic() + 3
        while self.app.get_job('restart-job')['status'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(.01)
        self.control('cancel', job_id='restart-job', delivery_id='old-cancel')
        final = self.app.get_job('restart-job')
        self.assertEqual(final['status'], 'done')
        self.assertIsNone(final['cancel_requested_at'])


class TestAsyncBridge(unittest.TestCase):
    setUp = bridge.TestBridge.setUp
    start_server = bridge.TestBridge.start_server
    wait_idle = bridge.TestBridge.wait_idle
    post = bridge.TestBridge.post
    control = bridge.TestBridge.control

    def wait_state(self, job_id, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = self.app.db.one('SELECT * FROM jobs WHERE job_id=?', (job_id,))
            if row is not None and predicate(row):
                return row
            time.sleep(.01)
        self.fail('Synthetic job did not reach the required state')

    def test_async_ack_does_not_wait_and_default_still_waits(self):
        release = threading.Event()
        self.addCleanup(release.set)
        original = worker.run_task
        def run(*args, **kwargs):
            release.wait(3)
            return original(*args, **kwargs)
        with mock.patch.object(worker, 'run_task', side_effect=run):
            result = self.post('inspect', 'async-job', Prefer='respond-async')
            self.assertIn(result['x_agentbridge']['status'], ('queued', 'running'))
            self.assertFalse(release.is_set())
            release.set()
            self.wait_idle()
            self.assertEqual(self.app.get_job('async-job')['status'], 'done')
        self.assertEqual(self.post('inspect', 'sync-job')['x_agentbridge']['status'], 'done')

    def test_running_cancel_uses_owner_supervisor(self):
        backend = self.root / 'backend.sh'
        backend.write_text('#!/bin/sh\nsleep 60\n')
        with mock.patch.object(worker, 'RUNNING_CANCEL_ENABLED', True, create=True):
            self.post('inspect', 'cancel-job', Prefer='respond-async')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                row = self.app.db.one("SELECT * FROM jobs WHERE job_id='cancel-job'")
                if row and row['exec_state'] == 'released':
                    break
                time.sleep(.02)
            self.assertEqual(row['exec_state'], 'released')
            status, result = self.control({'op': 'cancel', 'job_id': 'cancel-job', 'delivery_id': 'cancel-one'})
            self.assertEqual(status, 200)
            self.assertEqual(result['code'], 'CANCEL_REQUESTED')
            self.wait_idle()
            final = self.control({'op': 'status', 'job_id': 'cancel-job'})[1]
            self.assertEqual(final['status'], 'cancelled')
            self.assertFalse(worker._group_alive(row['exec_pgid']))

    def test_queued_cancel_releases_reservation_and_never_starts_backend(self):
        entered, release = threading.Event(), threading.Event()
        original = worker.run_task
        calls = []
        def run(*args, **kwargs):
            calls.append(1)
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        try:
            with mock.patch.object(worker, 'run_task', side_effect=run):
                self.post('inspect', 'queue-first', Prefer='respond-async')
                self.assertTrue(entered.wait(2))
                self.post('inspect', 'queue-second', Prefer='respond-async')
                status, result = self.control({'op': 'cancel', 'job_id': 'queue-second', 'delivery_id': 'queue-cancel'})
                self.assertEqual(status, 200)
                self.assertEqual(result['status'], 'cancelled')
                release.set()
                self.wait_idle()
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.app.sched.snapshot()['waiting'], 0)
        finally:
            release.set()
            self.wait_idle()

    def test_cancel_wins_registration_or_release_gate_without_side_effects(self):
        marker = self.root / 'must-not-exist'
        (self.root / 'backend.sh').write_text(f'#!/bin/sh\ntouch {marker}\necho ran\n')
        original = worker.run_task
        for stage in ('on_start', 'on_release'):
            with self.subTest(stage=stage):
                paused, release = threading.Event(), threading.Event()
                def run(*args, **kwargs):
                    callback = kwargs[stage]
                    def hold(value):
                        paused.set()
                        release.wait(5)
                        return callback(value)
                    kwargs[stage] = hold
                    return original(*args, **kwargs)
                try:
                    with mock.patch.object(worker, 'RUNNING_CANCEL_ENABLED', True), \
                            mock.patch.object(worker, 'run_task', side_effect=run):
                        self.post('inspect', stage, Prefer='respond-async')
                        self.assertTrue(paused.wait(2))
                        self.assertEqual(self.control({'op': 'cancel', 'job_id': stage, 'delivery_id': stage + '-cancel'})[1]['code'], 'CANCEL_REQUESTED')
                        release.set()
                        self.wait_idle()
                        self.assertEqual(self.app.get_job(stage)['status'], 'cancelled')
                        self.assertFalse(marker.exists())
                finally:
                    release.set()
                    self.wait_idle()

    def test_cancel_intent_before_terminal_commit_wins_and_late_finalize_cannot_override(self):
        paused, release = threading.Event(), threading.Event()
        original = worker._finalize
        captured = []
        def finish(*args):
            captured.append(args)
            paused.set()
            release.wait(5)
            return original(*args)
        try:
            with mock.patch.object(worker, 'RUNNING_CANCEL_ENABLED', True), \
                    mock.patch.object(worker, '_finalize', side_effect=finish):
                self.post('inspect', 'finish-race', Prefer='respond-async')
                self.assertTrue(paused.wait(3))
                self.control({'op': 'cancel', 'job_id': 'finish-race', 'delivery_id': 'finish-cancel'})
                release.set()
                self.wait_idle()
            self.assertEqual(original(*captured[0])['status'], 'cancelled')
            self.assertEqual(self.app.get_job('finish-race')['status'], 'cancelled')
            self.assertEqual(len(self.app.db.query("SELECT * FROM completion_events WHERE job_id='finish-race'")), 1)
        finally:
            release.set()
            self.wait_idle()

    def test_unverifiable_group_is_uncertain_and_quarantined_not_cancelled(self):
        (self.root / 'backend.sh').write_text('#!/bin/sh\nsleep 60\n')
        row = None
        try:
            with mock.patch.object(worker, 'RUNNING_CANCEL_ENABLED', True), \
                    mock.patch.object(worker, '_stop_owned_group', return_value=False):
                self.post('inspect', 'uncertain-cancel', Prefer='respond-async')
                row = self.wait_state('uncertain-cancel', lambda r: r['exec_state'] == 'released')
                self.control({'op': 'cancel', 'job_id': 'uncertain-cancel', 'delivery_id': 'uncertain-intent'})
                self.wait_idle()
                self.assertEqual(self.app.get_job('uncertain-cancel')['status'], 'uncertain')
                self.assertIsNotNone(self.app.db.one('SELECT * FROM quarantine'))
        finally:
            # Only the synthetic executor created by this test is cleanup authority.
            if row is not None:
                os.killpg(row['exec_pgid'], 9)
            self.wait_idle()

    def test_identity_change_or_permission_error_never_authorizes_kill(self):
        with mock.patch.object(worker, '_group_alive', return_value=True), \
                mock.patch.object(worker, '_group_members', return_value={42: 'new-birth'}), \
                mock.patch.object(worker.os, 'killpg') as signal_group:
            self.assertFalse(worker._stop_owned_group(42, {42: 'old-birth'}))
            signal_group.assert_not_called()
        with mock.patch.object(worker, '_group_alive', return_value=True), \
                mock.patch.object(worker, '_group_members', return_value={42: 'same-birth'}), \
                mock.patch.object(worker.os, 'killpg', side_effect=PermissionError):
            self.assertFalse(worker._stop_owned_group(42, {42: 'same-birth'}))

    def test_parent_exit_does_not_hide_owned_descendant_from_cancel(self):
        marker = self.root / 'descendant.pid'
        child = (f'import os,pathlib,signal,time; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); '
                 'signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)')
        parent = ('import subprocess,sys,time; '
                  f'subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(.3)')
        start = time.monotonic()
        seen = []
        try:
            result = worker.run_task([sys.executable, '-c', parent], str(self.root), 10,
                                     on_start=seen.append, cancel_check=lambda: time.monotonic() - start > .6)
            self.assertTrue(marker.exists())
            self.assertTrue(result['cancelled'])
            self.assertTrue(result['group_stopped'])
            self.assertFalse(worker._group_alive(seen[0]))
        finally:
            if seen and worker._group_alive(seen[0]):
                os.killpg(seen[0], 9)


class TestPluginCommands(unittest.TestCase):
    setUp = baseline.TestPluginDelivery.setUp

    def test_help_and_queries_never_create_a_task(self):
        with mock.patch.object(self.main, '_control', new=mock.AsyncMock(return_value=(200, {'ok': True}))) as ctl, \
                mock.patch.object(self.main, '_call', new=mock.AsyncMock()) as execute:
            asyncio.run(collect(self.main.mac(Event(), 'help')))
            ctl.assert_not_called()
            for command in ('diag', 'jobs', 'pending', 'deny 12345678', 'cancel 12345678'):
                asyncio.run(collect(self.main.mac(Event(), command)))
                self.assertEqual(ctl.call_args.args[1]['op'], command.split()[0])
            execute.assert_not_called()


class TestNotifications(unittest.TestCase):
    setUp = baseline.TestPluginDelivery.setUp

    def notification(self):
        return {'event_id': 'event-one', 'job_id': 'job-one', 'attempt_id': 'attempt-one',
                'seq': 1,
                'sender': 'owner', 'channel': Event.unified_msg_origin, 'status': 'done',
                'created_at': time.time(), 'elapsed_seconds': 1.2}

    def test_subscription_is_private_and_persistence_failure_prevents_dispatch(self):
        with mock.patch.object(plugin, 'NOTIFICATIONS_ENABLED', True, create=True), \
                mock.patch.object(self.main, '_control', new=mock.AsyncMock(return_value=(200, {'ok': True, 'event_seq': 0}))), \
                mock.patch.object(self.main, '_subscribe', side_effect=OSError('disk full')), \
                mock.patch.object(self.main, '_request', new=mock.AsyncMock()) as send:
            with self.assertRaises(OSError):
                asyncio.run(self.main._call(Event(), 'inspect', job_id='job-one'))
            send.assert_not_called()

    def test_api_timeout_is_unknown_and_never_automatically_resent(self):
        event = self.notification()
        with mock.patch.object(self.main, '_notification_allowed', return_value=True), \
                mock.patch.object(self.main, '_send_notification', new=mock.AsyncMock(side_effect=TimeoutError)) as send:
            asyncio.run(self.main._deliver_event(event, 'safe result'))
            asyncio.run(self.main._deliver_event(event, 'safe result'))
            self.assertEqual(send.call_count, 1)
        with self.main._notification_db() as db:
            self.assertEqual(db.execute('SELECT state FROM notifications').fetchone()['state'], 'unknown')

    def test_crash_claim_recovered_unknown_and_revoked_allowlist_blocks_send(self):
        event = self.notification()
        with self.main._notification_db() as db:
            self.main._record_event(db, event)
            db.execute("UPDATE notifications SET state='sending'")
        self.main._recover_notifications()
        with self.main._notification_db() as db:
            self.assertEqual(db.execute('SELECT state FROM notifications').fetchone()['state'], 'unknown')
        event['event_id'] = 'event-two'
        with mock.patch.object(self.main, '_notification_allowed', return_value=False), \
                mock.patch.object(self.main, '_send_notification', new=mock.AsyncMock()) as send:
            asyncio.run(self.main._deliver_event(event, 'safe result'))
            send.assert_not_called()

    def test_api_return_dedup_and_excerpt_redacts_secrets_commands_and_paths(self):
        event = self.notification()
        with mock.patch.object(self.main, '_notification_allowed', return_value=True), \
                mock.patch.object(self.main, '_send_notification', new=mock.AsyncMock(return_value=True)) as send:
            result = 'token=synthetic-private-token\n/Users/owner/private\n```sh\nrm -rf /tmp/test\n```\n' + 'safe ' * 300
            asyncio.run(self.main._deliver_event(event, result))
            asyncio.run(self.main._deliver_event(event, result))
            self.assertEqual(send.call_count, 1)
            text = send.call_args.args[1]
            self.assertNotIn('synthetic-private-token', text)
            self.assertNotIn('/Users/owner', text)
            self.assertNotIn('rm -rf', text)
            self.assertIn('event-one', text)
        self.assertLessEqual(len(self.main._excerpt(result)), 300)

    def test_reload_has_one_sender_and_restores_subscriptions(self):
        async def reload():
            second = plugin.Main(None)
            with mock.patch.object(plugin, 'NOTIFICATIONS_ENABLED', True), \
                    mock.patch.object(self.main, '_notification_tick', new=mock.AsyncMock()), \
                    mock.patch.object(second, '_notification_tick', new=mock.AsyncMock()):
                self.main._subscribe(Event(), 'surviving-job')
                await self.main.initialize()
                original = self.main._sender_task
                await self.main.initialize()
                self.assertIs(original, self.main._sender_task)
                with self.assertRaises(BlockingIOError):
                    await second.initialize()
                await self.main.terminate()
                self.assertTrue(original.done())
                await second.initialize()
                await second.terminate()
                with second._notification_db() as db:
                    self.assertEqual(db.execute('SELECT job_id FROM subscriptions').fetchone()['job_id'], 'surviving-job')
        asyncio.run(reload())

    def test_batch_source_check_force_events_and_alias_subscriptions_deduplicate(self):
        self.main._subscribe(Event(), 'job-one')
        self.main._subscribe(Event(), 'confirmation-alias')
        event = self.notification()
        def payload():
            return {'ok': True, 'jobs': [{'job_id': 'job-one', 'requested_id': value, 'status': 'done',
                    'attempt_id': event['attempt_id'], 'message': 'safe result', 'events': [dict(event)]}
                    for value in ('job-one', 'confirmation-alias')]}
        async def control(*args):
            return 200, payload()
        with mock.patch.object(self.main, '_notification_allowed', return_value=True), \
                mock.patch.object(self.main, '_control', side_effect=control), \
                mock.patch.object(self.main, '_send_notification', new=mock.AsyncMock(return_value=True)) as send:
            event['sender'] = 'intruder'
            asyncio.run(self.main._notification_tick())
            send.assert_not_called()
            event['sender'] = 'owner'
            asyncio.run(self.main._notification_tick())
            self.assertEqual(send.call_count, 1)
            event.update(event_id='event-two', attempt_id='attempt-two', created_at=time.time(), seq=2)
            asyncio.run(self.main._notification_tick())
            self.assertEqual(send.call_count, 2)
        self.assertEqual((self.root / 'notifications.db').stat().st_mode & 0o777, 0o600)

    def test_event_sequence_not_cross_machine_clock_controls_history(self):
        self.main._subscribe(Event(), 'job-one', after_seq=10)
        event = self.notification()
        event.update(seq=10, created_at=time.time() + 3600)
        async def control(*args):
            return 200, {'ok': True, 'jobs': [{'requested_id': 'job-one', 'job_id': 'job-one',
                'status': 'done', 'attempt_id': 'attempt-one', 'events': [dict(event)], 'message': 'safe'}]}
        with mock.patch.object(self.main, '_notification_allowed', return_value=True), \
                mock.patch.object(self.main, '_control', side_effect=control), \
                mock.patch.object(self.main, '_send_notification', new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(self.main._notification_tick())
            send.assert_not_called()
            event.update(seq=11, created_at=time.time() - 3600)
            asyncio.run(self.main._notification_tick())
            self.assertEqual(send.call_count, 1)

    def test_pre_api_retry_budget_and_expiration(self):
        event = self.notification()
        with mock.patch.object(self.main, '_notification_allowed', return_value=True), \
                mock.patch.object(self.main, '_excerpt', side_effect=ValueError), \
                mock.patch.object(self.main, '_send_notification', new=mock.AsyncMock()) as send:
            for index, delay in enumerate((5, 15, 60, 300), 1):
                before = time.time()
                asyncio.run(self.main._deliver_event(event, 'safe'))
                with self.main._notification_db() as db:
                    row = db.execute('SELECT * FROM notifications').fetchone()
                    self.assertEqual(row['state'], 'retry')
                    self.assertEqual(row['tries'], index)
                    self.assertGreaterEqual(row['next_at'], before + delay)
                    db.execute('UPDATE notifications SET next_at=0')
            with self.main._notification_db() as db:
                db.execute('UPDATE notifications SET first_seen_at=?', (time.time() - 86401,))
            asyncio.run(self.main._deliver_event(event, 'safe'))
            send.assert_not_called()
        with self.main._notification_db() as db:
            self.assertEqual(db.execute('SELECT state FROM notifications').fetchone()['state'], 'expired')


if __name__ == '__main__':
    unittest.main()
