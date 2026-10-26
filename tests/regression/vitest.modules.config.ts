import { defineConfig } from 'vitest/config';

// Fresh coupled module-load cells; admission belongs to the caller's shared budget.
export default defineConfig({
  test: {
    include: ['modules/**/*.test.ts'],
    pool: 'forks',
    maxWorkers: 1,
    fileParallelism: false,
    testTimeout: 300_000,
    hookTimeout: 150_000,
  },
});
