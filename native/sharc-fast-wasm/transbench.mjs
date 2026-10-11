#!/usr/bin/env node
// Cost of one kernel-to-kernel transition in V8: a host call per kernel (the
// engine round trip) against direct, indirect and br_table transfers inside
// one module. Usage: node transbench.mjs TRANSBENCH.wasm   (wasmgate/release/transbench OUT.wasm)
import fs from 'node:fs';
const { instance } = await WebAssembly.instantiate(fs.readFileSync(process.argv[2]), {});
const e = instance.exports;
const N = 20_000_000;
const time = (name, f) => {
  f(100_000); // warm
  const t0 = process.hrtime.bigint();
  f(N);
  const ns = Number(process.hrtime.bigint() - t0) / N;
  console.log(`${name.padEnd(34)} ${ns.toFixed(2)} ns per transition`);
};
time('JS -> wasm call per kernel', (n) => { let a = 0; for (let i = 0; i < n; i++) a += e.k0(0, 0); return a; });
time('direct call inside the module', e.direct);
time('call_indirect inside the module', e.indirect);
time('br_table dispatch (one function)', e.dispatch);
