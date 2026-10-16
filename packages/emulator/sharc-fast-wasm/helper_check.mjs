// Runs the module that `cargo test -p sharc-fast-wasm` writes when WASM_HELPER_OUT=FILE is set:
//   node packages/emulator/sharc-fast-wasm/helper_check.mjs FILE
// The kernel is `ok = env.helper(st, 0x1234); guard ok` over 3 iterations. The host
// stub records each call and returns a status: non-zero keeps the kernel going,
// zero makes the guard leave (return code 1, exit index 3 stored at CTX_EXIT_K).
import fs from 'node:fs';
import assert from 'node:assert/strict';

const bytes = fs.readFileSync(process.argv[2]);
const module = new WebAssembly.Module(bytes);
const memory = new WebAssembly.Memory({ initial: 2 });
const calls = [];
let status = 1;
const instance = new WebAssembly.Instance(module, {
  env: {
    memory,
    helper: (st, rec) => {
      calls.push([st, rec]);
      return status;
    },
  },
});
const view = new DataView(memory.buffer);
const ctx = 0x1000,
  st = 0x2000;
const CTX_ITERS = 0,
  CTX_DONE = 4,
  CTX_EXIT_K = 8;
assert.deepEqual(
  WebAssembly.Module.imports(module).map((i) => `${i.module}.${i.name}:${i.kind}`),
  ['env.memory:memory', 'env.helper:function'],
);

view.setUint32(ctx + CTX_ITERS, 3, true);
assert.equal(instance.exports.k0(ctx, st), 0);
assert.deepEqual(calls, [
  [st, 0x1234],
  [st, 0x1234],
  [st, 0x1234],
]);
assert.equal(view.getUint32(ctx + CTX_DONE, true), 3);

calls.length = 0;
status = 0;
assert.equal(instance.exports.k0(ctx, st), 1);
assert.equal(calls.length, 1);
assert.equal(view.getUint32(ctx + CTX_EXIT_K, true), 3);
console.log('helper_check: ok');
