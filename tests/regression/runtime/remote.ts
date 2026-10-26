import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';
import type { DkSession } from '@digi/dk-session';
import { button, repoRoot, type Session } from '@digi/test-support';
import type { PatchedSession } from '../support.ts';
import { prepareRuntimePlan, type RuntimePlan } from './runtime-plan.ts';
import { installModule } from './transfer-cli.ts';

const exec = promisify(execFile);
const root = repoRoot();
/** Prepare an exact loaded-image ABOUT plan, then install only the remote module. */
export async function prepareRemote({ session }: PatchedSession): Promise<RuntimePlan> {
  const plan = await prepareRuntimePlan(session, remoteArtifacts(session), ['DK_ABOUT', 'DK_ABOUT_N']);
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
  let actionFailed = false;
  let actionError: unknown;
  try {
    await action();
  } catch (error) {
    actionFailed = true;
    actionError = error;
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      dk.remoteRelease(),
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error('remote release timed out')), 5000);
      }),
    ]);
  } catch (error) {
    if (actionFailed) throw new AggregateError([actionError, error], 'remote action and release failed');
    throw error;
  } finally {
    clearTimeout(timer);
  }
  if (actionFailed) throw actionError;
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
