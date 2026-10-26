/** DT2 1.17 send FX and compressor on the generated 1 kHz, one-second sample card. */
import { pathToFileURL } from 'node:url';
import type { Scenario } from './dn2-trackfx.ts';
import { Timeline } from './dt2-trackfx.ts';

type Effect = 'del' | 'rev' | 'chr' | 'mst';
type Sweep = [encoder: number, events: number];
function build(
  name: string,
  description: string,
  fx: Effect = 'del',
  send = false,
  sweeps: Sweep[] = [],
  hold = 0.3,
  tail = 2.5,
): Scenario {
  const tl = new Timeline();
  tl.prelude();
  tl.page(4_000_000);
  tl.sweep(14_000_000, { del: 5, rev: 6, chr: 7, mst: 5 }[fx], 56, send ? 52 : 0);
  for (let i = 0; i < { del: 1, rev: 2, chr: 3, mst: 1 }[fx]; i++)
    tl.chord(74_000_000 + i * 6_000_000, fx === 'mst' ? 6 : 5);
  (sweeps.length ? sweeps : [[1, 0]]).forEach(([encoder, events], i) =>
    tl.sweep(98_000_000 + i * 500_000, encoder!, 56, Math.abs(events!), events! >= 0 ? 4 : -4),
  );
  return tl.finish(name, description, 160_000_000, hold, tail);
}
const parameters: [Effect, string, number, number[]][] = [
  ['del', 'time', 1, [-50, 20, 50]],
  ['del', 'pp', 2, [20]],
  ['del', 'width', 3, [-50, 50]],
  ['del', 'fb', 4, [-50, 20, 50]],
  ['del', 'lpf', 6, [-50]],
  ['del', 'rsend', 8, [50]],
  ['del', 'mix', 9, [-50, 50]],
  ['rev', 'pre', 1, [50]],
  ['rev', 'decay', 2, [-50, 20, 50]],
  ['rev', 'lpf', 6, [-50]],
  ['rev', 'mix', 9, [-50]],
  ['chr', 'depth', 1, [-50]],
  ['chr', 'speed', 2, [-50, 50]],
  ['chr', 'mix', 9, [-50]],
  ['chr', 'dsend', 7, [50]],
];
export const scenarios: Scenario[] = [];
for (const [hold, tail, tag] of [
  [0.3, 2.5, 'tap'],
  [1.2, 0.2, 'hold'],
] as const)
  scenarios.push(
    build(
      `sf-dry-${tag}`,
      `DT2 send FX sweep reference: no sends, no turns, Trig 1 held ${hold} s.`,
      'del',
      false,
      [],
      hold,
      tail,
    ),
  );
for (const fx of ['del', 'rev', 'chr'] as const) {
  scenarios.push(build(`sf-${fx}-ref`, `Track ${fx} send at UI 127, send FX page left at its defaults.`, fx, true));
  for (const [effect, parameter, encoder, settings] of parameters) {
    if (effect !== fx) continue;
    for (const n of settings) {
      const tag = n > 0 ? `u${n}` : `d${-n}`;
      scenarios.push(
        build(
          `sf-${fx}-${parameter}-${tag}`,
          `Track ${fx} send at UI 127; send FX page encoder ${encoder} (${parameter}): ${Math.abs(n)} events of ${n > 0 ? '+' : '-'}4 detents.`,
          fx,
          true,
          [[encoder, n]],
        ),
      );
    }
  }
}
for (const n of [-50, 50]) {
  const tag = n > 0 ? `u${n}` : `d${-n}`;
  scenarios.push(
    build(
      `sf-del-ppwidth-${tag}`,
      `Track del send at UI 127; Pingpong on, Stereo Width ${n > 0 ? '+' : ''}${n} events of 4 detents.`,
      'del',
      true,
      [
        [2, 20],
        [3, n],
      ],
    ),
  );
}
for (const [name, sweeps] of [
  ['mix', [[8, 50]]],
  [
    'thr0',
    [
      [8, 50],
      [1, -50],
    ],
  ],
  [
    'ratio',
    [
      [8, 50],
      [1, -50],
      [5, 50],
    ],
  ],
  [
    'makeup',
    [
      [8, 50],
      [4, 50],
    ],
  ],
] satisfies [string, Sweep[]][]) {
  scenarios.push(
    build(
      `sf-mst-${name}`,
      `Master compressor page, encoders [${sweeps.map(([encoder]) => encoder).join(', ')}] turned; Trig 1 held 1.2 s.`,
      'mst',
      false,
      sweeps,
      1.2,
      0.2,
    ),
  );
}
export const document = { scenarios };
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) console.log(JSON.stringify(document));
