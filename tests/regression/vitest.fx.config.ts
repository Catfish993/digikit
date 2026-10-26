import { defineConfig } from 'vitest/config';

// Stock coupled audio, distinct from patched-daemon runtime and build-free host tests.
export default defineConfig({
  test: {
    include: ['scenarios/dn2-*.test.ts'],
    pool: 'forks',
    maxWorkers: 1,
    fileParallelism: false,
    testTimeout: 30_000,
    hookTimeout: 1_900_000,
  },
});
