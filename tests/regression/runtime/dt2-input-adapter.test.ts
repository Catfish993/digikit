import { test } from 'vitest';
import { expectInputAdapter } from './input-adapter.ts';

test('DT2 input adapter matches panel menu navigation and accepts signed TURN after identical boots', async () => {
  await expectInputAdapter('dt2');
}, 1_000_000);
