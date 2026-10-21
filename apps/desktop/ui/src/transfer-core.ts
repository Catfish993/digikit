// The pure logic of the Transfer panel (no DOM, no Tauri): grouping, the initial ticks, the install request and the progress text.
// Written without TS-only runtime syntax so node can import it (apps/web/tests/transfer.test.mjs).

export type Group = 'Machines' | 'Effects' | 'Tools';
export const GROUPS: Group[] = ['Machines', 'Effects', 'Tools'];

/** One row of `dktransfer list --json`: a repo module (`id` set) or a card file that matches no repo module (`id` null). */
export interface TransferRow {
  id: string | null;
  path: string;
  name: string;
  label?: string;
  group: Group;
  test: boolean;
  version: string | null;
  file_size: number;
  arena?: number;
  dsp?: number;
  build?: 'unchanged' | 'needs build';
  on_card: 'yes' | 'no' | 'other version';
  loaded: boolean;
  fits?: boolean | null;
  warnings?: string[];
  known?: boolean;
}

export interface TransferView {
  device: string;
  runtime: string;
  free_bytes: number;
  loaded: string[];
  modules: TransferRow[];
  card_only: TransferRow[];
}

export interface TransferEvent {
  event: string;
  [k: string]: unknown;
}

export interface InstallRequest {
  modules: string[];
  keep: string[];
}

/** The key of a row in the tick set. */
export const rowKey = (r: TransferRow): string => r.id ?? r.path;

/** Rows per group, in the tool's order; test modules only when `showTests`; card files join the group of their header. */
export function groupRows(view: TransferView, showTests: boolean): { group: Group; rows: TransferRow[] }[] {
  const all = [...view.modules, ...view.card_only].filter((r) => showTests || !r.test);
  return GROUPS.map((group) => ({ group, rows: all.filter((r) => r.group === group) }));
}

/** The ticks a fresh list starts with: what is loaded now. */
export function initialTicks(view: TransferView): Set<string> {
  return new Set([...view.modules, ...view.card_only].filter((r) => r.loaded).map(rowKey));
}

/** Install the ticked repo modules; keep the ticked card files (loaded modules the repo does not know). */
export function installRequest(view: TransferView, ticks: Set<string>): InstallRequest {
  return {
    modules: view.modules.filter((r) => r.id && ticks.has(rowKey(r))).map((r) => r.id as string),
    keep: view.card_only.filter((r) => ticks.has(rowKey(r))).map((r) => r.path),
  };
}

/** Does the install change anything? (The ticks equal what is loaded: nothing to do.) */
export function differsFromLoaded(view: TransferView, ticks: Set<string>): boolean {
  const now = initialTicks(view);
  return now.size !== ticks.size || [...ticks].some((k) => !now.has(k));
}

/** A readable line for a progress event of `dktransfer install --json`. */
export function eventText(e: TransferEvent): string {
  const s = (v: unknown) => (Array.isArray(v) ? v.join(', ') : String(v));
  switch (e.event) {
    case 'start':
      return `Installing ${s(e.modules)} on ${e.device}`;
    case 'build':
      return `Building ${e.id}`;
    case 'upload':
      return `Uploading ${e.path} (${e.bytes} bytes)`;
    case 'skip':
      return `${e.id}: already on the card, not uploaded`;
    case 'select':
      return `Selecting ${s(e.paths)}`;
    case 'restart':
      return 'Restarting the device';
    case 'wait':
      return `Waiting for ${s(e.modules)} to load`;
    case 'verify':
      return e.ok ? `Verified ${e.path} on the card` : `Verify failed: ${e.path} differs on the card`;
    case 'done':
      if (e.restarted === false) return 'Selected; the modules load at the next restart';
      return Array.isArray(e.missing) && e.missing.length
        ? `Loaded: ${s(e.loaded)}; not loaded: ${s(e.missing)}`
        : `Loaded: ${s(e.loaded)}`;
    case 'error':
      return `Error: ${e.message}`;
    default:
      return e.event;
  }
}

/** The fit warning of a row, or ''. */
export function warningText(r: TransferRow): string {
  return r.warnings && r.warnings.length ? r.warnings.join('; ') : '';
}

/** What the last install selected (`undefined`: nothing since the app started: the loaded modules are the selection). */
export type Selected = Set<string> | undefined;

/** Per store path: what VERIFY said after the last install. */
export type Verified = Record<string, 'ok' | 'failed'>;

/** The state words of a row. After an install without a restart the selection differs from what is loaded: `selected` says which. */
export function stateText(r: TransferRow, selected?: Selected): string {
  const out: string[] = [];
  const key = rowKey(r);
  if (r.loaded) out.push(selected && !selected.has(key) ? 'loaded, unloads at restart' : 'loaded');
  else if (selected?.has(key) && r.on_card === 'yes') out.push('on card, loads at restart');
  else
    out.push(r.on_card === 'yes' ? 'on card' : r.on_card === 'other version' ? 'other version on card' : 'not on card');
  if (r.build === 'needs build') out.push('needs build');
  return out.join(', ');
}

/** The "On device" list: every file on the card (repo modules that are on it, then files the repo does not know), by path. */
export function onDevice(view: TransferView): TransferRow[] {
  const mine = view.modules.filter((r) => r.on_card !== 'no');
  return [...mine, ...view.card_only].sort((a, b) => a.path.localeCompare(b.path));
}

/** The "Available" list: the repo modules in the three groups (test modules behind the toggle). */
export function available(view: TransferView, showTests: boolean): { group: Group; rows: TransferRow[] }[] {
  return GROUPS.map((group) => ({
    group,
    rows: view.modules.filter((r) => r.group === group && (showTests || !r.test)),
  }));
}

/** Does the card hold something the next boot will load differently from now? (A selection was written and differs from what is loaded.) */
export function restartPending(view: TransferView, selected: Selected): boolean {
  if (!selected) return false;
  return differsFromLoaded(view, selected);
}

// ---- the progress bar of an install ------------------------------------------------------------------------------------------------

export type StepState = 'wait' | 'build' | 'upload' | 'verify' | 'ok' | 'failed';
export interface Progress {
  /** One entry per module, in install order. */
  modules: { id: string; state: StepState }[];
  /** The last step after the modules: SELECT, then (with a restart) the restart. */
  select: StepState;
  restart: StepState | 'none';
  message: string;
}

export function startProgress(ids: string[], restart: boolean): Progress {
  return {
    modules: ids.map((id) => ({ id, state: 'wait' })),
    select: 'wait',
    restart: restart ? 'wait' : 'none',
    message: '',
  };
}

/** The progress after one more event of `dktransfer` (a new object; `error` marks the active step failed). */
export function applyEvent(p: Progress, e: TransferEvent): Progress {
  const next: Progress = { ...p, modules: p.modules.map((m) => ({ ...m })), message: eventText(e) };
  const mod = (id: unknown) => next.modules.find((m) => m.id === id);
  switch (e.event) {
    case 'build':
    case 'upload':
    case 'verify': {
      const m = mod(e.id);
      if (m) m.state = e.event === 'verify' ? (e.ok ? 'ok' : 'failed') : (e.event as StepState);
      break;
    }
    case 'skip':
      break; // the verify that follows ends the module
    case 'select':
      next.select = 'ok';
      break;
    case 'restart':
      next.restart = 'build';
      break;
    case 'done':
      if (next.restart !== 'none') next.restart = Array.isArray(e.missing) && e.missing.length ? 'failed' : 'ok';
      break;
    case 'error': {
      const active = next.modules.find((m) => m.state === 'build' || m.state === 'upload' || m.state === 'verify');
      if (active) active.state = 'failed';
      else if (next.restart === 'build') next.restart = 'failed';
      else if (next.select !== 'ok') next.select = 'failed';
      break;
    }
  }
  return next;
}

/** Done steps over all steps, 0 to 1. */
export function progressFraction(p: Progress): number {
  const steps = [...p.modules.map((m) => m.state), p.select, ...(p.restart === 'none' ? [] : [p.restart])];
  return steps.filter((s) => s === 'ok').length / steps.length;
}

/** The verified map after more events: a `verify` event sets its path. */
export function applyVerified(v: Verified, e: TransferEvent): Verified {
  if (e.event !== 'verify' || typeof e.path !== 'string') return v;
  return { ...v, [e.path]: e.ok ? 'ok' : 'failed' };
}
