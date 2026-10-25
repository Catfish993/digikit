import { expect } from 'vitest';
import type { PatchedSession } from '../support.ts';

/** Boot identity and health only: runtime patch sets do not install remote-control modules. */
export async function expectRuntimeBoot({ session, dk }: PatchedSession): Promise<void> {
  const identity = session.loadResult.runtime;
  expect(identity?.patched).toBe(true);
  if (!identity?.patched) throw new Error('stock input did not produce a runtime patch identity');
  expect(identity.set).toBe('emulator');
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
