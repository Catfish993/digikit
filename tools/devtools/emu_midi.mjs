// Connection adapter for the live benchmark and Memory panel tools. Socket and SysEx framing
// belong to the same shared clients used by the desktop and native regression tests.
import { connect as connectControl } from '../../packages/control-client/src/index.ts';
import { EmuUsbTransport } from '../../packages/dk-session/src/index.ts';

/** Connect to the daemon's socket: `{ call(method, params), transport, close() }`. */
export async function connect(socketPath) {
  const client = await connectControl(socketPath);
  const call = client.call.bind(client);
  const transport = new EmuUsbTransport(call, { pollMs: Number(process.env.DIGI_POLL_MS ?? 10) });
  return { call, transport, close: () => client.close() };
}
