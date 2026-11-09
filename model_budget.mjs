/** Opt-in, fail-closed meter for this trial's paid probes, not daily chat. */
import assert from 'node:assert/strict';
import { constants } from 'node:fs';
import { open, unlink } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// Peak CNY prices, also conservative during off-peak hours. One micro-CNY per
// token at 1 CNY/M. Reserve the ENTIRE context, not a guessed tokenizer count.
export const POLICY = Object.freeze({ version: 1, limit: 3000000, requests: 8,
  input: 2, cached: 0.04, output: 8, context: 1048576, maxTokens: 4096,
  model: 'deepseek-v4-flash', priceDate: '2026-11-09',
  priceSource: 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing',
  expires: '2026-11-10T16:00:00Z' });
const RESERVE = POLICY.context * POLICY.input + POLICY.maxTokens * POLICY.output;
const NOFOLLOW = constants.O_NOFOLLOW;

async function privateOpen(file, flags) {
  assert.ok(path.isAbsolute(file));
  const fd = await open(file, flags | NOFOLLOW, 0o600);
  try {
    const info = await fd.stat();
    assert.ok(info.isFile() && info.uid === process.getuid() && (info.mode & 0o077) === 0);
    return fd;
  } catch (error) { await fd.close(); throw error; }
}

async function syncParent(file) {
  const fd = await open(path.dirname(file), constants.O_RDONLY);
  try { await fd.sync(); } finally { await fd.close(); }
}

export async function initializeBudget(file) {
  const fd = await privateOpen(file, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL);
  try { await fd.writeFile(JSON.stringify({ policy: POLICY }) + '\n'); await fd.sync(); }
  finally { await fd.close(); }
  await syncParent(file);
}

async function append(fd, row) {
  await fd.write(JSON.stringify(row) + '\n');
  await fd.sync();
}

function stateOf(text) {
  assert.ok(text.endsWith('\n'), 'torn budget ledger');
  const [header, ...rows] = text.trimEnd().split('\n').map(line => JSON.parse(line));
  assert.deepEqual(header, { policy: POLICY });
  let used = 0, count = 0, pending = false, stopped = false;
  for (const row of rows) {
    if (row.kind === 'reserve') {
      assert.ok(!pending && !stopped && count < POLICY.requests && used + RESERVE <= POLICY.limit);
      assert.deepEqual(row, { kind: 'reserve', number: count + 1, amount: RESERVE });
      count++; used += RESERVE; pending = true;
    } else if (row.kind === 'settle') {
      assert.ok(pending && row.number === count && Number.isSafeInteger(row.amount)
        && row.amount >= 0 && row.amount <= RESERVE);
      used += row.amount - RESERVE; pending = false;
    } else {
      assert.ok(row.kind === 'stop' && pending && row.number === count);
      stopped = true;
    }
  }
  return { used, count, pending, stopped };
}

export async function budgetStatus(file) {
  const fd = await privateOpen(file, constants.O_RDONLY);
  try { return stateOf(await fd.readFile('utf8')); } finally { await fd.close(); }
}

function requestBody(url, init) {
  assert.ok(['https://api.deepseek.com/chat/completions',
    'https://api.deepseek.com/v1/chat/completions'].includes(String(url)));
  assert.equal(init?.method, 'POST');
  const body = JSON.parse(init.body);
  assert.equal(body.model, POLICY.model);
  assert.equal(body.max_tokens, POLICY.maxTokens);
  assert.deepEqual(body.thinking, { type: 'enabled' });
  assert.equal(body.reasoning_effort, 'high');
  assert.equal(body.stream, true);
  assert.equal(body.stream_options?.include_usage, true);
  // No independently billed search/fetch, files, media or provider built-ins.
  assert.ok(Array.isArray(body.messages) && body.messages.every(message =>
    typeof message.content === 'string' || message.content == null ||
    (Array.isArray(message.content) && message.content.every(block => block.type === 'text'))));
  assert.ok((body.tools ?? []).every(tool => tool.type === 'function'));
  return body;
}

function usageCost(bytes) {
  let usage, done = false, terminal = false;
  for (const line of bytes.toString('utf8').split(/\r?\n/)) {
    if (!line.startsWith('data:')) continue;
    const data = line.slice(5).trim();
    assert.ok(!done, 'data after stream end');
    if (data === '[DONE]') { done = true; continue; }
    const row = JSON.parse(data);
    assert.ok(!row.error);
    terminal ||= row.choices?.some(choice => typeof choice.finish_reason === 'string');
    if (row.usage != null) usage = row.usage;
  }
  assert.ok(done && terminal && usage, 'missing terminal usage');
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  assert.ok(integer(usage.prompt_tokens) && integer(usage.completion_tokens)
    && usage.prompt_tokens <= POLICY.context && usage.completion_tokens <= POLICY.maxTokens);
  const cached = usage.prompt_cache_hit_tokens ?? usage.prompt_tokens_details?.cached_tokens ?? 0;
  assert.ok(integer(cached) && cached <= usage.prompt_tokens);
  if (usage.prompt_cache_miss_tokens != null) assert.equal(usage.prompt_cache_miss_tokens, usage.prompt_tokens - cached);
  if (usage.prompt_cache_hit_tokens != null && usage.prompt_tokens_details?.cached_tokens != null)
    assert.equal(usage.prompt_cache_hit_tokens, usage.prompt_tokens_details.cached_tokens);
  if (usage.total_tokens != null) assert.equal(usage.total_tokens, usage.prompt_tokens + usage.completion_tokens);
  // Integer micro-CNY rounded UP. This is an upper bound, not a billing invoice.
  return Math.ceil((usage.prompt_tokens - cached) * POLICY.input + cached * POLICY.cached
    + usage.completion_tokens * POLICY.output);
}

export function createBudgetFetch(file, upstream = globalThis.fetch, now = () => Date.now()) {
  return async (url, init) => {
    requestBody(url, init);
    assert.ok(now() < Date.parse(POLICY.expires), 'price snapshot expired');
    // Hold the exclusive lock THROUGH settlement. A crash leaves it in place;
    // neither restart nor deleting a lock refunds an outstanding reservation.
    const lock = await privateOpen(file + '.lock', constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL);
    let fd;
    try {
      await syncParent(file);
      fd = await privateOpen(file, constants.O_RDWR | constants.O_APPEND);
      const state = stateOf(await fd.readFile('utf8'));
      assert.ok(!state.pending && !state.stopped && state.count < POLICY.requests
        && state.used + RESERVE <= POLICY.limit, 'paid probe budget unavailable');
      const number = state.count + 1;
      await append(fd, { kind: 'reserve', number, amount: RESERVE });
      let httpStatus;
      try {
        const response = await upstream(url, { ...init, redirect: 'error' });
        httpStatus = response.status;
        assert.equal(response.status, 200, 'paid probe HTTP failure');
        assert.ok(response.headers.get('content-type')?.startsWith('text/event-stream'));
        const chunks = [];
        let size = 0;
        for await (const chunk of response.body) {
          size += chunk.length;
          assert.ok(size <= 2097152, 'oversized paid probe response');
          chunks.push(chunk);
        }
        const bytes = Buffer.concat(chunks);
        const amount = usageCost(bytes);
        await append(fd, { kind: 'settle', number, amount });
        return new Response(bytes, { status: 200, headers: { 'content-type': 'text/event-stream' } });
      } catch {
        await append(fd, { kind: 'stop', number, ...(httpStatus ? { http_status: httpStatus } : {}) });
        throw new Error('PAID_PROBE_STOPPED_RESERVATION_RETAINED');
      }
    } finally {
      if (fd) await fd.close();
      await lock.close();
      await unlink(file + '.lock');
      await syncParent(file);
    }
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [operation, file] = process.argv.slice(2);
  if (operation === 'init') await initializeBudget(file);
  else assert.equal(operation, 'status');
  process.stdout.write(JSON.stringify(await budgetStatus(file)) + '\n');
}
