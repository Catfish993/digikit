#!/usr/bin/env node
// Headless parity check of the coupled (ColdFire + SHARC+) browser core.
//
// Loads packages/web/.local/emulator-core-sharc.wasm, restores the DN2 ready session,
// plays a scenario from native/coupled/scenarios.json exactly as `coupled-run`
// does, and prints the SHA-256 of the PCM (f32le bytes of every value produced
// since the ready point, the same bytes PcmTap hashes). With a scenario that has
// expect_sha256 the exit code is 1 on a mismatch.
//
//   node tools/wasm_parity.mjs --scenario trig1-tap-release
//   node tools/wasm_parity.mjs --scenario exactness-100m --out wasm.f32
//
// Options (defaults match tools/digi_env.py; DIGI_EMU_* variables are honoured):
//   --core PATH        packages/web/.local/emulator-core-sharc.wasm
//   --syx PATH         Digitone_II_OS1.11.syx            (DIGI_EMU_SYX)
//   (READY = ready_dir of tools/ready_inputs.json, or DIGI_EMU_READY_DIR)
//   --dsp-image PATH   READY/digi-audio-dn2-image.bin (DIGI_EMU_DSP_IMAGE)
//   --dsp-state PATH   READY/digi-audio-m5.snap.dsp   (DIGI_EMU_DSP_STATE)
//   --cf-snapshot PATH READY/digi-audio-m5.snap       (DIGI_EMU_CF_SNAPSHOT)
//   --scenarios PATH   native/coupled/scenarios.json
//   --scenario NAME    scenario to play (required)
//   --seconds S        override the scenario length in audio seconds (like coupled-run)
//   --out PATH         write the raw f32le PCM (compare with coupled-run --wav x.f32)
//   --repo PATH        repo root (default: the parent of this script's directory, else cwd)
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const STEP_CHUNK = 250_000; // native/coupled STEP_CHUNK
const FRAMES_PER_SECOND = 1500;
const VALUES_PER_FRAME = 64; // 32 stereo samples per DSPI2 frame

const args = {};
for (let i = 2; i < process.argv.length; i++) {
  const a = process.argv[i];
  if (!a.startsWith('--')) throw new Error(`unexpected argument ${a}`);
  args[a.slice(2)] = process.argv[++i];
}
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(args.repo ?? (fs.existsSync(path.join(here, '..', 'native')) ? path.join(here, '..') : process.cwd()));
const env = process.env;
const ready = path.resolve(root, env.DIGI_EMU_READY_DIR ?? JSON.parse(fs.readFileSync(path.join(root, 'tools/ready_inputs.json'), 'utf8')).ready_dir);
const paths = {
  core: args.core ?? path.join(root, 'packages/web/.local/emulator-core-sharc.wasm'),
  syx: args.syx ?? env.DIGI_EMU_SYX ?? env.DIGI_COUPLED_SYX ?? path.join(root, 'Digitone_II_OS1.11.syx'),
  image: args['dsp-image'] ?? env.DIGI_EMU_DSP_IMAGE ?? path.join(ready, 'digi-audio-dn2-image.bin'),
  dsp: args['dsp-state'] ?? env.DIGI_EMU_DSP_STATE ?? path.join(ready, 'digi-audio-m5.snap.dsp'),
  snap: args['cf-snapshot'] ?? env.DIGI_EMU_CF_SNAPSHOT ?? path.join(ready, 'digi-audio-m5.snap'),
  scenarios: args.scenarios ?? path.join(root, 'native/coupled/scenarios.json'),
};
if (!args.scenario) throw new Error('--scenario NAME is required');
const scenario = JSON.parse(fs.readFileSync(paths.scenarios, 'utf8')).scenarios.find((s) => s.name === args.scenario);
if (!scenario) throw new Error(`no scenario ${args.scenario} in ${paths.scenarios}`);

const { instance } = await WebAssembly.instantiate(fs.readFileSync(paths.core), {});
const e = instance.exports;
const mem = () => e.memory.buffer;
const json = () => JSON.parse(new TextDecoder().decode(new Uint8Array(mem(), e.digi_result_ptr(), e.digi_result_len())));
const call = (name, ...a) => {
  const c = e[name](...a);
  const v = json();
  if (c !== 0 || v.error) throw new Error(`${name}: ${v.error ?? c}`);
  return v.snapshot ?? v;
};
const put = (b) => {
  const p = e.digi_alloc(b.length);
  if (!p) throw new Error('digi_alloc failed');
  new Uint8Array(mem(), p, b.length).set(b);
  return p;
};

const bufs = Object.fromEntries(['syx', 'image', 'dsp', 'snap'].map((k) => [k, fs.readFileSync(paths[k])]));
const ptrs = Object.fromEntries(Object.entries(bufs).map(([k, b]) => [k, put(b)]));
const t0 = performance.now();
const first = call('digi_load_coupled', ptrs.syx, bufs.syx.length, ptrs.image, bufs.image.length, ptrs.dsp, bufs.dsp.length, ptrs.snap, bufs.snap.length);
const loadS = (performance.now() - t0) / 1000;
for (const k of Object.keys(ptrs)) e.digi_dealloc(ptrs[k], bufs[k].length);
if (!first.status.ready) throw new Error('the restored snapshot is not at ready');
const readyAt = first.status.icount;
console.error(`scenario ${scenario.name}: load ${loadS.toFixed(2)} s, ready at icount ${readyAt}`);

// The QA prelude of native/boot/examples/common (Script::poll): NO down at
// ready, up at +1M, down at +20M, up at +21M, encoder A +1 at +40M. It is
// polled after each chunk, before the scenario's own events of the next loop.
const qa = scenario.qa_script ? { stage: 1 } : null;
if (qa) call('digi_button', 12, 1);
const qaPoll = (icount) => {
  if (!qa) return;
  const elapsed = icount - readyAt;
  for (const [stage, when, act] of [[1, 1e6, 0], [2, 20e6, 1], [3, 21e6, 0], [4, 40e6, null]]) {
    if (qa.stage === stage && elapsed >= when) {
      if (act === null) call('digi_turn', 1, 1);
      else call('digi_button', 12, act);
      qa.stage = stage + 1;
    }
  }
};
qaPoll(readyAt);

// at_s events count audio seconds as DSPI2 frames sent / 1500.
const sortKey = (ev) => (ev.at_instr !== undefined ? [0, ev.at_instr] : [1, Math.round(ev.at_s * FRAMES_PER_SECOND)]);
const events = [...(scenario.events ?? [])].sort((a, b) => {
  const [ka, na] = sortKey(a), [kb, nb] = sortKey(b);
  return ka - kb || na - nb;
});
const seconds = args.seconds !== undefined ? Number(args.seconds) : scenario.seconds;
const targetFrames = seconds === undefined ? null : Math.ceil(seconds * FRAMES_PER_SECOND);
const untilInstr = args.seconds !== undefined ? null : scenario.instructions ?? null;

const chunks = [];
let values = 0, icount = readyAt, next = 0, nChunks = 0;
const frames = () => Math.floor(values / VALUES_PER_FRAME);
const wallStart = performance.now();
let lastLog = wallStart;
for (;;) {
  while (next < events.length) {
    const ev = events[next];
    const due = ev.at_instr !== undefined ? icount - readyAt >= ev.at_instr : frames() >= Math.round(ev.at_s * FRAMES_PER_SECOND);
    if (!due) break;
    if (ev.button !== undefined) call('digi_button', ev.button, (ev.down ?? true) ? 1 : 0);
    else call('digi_turn', ev.turn, ev.detents ?? 1);
    next++;
  }
  const elapsed = icount - readyAt;
  if (untilInstr !== null ? elapsed >= untilInstr : frames() >= targetFrames) break;
  let budget = STEP_CHUNK;
  if (untilInstr !== null && scenario.clip_last_chunk) budget = Math.min(untilInstr - elapsed, STEP_CHUNK);
  const ev = events[next];
  if (scenario.exact_events && ev && ev.at_instr !== undefined && ev.at_instr > elapsed) budget = Math.min(budget, ev.at_instr - elapsed);
  const status = call('digi_step', budget).status;
  nChunks++;
  icount = status.icount;
  const n = e.digi_pcm_take();
  if (n) {
    chunks.push(new Uint8Array(mem(), e.digi_pcm_ptr(), n * 4).slice());
    values += n;
  }
  if (status.error) throw new Error(`step error: ${status.error}`);
  qaPoll(icount);
  if (performance.now() - lastLog > 30000) {
    lastLog = performance.now();
    console.error(`  +${((icount - readyAt) / 1e6).toFixed(0)}M cf, ${(values / 2 / 48000).toFixed(2)} s audio, wall ${((lastLog - wallStart) / 1000).toFixed(0)} s`);
  }
}
const wallS = (performance.now() - wallStart) / 1000;
const pcm = Buffer.concat(chunks);
const stats = call('digi_sharc_stats');
const audioS = values / 2 / 48000;
const sha = crypto.createHash('sha256').update(pcm).digest('hex');
console.log(`scenario=${scenario.name} cf_instr=${icount - readyAt} chunks=${nChunks} frames=${stats.frames} dsp_instr=${stats.dsp_instructions} halted=${stats.halted} missing_blocks=${stats.missing_blocks}`);
console.log(`pcm values=${values} audio=${audioS.toFixed(3)}s sha256=${sha}`);
console.log(`wall=${wallS.toFixed(1)}s load=${loadS.toFixed(2)}s real_time_factor=${(wallS / audioS).toFixed(1)}x slower than real time`);
if (args.out) fs.writeFileSync(args.out, pcm);
let rc = 0;
if (scenario.expect_sha256) {
  const ok = scenario.expect_sha256 === sha;
  console.log(`expect_sha256=${scenario.expect_sha256} ${ok ? 'MATCH' : 'MISMATCH'}`);
  rc = ok ? 0 : 1;
}
process.exit(rc);
