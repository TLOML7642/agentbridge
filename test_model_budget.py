"""Paid probe meter: synthetic fetches only, private cross-process ledger."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


class BudgetTests(unittest.TestCase):
    def test_native_provider_is_metered_and_http_error_never_retries(self):
        node = os.environ['AGENTBRIDGE_TEST_NATIVE_NODE']
        packages = os.environ['AGENTBRIDGE_TEST_NATIVE_PACKAGES']
        with tempfile.TemporaryDirectory(prefix='ab-budget-provider-') as directory:
            root = Path(directory)
            credential = root / 'credential'
            credential.write_text('SERVER_KEY: synthetic-invalid-key\n')
            credential.chmod(0o600)
            fixture = root / 'fetch.mjs'
            fixture.write_text('''
import { appendFile } from 'node:fs/promises';
globalThis.fetch = async () => {
  await appendFile(process.env.SYNTHETIC_COUNTER, '1\\n');
  if (process.env.SYNTHETIC_HTTP_FAILURE === '1') return new Response('', { status: 402 });
  const row = { choices: [{ delta: { content: 'SYNTHETIC_PROVIDER_OK' }, finish_reason: 'stop' }],
    usage: { prompt_tokens: 100, completion_tokens: 10, total_tokens: 110 } };
  return new Response('data: ' + JSON.stringify(row) + '\\n\\ndata: [DONE]\\n\\n',
    { headers: { 'content-type': 'text/event-stream' } });
};
''')
            for failed in (False, True):
                lane = root / str(failed)
                lane.mkdir()
                ledger = lane / 'ledger'
                env = {'PATH': '/usr/bin:/bin', 'HOME': directory, 'ELECTRON_RUN_AS_NODE': '1',
                       'AGENTBRIDGE_AGENT_PACKAGES': packages,
                       'AGENTBRIDGE_AGENT_CREDENTIALS': str(credential),
                       'AGENTBRIDGE_AGENT_CREDENTIAL_REF': 'SERVER_KEY',
                       'AGENTBRIDGE_PAID_PROBE_LEDGER': str(ledger),
                       'SYNTHETIC_COUNTER': str(lane / 'calls'), 'SYNTHETIC_HTTP_FAILURE': str(int(failed))}
                init = subprocess.run([node, 'model_budget.mjs', 'init', str(ledger)], env=env,
                                      capture_output=True, text=True, timeout=10)
                self.assertEqual(init.returncode, 0, init.stderr)
                session, mid = uuid.uuid4().hex, uuid.uuid4().hex
                request = {'id': mid, 'session': session, 'root': str(lane / 'native'),
                           'text': 'synthetic provider fixture', 'resume': False, 'read_mode': 'none',
                           'subject': {'sender': 'synthetic', 'channel': 'synthetic', 'conversation': session,
                                       'session': session, 'project': 'p', 'cwd': directory, 'attempt': mid}}
                file = lane / 'request.json'
                file.write_text(json.dumps(request))
                result = subprocess.run([node, '--import', str(fixture), 'agent_driver.mjs', str(file)],
                                        env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual((lane / 'calls').read_text(), '1\n')
                rows = [json.loads(line) for line in ledger.read_text().splitlines()]
                self.assertEqual(rows[-1]['kind'], 'stop' if failed else 'settle')
                if failed:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(rows[-1]['http_status'], 402)
                    self.assertFalse((lane / 'native/receipts' / (mid + '.json')).exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['reply'], 'SYNTHETIC_PROVIDER_OK')
                    self.assertEqual(rows[-1]['amount'], 280)

    def test_persistent_fail_closed_meter(self):
        node = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE')
        if not node:
            self.fail('explicit AGENTBRIDGE_TEST_NATIVE_NODE required')
        with tempfile.TemporaryDirectory(prefix='ab-budget-') as directory:
            script = r'''
import assert from 'node:assert/strict';
import { writeFile, appendFile, chmod, symlink } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { initializeBudget, createBudgetFetch, budgetStatus, POLICY } from './model_budget.mjs';
const root = process.argv[1];
let calls = 0, scenarios = 0;
const url = 'https://api.deepseek.com/chat/completions';
const init = { method: 'POST', body: JSON.stringify({ model: POLICY.model, max_tokens: 4096,
  thinking: { type: 'enabled' }, reasoning_effort: 'high', stream: true,
  stream_options: { include_usage: true }, messages: [{ role: 'user', content: 'synthetic' }] }) };
const clock = () => Date.parse('2026-11-09T03:00:00Z');
const response = (usage = { prompt_tokens: 100, completion_tokens: 10,
  prompt_cache_hit_tokens: 50, prompt_cache_miss_tokens: 50, total_tokens: 110 }) => new Response(
  'data: ' + JSON.stringify({ choices: [{ finish_reason: 'stop' }], usage }) + '\n\ndata: [DONE]\n\n',
  { headers: { 'content-type': 'text/event-stream' } });
const fake = async () => { calls++; return response(); };
const fresh = async name => { const file = root + '/' + name; await initializeBudget(file); return file; };
const run = (file, fetch = fake) => createBudgetFetch(file, fetch, clock)(url, init);
let file = await fresh('eight');
await assert.rejects(initializeBudget(file)); scenarios++;
for (let i = 0; i < 8; i++) await run(file);
assert.deepEqual(await budgetStatus(file), { count: 8, used: 8 * 182, pending: false, stopped: false });
const child = spawnSync(process.execPath, ['--input-type=module', '-e', `
import { createBudgetFetch } from './model_budget.mjs';
try { await createBudgetFetch(${JSON.stringify(file)}, () => { process.exit(4); }, () => ${clock()})(
  ${JSON.stringify(url)}, ${JSON.stringify(init)}); process.exit(5); } catch { process.exit(0); }
`], { cwd: process.cwd() });
assert.equal(child.status, 0); assert.equal(calls, 8); scenarios++;
for (const [name, backend] of [
  ['http', async () => new Response('', { status: 402 })],
  ['transport', async () => { throw Error('synthetic secret-bearing error'); }],
  ['no-usage', async () => response(null)],
  ['bad-usage', async () => response({ prompt_tokens: -1, completion_tokens: 1 })],
  ['cache-conflict', async () => response({ prompt_tokens: 10, completion_tokens: 1,
    prompt_cache_hit_tokens: 2, prompt_cache_miss_tokens: 9 })],
  ['too-many-tokens', async () => response({ prompt_tokens: 1, completion_tokens: 4097 })],
  ['torn-stream', async () => new Response('data: {}\n\n', { headers: { 'content-type': 'text/event-stream' } })],
]) {
  file = await fresh(name);
  await assert.rejects(run(file, backend), /PAID_PROBE_STOPPED_RESERVATION_RETAINED/);
  assert.deepEqual(await budgetStatus(file), { used: 2129920, count: 1, pending: true, stopped: true });
  await assert.rejects(run(file)); scenarios++;
}
file = await fresh('balance');
await run(file, async () => response({ prompt_tokens: 1000000, completion_tokens: 4096 }));
assert.equal((await budgetStatus(file)).used, 2032768);
await assert.rejects(run(file)); scenarios++;
file = await fresh('pending');
await appendFile(file, JSON.stringify({ kind: 'reserve', number: 1, amount: 2129920 }) + '\n');
await assert.rejects(run(file)); scenarios++;
file = await fresh('concurrent');
let entered, release;
const ready = new Promise(resolve => entered = resolve);
const hold = new Promise(resolve => release = resolve);
const first = run(file, async () => { entered(); await hold; return response(); });
await ready; await assert.rejects(run(file)); release(); await first;
assert.equal((await budgetStatus(file)).count, 1); scenarios++;
file = await fresh('invalid');
for (const target of ['https://example.invalid/chat/completions', 'https://api.deepseek.com/files'])
  await assert.rejects(createBudgetFetch(file, fake, clock)(target, init));
for (const patch of [{ max_tokens: 128 }, { model: 'other' }, { reasoning_effort: 'low' },
  { tools: [{ type: 'web_search' }] }, { messages: [{ content: [{ type: 'image_url' }] }] }])
  await assert.rejects(createBudgetFetch(file, fake, clock)(url, { ...init,
    body: JSON.stringify({ ...JSON.parse(init.body), ...patch }) }));
assert.equal((await budgetStatus(file)).count, 0); scenarios++;
await assert.rejects(createBudgetFetch(file, fake, () => Date.parse('2026-11-11'))(url, init)); scenarios++;
await appendFile(file, 'torn'); await assert.rejects(run(file)); scenarios++;
file = await fresh('permissions'); await chmod(file, 0o644); await assert.rejects(run(file)); scenarios++;
await symlink(file, root + '/linked'); await assert.rejects(run(root + '/linked')); scenarios++;
console.log(JSON.stringify({ scenarios, real_requests: 0 }));
'''
            result = subprocess.run([node, '--input-type=module', '-e', script, directory],
                                    cwd=Path(__file__).resolve().parent, capture_output=True,
                                    text=True, timeout=30,
                                    env={'PATH': '/usr/bin:/bin', 'HOME': directory,
                                         'ELECTRON_RUN_AS_NODE': '1'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"scenarios":17', result.stdout)


if __name__ == '__main__':
    unittest.main()
