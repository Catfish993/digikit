import { setTimeout as delay } from 'node:timers/promises';
import {
  captureRemoteOled,
  planSymbol,
  prepareRemote,
  remoteTap,
  runtimeVersion,
  withRemoteRelease,
} from './remote.ts';
import { expect } from 'vitest';
import type { PatchedSession } from '../support.ts';

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

/** The original remote-keys behavior: KEY opens ABOUT and TURN reaches the firmware UI. */
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
    await tap('NO', 2000);
    await tap('SETTINGS', 3000);
    for (let i = 0; i < 8; i++) await tap('DOWN');
    await tap('YES', 3000); // DIGIKIT, cursor on MODULES
    await tap('DOWN'); // FX ROUTES
    await tap('DOWN'); // ABOUT
    await tap('YES', 3000);
    const base = planSymbol(plan, 'DK_ABOUT');
    const count = await session.client.memPeek(planSymbol(plan, 'DK_ABOUT_N'));
    expect(count).not.toBeNull();
    // The menu's fixed row storage is bounded; a bad count should fail without unbounded RPC reads.
    expect(count).toBeGreaterThan(0);
    expect(count).toBeLessThanOrEqual(24);
    const rows: string[] = [];
    for (let i = 0; i < count!; i++) {
      const row = Buffer.alloc(24);
      for (let offset = 0; offset < 24; offset += 4) {
        const word = await session.client.memPeek(base + i * 24 + offset);
        if (word === null) throw new Error('ABOUT row is unmapped');
        row.writeUInt32BE(word >>> 0, offset);
      }
      rows.push(row.toString('latin1').split('\0')[0]!);
    }
    expect(rows).toContain(`RUNTIME ${await runtimeVersion()}`);
    for (let i = 0; i < 4; i++) await tap('NO', 2000);
    const before = await session.frameHash();
    await dk.remoteTurn(1, 20);
    try {
      await expect.poll(() => session.frameHash(), { timeout: 8000, interval: 400 }).not.toBe(before);
    } finally {
      await shot('turn+');
    }
  });
  await session.expectNoFault('remote keys');
}
