/** DT2 1.17 coupled FX oracles. Test identities and thresholds match the Python suite. */
import { expect } from 'vitest';
import { scenarios as track } from './dt2-trackfx.ts';
import { scenarios as send } from './dt2-sendfx.ts';
import { changedUi, expectHealthy, expectMono, fxOracles, fxRender, raw } from './fx-support.ts';

const scenarios = [...track, ...send];
const needs = new Map<string, string[]>();
const rendered = fxRender(needs, { family: 'dt2-fx', scenarios, device: 'dt2' });
const row = rendered.row;
const oracle = fxOracles(needs, { subsets: false });
function bits(name: string, metric: string): number {
  return row(name).bits[metric]!;
}
function tf(name: string, metric: string): number {
  return row(name).tf[metric] as number;
}
function sf(name: string, metric: string): number {
  return row(name).sf[metric]!;
}
function ordered(names: string[], key: string, metric: (name: string) => number) {
  const got = names
    .map((name) => ({ name, value: raw(rendered, name, key), metric: metric(name) }))
    .sort((a, b) => a.value - b.value);
  expect(new Set(got.map((r) => r.value)).size, JSON.stringify(got)).toBe(got.length);
  expect(
    got.every((r) => Number.isFinite(r.metric)),
    JSON.stringify(got),
  ).toBe(true);
  return got;
}
function rises(names: string[], key: string, metric: (name: string) => number, increasing = true) {
  const got = ordered(names, key, metric);
  expect(
    got.slice(1).every((r, i) => (increasing ? r.metric > got[i]!.metric : r.metric < got[i]!.metric)),
    JSON.stringify(got),
  ).toBe(true);
}

const trackQuick = [
  'tf-dry-hold',
  'tf-dry-tap',
  'tf-dry-flt',
  'tf-br-0',
  'tf-br-22',
  'tf-br-54',
  'tf-br-127',
  'tf-over-22',
  'tf-over-54',
  'tf-over-127',
  'tf-srr-22',
  'tf-srr-54',
  'tf-srr-127',
  'tf-del-54',
  'tf-del-127',
  'tf-rev-54',
  'tf-rev-127',
  'tf-chr-54',
  'tf-chr-127',
  'tf-srr-127-flt',
  'tf-srr-127-flt-rt1',
  'tf-over-127-flt',
  'tf-over-127-flt-rt1',
];

const sendQuick = [
  'sf-dry-tap',
  'sf-dry-hold',
  'sf-del-ref',
  'sf-del-time-d50',
  'sf-del-time-u20',
  'sf-del-fb-d50',
  'sf-del-fb-u20',
  'sf-del-width-d50',
  'sf-del-width-u50',
  'sf-del-lpf-d50',
  'sf-del-mix-d50',
  'sf-del-mix-u50',
  'sf-rev-ref',
  'sf-rev-decay-d50',
  'sf-rev-decay-u20',
  'sf-rev-pre-u50',
  'sf-rev-mix-d50',
  'sf-chr-ref',
  'sf-chr-depth-d50',
  'sf-chr-speed-d50',
  'sf-chr-speed-u50',
  'sf-chr-mix-d50',
  'sf-mst-mix',
  'sf-mst-thr0',
  'sf-mst-ratio',
  'sf-mst-makeup',
];

const intent = {
  'sf-del-time-d50': {
    'fx[32]': 0,
  },
  'sf-del-time-u20': {
    'fx[32]': 30,
  },
  'sf-del-time-u50': {
    'fx[32]': 100,
  },
  'sf-del-pp-u20': {
    'fx[33]': 1,
  },
  'sf-del-width-d50': {
    'fx[34]': 0,
  },
  'sf-del-width-u50': {
    'fx[34]': 127,
  },
  'sf-del-fb-d50': {
    'fx[35]': 0,
  },
  'sf-del-fb-u20': {
    'fx[35]': 39,
  },
  'sf-del-fb-u50': {
    'fx[35]': 109,
  },
  'sf-del-lpf-d50': {
    'fx[37]': 0,
  },
  'sf-del-rsend-u50': {
    'fx[38]': 127,
  },
  'sf-del-mix-d50': {
    'fx[39]': 0,
  },
  'sf-del-mix-u50': {
    'fx[39]': 127,
  },
  'sf-rev-pre-u50': {
    'fx[41]': 127,
  },
  'sf-rev-decay-d50': {
    'fx[42]': 0,
  },
  'sf-rev-decay-u20': {
    'fx[42]': 38,
  },
  'sf-rev-decay-u50': {
    'fx[42]': 108,
  },
  'sf-rev-lpf-d50': {
    'fx[46]': 0,
  },
  'sf-rev-mix-d50': {
    'fx[47]': 0,
  },
  'sf-chr-depth-d50': {
    'fx[25]': 0,
  },
  'sf-chr-speed-d50': {
    'fx[26]': 0,
  },
  'sf-chr-speed-u50': {
    'fx[26]': 127,
  },
  'sf-chr-mix-d50': {
    'fx[31]': 0,
  },
  'sf-chr-dsend-u50': {
    'fx[29]': 127,
  },
  'sf-del-ppwidth-d50': {
    'fx[33]': 1,
    'fx[34]': 0,
  },
  'sf-del-ppwidth-u50': {
    'fx[33]': 1,
    'fx[34]': 127,
  },
  'sf-mst-mix': {
    'fx[67]': 127,
  },
  'sf-mst-thr0': {
    'fx[60]': 0,
    'fx[67]': 127,
  },
  'sf-mst-ratio': {
    'fx[60]': 0,
    'fx[64]': 7,
    'fx[67]': 127,
  },
  'sf-mst-makeup': {
    'fx[63]': 127,
    'fx[67]': 127,
  },
  'tf-br-22': {
    'snd[64]': 22,
  },
  'tf-br-54': {
    'snd[64]': 54,
  },
  'tf-br-127': {
    'snd[64]': 127,
  },
  'tf-over-22': {
    'snd[67]': 22,
  },
  'tf-over-54': {
    'snd[67]': 54,
  },
  'tf-over-127': {
    'snd[67]': 127,
  },
  'tf-srr-22': {
    'snd[65]': 22,
  },
  'tf-srr-54': {
    'snd[65]': 54,
  },
  'tf-srr-127': {
    'snd[65]': 127,
  },
  'tf-br-0': {},
  'tf-srr-127-rt1': {
    'snd[65]': 127,
    'snd[66]': 1,
  },
  'tf-over-127-rt1': {
    'snd[67]': 127,
    'snd[68]': 1,
  },
  'tf-del-54': {
    'snd[55]': 54,
  },
  'tf-del-127': {
    'snd[55]': 127,
  },
  'tf-rev-54': {
    'snd[56]': 54,
  },
  'tf-rev-127': {
    'snd[56]': 127,
  },
  'tf-chr-54': {
    'snd[57]': 54,
  },
  'tf-chr-127': {
    'snd[57]': 127,
  },
  'tf-srr-127-flt': {
    'snd[65]': 127,
  },
  'tf-srr-127-flt-rt1': {
    'snd[65]': 127,
    'snd[66]': 1,
  },
  'tf-over-127-flt': {
    'snd[67]': 127,
  },
  'tf-over-127-flt-rt1': {
    'snd[67]': 127,
    'snd[68]': 1,
  },
};

const mono = [
  'tf-dry-hold',
  'tf-br-22',
  'tf-br-127',
  'tf-over-22',
  'tf-over-127',
  'tf-srr-22',
  'tf-srr-127',
  'tf-del-54',
  'tf-del-127',
  'sf-del-ref',
];

for (const { name } of scenarios)
  oracle(
    `test_healthy ${name}`,
    [name],
    () => {
      expectHealthy(rendered, name, { growth: name !== 'sf-del-time-u50' && name !== 'sf-del-fb-u50' });
      if (name === 'sf-del-time-u50')
        expect(row(name).analysis.basic.peak).toBeLessThanOrEqual(
          1.5 * row(row(name).pcm.reference).analysis.basic.peak,
        );
    },
    { sweep: ![...trackQuick, ...sendQuick].includes(name) },
  );
for (const [name, expected] of Object.entries(intent))
  oracle(`test_readback ${name}`, [name], () => {
    const fx = name.split('-')[1];
    const reference = name.startsWith('sf-') && fx !== 'mst' ? `sf-${fx}-ref` : row(name).pcm.reference;
    expect(changedUi(rendered, name, reference)).toEqual(expected);
  });
for (const name of ['tf-br-0', 'sf-del-mix-d50', 'sf-rev-mix-d50', 'sf-chr-mix-d50'])
  oracle(`test_zero_is_dry ${name}`, [name], () => {
    const pcm = row(name).pcm;
    expect(pcm.shape).toEqual(pcm.reference_shape);
    if (name === 'tf-br-0') expect(pcm.reference_max_abs_diff).toBe(0);
    else expect(pcm.reference_max_abs_diff).toBeLessThan(1e-4);
  });
for (const name of mono) oracle(`test_mono_stays_mono ${name}`, [name], () => expectMono(rendered, name));
const br = ['tf-br-0', 'tf-br-22', 'tf-br-54', 'tf-br-127'];
const over = ['tf-over-22', 'tf-over-54', 'tf-over-127'];
const srr = ['tf-srr-22', 'tf-srr-54', 'tf-srr-127'];
for (const metric of ['thd_db', 'diff_db']) {
  oracle(`test_bit_reduction_error_rises ${metric}`, br, () => rises(br, 'snd[64]', (name) => bits(name, metric)));
  oracle(`test_overdrive_distortion_rises ${metric}`, ['tf-dry-hold', ...over], () => {
    rises(over, 'snd[67]', (name) => bits(name, metric));
    rises(over, 'snd[67]', (name) => tf(name, 'h3_db'));
    rises(over, 'snd[67]', (name) => tf(name, 'fund_change_db'));
  });
}
oracle('test_srr_rate_falls', srr, () => {
  rises(srr, 'snd[65]', (name) => row(name).tf.fs_candidates[0]![0]!, false);
  rises(srr, 'snd[65]', (name) => bits(name, 'diff_db'));
});
oracle('test_filter_closes', ['tf-dry-hold', 'tf-dry-flt'], () =>
  expect(bits('tf-dry-flt', 'fund_db')).toBeLessThan(bits('tf-dry-hold', 'fund_db') - 30),
);
oracle(
  'test_routing_changes_the_sound',
  ['tf-srr-127-flt', 'tf-srr-127-flt-rt1', 'tf-over-127-flt', 'tf-over-127-flt-rt1'],
  () => {
    for (const fx of ['srr', 'over']) {
      const comparison = row(`tf-${fx}-127-flt-rt1`).tf.vs_routing_0 as {
        shape_equal: boolean;
        full_max_abs_diff: number;
      };
      expect(comparison.shape_equal).toBe(true);
      expect(comparison.full_max_abs_diff).toBeGreaterThan(1e-3);
    }
  },
);
for (const [fx, key] of [
  ['del', 'snd[55]'],
  ['rev', 'snd[56]'],
  ['chr', 'snd[57]'],
]) {
  const names = [`tf-${fx}-54`, `tf-${fx}-127`];
  oracle(`test_send_adds_wet_signal ${fx}`, ['tf-dry-tap', ...names], () => {
    rises(names, key!, (name) => tf(name, 'wet_rms_db'));
    expect(tf(names[0]!, 'wet_rms_db')).toBeGreaterThan(-80);
  });
}
const time = ['sf-del-time-d50', 'sf-del-ref', 'sf-del-time-u20', 'sf-del-time-u50'];
oracle('test_delay_time_orders_the_echo', time, () => {
  rises(time, 'fx[32]', (name) => sf(name, 'wet_onset30_s'));
  for (const name of ['sf-del-ref', 'sf-del-time-u20']) {
    const first = sf(name, 'wet_onset30_s');
    expect(Math.abs(sf(name, 'spacing_s') - first)).toBeLessThan(0.08 * first);
  }
});
const feedback = ['sf-del-fb-d50', 'sf-del-ref', 'sf-del-fb-u20', 'sf-del-fb-u50'];
oracle('test_delay_feedback_orders_the_repeats', feedback, () => {
  const got = ordered(feedback, 'fx[35]', (name) => sf(name, 'db_per_repeat'));
  const steps = got.map((r) => r.metric);
  expect(steps).toEqual([...steps].sort((a, b) => a - b));
  expect(steps[0]).toBeLessThan(-6);
  expect(steps.at(-1)).toBeGreaterThan(0);
  expect(row('sf-del-fb-u50').analysis.basic.peak).toBeLessThanOrEqual(1);
});
oracle('test_delay_width_places_the_echo', ['sf-del-ref', 'sf-del-width-d50', 'sf-del-width-u50'], () => {
  expect(sf('sf-del-ref', 'wet_l_db')).toBe(sf('sf-del-ref', 'wet_r_db'));
  expect(sf('sf-del-width-d50', 'wet_l_db') - sf('sf-del-width-d50', 'wet_r_db')).toBeGreaterThanOrEqual(40);
  expect(sf('sf-del-width-u50', 'wet_r_db') - sf('sf-del-width-u50', 'wet_l_db')).toBeGreaterThanOrEqual(30);
});
oracle('test_delay_pingpong_alternates', ['sf-del-ppwidth-d50', 'sf-del-ppwidth-u50'], () => {
  expect(sf('sf-del-ppwidth-d50', 'wet_l_db')).toBeGreaterThan(sf('sf-del-ppwidth-d50', 'wet_r_db') + 3);
  expect(sf('sf-del-ppwidth-u50', 'wet_r_db')).toBeGreaterThan(sf('sf-del-ppwidth-u50', 'wet_l_db') + 3);
});
oracle('test_delay_lpf_and_mix', ['sf-del-ref', 'sf-del-lpf-d50', 'sf-del-mix-d50', 'sf-del-mix-u50'], () => {
  expect(sf('sf-del-lpf-d50', 'wet_rms_db')).toBeLessThan(sf('sf-del-ref', 'wet_rms_db') - 20);
  expect(sf('sf-del-mix-d50', 'wet_rms_db')).toBeLessThan(-100);
  expect(sf('sf-del-mix-u50', 'wet_rms_db')).toBeGreaterThan(sf('sf-del-ref', 'wet_rms_db'));
});
const reverb = ['sf-rev-decay-d50', 'sf-rev-ref', 'sf-rev-decay-u20', 'sf-rev-decay-u50'];
oracle('test_reverb_rt60_rises_with_decay', reverb, () => rises(reverb, 'fx[42]', (name) => sf(name, 'rt60_s')));
oracle('test_reverb_pre_delay_lpf_mix', ['sf-rev-ref', 'sf-rev-pre-u50', 'sf-rev-lpf-d50', 'sf-rev-mix-d50'], () => {
  expect(sf('sf-rev-pre-u50', 'onset35_s')).toBeGreaterThan(sf('sf-rev-ref', 'onset35_s') + 0.3);
  expect(sf('sf-rev-lpf-d50', 'wet_rms_db')).toBeLessThan(sf('sf-rev-ref', 'wet_rms_db') - 20);
  expect(sf('sf-rev-mix-d50', 'wet_rms_db')).toBeLessThan(-100);
});
const speed = ['sf-chr-speed-d50', 'sf-chr-ref', 'sf-chr-speed-u50'];
oracle('test_chorus_rate_rises_with_speed', speed, () => rises(speed, 'fx[26]', (name) => sf(name, 'mod_phase_hz')));
oracle('test_chorus_depth_and_mix', ['sf-chr-ref', 'sf-chr-depth-d50', 'sf-chr-mix-d50'], () => {
  expect(sf('sf-chr-ref', 'wet_corr')).toBeLessThan(0.5);
  expect(sf('sf-chr-depth-d50', 'wet_corr')).toBeGreaterThan(0.99);
  expect(sf('sf-chr-mix-d50', 'wet_rms_db')).toBeLessThan(-100);
});
const master = ['sf-mst-mix', 'sf-mst-thr0', 'sf-mst-ratio', 'sf-mst-makeup'];
oracle('test_master_compressor_moves_the_level', master, () => {
  expect(sf(master[2]!, 'rms_change_db')).toBeLessThan(sf(master[1]!, 'rms_change_db'));
  expect(sf(master[1]!, 'rms_change_db')).toBeLessThan(sf(master[0]!, 'rms_change_db'));
  expect(sf(master[0]!, 'rms_change_db')).toBeLessThan(sf(master[3]!, 'rms_change_db'));
});
