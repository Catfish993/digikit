// The fit rule (src/modules/dkfit.ts) on the shared vectors of sdk/fit-vectors.json: dkrt_fit (Rust) and tools/device/dkfit.py read the same file. The compare only: the needs are inferred in Rust (dkneeds.test.mjs).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { describeShort, emptyCaps, fitsNow, fitsSet, take } from '../src/modules/dkfit.ts';

const V = JSON.parse(readFileSync(new URL('../../../sdk/fit-vectors.json', import.meta.url), 'utf8'));

const capsOf = (over) => {
  const merged = { ...V.caps_base, ...over };
  return Object.fromEntries(
    Object.entries(merged)
      .filter(([, v]) => v !== null)
      .map(([k, [limit, used, largest]]) => [k, { limit, used, largest }]),
  );
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

test('a short resource reads as a line', () => {
  assert.equal(describeShort({ resource: 'STAGE', need: 70000, free: 65536 }), 'file 69 KiB needed, 64 KiB at most');
  assert.equal(
    describeShort({ resource: 'DSP_AREA', need: 409600, free: 260096 }),
    'DSP area 400 KiB needed, 254 KiB free',
  );
  assert.equal(describeShort({ resource: 'PARAM_ROWS', need: 3, free: 2 }), 'parameter rows 3 needed, 2 free');
});
