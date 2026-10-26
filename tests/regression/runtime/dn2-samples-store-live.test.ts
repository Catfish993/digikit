import { test } from 'vitest';
import { withPatchedSession } from '../support.ts';
import { liveOptions } from './live.ts';
import { expectSamplesStore } from './samples-store-live.ts';

const handle = withPatchedSession(liveOptions('dn2'));
test('DN2 samples-store-live conversion, upload and persistent card bytes', async () => {
  await expectSamplesStore(handle.runtime);
}, 1_000_000);
