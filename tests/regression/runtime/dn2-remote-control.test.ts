import { test } from 'vitest';
import { withPatchedSession } from '../support.ts';
import { expectRuntimeBoot } from './acceptance.ts';
import { expectRemoteControl } from './remote-control.ts';

const binary = process.env.DIGI_EMUD_DN2;
if (!binary) throw new Error('DIGI_EMUD_DN2 must name a preflighted DN2 daemon binary');
const handle = withPatchedSession({
  device: 'dn2',
  firmware: '1.12',
  binary,
  patchSet: 'device',
});
test('DN2 1.12 remote switch guards commands and panel toggle enables paced input', async () => {
  await expectRuntimeBoot(handle.runtime);
  await expectRemoteControl(handle.runtime);
}, 300_000);
