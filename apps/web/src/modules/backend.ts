// What the Modules panel needs from a transport. The desktop app implements it with the native commands (the running
// emulator), the site with Web MIDI (a device). Written without TS-only runtime syntax so node can import it.

import type { DkEntry, DkInfo, DkProgress } from './dkupload-core.ts';

export interface ModuleBackend {
  /** "Emulator", "Digitakt II (USB)". */
  label: string;
  info(): Promise<DkInfo>;
  list(): Promise<DkEntry[]>;
  /** Upload one file; `onProgress` returns false to cancel. */
  put(path: string, data: Uint8Array, onProgress: (p: DkProgress) => boolean | void): Promise<void>;
  remove(path: string): Promise<void>;
  clear(): Promise<void>;
}

/** A readable line for an error from either backend: the status name first, then its meaning. */
export function errorLine(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  try {
    const j = JSON.parse(raw);
    if (j && typeof j.error === 'string') return j.error;
  } catch {
    /* plain text */
  }
  return raw;
}

/** Whether an error is the user's cancel. */
export function isCancelled(e: unknown): boolean {
  const raw = e instanceof Error ? e.message : String(e);
  return (e as { kind?: string })?.kind === 'cancelled' || /"kind":"cancelled"/.test(raw);
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1024 / 1024).toFixed(2)} MiB`;
}
