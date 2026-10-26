/** Stock DN2 1.12 send FX oracles; Python retains NumPy analysis and render caching. */
import { expect, test } from 'vitest';
import { scenarios } from './dn2-sendfx.ts';
import { fxRender } from './fx-support.ts';

const full = process.env.DIGI_FX_SWEEP === '1';
const noAotSubset = process.env.DIGI_FX_NO_AOT_SUBSET === '1';
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
function oracle(identity: string, names: string[], sweep: boolean, body: () => void, subset = false) {
  needs.set(identity, names);
  test.skipIf((sweep && !full) || (noAotSubset && !subset))(identity, body);
}
function raw(name: string, prefix: string): number {
  const value = Object.entries(row(name).decoded).find(([key]) => key.startsWith(`${prefix} `))?.[1];
  expect(value, `${name} ${prefix}`).toBeDefined();
  return value! & 0xffff;
}
const ui = (name: string, prefix: string) => raw(name, prefix) >> 8;
function measure(name: string, metric: string): number {
  const value = row(name).sf[metric];
  expect(Number.isFinite(value), `${name} ${metric}: ${value}`).toBe(true);
  return value!;
}
for (const { name } of scenarios) {
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
      if (name === 'sf-del-time-u127')
        expect(basic.peak).toBeLessThanOrEqual(1.5 * row(row(name).pcm.reference).analysis.basic.peak);
      else if (!runaway.includes(name)) expect(basic.growth).toBe(false);
    },
    noAotHealth.includes(name),
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
    false,
    () => {
      const reference = row(`sf-${name.split('-')[1]}-ref`).decoded;
      const changed = Object.fromEntries(
        Object.entries(row(name).decoded)
          .filter(([key, stored]) => reference[key] !== stored)
          .map(([key, stored]) => [key.split(' ')[0], (stored & 0xffff) >> 8]),
      );
      expect(changed).toEqual({ [prefix]: value });
    },
    noAotHealth.includes(name),
  );
for (const fx of ['del', 'rev', 'chr']) {
  const name = `sf-${fx}-mix-d127`;
  oracle(`test_zero_is_dry ${name}`, [name], true, () => {
    expect(row(name).pcm.shape).toEqual(row(name).pcm.reference_shape);
    expect(row(name).pcm.reference_equal).toBe(true);
  });
}
oracle('test_mono_stays_mono sf-del-ref', ['sf-del-ref'], false, () =>
  expect(row('sf-del-ref').pcm.mono_equal).toBe(true),
);
for (const sweep of [false, true]) {
  const spacing = ['sf-del-ref', 'sf-del-time-u20', ...(sweep ? ['sf-del-time-u44', 'sf-del-time-u127'] : [])];
  oracle(`test_delay_spacing ${sweep ? 'sweep' : 'fast'}`, spacing, sweep, () => {
    for (const name of spacing)
      expect(Math.abs(measure(name, 'spacing_s') - (raw(name, 'fx[32]') / 256 + 1) * 0.015625)).toBeLessThan(0.002);
  });
}
oracle('test_delay_feedback_zero_one_echo', ['sf-del-fb-d127'], false, () => {
  expect(raw('sf-del-fb-d127', 'fx[35]')).toBe(0);
  expect(row('sf-del-fb-d127').delay.echo_onsets_s).toHaveLength(1);
});
oracle(
  'test_delay_default_feedback_minus_6_db',
  ['sf-del-ref'],
  false,
  () => {
    expect(ui('sf-del-ref', 'fx[35]')).toBe(32);
    expect(measure('sf-del-ref', 'n_echoes')).toBeGreaterThanOrEqual(4);
    expect(Math.abs(measure('sf-del-ref', 'db_per_repeat') + 6)).toBeLessThanOrEqual(1);
  },
  true,
);
oracle('test_delay_wid_min_is_left', ['sf-del-width-d127'], false, () => {
  expect(ui('sf-del-width-d127', 'fx[33]')).toBe(0);
  expect(ui('sf-del-width-d127', 'fx[34]')).toBe(0);
  expect(measure('sf-del-width-d127', 'wet_l_db') - measure('sf-del-width-d127', 'wet_r_db')).toBeGreaterThanOrEqual(
    40,
  );
});
oracle('test_delay_wid_max_is_right', ['sf-del-width-u44'], true, () => {
  expect(ui('sf-del-width-u44', 'fx[34]')).toBe(127);
  expect(measure('sf-del-width-u44', 'wet_r_db') - measure('sf-del-width-u44', 'wet_l_db')).toBeGreaterThanOrEqual(30);
});
oracle('test_delay_feedback_above_unity_swells', ['sf-del-fb-u127', 'sf-del-fb-u44'], true, () => {
  for (const name of ['sf-del-fb-u44', 'sf-del-fb-u127']) {
    expect(measure(name, 'db_per_repeat')).toBeGreaterThan(0);
    expect(row(name).analysis.basic.peak).toBeLessThanOrEqual(1);
  }
});
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
      sweep,
      () => {
        const ordered = names
          .map((name) => ({ name, raw: raw(name, prefix), value: measure(name, metric) }))
          .sort((a, b) => a.raw - b.raw);
        expect(new Set(ordered.map((r) => r.raw)).size, JSON.stringify(ordered)).toBe(ordered.length);
        expect(
          ordered.slice(1).every((r, i) => (increasing ? r.value > ordered[i]!.value : r.value < ordered[i]!.value)),
          JSON.stringify(ordered),
        ).toBe(true);
      },
      subset,
    );
  }
}
