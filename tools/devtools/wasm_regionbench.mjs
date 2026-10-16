// Run the wasm32-wasip1 build of packages/emulator/sharc/examples/region_bench.rs under node or bun.
// Rows on stdout are the same JSON as the native example.
//   node tools/devtools/wasm_regionbench.mjs REGION_BENCH.wasm ARGS...
import { readFile } from 'node:fs/promises';
import { WASI } from 'node:wasi';

const [path, ...rawArgs] = process.argv.slice(2);
// bun's node:wasi opens absolute paths as <cwd>/<abs path> (ENOENT); relative paths work.
const cwdPrefix = process.cwd() + '/';
const args = process.versions.bun
  ? rawArgs.map((a) => (a.startsWith(cwdPrefix) ? a.slice(cwdPrefix.length) : a))
  : rawArgs;
const wasi = new WASI({
  version: 'preview1',
  args: ['region_bench', ...args],
  env: {},
  preopens: { '/': '/' },
});
const module = await WebAssembly.compile(await readFile(path));
const instance = await WebAssembly.instantiate(
  module,
  wasi.getImportObject ? wasi.getImportObject() : { wasi_snapshot_preview1: wasi.wasiImport },
);
process.exit(wasi.start(instance));
