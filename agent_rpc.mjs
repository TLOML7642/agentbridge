/** Private worker IPC. Capability and socket never come from model arguments. */
import net from 'node:net';
import { openSync, writeSync, closeSync, fstatSync, constants } from 'node:fs';
import path from 'node:path';

export function diagnostic(broker, stage, code, state, exception, milliseconds) {
  if (!broker?.diagnostics || !/^[a-f0-9]{32}$/.test(broker.correlation ?? '')) return;
  let fd;
  try {
    const event = { time: Date.now() / 1000, correlation: broker.correlation,
      stage: ['native_rpc', 'guard', 'receipt'].includes(stage) ? stage : 'native_rpc',
      code: ['OK', 'START', 'DENIED', 'TIMEOUT', 'CLOSED', 'CANCELLED', 'UNKNOWN'].includes(code) ? code : 'UNKNOWN' };
    if (['pending', 'approved', 'expired', 'revoked', 'denied', 'consumed'].includes(state)) event.state = state;
    if (exception) event.exception_type = ['Error', 'TypeError', 'AbortError'].includes(exception.name) ? exception.name : 'Error';
    if (['EPIPE', 'ECONNRESET', 'ECONNREFUSED', 'ENOENT', 'EACCES', 'EMFILE', 'ENFILE'].includes(exception?.code)) event.errno = exception.code;
    if (Number.isFinite(milliseconds) && milliseconds >= 0) event.milliseconds = Math.round(milliseconds * 1000) / 1000;
    const day = new Date().toISOString().slice(0, 10).replaceAll('-', '');
    fd = openSync(path.join(broker.diagnostics, 'diagnostic-' + day + '.jsonl'),
      constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT | constants.O_NOFOLLOW, 0o600);
    const info = fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077)) throw new Error();
    writeSync(fd, JSON.stringify(event) + '\n');
  } catch { process.stderr.write('AGENTBRIDGE_DIAGNOSTICS_WRITE_FAILED\n'); }
  finally { if (fd !== undefined) closeSync(fd); }
}

export function rpc(broker, op, fields, signal) {
  signal?.throwIfAborted();
  const purpose = broker.purpose === 'personal-service' ? 'SERVICE_RPC' : 'TASK_RPC';
  return new Promise((resolve, reject) => {
    const started = performance.now();
    let completed = false;
    const socket = net.createConnection({ path: broker.socket });
    socket.setEncoding('utf8'); // Preserve code points split across IPC chunks.
    let data = '';
    const timeout = setTimeout(() => socket.destroy(new Error(purpose + '_TIMEOUT')), 5000);
    const abort = () => socket.destroy(new Error(purpose + '_CANCELLED'));
    signal?.addEventListener('abort', abort, { once: true });
    const clean = () => { clearTimeout(timeout); signal?.removeEventListener('abort', abort); };
    socket.once('connect', () => socket.write(JSON.stringify({ ...fields, op, capability: broker.capability }) + '\n'));
    socket.on('data', (chunk) => {
      data += chunk;
      if (Buffer.byteLength(data) > 65536) { socket.destroy(new Error(purpose + '_TOO_LARGE')); return; }
      if (!data.includes('\n')) return;
      try {
        const value = JSON.parse(data.slice(0, data.indexOf('\n')));
        if (value?.ok !== true) {
          // Preserve only fixed host outcome codes, never remote prose or
          // model-supplied fields. Offline is not an approval rejection.
          const codes = ['MAC_OFFLINE', 'EXECUTOR_UNKNOWN', ...(purpose === 'SERVICE_RPC'
            ? ['REMINDER_INTENT_REQUIRED', 'REMINDER_RECURRING_UNSUPPORTED', 'REMINDER_FOLLOWUP_CONFLICT'] : [])];
          const code = codes.includes(value?.code) ? value.code : purpose + '_DENIED';
          throw new Error(code);
        }
        completed = true;
        diagnostic(broker, 'native_rpc', 'OK', value.state, null, performance.now() - started);
        clean(); socket.destroy(); resolve(value);
      } catch (error) { socket.destroy(error); }
    });
    socket.once('error', (error) => {
      completed = true;
      const code = error.message === purpose + '_TIMEOUT' ? 'TIMEOUT' :
        error.message === purpose + '_CANCELLED' ? 'CANCELLED' :
        [purpose + '_DENIED', 'REMINDER_INTENT_REQUIRED', 'REMINDER_RECURRING_UNSUPPORTED', 'REMINDER_FOLLOWUP_CONFLICT'].includes(error.message) ? 'DENIED' : 'UNKNOWN';
      diagnostic(broker, 'native_rpc', code, null, error, performance.now() - started);
      clean(); reject(error);
    });
    socket.once('close', () => {
      if (!completed) diagnostic(broker, 'native_rpc', 'CLOSED', null, null, performance.now() - started);
      clean(); reject(new Error(purpose + '_CLOSED'));
    });
  });
}
