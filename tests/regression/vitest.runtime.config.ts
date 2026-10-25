import { defineConfig } from 'vitest/config';

// One coupled daemon (two cores) at a time; admission belongs to the caller's shared budget.
export default defineConfig({
  test: {
    include: ['runtime/**/*.test.ts'],
    pool: 'forks',
    maxWorkers: 1,
    fileParallelism: false,
    testTimeout: 30_000,
    hookTimeout: 150_000,
  },
});
