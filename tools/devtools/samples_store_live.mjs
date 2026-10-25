#!/usr/bin/env node
// A live check of the generic samples API (apps/web/src/modules/samples.ts) against a running emulator, through its control socket. The backend is the device
// profile's `[storage] samples` value (`--storage`); the scenario `samples-store` runs this on the Digitone II, where it is "digikit": the samples go into the
// `samples/` folder of the digikit store through the runtime's upload commands, and VERIFY (the runtime hashes the stored file from a fresh card read) is compared
// with the host's sha256 of the bytes it sent. A dev tool: the app starts no process. By hand:
//
//     node tools/devtools/samples_store_live.mjs SOCKET --out DIR --storage digikit --total-kib N
//
// It converts a WAV, makes a folder, uploads the sample in several chunks, lists, verifies (and a corrupted copy, which must fail, and a path that is not there),
// deletes it (the space goes back up), uploads it again, restarts the device and checks that the store still lists and verifies it. One JSON line per check on stdout;
// the last line is the result; exit code 0 when all hold. The bytes stored are written to DIR for the scenario's comparison with the card mirror.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { DkSession, hexSha256 } from '../../packages/dk-session/src/index.ts';
import { Sample, prepareUpload } from '../../apps/web/src/modules/dksample.ts';
import { openSamples } from '../../apps/web/src/modules/samples.ts';
import { connect } from './emu_midi.mjs';

const [socketPath, ...flags] = process.argv.slice(2);
const flag = (name) => flags[flags.indexOf(name) + 1];
const out = flag('--out');
const storage = flag('--storage');
const totalKib = flag('--total-kib') ? Number(flag('--total-kib')) : null;
if (!socketPath || !out || !storage) {
  console.error('usage: node tools/devtools/samples_store_live.mjs SOCKET --out DIR --storage KIND [--total-kib N]');
  process.exit(2);
}
mkdirSync(out, { recursive: true });

let failed = 0;
const check = (name, ok, detail = '') => {
  if (!ok) failed++;
  console.log(JSON.stringify({ check: name, ok: !!ok, detail: String(detail) }));
};

// a 44.1 kHz stereo 16-bit WAV of 0.1 s: 4410 frames become 4800 at 48 kHz, 19200 bytes of PCM (five chunks of 4096)
const wav = (() => {
  const frames = 4410;
  const pcm = Buffer.alloc(frames * 4);
  for (let i = 0; i < frames; i++) {
    pcm.writeInt16LE(Math.round(12000 * Math.sin((2 * Math.PI * 440 * i) / 44100)), i * 4);
    pcm.writeInt16LE(Math.round(9000 * Math.sin((2 * Math.PI * 660 * i) / 44100)), i * 4 + 2);
  }
  const h = Buffer.alloc(44);
  h.write('RIFF', 0);
  h.writeUInt32LE(36 + pcm.length, 4);
  h.write('WAVEfmt ', 8);
  h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20);
  h.writeUInt16LE(2, 22);
  h.writeUInt32LE(44100, 24);
  h.writeUInt32LE(176400, 28);
  h.writeUInt16LE(4, 32);
  h.writeUInt16LE(16, 34);
  h.write('data', 36);
  h.writeUInt32LE(pcm.length, 40);
  return Uint8Array.from(Buffer.concat([h, pcm]));
})();

const emu = await connect(socketPath);
const sample = await Sample.create(readFileSync(new URL('../../apps/web/public/dksample.wasm', import.meta.url)));
const session = new DkSession(emu.transport, { timeoutMs: 20000, chunk: 4096 });
const dev = openSamples(storage, { session, hashOf: (b) => sample.hash(b) });
check(`the backend for [storage] samples = "${storage}"`, dev.kind === storage, dev.kind);

const prep = prepareUpload(sample, 'test.wav', wav);
check(
  'a WAV is converted to the native sample file (header, 4800 stereo frames, trailer)',
  prep.kind === 'converted' && prep.name === 'test' && prep.stored.length === 0x40 + 19200 + 16,
  `${prep.kind} ${prep.name} ${prep.stored.length}`,
);
writeFileSync(join(out, 'stored-test.bin'), prep.stored);

const space0 = await dev.space();
check(
  'free space and total from the device',
  space0.free > 0 && space0.total !== null && (totalKib === null || space0.total === totalKib * 1024),
  JSON.stringify(space0),
);
check('the folder is not there yet', !(await dev.list('')).some((e) => e.name === 'drums'));
check('folders can be made', await dev.canMkdir());
await dev.mkdir('drums');
check(
  'MKDIR: the empty folder is listed',
  (await dev.list('')).some((e) => e.name === 'drums' && e.kind === 'folder'),
);

const progress = [];
await dev.put('drums', prep, (sent, total) => progress.push([sent, total]));
check(
  'the upload took several chunks and ended at the file size',
  progress.length >= 3 && progress.at(-1)[0] === prep.stored.length && progress.at(-1)[1] === prep.stored.length,
  progress.map((p) => p[0]).join(' '),
);
const listing = await dev.list('drums');
check(
  'the folder lists the file with the size sent',
  listing.length === 1 &&
    listing[0].name === 'test' &&
    listing[0].kind === 'file' &&
    listing[0].size === prep.stored.length,
  JSON.stringify(listing),
);
check(
  'the folder entry went when a file was stored in it, the folder is still listed',
  (await dev.list('')).some((e) => e.name === 'drums' && e.kind === 'folder'),
);
const space1 = await dev.space();
check('the file took space', space1.free < space0.free, `${space0.free} -> ${space1.free}`);

const ok = await dev.verify('drums', prep);
check(
  'VERIFY: the device sha256 of the stored file equals the host sha256 of the bytes sent',
  ok.ok &&
    ok.device === ok.host &&
    ok.message === 'device sha256 matches' &&
    ok.host === (await hexSha256(prep.stored)),
  JSON.stringify(ok),
);
const wrong = { ...prep, stored: Uint8Array.from(prep.stored) };
wrong.stored[0x40 + 100] ^= 1;
const bad = await dev.verify('drums', wrong);
check(
  'a different content reports failed',
  !bad.ok && bad.device === ok.host && bad.host !== ok.host && bad.message === 'device sha256 differs',
  JSON.stringify(bad),
);
const none = await dev.verify('drums', { ...prep, name: 'nothing' });
check('a path the device does not have reports failed', !none.ok && none.device === null, none.message);

await dev.remove('drums', 'test');
check('the delete removes the file', (await dev.list('drums')).length === 0);
const space2 = await dev.space();
check(
  'the delete freed the space',
  space2.free === space0.free && space2.free > space1.free,
  `${space1.free} -> ${space2.free}`,
);
await dev.put('drums', prep);
check('the file goes in again and verifies', (await dev.verify('drums', prep)).ok);
check('the space is the same as after the first upload', (await dev.space()).free === space1.free);

// the store survives a restart: the guest's card writes are kept, the runtime finds its store again
await session.restart();
emu.transport.rx = new Uint8Array();
emu.transport.ready = [];
await session.waitForBoot();
let again;
for (let i = 0; i < 40 && !again; i++) {
  try {
    again = await dev.list('drums');
  } catch {
    await new Promise((r) => setTimeout(r, 1000));
  }
}
check(
  'after the restart the file is still listed',
  again?.length === 1 && again[0].name === 'test' && again[0].size === prep.stored.length,
  JSON.stringify(again),
);
const after = await dev.verify('drums', prep);
check('after the restart VERIFY still matches', after.ok && after.device === after.host, JSON.stringify(after));
check('after the restart the space is the same', (await dev.space()).free === space1.free);

console.log(
  JSON.stringify({
    path: 'samples/drums/test',
    sha256: after.host,
    size: prep.stored.length,
    compared: after.compared,
  }),
);
emu.close();
process.exit(failed ? 1 : 0);
