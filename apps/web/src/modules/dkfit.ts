// The fit rule: does a module, from the needs its .dkm header declares, fit the capacity of a device (docs/plans/what-fits.md)?
// The rule is written three times and kept equal by one set of vectors (sdk/fit-vectors.json): `dkrt_sdk::fit` (Rust: the runtime and the
// DIGIKIT menu), this file (the host app) and tools/device/dkfit.py (dev tools). Written without TS-only runtime syntax so node can import it.
import { DKM_NEEDS } from './gen_proto.ts';

/** CAPACITY as the session returns it: kind name -> limit, used, largest free piece. */
export type Caps = Record<string, { limit: number; used: number; largest: number }>;

/** The declared needs of a .dkm (names of `[dkm.needs]` in sdk/sdk.toml) and its file size. Missing = 0. */
export type Needs = { file_size?: number } & Partial<Record<keyof typeof DKM_NEEDS, number>>;

/** The first short resource: its CAPACITY kind name, what the module needs and what is free (bytes, or a count). */
export type Short = { resource: string; need: number; free: number };

/** The arena's block header (arena.rs). */
const HDR = 16;

/** The order the first short resource is looked for in: the loader's own order of refusals, with the slots first. */
const ORDER = [
  'STAGE',
  'MODULES',
  'SELECTION',
  'DSP_AREA',
  'ARENA',
  'VOICE_ROWS',
  'MACHINE_ROWS',
  'PARAM_ROWS',
  'PAGE_ROWS',
  'FX_SLOTS',
] as const;
const ROWS = ['VOICE_ROWS', 'MACHINE_ROWS', 'PARAM_ROWS', 'PAGE_ROWS', 'FX_SLOTS'] as const;

/** The needs a .dkm declares, from its header (at least `DKM_HEADER` bytes) and file size. */
export function needsOf(h: Uint8Array): Needs {
  const v = new DataView(h.buffer, h.byteOffset, h.length);
  const out: Needs = { file_size: v.getUint32(8) };
  for (const [name, at] of Object.entries(DKM_NEEDS)) out[name as keyof typeof DKM_NEEDS] = v.getUint32(at);
  return out;
}

const cap = (caps: Caps, kind: string) => caps[kind] ?? { limit: 0, used: 0, largest: 0 };
const free = (c: { limit: number; used: number }) => Math.max(c.limit - c.used, 0);

/** DSP area bytes with the room a placement can lose to alignment (up to ALIGN - 8 bytes). */
export function dspNeed(n: Needs): number {
  const a = n.DSP_AREA ?? 0;
  return a ? a + Math.max(n.DSP_ALIGN ?? 0, 8) - 8 : 0;
}

/** null when the module fits the capacity, else the first short resource. */
export function fitsNow(n: Needs, caps: Caps): Short | null {
  for (const kind of ORDER) {
    const c = cap(caps, kind);
    let need: number;
    let room: number;
    if (kind === 'STAGE') {
      need = n.file_size ?? 0;
      room = c.largest;
    } else if (kind === 'MODULES' || kind === 'SELECTION') {
      need = 1;
      room = free(c);
    } else if (kind === 'DSP_AREA') {
      need = dspNeed(n);
      room = Math.min(free(c), c.largest);
    } else if (kind === 'ARENA') {
      // the module's block and the block of its DSP image are in the arena together until the transfer ends
      const total = (n.ARENA ?? 0) + (n.ARENA_XFER ?? 0);
      if (total > free(c)) return { resource: kind, need: total, free: free(c) };
      need = Math.max(n.ARENA ?? 0, n.ARENA_XFER ?? 0);
      room = c.largest + HDR;
    } else {
      need = n[kind] ?? 0;
      room = free(c);
    }
    if (need > room) return { resource: kind, need, free: room };
  }
  return null;
}

/** A copy of `caps` with the module's needs taken out (what is left once it is loaded). */
export function take(n: Needs, caps: Caps): Caps {
  const out: Caps = {};
  for (const [k, v] of Object.entries(caps)) out[k] = { ...v };
  const use = (kind: string, amount: number) => {
    const c = (out[kind] ??= { limit: 0, used: 0, largest: 0 });
    c.used += amount;
    c.largest = Math.max(c.largest - amount, 0);
  };
  use('MODULES', 1);
  use('SELECTION', 1);
  use('ARENA', (n.ARENA ?? 0) + (n.ARENA_XFER ?? 0));
  use('DSP_AREA', dspNeed(n));
  for (const kind of ROWS) use(kind, n[kind] ?? 0);
  return out;
}

/** Load the modules in order (the sum rule: each takes its needs out of the capacity; fragmentation is not modelled, so it is conservative).
 *  null when all fit, else the index of the first that does not and what it is short of. */
export function fitsSet(needs: Needs[], caps: Caps): ({ index: number } & Short) | null {
  let left = caps;
  for (let index = 0; index < needs.length; index++) {
    const short = fitsNow(needs[index], left);
    if (short) return { index, ...short };
    left = take(needs[index], left);
  }
  return null;
}

const KIB = 1024;
/** `400 KiB` for a size (rounded up for what is needed, down for what is free); bytes below 1 KiB. */
export function fmtSize(bytes: number, up: boolean): string {
  if (bytes < KIB) return `${bytes} bytes`;
  return `${up ? Math.ceil(bytes / KIB) : Math.floor(bytes / KIB)} KiB`;
}

const LABEL: Record<string, string> = {
  STAGE: 'file',
  MODULES: 'module records',
  SELECTION: 'selection paths',
  DSP_AREA: 'DSP area',
  ARENA: 'memory',
  VOICE_ROWS: 'voice rows',
  MACHINE_ROWS: 'machine rows',
  PARAM_ROWS: 'parameter rows',
  PAGE_ROWS: 'page rows',
  FX_SLOTS: 'effect slots',
};

/** One line for a person: `DSP area 400 KiB needed, 254 KiB free`. */
export function describeShort(s: Short): string {
  const label = LABEL[s.resource] ?? s.resource;
  if (s.resource === 'STAGE' || s.resource === 'DSP_AREA' || s.resource === 'ARENA')
    return `${label} ${fmtSize(s.need, true)} needed, ${fmtSize(s.free, false)} free`;
  return `${label} ${s.need} needed, ${s.free} free`;
}
