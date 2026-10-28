/** Private worker IPC. Capability and socket never come from model arguments. */
import net from 'node:net';

export function rpc(broker, op, fields, signal) {
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ path: broker.socket });
    let data = '';
    const timeout = setTimeout(() => socket.destroy(new Error('TASK_RPC_TIMEOUT')), 5000);
    const abort = () => socket.destroy(new Error('TASK_RPC_CANCELLED'));
    signal?.addEventListener('abort', abort, { once: true });
    const clean = () => { clearTimeout(timeout); signal?.removeEventListener('abort', abort); };
    socket.once('connect', () => socket.write(JSON.stringify({ ...fields, op, capability: broker.capability }) + '\n'));
    socket.on('data', (chunk) => {
      data += chunk.toString('utf8');
      if (Buffer.byteLength(data) > 65536) { socket.destroy(new Error('TASK_RPC_TOO_LARGE')); return; }
      if (!data.includes('\n')) return;
      try {
        const value = JSON.parse(data.slice(0, data.indexOf('\n')));
        if (value?.ok !== true) throw new Error('TASK_RPC_DENIED');
        clean(); socket.destroy(); resolve(value);
      } catch (error) { socket.destroy(error); }
    });
    socket.once('error', (error) => { clean(); reject(error); });
    socket.once('close', () => { clean(); reject(new Error('TASK_RPC_CLOSED')); });
  });
}
