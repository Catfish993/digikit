// The pure logic of the Card panel (no DOM, no Tauri): the folder status of `card.folder` and its texts.
// Written without TS-only runtime syntax so node can import it (apps/web/tests/card.test.mjs).

export interface CardFile {
  /** Relative to `digikit/` of the folder. */
  path: string;
  bytes: number;
  sha256: string;
}

/** The answer of the controller's `card.folder` / `card.sync`. */
export interface CardFolder {
  folder: string | null;
  /** The module store is mirrored to the folder while the emulator runs. */
  continuous: boolean;
  files: CardFile[];
  /** ms since the epoch of the last pass, or null. */
  last_sync: number | null;
  /** The card has writes the folder does not hold yet. */
  behind?: boolean;
  error: string | null;
}

/** "never", "just now", "12 s ago", "3 min ago", "2 h ago". */
export function agoText(lastSync: number | null, now: number): string {
  if (lastSync === null) return 'never';
  const s = Math.max(0, Math.round((now - lastSync) / 1000));
  if (s < 3) return 'just now';
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}

/** The status line under the file list. */
export function statusText(c: CardFolder, now: number): string {
  if (!c.continuous) return 'The card is not mirrored to a folder.';
  if (c.error) return `Sync failed: ${c.error}`;
  const when = `Last sync ${agoText(c.last_sync, now)}`;
  return c.behind ? `${when}; the card has changes not synced yet` : when;
}

/** The folder's shortened path for the header: the last two parts, the whole path stays the tooltip. */
export function shortFolder(folder: string | null): string {
  if (!folder) return '';
  const parts = folder.split('/').filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join('/')}` : folder;
}

/** The files as shown: the module store files by path. */
export function fileRows(c: CardFolder): CardFile[] {
  return [...c.files].sort((a, b) => a.path.localeCompare(b.path));
}
