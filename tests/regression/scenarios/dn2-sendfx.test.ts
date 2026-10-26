/** Stock DN2 1.12 send FX oracles; Python retains NumPy analysis and render caching. */
import { expect } from 'vitest';
import { scenarios } from './dn2-sendfx.ts';
import { changedUi, expectHealthy, expectMono, expectZeroIsDry, fxOracles, fxRender, raw } from './fx-support.ts';

const quick = [
  'sf-del-ref',
  'sf-del-time-u20',
  'sf-del-fb-d127',
  'sf-del-width-d127',
  'sf-rev-ref',
  'sf-rev-decay-d127',
  'sf-rev-decay-u20',
  'sf-chr-ref',
  'sf-chr-speed-d127',
  'sf-chr-speed-u20',
  'sf-chr-width-d127',
];
// Feedback beyond unity swells until saturation. A sparse two-second echo may trip the growth heuristic.
const runaway = ['sf-del-fb-u44', 'sf-del-fb-u127', 'sf-rev-decay-u127'];
const noAotHealth = ['sf-del-time-u20', 'sf-rev-decay-u20', 'sf-chr-speed-u20'];
const needs = new Map<string, string[]>();
const rendered = fxRender(needs, { family: 'sendfx', scenarios });
const row = rendered.row;
const oracle = fxOracles(needs, { subsets: true });
const ui = (name: string, prefix: string) => raw(rendered, name, prefix) >> 8;
function measure(name: string, metric: string): number {
  const value = row(name).sf[metric];
  expect(Number.isFinite(value), `${name} ${metric}: ${value}`).toBe(true);
  return value!;
}
for (const { name } of scenarios) {
  oracle(
    `test_healthy ${name}`,
    [name],
    () => {
      expectHealthy(rendered, name, { growth: name !== 'sf-del-time-u127' && !runaway.includes(name) });
      if (name === 'sf-del-time-u127')
        expect(row(name).analysis.basic.peak).toBeLessThanOrEqual(
          1.5 * row(row(name).pcm.reference).analysis.basic.peak,
        );
    },
    { sweep: !quick.includes(name), subset: noAotHealth.includes(name) },
  );
}
const intent: [string, string, number][] = [
  ['sf-del-time-u20', 'fx[32]', 32],
  ['sf-del-fb-d127', 'fx[35]', 0],
  ['sf-del-width-d127', 'fx[34]', 0],
  ['sf-rev-decay-d127', 'fx[42]', 0],
  ['sf-rev-decay-u20', 'fx[42]', 40],
  ['sf-chr-speed-d127', 'fx[26]', 0],
  ['sf-chr-speed-u20', 'fx[26]', 52],
  ['sf-chr-width-d127', 'fx[28]', 0],
];
for (const [name, prefix, value] of intent)
  oracle(
    `test_readback ${name}`,
    [name],
    () => expect(changedUi(rendered, name, `sf-${name.split('-')[1]}-ref`)).toEqual({ [prefix]: value }),
    { subset: noAotHealth.includes(name) },
  );
for (const fx of ['del', 'rev', 'chr']) {
  const name = `sf-${fx}-mix-d127`;
  oracle(`test_zero_is_dry ${name}`, [name], () => expectZeroIsDry(rendered, name), { sweep: true });
}
oracle('test_mono_stays_mono sf-del-ref', ['sf-del-ref'], () => expectMono(rendered, 'sf-del-ref'));
for (const sweep of [false, true]) {
  const spacing = ['sf-del-ref', 'sf-del-time-u20', ...(sweep ? ['sf-del-time-u44', 'sf-del-time-u127'] : [])];
  oracle(
    `test_delay_spacing ${sweep ? 'sweep' : 'fast'}`,
    spacing,
    () => {
      for (const name of spacing)
        expect(
          Math.abs(measure(name, 'spacing_s') - (raw(rendered, name, 'fx[32]') / 256 + 1) * 0.015625),
        ).toBeLessThan(0.002);
    },
    { sweep },
  );
}
oracle('test_delay_feedback_zero_one_echo', ['sf-del-fb-d127'], () => {
  expect(raw(rendered, 'sf-del-fb-d127', 'fx[35]')).toBe(0);
  expect(row('sf-del-fb-d127').delay.echo_onsets_s).toHaveLength(1);
});
oracle(
  'test_delay_default_feedback_minus_6_db',
  ['sf-del-ref'],
  () => {
    expect(ui('sf-del-ref', 'fx[35]')).toBe(32);
    expect(measure('sf-del-ref', 'n_echoes')).toBeGreaterThanOrEqual(4);
    expect(Math.abs(measure('sf-del-ref', 'db_per_repeat') + 6)).toBeLessThanOrEqual(1);
  },
  { subset: true },
);
oracle('test_delay_wid_min_is_left', ['sf-del-width-d127'], () => {
  expect(ui('sf-del-width-d127', 'fx[33]')).toBe(0);
  expect(ui('sf-del-width-d127', 'fx[34]')).toBe(0);
  expect(measure('sf-del-width-d127', 'wet_l_db') - measure('sf-del-width-d127', 'wet_r_db')).toBeGreaterThanOrEqual(
    40,
  );
});
oracle(
  'test_delay_wid_max_is_right',
  ['sf-del-width-u44'],
  () => {
    expect(ui('sf-del-width-u44', 'fx[34]')).toBe(127);
    expect(measure('sf-del-width-u44', 'wet_r_db') - measure('sf-del-width-u44', 'wet_l_db')).toBeGreaterThanOrEqual(
      30,
    );
  },
  { sweep: true },
);
oracle(
  'test_delay_feedback_above_unity_swells',
  ['sf-del-fb-u127', 'sf-del-fb-u44'],
  () => {
    for (const name of ['sf-del-fb-u44', 'sf-del-fb-u127']) {
      expect(measure(name, 'db_per_repeat')).toBeGreaterThan(0);
      expect(row(name).analysis.basic.peak).toBeLessThanOrEqual(1);
    }
  },
  { sweep: true },
);
for (const [behavior, prefix, metric, fast, extra, increasing, subset] of [
  [
    'reverb_rt60_rises_with_decay',
    'fx[42]',
    'rt60_s',
    ['sf-rev-decay-d127', 'sf-rev-ref', 'sf-rev-decay-u20'],
    ['sf-rev-decay-u44'],
    true,
    true,
  ],
  [
    'chorus_rate_rises_with_speed',
    'fx[26]',
    'mod_phase_hz',
    ['sf-chr-speed-d127', 'sf-chr-ref', 'sf-chr-speed-u20'],
    ['sf-chr-speed-u44', 'sf-chr-speed-u127'],
    true,
    true,
  ],
  [
    'chorus_decorrelates_with_width',
    'fx[28]',
    'wet_corr',
    ['sf-chr-width-d127', 'sf-chr-ref'],
    ['sf-chr-width-d44', 'sf-chr-width-d20'],
    false,
    false,
  ],
] as const) {
  for (const sweep of [false, true]) {
    const names = [...fast, ...(sweep ? extra : [])];
    oracle(
      `test_${behavior} ${sweep ? 'sweep' : 'fast'}`,
      names,
      () => {
        const ordered = names
          .map((name) => ({ name, raw: raw(rendered, name, prefix), value: measure(name, metric) }))
          .sort((a, b) => a.raw - b.raw);
        expect(new Set(ordered.map((r) => r.raw)).size, JSON.stringify(ordered)).toBe(ordered.length);
        expect(
          ordered.slice(1).every((r, i) => (increasing ? r.value > ordered[i]!.value : r.value < ordered[i]!.value)),
          JSON.stringify(ordered),
        ).toBe(true);
      },
      { sweep, subset },
    );
  }
}
