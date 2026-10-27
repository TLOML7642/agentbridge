/** Stage A only: exercise installed DSH with synthetic tools/model and private scratch state. */
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { promisify } from 'node:util';

const packages = process.env.AGENTBRIDGE_PROBE_PACKAGES;
assert.ok(packages && path.isAbsolute(packages), 'explicit installed @deepseek-ai package root required');
const load = (name) => import(pathToFileURL(path.join(packages, name, 'lib/index.js')).href);
const { Context } = await load('cordis');
const { default: Agents, assembleContextFor } = await load('dsh-agent');
const { default: Loop } = await load('dsh-agent-loop');
const { default: Sessions } = await load('dsh-session');
const { default: Persistence } = await load('dsh-session-persistence-jsonl');
const { default: Prompt, renderPrompt } = await load('dsh-system-prompt');
const { default: Tools, defineTool } = await load('dsh-tools');
const { default: Llm, createUserMessage } = await load('dsh-llm');
const persona = await load('dsh-persona');

const scratch = await mkdtemp(path.join(os.tmpdir(), 'agentbridge-native-probe-'));
const results = [];
const contexts = [];
const evidence = [];
let gatePassed = true;
const initialUmask = process.umask(0o077);

async function check(name, operation) {
  try {
    await operation();
    results.push({ name, passed: true });
  } catch (error) {
    gatePassed = false;
    results.push({ name, passed: false, error: error.message });
  }
}

async function* reply() {
  yield { type: 'block-start', index: 0, blockType: 'text' };
  yield { type: 'text-delta', index: 0, text: 'synthetic reply' };
  yield { type: 'block-end', index: 0, block: { type: 'text', text: 'synthetic reply' } };
  yield { type: 'finish', reason: { kind: 'stop' } };
}

function runtime(root, model = reply) {
  const ctx = new Context();
  contexts.push(ctx);
  new Prompt(ctx, { persona: 'global synthetic persona', includeHarnessIdentity: false });
  new Tools(ctx, { mode: 'native' });
  new Sessions(ctx);
  new Agents(ctx);
  new Llm(ctx);
  // Intercept all model calls; no provider, credential, network, shell or FS tool is loaded.
  ctx.on('llm/stream', model);
  new Persistence(ctx, { root, compression: 'none' });
  new Loop(ctx, { agents: [], maxParallelToolCalls: 1 });
  return ctx;
}

function register(ctx, name, body) {
  return ctx.tools.register(defineTool({
    name,
    description: 'Synthetic counter only; never performs the named action.',
    parameters: { target: { type: 'string' } },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    execute: body,
  }));
}

const input = (name, args = {}, extra = {}) => ({
  name, arguments: args, callId: 'synthetic-call', signal: new AbortController().signal, ...extra,
});
const options = { provider: 'synthetic-only', model: 'synthetic-only' };
const message = (text) => createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user' } });

function toolModel(name, args) {
  let first = true;
  return async function* () {
    if (!first) { yield* reply(); return; }
    first = false;
    yield { type: 'block-start', index: 0, blockType: 'tool-call' };
    yield { type: 'block-end', index: 0, block: {
      type: 'tool-call', id: 'synthetic-model-call', name, arguments: JSON.stringify(args),
    } };
    yield { type: 'finish', reason: { kind: 'tool-calls' } };
  };
}

async function childPhase(phase, root) {
  assert.ok(['write', 'read'].includes(phase));
  const parent = path.dirname(root);
  assert.ok(path.basename(parent).startsWith('agentbridge-native-probe-'));
  assert.equal(path.dirname(parent), path.resolve(os.tmpdir()));
  assert.equal(path.basename(root), 'process-sessions');
  const ctx = runtime(root);
  const resumed = phase === 'read';
  const handle = resumed
    ? await ctx.agents.resume({ resumeSessionId: 'synthetic-process', agentOptions: options })
    : await ctx.agents.create({ sessionId: 'synthetic-process', agentOptions: options });
  if (resumed) {
    assert.equal(handle.agent.session.events.filter((e) => e.type === 'assistant/message').length, 1);
  }
  handle.agent.followup(message(`synthetic ${phase} turn`));
  await handle.agent.whenIdle();
  await ctx.sessions.flush(handle.agent.session);
  assert.equal(handle.agent.session.events.filter((e) => e.type === 'assistant/message').length, resumed ? 2 : 1);
  await handle.dispose();
}

async function runChecks() {
  await check('session_survives_process_exit', async () => {
    for (const phase of ['write', 'read']) {
      const result = await promisify(execFile)(process.execPath, [fileURLToPath(import.meta.url),
        '--child', phase, path.join(scratch, 'process-sessions')], {
        env: { PATH: process.env.PATH, ELECTRON_RUN_AS_NODE: process.env.ELECTRON_RUN_AS_NODE,
          AGENTBRIDGE_PROBE_PACKAGES: packages, TMPDIR: os.tmpdir(), HOME: scratch, DSH_HOME: scratch },
        timeout: 15000, maxBuffer: 65536,
      });
      assert.equal(result.stderr, '');
    }
  });

  await check('create_flush_dispose_resume', async () => {
    const root = path.join(scratch, 'persistence');
    const first = runtime(root);
    const handle = await first.agents.create({ sessionId: 'synthetic-resume', agentOptions: options, meta: { cwd: scratch } });
    handle.agent.followup(message('synthetic first turn'));
    await handle.agent.whenIdle();
    assert.equal(handle.agent.session.events.filter((e) => e.type === 'assistant/message').length, 1);
    assert.equal(await first.sessions.flush(handle.agent.session), true);
    const count = handle.agent.session.events.length;
    await handle.dispose();
    const second = runtime(root);
    const resumed = await second.agents.resume({ resumeSessionId: 'synthetic-resume', agentOptions: options });
    assert.ok(resumed.agent.session.events.length >= count);
    assert.ok(resumed.agent.session.events.some((e) => e.type === 'assistant/message'));
    resumed.agent.followup(message('synthetic second turn'));
    await resumed.agent.whenIdle();
    assert.equal(resumed.agent.session.events.filter((e) => e.type === 'assistant/message').length, 2);
    await second.sessions.flush(resumed.agent.session);
    await resumed.dispose();
  });

  await check('persona_is_agent_scoped', async () => {
    const ctx = runtime(path.join(scratch, 'persona'));
    const one = await ctx.agents.create({ sessionId: 'persona-one', agentOptions: options,
      setup: (scoped) => { persona.apply(scoped, { text: 'synthetic shared persona' }); } });
    const two = await ctx.agents.create({ sessionId: 'persona-two', agentOptions: options });
    const prompt = async (agent) => renderPrompt(await ctx.systemPrompt.assemble(assembleContextFor(agent, new AbortController().signal)));
    assert.match(await prompt(one.agent), /synthetic shared persona/);
    assert.doesNotMatch(await prompt(two.agent), /synthetic shared persona/);
    await one.dispose();
    await two.dispose();
  });

  await check('pre_execute_denial_reaches_no_body', async () => {
    const ctx = runtime(path.join(scratch, 'deny'));
    let calls = 0;
    register(ctx, 'synthetic_write', () => { calls++; return 'not allowed'; });
    ctx.on('tools/pre-execute', async () => ({ kind: 'deny', reason: 'synthetic policy' }));
    assert.equal((await ctx.tools.execute(input('synthetic_write'))).isError, true);
    assert.equal(calls, 0);
  });

  await check('final_guard_survives_pre_execute_allow', async () => {
    const ctx = runtime(path.join(scratch, 'guard'));
    let calls = 0;
    register(ctx, 'synthetic_write', () => { calls++; return 'not allowed'; });
    ctx.on('tools/pre-execute', async () => ({ kind: 'allow' }));
    ctx.tools.guard(() => 'synthetic final denial');
    assert.equal((await ctx.tools.execute(input('synthetic_write'))).isError, true);
    assert.equal(calls, 0);
  });

  await check('native_mode_rejects_code_and_unregistered_bypasses', async () => {
    const ctx = runtime(path.join(scratch, 'bypasses'));
    for (const name of ['run_code', 'cordis', 'spawn_agent', 'bash', 'synthetic_unknown']) {
      assert.equal((await ctx.tools.execute(input(name))).isError, true, name);
    }
    assert.equal(ctx.get('codeRuntime'), undefined);
    assert.equal(ctx.get('loader'), undefined);
    assert.equal(ctx.get('shell'), undefined);
  });

  await check('nested_dispatch_reenters_guard', async () => {
    const ctx = runtime(path.join(scratch, 'nested'));
    let calls = 0;
    register(ctx, 'synthetic_write', () => { calls++; return 'not allowed'; });
    register(ctx, 'synthetic_nested', async (_args, exec) => {
      const result = await ctx.tools.execute(input('synthetic_write', {}, { parent: exec.token }));
      assert.equal(result.isError, true);
      return 'nested denied';
    });
    ctx.tools.guard((exec) => exec.name === 'synthetic_write' ? 'no synthetic writes' : undefined);
    assert.notEqual((await ctx.tools.execute(input('synthetic_nested'))).isError, true);
    assert.equal(calls, 0);
  });

  await check('cancel_before_dispatch_and_resume_without_replay', async () => {
    const root = path.join(scratch, 'cancel');
    const ctx = runtime(root, toolModel('synthetic_write', {}));
    let calls = 0;
    register(ctx, 'synthetic_write', () => { calls++; return 'counter only'; });
    const entered = Promise.withResolvers();
    ctx.on('tools/pre-execute', async (exec) => {
      entered.resolve();
      await new Promise((resolve) => {
        if (exec.signal.aborted) resolve();
        else exec.signal.addEventListener('abort', resolve, { once: true });
      });
      return { kind: 'allow' };
    });
    const handle = await ctx.agents.create({ sessionId: 'synthetic-cancel', agentOptions: options });
    handle.agent.followup(message('synthetic cancel'));
    await entered.promise;
    handle.agent.cancel({ kind: 'user' });
    await handle.agent.whenIdle();
    assert.equal(calls, 0);
    assert.equal(handle.agent.session.events.findLast((e) => e.type === 'turn/end').data.reason.kind, 'aborted');
    await ctx.sessions.flush(handle.agent.session);
    await handle.dispose();
    let resumedModelCalls = 0;
    const next = runtime(root, async function* () { resumedModelCalls++; yield* reply(); });
    const resumed = await next.agents.resume({ resumeSessionId: 'synthetic-cancel', agentOptions: options });
    await resumed.agent.whenIdle();
    assert.equal(resumedModelCalls, 0);
    await resumed.dispose();
  });

  await check('guard_binds_actual_dispatch_parameters', async () => {
    const ctx = runtime(path.join(scratch, 'parameter-binding'),
      toolModel('synthetic_write', { target: 'approved-target' }));
    const executed = [];
    register(ctx, 'synthetic_write', (args) => { executed.push(args.target); return 'counter only'; });
    ctx.tools.guard((exec) => exec.arguments.target === 'approved-target' ? undefined : 'unapproved target');
    ctx.on('tools/execute', async (exec, next) => {
      exec.arguments = { target: 'unapproved-target' };
      return next();
    });
    const handle = await ctx.agents.create({ sessionId: 'synthetic-parameter-binding', agentOptions: options });
    handle.agent.followup(message('synthetic tool request'));
    await handle.agent.whenIdle();
    evidence.push({ case: 'post_guard_parameter_replacement_via_agent_loop', bodyTargets: executed });
    await handle.dispose();
    assert.deepEqual(executed, [], 'unapproved replacement arguments reached synthetic body after native final guard');
  });

  await check('guard_binds_actual_dispatch_tool', async () => {
    const ctx = runtime(path.join(scratch, 'tool-binding'));
    let writes = 0;
    register(ctx, 'synthetic_read', () => 'counter only');
    register(ctx, 'synthetic_write', () => { writes++; return 'counter only'; });
    ctx.tools.guard((exec) => exec.name === 'synthetic_read' ? undefined : 'unapproved tool');
    ctx.on('tools/execute', async (exec, next) => { exec.name = 'synthetic_write'; return next(); });
    const outcome = await ctx.tools.execute(input('synthetic_read'));
    evidence.push({ case: 'post_guard_tool_replacement', syntheticWrites: writes, isError: outcome.isError === true });
    assert.equal(writes, 0, 'unapproved replacement tool reached synthetic body after native final guard');
  });
}

try {
  if (process.argv[2] === '--child') await childPhase(process.argv[3], process.argv[4]);
  else await runChecks();
} finally {
  try {
    for (const ctx of contexts.reverse()) await ctx.fiber.dispose();
  } finally {
    await rm(scratch, { recursive: true, force: true });
    process.umask(initialUmask);
  }
}

if (process.argv[2] === '--child') process.exit(0);
const hashes = {};
for (const name of ['dsh-agent', 'dsh-agent-loop', 'dsh-tools', 'dsh-session-persistence-jsonl', 'dsh-persona']) {
  hashes[name] = createHash('sha256').update(await readFile(path.join(packages, name, 'lib/index.js'))).digest('hex');
}
process.stdout.write(JSON.stringify({ stage: 'A', gatePassed, node: process.version, hashes, results, evidence,
  productionChanged: false, realModelCalls: 0, realToolActions: 0 }, null, 2) + '\n');
process.exitCode = gatePassed ? 0 : 2;
