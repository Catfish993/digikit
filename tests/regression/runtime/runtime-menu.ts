import { fmtVersion, type DkSession } from '@digi/dk-session';
import type { Session } from '@digi/test-support';
import { expect } from 'vitest';
import { planSymbol, type RuntimePlan } from './runtime-plan.ts';

type Tap = (name: string, settleMs?: number) => Promise<void>;

/** Known menu states, independent of the currently displayed parameter or encoder target. */
export async function openDigikitMenu(tap: Tap, checkpoint?: (name: string) => Promise<void>): Promise<void> {
  await tap('NO', 2000);
  await tap('SETTINGS', 3000);
  for (let i = 0; i < 8; i++) await tap('DOWN');
  await checkpoint?.('settings-ninth-row');
  await tap('YES', 3000);
  await checkpoint?.('digikit-main');
}

export async function openRuntimeAbout(tap: Tap): Promise<void> {
  await tap('DOWN');
  await tap('DOWN');
  await tap('YES', 3000);
}

export async function readAboutRows(session: Session, plan: RuntimePlan): Promise<string[]> {
  const base = planSymbol(plan, 'DK_ABOUT');
  const count = await session.client.memPeek(planSymbol(plan, 'DK_ABOUT_N'));
  if (count === null || !Number.isInteger(count) || count < 1 || count > 24)
    throw new Error(`invalid ABOUT row count: ${count}`);
  const rows: string[] = [];
  for (let i = 0; i < count; i++) {
    const row = Buffer.alloc(24);
    for (let offset = 0; offset < 24; offset += 4) {
      const word = await session.client.memPeek(base + i * 24 + offset);
      if (word === null) throw new Error('ABOUT row is unmapped');
      row.writeUInt32BE(word >>> 0, offset);
    }
    rows.push(row.toString('latin1').split('\0')[0]!);
  }
  return rows;
}

/** Poll semantic menu content, bounded both in time and in the number of memory reads per poll. */
export async function expectRuntimeAbout(session: Session, dk: DkSession, plan: RuntimePlan): Promise<string[]> {
  const runtimeRow = `RUNTIME ${fmtVersion((await dk.info()).runtime)}`;
  let rows: string[] = [];
  await expect
    .poll(async () => (rows = await readAboutRows(session, plan)), {
      timeout: 10_000,
      interval: 500,
      message: 'ABOUT describes the live runtime',
    })
    .toContain(runtimeRow);
  return rows;
}

/** Stable identity/boot semantics only; other ABOUT rows may contain changing diagnostics. */
export function meaningfulAboutRows(rows: string[]): string[] {
  return rows.filter((row) => /^(DIGIKIT |RUNTIME |BOOT: )/.test(row));
}
