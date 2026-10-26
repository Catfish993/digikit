import { test } from 'vitest';
import { withPatchedSession } from '../support.ts';
import { expectRuntimeBoot } from './acceptance.ts';
import { expectServiceCommands } from './service-commands.ts';

const binary = process.env.DIGI_EMUD_DN2;
if (!binary) throw new Error('DIGI_EMUD_DN2 must name a preflighted DN2 daemon binary');
const handle = withPatchedSession({
  device: 'dn2',
  firmware: '1.12',
  binary,
});
test('DN2 1.12 service commands echo bodies and preserve registration and dispatch refusals', async () => {
  await expectRuntimeBoot(handle.runtime);
  await expectServiceCommands(handle.runtime);
}, 360_000);
