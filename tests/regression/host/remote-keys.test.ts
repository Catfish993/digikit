import fs from 'node:fs';
import { beforeEach, expect, test, vi } from 'vitest';
import type { DkSession } from '@digi/dk-session';
import type { Session } from '@digi/test-support';
import type { PatchedSession } from '../support.ts';

const fake = vi.hoisted(() => ({ exec: vi.fn(), read: vi.fn(), mkdir: vi.fn(), write: vi.fn() }));
vi.mock('node:timers/promises', () => ({
  setTimeout: (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)),
}));
vi.mock('node:child_process', () => ({
  execFile: Object.assign(vi.fn(), { [Symbol.for('nodejs.util.promisify.custom')]: fake.exec }),
}));
vi.mock('node:fs', async (original) => ({
  default: {
    ...(await original<{ default: typeof import('node:fs') }>()).default,
    readFileSync: fake.read,
    mkdirSync: fake.mkdir,
    writeFileSync: fake.write,
  },
}));
vi.mock('../../../packages/test-support/src/inputs.ts', async (original) => ({
  ...(await original<object>()),
  resolveInputs: () => ({ syx: '/stock.syx' }),
}));
import {
  installRemote,
  planSymbol,
  prepareRemote,
  remoteTap,
  validateRemotePlan,
  withRemoteRelease,
  type RemotePlan,
} from '../runtime/remote.ts';

const patchedSha256 = 'c'.repeat(64);
const plan: RemotePlan = {
  output: { syx_sha256: patchedSha256 },
  base: { device: 'dt2', version: '1.17', syx_sha256: 'a'.repeat(64) },
  modules: ['core/runtime@0.1.0', 'core/digikit-menu@0.1.0'],
  symbols: { 'core/digikit-menu@0.1.0:DK_ABOUT': 4096, 'core/digikit-menu@0.1.0:DK_ABOUT_N': 8192 },
};
const session = {
  options: { device: 'dt2', firmware: '1.17' },
  runDir: '/run/own-daemon',
  loadResult: {
    syx_sha256: patchedSha256,
    runtime: {
      patched: true,
      set: 'emulator',
      base_sha256: plan.base.syx_sha256,
      cache_file: '/cache/runtime.syx',
      modules: plan.modules,
    },
  },
} as unknown as Session;

beforeEach(() => {
  vi.resetAllMocks();
  fake.read.mockReturnValue(JSON.stringify(plan));
  fake.exec.mockResolvedValue({ stdout: '{"event":"result","loaded":["digikit/remote"]}\n' });
});

test('plan identity requires the device, version, base hash and expanded module membership', () => {
  expect(() => validateRemotePlan(plan, session)).not.toThrow();
  for (const base of [
    { ...plan.base, device: 'dn2' },
    { ...plan.base, version: '1.16' },
    { ...plan.base, syx_sha256: 'b'.repeat(64) },
  ])
    expect(() => validateRemotePlan({ ...plan, base }, session)).toThrow('does not match');
  for (const modules of [['emulator'], [...plan.modules, 'extra']]) {
    expect(() => validateRemotePlan({ ...plan, modules }, session)).toThrow('does not match');
  }
});

test('symbol lookup accepts aliases of one address, including mangled Rust names, but rejects ambiguity or absence', () => {
  const symbols = { 'a:DK_ABOUT': 1, 'b:DK_ABOUT': 1, _8DK_ABOUT: 1, 'a:DK_ABOUT_N': 2 };
  expect(planSymbol({ ...plan, symbols }, 'DK_ABOUT')).toBe(1);
  expect(planSymbol({ ...plan, symbols: { _8DK_ABOUT: 1 } }, 'DK_ABOUT')).toBe(1);
  expect(() => planSymbol({ ...plan, symbols: { ...symbols, 'c:DK_ABOUT': 3 } }, 'DK_ABOUT')).toThrow('2 addresses');
  expect(() => planSymbol({ ...plan, symbols: {} }, 'DK_ABOUT')).toThrow('0 addresses');
});

test('setup reuses Python patch/install commands, its own daemon and bounded tool deadlines', async () => {
  expect(await prepareRemote({ session } as PatchedSession)).toEqual(plan);
  expect(fake.exec.mock.calls[0]?.[1]).toEqual([
    'run',
    'python',
    '-m',
    'tools.patchkit.patchkit',
    'patch',
    '--firmware',
    '/stock.syx',
    '--set',
    'emulator',
    '-o',
    '/run/own-daemon/remote-dt2.syx',
    '--plan',
    '/run/own-daemon/remote-dt2.plan.json',
  ]);
  expect(fake.exec.mock.calls[1]?.[1]).toContain('/run/own-daemon');
  expect(fake.exec.mock.calls[1]?.[1].slice(-4)).toEqual(['--json', 'install', 'digikit/remote', '--restart']);
  expect(fake.exec.mock.calls.map((call) => call[2].timeout)).toEqual([30_000, 90_000]);
  expect(fs.readFileSync).toHaveBeenCalledWith('/run/own-daemon/remote-dt2.plan.json', 'utf8');
});

for (const stage of ['patch', 'install', 'load-result', 'plan'] as const) {
  test(`setup ${stage} failure cannot continue to navigation`, async () => {
    if (stage === 'patch') fake.exec.mockRejectedValueOnce(new Error('patch failed'));
    if (stage === 'install')
      fake.exec.mockResolvedValueOnce({ stdout: '' }).mockRejectedValueOnce(new Error('install failed'));
    if (stage === 'load-result') fake.exec.mockResolvedValue({ stdout: '{"event":"result","loaded":[]}\n' });
    if (stage === 'plan') fake.read.mockReturnValue(JSON.stringify({ ...plan, modules: ['wrong'] }));
    await expect(prepareRemote({ session } as PatchedSession)).rejects.toThrow();
    expect(fake.exec).toHaveBeenCalledTimes(stage === 'patch' || stage === 'plan' ? 1 : 2);
  });
}

test('failed KEY press still attempts key-up and overall release', async () => {
  const dk = {
    remoteKey: vi.fn().mockRejectedValueOnce(new Error('key failed')).mockResolvedValue(undefined),
    remoteRelease: vi.fn(),
  };
  await expect(
    withRemoteRelease(dk as unknown as DkSession, () => remoteTap(dk as unknown as DkSession, 'YES')),
  ).rejects.toThrow('key failed');
  expect(dk.remoteKey.mock.calls).toEqual([
    [10, true],
    [10, false],
  ]);
  expect(dk.remoteRelease).toHaveBeenCalledTimes(1);
});

test('navigation failure releases remote inputs; a hung release has a bounded deadline', async () => {
  const dk = { remoteRelease: vi.fn().mockResolvedValue(undefined) } as unknown as DkSession;
  await expect(
    withRemoteRelease(dk, async () => {
      throw new Error('ABOUT missing');
    }),
  ).rejects.toThrow('ABOUT missing');
  const actionError = new Error('ABOUT missing');
  const releaseError = new Error('release failed');
  vi.mocked(dk.remoteRelease).mockRejectedValueOnce(releaseError);
  await expect(
    withRemoteRelease(dk, async () => {
      throw actionError;
    }),
  ).rejects.toMatchObject({
    name: 'AggregateError',
    errors: [actionError, releaseError],
  });
  vi.useFakeTimers();
  try {
    vi.mocked(dk.remoteRelease).mockImplementation(() => new Promise(() => {}));
    const result = expect(withRemoteRelease(dk, async () => {})).rejects.toThrow('remote release timed out');
    await vi.advanceTimersByTimeAsync(5000);
    await result;
  } finally {
    vi.useRealTimers();
  }
});

test('successful tap retains the 300 ms hold and requested UI settle', async () => {
  vi.useFakeTimers();
  const dk = { remoteKey: vi.fn().mockResolvedValue(undefined) } as unknown as DkSession;
  try {
    const tap = remoteTap(dk, 'SETTINGS', 3000);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(299);
    expect(dk.remoteKey).toHaveBeenCalledExactlyOnceWith(8, true);
    await vi.advanceTimersByTimeAsync(1);
    expect(dk.remoteKey).toHaveBeenLastCalledWith(8, false);
    let settled = false;
    void tap.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(2999);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await tap;
    expect(settled).toBe(true);
  } finally {
    vi.useRealTimers();
  }
});

test('installer failure still releases remote inputs without reaching scenario actions', async () => {
  fake.exec.mockResolvedValueOnce({ stdout: '' }).mockRejectedValueOnce(new Error('install failed'));
  const dk = { remoteRelease: vi.fn().mockResolvedValue(undefined) } as unknown as DkSession;
  const action = vi.fn();
  await expect(
    withRemoteRelease(dk, async () => {
      await prepareRemote({ session } as PatchedSession);
      action();
    }),
  ).rejects.toThrow('install failed');
  expect(action).not.toHaveBeenCalled();
  expect(dk.remoteRelease).toHaveBeenCalledTimes(1);
});

test('key-up failure still reaches the overall remote release', async () => {
  const dk = { remoteKey: vi.fn().mockRejectedValue(new Error('key failed')), remoteRelease: vi.fn() };
  await expect(
    withRemoteRelease(dk as unknown as DkSession, () => remoteTap(dk as unknown as DkSession, 'YES')),
  ).rejects.toThrow('key failed');
  expect(dk.remoteRelease).toHaveBeenCalledTimes(1);
});

test('tagged dependency order and reordered untagged cache-hit reports identify the same loaded image', () => {
  const identity = session.loadResult.runtime;
  if (!identity?.patched) throw new Error('test requires a patched identity');
  for (const modules of [plan.modules, ['core/digikit-menu', 'core/runtime']]) {
    expect(() =>
      validateRemotePlan(plan, {
        options: session.options,
        loadResult: { ...session.loadResult, runtime: { ...identity, modules } },
      }),
    ).not.toThrow();
  }
  expect(fake.read).not.toHaveBeenCalled();
  expect(() =>
    validateRemotePlan(plan, {
      options: session.options,
      loadResult: { ...session.loadResult, runtime: { ...identity, modules: ['core/digikit-menu', 'different'] } },
    }),
  ).toThrow('module membership');
});

test('plan and actual loaded image require present matching SHA-256 digests', () => {
  for (const output of [null, { syx_sha256: '' }, { syx_sha256: 'b'.repeat(64) }]) {
    expect(() => validateRemotePlan({ ...plan, output }, session)).toThrow('output hash');
  }
  for (const syx_sha256 of [undefined, '', 'b'.repeat(64)]) {
    expect(() =>
      validateRemotePlan(plan, {
        options: session.options,
        loadResult: { ...session.loadResult, syx_sha256 },
      }),
    ).toThrow('output hash');
  }
});

test('negative setup installs on its own device-set daemon without patching or reading a symbol plan', async () => {
  const negative = {
    ...session,
    options: { ...session.options, runtime: { patchSet: 'device' } },
  } as Session;
  await installRemote(negative);
  expect(fake.exec).toHaveBeenCalledTimes(1);
  expect(fake.exec.mock.calls[0]?.[1]).toEqual([
    'run',
    'python',
    '-m',
    'tools.device.dktransfer',
    '--emu',
    '--run-dir',
    '/run/own-daemon',
    '--json',
    'install',
    'digikit/remote',
    '--restart',
  ]);
  expect(fake.exec.mock.calls[0]?.[2].timeout).toBe(90_000);
  expect(fake.read).not.toHaveBeenCalled();
  expect(fake.write.mock.calls[0]?.[0]).toMatch(/remote-control\/dt2\/install\.log$/);
});
