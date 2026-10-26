import { test } from 'vitest';
import { withPatchedSession } from '../support.ts';
import { expectRuntimeBoot } from './acceptance.ts';
import { expectTransferLive } from './transfer-live.ts';

const binary = process.env.DIGI_EMUD_DN2;
if (!binary) throw new Error('DIGI_EMUD_DN2 must name a preflighted DN2 daemon binary');
const handle = withPatchedSession({ device: 'dn2', firmware: '1.12', binary, mirror: true });
test('DN2 1.12 CLI transfer survives restart, mirrors the store and skips a deleted selection', async () => {
  await expectRuntimeBoot(handle.runtime);
  await expectTransferLive(handle.runtime);
}, 1_000_000);
