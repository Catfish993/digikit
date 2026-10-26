import { test } from 'vitest';
import { expectInputAdapter } from './input-adapter.ts';

test('DN2 input adapter matches the panel after identical boot histories', async () => {
  await expectInputAdapter('dn2');
}, 1_000_000);
