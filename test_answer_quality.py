"""Production-cap native reply tests, separate from prompt observation/model QA."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

from agent_runtime import AgentError, AgentRuntime, NativeExecutor
from process_supervisor import run_task
from tools.check_answer_quality import questions
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
            self.assertEqual(len(payloads), 13)
            self.assertEqual(len({p['session'] for p in payloads}), 13)
            for payload in payloads:
                self.assertFalse(payload['resume'])
                for key in ('synthetic', 'services', 'broker', 'remote_reads'):
                    self.assertNotIn(key, payload)
            observations = json.loads((report / 'observations.json').read_text())
            self.assertTrue(all(not row['completed_receipt'] and row['quality'] == 'pending manual review' for row in observations))
            self.assertTrue(all((report / row['label'] / 'process.json').is_file() for row in observations))


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
        for name in ('agent_driver.mjs', 'agent_persona.mjs', 'agent_rpc.mjs', 'native_boundary.mjs'):
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
