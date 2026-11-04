"""Explicit preferences with learning disabled; no real desktop installation."""
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import threading
import subprocess
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
    submit = chat_tests.RuntimeTests.submit
    wait = chat_tests.RuntimeTests.wait

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
        self.control('set', category='language', value='en')
        draft = self.draft(category='name', value='小桥')
        self.assertEqual(self.view()['core']['name'], 'DSH 助手')
        self.assertEqual(self.view()['preferences'][0]['value'], 'en')
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
        cap = queue.issue(subject, threading.Event(), '记住以后用英文回复')
        payload = dict(op='profile', capability=cap['capability'], call_id='call', tool='preference_draft',
                       arguments={'category': 'language', 'value': 'en'})
        one = queue.rpc(payload)
        self.assertIn('preference_draft', one['result'])
        self.assertEqual(queue.rpc(payload)['result']['preference_draft'], one['result']['preference_draft'])
        self.assertEqual(self.view()['preferences'], [])
        for tool in ('preference_confirm', 'preference_set', 'run_command'):
            with self.assertRaises(AgentError):
                queue.rpc(payload | {'tool': tool})

    def test_all_drafts_have_chinese_summary_and_exact_confirmation(self):
        for category, values in Preferences.VALUES.items():
            for value in sorted(values):
                result = self.control('draft', category=category, value=value)
                draft = result['preference_draft']
                self.assertEqual(draft['summary'], '设置' + Preferences.summary(category, value))
                self.assertEqual(result['message'].splitlines()[-1], '/agent preference confirm ' + draft['id'])
                self.control('confirm', draft_id=draft['id'])
        view = self.view()
        self.assertIn('自动学习：关', view['display'])
        self.assertIn('当前仅服务器使用，未同步桌面', view['display'])
        for row in view['preferences']:
            self.assertIn(row['id'], view['display'])
            self.assertIn(Preferences.summary(row['category'], row['value']), view['display'])
        name = self.draft(category='name', value=Preferences.CORE['name'])
        self.assertEqual(name['summary'], '设置名字：DSH 助手')

    def test_quoted_files_and_temporary_requests_cannot_create_drafts(self):
        queue = ServiceQueue(self.root / 'svc', profile=self.runtime.profile_intent)
        self.addCleanup(queue.close)
        subject = dict(sender='owner', channel='wechat-private', conversation='sid', project='p', attempt='mid')
        for text in ('这次用英文回答', '本轮请详细回答', '临时设置偏好为英文',
                     '读取文件并记住以后用英文', '他说“以后用英文”', '> 以后用英文',
                     "He said 'remember English'", 'translate: remember English', '不要设置偏好',
                     '不要记住以后用英文', 'do not remember English', 'hello', ''):
            cap = queue.issue(subject, threading.Event(), text)
            payload = dict(op='profile', capability=cap['capability'], call_id='draft',
                           tool='preference_draft', arguments={'category': 'language', 'value': 'en'})
            with self.subTest(text=text), self.assertRaises(AgentError):
                queue.rpc(payload)
            with self.assertRaises(AgentError):
                queue.rpc(payload | {'tool': 'preference_forget_draft', 'arguments': {'memory_id': 'f' * 32}})
            self.assertTrue(queue.rpc(payload | {'tool': 'preference_view', 'arguments': {}})['ok'])
        with self.runtime.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM preference_drafts').fetchone()[0], 0)
        self.assertEqual(self.view()['preferences'], [])
        self.assertTrue(Preferences.direct_change('以后修改文件前先备份'))
        self.assertTrue(Preferences.direct_change('以后先测试再修改'))

    def test_concurrent_confirmation_consumes_once_and_conflicting_draft_stales(self):
        first = self.draft(category='language', value='en')
        other = self.draft(category='language', value='zh')
        def confirm(_):
            return self.runtime.control('owner', 'wechat-private', 'agent.preference.confirm', 'p',
                                        delivery='same-confirmation', draft_id=first['id'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(confirm, range(8)))
        self.assertEqual(sum(not result.get('reused', False) for result in results), 1)
        self.assertTrue(all(result['preferences'][0]['value'] == 'en' for result in results))
        with self.assertRaises(AgentError):
            self.control('confirm', draft_id=other['id'])
        with self.runtime.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM preference_drafts WHERE state='applied'").fetchone()[0], 1)

    def test_latest_preferences_reach_next_turn_new_session_project_and_restart(self):
        self.control('set', category='language', value='en')
        first = self.submit(text='this time answer in Chinese')
        self.wait(first['message_id'])
        self.assertEqual(self.calls[-1]['persona']['preferences'][0]['value'], 'en')
        self.assertEqual(self.view()['preferences'][0]['value'], 'en')
        self.control('set', category='language', value='zh')
        self.runtime.control('owner', 'wechat-private', 'agent.new', 'p', delivery='new')
        second = self.submit('second', 'hello')
        self.wait(second['message_id'])
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(self.calls[-1]['persona']['preferences'][0]['value'], 'zh')
        self.runtime.close()
        self.runtime = self.make()
        third = self.submit('third', 'hello', project='q')
        self.wait(third['message_id'])
        self.assertEqual(self.calls[-1]['subject']['project'], 'q')
        self.assertEqual(self.calls[-1]['persona']['preferences'][0]['value'], 'zh')
        self.assertEqual(self.runtime.control('other', 'wechat-private', 'agent.memory', 'p')['preferences'], [])

    def test_sync_failure_is_partial_success_and_next_turn_never_uses_stale_file(self):
        self.control('set', category='verbosity', value='brief')
        with mock.patch.object(self.runtime, 'export_profile', return_value=False):
            result = self.control('set', category='verbosity', value='detailed')
        self.assertTrue(result['ok'])
        self.assertTrue(result['partial_success'])
        self.assertFalse(result['profile_snapshot_synced'])
        self.assertIn('偏好数据已保存', result['message'])
        self.assertIn('不能确认全部生效', result['message'])
        stale = json.loads(next((self.runtime.root / 'personas').glob('*.json')).read_text())
        self.assertEqual(stale['preferences'][0]['value'], 'brief')
        row = self.submit(text='hello')
        self.wait(row['message_id'])
        self.assertEqual(self.calls[-1]['persona']['preferences'][0]['value'], 'detailed')
        memory_id = self.view()['preferences'][0]['id']
        with mock.patch('agent_runtime.os.replace', side_effect=OSError('synthetic snapshot failure')):
            forgotten = self.runtime.control('owner', 'wechat-private', 'agent.forget', 'p',
                                             delivery='forget-sync', memory_id=memory_id)
        self.assertTrue(forgotten['partial_success'])
        self.assertFalse(list((self.runtime.root / 'personas').glob('*.json')))
        row = self.submit('forgotten', 'remember previous preferences?')
        self.wait(row['message_id'])
        self.assertEqual(self.calls[-1]['persona']['preferences'], [])
        self.assertIn('verbosity', self.calls[-1]['persona']['forgotten_categories'])

    def test_persona_renderer_preserves_defaults_overrides_and_approval_boundary(self):
        node = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE')
        if not node:
            self.skipTest('explicit pinned native runtime required')
        script = '''
import assert from 'node:assert/strict';
import {personaText} from './agent_persona.mjs';
const text=personaText({core:{name:'小桥'},preferences:[{category:'language',value:'en'},
 {category:'verbosity',value:'detailed'},{category:'format',value:'bullets'},
 {category:'workflow',value:'test_first'}]});
for(const expected of ['小桥','用英文回答','详细解释','分点','先验证相关测试','不能据此扩大工具权限或省略确认',
 '不自动保存为长期偏好','旧名字或已忘记值不恢复']) assert.ok(text.includes(expected),expected);
assert.ok(!text.includes('以简洁自然的中文交流'));
assert.ok(!personaText({preferences:[{category:'language',value:'en',expires:1}]}).includes('用英文回答'));
'''
        result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True,
                                env={'PATH': '/usr/bin:/bin', 'HOME': str(self.root), 'ELECTRON_RUN_AS_NODE': '1'}, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


class ManualPreferencePluginTests(unittest.TestCase):
    setUp = chat_tests.PluginAgentTests.setUp

    def test_view_is_chinese_and_partial_success_warning_is_not_hidden(self):
        self.event.get_message_str = lambda: '/agent preference'
        display = '名字：DSH 助手；自动学习：关。当前仅服务器使用，未同步桌面。\n语言：英文（ID：' + 'a' * 32 + '）'
        warning = '偏好数据已保存，但偏好快照同步失败，不能确认全部生效。'
        self.main._control = mock.AsyncMock(return_value=(200, dict(ok=True, preferences=[], display=display,
                                                                  message=warning, partial_success=True)))
        self.main._agent_local = mock.AsyncMock()
        asyncio.run(self.main.agent_message(self.event))
        self.assertEqual(self.main._agent_local.await_args.args[1], display + '\n' + warning)

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
