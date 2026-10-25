import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['dt2/**/*.test.ts'],
    pool: 'forks',
    testTimeout: 120_000,
    hookTimeout: 120_000,
    maxWorkers: Number(process.env.REGRESSION_JOBS ?? 4),
    reporters:
      process.env.REGRESSION_JUNIT === '1'
        ? ['default', ['junit', { outputFile: '../../out/regression/junit.xml' }]]
        : ['default'],
  },
});
