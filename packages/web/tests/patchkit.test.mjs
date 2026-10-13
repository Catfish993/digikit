// patchkit.wasm and the patchkit command line apply the same modules to the same stock image and must
// produce byte-identical output (and the same plan). Skips when the wasm, a stock image or the CLI is absent.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, mkdtempSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const wasmPath = join(root, 'packages/web/public/patchkit.wasm');
const images = {
  dt2: join(root, 'Digitakt_II_OS1.17.syx'),
  dn2: join(root, 'Digitone_II_OS1.12.syx'),
};
const need = [wasmPath, ...Object.values(images)].every(existsSync);
const py = (...args) => execFileSync('uv', ['run', 'python', ...args], { cwd: root, encoding: 'utf8', maxBuffer: 1 << 28 });

async function load() {
  const { instance } = await WebAssembly.instantiate(readFileSync(wasmPath), {});
  const x = instance.exports;
  const put = (bytes) => {
    const p = x.pk_alloc(bytes.length);
    new Uint8Array(x.memory.buffer, p, bytes.length).set(bytes);
    return [p, bytes.length];
  };
  const get = (ptr, len) => new Uint8Array(x.memory.buffer.slice(ptr(), ptr() + len()));
  const text = (ptr, len) => new TextDecoder().decode(get(ptr, len));
  return { x, put, get, text };
}

test('patchkit.wasm is small and carries no firmware', () => {
  if (!existsSync(wasmPath)) return;
  const bytes = readFileSync(wasmPath);
  assert.ok(statSync(wasmPath).size < 2 * 1024 * 1024);
  // no stock-image hash and no port: those are passed in at run time
  for (const f of readdirSync(join(root, 'devices/ports'))) {
    const sha = /syx_sha256 = "([0-9a-f]{64})"/.exec(readFileSync(join(root, 'devices/ports', f), 'utf8'))[1];
    assert.equal(Buffer.from(bytes).includes(Buffer.from(sha)), false, `${f} sha embedded`);
  }
});

test('wasm and CLI produce byte-identical output for fx/master-gain', { skip: !need }, async () => {
  const work = mkdtempSync(join(tmpdir(), 'pk-wasm-'));
  const bundlePath = join(work, 'bundle.json');
  py('tools/patchkit.py', 'bundle', '-o', bundlePath);
  const bundle = readFileSync(bundlePath);
  const { x, put, get, text } = await load();
  for (const [name, path] of Object.entries(images)) {
    const fw = readFileSync(path);
    const out = join(work, `${name}.syx`);
    const plan = join(work, `${name}.plan.json`);
    py('tools/patchkit.py', 'patch', '--firmware', path, '--modules', 'fx/master-gain', '-o', out, '--plan', plan);
    const [fp, fl] = put(fw);
    const [bp, bl] = put(bundle);
    const [ip, il] = put(Buffer.from('fx/master-gain'));
    const rc = x.pk_apply(fp, fl, bp, bl, ip, il);
    assert.equal(rc, 0, text(x.pk_err_ptr, x.pk_err_len));
    const wasmOut = Buffer.from(get(x.pk_out_ptr, x.pk_out_len));
    const cliOut = readFileSync(out);
    assert.equal(wasmOut.length, cliOut.length, `${name}: length`);
    assert.ok(wasmOut.equals(cliOut), `${name}: wasm and CLI output differ`);
    assert.equal(text(x.pk_plan_ptr, x.pk_plan_len), readFileSync(plan, 'utf8'), `${name}: plan.json differs`);
    // the page can verify what it made
    const [op, ol] = put(wasmOut);
    const [fbp, fbl] = put(fw);
    assert.equal(x.pk_verify(op, ol, bp, bl, fbp, fbl), 0, text(x.pk_err_ptr, x.pk_err_len));
    const report = JSON.parse(text(x.pk_plan_ptr, x.pk_plan_len));
    assert.equal(report.device, name);
    assert.ok(report.record.modules[0].startsWith('fx/master-gain@'));
  }
});

test('wasm and CLI produce byte-identical output for the machine framework (two machines)', { skip: !need }, async () => {
  const work = mkdtempSync(join(tmpdir(), 'pk-wasm-'));
  const bundlePath = join(work, 'bundle.json');
  py('tools/patchkit.py', 'bundle', '-o', bundlePath);
  const bundle = readFileSync(bundlePath);
  const { x, put, get, text } = await load();
  const mods = 'core/machines,machines/xslice,test/machine-clone';
  const fw = readFileSync(images.dt2);
  const out = join(work, 'dt2-machines.syx');
  const plan = join(work, 'dt2-machines.plan.json');
  py('tools/patchkit.py', 'patch', '--firmware', images.dt2, '--modules', mods, '-o', out, '--plan', plan);
  const [fp, fl] = put(fw);
  const [bp, bl] = put(bundle);
  const [ip, il] = put(Buffer.from(mods));
  assert.equal(x.pk_apply(fp, fl, bp, bl, ip, il), 0, text(x.pk_err_ptr, x.pk_err_len));
  assert.ok(Buffer.from(get(x.pk_out_ptr, x.pk_out_len)).equals(readFileSync(out)), 'wasm and CLI output differ');
  const report = JSON.parse(text(x.pk_plan_ptr, x.pk_plan_len));
  const entries = (name) => report.registries.find((r) => r.name === name).entries.map((e) => [e.id, e.index]);
  assert.deepEqual(entries('machines'), [['xslice', 7], ['xclone', 8]]);
  // Digitone II has core/machines, but XSLICE clones a Digitakt machine (MANUAL SLICE, type 6)
  const [dp, dl] = put(readFileSync(images.dn2));
  const [mp, ml] = put(Buffer.from('core/machines,machines/xslice'));
  assert.equal(x.pk_apply(dp, dl, bp, bl, mp, ml), 1);
  assert.match(text(x.pk_err_ptr, x.pk_err_len), /not compatible|outside the anchor|no registry/);
});

test('wasm and CLI produce byte-identical output for core/params and PLUCK', { skip: !need }, async () => {
  const work = mkdtempSync(join(tmpdir(), 'pk-wasm-'));
  const bundlePath = join(work, 'bundle.json');
  py('tools/patchkit.py', 'bundle', '-o', bundlePath);
  const bundle = readFileSync(bundlePath);
  const { x, put, get, text } = await load();
  const mods = 'core/dsp-voice,core/machines,core/params,machines/pluck';
  const fw = readFileSync(images.dt2);
  const out = join(work, 'dt2-pluck.syx');
  const plan = join(work, 'dt2-pluck.plan.json');
  py('tools/patchkit.py', 'patch', '--firmware', images.dt2, '--modules', mods, '-o', out, '--plan', plan);
  const [fp, fl] = put(fw);
  const [bp, bl] = put(bundle);
  const [ip, il] = put(Buffer.from(mods));
  assert.equal(x.pk_apply(fp, fl, bp, bl, ip, il), 0, text(x.pk_err_ptr, x.pk_err_len));
  assert.ok(Buffer.from(get(x.pk_out_ptr, x.pk_out_len)).equals(readFileSync(out)), 'wasm and CLI output differ');
  const report = JSON.parse(text(x.pk_plan_ptr, x.pk_plan_len));
  const entries = (name) => report.registries.find((r) => r.name === name).entries.map((e) => [e.id, e.index]);
  assert.deepEqual(entries('machines'), [['pluck', 7]]);
  assert.deepEqual(entries('params'), [
    ['pluck_decay', 275], ['pluck_pick', 276], ['pluck_bright', 277], ['pluck_color', 278],
  ]);
  // PLUCK builds for Digitone II too (device-filtered entries and objects): the same parity there
  const outDn2 = join(work, 'dn2-pluck.syx');
  py('tools/patchkit.py', 'patch', '--firmware', images.dn2, '--modules', mods, '-o', outDn2);
  const [dp, dl] = put(readFileSync(images.dn2));
  const [mp, ml] = put(Buffer.from(mods));
  assert.equal(x.pk_apply(dp, dl, bp, bl, mp, ml), 0, text(x.pk_err_ptr, x.pk_err_len));
  assert.ok(Buffer.from(get(x.pk_out_ptr, x.pk_out_len)).equals(readFileSync(outDn2)), 'DN2: wasm and CLI output differ');
});

test('wasm and CLI produce byte-identical output for the primitives (registry, section, hook)', { skip: !need }, async () => {
  const work = mkdtempSync(join(tmpdir(), 'pk-wasm-'));
  const bundlePath = join(work, 'bundle.json');
  py('tools/patchkit.py', 'bundle', '-o', bundlePath);
  const bundle = readFileSync(bundlePath);
  const { x, put, get, text } = await load();
  const out = join(work, 'dt2-rd.syx');
  const plan = join(work, 'dt2-rd.plan.json');
  py('tools/patchkit.py', 'patch', '--firmware', images.dt2, '--modules', 'test/registry-demo', '-o', out, '--plan', plan);
  const [fp, fl] = put(readFileSync(images.dt2));
  const [bp, bl] = put(bundle);
  const [ip, il] = put(Buffer.from('test/registry-demo'));
  assert.equal(x.pk_apply(fp, fl, bp, bl, ip, il), 0, text(x.pk_err_ptr, x.pk_err_len));
  assert.ok(Buffer.from(get(x.pk_out_ptr, x.pk_out_len)).equals(readFileSync(out)), 'wasm and CLI output differ');
  const wasmPlan = text(x.pk_plan_ptr, x.pk_plan_len);
  assert.equal(wasmPlan, readFileSync(plan, 'utf8'), 'plan.json differs');
  const j = JSON.parse(wasmPlan);
  assert.equal(j.registries[0].count, 2);
  assert.equal(j.sections[0].appended_rows, 2);
  assert.equal(j.hooks[0].displaced, 'run');
  // the demo is Digitakt only: its anchor does not exist on the Digitone II
  const [dp, dl] = put(readFileSync(images.dn2));
  assert.equal(x.pk_apply(dp, dl, bp, bl, ip, il), 0, text(x.pk_err_ptr, x.pk_err_len));
  assert.equal(JSON.parse(text(x.pk_plan_ptr, x.pk_plan_len)).hooks, undefined);
});

test('wasm refuses what the CLI refuses', { skip: !need }, async () => {
  const work = mkdtempSync(join(tmpdir(), 'pk-wasm-'));
  const bundlePath = join(work, 'bundle.json');
  // the DN2-only oracle is generated under out/modules (it holds a firmware word, so it is not committed)
  py('tools/pk_oracle_dn2_half.py');
  py('tools/patchkit.py', 'bundle', '-o', bundlePath, '--modules-dir', join(root, 'out', 'modules'));
  const { x, put, text } = await load();
  const [fp, fl] = put(readFileSync(images.dt2));
  const [bp, bl] = put(readFileSync(bundlePath));
  const [ip, il] = put(Buffer.from('oracle/dn2-master-half'));
  assert.equal(x.pk_apply(fp, fl, bp, bl, ip, il), 1);
  assert.match(text(x.pk_err_ptr, x.pk_err_len), /not compatible/);
});
