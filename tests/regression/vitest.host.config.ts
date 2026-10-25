import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: { include: ['host/**/*.test.ts'], maxWorkers: 1, testTimeout: 5000 },
});
