/** Stage A only: exercise installed DSH with synthetic tools/model and private scratch state. */
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, realpath, rename, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { promisify } from 'node:util';
import { installToolBoundary } from '../native_boundary.mjs';

const hardened = !process.argv.includes('--native-only');

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
const canonicalScratch = await realpath(scratch);
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

function register(ctx, name, body, boundary, effect = 'write') {
  const definition = defineTool({
    name,
    description: 'Synthetic counter only; never performs the named action.',
    parameters: { target: { type: 'string' } },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    execute: body,
  });
  return boundary ? boundary.register(definition, { effect }) : ctx.tools.register(definition);
}

const input = (name, args = {}, extra = {}) => ({
  name, arguments: args, callId: 'synthetic-call', signal: new AbortController().signal, ...extra,
});
const options = { provider: 'synthetic-only', model: 'synthetic-only' };
const message = (text) => createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user' } });

function protectedHarness(ctx, overrides = {}) {
  const requests = [];
  const consumed = new Set();
  const boundary = installToolBoundary(ctx, {
    writesEnabled: true,
    requestApproval: async (request) => {
      requests.push(request);
      return { approvalId: randomUUID(), digest: request.digest, epoch: request.epoch,
        expiresAt: Date.now() + 60000 };
    },
    consumeApproval: async (_request, grant) => {
      if (consumed.has(grant.digest)) return false;
      consumed.add(grant.digest);
      return true;
    },
    ...overrides,
  });
  const subject = (id, cwd = canonicalScratch) => ({ sender: 'synthetic-owner', channel: 'synthetic-source',
    conversation: 'synthetic-conversation', session: id, project: 'synthetic-project',
    cwd, attempt: 'synthetic-attempt' });
  let sealed = false;
  return {
    boundary, requests, consumed, subject,
    register: (name, body, effect = 'write') => register(ctx, name, body, boundary, effect),
    async create(id = `synthetic-${randomUUID()}`, cwd = scratch) {
      const trustedCwd = await realpath(cwd);
      const handle = await ctx.agents.create({ sessionId: id, agentOptions: options,
        meta: { cwd }, setup: (scope) => boundary.bindAgent(scope.agent, subject(id, trustedCwd)) });
      if (!sealed) { boundary.seal(); sealed = true; }
      return handle;
    },
    execute: (agent, name, args = {}, extra = {}) => ctx.tools.execute(input(name, args,
      { callId: randomUUID(), agent, ...extra })),
  };
}

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
    const harness = hardened ? protectedHarness(ctx) : undefined;
    register(ctx, 'synthetic_write', (args) => { executed.push(args.target); return 'counter only'; }, harness?.boundary);
    ctx.tools.guard((exec) => exec.arguments.target === 'approved-target' ? undefined : 'unapproved target');
    ctx.on('tools/execute', async (exec, next) => {
      exec.arguments = { target: 'unapproved-target' };
      return next();
    });
    const handle = harness ? await harness.create('synthetic-parameter-binding')
      : await ctx.agents.create({ sessionId: 'synthetic-parameter-binding', agentOptions: options });
    handle.agent.followup(message('synthetic tool request'));
    await handle.agent.whenIdle();
    evidence.push({ case: 'post_guard_parameter_replacement_via_agent_loop', bodyTargets: executed });
    await handle.dispose();
    assert.deepEqual(executed, [], 'unapproved replacement arguments reached synthetic body after native final guard');
  });

  await check('guard_binds_actual_dispatch_tool', async () => {
    const ctx = runtime(path.join(scratch, 'tool-binding'));
    let writes = 0;
    const harness = hardened ? protectedHarness(ctx) : undefined;
    register(ctx, 'synthetic_read', () => 'counter only', harness?.boundary, 'read');
    register(ctx, 'synthetic_write', () => { writes++; return 'counter only'; }, harness?.boundary);
    ctx.tools.guard((exec) => exec.name === 'synthetic_read' ? undefined : 'unapproved tool');
    ctx.on('tools/execute', async (exec, next) => { exec.name = 'synthetic_write'; return next(); });
    const handle = harness ? await harness.create('synthetic-tool-binding') : undefined;
    const outcome = harness ? await harness.execute(handle.agent, 'synthetic_read')
      : await ctx.tools.execute(input('synthetic_read'));
    evidence.push({ case: 'post_guard_tool_replacement', syntheticWrites: writes, isError: outcome.isError === true });
    assert.equal(writes, 0, 'unapproved replacement tool reached synthetic body after native final guard');
  });

  if (hardened) await runBoundaryChecks();
}

async function runBoundaryChecks() {
  await check('boundary_allows_approved_action_once', async () => {
    const ctx = runtime(path.join(scratch, 'approved'));
    const h = protectedHarness(ctx);
    const calls = [];
    h.register('synthetic_write', (args, context) => {
      assert.equal(Object.isFrozen(args), true);
      assert.equal(Object.isFrozen(context.subject), true);
      assert.equal(context.cwd, canonicalScratch);
      assert.equal(context.agent, undefined);
      calls.push(args.target); return 'counter only';
    });
    h.boundary.register({
      name: 'synthetic_no_mutable_this', description: 'Synthetic method receiver check.',
      parameters: { type: 'object', properties: {} },
      output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
      execute() { assert.equal(this, undefined); return 'no borrowed receiver'; },
    }, { effect: 'read' });
    const { agent } = await h.create();
    const result = await h.execute(agent, 'synthetic_write', { target: 'approved-target' }, { callId: 'same-call' });
    assert.notEqual(result.isError, true, JSON.stringify(result));
    assert.equal((await h.execute(agent, 'synthetic_write', { target: 'approved-target' }, { callId: 'same-call' })).isError, true);
    assert.deepEqual(calls, ['approved-target']);
    assert.equal(h.requests.length, 1);
    assert.equal(h.consumed.size, 1);
    assert.notEqual((await h.execute(agent, 'synthetic_no_mutable_this')).isError, true);
    const request = h.requests[0];
    for (const key of ['sender', 'channel', 'conversation', 'session', 'project', 'cwd', 'attempt']) {
      assert.equal(request.subject[key], h.subject(agent.id)[key]);
    }
  });

  await check('boundary_reads_need_no_approval', async () => {
    const ctx = runtime(path.join(scratch, 'read'));
    const h = protectedHarness(ctx, { writesEnabled: false });
    let reads = 0;
    h.register('synthetic_read', () => { reads++; return 'counter only'; }, 'read');
    const { agent } = await h.create();
    assert.notEqual((await h.execute(agent, 'synthetic_read', { target: '/etc/hosts' })).isError, true);
    assert.equal(reads, 1);
    assert.equal(h.requests.length, 0);
  });

  await check('boundary_writes_default_off', async () => {
    const ctx = runtime(path.join(scratch, 'write-off'));
    const h = protectedHarness(ctx, { writesEnabled: false });
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create();
    assert.equal((await h.execute(agent, 'synthetic_write')).isError, true);
    assert.equal(calls, 0);
    assert.equal(h.requests.length, 0);
  });

  await check('boundary_denies_missing_or_foreign_identity', async () => {
    const ctx = runtime(path.join(scratch, 'foreign'));
    const h = protectedHarness(ctx);
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create();
    const forged = { ...agent, id: agent.id, session: agent.session };
    assert.equal((await ctx.tools.execute(input('synthetic_write'))).isError, true);
    assert.equal((await h.execute(forged, 'synthetic_write')).isError, true);
    assert.equal(calls, 0);
    assert.equal(h.requests.length, 0);
  });

  await check('boundary_denies_unwrapped_registration', async () => {
    const ctx = runtime(path.join(scratch, 'unwrapped'));
    const h = protectedHarness(ctx);
    register(ctx, 'unreviewed', () => 'must not run');
    await assert.rejects(h.create());
  });

  await check('boundary_registry_replacement_revokes_inflight', async () => {
    const ctx = runtime(path.join(scratch, 'registry'));
    const h = protectedHarness(ctx);
    let calls = 0;
    const remove = h.register('synthetic_read', () => 'counter only', 'read');
    const { agent } = await h.create();
    ctx.on('tools/execute', async (exec, next) => {
      remove();
      register(ctx, 'synthetic_read', () => { calls++; return 'unreviewed'; });
      exec.signal = new AbortController().signal;
      return next();
    });
    assert.equal((await h.execute(agent, 'synthetic_read')).isError, true);
    assert.equal(calls, 0);
  });

  await check('boundary_scoped_shadow_revokes_inflight', async () => {
    const ctx = runtime(path.join(scratch, 'shadow'));
    const h = protectedHarness(ctx);
    let calls = 0;
    h.register('synthetic_read', () => 'counter only', 'read');
    const { agent } = await h.create();
    ctx.on('tools/execute', async (_exec, next) => {
      register(agent.ctx, 'synthetic_read', () => { calls++; return 'unreviewed'; });
      return next();
    });
    assert.equal((await h.execute(agent, 'synthetic_read')).isError, true);
    assert.equal(calls, 0);
  });

  await check('boundary_rechecks_after_async_consumption', async () => {
    const ctx = runtime(path.join(scratch, 'consume-race'));
    let calls = 0;
    const h = protectedHarness(ctx, { consumeApproval: async () => { h.boundary.revoke(); return true; } });
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create();
    assert.equal((await h.execute(agent, 'synthetic_write')).isError, true);
    assert.equal(calls, 0);
  });

  await check('boundary_cancel_cannot_be_replaced', async () => {
    const ctx = runtime(path.join(scratch, 'signal'));
    const h = protectedHarness(ctx);
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create();
    const abort = new AbortController();
    ctx.on('tools/execute', async (exec, next) => {
      abort.abort();
      exec.signal = new AbortController().signal;
      return next();
    });
    assert.equal((await h.execute(agent, 'synthetic_write', {}, { signal: abort.signal })).isError, true);
    assert.equal(calls, 0);
    assert.equal(h.consumed.size, 0);
  });

  await check('boundary_denies_stale_changed_or_missing_grants', async () => {
    for (const kind of ['expired', 'wrong-digest', 'old-epoch', 'rejected', 'disconnected', 'too-long']) {
      const ctx = runtime(path.join(scratch, kind));
      let calls = 0;
      const h = protectedHarness(ctx, { requestApproval: async (request) => {
        if (kind === 'disconnected') throw new Error('synthetic disconnect');
        if (kind === 'rejected') return null;
        return { approvalId: 'synthetic-approval', digest: kind === 'wrong-digest' ? 'wrong' : request.digest,
          epoch: kind === 'old-epoch' ? 'prior-process' : request.epoch,
          expiresAt: Date.now() + (kind === 'expired' ? -1 : kind === 'too-long' ? 600000 : 60000) };
      } });
      h.register('synthetic_write', () => { calls++; return 'counter only'; });
      const { agent } = await h.create();
      assert.equal((await h.execute(agent, 'synthetic_write')).isError, true, kind);
      assert.equal(calls, 0, kind);
    }
  });

  await check('boundary_pre_hook_cannot_discard_denial', async () => {
    const ctx = runtime(path.join(scratch, 'discard-denial'));
    ctx.on('tools/pre-execute', async (_exec, next) => { await next(); return { kind: 'allow' }; });
    const h = protectedHarness(ctx, { writesEnabled: false });
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create();
    // Even an earlier listener discarding the boundary's deny cannot make the
    // private execution record granted, so the monotonic guard still refuses.
    assert.equal((await h.execute(agent, 'synthetic_write')).isError, true);
    assert.equal(calls, 0);
  });

  await check('boundary_nested_dispatch_cannot_inherit_grant', async () => {
    const ctx = runtime(path.join(scratch, 'protected-nested'));
    const h = protectedHarness(ctx, { requestApproval: async () => null });
    let agent;
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    h.register('synthetic_nested', async () => {
      const result = await h.execute(agent, 'synthetic_write', {}, { parent: {}, rootCallId: 'parent-call' });
      assert.equal(result.isError, true);
      return 'nested denied';
    }, 'read');
    ({ agent } = await h.create());
    assert.notEqual((await h.execute(agent, 'synthetic_nested')).isError, true);
    assert.equal(calls, 0);
  });

  await check('boundary_seals_all_execution_identity_fields', async () => {
    for (const key of ['name', 'arguments', 'agent', 'callId', 'rootCallId', 'parent', 'token']) {
      const ctx = runtime(path.join(scratch, `seal-${key}`));
      const h = protectedHarness(ctx);
      let calls = 0;
      h.register('synthetic_write', () => { calls++; return 'counter only'; });
      const { agent } = await h.create();
      ctx.on('tools/execute', async (exec, next) => { exec[key] = {}; return next(); });
      assert.equal((await h.execute(agent, 'synthetic_write')).isError, true, key);
      assert.equal(calls, 0, key);
      assert.equal(h.consumed.size, 0, key);
    }
  });

  await check('boundary_pre_hook_parameter_change_denied', async () => {
    const ctx = runtime(path.join(scratch, 'pre-arguments'));
    const h = protectedHarness(ctx);
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create();
    ctx.on('tools/pre-execute', async (exec, next) => {
      exec.arguments = { target: 'unapproved-target' }; return next();
    });
    assert.equal((await h.execute(agent, 'synthetic_write', { target: 'approved-target' })).isError, true);
    assert.equal(calls, 0);
    assert.equal(h.consumed.size, 0);
  });

  await check('boundary_pending_approval_cancel_discards_late_allow', async () => {
    const ctx = runtime(path.join(scratch, 'late-allow'));
    const entered = Promise.withResolvers();
    const late = Promise.withResolvers();
    let saved;
    const h = protectedHarness(ctx, { requestApproval: async (request) => {
      saved = request; entered.resolve(); return late.promise;
    } });
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create();
    const abort = new AbortController();
    const task = h.execute(agent, 'synthetic_write', {}, { signal: abort.signal });
    await entered.promise;
    abort.abort();
    assert.equal((await task).isError, true);
    late.resolve({ approvalId: 'too-late', digest: saved.digest, epoch: saved.epoch, expiresAt: Date.now() + 60000 });
    await Promise.resolve();
    assert.equal(calls, 0);
    assert.equal(h.consumed.size, 0);
  });

  await check('boundary_consumption_failure_spends_execution', async () => {
    for (const kind of ['false', 'disconnect', 'cancel']) {
      const ctx = runtime(path.join(scratch, `consume-${kind}`));
      const abort = new AbortController();
      const h = protectedHarness(ctx, { consumeApproval: async () => {
        if (kind === 'disconnect') throw new Error('synthetic disconnect');
        if (kind === 'cancel') { abort.abort(); return true; }
        return false;
      } });
      let calls = 0;
      h.register('synthetic_write', () => { calls++; return 'counter only'; });
      const { agent } = await h.create();
      assert.equal((await h.execute(agent, 'synthetic_write', {}, { signal: abort.signal, callId: 'same-call' })).isError, true);
      assert.equal((await h.execute(agent, 'synthetic_write', {}, { callId: 'same-call' })).isError, true);
      assert.equal(calls, 0, kind);
    }
  });

  await check('boundary_expiry_rechecked_before_action', async () => {
    const ctx = runtime(path.join(scratch, 'expiry-race'));
    const start = Date.now();
    let wall = start;
    let monotonic = 0;
    const h = protectedHarness(ctx, {
      clock: () => ({ wall, monotonic }),
      requestApproval: async (request) => ({ approvalId: 'expires', digest: request.digest,
        epoch: request.epoch, expiresAt: start + 300000 }),
      consumeApproval: async () => { wall = start + 300000; monotonic = 300000; return true; },
    });
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create();
    assert.equal((await h.execute(agent, 'synthetic_write')).isError, true);
    assert.equal(calls, 0);
  });

  await check('boundary_one_approval_id_cannot_authorize_two_calls', async () => {
    const ctx = runtime(path.join(scratch, 'same-approval'));
    const h = protectedHarness(ctx, { requestApproval: async (request) => ({
      approvalId: 'same-approval', digest: request.digest, epoch: request.epoch, expiresAt: Date.now() + 60000,
    }) });
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create();
    const results = await Promise.all([h.execute(agent, 'synthetic_write'), h.execute(agent, 'synthetic_write')]);
    assert.equal(results.filter((result) => !result.isError).length, 1);
    assert.equal(calls, 1);
  });

  await check('boundary_action_can_only_enter_once', async () => {
    const ctx = runtime(path.join(scratch, 'same-execution'));
    const h = protectedHarness(ctx);
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create();
    ctx.on('tools/execute', async (exec, next) => {
      const definition = ctx.tools.get(exec.name, exec.agent);
      const first = definition.execute(exec.arguments, exec);
      await assert.rejects(definition.execute(exec.arguments, exec));
      await first;
      return next();
    });
    assert.equal((await h.execute(agent, 'synthetic_write')).isError, true);
    assert.equal(calls, 1);
    assert.equal(h.consumed.size, 1);
  });

  await check('boundary_disabled_services_and_command_bypasses_denied', async () => {
    for (const scope of ['global', 'agent']) {
      for (const service of ['loader', 'codeRuntime', 'subagents', 'dynamicCordisRunner']) {
        const ctx = runtime(path.join(scratch, `service-${scope}-${service}`));
        const h = protectedHarness(ctx);
        let calls = 0;
        h.register('synthetic_read', () => { calls++; return 'counter only'; }, 'read');
        const { agent } = await h.create();
        (scope === 'global' ? ctx : agent.ctx).provide(service, {});
        assert.equal((await h.execute(agent, 'synthetic_read')).isError, true, service);
        assert.equal(calls, 0, service);
      }
    }
    const ctx = runtime(path.join(scratch, 'prohibited-tools'));
    const h = protectedHarness(ctx);
    for (const name of ['run_code', 'cordis_run', 'spawn_agent', 'subagent', 'workflow_run', 'ralph', 'bash', 'pwsh']) {
      assert.throws(() => h.register(name, () => 'must not run', 'read'), name);
    }
  });

  await check('boundary_pins_and_rechecks_native_subject', async () => {
    for (const field of ['id', 'session']) {
      const ctx = runtime(path.join(scratch, `subject-${field}`));
      const h = protectedHarness(ctx);
      let calls = 0;
      h.register('synthetic_write', () => { calls++; return 'counter only'; });
      const { agent } = await h.create();
      assert.throws(() => h.boundary.bindAgent(agent, { ...h.subject(agent.id), sender: 'another-owner' }));
      const previous = agent[field];
      ctx.on('tools/execute', async (_exec, next) => {
        agent[field] = field === 'id' ? 'foreign-session' : { id: agent.id, header: { cwd: canonicalScratch } };
        try { return await next(); } finally { agent[field] = previous; }
      });
      assert.equal((await h.execute(agent, 'synthetic_write')).isError, true, field);
      assert.equal(calls, 0, field);
    }
  });

  await check('boundary_directory_replacement_denied', async () => {
    const directory = path.join(scratch, 'directory-fixture');
    await mkdir(directory);
    const ctx = runtime(path.join(scratch, 'directory-replacement'));
    const h = protectedHarness(ctx);
    let calls = 0;
    h.register('synthetic_write', () => { calls++; return 'counter only'; });
    const { agent } = await h.create(undefined, directory);
    ctx.on('tools/execute', async (_exec, next) => {
      await rename(directory, directory + '.original');
      await mkdir(directory);
      return next();
    });
    assert.equal((await h.execute(agent, 'synthetic_write')).isError, true);
    assert.equal(calls, 0);
    assert.equal(h.consumed.size, 0);
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
const boundaryHash = createHash('sha256').update(await readFile(new URL('../native_boundary.mjs', import.meta.url))).digest('hex');
process.stdout.write(JSON.stringify({ stage: 'A', boundary: hardened ? 'protected' : 'native-only', boundaryHash,
  gatePassed, node: process.version, hashes, results, evidence,
  productionChanged: false, realModelCalls: 0, realToolActions: 0 }, null, 2) + '\n');
process.exitCode = gatePassed ? 0 : 2;
