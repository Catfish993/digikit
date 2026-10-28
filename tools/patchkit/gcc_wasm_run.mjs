// Run a wasm32-wasip1 GNU tool (cc1 or as) under node's WASI: node gcc_wasm_run.mjs TOOL.wasm PREOPEN[,PREOPEN] -- ARGS...
// PREOPEN is HOST or HOST::GUEST. Prints wall time and peak RSS to stderr.
import { readFileSync } from 'node:fs';
import { WASI } from 'node:wasi';

const argv = process.argv.slice(2);
const sep = argv.indexOf('--');
const [wasmPath, preopens] = argv.slice(0, sep);
const args = argv.slice(sep + 1);
const map = {};
for (const p of preopens.split(',')) {
  const [h, g] = p.split('::');
  map[g ?? h] = h;
}
const t0 = performance.now();
const mod = await WebAssembly.compile(readFileSync(wasmPath));
const t1 = performance.now();
const wasi = new WASI({ version: 'preview1', args: ['tool', ...args], preopens: map, returnOnExit: true });
const inst = await WebAssembly.instantiate(mod, wasi.getImportObject());
const rc = wasi.start(inst);
const t2 = performance.now();
console.error(
  `compile ${(t1 - t0).toFixed(0)} ms, run ${(t2 - t1).toFixed(0)} ms, rss ${(process.resourceUsage().maxRSS / 1024).toFixed(0)} MB`,
);
process.exitCode = rc;
