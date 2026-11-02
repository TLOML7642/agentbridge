"""Independent P1 candidate and fixed shim routing, synthetic backends only."""
import asyncio
import concurrent.futures
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import assistant
from agent_runtime import AgentError
import shim
import worker
import process_supervisor
from http.server import ThreadingHTTPServer
from test_s1_s5 import load_plugin


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='aba-', dir='/tmp'))).resolve()
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.stack.callback(self.release.set)
        self.app = self.make()
        self.server = assistant.create_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.stack.callback(self.stop_server)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.secret_file = self.root / 'shared-secret'
        self.secret_file.write_text('synthetic-assistant-secret')
        self.secret_file.chmod(0o600)
        self.stack.enter_context(mock.patch.multiple(shim, ASSISTANT=self.url,
            ASSISTANT_SECRET_FILE=str(self.secret_file), AGENT_ENABLED=True, SENDER_ALLOW={'owner'},
            AGENT_CHANNELS={'private'}, WORKER='http://127.0.0.1:1/execute', WORKER_SECRET='synthetic-mac-secret'))

    def run_turn(self, turn, cancel):
        self.calls.append(turn)
        while not self.release.wait(.01):
            if cancel.is_set():
                raise AgentError('CANCELLED', 'synthetic executor stopped')
        return 'synthetic-reply:' + turn['text']

    def make(self):
        app = assistant.App(self.root / 'a', 'synthetic-assistant-secret', {'owner', 'other'},
                            {'private', 'other-private'}, services=True, runner=self.run_turn)
        self.stack.callback(app.close)
        return app

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()

    def request(self, path, body=None, secret='synthetic-assistant-secret'):
        request = urllib.request.Request(self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'X-AgentBridge-Secret': secret, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def message(self, delivery='d1', text='hello', **extra):
        return self.request('/agent/messages', dict(sender='owner', channel='private', delivery_id=delivery, text=text) | extra)

    def control(self, op, **extra):
        return self.request('/control', dict(sender='owner', channel='private', op=op) | extra)

    def wait(self, identifier, state='done'):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            _, row = self.control('agent.status', message_id=identifier)
            if row.get('state') == state:
                return row
            time.sleep(.01)
        self.fail('isolated assistant did not reach ' + state)

    def test_durable_duplicate_and_parameter_conflict_without_mac(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.message()[1], range(8)))
        self.assertEqual(len({x['message_id'] for x in results}), 1)
        self.wait(results[0]['message_id'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.message(text='changed')[1]['code'], 'DELIVERY_CONFLICT')
        self.assertEqual(self.calls[0]['subject']['cwd'], str(self.app.workspace))
        self.assertEqual(self.calls[0]['subject']['project'], 'assistant')

    def test_restart_resumes_and_lookup_never_creates_new_input(self):
        _, first = self.message()
        self.wait(first['message_id'])
        self.app.close()
        self.app = self.make()
        self.server.app = self.app
        self.assertEqual(self.control('agent.lookup', delivery_id='d1')[1]['message_id'], first['message_id'])
        self.assertEqual(self.control('agent.lookup', delivery_id='missing')[1]['code'], 'NOT_FOUND')
        _, second = self.message('d2', 'second')
        self.wait(second['message_id'])
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertTrue(self.calls[-1]['resume'])

    def test_preferences_set_view_forget_without_mac_or_learning(self):
        code, data = self.control('agent.preference.set', category='verbosity', value='brief', delivery_id='pref-1')
        self.assertEqual(code, 200)
        memory = data['preferences'][0]['id']
        self.assertFalse(data['learning'])
        self.assertTrue(data['server_profile_synced'])
        self.assertEqual(data['profile_projection'], 'server_only')
        self.assertNotIn('desktop_profile_synced', data)
        self.assertEqual(self.control('agent.memory')[1]['preferences'][0]['value'], 'brief')
        self.assertTrue(self.control('agent.forget', memory_id=memory, delivery_id='forget-1')[1]['ok'])
        self.assertEqual(self.control('agent.memory')[1]['preferences'], [])
        self.assertEqual(self.calls, [])

    def test_queued_chat_does_not_block_preferences_and_cancel_is_confirmed(self):
        self.release.clear()
        _, first = self.message()
        self.wait(first['message_id'], 'running')
        self.assertEqual(self.control('agent.memory')[0], 200)
        _, second = self.message('d2', 'queued')
        self.control('agent.cancel', message_id=second['message_id'], delivery_id='cancel-queued')
        self.wait(second['message_id'], 'cancelled')
        self.control('agent.cancel', message_id=first['message_id'], delivery_id='cancel-running')
        self.wait(first['message_id'], 'cancelled')
        self.assertEqual(len(self.calls), 1)

    def test_unknown_model_outcome_blocks_session_instead_of_retry(self):
        self.app.agent.runner = mock.Mock(side_effect=AgentError('DRIVER_UNKNOWN', 'synthetic unknown outcome'))
        _, first = self.message()
        self.wait(first['message_id'], 'uncertain')
        self.assertEqual(self.message()[1]['message_id'], first['message_id'])
        self.assertEqual(self.message('fresh-id')[1]['code'], 'SESSION_UNCERTAIN')
        self.assertEqual(self.app.agent.runner.call_count, 1)

    def test_identity_and_endpoint_parameters_are_host_bound(self):
        self.assertEqual(self.message(sender='unauthorized')[0], 403)
        for key in ('cwd', 'project', 'executor', 'endpoint', 'receiver', 'read_mode'):
            self.assertEqual(self.message(**{key: 'forged'})[1]['code'], 'INVALID_MESSAGE')
            self.assertEqual(self.control('agent.memory', **{key: 'forged'})[1]['code'], 'INVALID_MESSAGE')
        self.assertEqual(self.calls, [])
        _, first = self.message()
        self.wait(first['message_id'])
        self.assertEqual(self.control('agent.status', message_id=first['message_id'], sender='other')[1]['code'], 'NOT_FOUND')
        self.assertEqual(self.control('agent.status', message_id=first['message_id'], channel='other-private')[1]['code'], 'NOT_FOUND')

    def test_loopback_auth_and_no_legacy_execute_or_project_registration(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.request('/health', secret='bad')[0], 401)
        health = self.request('/health')[1]
        self.assertEqual(health['host_role'], 'server_assistant')
        self.assertEqual(health['mac_status'], 'not_probed')
        for path in ('/execute', '/project/add', '/job', '/agent/messages?executor=mac'):
            self.assertEqual(self.request(path, {'task': 'never execute'})[0], 404)
        self.assertEqual(self.control('use', project='arbitrary', delivery_id='x')[1]['code'], 'INVALID_MESSAGE')
        for op in assistant.MAC_CONTROLS:
            self.assertEqual(self.control(op)[1]['code'], 'MAC_TOOLS_UNAVAILABLE')
        self.assertEqual(self.calls, [])

    def test_chunked_duplicate_lengths_and_nonobject_json_rejected(self):
        for headers in ({'Transfer-Encoding': 'chunked'}, {'Content-Length': '999999'}):
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
            try:
                connection.request('POST', '/agent/messages', b'{}',
                                   headers={'X-AgentBridge-Secret': self.app.secret, **headers})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                connection.close()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.putrequest('POST', '/agent/messages')
            connection.putheader('X-AgentBridge-Secret', self.app.secret)
            connection.putheader('Content-Length', '2')
            connection.putheader('Content-Length', '2')
            connection.endheaders(b'{}')
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()
        self.assertEqual(self.calls, [])

    def test_shim_messages_and_agent_controls_use_assistant_only(self):
        result = shim._accept_agent({'delivery_id': 'shim-1', 'text': 'hello'}, 'owner', 'private')
        self.wait(result['message_id'])
        queried = shim.control_worker({'op': 'agent.lookup', 'delivery_id': 'shim-1'}, {'sender': 'owner', 'channel': 'private'})
        self.assertEqual(queried['message_id'], result['message_id'])
        self.assertEqual(shim._worker_base(), 'http://127.0.0.1:1')
        self.assertEqual(shim._accept_agent({'delivery_id': 'shim-1', 'text': 'changed'}, 'owner', 'private')['code'], 'DELIVERY_CONFLICT')

    def test_shim_legacy_controls_still_use_mac_not_assistant(self):
        with mock.patch.object(shim.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"ok":true}')) as opened:
            shim.control_worker({'op': 'status'}, {'sender': 'owner', 'channel': 'private'})
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:1/control')
        self.assertEqual(request.get_header('X-agentbridge-secret'), 'synthetic-mac-secret')

    def test_bad_assistant_config_does_not_fall_back_or_dispatch(self):
        for url in ('https://127.0.0.1:9010', 'http://example.com:9010', 'http://localhost:9010',
                    'http://127.0.0.1:9010/execute', 'http://user@127.0.0.1:9010', 'http://127.0.0.1:9010?x=1'):
            with mock.patch.object(shim, 'ASSISTANT', url), mock.patch.object(shim.urllib.request, 'urlopen') as opened:
                with self.assertRaises(shim.ApiError):
                    shim._accept_agent({'delivery_id': 'bad', 'text': 'hello'}, 'owner', 'private')
                opened.assert_not_called()
        with mock.patch.object(shim, 'ASSISTANT', ''):
            self.assertEqual(shim._agent_target(), ('http://127.0.0.1:1', 'synthetic-mac-secret'))

    def test_assistant_failure_and_lost_acceptance_never_fall_back_to_mac(self):
        for error in (urllib.error.URLError(ConnectionRefusedError()), TimeoutError()):
            with mock.patch.object(shim.urllib.request, 'urlopen', side_effect=error) as opened:
                with self.assertRaises((urllib.error.URLError, TimeoutError)):
                    shim._accept_agent({'delivery_id': 'unknown', 'text': 'hello'}, 'owner', 'private')
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(opened.call_args.args[0].full_url, self.url + '/agent/messages')

    def shim_server(self):
        self.stack.enter_context(mock.patch.object(shim, 'TOKEN', 'synthetic-client-token'))
        server = ThreadingHTTPServer(('127.0.0.1', 0), shim.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            thread.join(3)
            server.server_close()

        self.stack.callback(close)
        return server

    def shim_post(self, server, path, body):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=4)
        try:
            connection.request('POST', path, json.dumps(body).encode(), headers={
                'Authorization': 'Bearer synthetic-client-token', 'Content-Type': 'application/json',
                'X-AgentBridge-Sender': 'owner', 'X-AgentBridge-Channel': 'private'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_actual_shim_http_accepts_and_reconciles_with_mac_unreachable(self):
        server = self.shim_server()
        code, result = self.shim_post(server, '/v1/agentbridge/messages', {'delivery_id': 'http-1', 'text': 'hello'})
        self.assertEqual(code, 202)
        self.wait(result['message_id'])
        code, queried = self.shim_post(server, '/v1/agentbridge/control', {'op': 'agent.lookup', 'delivery_id': 'http-1'})
        self.assertEqual(code, 200)
        self.assertEqual(queried['message_id'], result['message_id'])
        self.assertEqual(queried['state'], 'done')

    def test_actual_shim_http_distinguishes_not_accepted_and_unknown(self):
        server = self.shim_server()
        for error, expected in ((urllib.error.URLError(ConnectionRefusedError()), 'NOT_ACCEPTED'),
                                (TimeoutError(), 'ACCEPT_UNKNOWN')):
            with mock.patch.object(shim.urllib.request, 'urlopen', side_effect=error) as opened:
                status, data = self.shim_post(server, '/v1/agentbridge/messages', {'delivery_id': 'lost', 'text': 'hello'})
                self.assertEqual(status, 503)
                self.assertEqual(data['error']['code'], expected)
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(opened.call_args.args[0].full_url, self.url + '/agent/messages')

    def test_original_plugin_reminders_use_assistant_queue_not_another_scheduler(self):
        cancel = threading.Event()
        subject = dict(sender='owner', channel='private', conversation='synthetic-session',
                       project='assistant', attempt='synthetic-attempt')
        queue = self.app.agent.services
        capability = queue.issue(subject, cancel, '请提醒我明天喝水')
        with mock.patch.object(cancel, 'wait', return_value=True):
            request = queue.rpc(dict(op='request', capability=capability['capability'], call_id='remind-1',
                tool='reminder_set', arguments={'due_at': '2028-01-01T10:00', 'content': 'synthetic-reminder'}))
        plugin = load_plugin()
        self.stack.enter_context(mock.patch.multiple(plugin, DATA_DIR=self.root / 'plugin', AGENT_ENABLED=True, PERSONAL_SERVICES=True))
        main = plugin.Main(None)
        main._agent_allowed = lambda sender, channel: sender == 'owner' and channel == 'private'

        async def control(event, payload):
            return 200, shim.control_worker(payload, {'sender': event.get_sender_id(), 'channel': event.unified_msg_origin})

        main._control = control
        with main._agent_db() as db:
            db.execute('INSERT INTO agent_service_sources VALUES (?,?)', ('owner', 'private'))
        asyncio.run(main._service_tick())
        asyncio.run(main._service_tick())
        result = queue.result(subject, request['request_id'])
        self.assertEqual(result['state'], 'resolved')
        self.assertTrue(result['result']['ok'])
        self.assertTrue((self.root / 'plugin/reminders.db').exists())
        self.assertEqual(list((self.root / 'a').rglob('reminders.db')), [])
        self.assertIsNone(main._reminder_task)
        self.assertEqual(self.calls, [])

    def test_disabled_services_and_invalid_roots_fail_closed(self):
        for senders, channels in ((set(), {'private'}), ({'owner'}, set())):
            with self.assertRaises(AgentError):
                assistant.App(self.root / 'invalid', 'synthetic', senders, channels, runner=self.run_turn)
        link = self.root / 'link'
        link.symlink_to(self.root / 'a', target_is_directory=True)
        with self.assertRaises(AgentError):
            assistant.App(link, 'synthetic', {'owner'}, {'private'}, runner=self.run_turn)
        app = assistant.App(self.root / 'disabled', 'synthetic', {'owner'}, {'private'}, runner=self.run_turn)
        self.stack.callback(app.close)
        with self.assertRaises(AgentError) as caught:
            app.control(dict(sender='owner', channel='private', op='agent.service.requests'))
        self.assertEqual(caught.exception.code, 'SERVICE_DISABLED')

    def test_cli_is_opt_in_and_refuses_unverified_platform_before_listening(self):
        previous = os.umask(0o077)
        os.umask(previous)
        self.stack.callback(os.umask, previous)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(assistant, 'create_server') as listening:
            with self.assertRaises(SystemExit):
                assistant.main()
            listening.assert_not_called()
        values = dict(AGENTBRIDGE_AGENT_MESSAGES='1', AGENTBRIDGE_AGENT_NODE='/unverified/node',
                      AGENTBRIDGE_AGENT_PACKAGES='/unverified/packages',
                      AGENTBRIDGE_AGENT_CREDENTIALS='/unverified/credentials',
                      AGENTBRIDGE_AGENT_CREDENTIAL_REF='SERVER_KEY')
        with mock.patch.dict(os.environ, values, clear=True), \
                mock.patch.object(assistant.sys, 'platform', 'darwin'), \
                mock.patch.object(assistant, 'create_server') as listening:
            with self.assertRaises(SystemExit):
                assistant.main()
            listening.assert_not_called()


class NativeAssistantTests(unittest.TestCase):
    def test_server_native_tools_do_not_read_local_files_or_launch_old_scheduler(self):
        node, packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE'), os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not node or not packages:
            self.skipTest('explicit pinned native test runtime required')
        with tempfile.TemporaryDirectory(prefix='abna-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            original = worker.run_task
            selected = {'name': 'runtime_status', 'arguments': {}}

            def run(command, **kwargs):
                file = Path(command[-1])
                payload = json.loads(file.read_text())
                self.assertEqual(payload['read_mode'], 'none')
                payload.update(synthetic=True, syntheticTool=selected)
                file.write_text(json.dumps(payload))
                return original(command, **kwargs)

            with mock.patch.dict(os.environ, {'AGENTBRIDGE_AGENT_NODE': node, 'AGENTBRIDGE_AGENT_PACKAGES': packages}), \
                    mock.patch.object(process_supervisor, 'run_task', side_effect=run), \
                    mock.patch.object(worker, 'App') as legacy, mock.patch.object(worker, 'Scheduler') as scheduler:
                app = assistant.App(root / 'a', 'synthetic', {'owner'}, {'private'})
                try:
                    def turn(delivery):
                        row = app.message(dict(sender='owner', channel='private', delivery_id=delivery, text='synthetic native request'))
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            final = app.control(dict(sender='owner', channel='private', op='agent.status', message_id=row['message_id']))
                            if final['state'] not in ('queued', 'running'):
                                return final
                            time.sleep(.01)
                        self.fail('native assistant did not finish')

                    first = turn('native-status')
                    self.assertEqual(first['state'], 'done')
                    tool = json.loads(json.loads(first['reply'])['toolResult'])['content'][0]
                    info = json.loads(tool['content'][0]['text'])
                    self.assertEqual(set(info['tools']), {'recent_tasks', 'runtime_status'})
                    self.assertEqual(info['host_role'], 'server_assistant')
                    marker = app.workspace / 'private.txt'
                    marker.write_text('synthetic-private-file-not-authorized')
                    selected = {'name': 'read_text', 'arguments': {'file': str(marker)}}
                    second = turn('native-forbidden-read')
                    self.assertEqual(second['state'], 'done')
                    tool = json.loads(json.loads(second['reply'])['toolResult'])['content'][0]
                    self.assertTrue(tool['isError'])
                    self.assertNotIn('synthetic-private-file-not-authorized', second['reply'])
                    legacy.assert_not_called()
                    scheduler.assert_not_called()
                finally:
                    app.close()


if __name__ == '__main__':
    unittest.main()
