// The pure logic of the Transfer page (no DOM, no Tauri): the two lists, the install request, the progress bar and the texts.
// Written without TS-only runtime syntax so node can import it (apps/web/tests/transfer.test.mjs).
//
// The page only puts files on the card (upload and verify) and restarts the device. Which modules the device loads is its own
// selection, made in SETTINGS > DIGIKIT > MODULES on the device and applied there.

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

/** The key of a row in the tick set. */
export const rowKey = (r: TransferRow): string => r.id ?? r.path;

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

/** The module ids to install: the ticked repo modules, in list order. (Ticks mean "install these"; nothing else.) */
export function installModules(view: TransferView, ticks: Set<string>): string[] {
  return view.modules.filter((r) => r.id && ticks.has(rowKey(r))).map((r) => r.id as string);
}

/** A readable line for a progress event of `dktransfer` (`install`, `restart`). */
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
    case 'verify':
      return e.ok ? `Verified ${e.path} on the card` : `Verify failed: ${e.path} differs on the card`;
    case 'restart':
      return 'Restarting the device';
    case 'wait':
      return 'Waiting for the device to boot';
    case 'done':
      if (e.restarted === false) return DONE_ON_CARD;
      return Array.isArray(e.loaded) && e.loaded.length
        ? `Restarted. Loaded: ${s(e.loaded)}`
        : 'Restarted. No module is loaded.';
    case 'error':
      return `Error: ${e.message}`;
    default:
      return e.event;
  }
}

/** The text after a successful install. */
export const DONE_ON_CARD = 'On the card. Select it in MODULES on the device, then apply and restart.';

/** The fit warning of a row, or ''. */
export function warningText(r: TransferRow): string {
  return r.warnings && r.warnings.length ? r.warnings.join('; ') : '';
}

/** The state words of a row: loaded or on the card, and the build state. */
export function stateText(r: TransferRow): string {
  const out: string[] = [];
  if (r.loaded) out.push('loaded');
  else
    out.push(r.on_card === 'yes' ? 'on card' : r.on_card === 'other version' ? 'other version on card' : 'not on card');
  if (r.build === 'needs build') out.push('needs build');
  return out.join(', ');
}

/** Per store path: what VERIFY said after the last install. */
export type Verified = Record<string, 'ok' | 'failed'>;

// ---- the progress bar ---------------------------------------------------------------------------------------------------------

export type StepState = 'wait' | 'build' | 'upload' | 'verify' | 'ok' | 'failed';
export interface Progress {
  /** One entry per module of an install, in order (none for a restart). */
  modules: { id: string; state: StepState }[];
  /** A restart has one step; an install has none. */
  restart: StepState | 'none';
  message: string;
}

export function startProgress(ids: string[], restart: boolean): Progress {
  return { modules: ids.map((id) => ({ id, state: 'wait' })), restart: restart ? 'wait' : 'none', message: '' };
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
    case 'restart':
      next.restart = 'build';
      break;
    case 'done':
      if (next.restart !== 'none') next.restart = 'ok';
      break;
    case 'error': {
      const active = next.modules.find((m) => m.state === 'build' || m.state === 'upload' || m.state === 'verify');
      if (active) active.state = 'failed';
      else if (next.restart === 'build') next.restart = 'failed';
      break;
    }
  }
  return next;
}

/** Done steps over all steps, 0 to 1. */
export function progressFraction(p: Progress): number {
  const steps = [...p.modules.map((m) => m.state), ...(p.restart === 'none' ? [] : [p.restart])];
  return steps.length ? steps.filter((s) => s === 'ok').length / steps.length : 0;
}

/** The verified map after more events: a `verify` event sets its path. */
export function applyVerified(v: Verified, e: TransferEvent): Verified {
  if (e.event !== 'verify' || typeof e.path !== 'string') return v;
  return { ...v, [e.path]: e.ok ? 'ok' : 'failed' };
}
