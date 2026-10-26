import { setTimeout as delay } from 'node:timers/promises';
import { captureRemoteOled, prepareRemote, remoteTap, withRemoteRelease } from './remote.ts';
import { expect } from 'vitest';
import type { PatchedSession } from '../support.ts';
import { openDigikitMenu, openRuntimeAbout, expectRuntimeAbout } from './runtime-menu.ts';

/** Boot identity and health only: runtime patch sets do not install remote-control modules. */
export async function expectRuntimeBoot({ session, dk }: PatchedSession): Promise<void> {
  const identity = session.loadResult.runtime;
  expect(identity?.patched).toBe(true);
  if (!identity?.patched) throw new Error('stock input did not produce a runtime patch identity');
  expect(identity.set).toBe(session.options.runtime?.patchSet ?? 'emulator');
  expect(identity.base_sha256).toMatch(/^[a-f0-9]{64}$/);
  expect(identity.modules.length).toBeGreaterThan(0);
  expect(identity.cache_file).toBeTruthy();
  expect(session.loadResult.start_used).toBeTruthy();
  await expect.poll(async () => (await session.status()).cf.ready, { timeout: 20_000 }).toBe(true);
  const status = await session.status();
  expect(status.mode).toBe('free');
  expect(status.cf.ready).toBe(true);
  expect(status.firmware?.runtime).toEqual(identity);
  expect(dk.rpcChunk).toBe(16384);
  await session.expectNoFault('runtime boot');
}

/** KEY navigation opens semantic ABOUT content; TURN reports accepted signed deltas. */
export async function expectRemoteKeys(runtime: PatchedSession): Promise<void> {
  const { session, dk } = runtime;
  // Preserve live_boot's device-specific pre-install settling, then the remote scenario's own settle.
  await delay(session.options.device === 'dn2' ? 45_000 : 12_000);
  await withRemoteRelease(dk, async () => {
    const plan = await prepareRemote(runtime);
    await delay(10_000);
    expect((await dk.remoteState()).enabled).toBe(true);
    let shots = 0;
    const shot = (name: string) => captureRemoteOled(session, `${String(++shots).padStart(2, '0')}-${name}`);
    const tap = async (name: string, settleMs = 1200) => {
      await remoteTap(dk, name, settleMs);
      await shot(name.toLowerCase());
    };
    await shot('start');
    await openDigikitMenu(tap);
    await openRuntimeAbout(tap);
    await expectRuntimeAbout(session, dk, plan);
    for (let i = 0; i < 4; i++) await tap('NO', 2000);
    try {
      expect(await dk.remoteTurnOnce(1, 20)).toBe(20);
      expect(await dk.remoteTurnOnce(1, -20)).toBe(-20);
    } finally {
      await shot('turn-signed');
    }
  });
  await session.expectNoFault('remote keys');
}
