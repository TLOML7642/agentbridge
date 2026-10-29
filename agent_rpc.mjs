/** Private worker IPC. Capability and socket never come from model arguments. */
import net from 'node:net';

export function rpc(broker, op, fields, signal) {
  signal?.throwIfAborted();
  const purpose = broker.purpose === 'personal-service' ? 'SERVICE_RPC' : 'TASK_RPC';
  return new Promise((resolve, reject) => {
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
        if (value?.ok !== true) throw new Error(purpose + '_DENIED');
        clean(); socket.destroy(); resolve(value);
      } catch (error) { socket.destroy(error); }
    });
    socket.once('error', (error) => { clean(); reject(error); });
    socket.once('close', () => { clean(); reject(new Error(purpose + '_CLOSED')); });
  });
}
