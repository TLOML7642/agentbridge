/** Execute exactly one host-frozen native tool call, without a chat model. */
import assert from 'node:assert/strict';
import { open, readFile, mkdir, link, unlink } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import path from 'node:path';
import os from 'node:os';
import { pathToFileURL } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import { installToolBoundary } from './native_boundary.mjs';
import { rpc } from './agent_rpc.mjs';

process.umask(0o077);
let ctx, handle, boundary, timer;
let entered = false;
let request;
let receipt;
let approvalEvidence = { reason: 'boundary_denied' };
let approvalRequest;
const stop = new AbortController();
for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => {
  stop.abort(); boundary?.revoke(); handle?.agent.cancel({ kind: 'user' });
});

async function action(args, execution) {
  entered = true;
  const home = path.join(request.root, 'empty-home');
  await mkdir(home, { recursive: true, mode: 0o700 });
  const input = { root: execution.cwd, tool: request.tool, arguments: args, home,
    private_prefixes: request.private_prefixes };
  const result = await new Promise((resolve, reject) => {
    const script = path.join(import.meta.dirname, 'agent_actions.py');
    const code = 'import sys,runpy;sys.path.insert(0,sys.argv[1]);runpy.run_path(sys.argv[2],run_name="__main__")';
    const child = spawn(request.python, ['-I', '-c', code, import.meta.dirname, script],
      { stdio: ['pipe', 'pipe', 'ignore'], signal: execution.signal, env: { PATH: '/usr/bin:/bin' } });
    const output = [];
    let bytes = 0;
    child.on('error', reject);
    child.stdin.on('error', () => {});
    child.stdout.on('data', (chunk) => {
      bytes += chunk.length;
      if (bytes <= 131072) output.push(chunk); else child.kill('SIGKILL');
    });
    child.once('close', (code) => {
      if (code !== 0 || bytes > 131072) reject(new Error('ACTION_OUTCOME_UNKNOWN'));
      else { try { resolve(JSON.parse(Buffer.concat(output).toString('utf8'))); } catch (error) { reject(error); } }
    });
    child.stdin.end(JSON.stringify(input));
  });
  assert.equal(result.ok, true);
  assert.ok(['verified', 'process-ended'].includes(result.result.completion));
  receipt = result.result;
  return JSON.stringify(result.result);
}

async function main() {
  request = JSON.parse(await readFile(process.argv[2], 'utf8'));
  assert.match(request.id, /^[a-f0-9]{32}$/);
  assert.match(request.session, /^[a-f0-9]{32}$/);
  assert.equal(request.subject.attempt, request.id);
  assert.equal(request.subject.session, request.session);
  assert.ok(['create_text', 'make_directory', 'run_command'].includes(request.tool));
  assert.ok(Object.values(request.arguments).every((value) => typeof value === 'string'));
  process.env.DSH_HOME = request.root;
  const packages = process.env.AGENTBRIDGE_AGENT_PACKAGES ?? path.join(os.homedir(),
    '.deepseek-harness/plugins/packages/dsh-0.1.1-rc.2/node_modules/@deepseek-ai');
  const load = (name) => import(pathToFileURL(path.join(packages, name, 'lib/index.js')));
  const { Context } = await load('cordis');
  const { default: Tools, defineTool } = await load('dsh-tools');
  const { default: Prompt } = await load('dsh-system-prompt');
  const { default: Sessions } = await load('dsh-session');
  const { default: Agents } = await load('dsh-agent');
  const { default: Llm } = await load('dsh-llm');
  const { default: Loop } = await load('dsh-agent-loop');
  ctx = new Context();
  new Prompt(ctx, { persona: '', includeHarnessIdentity: false });
  new Tools(ctx, { mode: 'native' }); new Sessions(ctx); new Agents(ctx); new Llm(ctx);
  new Loop(ctx, { agents: [], maxParallelToolCalls: 1 });
  boundary = installToolBoundary(ctx, { writesEnabled: true,
    requestApproval: async (envelope, signal) => {
      approvalRequest = envelope;
      while (true) {
        approvalEvidence = { reason: 'approval_unavailable' };
        const result = await rpc(request.broker, 'approval', { request: envelope }, signal);
        approvalEvidence = { approval_id: result.approval_id, approval_state: result.state,
          reason: result.reason ?? 'approval_not_consumed' };
        if (result.state === 'approved') return result.grant;
        if (result.state !== 'pending') return null;
        await sleep(300, undefined, { signal });
      }
    },
    consumeApproval: async (envelope, grant, signal) => {
      approvalEvidence = { reason: 'approval_unavailable' };
      const result = await rpc(request.broker, 'consume', { request: envelope, grant }, signal);
      if (result.consumed === true) {
        approvalEvidence = { approval_id: grant.approvalId, approval_state: 'consumed', reason: 'boundary_denied' };
        return true;
      }
      // Re-read the same call, never create or retry an action after a failed consume.
      const current = await rpc(request.broker, 'approval', { request: envelope }, signal);
      approvalEvidence = { approval_id: current.approval_id, approval_state: current.state,
        reason: current.reason ?? 'approval_not_consumed' };
      return false;
    } });
  const parameters = Object.fromEntries(Object.keys(request.arguments).map((key) => [key, { type: 'string', required: true }]));
  boundary.register(defineTool({ name: request.tool, description: 'One host-frozen, individually approved background action.',
    parameters, output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    execute: action }), { effect: 'write' });
  handle = await ctx.agents.create({ sessionId: request.session, agentOptions: { provider: 'none', model: 'none' },
    meta: { cwd: request.subject.cwd }, setup: (scope) => boundary.bindAgent(scope.agent, request.subject) });
  boundary.seal();
  const parent = process.ppid;
  timer = setInterval(() => {
    if (process.ppid !== parent) { stop.abort(); boundary.revoke(); handle.agent.cancel({ kind: 'user' }); }
  }, 250);
  const result = await ctx.tools.execute({ name: request.tool, arguments: request.arguments,
    callId: request.id, agent: handle.agent, signal: stop.signal });
  if (result.isError) {
    assert.equal(entered, false, 'an entered action without a receipt is uncertain');
    // A grant may expire in the native guard before consumeApproval is called.
    // Refresh only an already registered request, without retrying the action.
    if (approvalRequest && approvalEvidence.approval_state === 'approved') {
      try {
        const current = await rpc(request.broker, 'approval', { request: approvalRequest }, stop.signal);
        approvalEvidence = { approval_id: current.approval_id, approval_state: current.state,
          reason: current.reason ?? 'boundary_denied' };
      } catch { approvalEvidence = { reason: 'approval_unavailable' }; }
    }
    receipt = { completion: 'denied', evidence: 'tool_body_not_entered', ...approvalEvidence };
  }
  assert.ok(receipt);
  return { id: request.id, session: request.session, subject: request.subject, digest: request.digest, result: receipt };
}

let result;
try { result = await main(); }
catch { process.stderr.write('TASK_OUTCOME_UNKNOWN\n'); process.exitCode = 1; }
finally {
  clearInterval(timer);
  try { if (handle) await handle.dispose(); if (ctx) await ctx.fiber.dispose(); }
  catch { process.exitCode = 1; }
}
if (!process.exitCode && result) {
  try {
    const directory = path.join(request.root, 'receipts');
    await mkdir(directory, { recursive: true, mode: 0o700 });
    const temporary = path.join(directory, request.id + '.tmp');
    const output = await open(temporary, 'wx', 0o600);
    try { await output.writeFile(JSON.stringify(result)); await output.sync(); } finally { await output.close(); }
    await link(temporary, path.join(directory, request.id + '.json'));
    await unlink(temporary);
    const descriptor = await open(directory, 'r');
    try { await descriptor.sync(); } finally { await descriptor.close(); }
    process.stdout.write(JSON.stringify(result));
  } catch { process.stderr.write('TASK_RECEIPT_FAILED\n'); process.exitCode = 1; }
}
