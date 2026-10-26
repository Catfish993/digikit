/** DN2 1.12 track FX: fixed instruction slots preserve encoder acceleration and chunk boundaries. */
import { pathToFileURL } from 'node:url';

export type Event = { at_instr: number } & ({ button: number; down: boolean } | { turn: number; detents: number });
export interface Scenario {
  name: string;
  description: string;
  seconds: number;
  exact_events: boolean;
  events: Event[];
}
const hz = 132_000_000;
const slot = 1_000_000;
// Number of +4-detent events needed for each measured UI value (not linear).
const ups: Record<number, number> = { 28: 22, 44: 26, 71: 30, 93: 40, 127: 44 };
const levels = [28, 44, 71, 93, 127];
const controls = [
  [1, 'br'],
  [2, 'over'],
  [3, 'srr'],
] as const;
const sends = [
  [5, 'del'],
  [6, 'rev'],
  [7, 'chr'],
] as const;

interface Settings {
  encoder?: number;
  ui?: number;
  route?: number;
  tap?: boolean;
  filter?: boolean;
}
function build(
  name: string,
  description: string,
  { encoder = 1, ui = 0, route = 0, tap = false, filter = false }: Settings = {},
): Scenario {
  const events: Event[] = [];
  const button = (at: number, code: number, down: boolean) => events.push({ at_instr: at, button: code, down });
  const turn = (at: number, code: number, detents: number) => events.push({ at_instr: at, turn: code, detents });
  const page = (at: number, code: number) => {
    button(at, code, true);
    button(at + 2 * slot, code, false);
  };
  const sweep = (start: number, code: number, count: number, active: number) => {
    for (let i = 0; i < count; i++) turn(start + i * slot, code, i < count - active ? 0 : 4);
  };
  const offset = filter ? 70 * slot : 0;
  if (filter) {
    page(slot, 3);
    for (let i = 0; i < 46; i++) turn(5 * slot + i * slot, 5, -4);
  }
  page(slot + offset, 5);
  sweep(20 * slot + offset, encoder, 160, ups[ui] ?? 0);
  sweep(180 * slot + offset, route || 4, 10, route ? 8 : 0);
  const note = 210 * slot + offset;
  const release = note + Math.round((tap ? 0.3 : 1.5) * hz);
  button(note, 25, true);
  button(release, 25, false);
  return {
    name,
    description,
    seconds: Math.ceil((release / hz + (tap ? 4 : 1)) * 100) / 100,
    exact_events: true,
    events: events.sort((a, b) => a.at_instr - b.at_instr),
  };
}

export const scenarios: Scenario[] = [build('tf-dry-hold', 'DN2 track FX page, no turns, Trig 1 held 1.5 s.')];
for (const [encoder, name] of controls) {
  for (const ui of levels)
    scenarios.push(
      build(
        `tf-${name}-${ui}`,
        `DN2 track FX page: encoder ${encoder} (${name}) to about UI ${ui}, Trig 1 held 1.5 s.`,
        { encoder, ui },
      ),
    );
  scenarios.push(build(`tf-${name}-0`, `As tf-dry-hold, with zero-detent turns on encoder ${encoder}.`, { encoder }));
}
for (const [encoder, route, name] of [
  [3, 4, 'srr'],
  [2, 8, 'over'],
] as const) {
  for (const ui of [71, 127])
    scenarios.push(
      build(`tf-${name}-${ui}-rt1`, `As tf-${name}-${ui}, with the routing encoder ${route} set to 1 (256).`, {
        encoder,
        ui,
        route,
      }),
    );
}
scenarios.push(build('tf-dry-tap', 'DN2 track FX page, no turns, Trig 1 tap (0.3 s), 4 s tail.', { tap: true }));
for (const [encoder, name] of sends) {
  for (const ui of [28, 71, 127])
    scenarios.push(
      build(
        `tf-${name}-${ui}`,
        `DN2 track FX page: send encoder ${encoder} (${name}) to about UI ${ui}, Trig 1 tap, 4 s tail.`,
        { encoder, ui, tap: true },
      ),
    );
  scenarios.push(
    build(`tf-${name}-0`, `As tf-dry-tap, with zero-detent turns on encoder ${encoder}.`, { encoder, tap: true }),
  );
}
for (const ui of [71, 127])
  scenarios.push(
    build(
      `tf-chr-${ui}-hold`,
      `DN2 track FX page: chorus send to about UI ${ui}, Trig 1 held 1.5 s, for the modulation rate.`,
      { encoder: 7, ui },
    ),
  );
scenarios.push(
  build('tf-dry-flt', 'Filter Frequency lowered to about UI 60, FX page, no FX, Trig 1 held 1.5 s.', { filter: true }),
);
for (const [encoder, route, name] of [
  [3, 4, 'srr'],
  [2, 8, 'over'],
] as const) {
  for (const ui of [71, 127])
    for (const routing of [0, route]) {
      const suffix = routing ? '-rt1' : '';
      scenarios.push(
        build(`tf-${name}-${ui}-flt${suffix}`, `As tf-${name}-${ui}${suffix}, with the filter closed first.`, {
          encoder,
          ui,
          route: routing,
          filter: true,
        }),
      );
    }
}
export const document = { scenarios };
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) console.log(JSON.stringify(document));
