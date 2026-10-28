// Build step of the patch gallery: writes public/modules/bundle.json (what patchkit.wasm reads: ports + modules as
// texts) from the COMMITTED devices/ports/, modules/ and tests/fixtures/modules/. Never reads out/modules (generated oracles hold firmware
// words). The bundle equals `tools/patchkit/patchkit.py bundle` (the node test compares them).
//
// The objects of content modules (every module but category "core") are build output, not committed: `patchkit build` makes them
// first (it needs the m68k-elf toolchain; nothing is built when they are current) and they are read from the cache out/cache/dko/<id>/.
// The objects of `core/*` modules are committed in the module. The browser never builds: patchkit.wasm packs from the bundle.
//
//   node scripts/gen-modules.mjs [--out DIR]
import { readFileSync, readdirSync, writeFileSync, mkdirSync, existsSync, statSync } from 'node:fs';
import { join, dirname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { parse } from 'smol-toml';

const here = dirname(fileURLToPath(import.meta.url));
export const repoRoot = resolve(here, '../../..');

const walk = (dir, name, out = []) => {
  if (!existsSync(dir)) return out;
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, e.name);
    if (e.isDirectory()) walk(full, name, out);
    else if (e.name === name) out.push(full);
  }
  return out;
};
// Python's sorted(Path) compares path parts, not the joined string.
const byParts = (a, b) => {
  const x = a.split(sep),
    y = b.split(sep);
  for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
  return x.length - y.length;
};

const GALLERY = 'build/gen/gallery.json';

/** `patchkit build` over the repository's module roots: builds the stale objects of content modules into the cache. */
export function buildObjects(root = repoRoot) {
  const env = { ...process.env, PYTHONPATH: root };
  delete env.PATCHKIT_MODULES; // only the committed roots go into the bundle
  try {
    execFileSync('python3', ['-m', 'tools.patchkit.patchkit', 'build'], {
      cwd: root,
      env,
      encoding: 'utf8',
      stdio: 'pipe',
    });
  } catch (e) {
    throw new Error(
      `patchkit build failed (the gallery needs the module objects; toolchain: uv run python -m tools.patchkit.pk_toolchains m68k):\n${e.stderr || e.message}`,
    );
  }
}

/** The text of an object: in the module for `core/*`, else in the cache. */
const objectText = (root, dir, d, o) =>
  readFileSync(
    d.module.category !== 'core' && (o.sources ?? []).length > 0
      ? join(root, 'out', 'cache', 'dko', d.module.id, o.path)
      : join(dir, o.path),
    'utf8',
  );

/** `{bundle}` from the committed tree under `root` plus the built objects of the content modules. */
export function buildModuleData(root = repoRoot) {
  buildObjects(root);
  const ports = readdirSync(join(root, 'devices', 'ports'))
    .filter((f) => f.endsWith('.toml'))
    .sort()
    .map((f) => readFileSync(join(root, 'devices', 'ports', f), 'utf8'));
  // the module roots in the order of tools/patchkit/modroots.py REPO_ROOTS; the tests use the fixtures
  const manifests = ['modules', join('tests', 'fixtures', 'modules')].flatMap((r) =>
    walk(join(root, r), 'module.toml').sort(byParts),
  );
  const modules = [];
  for (const file of manifests) {
    const dir = dirname(file);
    const text = readFileSync(file, 'utf8');
    const d = parse(text);
    const files = {};
    for (const o of d.object ?? []) files[o.path] = objectText(root, dir, d, o);
    for (const r of d.registry ?? [])
      if (r.index_table) files[r.index_table] = readFileSync(join(dir, r.index_table), 'utf8');
    // `patchkit gen`: machines and parameters, for the page
    if (existsSync(join(dir, GALLERY))) files[GALLERY] = readFileSync(join(dir, GALLERY), 'utf8');
    modules.push({ manifest: text, files });
  }
  // the device files with a [modules] section: the knob models the typed module sections are lowered with
  const devices = readdirSync(join(root, 'devices'))
    .filter((f) => f.endsWith('.toml'))
    .sort()
    .map((f) => readFileSync(join(root, 'devices', f), 'utf8'))
    .filter((t) => 'modules' in parse(t));
  return { bundle: { ports, modules, devices } };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const i = process.argv.indexOf('--out');
  const out = i > 0 ? resolve(process.argv[i + 1]) : join(here, '..', 'public', 'modules');
  const { bundle } = buildModuleData();
  mkdirSync(out, { recursive: true });
  writeFileSync(join(out, 'bundle.json'), JSON.stringify(bundle));
  console.log(
    `modules: ${bundle.modules.length} modules, ${bundle.ports.length} ports, bundle ${statSync(join(out, 'bundle.json')).size} bytes`,
  );
}
