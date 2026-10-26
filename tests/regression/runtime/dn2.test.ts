import { test } from 'vitest';
import { withPatchedSession } from '../support.ts';
import { expectRuntimeBoot, expectRemoteKeys } from './acceptance.ts';

const binary = process.env.DIGI_EMUD_DN2;
if (!binary) throw new Error('DIGI_EMUD_DN2 must name a preflighted DN2 daemon binary');
const handle = withPatchedSession({
  device: 'dn2',
  firmware: '1.12',
  binary,
});
test('DN2 1.12 remote keys open ABOUT and TURN accepts signed deltas', async () => {
  await expectRuntimeBoot(handle.runtime);
  await expectRemoteKeys(handle.runtime);
}, 300_000);
