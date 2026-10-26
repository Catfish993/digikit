/** DN2 1.12 send FX: padded instruction slots preserve encoder acceleration and chunk boundaries. */
import { pathToFileURL } from 'node:url';
import type { Event, Scenario } from './dn2-trackfx.ts';

const hz = 132_000_000;
const slot = 1_000_000;
const up = [20, 44, 127];
const down = [-127, -44, -20];
const mix = [-127, -44, 127];
type Fx = 'del' | 'rev' | 'chr' | 'mst';
const params: [Fx, string, number, number[]][] = [
  ['del', 'time', 1, up],
  ['del', 'pp', 2, [20]],
  ['del', 'width', 3, [-127, -44, -30, -20, 20, 44, 127]],
  ['del', 'fb', 4, [-127, 10, 20, 30, 44, 127]],
  ['del', 'hpf', 5, up],
  ['del', 'lpf', 6, down],
  ['del', 'rsend', 8, up],
  ['del', 'mix', 9, mix],
  ['rev', 'pre', 1, [20, 28, 32, 36, 44, 127]],
  ['rev', 'decay', 2, [-127, 20, 44, 127]],
  ['rev', 'shfreq', 3, mix],
  ['rev', 'shgain', 4, mix],
  ['rev', 'hpf', 5, up],
  ['rev', 'lpf', 6, down],
  ['rev', 'mix', 9, mix],
  ['chr', 'depth', 1, down],
  ['chr', 'speed', 2, [-127, 20, 44, 127]],
  ['chr', 'hpf', 3, up],
  ['chr', 'width', 4, down],
  ['chr', 'dsend', 7, [44, 127]],
  ['chr', 'rsend', 8, [44, 127]],
  ['chr', 'mix', 9, mix],
];
interface Settings {
  fx?: Fx;
  send?: boolean;
  sweeps?: [number, number][];
  hold?: number;
  tail?: number;
}
function build(
  name: string,
  description: string,
  { fx = 'del', send = false, sweeps = [[1, 0]], hold = 0.3, tail = 4 }: Settings = {},
): Scenario {
  const events: Event[] = [];
  const button = (at: number, code: number, down: boolean) => events.push({ at_instr: at, button: code, down });
  const sweep = (start: number, encoder: number, count: number, active: number) => {
    for (let i = 0; i < count; i++)
      events.push({
        at_instr: start + i * slot,
        turn: encoder,
        detents: i < count - Math.abs(active) ? 0 : active < 0 ? -4 : 4,
      });
  };
  button(slot, 5, true);
  button(3 * slot, 5, false);
  sweep(20 * slot, { del: 5, rev: 6, chr: 7, mst: 5 }[fx], 50, send ? 44 : 0);
  for (let i = 0; i < { del: 1, rev: 2, chr: 3, mst: 1 }[fx]; i++) {
    const at = (75 + i * 6) * slot;
    const key = fx === 'mst' ? 6 : 5;
    button(at, 17, true);
    button(at + slot, key, true);
    button(at + 2 * slot, key, false);
    button(at + 3 * slot, 17, false);
  }
  sweeps.forEach(([encoder, active], i) => sweep(100 * slot + i * 500_000, encoder, 130, active));
  const release = 245 * slot + Math.round(hold * hz);
  button(245 * slot, 25, true);
  button(release, 25, false);
  return {
    name,
    description,
    seconds: Math.ceil((release / hz + tail) * 100) / 100,
    exact_events: true,
    events: events.sort((a, b) => a.at_instr - b.at_instr),
  };
}
const tag = (n: number) => (n > 0 ? `u${n}` : `d${-n}`);
export const scenarios: Scenario[] = [];
for (const [hold, tail, label] of [
  [0.3, 4, 'tap'],
  [1.5, 1, 'hold'],
] as const)
  scenarios.push(
    build(`sf-dry-${label}`, `DN2 send FX sweep reference: no sends, no turns, Trig 1 held ${hold} s.`, { hold, tail }),
  );
for (const fx of ['del', 'rev', 'chr'] as const) {
  const settings = { fx, send: true, hold: fx === 'chr' ? 1.5 : 0.3, tail: fx === 'chr' ? 1 : 4 };
  scenarios.push(build(`sf-${fx}-ref`, `Track ${fx} send at UI 127, send FX page left at its defaults.`, settings));
  for (const [family, param, encoder, values] of params) {
    if (family !== fx) continue;
    for (const n of values)
      scenarios.push(
        build(
          `sf-${fx}-${param}-${tag(n)}`,
          `Track ${fx} send at UI 127; send FX page encoder ${encoder} (${param}): ${Math.abs(n)} events of ${n > 0 ? '+' : '-'}4 detents.`,
          { ...settings, sweeps: [[encoder, n]] },
        ),
      );
  }
}
for (const n of [-127, -44, 20, 127])
  scenarios.push(
    build(
      `sf-del-ppwidth-${tag(n)}`,
      `Track del send at UI 127; Pingpong on, Stereo Width ${n > 0 ? '+' : ''}${n} events of 4 detents.`,
      {
        send: true,
        sweeps: [
          [2, 20],
          [3, n],
        ],
      },
    ),
  );
for (const [name, sweeps] of [
  ['mix', [[8, 127]]],
  [
    'thr0',
    [
      [8, 127],
      [1, -127],
    ],
  ],
  [
    'ratio',
    [
      [8, 127],
      [1, -127],
      [5, 127],
    ],
  ],
  [
    'makeup',
    [
      [8, 127],
      [4, 127],
    ],
  ],
] as [string, [number, number][]][])
  scenarios.push(
    build(
      `sf-mst-${name}`,
      `Master compressor page, encoders [${sweeps.map(([encoder]) => encoder).join(', ')}] turned; Trig 1 held 1.5 s.`,
      { fx: 'mst', sweeps, hold: 1.5, tail: 1 },
    ),
  );
export const document = { scenarios };
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) console.log(JSON.stringify(document));
