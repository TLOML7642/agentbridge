"""Synthetic clocks and local databases only; no real reminders or recipients."""
import asyncio
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock

from server.plugins.agentbridge_mac.reminders import Reminders, ReminderError, absolute_time
import test_agent_runtime as chat_tests


class ReminderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='reminder-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1800000000.
        self.store = Reminders(self.root, lambda: self.now)
        self.addCleanup(self.store.close)

    def call(self, tool, args=None, request=None, sender='owner', channel='private'):
        return self.store.request(sender, channel, request or uuid.uuid4().hex, 'reminder_' + tool, args or {})

    def draft(self, delta=60, **args):
        data = dict(due_at=datetime.fromtimestamp(self.now + delta, timezone.utc).isoformat(),
                    timezone='UTC', content='synthetic reminder') | args
        return self.call('draft', data)['reminder']

    def confirm(self, row, **kwargs):
        return self.call('confirm', {'reminder_id': row['id']}, **kwargs)

    def get(self, row, **kwargs):
        return self.call('get', {'reminder_id': row['id']}, **kwargs)

    def setting(self, delta=60, **args):
        return self.call('set', dict(due_at=datetime.fromtimestamp(self.now + delta, timezone.utc).isoformat(),
                                     timezone='UTC', content='synthetic reminder') | args)

    def test_immediate_creation_and_restart_dispatch_without_confirmation(self):
        row = self.setting()['reminder']
        self.assertEqual(row['state'], 'active')
        self.assertIsNone(row['expires'])
        self.store.close()
        self.store = Reminders(self.root, lambda: self.now)
        self.addCleanup(self.store.close)
        self.store.start_sender()
        self.now += 60
        self.assertEqual(self.store.claim_due()['id'], row['id'])
        self.assertIsNone(self.store.claim_due())

    def test_immediate_modify_preserves_id_and_invalidates_old_draft(self):
        row = self.setting()['reminder']
        old = self.draft(120, reminder_id=row['id'])
        changed = self.setting(180, reminder_id=row['id'], content='changed')['reminder']
        self.assertEqual(changed['id'], row['id'])
        self.assertEqual(changed['due'], self.now + 180)
        self.assertEqual(changed['content'], 'changed')
        self.assertFalse(self.confirm(old)['ok'])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reminders').fetchone()[0], 2)

    def test_immediate_quota_validation_and_modify_at_capacity(self):
        rows = [self.setting()['reminder'] for _ in range(20)]
        self.assertFalse(self.setting()['ok'])
        self.assertTrue(self.setting(120, reminder_id=rows[0]['id'])['ok'])
        for args in ({'content': ''}, {'content': 'x' * 1001}, {'due_at': 'tomorrow'},
                     {'due_at': '2020-01-01T10:00'}, {'sender': 'other'}, {'channel': 'other'},
                     {'reminder_id': ''}):
            self.assertFalse(self.setting(**args)['ok'])

    def test_immediate_replay_after_cancel_is_receipt_not_reactivation(self):
        args = {'due_at': '2028-01-01T10:00', 'content': 'synthetic'}
        one = self.call('set', args, request='stable-set')
        row = one['reminder']
        self.call('cancel', {'reminder_id': row['id']})
        self.assertEqual(self.call('set', args, request='stable-set'), one)
        self.assertEqual(self.get(row)['reminder']['state'], 'cancelled')
        self.store.start_sender()
        self.now = row['due']
        self.assertIsNone(self.store.claim_due())

    def test_immediate_modify_is_source_scoped_and_cannot_change_sending(self):
        row = self.setting()['reminder']
        args = {'due_at': '2028-01-01T10:00', 'content': 'changed', 'reminder_id': row['id']}
        self.assertFalse(self.call('set', args, sender='other')['ok'])
        self.assertFalse(self.call('set', args, channel='other')['ok'])
        self.store.start_sender()
        self.now += 60
        self.store.claim_due()
        self.assertFalse(self.call('set', args)['ok'])
        self.store.finish(row['id'], 'unknown')
        self.assertFalse(self.call('set', args)['ok'])

    def test_immediate_concurrent_replay_and_modify_cancel_race(self):
        args = {'due_at': '2028-01-01T10:00', 'content': 'synthetic'}
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.call('set', args, request='one'), range(16)))
        self.assertTrue(all(result == results[0] for result in results))
        row = results[0]['reminder']
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.setting, 120, reminder_id=row['id']),
                       pool.submit(self.call, 'cancel', {'reminder_id': row['id']})]
            for future in futures:
                future.result()
        self.assertEqual(self.get(row)['reminder']['state'], 'cancelled')

    def test_draft_is_not_active_and_explicit_confirmation_is_required(self):
        row = self.draft()
        self.store.start_sender()
        self.now += 60
        self.assertIsNone(self.store.claim_due())
        self.assertEqual(self.get(row)['reminder']['state'], 'draft')
        self.assertFalse(self.confirm(row)['ok'])

    def test_default_timezone_and_absolute_date(self):
        due = datetime.fromtimestamp(self.now + 1000, timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai'))
        result = self.call('draft', {'due_at': due.replace(tzinfo=None).isoformat(), 'content': 'synthetic'})
        self.assertEqual(result['reminder']['timezone'], 'Asia/Shanghai')
        self.assertEqual(result['reminder']['due'], self.now + 1000)

    def test_ambiguous_past_invalid_and_mismatched_times_rejected(self):
        for value, zone in [('tomorrow', 'UTC'), ('2027-01-15', 'UTC'), ('2027-02-30T10:00', 'UTC'),
                            ('2020-01-01T10:00', 'UTC'), ('2028-01-01T10:00+00:00', 'Asia/Shanghai'),
                            ('2028-01-01T10:00', 'not/a-zone'), ('2028-11-05T01:30', 'America/New_York'),
                            ('2028-03-12T02:30', 'America/New_York')]:
            with self.subTest(value=value, zone=zone), self.assertRaises(ReminderError):
                absolute_time(value, zone, self.now)

    def test_explicit_dst_offset_is_accepted(self):
        self.assertNotEqual(absolute_time('2028-11-05T01:30-04:00', 'America/New_York', self.now),
                            absolute_time('2028-11-05T01:30-05:00', 'America/New_York', self.now))

    def test_draft_expires_at_ten_minutes_and_confirm_rechecks_due(self):
        row = self.draft(1200)
        self.now += 600
        self.assertFalse(self.confirm(row)['ok'])
        self.assertEqual(self.get(row)['reminder']['state'], 'expired')
        another = self.draft(5)
        self.now += 5
        self.assertFalse(self.confirm(another)['ok'])

    def test_modify_does_not_change_original_until_confirm(self):
        row = self.draft()
        self.assertTrue(self.confirm(row)['ok'])
        edited = self.draft(120, reminder_id=row['id'])
        self.assertEqual(self.get(row)['reminder']['due'], self.now + 60)
        self.assertEqual(self.confirm(edited)['reminder']['id'], row['id'])
        self.assertEqual(self.get(row)['reminder']['due'], self.now + 120)
        self.assertEqual(self.get(edited)['reminder']['state'], 'applied')

    def test_competing_modification_and_cancel_prevent_stale_confirmation(self):
        row = self.draft()
        self.confirm(row)
        a, b = self.draft(120, reminder_id=row['id']), self.draft(180, reminder_id=row['id'])
        self.confirm(a)
        self.assertFalse(self.confirm(b)['ok'])
        c = self.draft(200, reminder_id=row['id'])
        self.call('cancel', {'reminder_id': row['id']})
        self.assertFalse(self.confirm(c)['ok'])

    def test_cancel_active_and_draft_never_sends(self):
        for active in (False, True):
            row = self.draft()
            if active:
                self.confirm(row)
            result = self.call('cancel', {'reminder_id': row['id']})
            self.assertEqual(result['reminder']['state'], 'cancelled')
        self.store.start_sender()
        self.now += 120
        self.assertIsNone(self.store.claim_due())

    def test_replayed_confirmation_is_only_an_old_receipt_not_reactivation(self):
        row = self.draft()
        one = self.confirm(row, request='stable-confirm')
        self.call('cancel', {'reminder_id': row['id']})
        replay = self.confirm(row, request='stable-confirm')
        self.assertEqual(one, replay)
        self.assertEqual(replay['evidence'], 'operation_receipt_not_live_status')
        self.assertEqual(self.get(row)['reminder']['state'], 'cancelled')

    def test_crash_during_send_is_unknown_not_replayed(self):
        row = self.draft()
        self.confirm(row)
        self.store.start_sender()
        self.now += 60
        self.assertEqual(self.store.claim_due()['id'], row['id'])
        self.store.close()
        self.store = Reminders(self.root, lambda: self.now)
        self.addCleanup(self.store.close)
        self.store.start_sender()
        self.assertEqual(self.get(row)['reminder']['state'], 'unknown')
        self.assertIsNone(self.store.claim_due())

    def test_restart_catchup_at_24_hours_and_expire_older(self):
        fresh, old = self.draft(100), self.draft(99)
        self.confirm(fresh); self.confirm(old)
        self.store.close()
        self.now += 100 + 86400
        self.store = Reminders(self.root, lambda: self.now)
        self.addCleanup(self.store.close)
        self.store.start_sender()
        claim = self.store.claim_due()
        self.assertEqual(claim['id'], fresh['id'])
        self.assertEqual(self.store.notification(claim), 'synthetic reminder\n（这条消息晚到了 86400 秒。）')
        self.assertEqual(self.get(old)['reminder']['state'], 'expired')

    def test_short_restart_delay_is_still_disclosed(self):
        row = self.draft()
        self.confirm(row)
        self.store.close()
        self.now += 61
        self.store = Reminders(self.root, lambda: self.now)
        self.addCleanup(self.store.close)
        self.store.start_sender()
        self.assertEqual(self.store.notification(self.store.claim_due()), 'synthetic reminder\n（这条消息晚到了 1 秒。）')

    def test_on_time_notification_is_exact_content_without_management_template(self):
        for content in ('做一次验收', '喝水\n顺便活动一下', '复核原文：提醒 af123 [UTC]'):
            with self.subTest(content=content):
                self.assertEqual(self.store.notification({'content': content, 'late_seconds': 0}), content)

    def test_notification_does_not_mutate_record_or_hide_query_details(self):
        row = self.setting(content='做一次验收')['reminder']
        self.store.start_sender()
        self.now += 60
        claim = self.store.claim_due()
        snapshot = dict(claim)
        self.assertEqual(self.store.notification(claim), '做一次验收')
        self.assertEqual(claim, snapshot)
        details = self.get(row)['reminder']
        self.assertEqual(details['id'], row['id'])
        self.assertEqual(details['absolute_time'], row['absolute_time'])
        self.assertEqual(details['timezone'], 'UTC')

    def test_sender_lease_is_exclusive_and_no_lease_cannot_claim(self):
        with self.assertRaises(RuntimeError):
            self.store.claim_due()
        self.store.start_sender()
        other = Reminders(self.root, lambda: self.now)
        self.addCleanup(other.close)
        with self.assertRaises(BlockingIOError):
            other.start_sender()

    def test_cancel_during_or_after_send_reports_uncertainty(self):
        row = self.draft()
        self.confirm(row)
        self.store.start_sender()
        self.now += 60
        self.store.claim_due()
        for state in ('sending', 'api_returned'):
            if state == 'api_returned':
                self.store.finish(row['id'], state)
            result = self.call('cancel', {'reminder_id': row['id']})
            self.assertEqual(result['reminder']['state'], state)
            self.assertIn('可能已经发送', result['message'])
            self.assertNotIn('已取消', result['message'])

    def test_concurrent_confirmation_and_dispatch_claim_are_unique(self):
        row = self.draft()
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.confirm(row, request='stable-confirm'), range(16)))
        self.assertTrue(all(result == results[0] for result in results))
        self.store.start_sender()
        self.now += 60
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            claims = list(pool.map(lambda _: self.store.claim_due(), range(16)))
        self.assertEqual(sum(row is not None for row in claims), 1)

    def test_duplicate_request_cannot_change_source_tool_or_parameters(self):
        row = self.draft()
        self.get(row, request='same')
        for sender, channel, tool, args in [('other', 'private', 'get', {'reminder_id': row['id']}),
                                          ('owner', 'other', 'get', {'reminder_id': row['id']}),
                                          ('owner', 'private', 'cancel', {'reminder_id': row['id']})]:
            with self.assertRaises(ReminderError):
                self.call(tool, args, request='same', sender=sender, channel=channel)
        self.assertFalse(self.get(row, sender='other')['ok'])
        self.assertFalse(self.get(row, channel='other')['ok'])

    def test_quota_and_content_bounds(self):
        for _ in range(5):
            self.draft()
        self.assertFalse(self.call('draft', {'due_at': '2028-01-01T10:00', 'content': 'sixth'})['ok'])
        for item in self.call('list')['reminders']:
            self.call('cancel', {'reminder_id': item['id']})
        for index in range(20):
            self.confirm(self.draft())
        row = self.draft()
        self.assertFalse(self.confirm(row)['ok'])
        for content in ('', 'x' * 1001, '\0'):
            self.assertFalse(self.call('draft', {'due_at': '2028-01-01T10:00', 'content': content})['ok'])

    def test_file_actions_other_recipients_and_unknown_fields_rejected(self):
        for tool, args in [('create_text', {'path': 'x', 'text': 'x'}), ('run_command', {'program': '/bin/echo'}),
                           ('draft', {'content': 'x', 'due_at': '2028-01-01T10:00', 'sender': 'other'}),
                           ('draft', {'content': 'x', 'due_at': '2028-01-01T10:00', 'channel': 'other'})]:
            self.assertFalse(self.call(tool, args)['ok'])

    def test_list_is_bounded_and_database_is_private(self):
        for _ in range(5):
            self.draft(content='中' * 1000)
        result = self.call('list')
        self.assertFalse(result['truncated'])
        self.assertTrue(all(row['content_truncated'] for row in result['reminders']))
        self.assertEqual(len(result['reminders']), 5)
        import json
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 16384)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)


class ReminderPluginTests(unittest.TestCase):
    def setUp(self):
        chat_tests.PluginAgentTests.setUp(self)
        self.stack.enter_context(mock.patch.object(self.plugin, 'PERSONAL_SERVICES', True))
        self.stack.enter_context(mock.patch.object(self.plugin, 'AGENT_TASKS', False))
        self.store = self.main._reminders()
        self.now = 1800000000.
        self.store.clock = lambda: self.now
        self.store.start_sender()
        self.stack.callback(self.store.close)

    def active(self):
        row = self.store.request('owner', self.event.unified_msg_origin, uuid.uuid4().hex, 'reminder_set',
                                 {'due_at': datetime.fromtimestamp(self.now + 60, timezone.utc).isoformat(),
                                  'timezone': 'UTC', 'content': 'synthetic'})['reminder']
        self.now = row['due']
        return row

    def tick(self):
        async def run():
            await self.main._reminder_tick()
            await asyncio.gather(*self.main._reminder_sends)
        asyncio.run(run())

    def test_send_without_worker_or_file_tasks(self):
        row = self.active()
        self.main._control = mock.AsyncMock(side_effect=AssertionError('Mac must not be called'))
        self.tick(); self.tick()
        self.main._send_notification.assert_awaited_once()
        self.main._send_notification.assert_awaited_once_with(self.event.unified_msg_origin, 'synthetic')
        self.assertEqual(self.store.request('owner', self.event.unified_msg_origin, 'get', 'reminder_get',
                                           {'reminder_id': row['id']})['reminder']['state'], 'api_returned')
        self.main._control.assert_not_awaited()

    def test_receipt_is_short_without_unrelated_delivery_warning(self):
        row = self.active()
        result = {'ok': True, 'reminder': row, 'message': '提醒已设置。'}
        text = self.main._reminder_reply(result)
        self.assertNotIn('API', text)
        self.assertNotIn('confirm', text)
        self.assertIn(row['absolute_time'], text)
        row['state'] = 'unknown'
        self.assertIn('不等于手机收到', self.main._reminder_reply(result))

    def test_api_timeout_or_false_is_unknown_not_retried(self):
        for failure in (TimeoutError('synthetic'), False):
            row = self.active()
            self.main._send_notification = mock.AsyncMock(**({'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}))
            self.tick(); self.tick()
            self.main._send_notification.assert_awaited_once()
            result = self.store.request('owner', self.event.unified_msg_origin, uuid.uuid4().hex, 'reminder_get', {'reminder_id': row['id']})
            self.assertEqual(result['reminder']['state'], 'unknown')

    def test_revoked_source_cannot_send(self):
        self.active()
        with mock.patch.object(self.main, '_agent_allowed', return_value=False):
            self.tick()
        self.main._send_notification.assert_not_awaited()

    def test_fixed_control_never_calls_mac_and_repeated_delivery_does_not_reenable(self):
        row = self.active()
        self.event.get_message_str = lambda: '/agent remind cancel ' + row['id']
        self.main._control = mock.AsyncMock(side_effect=AssertionError('Mac must not be called'))
        asyncio.run(self.main.agent_message(self.event))
        asyncio.run(self.main.agent_message(self.event))
        self.tick()
        self.main._control.assert_not_awaited()
        self.main._send_notification.assert_awaited_once()

    def test_service_poller_cannot_confirm_or_forward_identity_arguments(self):
        with self.main._agent_db() as db:
            db.execute('INSERT INTO agent_service_sources VALUES (?,?)', ('owner', self.event.unified_msg_origin))
        self.main._control = mock.AsyncMock(return_value=(200, {'ok': True, 'requests': [
            {'id': 'forged', 'tool': 'reminder_confirm', 'arguments': {'reminder_id': 'f' * 32}, 'expires': 9999999999}]}))
        asyncio.run(self.main._service_tick())
        self.assertEqual(self.main._control.await_count, 1)

    def test_reminder_loop_stays_live_while_mac_poll_is_unavailable(self):
        self.active()

        async def run():
            blocked, release = asyncio.Event(), asyncio.Event()

            async def slow_mac(*args, **kwargs):
                blocked.set()
                await release.wait()
                return 503, {'ok': False}

            self.main._control = slow_mac
            with self.main._agent_db() as db:
                db.execute('INSERT INTO agent_service_sources VALUES (?,?)', ('owner', self.event.unified_msg_origin))
            with mock.patch.object(self.plugin, 'NOTIFICATIONS_ENABLED', False):
                await self.main.initialize()
                try:
                    await asyncio.wait_for(blocked.wait(), timeout=2)
                    deadline = asyncio.get_running_loop().time() + 2
                    while not self.main._send_notification.await_count and asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(.01)
                    self.main._send_notification.assert_awaited_once()
                finally:
                    release.set()
                    await self.main.terminate()
        asyncio.run(run())
