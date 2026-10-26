import fs from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { button, repoRoot, type Device } from '@digi/test-support';
import { expect } from 'vitest';
import { startPatchedSession } from '../support.ts';
import { prepareRuntimePlan } from './runtime-plan.ts';
import { openDigikitMenu, openRuntimeAbout, expectRuntimeAbout, meaningfulAboutRows } from './runtime-menu.ts';
import { liveArtifacts, liveOptions, liveProtocol, settleLive } from './live.ts';

/** Two fresh daemons with identical probe installation and exactly one protocol restart. */
export async function expectInputAdapter(device: Device): Promise<void> {
  // Probe DKM preparation is separate; the exact-image symbol plan uses the existing patch CLI below.
  const directory = process.env.DIGI_RUNTIME_MODULES ?? path.join(repoRoot(), 'out/regression/modules');
  const artifact = path.join(directory, device, 'input-probe.dkm');
  const bytes = fs.readFileSync(artifact);
  const results: { frames: Record<string, string>; rows: string[] }[] = [];
  for (const source of ['panel', 'probe'] as const) {
    const runtime = await startPatchedSession({ ...liveOptions(device), autoNo: false });
    const { session } = runtime;
    let protocol = liveProtocol(session);
    const artifacts = liveArtifacts(session, 'input-adapter');
    const frames: Record<string, string> = {};
    const answers: number[] = [];
    const statuses: unknown[] = [];
    let rows: string[] = [];
    let refusals: number[] = [];
    try {
      await settleLive(session, device === 'dn2' ? 45_000 : 12_000);
      const plan = await prepareRuntimePlan(session, artifacts, ['DK_ABOUT', 'DK_ABOUT_N']);
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
      const tap = async (name: string, settleMs = 1200) => {
        const code = button(name);
        const send = async (down: boolean) => {
          if (source === 'panel') await session.client.inputButton(code, down);
          else {
            const status = (await protocol.request(0x38, Uint8Array.of(code, down ? 1 : 0))).status;
            answers.push(status);
            expect(status, `probe ${name} ${down ? 'down' : 'up'}`).toBe(0);
          }
        };
        try {
          await send(true);
          await delay(300);
        } finally {
          await send(false);
        }
        await delay(settleMs);
      };
      // Delivery/decoder acceptance, not a claim that a vendor parameter changes in this UI state.
      for (const delta of [4, -4]) {
        if (source === 'panel') {
          const before = await session.status();
          statuses.push(before);
          expect(typeof before.cf.input_irqs).toBe('number');
          await session.client.inputTurn(1, delta);
          await expect
            .poll(
              async () => {
                const after = await session.status();
                statuses.push(after);
                return after.cf.input_pending === 0 && Number(after.cf.input_irqs) > Number(before.cf.input_irqs);
              },
              { timeout: 8000, interval: 400, message: `panel TURN ${delta} delivered through UART` },
            )
            .toBe(true);
        } else {
          const status = (await protocol.request(0x39, Uint8Array.of(1, delta & 255))).status;
          answers.push(status);
          expect(status, `probe TURN ${delta} accepted by input_turn`).toBe(0);
        }
      }
      await openDigikitMenu(tap, async (name) => {
        const frame = await session.client.framebufferGet({ hash: true, data: true });
        expect(frame.sha256).toMatch(/^[a-f0-9]{64}$/);
        frames[name] = frame.sha256!;
        fs.writeFileSync(path.join(artifacts, `${source}-${name}.json`), JSON.stringify(frame, null, 2));
      });
      await openRuntimeAbout(tap);
      rows = await expectRuntimeAbout(session, protocol, plan);
      if (source === 'probe') {
        refusals = [
          (await protocol.request(0x38, Uint8Array.of(200, 1))).status,
          (await protocol.request(0x39, Uint8Array.of(99, 1))).status,
        ];
        expect(refusals, 'invalid key and encoder are refused').toEqual([-1, -1]);
        expect(frames, 'known SETTINGS/DIGIKIT menu frames match the panel').toEqual(results[0]!.frames);
        expect(meaningfulAboutRows(rows), 'ABOUT identity and boot rows match the panel').toEqual(results[0]!.rows);
      }
      await session.expectNoFault(`input adapter ${source}`);
      results.push({ frames, rows: meaningfulAboutRows(rows) });
    } finally {
      try {
        fs.writeFileSync(
          path.join(artifacts, `${source}.json`),
          JSON.stringify({ frames, rows, answers, refusals, statuses }, null, 2),
        );
      } finally {
        protocol.cancelPending();
        await runtime.close();
      }
    }
  }
}
