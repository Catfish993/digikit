import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';
import type { DkSession } from '@digi/dk-session';
import { button, repoRoot, resolveInputs, type Session } from '@digi/test-support';
import type { PatchedSession } from '../support.ts';
import { installModule } from './module-install.ts';

const exec = promisify(execFile);
const root = repoRoot();
export interface RemotePlan {
  base: { device: string; version: string; syx_sha256: string };
  modules: string[];
  symbols: Record<string, number>;
  output: { syx_sha256: string } | null;
}

/** Same exact-one-address semantics as pk_harness.plan_symbol, including Rust statics. */
export function planSymbol(plan: RemotePlan, name: string): number {
  const addresses = new Set(
    Object.entries(plan.symbols)
      .filter(([key]) => key.endsWith(`:${name}`) || new RegExp(`\\d${name}$`).test(key))
      .map(([, value]) => value),
  );
  if (addresses.size !== 1) throw new Error(`symbol ${name}: ${addresses.size} addresses in the plan`);
  return [...addresses][0]!;
}

export function validateRemotePlan(plan: RemotePlan, session: Pick<Session, 'options' | 'loadResult'>): void {
  const identity = session.loadResult.runtime;
  // Cache hits report plain IDs in root order; misses report tags in dependency order.
  const moduleIds = (modules: string[]) => modules.map((module) => module.split('@', 1)[0]).sort();
  if (
    !identity?.patched ||
    identity.set !== 'emulator' ||
    plan.base.device !== session.options.device ||
    plan.base.version !== session.options.firmware ||
    plan.base.syx_sha256 !== identity.base_sha256 ||
    JSON.stringify(moduleIds(plan.modules)) !== JSON.stringify(moduleIds(identity.modules))
  ) {
    throw new Error('remote ABOUT plan does not match the loaded runtime identity and module membership');
  }
  // Exact loaded-image identity proves version, ordering and symbol layout despite report differences.
  const loadedSha256 = session.loadResult.syx_sha256;
  if (
    typeof loadedSha256 !== 'string' ||
    !/^[a-f0-9]{64}$/.test(loadedSha256) ||
    plan.output?.syx_sha256 !== loadedSha256
  ) {
    throw new Error('remote ABOUT plan output hash does not match the loaded runtime image');
  }
}

/** Existing Python tools own patch/module preparation; this helper never builds a daemon or core. */
export async function prepareRemote({ session }: PatchedSession): Promise<RemotePlan> {
  const { device, firmware } = session.options;
  const syx = path.join(session.runDir, `remote-${device}.syx`);
  const planFile = syx.replace(/\.syx$/, '.plan.json');
  const patch = await exec(
    'uv',
    [
      'run',
      'python',
      '-m',
      'tools.patchkit.patchkit',
      'patch',
      '--firmware',
      resolveInputs(device, firmware).syx,
      '--set',
      'emulator',
      '-o',
      syx,
      '--plan',
      planFile,
    ],
    { cwd: root, timeout: 30_000, maxBuffer: 4 * 1024 * 1024 },
  );
  const artifacts = remoteArtifacts(session);
  fs.writeFileSync(path.join(artifacts, 'patch.log'), `${patch.stdout}\n${patch.stderr}`);
  const plan: RemotePlan = JSON.parse(fs.readFileSync(planFile, 'utf8'));
  validateRemotePlan(plan, session);
  // Fail before installation/navigation if either diagnostic symbol is absent or ambiguous.
  planSymbol(plan, 'DK_ABOUT');
  planSymbol(plan, 'DK_ABOUT_N');
  await installRemote(session);
  return plan;
}

/** Install only: negative device-set cases need no offline ABOUT symbol plan. */
export async function installRemote(session: Session): Promise<void> {
  await installModule(session, 'digikit/remote', remoteArtifacts(session));
}

export async function remoteTap(dk: DkSession, name: string, settleMs = 1200): Promise<void> {
  const code = button(name);
  try {
    await dk.remoteKey(code, true);
    await delay(300);
  } finally {
    await dk.remoteKey(code, false);
  }
  await delay(settleMs);
}

/** Always release held remote inputs, including on navigation/assertion failure. */
export async function withRemoteRelease(dk: DkSession, action: () => Promise<void>): Promise<void> {
  try {
    await action();
  } finally {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        dk.remoteRelease(),
        new Promise<never>((_resolve, reject) => {
          timer = setTimeout(() => reject(new Error('remote release timed out')), 5000);
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }
}

function remoteArtifacts(session: Session): string {
  const scenario = session.options.runtime?.patchSet === 'device' ? 'remote-control' : 'remote-keys';
  const dir = path.join(root, 'out/regression', scenario, session.options.device);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

/** Existing OLED renderer; persistent diagnostics survive Session's temporary-directory cleanup. */
export async function captureRemoteOled(session: Session, name: string): Promise<void> {
  const dir = remoteArtifacts(session);
  await exec(
    'uv',
    [
      'run',
      'python',
      '-m',
      'tools.control.digi_client',
      '--run-dir',
      session.runDir,
      'frame',
      path.join(dir, `${name}.png`),
    ],
    { cwd: root, timeout: 10_000 },
  );
}

export async function runtimeVersion(): Promise<string> {
  const { stdout } = await exec(
    'uv',
    ['run', 'python', '-c', 'from tools.patchkit.pk_harness import runtime_text; print(runtime_text())'],
    { cwd: root, timeout: 10_000 },
  );
  const version = stdout.trim();
  if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error('invalid runtime_text output');
  return version;
}
