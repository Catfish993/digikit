#!/usr/bin/env node
// V8 compile cost of the wasm kernels the fast tier would load.
//
//   SHARC_FAST_TL=1 SHARC_FAST_TL_DUMP=KDUMP coupled-run ...   (pointer-free kernels)
//   wasmgen KDUMP WASM_DIR
//   node tools/devtools/wasm_compile_bench.mjs WASM_DIR [limit]
//
// Reports, per kernel: the synchronous `new WebAssembly.Module` time (what a
// blocked worker pays today, `host_wasm_compile`), the p50/p99/max, and the
// wall time of compiling every module with the async `WebAssembly.compile`
// (V8 compiles off the calling thread). Also instantiates a sample over a
// shared memory and serialises a module through structured clone to test
// that a Module can be handed to another thread.
import fs from 'node:fs';
import { Worker, isMainThread, parentPort, workerData } from 'node:worker_threads';

if (!isMainThread) {
  const scratch = new WebAssembly.Memory({ initial: 64 });
  // Compile worker: receives bytes, returns the Module (structured clone).
  parentPort.on('message', async (m) => {
    const t0 = performance.now();
    const mod = await WebAssembly.compile(m.bytes);
    if (m.warm) {
      // Run the function once on scratch memory so V8 compiles it here.
      const inst = new WebAssembly.Instance(mod, { env: { memory: scratch } });
      try { inst.exports.k0(0x10000, 0x40000); } catch (e) { /* trap ignored */ }
    }
    parentPort.postMessage({ id: m.id, mod, ms: performance.now() - t0 });
  });
} else {
  const [dir, limitArg] = process.argv.slice(2);
  const names = fs.readdirSync(dir).filter((n) => n.startsWith('kernel-') && n.endsWith('.wasm')).sort();
  const limit = Number(limitArg ?? names.length);
  const files = names.slice(0, limit).map((n) => fs.readFileSync(`${dir}/${n}`));
  const q = (xs, p) => [...xs].sort((a, b) => a - b)[Math.min(xs.length - 1, Math.floor(xs.length * p))];
  const bytes = files.reduce((a, f) => a + f.length, 0);
  console.log(`${files.length} modules, ${(bytes / 1e6).toFixed(1)} MB, mean ${(bytes / files.length / 1024).toFixed(1)} KB`);

  // V8 caches compiled modules by their bytes, so each measurement needs its
  // own process: ONLY=sync | async | worker.
  const only = process.env.ONLY ?? 'sync';
  if (only === 'async') {
    setInterval(() => {}, 50); // keep the loop alive while V8 compiles in the background
    const t1 = performance.now();
    for (let i = 0; i < files.length; i += 64) await Promise.all(files.slice(i, i + 64).map((f) => WebAssembly.compile(f)));
    console.log(`async WebAssembly.compile of all, 64 in flight (V8 background threads): wall ${((performance.now() - t1) / 1e3).toFixed(2)} s`);
    process.exit(0);
  }
  if (only === 'worker') {
    const w = new Worker(new URL(import.meta.url));
    const t2 = performance.now();
    const done = new Promise((resolve) => {
      let n = 0;
      w.on('message', (m) => {
        if (!(m.mod instanceof WebAssembly.Module)) throw new Error('not a Module');
        if (++n === files.length) resolve();
      });
    });
    files.forEach((f, id) => w.postMessage({ id, bytes: f }));
    await done;
    console.log(`compile worker + postMessage(Module) of ${files.length}: wall ${((performance.now() - t2) / 1e3).toFixed(2)} s`);
    process.exit(0);
  }

  if (only === 'warm' || only === 'cold') {
    // Compile (and for 'warm', call once) in a worker, hand the Module over,
    // and time the first call here: is the lazily compiled code shared?
    const w = new Worker(new URL(import.meta.url));
    const mods = new Array(files.length);
    const t0 = performance.now();
    const done = new Promise((resolve) => {
      let n = 0;
      w.on('message', (m) => { mods[m.id] = m.mod; if (++n === files.length) resolve(); });
    });
    files.forEach((f, id) => w.postMessage({ id, bytes: f, warm: only === 'warm' }));
    await done;
    const wall = performance.now() - t0;
    const mem = new WebAssembly.Memory({ initial: 64 });
    const call = [];
    for (const m of mods) {
      const inst = new WebAssembly.Instance(m, { env: { memory: mem } });
      const t = performance.now();
      try { inst.exports.k0(0x10000, 0x40000); } catch (e) { /* trap ignored */ }
      call.push(performance.now() - t);
    }
    const sum = (xs) => xs.reduce((a, b) => a + b, 0);
    console.log(`${only}: worker wall ${(wall / 1e3).toFixed(2)} s; first call here mean ${(sum(call) / call.length).toFixed(3)} ms p99 ${q(call, 0.99).toFixed(3)} max ${Math.max(...call).toFixed(2)} ms`);
    process.exit(0);
  }

  if (only === 'cross') {
    // Does a module compiled in a worker make the same bytes cheap to
    // construct here (V8's native module cache is keyed by the wire bytes)?
    const w = new Worker(new URL(import.meta.url));
    const done = new Promise((resolve) => {
      let n = 0;
      w.on('message', () => { if (++n === files.length) resolve(); });
    });
    files.forEach((f, id) => w.postMessage({ id, bytes: f }));
    await done;
    const ctor = [], call = [];
    const mem = new WebAssembly.Memory({ initial: 64 });
    for (const f of files) {
      let t = performance.now();
      const m = new WebAssembly.Module(f);
      ctor.push(performance.now() - t);
      const inst = new WebAssembly.Instance(m, { env: { memory: mem } });
      t = performance.now();
      try { inst.exports.k0(0x10000, 0x40000); } catch (e) { /* trap ignored */ }
      call.push(performance.now() - t);
    }
    const sum = (xs) => xs.reduce((a, b) => a + b, 0);
    console.log(`after worker compile: constructor mean ${(sum(ctor) / ctor.length).toFixed(3)} ms max ${Math.max(...ctor).toFixed(2)}; first call mean ${(sum(call) / call.length).toFixed(3)} ms p99 ${q(call, 0.99).toFixed(3)} max ${Math.max(...call).toFixed(2)} ms`);
    process.exit(0);
  }

  // 1. Synchronous constructor, one by one.
  const sync = [];
  const mods = [];
  const t0 = performance.now();
  for (const f of files) {
    const t = performance.now();
    mods.push(new WebAssembly.Module(f));
    sync.push(performance.now() - t);
  }
  const total = performance.now() - t0;
  console.log(
    `sync new WebAssembly.Module: total ${(total / 1e3).toFixed(2)} s, per kernel ` +
      `mean ${(total / files.length).toFixed(3)} ms p50 ${q(sync, 0.5).toFixed(3)} p99 ${q(sync, 0.99).toFixed(3)} max ${Math.max(...sync).toFixed(2)} ms; ` +
      `> 0.667 ms: ${sync.filter((x) => x > 0.667).length}`,
  );

  // 4. Instantiate over a memory and make the first call (V8 compiles a
  //    function lazily on its first call unless --no-wasm-lazy-compilation).
  const mem = new WebAssembly.Memory({ initial: 64 });
  const first = [];
  let ok = 0, trapped = 0;
  for (const m of mods) {
    let inst;
    try { inst = new WebAssembly.Instance(m, { env: { memory: mem } }); } catch (e) { continue; }
    ok++;
    const t = performance.now();
    try { inst.exports.k0(0x10000, 0x40000); } catch (e) { trapped++; }
    first.push(performance.now() - t);
  }
  const ft = first.reduce((a, b) => a + b, 0);
  console.log(
    `instantiate ${ok}/${mods.length}, first call (lazy compile): total ${(ft / 1e3).toFixed(2)} s, ` +
      `mean ${(ft / first.length).toFixed(3)} ms p50 ${q(first, 0.5).toFixed(3)} p99 ${q(first, 0.99).toFixed(3)} max ${Math.max(...first).toFixed(2)} ms; ` +
      `> 0.667 ms: ${first.filter((x) => x > 0.667).length}; traps ${trapped}`,
  );
  process.exit(0);
}
