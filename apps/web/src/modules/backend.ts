// What the Modules panel needs from a transport. The desktop app implements it with the native commands (the running
// emulator), the site with Web MIDI (a device). Written without TS-only runtime syntax so node can import it.

import type { DkEntry, DkInfo, DkProgress } from '@digi/dk-session';

export interface ModuleBackend {
  /** "Emulator", "Digitakt II (USB)". */
  label: string;
  /** `quick`: one short INFO, no retries (is a runtime there?); it fails with kind "timeout" when nothing answers. */
  info(quick?: boolean): Promise<DkInfo>;
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

/** Whether an error is "no reply" (the device never answered). */
export function isTimeout(e: unknown): boolean {
  const raw = e instanceof Error ? e.message : String(e);
  return (e as { kind?: string })?.kind === 'timeout' || /"kind":\s*"timeout"/.test(raw);
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1024 / 1024).toFixed(2)} MiB`;
}
