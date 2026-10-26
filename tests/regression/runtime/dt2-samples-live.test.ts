import { test } from 'vitest';
import { withPatchedSession } from '../support.ts';
import { liveOptions } from './live.ts';
import { expectSamplesLive } from './samples-live.ts';

const handle = withPatchedSession(liveOptions('dt2'));
test('DT2 samples-live conversion, upload and persistent card bytes', async () => {
  await expectSamplesLive(handle.runtime);
}, 1_000_000);
