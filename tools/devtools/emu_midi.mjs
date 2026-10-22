// The control socket of a running emulator as a `DkTransport`: JSON-RPC lines, the `midi.usb.in` / `midi.usb.out` the desktop page reaches with `emu_call`.
// Shared by the live checks of the TypeScript clients (samples_live.mjs, samples_store_live.mjs). A dev tool: the app starts no process.
import net from 'node:net';
import { splitMessages } from '../../apps/web/src/modules/dkupload-core.ts';

/** Connect to the daemon's socket: `{ call(method, params), transport, close() }`. */
export async function connect(socketPath) {
  const sock = net.connect(socketPath);
  let buf = '';
  let nextId = 1;
  const waiting = new Map();
  sock.on('data', (d) => {
    buf += d;
    for (let i = buf.indexOf('\n'); i >= 0; i = buf.indexOf('\n')) {
      const m = JSON.parse(buf.slice(0, i));
      buf = buf.slice(i + 1);
      if (m.id !== undefined && waiting.has(m.id)) {
        const { ok, fail } = waiting.get(m.id);
        waiting.delete(m.id);
        if (m.error) fail(new Error(m.error.message));
        else ok(m.result);
      }
    }
  });
  const call = (method, params = {}) =>
    new Promise((ok, fail) => {
      const id = nextId++;
      waiting.set(id, { ok, fail });
      sock.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  await call('hello', { min_schema: 1 });

  const transport = {
    rx: new Uint8Array(),
    ready: [],
    async send(msg) {
      await call('midi.usb.in', { bytes: Buffer.from(msg).toString('hex'), chunk: 384, period: 16500 });
    },
    async recv(timeoutMs) {
      const end = Date.now() + timeoutMs;
      for (;;) {
        const next = this.ready.shift();
        if (next) return next;
        const { hex: got } = await call('midi.usb.out');
        if (got) {
          const { messages, rest } = splitMessages(Uint8Array.from(Buffer.concat([this.rx, Buffer.from(got, 'hex')])));
          this.rx = rest;
          this.ready.push(...messages);
          continue;
        }
        if (Date.now() >= end) return null;
        await new Promise((r) => setTimeout(r, 10));
      }
    },
  };
  return { call, transport, close: () => sock.end() };
}
