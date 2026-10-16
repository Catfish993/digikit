// Build step of the patch gallery: writes public/modules/index.json (what the cards show) and
// public/modules/bundle.json (what patchkit.wasm reads: ports + modules as texts) from the COMMITTED
// devices/ports/ and modules/. Never reads out/modules (generated oracles hold firmware words).
// The bundle equals `tools/patchkit/patchkit.py bundle` (the node test compares them).
//
//   node scripts/gen-modules.mjs [--out DIR]
import { readFileSync, readdirSync, writeFileSync, mkdirSync, existsSync, statSync } from 'node:fs';
import { join, dirname, resolve, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
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

/** A small, safe markdown subset as data (no HTML is ever produced): headings, paragraphs, lists, code blocks; `code` and **bold** runs. */
export function markdownBlocks(source) {
  const runs = (text) => {
    const out = [];
    const re = /`([^`]+)`|\*\*([^*]+)\*\*/g;
    let last = 0,
      m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push({ text: text.slice(last, m.index) });
      out.push(m[1] !== undefined ? { text: m[1], code: true } : { text: m[2], bold: true });
      last = re.lastIndex;
    }
    if (last < text.length) out.push({ text: text.slice(last) });
    return out;
  };
  const blocks = [];
  let para = [],
    list = null,
    code = null;
  const flush = () => {
    if (para.length) blocks.push({ t: 'p', runs: runs(para.join(' ')) });
    if (list) blocks.push({ t: 'ul', items: list.map(runs) });
    para = [];
    list = null;
  };
  for (const line of source.split('\n')) {
    if (code) {
      if (line.startsWith('```')) {
        blocks.push({ t: 'code', text: code.join('\n') });
        code = null;
      } else code.push(line);
      continue;
    }
    if (line.startsWith('```')) {
      flush();
      code = [];
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      flush();
      blocks.push({ t: 'h', level: Math.min(h[1].length + 1, 5), runs: runs(h[2]) });
      continue;
    }
    const li = /^\s*[-*]\s+(.*)$/.exec(line);
    if (li) {
      if (!list) {
        if (para.length) {
          blocks.push({ t: 'p', runs: runs(para.join(' ')) });
          para = [];
        }
        list = [];
      }
      list.push(li[1]);
      continue;
    }
    if (line.trim() === '') {
      flush();
      continue;
    }
    if (list) flush();
    para.push(line.trim());
  }
  flush();
  return blocks;
}

/** `{bundle, index}` from the committed tree under `root`. */
export function buildModuleData(root = repoRoot) {
  const ports = readdirSync(join(root, 'devices', 'ports'))
    .filter((f) => f.endsWith('.toml'))
    .sort()
    .map((f) => readFileSync(join(root, 'devices', 'ports', f), 'utf8'));
  const manifests = walk(join(root, 'modules'), 'module.toml').sort(byParts);
  const modules = [],
    cards = [];
  for (const file of manifests) {
    const dir = dirname(file);
    const text = readFileSync(file, 'utf8');
    const d = parse(text);
    const files = {};
    for (const o of d.object ?? []) files[o.path] = readFileSync(join(dir, o.path), 'utf8');
    for (const r of d.registry ?? [])
      if (r.index_table) files[r.index_table] = readFileSync(join(dir, r.index_table), 'utf8');
    modules.push({ manifest: text, files });
    const m = d.module;
    const readme = join(dir, 'gallery', 'README.md');
    const targets = Object.entries(d.targets ?? {}).map(([port, status]) => ({ port, status }));
    const claims = (d.claim ?? []).map((c) => c.resource);
    const devices = [...new Set(targets.map((t) => t.port.split('-')[0]))].sort();
    cards.push({
      id: m.id,
      version: m.version,
      name: m.name,
      category: m.category,
      status: m.status,
      summary: m.summary,
      requires: m.requires ?? [],
      conflicts: m.conflicts ?? [],
      ports: m.ports ?? [],
      targets,
      claims,
      devices,
      gallery: existsSync(readme) ? markdownBlocks(readFileSync(readme, 'utf8')) : [],
      path: relative(root, dir).split(sep).join('/'),
    });
  }
  return { bundle: { ports, modules }, index: { format: 1, modules: cards } };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const i = process.argv.indexOf('--out');
  const out = i > 0 ? resolve(process.argv[i + 1]) : join(here, '..', 'public', 'modules');
  const { bundle, index } = buildModuleData();
  mkdirSync(out, { recursive: true });
  writeFileSync(join(out, 'index.json'), JSON.stringify(index));
  writeFileSync(join(out, 'bundle.json'), JSON.stringify(bundle));
  console.log(
    `modules: ${index.modules.length} modules, ${bundle.ports.length} ports, bundle ${statSync(join(out, 'bundle.json')).size} bytes`,
  );
}
