#!/usr/bin/env node
// A live check of the TypeScript MidiRpc file client (apps/web/src/modules/rpcfiles.ts) and the WAV converter (dksample.wasm) against a running
// Digitakt II emulator, through its control socket (the `midi.usb.in` / `midi.usb.out` the desktop page reaches with `emu_call`). A dev tool: the app
// starts no process. The scenario `samples-live` (tests/digikit/scenarios) starts the emulator and runs this; by hand:
//
//     node tools/devtools/samples_live.mjs SOCKET --out DIR
//
// It converts a small WAV (44.1 kHz, so the resampler runs), uploads it in three chunks into a folder, closes with the byte count, lists the folder,
// compares the firmware's content hash with the host's (and a corrupted copy, which must fail), passes a native file and a text file through, and
// writes WAV and sent bytes to DIR for the scenario's card comparison. One JSON line per check on stdout; exit code 0 when all hold.
import net from 'node:net';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { DkSession, splitMessages } from '../../apps/web/src/modules/dkupload-core.ts';
import { Sample, prepareUpload } from '../../apps/web/src/modules/dksample.ts';
import { RpcFiles, putVerified, verifyWritten } from '../../apps/web/src/modules/rpcfiles.ts';

const [socketPath, ...flags] = process.argv.slice(2);
const out = flags[flags.indexOf('--out') + 1];
if (!socketPath || !out) {
  console.error('usage: node tools/devtools/samples_live.mjs SOCKET --out DIR');
  process.exit(2);
}
mkdirSync(out, { recursive: true });

// ---- JSON-RPC over the control socket ----
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

class Transport {
  rx = new Uint8Array();
  ready = [];
  async send(msg) {
    await call('midi.usb.in', { bytes: Buffer.from(msg).toString('hex'), chunk: 384, period: 16500 });
  }
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
  }
}

let failed = 0;
const check = (name, ok, detail = '') => {
  if (!ok) failed++;
  console.log(JSON.stringify({ check: name, ok: !!ok, detail: String(detail) }));
};
const same = (a, b) => Buffer.from(a).equals(Buffer.from(b));

// a 44.1 kHz mono 16-bit WAV of 0.1 s: 4410 frames become 4800 at 48 kHz
const wav = (() => {
  const frames = 4410;
  const pcm = Buffer.alloc(frames * 2);
  for (let i = 0; i < frames; i++)
    pcm.writeInt16LE(Math.round(12000 * Math.sin((2 * Math.PI * 440 * i) / 44100)), i * 2);
  const h = Buffer.alloc(44);
  h.write('RIFF', 0);
  h.writeUInt32LE(36 + pcm.length, 4);
  h.write('WAVEfmt ', 8);
  h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20);
  h.writeUInt16LE(1, 22);
  h.writeUInt32LE(44100, 24);
  h.writeUInt32LE(88200, 28);
  h.writeUInt16LE(2, 32);
  h.writeUInt16LE(16, 34);
  h.write('data', 36);
  h.writeUInt32LE(pcm.length, 40);
  return Uint8Array.from(Buffer.concat([h, pcm]));
})();
writeFileSync(join(out, 'test.wav'), wav);

const sample = await Sample.create(readFileSync(new URL('../../apps/web/public/dksample.wasm', import.meta.url)));
const session = new DkSession(new Transport(), { timeoutMs: 20000 });
const files = new RpcFiles(session, { chunk: 4096 });
const hashOf = (b) => sample.hash(b);

const space = await files.space();
check(
  'StorageSpace of the +Drive: free and total',
  space.free > 0n && space.total >= space.free,
  `${space.free} of ${space.total}`,
);
check('the folder is not there yet', !(await files.readDir('/')).some((e) => e.name === 'live'));

// the conversion
const prep = prepareUpload(sample, 'test.wav', wav);
check(
  'a WAV is converted to a native sample named without .wav',
  prep.kind === 'converted' && prep.name === 'test',
  `${prep.kind} ${prep.name}`,
);
check(
  '44.1 kHz mono: 4410 frames become 4800, the file is header + PCM',
  prep.info.srcFrames === 4410 && prep.info.frames === 4800 && prep.bytes.length === 0x40 + 9600,
  JSON.stringify(prep.info),
);
const converted = sample.convert(wav);
check(
  "what goes over MidiRpc is the card builder's file without its trailer",
  same(prep.bytes, converted.file.subarray(0, converted.file.length - 16)) &&
    converted.file.length === prep.bytes.length + 16,
);
writeFileSync(join(out, 'sent-test.bin'), prep.bytes);

// the upload: three chunks, close with the byte count, the folder listing, the hash
const progress = [];
const { closed, verified } = await putVerified(files, 'live/test', prep.bytes, hashOf, (sent) => progress.push(sent));
check('the upload took three chunks', progress.length === 3 && progress[2] === prep.bytes.length, progress.join(' '));
check('the close answers the size without the trailer', closed.size === prep.bytes.length, JSON.stringify(closed));
const listing = await files.readDir('/live');
const entry = listing.find((e) => e.name === 'test');
check(
  'ReadDir lists it as a file with the size sent',
  entry?.kind === 'F' && entry.size === prep.bytes.length,
  JSON.stringify(entry),
);
check(
  'the firmware hash|1 equals the host hash over the bytes sent and a zero trailer',
  verified.ok && verified.firmware === verified.host && verified.firmware === entry?.hash,
  `${verified.firmware} ${verified.host}`,
);
console.log(
  JSON.stringify({ hash: verified.firmware, size: verified.size, path: '/live/test', compared: verified.compared }),
);
const info = await files.info('/live/test');
check(
  'GetFileInfoV2 agrees with ReadDir',
  info.found && info.size === entry.size && info.hash === entry.hash,
  JSON.stringify(info),
);

// the host sees a mismatch: one byte of what it believes it sent is different
const wrong = Uint8Array.from(prep.bytes);
wrong[0x40 + 100] ^= 1;
const bad = await verifyWritten(files, '/live/test', wrong, hashOf);
check(
  'a different content hashes differently: verify reports failed',
  !bad.ok && bad.firmware !== bad.host && bad.size === wrong.length,
  `${bad.firmware} ${bad.host}`,
);
const missing = await verifyWritten(files, '/live/nothing', wrong, hashOf);
check('a path the device does not have reports failed', !missing.ok && missing.firmware === null);

// a native file passes through (its trailer is dropped: the firmware adds its own), any other file is unchanged
const native = prepareUpload(sample, 'again', converted.file);
check('a native file passes through without its trailer', native.kind === 'native' && same(native.bytes, prep.bytes));
const text = new TextEncoder().encode('digikit notes\n');
const plain = prepareUpload(sample, 'notes.txt', text);
check('a text file is not converted', plain.kind === 'unchanged' && plain.bytes === text && plain.name === 'notes.txt');
const second = await putVerified(files, 'live/notes.txt', plain.bytes, hashOf);
check(
  'the text file is uploaded and its hash verifies',
  second.verified.ok && second.closed.size === text.length,
  JSON.stringify(second.verified),
);

// remove and rename
await files.rename('/live/notes.txt', '/live/renamed.txt');
check(
  'RenameFile moves it',
  (await files.readDir('/live')).some((e) => e.name === 'renamed.txt'),
);
await files.remove('/live/renamed.txt');
check('DeleteFile removes it', !(await files.readDir('/live')).some((e) => e.name === 'renamed.txt'));
const after = await files.space();
check('StorageSpace shrank by the file', after.free < space.free, `${space.free} -> ${after.free}`);

sock.end();
process.exit(failed ? 1 : 0);
