import { beforeEach, expect, test, vi } from 'vitest';
import type { Session } from '@digi/test-support';

const fake = vi.hoisted(() => ({ exec: vi.fn(), mkdir: vi.fn(), write: vi.fn() }));
vi.mock('node:child_process', () => ({
  execFile: Object.assign(vi.fn(), { [Symbol.for('nodejs.util.promisify.custom')]: fake.exec }),
}));
vi.mock('node:fs', async (original) => ({
  default: {
    ...(await original<{ default: typeof import('node:fs') }>()).default,
    mkdirSync: fake.mkdir,
    writeFileSync: fake.write,
  },
}));
import { installModule } from '../runtime/module-install.ts';

const session = { runDir: '/run/own-daemon' } as Session;

beforeEach(() => {
  vi.resetAllMocks();
});

test('service install uses its own module, artifact directory and 300-second deadline', async () => {
  fake.exec.mockResolvedValueOnce({
    stdout: 'install progress\n{"event":"result","loaded":["test/svc-echo"]}\n',
    stderr: '',
  });
  await installModule(session, 'test/svc-echo', '/artifacts/service-command/dt2', 300_000);
  expect(fake.exec.mock.calls[0]?.[1].slice(-4)).toEqual(['--json', 'install', 'test/svc-echo', '--restart']);
  expect(fake.exec.mock.calls[0]?.[2].timeout).toBe(300_000);
  expect(fake.write.mock.calls[0]?.[0]).toBe('/artifacts/service-command/dt2/install.log');
});

test.each(['', '{"event":"progress"}\n', '{"event":"result","loaded":["digikit/remote"]}\n'])(
  'service install rejects missing or unrelated loaded results: %j',
  async (stdout) => {
    fake.exec.mockResolvedValueOnce({ stdout, stderr: '' });
    await expect(installModule(session, 'test/svc-echo', '/artifacts/service-command/dt2')).rejects.toThrow(
      'install --restart did not load test/svc-echo',
    );
    expect(fake.write).toHaveBeenCalledWith('/artifacts/service-command/dt2/install.log', `${stdout}\n`);
  },
);
