/** Stock DN2 1.12 coupled audio oracles; Python retains NumPy analysis and render caching. */
import { expect } from 'vitest';
import { scenarios } from './dn2-trackfx.ts';
import { changedUi, expectHealthy, expectMono, expectZeroIsDry, fxOracles, fxRender, raw } from './fx-support.ts';

const quick = ['tf-dry-hold', ...['br', 'over', 'srr'].flatMap((fx) => [0, 44, 127].map((ui) => `tf-${fx}-${ui}`))];
const noAotHealth = ['tf-dry-hold', 'tf-br-127', 'tf-over-127', 'tf-srr-127'];
const needs = new Map<string, string[]>();
const rendered = fxRender(needs, { family: 'trackfx', scenarios });
const oracle = fxOracles(needs, { subsets: true });
const row = rendered.row;
for (const scenario of scenarios) {
  const name = scenario.name;
  oracle(`test_healthy ${name}`, [name], () => expectHealthy(rendered, name), {
    sweep: !quick.includes(name),
    subset: noAotHealth.includes(name),
  });
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
      () => expect(changedUi(rendered, name, row(name).pcm.reference)).toEqual(ui ? { [`snd[${index}]`]: ui } : {}),
      { subset: ui === 127 },
    );
  }
}
for (const fx of ['br', 'over', 'srr', 'del', 'rev', 'chr']) {
  const name = `tf-${fx}-0`;
  oracle(`test_zero_is_dry ${name}`, [name], () => expectZeroIsDry(rendered, name), {
    sweep: ['del', 'rev', 'chr'].includes(fx),
  });
}
for (const name of ['tf-dry-hold', ...['br', 'over', 'srr'].flatMap((fx) => [44, 127].map((ui) => `tf-${fx}-${ui}`))]) {
  oracle(`test_mono_stays_mono ${name}`, [name], () => expectMono(rendered, name));
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
        () => {
          const ordered = names
            .map((name) => ({
              name,
              raw: raw(rendered, name, `snd[${index}]`),
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
        { sweep, subset: fx === 'srr' },
      );
    }
}
