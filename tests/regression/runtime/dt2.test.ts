import { test } from 'vitest';
import { withPatchedSession } from '../support.ts';
import { expectRuntimeBoot, expectRemoteKeys } from './acceptance.ts';

const card = process.env.DIGI_RUNTIME_CARD;
if (!card) throw new Error('DIGI_RUNTIME_CARD must name the generated DT2 sample directory');
const binary = process.env.DIGI_EMUD_DT2;
if (!binary) throw new Error('DIGI_EMUD_DT2 must name a preflighted DT2 daemon binary');
const handle = withPatchedSession({
  device: 'dt2',
  firmware: '1.17',
  binary,
  card,
});
test('DT2 1.17 remote keys open ABOUT and TURN accepts signed deltas', async () => {
  await expectRuntimeBoot(handle.runtime);
  await expectRemoteKeys(handle.runtime);
}, 300_000);
