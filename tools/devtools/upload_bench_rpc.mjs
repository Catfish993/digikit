#!/usr/bin/env node
// Throughput of the stock MidiRpc file write of the Digitakt II (+Drive: OpenFileForWrite, WriteFileV1 in chunks, CloseFileWriter; apps/web/src/modules/rpcfiles.ts)
// against a running emulator, as upload_bench.mjs measures the digikit store. tools/devtools/upload_bench.py --device dt2 starts the daemon. By hand:
//
//     node tools/devtools/upload_bench_rpc.mjs SOCKET [--bytes N] [--chunk N[,N...]] [--runs N]
//
// One JSON line per run and chunk size: wall seconds, MB/s, guest instructions per byte, the real-time factor, the controller's CPU seconds.
import { randomBytes } from 'node:crypto';
import { DkSession } from '../../apps/web/src/modules/dkupload-core.ts';
import { RpcFiles } from '../../apps/web/src/modules/rpcfiles.ts';
import { connect } from './emu_midi.mjs';

const [socketPath, ...flags] = process.argv.slice(2);
const flag = (name, dflt) => (flags.includes(name) ? flags[flags.indexOf(name) + 1] : dflt);
if (!socketPath) {
  console.error('usage: node tools/devtools/upload_bench_rpc.mjs SOCKET [--bytes N] [--chunk N[,N...]] [--runs N]');
  process.exit(2);
}
const bytes = Number(flag('--bytes', 1_000_000));
const chunks = flag('--chunk', '2048').split(',').map(Number);
const runs = Number(flag('--runs', 1));

const emu = await connect(socketPath);
const snap = async () => {
  const s = await emu.call('status');
  return { t: performance.now(), icount: s.cf.icount, idle: s.cf.idle_fast_forwarded_instructions, audio: s.audio?.source_audio_seconds ?? null, cpu: s.controller_cpu_ns / 1e9 };
};
const session = new DkSession(emu.transport, { timeoutMs: 60000 });
const data = Uint8Array.from(randomBytes(bytes));
for (let run = 0; run < runs; run++) for (const chunk of chunks) {
  const files = new RpcFiles(session, { chunk, timeoutMs: 60000 });
  const path = `bench/f${run}_${chunk}`;
  const a = await snap();
  let n = 0;
  const closed = await files.write(path, data, () => n++);
  const b = await snap();
  const wall = (b.t - a.t) / 1000;
  const info = await files.info(path);
  console.log(
    JSON.stringify({
      run, bytes, chunk, requests: n, closed: closed.size === bytes, stored: info.found && info.size === bytes,
      wall_s: +wall.toFixed(3), MBps: +(bytes / 1e6 / wall).toFixed(3),
      insn: b.icount - a.icount, busy_insn: b.icount - a.icount - (b.idle - a.idle), insn_per_byte: +((b.icount - a.icount) / bytes).toFixed(1),
      audio_s: +(b.audio - a.audio).toFixed(3), rtf: +((b.audio - a.audio) / wall).toFixed(3), cf_cpu_s: +(b.cpu - a.cpu).toFixed(3),
      ms_per_request: +((wall * 1000) / n).toFixed(1),
    }),
  );
  await files.remove(path).catch(() => {});
}
emu.close();
process.exit(0);
