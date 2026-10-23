// The fit rule (src/modules/dkfit.ts) on the shared vectors of sdk/fit-vectors.json: dkrt_sdk::fit (Rust) and tools/device/dkfit.py read the same file.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { describeShort, emptyCaps, fitsNow, fitsSet, needsOf, take } from '../src/modules/dkfit.ts';
import { DKM_NEEDS } from '../src/modules/gen_proto.ts';

const V = JSON.parse(readFileSync(new URL('../../../sdk/fit-vectors.json', import.meta.url), 'utf8'));

const capsOf = (over) => {
  const merged = { ...V.caps_base, ...(over ?? {}) };
  return Object.fromEntries(Object.entries(merged).map(([k, [limit, used, largest]]) => [k, { limit, used, largest }]));
};

for (const c of V.cases) {
  test(`fitsNow: ${c.name}`, () => {
    const got = fitsNow(c.needs, capsOf(c.caps));
    assert.deepEqual(got && [got.resource, got.need, got.free], c.want);
  });
}

for (const s of V.sets) {
  test(`fitsSet: ${s.name}`, () => {
    const needs = s.needs.flatMap(({ times = 1, ...n }) => Array.from({ length: times }, () => n));
    const got = fitsSet(needs, capsOf(s.caps));
    assert.deepEqual(got && [got.index, got.resource, got.need, got.free], s.want);
  });
}

test('emptyCaps is the capacity of a runtime that has loaded nothing', () => {
  const caps = (m) =>
    Object.fromEntries(Object.entries(m).map(([k, [limit, used, largest]]) => [k, { limit, used, largest }]));
  assert.deepEqual(emptyCaps(caps(V.empty.from)), caps(V.empty.want));
});

test('take leaves the capacity it says and does not change its input', () => {
  const caps = capsOf();
  const left = take({ ARENA: 32, ARENA_XFER: 1392, DSP_AREA: 265600, DSP_ALIGN: 64, VOICE_ROWS: 1 }, caps);
  assert.equal(left.ARENA.used, 1424);
  assert.equal(left.ARENA.largest, 524272 - 1424);
  assert.equal(left.DSP_AREA.used, 265656); // the alignment room is taken too
  assert.equal(left.MODULES.used, 1);
  assert.equal(caps.MODULES.used, 0);
});

test('needsOf reads the header at the offsets of [dkm.needs]', () => {
  const h = new Uint8Array(0xd0);
  const v = new DataView(h.buffer);
  v.setUint32(8, 700);
  Object.values(DKM_NEEDS).forEach((at, i) => v.setUint32(at, 100 + i));
  const n = needsOf(h);
  assert.equal(n.file_size, 700);
  Object.keys(DKM_NEEDS).forEach((name, i) => assert.equal(n[name], 100 + i));
});

test('a short resource reads as a line', () => {
  assert.equal(
    describeShort({ resource: 'DSP_AREA', need: 409600, free: 260096 }),
    'DSP area 400 KiB needed, 254 KiB free',
  );
  assert.equal(describeShort({ resource: 'PARAM_ROWS', need: 3, free: 2 }), 'parameter rows 3 needed, 2 free');
});
