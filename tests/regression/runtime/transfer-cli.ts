import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';
import { repoRoot, type Session } from '@digi/test-support';

const exec = promisify(execFile);

export interface TransferEvent {
  event: string;
  loaded?: string[];
}

/** Exercise the transfer CLI against this daemon, preserving stdout/stderr even on failure. */
export async function transferCommand(
  session: Session,
  args: string[],
  artifactFile: string,
  timeout = 90_000,
): Promise<TransferEvent[]> {
  fs.mkdirSync(path.dirname(artifactFile), { recursive: true });
  const command = [
    'run',
    'python',
    '-m',
    'tools.device.dktransfer',
    '--emu',
    '--run-dir',
    session.runDir,
    '--json',
    ...args,
  ];
  let stdout: string;
  let stderr: string;
  try {
    const result = await exec('uv', command, { cwd: repoRoot(), timeout, maxBuffer: 4 * 1024 * 1024 });
    stdout = result.stdout;
    stderr = result.stderr;
  } catch (error) {
    const output = error as Error & { stdout?: string; stderr?: string };
    fs.writeFileSync(artifactFile, `${output.stdout ?? ''}\n${output.stderr ?? ''}\n${output.message}`);
    throw new Error(`dktransfer ${args.join(' ')} failed: ${output.message}; see ${artifactFile}`, { cause: error });
  }
  fs.writeFileSync(artifactFile, `${stdout}\n${stderr}`);
  return stdout
    .split('\n')
    .filter((line) => line.startsWith('{'))
    .map((line) => JSON.parse(line) as TransferEvent);
}

/** Python owns module preparation and install; use the already-running daemon. */
export async function installModule(
  session: Session,
  moduleId: string,
  artifactDirectory: string,
  timeout = 90_000,
): Promise<void> {
  const events = await transferCommand(
    session,
    ['install', moduleId, '--restart'],
    path.join(artifactDirectory, 'install.log'),
    timeout,
  );
  const result = events.find((event) => event.event === 'result');
  if (!result?.loaded?.includes(moduleId)) {
    throw new Error(`install --restart did not load ${moduleId}: ${JSON.stringify(events)}`);
  }
}
