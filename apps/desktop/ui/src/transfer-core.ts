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
    case 'done':
      return `Loaded: ${s(e.loaded)}`;
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

/** The state words of a row. */
export function stateText(r: TransferRow): string {
  const out: string[] = [];
  if (r.loaded) out.push('loaded');
  out.push(r.on_card === 'yes' ? 'on card' : r.on_card === 'other version' ? 'other version on card' : 'not on card');
  if (r.build === 'needs build') out.push('needs build');
  return out.join(', ');
}
