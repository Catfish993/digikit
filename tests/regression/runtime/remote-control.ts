import { setTimeout as delay } from 'node:timers/promises';
import { SERVICE_CMD } from '@digi/dk-session/gen_proto';
import { button, type Session } from '@digi/test-support';
import { expect } from 'vitest';
import type { PatchedSession } from '../support.ts';
import { installRemote, withRemoteRelease } from './remote.ts';

/** Physical panel input remains available while the remote protocol is disabled. */
async function panelTap(session: Session, name: string, settleMs = 1200): Promise<void> {
  const code = button(name);
  try {
    await session.client.inputButton(code, true);
    await delay(400);
  } finally {
    await session.client.inputButton(code, false);
  }
  await delay(settleMs);
}

async function toggleRemote(session: Session): Promise<void> {
  await panelTap(session, 'NO', 2000);
  await panelTap(session, 'SETTINGS', 3000);
  for (let i = 0; i < 8; i++) await panelTap(session, 'DOWN');
  await panelTap(session, 'YES', 3000);
  for (let i = 0; i < 6; i++) await panelTap(session, 'DOWN');
  await panelTap(session, 'YES', 2000);
  await panelTap(session, 'NO', 2000);
}

/** One-for-one port of the full-tier Python remote-control negative scenario. */
export async function expectRemoteControl({ session, dk }: PatchedSession): Promise<void> {
  await delay(session.options.device === 'dn2' ? 45_000 : 12_000);
  await installRemote(session);
  await delay(10_000);
  const cmd = (command: number, body: number[] = []) => dk.request(command, Uint8Array.from(body));
  const state = async () => {
    const reply = await cmd(SERVICE_CMD.REMOTE_STATE);
    return [reply.status, ...reply.body];
  };
  expect(await state()).toEqual([0, 0, 1, 30]);
  expect([
    (await cmd(SERVICE_CMD.REMOTE_KEY, [2, 1])).status,
    (await cmd(SERVICE_CMD.REMOTE_TURN, [1, 1])).status,
    (await cmd(SERVICE_CMD.REMOTE_RELEASE)).status,
  ]).toEqual([-65, -65, -65]);

  await toggleRemote(session);
  expect(await state()).toEqual([0, 1, 1, 30]);
  await withRemoteRelease(dk, async () => {
    expect([
      (await cmd(SERVICE_CMD.REMOTE_KEY, [2, 1])).status,
      (await cmd(SERVICE_CMD.REMOTE_KEY, [2, 0])).status,
      (await cmd(SERVICE_CMD.REMOTE_RELEASE)).status,
    ]).toEqual([0, 0, 0]);
    expect([
      (await cmd(SERVICE_CMD.REMOTE_KEY, [1])).status,
      (await cmd(SERVICE_CMD.REMOTE_KEY, [2, 2])).status,
      (await cmd(SERVICE_CMD.REMOTE_TURN, [0, 1])).status,
    ]).toEqual([-50, -50, -50]);
    expect((await cmd(SERVICE_CMD.REMOTE_KEY, [127, 1])).status).toBe(-1);
    const first = await cmd(SERVICE_CMD.REMOTE_TURN, [1, 100]);
    const immediate = await cmd(SERVICE_CMD.REMOTE_TURN, [1, 100]);
    expect([first.status, [...first.body], immediate.status, [...immediate.body]]).toEqual([0, [30], 0, [0]]);
    await delay(3000);
    const back = await cmd(SERVICE_CMD.REMOTE_TURN, [1, 156]);
    expect(back.status).toBe(0);
    expect(back.body.length).toBe(1);
    expect(256 - back.body[0]!).toBeGreaterThanOrEqual(1);
    expect(256 - back.body[0]!).toBeLessThanOrEqual(30);
  });
  // Release while enabled; disabled RELEASE must not be treated as successful cleanup.
  await toggleRemote(session);
  expect(await state()).toEqual([0, 0, 1, 30]);
  expect((await cmd(SERVICE_CMD.REMOTE_KEY, [2, 1])).status).toBe(-65);
  await session.expectNoFault('remote control');
}
