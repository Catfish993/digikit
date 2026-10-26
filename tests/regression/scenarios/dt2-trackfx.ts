/** DT2 1.17 one-sample card FX. Fixed slots preserve measured encoder acceleration. */
import { pathToFileURL } from 'node:url';
import type { Event, Scenario } from './dn2-trackfx.ts';

export const hz = 132_000_000;
const slot = 1_000_000;
const ups: Record<number, number> = { 22: 24, 54: 30, 127: 52 };

/** Panel timeline shared by the DT2 track and send sweeps. */
export class Timeline {
  events: Event[] = [];
  button(at: number, button: number, down: boolean) {
    this.events.push({ at_instr: at, button, down });
  }
  turn(at: number, turn: number, detents: number) {
    this.events.push({ at_instr: at, turn, detents });
  }
  page(at: number, button = 5) {
    this.button(at, button, true);
    this.button(at + 2 * slot, button, false);
  }
  chord(at: number, key: number) {
    this.button(at, 17, true);
    this.button(at + slot, key, true);
    this.button(at + 2 * slot, key, false);
    this.button(at + 3 * slot, 17, false);
  }
  sweep(start: number, encoder: number, count: number, active: number, step = 4) {
    for (let i = 0; i < count; i++) this.turn(start + i * slot, encoder, i < count - active ? 0 : step);
  }
  prelude() {
    // NO closes the boot modal before the first page/encoder event.
    this.button(0, 12, true);
    this.button(slot, 12, false);
  }
  finish(name: string, description: string, note: number, hold: number, tail: number): Scenario {
    const release = note + Math.round(hold * hz);
    this.button(note, 25, true);
    this.button(release, 25, false);
    return {
      name,
      description,
      seconds: Math.ceil((release / hz + tail) * 100) / 100,
      exact_events: true,
      events: this.events.sort((a, b) => a.at_instr - b.at_instr),
    };
  }
}

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
  const tl = new Timeline();
  tl.prelude();
  const offset = filter ? 60 * slot : 0;
  if (filter) {
    tl.page(4 * slot, 3);
    for (let i = 0; i < 46; i++) tl.turn(10 * slot + i * slot, 5, -4);
  }
  tl.page(4 * slot + offset);
  tl.sweep(14 * slot + offset, encoder, 56, filter ? (ui ? 46 : 0) : (ups[ui] ?? 0));
  tl.sweep(70 * slot + offset, route || 4, 10, route ? 8 : 0);
  return tl.finish(name, description, 86 * slot + offset, tap ? 0.3 : 1.2, tap ? 3 : 0.2);
}
const dev = 'DT2 track FX page';
export const scenarios: Scenario[] = [build('tf-dry-hold', `${dev}, no turns, Trig 1 held 1.2 s.`)];
for (const [encoder, name] of [
  [1, 'br'],
  [2, 'over'],
  [3, 'srr'],
] as const)
  for (const ui of [22, 54, 127])
    scenarios.push(
      build(`tf-${name}-${ui}`, `${dev}: encoder ${encoder} (${name}) to about UI ${ui}, Trig 1 held 1.2 s.`, {
        encoder,
        ui,
      }),
    );
scenarios.push(build('tf-br-0', 'As tf-dry-hold, with zero-detent turns on encoder 1.'));
for (const [encoder, route, name] of [
  [3, 4, 'srr'],
  [2, 8, 'over'],
] as const)
  scenarios.push(
    build(`tf-${name}-127-rt1`, `As tf-${name}-127, with the routing encoder ${route} set to 1 (256).`, {
      encoder,
      route,
      ui: 127,
    }),
  );
scenarios.push(build('tf-dry-tap', `${dev}, no turns, Trig 1 tap (0.3 s), 3 s tail.`, { tap: true }));
for (const [encoder, name] of [
  [5, 'del'],
  [6, 'rev'],
  [7, 'chr'],
] as const)
  for (const ui of [54, 127])
    scenarios.push(
      build(`tf-${name}-${ui}`, `${dev}: send encoder ${encoder} (${name}) to about UI ${ui}, Trig 1 tap, 3 s tail.`, {
        encoder,
        ui,
        tap: true,
      }),
    );
scenarios.push(
  build('tf-dry-flt', 'Filter Frequency lowered to about UI 60, FX page, no FX, Trig 1 held 1.2 s.', { filter: true }),
);
for (const [encoder, route, name] of [
  [3, 4, 'srr'],
  [2, 8, 'over'],
] as const)
  for (const routing of [0, route]) {
    const suffix = routing ? '-rt1' : '';
    scenarios.push(
      build(`tf-${name}-127-flt${suffix}`, `As tf-${name}-127${suffix}, with the filter closed first.`, {
        encoder,
        route: routing,
        ui: 127,
        filter: true,
      }),
    );
  }
/** Encoder calibration: same fixed layout, without playing the sample. */
export function ladder(): Scenario[] {
  const calibration: Scenario[] = [];
  for (const filter of [false, true]) {
    for (let count = 12; count <= 56; count += 2) {
      const tl = new Timeline();
      tl.prelude();
      const offset = filter ? 60_000_000 : 0;
      if (filter) {
        tl.page(4_000_000, 3);
        for (let i = 0; i < 46; i++) tl.turn(10_000_000 + i * 1_000_000, 5, -4);
      }
      tl.page(4_000_000 + offset);
      tl.sweep(14_000_000 + offset, 1, 56, count);
      calibration.push({
        name: `lad${filter ? 'f' : ''}-${count}`,
        description: 'BR ladder',
        seconds: Math.ceil(((74_000_000 + offset) / hz) * 100) / 100,
        exact_events: true,
        events: tl.events.sort((a, b) => a.at_instr - b.at_instr),
      });
    }
  }
  return calibration;
}
export const document = { scenarios };
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  console.log(JSON.stringify(process.argv.includes('--ladder') ? { scenarios: ladder() } : document));
