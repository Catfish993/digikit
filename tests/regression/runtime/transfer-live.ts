import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { LOG_KIND } from '@digi/dk-session/gen_proto';
import { repoRoot } from '@digi/test-support';
import { expect } from 'vitest';
import type { PatchedSession } from '../support.ts';
import { installModule, transferCommand } from './transfer-cli.ts';

interface FolderResult {
  folder: string;
  continuous: boolean;
  files: { path: string; size: number; sha256: string }[];
}

/** CLI upload, persistence, folder mirroring and the selected-but-missing module boot. */
export async function expectTransferLive({ session, dk }: PatchedSession): Promise<void> {
  const artifacts = path.join(repoRoot(), 'out/regression/transfer-live', session.options.device);
  fs.mkdirSync(artifacts, { recursive: true });
  const record = (name: string, value: unknown) =>
    fs.writeFileSync(path.join(artifacts, `${name}.json`), JSON.stringify(value, null, 2));
  const mirror = path.join(session.runDir, 'mirror');
  const modulePath = 'modules/pluck.dkm';
  const mirrored = path.join(mirror, 'digikit', modulePath);
  const sha256 = () =>
    fs.existsSync(mirrored) ? createHash('sha256').update(fs.readFileSync(mirrored)).digest('hex') : null;

  // live_boot's wall-clock settle, on the free-running daemon; no instruction stepping.
  await delay(12_000);
  expect((await session.status()).mode, 'transfer daemon runs freely').toBe('free');
  const epoch = (await session.status()).epoch;
  if (typeof epoch !== 'number') throw new Error('daemon status must report a numeric epoch');
  await installModule(session, 'digikit/pluck', artifacts, 300_000);
  expect((await session.status()).epoch, 'CLI RESTART boots another epoch').toBeGreaterThan(epoch);
  const entries = await dk.list();
  record('installed-store', entries);
  expect(
    entries.map((entry) => entry.path),
    'upload survives the soft reset',
  ).toEqual([modulePath]);
  const entry = entries[0]!;

  // The store follows the guest's card writes after its quiet period (about two seconds).
  await expect
    .poll(sha256, { timeout: 30_000, interval: 500, message: `mirror SHA-256: ${mirrored}` })
    .toBe(entry.sha256);
  const folder = await session.client.call<FolderResult>('card.folder');
  record('installed-folder', folder);
  expect(folder.folder, 'card.folder reports the session-owned mirror').toBe(mirror);
  expect(folder.continuous, 'card.folder continuously mirrors the store').toBe(true);
  expect(
    folder.files.map(({ path, sha256 }) => ({ path, sha256 })),
    'mirror metadata matches the card',
  ).toEqual([{ path: modulePath, sha256: entry.sha256 }]);

  const deleted = await transferCommand(session, ['delete', modulePath], path.join(artifacts, 'delete.log'), 300_000);
  expect(
    deleted.some((event) => event.event === 'delete'),
    'CLI reports deletion',
  ).toBe(true);
  const emptyStore = await dk.list();
  record('deleted-store', emptyStore);
  expect(emptyStore, 'CLI delete empties the module store').toEqual([]);
  const synced = await session.client.call<FolderResult>('card.sync');
  record('deleted-folder', synced);
  expect(fs.existsSync(mirrored), 'card.sync removes the deleted module from the folder').toBe(false);
  expect(synced.files, 'card.sync reports an empty mirror').toEqual([]);

  // DELETE keeps the selection. Exactly one more protocol restart preserves the original
  // boot-loop history: another early restart could enter safe mode instead of trying the path.
  await transferCommand(session, ['restart'], path.join(artifacts, 'restart.log'), 300_000);
  const events = await dk.readLog();
  record('missing-module-boot', events);
  expect(
    events.some((event) => event.kind === LOG_KIND.MOD_LOAD && event.args[1] === 0xffffffff),
    'selected missing path logs MOD_LOAD E_NOT_FOUND; see missing-module-boot.json',
  ).toBe(true);
  expect(
    events.some((event) => event.kind === LOG_KIND.UI_FIRST),
    'boot reaches UI_FIRST after the missing module',
  ).toBe(true);
  const modules = await dk.modules();
  record('missing-module-loaded', modules);
  expect(
    modules.map((module) => module.name),
    'boot skips the missing selected module',
  ).toEqual([]);
}
