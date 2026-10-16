// The module gallery, read at build time from the committed modules/**/module.toml and each module's
// gallery/icon.svg (line art on a 48 grid; apps/web/tests/icons.test.mjs checks what an icon may contain).
import { parse } from 'smol-toml';
import type { DeviceId } from './device-profiles';

const manifests = import.meta.glob('../../../../modules/*/*/module.toml', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;
const icons = import.meta.glob('../../../../modules/*/*/gallery/icon.svg', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

export type Category = 'fx' | 'machines' | 'core' | 'test' | 'ui' | 'tools';

export interface ModuleInfo {
  id: string;
  name: string;
  version: string;
  category: Category;
  status: string;
  summary: string;
  requires: string[];
  conflicts: string[];
  /** Resources the module takes (only one module may hold each). */
  claims: string[];
  /** The devices any target names, in a fixed order. */
  devices: DeviceId[];
  /** Inline SVG markup, from the repository. */
  icon: string;
}

export const CATEGORY_LABEL: Record<Category, string> = {
  fx: 'Effect',
  machines: 'Machine',
  core: 'Framework',
  test: 'Test fixture',
  ui: 'Interface',
  tools: 'Tool',
};

/** Categories the gallery hides unless asked: framework modules are added by what needs them. */
export const HIDDEN_CATEGORIES = new Set<Category>(['core', 'test']);

export const DEVICE_SHORT: Record<DeviceId, string> = { dt2: 'DT2', dn2: 'DN2' };

const FRAMEWORK_ICON = `<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="12" y="12" width="24" height="24" rx="4"/><path d="M18 6v6M24 6v6M30 6v6M18 36v6M24 36v6M30 36v6M6 18h6M6 24h6M6 30h6M36 18h6M36 24h6M36 30h6" stroke-width="1.5"/><rect x="20" y="20" width="8" height="8" rx="1.5" style="stroke: var(--icon-accent); fill: var(--icon-accent)"/></svg>`;

type Manifest = {
  module: {
    id: string;
    name: string;
    version: string;
    category: Category;
    status: string;
    summary: string;
    requires?: string[];
    conflicts?: string[];
  };
  targets?: Record<string, string>;
  claim?: { resource: string }[];
};

const order: DeviceId[] = ['dt2', 'dn2'];

export const modules: ModuleInfo[] = Object.entries(manifests)
  .map(([path, text]) => {
    const { module: m, targets = {}, claim = [] } = parse(text) as unknown as Manifest;
    const named = new Set(Object.keys(targets).map((port) => port.split('-')[0]));
    return {
      id: m.id,
      name: m.name,
      version: m.version,
      category: m.category,
      status: m.status,
      summary: m.summary,
      requires: m.requires ?? [],
      conflicts: m.conflicts ?? [],
      claims: claim.map((c) => c.resource),
      devices: order.filter((d) => named.has(d)),
      icon: icons[path.replace(/module\.toml$/, 'gallery/icon.svg')] ?? FRAMEWORK_ICON,
    };
  })
  .sort((a, b) => a.id.localeCompare(b.id));

/** The modules a visitor picks from (no framework or test modules). */
export const publicModules = modules.filter((m) => !HIDDEN_CATEGORIES.has(m.category));
