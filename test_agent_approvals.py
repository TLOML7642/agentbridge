"""Synthetic durable approvals, no production data or executor commands."""
import concurrent.futures
import contextlib
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from agent_approvals import ApprovalAuthority, fingerprint
from agent_runtime import AgentError


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='agent-approval-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.now = 1700000000.0
        self.authority = self.make()
        self.subject = dict(sender='owner', channel='private', conversation='original', session='task-session',
                            project='p', cwd=str(self.workspace), attempt=uuid.uuid4().hex)
        self.authority.register(self.subject)
        self.request = self.envelope()

    def make(self):
        authority = ApprovalAuthority(self.root / 'agent', tools=('synthetic_write',), enabled=True, clock=lambda: self.now)
        self.addCleanup(authority.close)
        return authority

    def envelope(self, **fields):
        value = dict(version=1, epoch=uuid.uuid4().hex, execution=uuid.uuid4().hex, subject=copy.deepcopy(self.subject),
                     tool='synthetic_write', arguments={'target': 'synthetic.txt', 'text': 'synthetic content'},
                     callId=uuid.uuid4().hex, rootCallId='synthetic-root', nested=False)
        value.update(fields)
        value['digest'] = fingerprint(value)
        return value

    def pending(self, request=None):
        return self.authority.request(self.subject['attempt'], request or self.request)

    def decide(self, pending, allow=True, **kwargs):
        values = dict(sender='owner', channel='private', conversation='original', project='p',
                      identifier=pending['approval_id'], delivery='decision', allow=allow)
        values.update(kwargs)
        return self.authority.decide(**values)

    def test_pending_durable_and_duplicate_request_stable(self):
        first = self.pending()
        self.assertEqual(first['state'], 'pending')
        self.assertIsNone(first['grant'])
        self.assertIsNone(first['reason'])
        self.assertEqual(first, self.pending())
        self.assertEqual(self.authority.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.authority.root.stat().st_mode & 0o777, 0o700)

    def test_approved_exact_action_consumed_once_under_concurrency(self):
        grant = self.decide(self.pending())['grant']
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            consumed = list(pool.map(lambda _: self.authority.consume(self.subject['attempt'], self.request, grant), range(20)))
        self.assertEqual(sum(consumed), 1)
        self.assertEqual(self.pending()['state'], 'consumed')

    def test_source_conversation_project_cannot_authorize_or_inspect(self):
        pending = self.pending()
        for field in ('sender', 'channel', 'conversation', 'project'):
            with self.subTest(field=field), self.assertRaises(AgentError):
                self.decide(pending, **{field: 'foreign'})
        self.assertEqual(self.pending()['state'], 'pending')
        with self.assertRaises(AgentError):
            self.authority.inspect('foreign', 'private', 'original', 'p', pending['approval_id'])

    def test_actual_parameters_and_identity_are_bound(self):
        pending = self.pending()
        grant = self.decide(pending)['grant']
        for key, value in [('arguments', {'target': 'different'}), ('tool', 'shell'), ('nested', True),
                           ('subject', {**self.subject, 'sender': 'foreign'}), ('callId', 'different')]:
            altered = copy.deepcopy(self.request)
            altered[key] = value
            altered['digest'] = fingerprint({k: v for k, v in altered.items() if k != 'digest'})
            with self.subTest(key=key):
                try:
                    result = self.authority.consume(self.subject['attempt'], altered, grant)
                except AgentError:
                    result = False
                self.assertFalse(result)
        self.assertTrue(self.authority.consume(self.subject['attempt'], self.request, grant))

    def test_call_id_cannot_change_parameters_or_epoch(self):
        self.pending()
        for key, value in [('arguments', {'target': 'changed'}), ('epoch', uuid.uuid4().hex)]:
            changed = {k: v for k, v in self.request.items() if k != 'digest'}
            changed[key] = value
            changed['digest'] = fingerprint(changed)
            with self.assertRaises(AgentError):
                self.pending(changed)

    def test_forged_missing_or_old_grants_denied(self):
        pending = self.pending()
        grant = self.decide(pending)['grant']
        for key, value in [('approvalId', 'old'), ('digest', 'different'), ('epoch', 'old'), ('expiresAt', self.now * 1000 + 999999)]:
            self.assertFalse(self.authority.consume(self.subject['attempt'], self.request, {**grant, key: value}))
        for changed in (None, {}, {**grant, 'extra': 'field'}):
            self.assertFalse(self.authority.consume(self.subject['attempt'], self.request, changed))

    def test_denied_revoked_and_expired_never_consume(self):
        pending = self.pending()
        self.decide(pending, allow=False)
        self.assertEqual(self.pending()['state'], 'denied')
        self.assertEqual(self.pending()['reason'], 'user_denied')
        with self.assertRaises(AgentError):
            self.decide(pending, delivery='new-decision')
        second = self.envelope()
        row = self.pending(second)
        grant = self.decide(row, delivery='second')['grant']
        self.now += 241
        self.assertFalse(self.authority.consume(self.subject['attempt'], second, grant))
        self.assertEqual(self.pending(second)['state'], 'expired')
        self.assertEqual(self.pending(second)['reason'], 'approval_expired')

    def test_clock_rollback_expires_pending_approval(self):
        self.pending()
        self.now -= 1
        self.assertEqual(self.pending()['state'], 'expired')

    def test_decision_delivery_is_single_action_and_replay_cannot_regrant(self):
        pending = self.pending()
        grant = self.decide(pending)['grant']
        self.authority.consume(self.subject['attempt'], self.request, grant)
        duplicate = self.decide(pending)
        self.assertTrue(duplicate['reused'])
        self.assertEqual(duplicate['state'], 'consumed')
        with self.assertRaises(AgentError):
            self.decide(pending, allow=False)

    def test_shutdown_and_restart_revoke_unconsumed_grants(self):
        pending = self.pending()
        grant = self.decide(pending)['grant']
        self.authority.close()
        self.authority = self.make()
        row = self.authority.inspect('owner', 'private', 'original', 'p', pending['approval_id'])
        self.assertEqual(row['state'], 'revoked')
        self.assertEqual(row['reason'], 'approval_revoked')
        with self.assertRaises(AgentError):
            self.authority.consume(self.subject['attempt'], self.request, grant)
        with self.assertRaises(AgentError):
            self.authority.register(self.subject)

    def test_crash_boot_invalidates_pending_without_resetting_consumed(self):
        one = self.pending()
        grant = self.decide(one)['grant']
        self.authority.consume(self.subject['attempt'], self.request, grant)
        second = self.pending(self.envelope())
        # Simulate process death without invoking orderly revocation.
        self.authority.lease.close()
        self.authority.closed = True
        self.authority = self.make()
        self.assertEqual(self.authority.inspect('owner', 'private', 'original', 'p', one['approval_id'])['state'], 'consumed')
        self.assertEqual(self.authority.inspect('owner', 'private', 'original', 'p', second['approval_id'])['state'], 'revoked')

    def test_cancel_and_directory_replacement_rechecked_before_consumption(self):
        grant = self.decide(self.pending())['grant']
        self.workspace.rename(self.root / 'prior-workspace')
        self.workspace.mkdir()
        with self.assertRaises(AgentError):
            self.authority.consume(self.subject['attempt'], self.request, grant)
        self.authority.revoke(self.subject['attempt'])
        with self.assertRaises(AgentError):
            self.pending()

    def test_default_off_unregistered_tools_nested_and_malformed_refused(self):
        self.authority.enabled = False
        with self.assertRaises(AgentError):
            self.pending()
        self.authority.enabled = True
        for changed in [self.envelope(tool='shell'), self.envelope(nested=True), self.envelope(arguments={'target': 42}),
                        {**self.request, 'digest': 'forged'}, {**self.request, 'extra': 'field'}]:
            with self.assertRaises(AgentError):
                self.pending(changed)

    def test_single_host_lease_prevents_competing_authorities(self):
        with self.assertRaises(BlockingIOError):
            self.make()


class NativeApprovalTests(unittest.TestCase):
    SCRIPT = r'''
setTimeout(() => process.exit(99), 10000).unref();
import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';
const load = (name) => import(pathToFileURL(process.env.AGENTBRIDGE_TEST_NATIVE_PACKAGES + '/' + name + '/lib/index.js'));
const { Context } = await load('cordis');
const { default: Tools, defineTool } = await load('dsh-tools');
const { default: Agents } = await load('dsh-agent');
const { default: Sessions } = await load('dsh-session');
const { default: Prompt } = await load('dsh-system-prompt');
const { default: Llm } = await load('dsh-llm');
const { default: Loop } = await load('dsh-agent-loop');
const { installToolBoundary } = await import(pathToFileURL(process.env.BOUNDARY_SOURCE));
const subject = JSON.parse(process.env.SYNTHETIC_SUBJECT);
const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
async function rpc(op, request, grant) {
  process.stdout.write(JSON.stringify({op, request, grant}) + '\n');
  const line = await lines.next();
  if (line.done) throw new Error('host disconnected');
  return JSON.parse(line.value);
}
const ctx = new Context();
new Prompt(ctx, {persona:'synthetic', includeHarnessIdentity:false});
new Tools(ctx, { mode: 'native' }); new Sessions(ctx); new Agents(ctx); new Llm(ctx);
new Loop(ctx, {agents:[], maxParallelToolCalls:1});
const boundary = installToolBoundary(ctx, { writesEnabled: true,
  requestApproval: (request) => rpc('request', request),
  consumeApproval: (request, grant) => rpc('consume', request, grant) });
let calls = 0;
boundary.register(defineTool({ name: 'synthetic_write', description: 'Synthetic counter only.',
  parameters: { target: { type: 'string' } },
  output: { schema: { type: 'string' }, render: (_args, value) => [{type:'text',text:value}] },
  execute: () => { calls++; return 'synthetic'; } }), {effect:'write'});
const handle = await ctx.agents.create({ sessionId: subject.session,
  agentOptions: {provider:'synthetic', model:'synthetic'}, meta: {cwd:subject.cwd},
  setup: (scope) => boundary.bindAgent(scope.agent, subject) });
boundary.seal();
const result = await ctx.tools.execute({ name:'synthetic_write', arguments:{target:'synthetic-only'},
  agent:handle.agent, callId:'native-call', signal:new AbortController().signal });
process.stdout.write(JSON.stringify({op:'result',calls,denied:result.isError === true})+'\n');
await handle.dispose(); await ctx.fiber.dispose(); process.stdin.destroy();
'''

    def test_persistent_host_roundtrip_through_actual_native_guard(self):
        node, packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE'), os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not node or not packages:
            self.skipTest('explicit pinned native test runtime not supplied')
        for allow in (False, True):
            with self.subTest(allow=allow), tempfile.TemporaryDirectory(prefix='native-approval-test-') as scratch, contextlib.ExitStack() as stack:
                root = Path(scratch).resolve()
                subject = dict(sender='synthetic-owner', channel='synthetic-private', conversation='synthetic-conversation',
                               project='synthetic-project', cwd=str(root), session=uuid.uuid4().hex, attempt=uuid.uuid4().hex)
                authority = ApprovalAuthority(root / 'agent', tools=('synthetic_write',), enabled=True)
                stack.callback(authority.close)
                authority.register(subject)
                script = root / 'synthetic.mjs'
                script.write_text(self.SCRIPT)
                env = {'HOME': scratch, 'DSH_HOME': scratch, 'PATH': '/usr/bin:/bin', 'ELECTRON_RUN_AS_NODE': '1',
                       'AGENTBRIDGE_TEST_NATIVE_PACKAGES': packages, 'SYNTHETIC_SUBJECT': json.dumps(subject),
                       'BOUNDARY_SOURCE': str(Path(__file__).with_name('native_boundary.mjs'))}
                proc = subprocess.Popen([node, str(script)], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                try:
                    # readline is bounded by the child watchdog and test process timeout.
                    # No real model, shell command or filesystem tool is mounted.
                    line = proc.stdout.readline()
                    self.assertTrue(line, proc.stderr.read() if proc.poll() is not None else 'native driver returned no request')
                    first = json.loads(line)
                    self.assertEqual(first['op'], 'request')
                    pending = authority.request(subject['attempt'], first['request'])
                    decided = authority.decide(subject['sender'], subject['channel'], subject['conversation'], subject['project'],
                                               pending['approval_id'], 'synthetic-decision', allow)
                    proc.stdin.write(json.dumps(decided['grant']) + '\n'); proc.stdin.flush()
                    value = json.loads(proc.stdout.readline())
                    if allow:
                        self.assertEqual(value['op'], 'consume')
                        self.assertTrue(authority.consume(subject['attempt'], value['request'], value['grant']))
                        self.assertFalse(authority.consume(subject['attempt'], value['request'], value['grant']))
                        proc.stdin.write('true\n'); proc.stdin.flush()
                        value = json.loads(proc.stdout.readline())
                    self.assertEqual(value, {'op': 'result', 'calls': int(allow), 'denied': not allow})
                    self.assertEqual(proc.wait(10), 0, proc.stderr.read())
                finally:
                    if proc.poll() is None:
                        proc.kill(); proc.wait()
                    proc.stdin.close(); proc.stdout.close(); proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
