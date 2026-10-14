#!/usr/bin/env node
// V8 gate for the wasm kernel backend: replays the calls of a SHARC_FAST_DUMP
// directory (packages/emulator/sharc/src/fast/dump.rs) through the modules wasmgen wrote,
// checks each against the recorded result, and times them.
//
//   node [--no-liftoff | --liftoff-only] gate.mjs DUMP_DIR WASM_DIR [budget_ms]
//
// Same replay rules as src/bin/clbench.rs (the native column): the context and
// register words are restored before every repetition, window memory is not.
import fs from 'node:fs';

const [dump, wasmDir, budgetArg] = process.argv.slice(2);
const BUDGET_MS = Number(budgetArg ?? 30);
const CTX_WIN = 16;
const MAX_WIN = 12;
const CTX_BASE = 0x10000;
const ST_BASE = 0x40000;
const ARENA_BASE = 0x80000;

function parseCall(buf) {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  let o = 0;
  const u32 = () => { const v = dv.getUint32(o, true); o += 4; return v; };
  const bytes = (n) => { const v = buf.subarray(o, o + n); o += n; return v; };
  if (u32() !== 0x31504143) throw new Error('bad capture');
  const kid = (BigInt(u32()) | (BigInt(u32()) << 32n)).toString(16).padStart(16, '0');
  const insns = u32() + u32() * 2 ** 32;
  const code = u32();
  const ctxLen = u32(), stOff = u32(), stLen = u32(), nwin = u32();
  const c = { kid, insns, code, stOff };
  c.ctxPre = bytes(ctxLen); c.ctxPost = bytes(ctxLen);
  c.stPre = bytes(stLen); c.stPost = bytes(stLen);
  c.wins = [];
  for (let i = 0; i < nwin; i++) {
    const w = u32(), lo = u32(), len = u32(), hlo = u32(), hhi = u32();
    const host = BigInt(hlo) | (BigInt(hhi) << 32n);
    c.wins.push({ w, lo, len, host, pre: bytes(len), post: bytes(len) });
  }
  return c;
}

const eq = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

const calls = fs.readdirSync(dump).filter((n) => n.startsWith('call-')).sort()
  .map((n) => ({ name: n, ...parseCall(fs.readFileSync(`${dump}/${n}`)) }));

// Compile every kernel module once (timed) -------------------------------------
const mods = new Map();
let compileMs = 0;
const kids = [...new Set(calls.map((c) => c.kid))];
for (const kid of kids) {
  const bytes = fs.readFileSync(`${wasmDir}/kernel-${kid}.wasm`);
  const t0 = performance.now();
  mods.set(kid, new WebAssembly.Module(bytes));
  compileMs += performance.now() - t0;
}
console.log(`compiled ${kids.length} kernels: ${compileMs.toFixed(1)} ms total, ${(compileMs / kids.length * 1000).toFixed(0)} us per kernel (sync WebAssembly.Module)`);

const mem = new WebAssembly.Memory({ initial: 64 }); // 4 MiB
let bad = 0, totNs = 0, totInsns = 0, firstBad = null;
const lines = [];
for (const c of calls) {
  const inst = new WebAssembly.Instance(mods.get(c.kid), { env: { memory: mem } });
  const run = inst.exports.k0;
  // Arenas: windows that overlap in host memory share one region.
  const order = c.wins.map((_, i) => i).sort((a, b) => (c.wins[a].host < c.wins[b].host ? -1 : 1));
  const arenas = [];
  const place = [];
  let next = ARENA_BASE;
  for (const i of order) {
    const w = c.wins[i];
    const last = arenas[arenas.length - 1];
    if (last && w.host < last.host + BigInt(last.len)) {
      const end = Number(w.host - last.host) + w.len;
      if (end > last.len) { next += end - last.len; last.len = end; }
    } else {
      next = (next + 15) & ~15;
      arenas.push({ host: w.host, len: w.len, at: next });
      next += w.len;
    }
    const a = arenas[arenas.length - 1];
    place[i] = a.at + Number(w.host - a.host);
  }
  if (next > mem.buffer.byteLength) mem.grow(Math.ceil((next - mem.buffer.byteLength) / 65536) + 1);
  const m8 = new Uint8Array(mem.buffer);
  const dv = new DataView(mem.buffer);
  for (const i of order) m8.set(c.wins[i].pre, place[i]);
  const setup = () => {
    m8.set(c.ctxPre, CTX_BASE);
    c.wins.forEach((w, i) => dv.setBigUint64(CTX_BASE + CTX_WIN + 8 * w.w, BigInt.asUintN(64, BigInt(place[i]) - BigInt(w.lo)), true));
    m8.set(c.stPre, ST_BASE + c.stOff);
  };
  setup();
  const code = run(CTX_BASE, ST_BASE);
  const winLo = CTX_WIN, winHi = CTX_WIN + 8 * MAX_WIN;
  const ctxNow = m8.subarray(CTX_BASE, CTX_BASE + c.ctxPre.length);
  let ok = code === c.code
    && eq(ctxNow.subarray(0, winLo), c.ctxPost.subarray(0, winLo))
    && eq(ctxNow.subarray(winHi), c.ctxPost.subarray(winHi))
    && eq(m8.subarray(ST_BASE + c.stOff, ST_BASE + c.stOff + c.stPost.length), c.stPost);
  c.wins.forEach((w, i) => { ok = ok && eq(m8.subarray(place[i], place[i] + w.len), w.post); });
  if (!ok) { bad++; firstBad ??= c.name; }
  // Warm up (tier-up), then time: restore ctx/regs each repetition.
  const warmEnd = performance.now() + 60;
  while (performance.now() < warmEnd) { for (let r = 0; r < 16; r++) { setup(); run(CTX_BASE, ST_BASE); } }
  let reps = 0;
  const t0 = process.hrtime.bigint();
  const stop = performance.now() + BUDGET_MS;
  while (performance.now() < stop) { for (let r = 0; r < 16; r++) { setup(); run(CTX_BASE, ST_BASE); reps++; } }
  const total = Number(process.hrtime.bigint() - t0);
  const t1 = process.hrtime.bigint();
  for (let r = 0; r < reps; r++) setup();
  const ns = (total - Number(process.hrtime.bigint() - t1)) / reps;
  lines.push(`${c.kid}\t${c.insns}\t${ns.toFixed(0)}\t${(ns / c.insns).toFixed(3)}\t${ok}`);
  totNs += ns; totInsns += c.insns;
}
console.log(lines.join('\n'));
console.log(`TOTAL wasm/V8: ${totInsns} insns, ${(totNs / totInsns).toFixed(3)} ns/insn (call + setup subtracted), ${bad} mismatches${firstBad ? ' (first ' + firstBad + ')' : ''}`);
