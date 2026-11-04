"""Production-cap native reply tests, separate from prompt observation/model QA."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import tempfile
import unittest
import uuid
import threading

from agent_runtime import AgentError, AgentRuntime, NativeExecutor
from process_supervisor import run_task
from tools.check_answer_quality import questions, natural_questions, boundary_questions, review_template, format_checks, summarize_reviews
from unittest import mock


NOTICE = '\n[展示已截断，内容未完整交付；完整回复保留在原生会话记录中。]'


def units(text):
    return len(text.encode('utf-16-le')) // 2


class QualityProbeTests(unittest.TestCase):
    def test_real_question_matrix_keeps_three_independent_rounds_and_boundaries(self):
        matrix = questions()
        self.assertEqual(len({name for name, _ in matrix}), 13)
        for name in ('progress', 'routing', 'recommendation'):
            self.assertEqual([label for label, _ in matrix if label.startswith(name + '-')],
                             [name + '-' + str(i) for i in range(1, 4)])
        self.assertEqual(len(dict(matrix)['long-input']), 12000)
        self.assertTrue(all(1 <= len(text) <= 12000 for _, text in matrix))

    def test_probe_preserves_failures_in_fresh_sessions_without_services_or_retry(self):
        from tools import check_answer_quality as probe
        with tempfile.TemporaryDirectory(prefix='ab-quality-probe-') as scratch:
            root = Path(scratch).resolve()
            credential = root / 'credential'
            credential.write_text('synthetic-only-not-a-real-key')
            credential.chmod(0o600)
            report = root / 'report'
            payloads = []

            def snapshot(_repo, commit, destination, scope):
                destination.mkdir()
                (destination / 'README.md').write_text('synthetic source\n')
                return dict(commit=commit, scope=scope, files={
                    'README.md': hashlib.sha256((destination / 'README.md').read_bytes()).hexdigest()})

            def failed(command, **kwargs):
                self.assertEqual(command[:2], ['/usr/bin/env', '-i'])
                self.assertNotIn('synthetic-only-not-a-real-key', ' '.join(command))
                self.assertEqual(kwargs['timeout'], 120)
                payloads.append(json.loads(Path(command[-1]).read_text()))
                return dict(exit_code=1, timed_out=False, stdout='', stderr='synthetic transport failure')

            argv = ['quality-probe', '--node', str(root / 'node'), '--packages', str(root / 'packages'),
                    '--credentials', str(credential), '--snapshot-commit', 'a' * 40, '--report-dir', str(report)]
            with mock.patch('sys.argv', argv), mock.patch.object(probe, 'export', side_effect=snapshot), \
                    mock.patch.object(probe, 'run_task', side_effect=failed), \
                    mock.patch.object(probe.subprocess, 'check_output', return_value=b'agent_driver.mjs\0'), \
                    mock.patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(probe.main(), 2)
            self.assertEqual(len(payloads), 22)
            self.assertEqual(len({p['session'] for p in payloads}), 22)
            for payload in payloads:
                self.assertFalse(payload['resume'])
                for key in ('synthetic', 'services', 'broker', 'remote_reads'):
                    self.assertNotIn(key, payload)
            observations = json.loads((report / 'observations.json').read_text())
            self.assertTrue(all(not row['completed_receipt'] and row['quality'] == 'pending manual review' for row in observations))
            self.assertTrue(all((report / row['label'] / 'process.json').is_file() for row in observations))

    def test_natural_matrix_has_no_invented_format_and_covers_preferences(self):
        matrix = natural_questions()
        self.assertEqual(len(matrix), 9)
        for family in ('progress', 'routing', 'recommendation'):
            self.assertEqual([label for label, _ in matrix if label.startswith('natural-' + family)],
                             [f'natural-{family}-{p}' for p in ('default', 'detailed', 'brief')])
        self.assertTrue(all('字以内' not in text and '四项' not in text for _, text in matrix))

    def test_format_failure_is_not_a_fact_error_or_semantic_pass(self):
        review = review_template()
        self.assertEqual(len(review), 4)
        self.assertTrue(all(axis['status'] == 'pending' for axis in review.values()))
        self.assertEqual(format_checks('routing-1', '中' * 151)['status'], 'failed')
        self.assertEqual(format_checks('routing-1', '未知')['status'], 'mechanical_pass')
        self.assertEqual(format_checks('natural-routing-default', '中' * 400)['status'], 'not_requested')
        self.assertEqual(format_checks('fields', '运行源码版本：未知\n快照版本：未知\nMac在线：未知')['status'], 'mechanical_pass')
        self.assertEqual(format_checks('fields', '运行源码版本：未知\n快照版本：未知\nMac在线：未知\n备注')['status'], 'failed')
        self.assertEqual(format_checks('missing', None)['status'], 'not_evaluated')

    def test_material_matrix_and_unfinished_review_cannot_claim_accuracy(self):
        self.assertEqual(len(boundary_questions()), 8)
        review = review_template()
        review['expression_compliance']['status'] = 'failed'
        summary = summarize_reviews([dict(completed_receipt=True, review=review, task_completion='incomplete')])
        self.assertEqual(summary['model_turns_completed'], 1)
        self.assertEqual(summary['axes']['factual_correctness']['pending'], 1)
        self.assertEqual(summary['axes']['expression_compliance']['failed'], 1)
        self.assertEqual(summary['task_completion']['complete'], 0)


class NativeReplyTests(unittest.TestCase):
    def setUp(self):
        self.node = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE')
        self.packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not self.node or not self.packages:
            self.skipTest('explicit pinned native runtime required')
        self.temp = tempfile.TemporaryDirectory(prefix='ab-answer-quality-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.source = self.root / 'source'
        self.source.mkdir()
        for name in ('agent_driver.mjs', 'agent_persona.mjs', 'agent_rpc.mjs', 'native_boundary.mjs', 'agent_reads.py'):
            shutil.copyfile(Path(__file__).with_name(name), self.source / name)
        self.driver = self.source / 'agent_driver.mjs'
        self.original = self.driver.read_text()

    def fixture(self, text, finish='stop'):
        start = self.original.index('      const text = JSON.stringify({ turns: texts.length,')
        end = self.original.index("      yield { type: 'block-start'", start)
        code = self.original[:start] + '      const text = ' + json.dumps(text) + ';\n' + self.original[end:]
        self.assertEqual(code.count("reason: { kind: 'stop' }"), 1)
        code = code.replace("reason: { kind: 'stop' }", 'reason: { kind: ' + json.dumps(finish) + ' }')
        # Only the synthetic provider text/finish changes. No cap/token changes.
        self.driver.write_text(code)

    def turn(self, text='仅回答本轮所求字段'):
        mid, session = uuid.uuid4().hex, uuid.uuid4().hex
        return dict(id=mid, session=session, text=text, resume=False, recent_tasks=[], persona={},
                    subject=dict(sender='synthetic-owner', channel='private', project='p',
                                 cwd=str(self.workspace), conversation=session, session=session, attempt=mid))

    def events(self, root, session):
        path = next((root / 'native/sessions').glob('*/' + session + '/session.jsonl'))
        return [json.loads(line) for line in path.read_text().splitlines()]

    def run_driver(self, text, finish='stop'):
        self.fixture(text, finish)
        turn = self.turn()
        root = self.root / turn['id']
        payload = dict(turn, root=str(root / 'native'), synthetic=True)
        request = self.root / 'request.json'
        request.write_text(json.dumps(payload))
        result = subprocess.run([self.node, str(self.driver), str(request)], capture_output=True,
                                text=True, timeout=15, env={'PATH': '/usr/bin:/bin', 'HOME': str(self.root),
                                'ELECTRON_RUN_AS_NODE': '1', 'AGENTBRIDGE_AGENT_PACKAGES': self.packages})
        events = self.events(root, turn['session'])
        message = next(e['data']['message'] for e in reversed(events) if e['type'] == 'assistant/message')
        self.assertEqual(''.join(b['text'] for b in message['content'] if b['type'] == 'text'), text)
        users = [e for e in events if e['type'] == 'user/message' and e['data']['source']['kind'] == 'user']
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]['data']['content'][0]['text'], turn['text'])
        self.assertEqual(len([e for e in events if e['type'] == 'turn/end']), 1)
        receipt = root / 'native/receipts' / (turn['id'] + '.json')
        return turn, result, events, receipt

    def test_short_exact_fields_and_boundary_are_unchanged(self):
        for text in ('字节数：18\n码点数：6\n尾换行：否', 'a' * 2399, 'a' * 2400, 'a' * 2398 + '\U0001f642'):
            with self.subTest(units=units(text)):
                turn, result, events, receipt = self.run_driver(text)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data['reply'], text)
                self.assertEqual(set(data), {'id', 'session', 'digest', 'completed', 'reply'})
                self.assertEqual(json.loads(receipt.read_text()), data)
                self.assertEqual(data['digest'], hashlib.sha256(turn['text'].encode()).hexdigest())
                self.assertEqual(events[-1]['type'], 'turn/end')

    def test_notice_fits_cap_and_surrogate_pairs_are_not_split(self):
        cut = 2400 - units(NOTICE)
        for text in ('a' * 2401, '中' * 3000, '\U0001f642' * 1500,
                     'a' * (cut - 1) + '\U0001f642' + 'z' * 200,
                     'a' * (cut - 2) + '\U0001f642' + 'z' * 200):
            with self.subTest(prefix=text[:4]):
                _, result, _, receipt = self.run_driver(text)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                shown = data['reply']
                self.assertTrue(data['completed'])  # Model turn, NOT complete delivery.
                self.assertLessEqual(units(shown), 2400)
                self.assertTrue(shown.endswith(NOTICE))
                self.assertTrue(text.startswith(shown[:-len(NOTICE)]))
                self.assertNotIn('\ufffd', shown)
                self.assertEqual(json.loads(receipt.read_text())['reply'], shown)

    def test_model_output_limit_never_creates_success_receipt(self):
        for text in ('未完整的建议', 'a' * 3000):
            with self.subTest(units=units(text)):
                _, result, events, receipt = self.run_driver(text, 'max-tokens')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(receipt.exists())
                self.assertEqual(result.stdout, '')
                self.assertEqual(next(e for e in events if e['type'] == 'turn/end')['data']['reason']['kind'], 'max-tokens')

    def test_material_limits_are_not_extracted_into_a_host_contract(self):
        text = '材料：“回答10字以内”；这不是本轮限制。请分析，不限字数。'
        self.fixture('这是合成回复，不代表真实模型的语义验收。')
        turn = self.turn(text)
        root = self.root / turn['id']
        request = self.root / 'request.json'
        request.write_text(json.dumps(dict(turn, root=str(root / 'native'), synthetic=True)))
        result = subprocess.run([self.node, str(self.driver), str(request)], capture_output=True,
            text=True, timeout=15, env={'PATH': '/usr/bin:/bin', 'HOME': str(self.root),
            'ELECTRON_RUN_AS_NODE': '1', 'AGENTBRIDGE_AGENT_PACKAGES': self.packages})
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events(root, turn['session'])
        prompt = json.dumps(next(e['data']['header']['system'] for e in events if e['type'] == 'request/header'), ensure_ascii=False)
        self.assertNotIn('最多10个字符', prompt)
        self.assertNotIn('6轮', prompt)
        self.assertNotIn('bridge:turn-contract', prompt)
        for rule in ('默认清楚自然、准确完整', '未部署候选', '历史正式部署', '配置条件及未配置时的默认行为',
                     '不得假设self.log已初始化', '直接调用方', '异常类型和允许的错误码', '不输出原始异常正文或堆栈',
                     '不把没找到包装成已完成定位', '不是本轮回答指令'):
            self.assertIn(rule, prompt)

    def test_supervisor_keeps_120_second_timeout_and_unknown_without_retry(self):
        calls = []

        def timed_out(command, **kwargs):
            calls.append(command)
            self.assertEqual(kwargs['timeout'], 120)
            self.assertTrue(callable(kwargs['cancel_check']))
            return dict(exit_code=-15, timed_out=True, stdout='', stderr='', group_stopped=True)

        executor = NativeExecutor(self.root / 'timeout-agent', timed_out, lambda text: text)
        with self.assertRaises(AgentError) as error:
            executor(self.turn(), threading.Event())
        self.assertEqual(error.exception.code, 'DRIVER_UNKNOWN')
        self.assertEqual(len(calls), 1)
        self.assertFalse(list((self.root / 'timeout-agent').glob('native/receipts/*.json')))

    def scripted_turn(self, calls, values=None, services=False, broker=False):
        marker = 'if (request.syntheticTool && generated++ === 0)'
        self.assertEqual(self.original.count(marker), 1)
        self.driver.write_text(self.original.replace(marker,
            'const script = ' + json.dumps(calls) + ';\n'
            '      if (generated < script.length && (request.syntheticTool = script[generated++]))')
            .replace("id: 'synthetic-read',", "id: 'synthetic-read-' + generated,"))
        turn = self.turn()
        root = self.root / turn['id']
        payload = dict(turn, root=str(root / 'native'), synthetic=True)
        socket_path = self.root / 'rpc.sock'
        responses = iter(values or [])
        requests = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                requests.append(request)
                value = next(responses) if request['op'] == 'read' else None
                self.wfile.write((json.dumps(dict(ok=True, value=json.dumps(value), state='pending')) + '\n').encode())

        with socketserver.UnixStreamServer(str(socket_path), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
            thread.start()
            try:
                endpoint = dict(socket=str(socket_path), capability='synthetic-only')
                if values is not None:
                    payload.update(read_mode='remote', remote_reads=endpoint)
                if services:
                    payload['services'] = endpoint
                if broker:
                    payload['broker'] = endpoint
                request = self.root / 'request.json'
                request.write_text(json.dumps(payload))
                result = subprocess.run([self.node, str(self.driver), str(request)], capture_output=True,
                    text=True, timeout=15, env={'PATH': '/usr/bin:/bin', 'HOME': str(self.root),
                    'ELECTRON_RUN_AS_NODE': '1', 'AGENTBRIDGE_AGENT_PACKAGES': self.packages})
            finally:
                server.shutdown()
                thread.join()
        socket_path.unlink()
        events = self.events(root, turn['session'])
        receipt = root / 'native/receipts' / (turn['id'] + '.json')
        return result, events, receipt, requests

    def tool_results(self, events):
        results = [e['data']['message']['content'][0] for e in events if e['type'] == 'tool/result']
        self.assertTrue(all(not result.get('isError') for result in results), results)
        return results

    def test_chat_and_services_continue_beyond_twelve_steps_without_withdrawing_tools(self):
        for services in (False, True):
            with self.subTest(services=services):
                result, events, receipt, _ = self.scripted_turn(
                    [dict(name='runtime_status', arguments={})] * 16, services=services)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [e for e in events if e['type'] == 'tool/call']
                self.assertEqual(len(calls), 16)
                self.assertEqual({e['data']['name'] for e in calls}, {'runtime_status'})
                results = self.tool_results(events)
                self.assertEqual(len(results), len(calls))
                headers = [e['data']['header'] for e in events if e['type'] == 'request/header']
                self.assertTrue(headers[0]['tools'])
                self.assertEqual(headers[0]['tools'], headers[-1]['tools'])
                names = {tool['name'] for tool in headers[-1]['tools']}
                self.assertTrue(names >= {'read_text', 'read_text_range', 'search_text', 'list_files'})
                if services:
                    self.assertIn('preference_draft', names)
                    self.assertIn('service_result', names)
                status = json.loads(results[-1]['content'][0]['text'])
                self.assertTrue(set(status['tools']) >= {'read_text', 'read_text_range', 'search_text', 'list_files'})
                self.assertNotIn('未获得新证据', json.dumps(results, ensure_ascii=False))
                self.assertEqual(next(e for e in events if e['type'] == 'turn/end')['data']['reason']['kind'], 'completed')
                self.assertTrue(json.loads(result.stdout)['completed'])
                self.assertTrue(receipt.exists())

    def test_file_investigation_beyond_twelve_steps_and_repeat_only_warns_once(self):
        calls = [dict(name='read_text', arguments={'file': './sample.py' if i % 2 else 'sample.py'}) for i in range(16)]
        value = dict(text='synthetic evidence', sha256='a' * 64)
        result, events, receipt, requests = self.scripted_turn(calls, [value] * 16)
        self.assertEqual(result.returncode, 0, result.stderr)
        results = self.tool_results(events)
        self.assertEqual(len(results), 16)
        self.assertEqual(len(requests), 16)
        self.assertEqual(json.dumps(results, ensure_ascii=False).count('未获得新证据'), 1)
        self.assertTrue(receipt.exists())

    def test_new_pages_versions_and_hits_are_not_repeated_evidence(self):
        calls = [dict(name='read_text_range', arguments=dict(file='sample.py', start_line=1, max_lines=1)),
                 dict(name='read_text_range', arguments=dict(file='./sample.py', start_line=2, max_lines=1, expected_version='a' * 64)),
                 dict(name='read_text_range', arguments=dict(file='sample.py', start_line=1, max_lines=1)),
                 dict(name='search_text', arguments=dict(literal='sample', file='sample.py')),
                 dict(name='search_text', arguments=dict(file='./sample.py', literal='sample'))]
        page = dict(text='sample\n', start_line=1, end_line=1, file_version='a' * 64)
        values = [page, dict(page, start_line=2, end_line=2), dict(page, file_version='b' * 64),
                  dict(matches=[1]), dict(matches=[1, 2])]
        result, events, _, _ = self.scripted_turn(calls, values)
        self.assertEqual(result.returncode, 0, result.stderr)
        results = self.tool_results(events)
        self.assertEqual(len(results), 5)
        self.assertNotIn('未获得新证据', json.dumps(results, ensure_ascii=False))

    def test_service_polling_and_preferences_do_not_participate_in_read_detection(self):
        calls = ([dict(name='service_result', arguments={'request_id': 'synthetic-result'})] * 14
                 + [dict(name='preference_view', arguments={})] * 2)
        result, events, _, requests = self.scripted_turn(calls, services=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        results = self.tool_results(events)
        self.assertEqual(len(results), 16)
        self.assertEqual(len(requests), 16)
        self.assertNotIn('未获得新证据', json.dumps(results, ensure_ascii=False))

    def test_action_broker_retains_twelve_step_limit_and_no_success_receipt(self):
        result, events, receipt, requests = self.scripted_turn(
            [dict(name='runtime_status', arguments={})] * 16, broker=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('turn step limit reached', json.dumps(events))
        self.assertEqual(next(e for e in events if e['type'] == 'turn/end')['data']['reason']['kind'], 'error')
        self.assertEqual(len(self.tool_results(events)), 12)
        self.assertFalse(receipt.exists())
        self.assertFalse(requests)  # No action/approval RPC was attempted.

    def test_output_limit_is_uncertain_and_not_replayed_after_restart(self):
        self.fixture('未完整建议', 'max-tokens')
        calls = []

        def supervised(command, **kwargs):
            command = list(command)
            command[-2] = str(self.driver)
            request = Path(command[-1])
            request.write_text(json.dumps(dict(json.loads(request.read_text()), synthetic=True)))
            calls.append(command)
            return run_task(command, **kwargs)

        executor = NativeExecutor(self.root / 'agent', supervised, lambda text: text)
        with mock.patch.dict(os.environ, {'AGENTBRIDGE_AGENT_NODE': self.node, 'AGENTBRIDGE_AGENT_PACKAGES': self.packages}):
            runtime = AgentRuntime(self.root / 'agent', executor, lambda _: str(self.workspace))
            self.addCleanup(runtime.close)
            row = runtime.submit('synthetic-owner', 'private', 'one', '只建议，不修改', 'p')
            import time
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                final = runtime.control('synthetic-owner', 'private', 'agent.status', 'p', row['message_id'])
                if final['state'] not in ('queued', 'running'):
                    break
                time.sleep(.01)
            self.assertEqual(final['state'], 'uncertain')
            self.assertEqual(len(calls), 1)
            self.assertFalse(list((self.root / 'agent/native').glob('receipts/*.json')))
            runtime.close()
            runtime = AgentRuntime(self.root / 'agent', executor, lambda _: str(self.workspace))
            self.addCleanup(runtime.close)
            self.assertEqual(runtime.submit('synthetic-owner', 'private', 'one', '只建议，不修改', 'p')['state'], 'uncertain')
            with self.assertRaises(AgentError):
                runtime.submit('synthetic-owner', 'private', 'two', '继续', 'p')
            self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
