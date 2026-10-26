import { fileURLToPath } from 'node:url';
import { configDefaults, defineConfig } from 'vitest/config';
// tools/devtools/verify.py reads this file to treat host tests as device-free.
import projects from './vitest.host.projects.json';

const root = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  root,
  test: {
    maxWorkers: 1,
    // Host tests also read desktop source/CSS via fs: related cannot discover
    // those edges. This native trigger conservatively reruns every host project.
    forceRerunTriggers: [...configDefaults.forceRerunTriggers, '**/apps/desktop/ui/src/**'],
    projects: projects.map(({ name, dir, include }) => ({
      test: {
        name,
        root: fileURLToPath(new URL(dir, import.meta.url)),
        include: [include],
        testTimeout: 5000,
      },
    })),
  },
});
