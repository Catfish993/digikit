/** Stock DN2 1.12 coupled audio oracles; Python retains NumPy analysis and render caching. */
import { expect, test } from 'vitest';
import { scenarios } from './dn2-trackfx.ts';
import { trackFxRender } from './fx-support.ts';

const full = process.env.DIGI_FX_SWEEP === '1';
const noAotSubset = process.env.DIGI_FX_NO_AOT_SUBSET === '1';
const quick = ['tf-dry-hold', ...['br', 'over', 'srr'].flatMap((fx) => [0, 44, 127].map((ui) => `tf-${fx}-${ui}`))];
const noAotHealth = ['tf-dry-hold', 'tf-br-127', 'tf-over-127', 'tf-srr-127'];
const needs = new Map<string, string[]>();
const rendered = trackFxRender(needs);

function oracle(identity: string, names: string[], sweep: boolean, body: () => void, subset = false) {
  needs.set(identity, names);
  test.skipIf((sweep && !full) || (noAotSubset && !subset))(identity, body);
}
const row = rendered.row;
const raw = (name: string, prefix: string) =>
  Object.entries(row(name).decoded).find(([key]) => key.startsWith(`${prefix} `))?.[1];
for (const scenario of scenarios) {
  const name = scenario.name;
  oracle(
    `test_healthy ${name}`,
    [name],
    !quick.includes(name),
    () => {
      const basic = row(name).analysis.basic;
      expect(rendered.gates(name)?.ok).toBe(true);
      expect(basic.nonfinite).toBe(0);
      expect(Math.max(...basic.dc.map(Math.abs))).toBeLessThan(1e-3);
      expect(basic.peak).toBeLessThanOrEqual(1);
      expect(basic.growth).toBe(false);
    },
    noAotHealth.includes(name),
  );
}
for (const [fx, index] of [
  ['br', 95],
  ['over', 98],
  ['srr', 96],
] as const) {
  for (const ui of [0, 44, 127]) {
    const name = `tf-${fx}-${ui}`;
    oracle(
      `test_readback ${name}`,
      [name],
      false,
      () => {
        const reference = row(row(name).pcm.reference).decoded;
        const changed = Object.fromEntries(
          Object.entries(row(name).decoded)
            .filter(([key, value]) => reference[key] !== value)
            .map(([key, value]) => [key.split(' ')[0], (value & 0xffff) >> 8]),
        );
        expect(changed).toEqual(ui ? { [`snd[${index}]`]: ui } : {});
      },
      ui === 127,
    );
  }
}
for (const fx of ['br', 'over', 'srr', 'del', 'rev', 'chr']) {
  const name = `tf-${fx}-0`;
  oracle(`test_zero_is_dry ${name}`, [name], ['del', 'rev', 'chr'].includes(fx), () => {
    const pcm = row(name).pcm;
    expect(pcm.shape).toEqual(pcm.reference_shape);
    expect(pcm.reference_equal).toBe(true);
  });
}
for (const name of ['tf-dry-hold', ...['br', 'over', 'srr'].flatMap((fx) => [44, 127].map((ui) => `tf-${fx}-${ui}`))]) {
  oracle(`test_mono_stays_mono ${name}`, [name], false, () => expect(row(name).pcm.mono_equal).toBe(true));
}
for (const [fx, index, behavior] of [
  ['br', 95, 'bit_reduction_error_rises'],
  ['over', 98, 'overdrive_distortion_rises'],
  ['srr', 96, 'srr_rate_falls'],
] as const) {
  for (const sweep of [false, true])
    for (const metric of fx === 'srr' ? ['rate'] : ['thd_db', 'diff_db']) {
      const levels =
        fx === 'srr' ? (sweep ? [28, 44, 71, 93, 127] : [44, 127]) : sweep ? [0, 28, 44, 71, 93, 127] : [0, 44, 127];
      const names = levels.map((ui) => `tf-${fx}-${ui}`);
      oracle(
        `test_${behavior} ${metric} ${sweep ? 'sweep' : 'fast'}`,
        names,
        sweep,
        () => {
          const ordered = names
            .map((name) => ({
              name,
              raw: (raw(name, `snd[${index}]`) ?? 0) & 0xffff,
              metric: metric === 'rate' ? row(name).tf.fs_candidates[0]![0]! : row(name).bits[metric]!,
            }))
            .sort((a, b) => a.raw - b.raw);
          expect(new Set(ordered.map((r) => r.raw)).size, JSON.stringify(ordered)).toBe(ordered.length);
          expect(
            ordered.every((r) => Number.isFinite(r.metric)),
            JSON.stringify(ordered),
          ).toBe(true);
          expect(
            ordered
              .slice(1)
              .every((r, i) => (fx === 'srr' ? r.metric < ordered[i]!.metric : r.metric > ordered[i]!.metric)),
            JSON.stringify(ordered),
          ).toBe(true);
        },
        fx === 'srr',
      );
    }
}
