// The result of `dev.reload` (docs/CONTROL.md): the daemon applies the modules again from the objects on disk and brings the running
// session to them. It never builds; `mise run dk:dev` or `module:build` does that first.

export interface ReloadReport {
  /** `swap`: UI module bytes written into a restored main-screen state. `boot`: the new image was loaded (a cold boot unless it was
   * booted before). `dsp`: only the DSP section changed (hot injection). `none`: the running image already is the new one. */
  path: 'swap' | 'boot' | 'dsp' | 'none';
  /** Why a boot was needed (`boot` and `dsp`). */
  reason?: string;
  /** Milliseconds of a `swap` or `none`. */
  ms?: number;
  /** Seconds of a `boot` or `dsp` (the report of `firmware.load`). */
  seconds?: number;
  ranges?: number;
  bytes?: number;
  owners?: string[];
}

const wall = (report: ReloadReport) =>
  report.ms !== undefined
    ? `${Math.round(report.ms)} ms`
    : report.seconds !== undefined
      ? `${report.seconds.toFixed(1)} s`
      : '';

/** One line for the status area. */
export const reloadLine = (report: ReloadReport): string => {
  const time = wall(report);
  switch (report.path) {
    case 'none':
      return `Reload: nothing changed (${time})`;
    case 'swap':
      return `Reload: swapped ${report.owners?.join(', ') || 'no module'}, ${report.bytes ?? 0} bytes in ${report.ranges ?? 0} ranges (${time})`;
    case 'dsp':
      return `Reload: DSP only, injected (${time})`;
    default:
      return `Reload: booted the new image, ${report.reason ?? 'the core changed'} (${time})`;
  }
};

/** An error of `dev.reload` for the status area: the daemon's text, without the method prefix it adds. */
export const reloadError = (error: unknown): string => {
  const text = error instanceof Error ? error.message : String(error);
  return `Reload failed: ${text.replace(/^dev\.reload:\s*/, '')}`;
};
