import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';
import { repoRoot, type Session } from '@digi/test-support';

const exec = promisify(execFile);

/** Python owns module preparation and install; use the already-running daemon. */
export async function installModule(
  session: Session,
  moduleId: string,
  artifactDirectory: string,
  timeout = 90_000,
): Promise<void> {
  fs.mkdirSync(artifactDirectory, { recursive: true });
  const install = await exec(
    'uv',
    [
      'run',
      'python',
      '-m',
      'tools.device.dktransfer',
      '--emu',
      '--run-dir',
      session.runDir,
      '--json',
      'install',
      moduleId,
      '--restart',
    ],
    { cwd: repoRoot(), timeout, maxBuffer: 4 * 1024 * 1024 },
  );
  const { stdout } = install;
  fs.writeFileSync(path.join(artifactDirectory, 'install.log'), `${stdout}\n${install.stderr}`);
  const events = stdout
    .split('\n')
    .filter((line) => line.startsWith('{'))
    .map((line) => JSON.parse(line));
  const result = events.find((event) => event.event === 'result');
  if (!result?.loaded?.includes(moduleId)) {
    throw new Error(`install --restart did not load ${moduleId}: ${stdout}`);
  }
}
