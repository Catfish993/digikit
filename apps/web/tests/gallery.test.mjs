// The gallery's build step (scripts/gen-modules.mjs), the selection rules and the patch worker's core.
// The apply tests compare with the patchkit command line and skip when the wasm or a stock image is absent.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, mkdtempSync, readdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'smol-toml';
import { buildModuleData } from '../scripts/gen-modules.mjs';
import { PatchKit, patchedFileName } from '../src/patch/patchkit-core.ts';
import { clash, selectionProblems, withRequired, withoutDependents } from '../src/patch/selection.ts';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const wasmPath = join(root, 'apps/web/public/patchkit.wasm');
const images = {
  dt2: join(root, 'firmware', 'Digitakt_II_OS1.17.syx'),
  dn2: join(root, 'firmware', 'Digitone_II_OS1.12.syx'),
};
const have = [wasmPath, ...Object.values(images)].every(existsSync);
const py = (module, ...args) =>
  execFileSync('uv', ['run', 'python', '-m', module, ...args], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: root },
    encoding: 'utf8',
    maxBuffer: 1 << 28,
  });

const manifestFiles = (dir = join(root, 'modules')) =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? manifestFiles(join(dir, e.name)) : e.name === 'module.toml' ? [join(dir, e.name)] : [],
  );

test('bundle lists the committed modules and ports', () => {
  const { bundle } = buildModuleData(root);
  assert.equal(bundle.modules.length, manifestFiles().length);
  assert.ok(bundle.modules.some((m) => m.manifest.includes('id = "fx/master-gain"')));
  assert.equal(bundle.ports.length, readdirSync(join(root, 'devices/ports')).length);
  // committed content only: nothing generated under out/ and no firmware-sized blobs
  const json = JSON.stringify(bundle);
  assert.ok(!json.includes('out/modules'));
  assert.ok(json.length < 2 * 1024 * 1024);
});

test('public module summaries are one short sentence', () => {
  for (const file of manifestFiles()) {
    const { module: m } = parse(readFileSync(file, 'utf8'));
    if (m.category === 'core' || m.category === 'test') continue;
    assert.ok(m.summary.length <= 120, `${m.id}: summary is longer than 120 characters`);
    assert.ok(m.summary.endsWith('.'), `${m.id}: summary does not end with a full stop`);
  }
});

test('bundle equals tools/patchkit/patchkit.py bundle', { skip: !existsSync(join(root, '.venv')) }, () => {
  const out = join(mkdtempSync(join(tmpdir(), 'gal-')), 'bundle.json');
  py('tools.patchkit.patchkit', 'bundle', '-o', out);
  assert.deepEqual(buildModuleData(root).bundle, JSON.parse(readFileSync(out, 'utf8')));
});

test('selection: requires are added, dependents removed, clashes explained', () => {
  const c = (id, extra = {}) => ({ id, name: id, requires: [], conflicts: [], claims: [], ...extra });
  const cards = [
    c('a', { requires: ['b'] }),
    c('b', { requires: ['d'] }),
    c('d'),
    c('x', { claims: ['hook'] }),
    c('y', { claims: ['hook'] }),
    c('z', { conflicts: ['d'] }),
  ];
  assert.deepEqual(withRequired(cards, [], 'a'), ['a', 'b', 'd']);
  assert.deepEqual(withoutDependents(cards, ['a', 'b', 'd'], 'd'), []);
  assert.deepEqual(withoutDependents(cards, ['a', 'b', 'd', 'x'], 'b'), ['d', 'x']);
  assert.match(clash(cards[3], cards[4]), /both use "hook"/);
  assert.match(clash(cards[5], cards[2]), /conflicts/);
  const p = selectionProblems(cards, ['x']);
  assert.deepEqual(p.conflicts, []);
  assert.match(p.blockers.get('y'), /hook/);
  assert.equal(p.blockers.has('a'), false);
  assert.match(selectionProblems(cards, ['d']).blockers.get('z'), /conflicts/);
  assert.equal(selectionProblems(cards, ['x', 'y']).conflicts.length, 1);
});

test('patched file names', () => {
  assert.equal(patchedFileName('Digitakt II', '1.17', ['fx/master-gain']), 'Digitakt_II_OS1.17+fx-master-gain.syx');
});

test('worker core: check and apply equal the command line (fx/master-gain)', { skip: !have }, async () => {
  const work = mkdtempSync(join(tmpdir(), 'gal-'));
  const { bundle } = buildModuleData(root);
  const kit = await PatchKit.create(readFileSync(wasmPath), JSON.stringify(bundle));
  for (const [name, path] of Object.entries(images)) {
    const fw = new Uint8Array(readFileSync(path));
    const checked = kit.check(fw);
    assert.equal(checked.device, name);
    assert.ok(checked.port?.startsWith(name));
    assert.equal(checked.modules.find((m) => m.id === 'fx/master-gain').ok, true);
    const out = join(work, `${name}.syx`);
    py('tools.patchkit.patchkit', 'patch', '--firmware', path, '--modules', 'fx/master-gain', '-o', out);
    const r = kit.apply(fw, ['fx/master-gain']);
    assert.ok(Buffer.from(r.syx).equals(readFileSync(out)), `${name}: output differs from the CLI`);
    assert.equal(r.plan.record.modules[0].split('@')[0], 'fx/master-gain');
    assert.ok(r.verify.checks.length > 0);
    assert.equal(r.sha256.length, 64);
    // always from stock: an already patched image is refused with patchkit's text
    assert.throws(() => kit.apply(r.syx, ['fx/master-gain']), /./);
  }
});

test('worker core: a firmware of the other device, and garbage, are refused', { skip: !have }, async () => {
  const { bundle } = buildModuleData(root);
  const kit = await PatchKit.create(readFileSync(wasmPath), JSON.stringify(bundle));
  assert.throws(() => kit.check(new Uint8Array(1000)), /./);
  const old = join(root, 'firmware', 'Digitakt_II_OS1.15C.syx');
  if (existsSync(old)) {
    const c = kit.check(new Uint8Array(readFileSync(old)));
    assert.equal(c.port, null);
    assert.ok(c.modules.every((m) => !m.ok && /no port/.test(m.error)));
  }
});

test('built site (when present) carries no firmware', { skip: !existsSync(join(root, 'apps/web/dist')) }, () => {
  const walk = (d) =>
    readdirSync(d, { withFileTypes: true }).flatMap((e) =>
      e.isDirectory() ? walk(join(d, e.name)) : [join(d, e.name)],
    );
  for (const f of walk(join(root, 'apps/web/dist'))) {
    assert.ok(!/\.(syx|snap|mstate)$/i.test(f), f);
    assert.ok(statSync(f).size < 5 * 1024 * 1024 || f.endsWith('emulator-core.wasm'), f);
  }
});
