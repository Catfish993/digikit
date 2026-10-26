import { test } from 'vitest';
import { withPatchedSession } from '../support.ts';
import { expectRuntimeBoot } from './acceptance.ts';
import { expectTransferLive } from './transfer-live.ts';

const card = process.env.DIGI_RUNTIME_CARD;
if (!card) throw new Error('DIGI_RUNTIME_CARD must name the generated DT2 sample directory');
const binary = process.env.DIGI_EMUD_DT2;
if (!binary) throw new Error('DIGI_EMUD_DT2 must name a preflighted DT2 daemon binary');
const handle = withPatchedSession({ device: 'dt2', firmware: '1.17', binary, card, mirror: true });
test('DT2 1.17 CLI transfer survives restart, mirrors the store and skips a deleted selection', async () => {
  await expectRuntimeBoot(handle.runtime);
  await expectTransferLive(handle.runtime);
}, 1_000_000);
