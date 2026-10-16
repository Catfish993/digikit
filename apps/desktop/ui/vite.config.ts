// The desktop app's own frontend (a developer console). It shares the panel and the native runtime with the
// site through `@web`, apps/web/src, so a later move to a shared package is a path change only.
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import solid from 'vite-plugin-solid';

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));

export default defineConfig({
  root: here('.'),
  base: './',
  plugins: [solid()],
  resolve: { alias: { '@web': here('../../web/src') }, dedupe: ['solid-js'] },
  build: { outDir: 'dist', emptyOutDir: true, target: 'safari16' },
  server: { fs: { allow: [here('../../..')] } },
});
