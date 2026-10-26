import { test } from 'vitest';
import { expectRuntimeBoot, startPatchedSession } from '../support.ts';
import { DEVICES, FIRMWARE, liveOptions, moduleArtifact } from '../runtime/live.ts';
import { expectModuleLoad, type RuntimeModule } from './acceptance.ts';

// Every cell owns a fresh coupled daemon, card and module selection. No patched restart/reset pooling.
export function moduleLoadTests(module: RuntimeModule): void {
  for (const device of DEVICES) {
    test(`${device} ${FIRMWARE[device]} loads and registers ${module.id} through USB without runtime faults`, async () => {
      const options = liveOptions(device, { mirror: true });
      const artifact = moduleArtifact(device, module.id);
      const runtime = await startPatchedSession(options);
      try {
        await expectRuntimeBoot(runtime);
        await expectModuleLoad(runtime, module, artifact);
      } finally {
        await runtime.close();
      }
    });
  }
}
