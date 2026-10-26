import fs from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { repoRoot, type Device } from '@digi/test-support';
import { expect } from 'vitest';
import { startPatchedSession } from '../support.ts';
import { liveArtifacts, liveOptions, liveProtocol, settleLive } from './live.ts';

/** Two fresh daemons with identical probe installation and exactly one protocol restart. */
export async function expectInputAdapter(device: Device): Promise<void> {
  // Preparation is separate from acceptance; no hidden module/toolchain build in the test.
  const directory = process.env.DIGI_RUNTIME_MODULES ?? path.join(repoRoot(), 'out/regression/modules');
  const artifact = path.join(directory, device, 'input-probe.dkm');
  const bytes = fs.readFileSync(artifact);
  const results: string[][] = [];
  for (const source of ['panel', 'probe'] as const) {
    const runtime = await startPatchedSession({ ...liveOptions(device), autoNo: false });
    const { session } = runtime;
    let protocol = liveProtocol(session);
    const artifacts = liveArtifacts(session, 'input-adapter');
    try {
      await settleLive(session, device === 'dn2' ? 45_000 : 12_000);
      await protocol.put('modules/input-probe.dkm', bytes);
      expect(
        await protocol.verifyFile('modules/input-probe.dkm', bytes),
        'prepared probe bytes read back from the card',
      ).toBe(true);
      await protocol.select(['modules/input-probe.dkm']);
      await protocol.restart();
      protocol.cancelPending();
      protocol = liveProtocol(session);
      await protocol.waitForBoot();
      await expect
        .poll(async () => (await protocol.modules()).map((module) => module.name), {
          timeout: 20_000,
          interval: 500,
          message: 'input probe imports core/input',
        })
        .toContain('test/input-probe');
      await delay(10_000);
      const answers: number[] = [];
      const key = async (code: number) => {
        if (source === 'panel') {
          await session.client.inputButton(code, true);
          try {
            await delay(300);
          } finally {
            await session.client.inputButton(code, false);
          }
        } else {
          answers.push((await protocol.request(0x38, Uint8Array.of(code, 1))).status);
          try {
            await delay(300);
          } finally {
            answers.push((await protocol.request(0x38, Uint8Array.of(code, 0))).status);
          }
        }
      };
      const turn = async () => {
        if (source === 'panel') await session.client.inputTurn(1, 4);
        else answers.push((await protocol.request(0x39, Uint8Array.of(1, 4))).status);
      };
      const frames: string[] = [];
      for (const action of [() => key(2), turn, () => key(1)]) {
        await action();
        await delay(10_000);
        frames.push(await session.frameHash());
      }
      const refusals =
        source === 'probe'
          ? [
              (await protocol.request(0x38, Uint8Array.of(200, 1))).status,
              (await protocol.request(0x39, Uint8Array.of(99, 1))).status,
            ]
          : [];
      fs.writeFileSync(path.join(artifacts, `${source}.json`), JSON.stringify({ frames, answers, refusals }, null, 2));
      // As in the original collecting runner, this oracle must not suppress the independent probe workflow.
      if (source === 'panel') expect.soft(new Set(frames).size, 'each panel step changes the screen').toBe(3);
      else {
        expect(new Set(answers), 'every probe command answers OK').toEqual(new Set([0]));
        expect(frames, 'key, turn and second key match the panel OLEDs').toEqual(results[0]);
        expect(refusals, 'invalid key and encoder are refused').toEqual([-1, -1]);
      }
      results.push(frames);
    } finally {
      protocol.cancelPending();
      await runtime.close();
    }
  }
}
