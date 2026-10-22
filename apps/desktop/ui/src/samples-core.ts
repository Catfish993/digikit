// The pure logic of the Samples tab of the Transfer page (no DOM, no Tauri): the tabs, the folder path and its breadcrumb, the rows of the
// device's folder with the state of each upload, and the upload run over a samples backend (apps/web/src/modules/samples.ts).
// Written without TS-only runtime syntax so node can import it (apps/web/tests/samples-page.test.mjs).
import { errorLine, formatBytes } from '../../../web/src/modules/backend.ts';
import type { Prepared } from '../../../web/src/modules/dksample.ts';
import type { SampleEntry, SampleSpace, SamplesBackend } from '../../../web/src/modules/samples.ts';
import type { RowStatus } from './transfer-core.ts';

// ---- the tabs ---------------------------------------------------------------------------------------------------------------------

export type TabId = 'modules' | 'samples';
export const TABS: { id: TabId; label: string }[] = [
  { id: 'modules', label: 'Modules' },
  { id: 'samples', label: 'Samples' },
];
/** The saved tab, or Modules when nothing (or something else) was saved. */
export const parseTab = (v: unknown): TabId => TABS.find((t) => t.id === v)?.id ?? 'modules';

// ---- folders ----------------------------------------------------------------------------------------------------------------------

export const joinPath = (folder: string, name: string): string => (folder ? `${folder}/${name}` : name);
export const parentOf = (folder: string): string => folder.split('/').slice(0, -1).join('/');

export interface Crumb {
  name: string;
  /** The folder this crumb opens ('' is the top). */
  path: string;
}
/** The breadcrumb of a folder: the top, then one crumb per level. */
export function crumbs(folder: string, top = 'Samples'): Crumb[] {
  const out: Crumb[] = [{ name: top, path: '' }];
  let path = '';
  for (const part of folder.split('/').filter(Boolean)) {
    path = joinPath(path, part);
    out.push({ name: part, path });
  }
  return out;
}

/** Why `name` cannot be a new folder here, or '' when it can. */
export function folderNameError(name: string, entries: SampleEntry[]): string {
  if (!name.trim()) return 'Give the folder a name.';
  if (name !== name.trim() || name === '.' || name === '..' || /[/\\\0]/.test(name)) return 'Not a folder name.';
  if (entries.some((e) => e.name === name)) return `${name} exists already.`;
  return '';
}

// ---- uploads ----------------------------------------------------------------------------------------------------------------------

/** One file of an upload run, from "waiting" to its result. A file that could not be prepared (a broken WAV) is `failed` with kind `none`. */
export interface Upload {
  folder: string;
  /** The name on the device (a WAV loses its extension). */
  name: string;
  /** The name of the local file. */
  source: string;
  kind: Prepared['kind'] | 'none';
  state: 'waiting' | 'uploading' | 'verifying' | 'ok' | 'failed';
  sent: number;
  bytes: number;
  /** The compared message of the verify (the tooltip of the chip), or the error. */
  message: string;
}
/** Per device path (`folder/name`). */
export type Uploads = Record<string, Upload>;

export type UploadEvent =
  | { event: 'prepared'; folder: string; name: string; source: string; kind: Prepared['kind']; bytes: number }
  | { event: 'progress'; folder: string; name: string; sent: number; bytes: number }
  | { event: 'verifying'; folder: string; name: string }
  | { event: 'verified'; folder: string; name: string; ok: boolean; message: string }
  | { event: 'failed'; folder: string; name: string; source?: string; message: string };

/** The uploads after one more event (a new object). */
export function applyUpload(u: Uploads, e: UploadEvent): Uploads {
  const key = joinPath(e.folder, e.name);
  const base: Upload = u[key] ?? {
    folder: e.folder,
    name: e.name,
    source: e.event === 'failed' ? (e.source ?? e.name) : e.name,
    kind: 'none',
    state: 'waiting',
    sent: 0,
    bytes: 0,
    message: '',
  };
  switch (e.event) {
    case 'prepared':
      return { ...u, [key]: { ...base, source: e.source, kind: e.kind, bytes: e.bytes, state: 'waiting' } };
    case 'progress':
      return { ...u, [key]: { ...base, state: 'uploading', sent: e.sent, bytes: e.bytes } };
    case 'verifying':
      return { ...u, [key]: { ...base, state: 'verifying', sent: base.bytes } };
    case 'verified':
      return { ...u, [key]: { ...base, state: e.ok ? 'ok' : 'failed', message: e.message } };
    case 'failed':
      return { ...u, [key]: { ...base, state: 'failed', message: e.message } };
  }
}

/** What the page puts on a row or a local file: the label of what was done to the file. */
export function kindLabel(kind: Upload['kind']): string {
  switch (kind) {
    case 'converted':
      return 'WAV, converted';
    case 'native':
      return 'native sample';
    case 'unchanged':
      return "won't play as a sample";
    default:
      return '';
  }
}

/** What a row of the device list shows for its upload: the shape of the Modules page (`RowStatus`), plus waiting and the tooltip. */
export type SampleStatus = (RowStatus | { kind: 'waiting' }) & { message: string };
export function uploadStatus(u: Upload | undefined): SampleStatus | undefined {
  if (!u) return undefined;
  const message = u.message;
  switch (u.state) {
    case 'waiting':
      return { kind: 'waiting', message };
    case 'uploading':
      return u.sent < u.bytes
        ? { kind: 'uploading', sent: u.sent, bytes: u.bytes, message }
        : { kind: 'verifying', message };
    case 'verifying':
      return { kind: 'verifying', message };
    case 'ok':
      return { kind: 'ok', message };
    case 'failed':
      return { kind: 'failed', message };
  }
}

/** The files of a run in the order they were added, for the local pane. */
export const uploadList = (u: Uploads): Upload[] => Object.values(u);

/** The uploads without the given device paths (their files were deleted). */
export function dropUploads(u: Uploads, paths: string[]): Uploads {
  const out = { ...u };
  for (const p of paths) delete out[p];
  return out;
}

// ---- the device's folder ----------------------------------------------------------------------------------------------------------

export interface SampleRow {
  name: string;
  /** The path from the top: the key of ticks and uploads. */
  path: string;
  kind: 'file' | 'folder';
  size: number;
  /** A file of an upload that is not in the listing (yet). */
  pending: boolean;
  status?: SampleStatus;
}

/** The rows of `folder`: folders first, then files, each by name; the files of an upload run that the listing does not hold are rows too. */
export function sampleRows(folder: string, entries: SampleEntry[], uploads: Uploads): SampleRow[] {
  const rows: SampleRow[] = entries.map((e) => {
    const path = joinPath(folder, e.name);
    return {
      name: e.name,
      path,
      kind: e.kind,
      size: e.size,
      pending: false,
      status: e.kind === 'file' ? uploadStatus(uploads[path]) : undefined,
    };
  });
  const have = new Set(rows.map((r) => r.path));
  for (const [path, u] of Object.entries(uploads)) {
    if (u.folder === folder && !have.has(path))
      rows.push({ name: u.name, path, kind: 'file', size: u.bytes, pending: true, status: uploadStatus(u) });
  }
  return rows.sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === 'folder' ? -1 : 1));
}

/** The ticked rows of the folder, in list order (ticks of rows that are gone are dropped). Pending rows are not on the device. */
export const deleteRows = (rows: SampleRow[], ticked: Set<string>): SampleRow[] =>
  rows.filter((r) => !r.pending && ticked.has(r.path));

/** The free space line: "12.00 MiB free of 20480.00 MiB", or just the free part when the device does not give the total. */
export function spaceText(s: SampleSpace | undefined): string {
  if (!s) return '';
  return s.total === null ? `${formatBytes(s.free)} free` : `${formatBytes(s.free)} free of ${formatBytes(s.total)}`;
}

/** The note under the list for a device whose samples go to the digikit store: its firmware cannot play them. `name` is the device's. */
export const storeNote = (samples: string, name: string): string =>
  samples === 'digikit' ? `Stored for a future sampling engine; the ${name} can't play samples yet.` : '';

// ---- an upload run ----------------------------------------------------------------------------------------------------------------

/** What `prepareUpload` of dksample.ts is, with the wasm bound. */
export type Prepare = (name: string, bytes: Uint8Array) => Prepared;

/**
 * Put the files into `folder`: each is prepared (a WAV converted), then sent with its progress, then verified against what was sent. A file that
 * cannot be prepared fails alone; a failed send stops the run (the device or the link is gone) and the files after it fail as not sent.
 * Every step is an event; nothing throws.
 */
export async function uploadFiles(
  backend: SamplesBackend,
  prepare: Prepare,
  folder: string,
  files: { name: string; data: Uint8Array }[],
  emit: (e: UploadEvent) => void,
): Promise<void> {
  const ready: Prepared[] = [];
  for (const f of files) {
    try {
      const p = prepare(f.name, f.data);
      ready.push(p);
      emit({ event: 'prepared', folder, name: p.name, source: f.name, kind: p.kind, bytes: p.stored.length });
    } catch (e) {
      emit({ event: 'failed', folder, name: f.name, source: f.name, message: errorLine(e) });
    }
  }
  let stopped = '';
  for (const p of ready) {
    if (stopped) {
      emit({ event: 'failed', folder, name: p.name, message: `not sent: ${stopped}` });
      continue;
    }
    try {
      await backend.put(folder, p, (sent, bytes) => emit({ event: 'progress', folder, name: p.name, sent, bytes }));
    } catch (e) {
      const message = errorLine(e);
      emit({ event: 'failed', folder, name: p.name, message });
      stopped = `the upload of ${p.name} failed`;
      continue;
    }
    emit({ event: 'verifying', folder, name: p.name });
    try {
      const v = await backend.verify(folder, p);
      emit({ event: 'verified', folder, name: p.name, ok: v.ok, message: `${v.message}\n${v.compared}` });
    } catch (e) {
      emit({ event: 'failed', folder, name: p.name, message: errorLine(e) });
    }
  }
}
