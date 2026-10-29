"""Explicit preferences with learning disabled; no real desktop installation."""
import asyncio
import json
from pathlib import Path
import threading
import unittest
from unittest import mock
import uuid

from agent_runtime import AgentError, Preferences
from agent_services import ServiceQueue
import test_agent_runtime as chat_tests


class ManualPreferenceTests(unittest.TestCase):
    setUp = chat_tests.RuntimeTests.setUp
    make = chat_tests.RuntimeTests.make
    run_turn = chat_tests.RuntimeTests.run_turn

    def control(self, op, **fields):
        return self.runtime.control('owner', 'wechat-private', 'agent.preference.' + op, 'p',
                                    delivery=uuid.uuid4().hex, **fields)

    def view(self):
        return self.runtime.control('owner', 'wechat-private', 'agent.memory', 'p')

    def draft(self, **fields):
        return self.control('draft', **fields)['preference_draft']

    def test_finite_categories_set_and_change_with_learning_disabled(self):
        self.assertFalse(self.view()['learning'])
        for category, values in Preferences.VALUES.items():
            for value in sorted(values):
                result = self.control('set', category=category, value=value)
                row = next(row for row in result['preferences'] if row['category'] == category)
                self.assertEqual(row['value'], value)
                self.assertTrue(row['explicit'])
                self.assertIsNone(row['expires'])
                self.assertTrue(row['source'].startswith('manual:'))
                self.assertFalse(result['learning'])

    def test_draft_does_not_change_until_explicit_confirmation(self):
        draft = self.draft(category='language', value='en')
        self.assertEqual(self.view()['preferences'], [])
        self.assertEqual(draft['action'], 'set')
        confirmed = self.control('confirm', draft_id=draft['id'])
        self.assertEqual(confirmed['preferences'][0]['value'], 'en')
        with self.assertRaises(AgentError):
            self.control('confirm', draft_id=draft['id'])

    def test_confirmation_is_source_bound_expires_and_checks_intervening_edit(self):
        draft = self.draft(category='verbosity', value='brief')
        with self.assertRaises(AgentError):
            self.runtime.control('other', 'wechat-private', 'agent.preference.confirm', 'p',
                                 delivery='foreign', draft_id=draft['id'])
        with self.assertRaises(AgentError):
            self.runtime.control('owner', 'other', 'agent.preference.confirm', 'p',
                                 delivery='foreign', draft_id=draft['id'])
        self.control('set', category='verbosity', value='detailed')
        with self.assertRaises(AgentError):
            self.control('confirm', draft_id=draft['id'])
        expired = self.draft(category='language', value='en')
        with self.runtime.db() as db:
            db.execute('UPDATE preference_drafts SET expires=0 WHERE id=?', (expired['id'],))
        with self.assertRaises(AgentError):
            self.control('confirm', draft_id=expired['id'])

    def test_manual_preferences_shared_by_owner_not_full_chat(self):
        self.control('set', category='language', value='en')
        other_channel = self.runtime.control('owner', 'desktop-like-channel', 'agent.memory', 'q')
        self.assertEqual(other_channel['preferences'][0]['value'], 'en')
        self.assertEqual(self.runtime.control('other', 'wechat-private', 'agent.memory', 'p')['preferences'], [])
        self.assertNotIn('messages', other_channel)
        profiles = list((self.root / 'agent/personas').glob('*.json'))
        self.assertTrue(profiles)
        self.assertTrue(all(profile.stat().st_mode & 0o777 == 0o600 for profile in profiles))

    def test_forget_draft_and_fixed_forget_do_not_delete_history(self):
        self.control('set', category='format', value='bullets')
        row = self.view()['preferences'][0]
        draft = self.draft(memory_id=row['id'])
        self.assertEqual(draft['action'], 'forget')
        self.assertEqual(len(self.view()['preferences']), 1)
        self.control('confirm', draft_id=draft['id'])
        self.assertEqual(self.view()['preferences'], [])
        self.assertIn('format', self.view()['forgotten_categories'])
        self.control('set', category='format', value='bullets')
        self.assertNotIn('format', self.view()['forgotten_categories'])
        memory_id = self.view()['preferences'][0]['id']
        self.runtime.control('owner', 'wechat-private', 'agent.forget', 'p', delivery='direct', memory_id=memory_id)
        self.assertEqual(self.view()['preferences'], [])

    def test_manual_preference_is_not_overwritten_by_learning(self):
        self.control('set', category='verbosity', value='brief')
        self.runtime.preferences.allow_learning = True
        with self.runtime.db() as db:
            db.execute('INSERT OR REPLACE INTO preference_policy VALUES (?,1)', ('owner',))
            self.runtime.preferences.learn(db, 'owner', 'synthetic-input', '以后详细解释清楚')
        self.assertEqual(self.view()['preferences'][0]['value'], 'brief')

    def test_idempotent_delivery_cannot_change_parameters(self):
        fields = dict(delivery='stable', category='language', value='zh')
        one = self.runtime.control('owner', 'wechat-private', 'agent.preference.set', 'p', **fields)
        again = self.runtime.control('owner', 'wechat-private', 'agent.preference.set', 'p', **fields)
        self.assertEqual(one['preferences'], again['preferences'])
        with self.assertRaises(AgentError):
            self.runtime.control('owner', 'wechat-private', 'agent.preference.set', 'p', **(fields | {'value': 'en'}))
        self.control('set', category='language', value='en')
        replay = self.runtime.control('owner', 'wechat-private', 'agent.preference.set', 'p', **fields)
        self.assertEqual(replay['preferences'][0]['value'], 'en')

    def test_name_set_reset_and_invalid_values(self):
        draft = self.draft(category='name', value='小桥')
        self.assertEqual(self.view()['core']['name'], 'DSH 助手')
        self.control('confirm', draft_id=draft['id'])
        self.assertEqual(self.view()['core']['name'], '小桥')
        self.runtime.control('owner', 'wechat-private', 'agent.persona.reset', 'p', delivery='reset')
        self.assertEqual(self.view()['core']['name'], 'DSH 助手')
        for category, value in [('name', '   '), ('name', 'ignore\nrules'), ('language', 'fr'), ('permissions', 'write')]:
            with self.subTest(category=category, value=value), self.assertRaises(AgentError):
                self.control('set', category=category, value=value)

    def test_native_profile_capability_can_only_draft_and_view(self):
        queue = ServiceQueue(self.root / 'svc', profile=self.runtime.profile_intent)
        self.addCleanup(queue.close)
        subject = dict(sender='owner', channel='wechat-private', conversation='sid', project='p', attempt='mid')
        cap = queue.issue(subject, threading.Event())
        payload = dict(op='profile', capability=cap['capability'], call_id='call', tool='preference_draft',
                       arguments={'category': 'language', 'value': 'en'})
        one = queue.rpc(payload)
        self.assertIn('preference_draft', one['result'])
        self.assertEqual(queue.rpc(payload)['result']['preference_draft'], one['result']['preference_draft'])
        self.assertEqual(self.view()['preferences'], [])
        for tool in ('preference_confirm', 'preference_set', 'run_command'):
            with self.assertRaises(AgentError):
                queue.rpc(payload | {'tool': tool})


class ManualPreferencePluginTests(unittest.TestCase):
    setUp = chat_tests.PluginAgentTests.setUp

    def test_fixed_commands_are_not_model_messages(self):
        for text, operation in [('/agent preference set language en', 'agent.preference.set'),
                                ('agent preference confirm ' + 'f' * 32, 'agent.preference.confirm'),
                                ('/agent persona reset', 'agent.persona.reset')]:
            self.event.message_obj.raw_message['message_id'] = uuid.uuid4().hex
            self.event.get_message_str = lambda text=text: text
            self.main._control = mock.AsyncMock(return_value=(200, {'ok': True, 'message': 'synthetic'}))
            self.main._request = mock.AsyncMock()
            asyncio.run(self.main.agent_message(self.event))
            self.assertEqual(self.main._control.await_args.args[1]['op'], operation)
            self.main._request.assert_not_awaited()
