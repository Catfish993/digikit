import { describe, expect, it } from 'vitest';
import { reloadError, reloadLine } from '../../../web/src/runtime/reload.ts';

describe('the reload status line', () => {
  it('names what a swap wrote and how long it took', () => {
    expect(reloadLine({ path: 'swap', ms: 1432.6, ranges: 2, bytes: 1444, owners: ['test/ui-demo'] })).toBe(
      'Reload: swapped test/ui-demo, 1444 bytes in 2 ranges (1433 ms)',
    );
  });
  it('says why a boot was needed, in seconds', () => {
    expect(reloadLine({ path: 'boot', reason: 'core/runtime changed at 0x4671a000', seconds: 5.34 })).toBe(
      'Reload: booted the new image, core/runtime changed at 0x4671a000 (5.3 s)',
    );
  });
  it('reports no change and a DSP-only change', () => {
    expect(reloadLine({ path: 'none', ms: 1200 })).toBe('Reload: nothing changed (1200 ms)');
    expect(reloadLine({ path: 'dsp', seconds: 0.2 })).toBe('Reload: DSP only, injected (0.2 s)');
  });
  it('shows the daemon error without its method prefix', () => {
    expect(reloadError(new Error('dev.reload: the running image has no patch record'))).toBe(
      'Reload failed: the running image has no patch record',
    );
    expect(reloadError('a job is running; wait for it before dev.reload')).toBe(
      'Reload failed: a job is running; wait for it before dev.reload',
    );
  });
});
