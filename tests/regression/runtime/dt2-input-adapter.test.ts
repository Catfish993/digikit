import { test } from 'vitest';
import { expectInputAdapter } from './input-adapter.ts';

test('DT2 input adapter matches the panel after identical boot histories', async () => {
  await expectInputAdapter('dt2');
}, 1_000_000);
