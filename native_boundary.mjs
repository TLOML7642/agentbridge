/** Bridge-owned adapter for audited tools, not a sandbox for arbitrary JS plugins. */
import { createHash, randomUUID } from 'node:crypto';
import { realpathSync, statSync } from 'node:fs';
import path from 'node:path';
import { performance } from 'node:perf_hooks';

const SUBJECT_KEYS = ['sender', 'channel', 'conversation', 'session', 'project', 'cwd', 'attempt'];
const EXECUTION_KEYS = ['token', 'name', 'arguments', 'agent', 'callId', 'rootCallId', 'parent'];
const DENIED_SERVICES = ['loader', 'codeRuntime', 'subagents', 'dynamicCordisRunner'];
const DENIED_TOOLS = /^(?:run_code$|cordis(?:_|$)|spawn_agent$|subagent(?:_|$)|workflow(?:_|$)|ralph$)/;
const COMMAND_TOOLS = new Set(['bash', 'shell', 'pwsh', 'exec_command', 'run_command']);
const MAX_TTL = 300000;

function fail() { throw new Error('AGENTBRIDGE_BOUNDARY_DENIED'); }
function text(value) {
  if (typeof value !== 'string' || !value || value.length > 4096 || value.includes('\0')) fail();
  return value;
}
function freeze(value) {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
function json(value) {
  // Native ToolRuntime already detached lossless JSON. Refuse non-JSON inputs
  // on this adapter's direct-call face too, rather than silently coercing them.
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (Array.isArray(value)) return value.map(json);
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) fail();
  const copy = {};
  for (const key of Object.keys(value).sort()) {
    Object.defineProperty(copy, key, { value: json(value[key]), enumerable: true });
  }
  return copy;
}
function snapshot(value) { return freeze(json(value)); }
function digest(value) { return createHash('sha256').update(JSON.stringify(value)).digest('hex'); }
function canonicalDirectory(value) {
  text(value);
  if (!path.isAbsolute(value)) fail();
  const resolved = realpathSync(value);
  if (!statSync(resolved).isDirectory()) fail();
  return resolved;
}

/**
 * Install BEFORE extension hooks, register only reviewed tools, bind trusted
 * native agents during setup, then seal before their first message.
 * Approval callbacks are trusted host code, never model tools. The caller must
 * implement durable request/atomic consumption; absent callbacks deny writes.
 */
export function installToolBoundary(ctx, {
  writesEnabled = false,
  requestApproval,
  consumeApproval,
  clock = () => ({ wall: Date.now(), monotonic: performance.now() }),
} = {}) {
  const epoch = randomUUID();
  const revoked = new AbortController();
  const agents = new WeakMap();
  const executions = new WeakMap();
  const definitions = new Map();
  const usedCalls = new Set();
  const usedApprovals = new Set();
  let sealed = false;
  let closed = false;

  function revoke() {
    closed = true;
    revoked.abort(new Error('AGENTBRIDGE_BOUNDARY_REVOKED'));
  }

  function composition(agent) {
    if (closed || !sealed || ctx.tools.modeFor(agent) !== 'native') fail();
    for (const service of DENIED_SERVICES) {
      if (ctx.get(service) !== undefined || agent?.ctx.get(service) !== undefined) fail();
    }
  }

  function binding(agent) {
    const bound = agents.get(agent);
    if (!bound || agent.session !== bound.session || agent.id !== bound.subject.session
        || agent.session.id !== bound.subject.session || ctx.agents.get(agent.id) !== agent
        || canonicalDirectory(agent.session.header.cwd) !== bound.subject.cwd) fail();
    const directory = statSync(bound.subject.cwd);
    if (directory.dev !== bound.directory.dev || directory.ino !== bound.directory.ino) fail();
    return bound;
  }

  async function waitForHost(operation, record) {
    const signal = AbortSignal.any([record.originalSignal, revoked.signal]);
    signal.throwIfAborted();
    let timer;
    let abort;
    const stopped = new Promise((_resolve, reject) => {
      abort = () => reject(new Error('AGENTBRIDGE_BOUNDARY_DENIED'));
      signal.addEventListener('abort', abort, { once: true });
      timer = setTimeout(abort, Math.max(0, MAX_TTL - (clock().monotonic - record.start.monotonic)));
    });
    try { return await Promise.race([Promise.resolve().then(operation), stopped]); }
    finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
  }

  function valid(record, exec) {
    composition(exec.agent);
    const bound = binding(exec.agent);
    if (bound !== record.bound || record.originalSignal.aborted || revoked.signal.aborted
        || exec.signal.aborted || ctx.tools.get(record.definition.name, exec.agent) !== record.definition) fail();
    for (const key of EXECUTION_KEYS) if (exec[key] !== record.fields[key]) fail();
    if (record.effect === 'write') {
      const now = clock();
      if (!record.grant || !Number.isFinite(now.wall) || !Number.isFinite(now.monotonic)
          || now.wall < record.start.wall || now.monotonic < record.start.monotonic
          || now.wall >= record.grant.expiresAt
          || now.monotonic - record.start.monotonic >= record.grant.expiresAt - record.start.wall) fail();
    }
  }

  function sealExecution(record, exec) {
    for (const key of EXECUTION_KEYS) Object.defineProperty(exec, key, {
      value: record.fields[key], writable: false, configurable: false, enumerable: true,
    });
    // DSH replaces signal while dispatching and restoring it. Keep that API,
    // but a wrapper can never discard the caller's cancellation or revocation.
    let signal = AbortSignal.any([record.originalSignal, revoked.signal, exec.signal]);
    Object.defineProperty(exec, 'signal', {
      enumerable: true, configurable: false,
      get: () => signal,
      set: (value) => { signal = AbortSignal.any([record.originalSignal, revoked.signal, value]); },
    });
  }

  ctx.on('tools/change', () => { if (sealed) revoke(); });
  ctx.effect(() => revoke, 'agentbridge.boundary.lifecycle()');

  ctx.on('tools/pre-execute', async (exec, next) => {
    try {
      composition(exec.agent);
      const bound = binding(exec.agent);
      const entry = definitions.get(exec.name);
      if (!entry || ctx.tools.get(exec.name, exec.agent) !== entry.definition) fail();
      const callKey = JSON.stringify([bound.subject.session, bound.subject.attempt, text(exec.callId)]);
      if (usedCalls.has(callKey)) fail();
      usedCalls.add(callKey);
      const start = snapshot(clock());
      const fields = Object.fromEntries(EXECUTION_KEYS.map((key) => [key, exec[key]]));
      const args = snapshot(exec.arguments);
      const envelope = snapshot({ version: 1, epoch, execution: randomUUID(),
        subject: bound.subject, tool: exec.name, arguments: args,
        callId: text(exec.callId), rootCallId: text(exec.rootCallId), nested: exec.parent !== undefined });
      const request = freeze({ ...envelope, digest: digest(envelope) });
      const record = { bound, fields, args, definition: entry.definition, effect: entry.effect,
        originalSignal: exec.signal, request, start, state: 'pending', guarded: false };
      executions.set(exec, record);
      const policy = await next();
      if (policy.kind !== 'allow') return policy;
      if (entry.effect === 'write') {
        if (writesEnabled !== true || typeof requestApproval !== 'function' || typeof consumeApproval !== 'function') fail();
        const answer = await waitForHost(() => requestApproval(request, record.originalSignal), record);
        if (!answer || answer.digest !== request.digest || answer.epoch !== epoch
            || typeof answer.approvalId !== 'string' || !answer.approvalId
            || !Number.isFinite(answer.expiresAt) || answer.expiresAt > start.wall + MAX_TTL) fail();
        text(answer.approvalId);
        if (usedApprovals.has(answer.approvalId)) fail();
        usedApprovals.add(answer.approvalId);
        record.grant = snapshot({ approvalId: answer.approvalId, digest: answer.digest,
          epoch: answer.epoch, expiresAt: answer.expiresAt });
      }
      valid(record, exec);
      record.state = 'granted';
      return { kind: 'allow' };
    } catch {
      return { kind: 'deny', reason: 'AGENTBRIDGE_BOUNDARY_DENIED' };
    }
  });

  ctx.tools.guard((exec) => {
    try {
      const record = executions.get(exec);
      if (!record || record.state !== 'granted' || record.guarded) fail();
      valid(record, exec);
      sealExecution(record, exec);
      record.guarded = true;
      return undefined;
    } catch { return 'AGENTBRIDGE_BOUNDARY_DENIED'; }
  });

  return Object.freeze({
    bindAgent(agent, subject) {
      if (closed || agents.has(agent)) fail();
      const values = Object.fromEntries(SUBJECT_KEYS.map((key) => [key, text(subject[key])]));
      values.cwd = canonicalDirectory(values.cwd);
      if (agent.id !== values.session || agent.session.id !== values.session
          || canonicalDirectory(agent.session.header.cwd) !== values.cwd) fail();
      const directory = statSync(values.cwd);
      agents.set(agent, Object.freeze({ session: agent.session, subject: snapshot(values),
        directory: Object.freeze({ dev: directory.dev, ino: directory.ino }) }));
    },
    register(definition, { effect } = {}) {
      if (sealed || closed || !['read', 'write'].includes(effect) || definitions.has(definition.name)
          || typeof definition.execute !== 'function') fail();
      const name = text(definition.name);
      if (DENIED_TOOLS.test(name) || (effect === 'read' && COMMAND_TOOLS.has(name))) fail();
      const action = definition.execute;
      // No finalizers/classifiers/presenters borrowed from arbitrary plugins:
      // callbacks before/after execution are not an approval-protected action.
      const protectedDefinition = Object.freeze({
        name, description: text(definition.description), parameters: snapshot(definition.parameters),
        output: Object.freeze({ schema: snapshot(definition.output.schema), render: definition.output.render }),
        async execute(args, exec) {
          const record = executions.get(exec);
          if (!record || record.definition !== protectedDefinition || !record.guarded
              || record.state !== 'granted' || args !== record.fields.arguments) fail();
          valid(record, exec);
          record.state = 'consuming';
          if (effect === 'write') {
            // Host must atomically persist consumption before this resolves.
            // Cancellation after consumption spends the grant without running.
            if (await waitForHost(() => consumeApproval(record.request, record.grant, record.originalSignal), record) !== true) fail();
          }
          record.state = 'spent';
          valid(record, exec);
          return action(record.args, Object.freeze({
            cwd: record.bound.subject.cwd, subject: record.bound.subject,
            callId: record.request.callId, signal: exec.signal,
          }));
        },
      });
      definitions.set(name, { definition: protectedDefinition, effect });
      const unregister = ctx.tools.register(protectedDefinition);
      return () => { revoke(); unregister(); };
    },
    seal() {
      if (closed || sealed) fail();
      sealed = true;
      try {
        composition(undefined);
        for (const [name, definition] of ctx.tools.view(undefined).visible) {
          if (definitions.get(name)?.definition !== definition) fail();
        }
      } catch { revoke(); fail(); }
    },
    revoke,
  });
}
