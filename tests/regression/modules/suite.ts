import fs from 'node:fs';
import { test } from 'vitest';
import { startPatchedSession } from '../support.ts';
import { expectRuntimeBoot } from '../runtime/acceptance.ts';
import { expectModuleLoad, moduleArtifact, type RuntimeModule } from './acceptance.ts';

// Every cell owns a fresh coupled daemon, card and module selection. No patched restart/reset pooling.
export function moduleLoadTests(module: RuntimeModule): void {
  for (const [device, firmware] of [
    ['dt2', '1.17'],
    ['dn2', '1.12'],
  ] as const) {
    test(`${device} ${firmware} loads and registers ${module.id} through USB without runtime faults`, async () => {
      const binary = process.env[`DIGI_EMUD_${device.toUpperCase()}`];
      if (!binary) throw new Error(`DIGI_EMUD_${device.toUpperCase()} must name a preflighted daemon binary`);
      const card = device === 'dt2' ? process.env.DIGI_RUNTIME_CARD : undefined;
      if (device === 'dt2' && !card) throw new Error('DIGI_RUNTIME_CARD must name the generated DT2 sample directory');
      const artifact = moduleArtifact(device, module);
      if (!fs.existsSync(artifact))
        throw new Error(`prepare ${module.id} for ${device} with patchkit pack: missing ${artifact}`);
      const runtime = await startPatchedSession({ device, firmware, binary, card, mirror: true });
      try {
        await expectRuntimeBoot(runtime);
        await expectModuleLoad(runtime, module, artifact);
      } finally {
        await runtime.close();
      }
    }, 300_000);
  }
}
