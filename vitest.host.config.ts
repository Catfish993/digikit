import { fileURLToPath } from 'node:url';
import { configDefaults, defineConfig } from 'vitest/config';

const root = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  root,
  test: {
    maxWorkers: 1,
    // Host tests also read desktop source/CSS via fs: related cannot discover
    // those edges. This native trigger conservatively reruns every host project.
    forceRerunTriggers: [...configDefaults.forceRerunTriggers, '**/apps/desktop/ui/src/**'],
    projects: [
      ['control-client', 'packages/control-client', 'tests/*.test.ts'],
      ['test-support', 'packages/test-support', 'tests/*.test.ts'],
      ['dk-session', 'packages/dk-session', 'tests/*.test.ts'],
      ['desktop-host', 'apps/desktop', 'ui/tests/*.test.ts'],
      ['regression-host', 'tests/regression', 'host/**/*.test.ts'],
    ].map(([name, directory, include]) => ({
      test: {
        name,
        root: fileURLToPath(new URL(directory, import.meta.url)),
        include: [include],
        testTimeout: 5000,
      },
    })),
  },
});
