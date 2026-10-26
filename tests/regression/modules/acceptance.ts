import fs from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { hexSha256, type DkLogEvent } from '@digi/dk-session';
import { LOG_KIND, SERVICE_CMD } from '@digi/dk-session/gen_proto';
import { expect } from 'vitest';
import type { PatchedSession } from '../support.ts';
import { installModule, liveArtifacts, record, settleUi } from '../runtime/live.ts';

/** Explicit production upload candidates; framework/static modules are firmware prerequisites. */
export const runtimeModules = [
  { id: 'digikit/pluck', contract: 1, dsp: true },
  { id: 'digikit/freqshift', contract: 2, dsp: true },
  { id: 'digikit/remote', contract: 3, dsp: false },
] as const;
export type RuntimeModule = (typeof runtimeModules)[number];

/** Real USB upload, persisted bytes, selected reboot, registration and fault acceptance.
 * DSP allocation/registration does not establish rendered audio correctness. */
export async function expectModuleLoad(
  runtime: PatchedSession,
  module: RuntimeModule,
  artifact: string,
): Promise<void> {
  const { session, dk } = runtime;
  const bytes = fs.readFileSync(artifact);
  const storePath = `modules/${module.id.split('/')[1]}.dkm`;
  const hash = await hexSha256(bytes);
  const artifacts = path.join(liveArtifacts(session, 'module-load'), module.id.split('/')[1]!);
  fs.mkdirSync(artifacts, { recursive: true });

  // Match the existing coupled runtime fixtures' pre-install settle; do not step a free-running guest.
  await settleUi(session);
  expect(await dk.list(), 'fresh daemon has an empty store').toEqual([]);
  expect(await dk.modules(), 'fresh runtime has no uploaded modules').toEqual([]);
  if (module.id === 'digikit/remote') {
    expect((await dk.request(SERVICE_CMD.REMOTE_STATE)).status, 'remote service is absent before upload').toBe(-60);
  }
  const epoch = (await session.status()).epoch;
  if (typeof epoch !== 'number') throw new Error('daemon status must report a numeric epoch');
  await installModule(dk, module.id, artifact);
  expect(
    (await dk.modules()).map((entry) => entry.name),
    'only the uploaded module is registered',
  ).toEqual([module.id]);
  expect((await session.status()).epoch, 'protocol restart boots another epoch').toBeGreaterThan(epoch);
  const entries = await dk.list();
  record(artifacts, 'store', entries);
  expect(entries).toEqual([{ path: storePath, size: bytes.length, sha256: hash }]);
  expect(await dk.verifyFile(storePath, bytes), 'module bytes survive reboot').toBe(true);

  try {
    if (module.dsp) {
      // MOD_LOAD publishes the CF module before its DSP part completes over UI-loop passes.
      // This cell selects one path, so boot's selection index is zero (not MODULES' registry index).
      let completionLog: DkLogEvent[] = [];
      try {
        await expect
          .poll(
            async () => {
              completionLog = await dk.readLog();
              return completionLog.some((event) => event.kind === LOG_KIND.MOD_DSP && event.args[0] === 0);
            },
            { timeout: 30_000, interval: 500, message: 'selected module DSP completion' },
          )
          .toBe(true);
      } finally {
        record(artifacts, 'dsp-completion-log', completionLog);
      }
      expect(
        completionLog
          .filter((event) => event.kind === LOG_KIND.MOD_DSP && event.args[0] === 0)
          .map((event) => event.args[1]),
        'DSP loader completes the selected module successfully',
      ).toEqual([0]);
    }
    const loaded = (await dk.modules())[0]!;
    expect(loaded.version, 'loader reports the prepared module version').toBe(bytes.readUInt32BE(0x34));
    const contracts = Array.from({ length: 4 }, (_, index) => [bytes[0x38 + 2 * index], bytes[0x39 + 2 * index]]);
    expect(loaded.contracts, 'loader reports the prepared SDK contracts').toEqual(contracts);
    expect(loaded.contracts[module.contract]![0], 'intended module contract is active').toBeGreaterThan(0);
    if (module.dsp) {
      expect(loaded.dsp_len, 'module DSP image was allocated').toBeGreaterThan(0);
      if (module.id === 'digikit/pluck') {
        expect(loaded.rows, 'machine has a published DSP voice row').toBeGreaterThan(0);
        expect(loaded.types, 'machine has a published firmware type').toHaveLength(1);
      }
    } else {
      expect(loaded.dsp_len).toBe(0);
      expect(await dk.remoteState(), 'loaded remote service dispatches STATE').toEqual({
        enabled: true,
        format: 1,
        turnLimit: 30,
      });
      await dk.remoteRelease();
    }
    // Allow the loaded registration to run on the coupled DSP before checking its guard counters.
    await delay(2000);
    const modules = await dk.modules();
    record(artifacts, 'modules', modules);
    expect(modules.map(({ name, faults, guard_off }) => ({ name, faults, guard_off }))).toEqual([
      { name: module.id, faults: 0, guard_off: false },
    ]);
    await expect
      .poll(async () => (await dk.readLog()).some((event) => event.kind === LOG_KIND.UI_FIRST), {
        timeout: 30_000,
        interval: 500,
      })
      .toBe(true);
    const log = await dk.readLog();
    record(artifacts, 'boot-log', log);
    expect(
      log.some((event) => event.kind === LOG_KIND.UI_FIRST),
      'selected module boot reaches the firmware UI',
    ).toBe(true);
    const failures = new Set<number>([
      LOG_KIND.SAFE,
      LOG_KIND.DSP_STOPPED,
      LOG_KIND.CRASH_FOUND,
      LOG_KIND.DSP_ABSENT,
      LOG_KIND.MACHINE_FAULT,
      LOG_KIND.MACHINE_DISABLED,
      LOG_KIND.TABLE_BAD,
    ]);
    expect(
      log.filter((event) => failures.has(event.kind)),
      'no runtime crash, missing DSP or disabled registration',
    ).toEqual([]);
    await session.expectNoFault(`loaded ${module.id}`);
  } finally {
    record(artifacts, 'final-modules', await dk.modules());
    record(artifacts, 'final-status', await session.status());
  }
}
