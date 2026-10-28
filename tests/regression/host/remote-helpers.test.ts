import fs from 'node:fs';
import { beforeEach, expect, test, vi } from 'vitest';
import type { DkSession } from '@digi/dk-session';
import type { Session } from '@digi/test-support';
import type { PatchedSession } from '../support.ts';

const fake = vi.hoisted(() => ({
  exec: vi.fn(),
  read: vi.fn(),
  exists: vi.fn(() => true),
  mkdir: vi.fn(),
  mkdtemp: vi.fn(),
  write: vi.fn(),
}));
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
    existsSync: fake.exists,
    mkdirSync: fake.mkdir,
    mkdtempSync: fake.mkdtemp,
    writeFileSync: fake.write,
  },
}));
vi.mock('../../../packages/test-support/src/inputs.ts', async (original) => ({
  ...(await original<object>()),
  resolveInputs: () => ({ syx: '/stock.syx' }),
}));
import { installRemote, prepareRemote, remoteTap, withRemoteRelease } from '../runtime/remote.ts';
import { planSymbol, validateRuntimePlan, type RuntimePlan } from '../runtime/runtime-plan.ts';
import { openDigikitMenu, openRuntimeAbout, readAboutRows, expectRuntimeAbout } from '../runtime/runtime-menu.ts';

const patchedSha256 = 'c'.repeat(64);
const plan: RuntimePlan = {
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

/** A fake protocol session that records the order of the native install calls. */
function fakeDk() {
  const calls: string[] = [];
  const record = (name: string, result?: unknown) =>
    vi.fn(async (..._args: unknown[]) => {
      calls.push(name);
      return result;
    });
  const dk = {
    put: record('put'),
    verifyFile: record('verifyFile', true),
    select: record('select'),
    restart: record('restart'),
    waitForBoot: record('waitForBoot'),
    modules: record('modules', [{ name: 'digikit/remote' }]),
    remoteRelease: vi.fn().mockResolvedValue(undefined),
  };
  return { calls, dk, runtime: { session, dk } as unknown as PatchedSession };
}

beforeEach(() => {
  vi.resetAllMocks();
  fake.read.mockReturnValue(JSON.stringify(plan));
  fake.exists.mockReturnValue(true);
  fake.mkdtemp.mockReturnValue('/artifacts/remote-input/dt2/run-1');
  fake.exec.mockResolvedValue({ stdout: '' });
});

test('plan identity requires the device, version, base hash and expanded module membership', () => {
  expect(() => validateRuntimePlan(plan, session)).not.toThrow();
  for (const base of [
    { ...plan.base, device: 'dn2' },
    { ...plan.base, version: '1.16' },
    { ...plan.base, syx_sha256: 'b'.repeat(64) },
  ])
    expect(() => validateRuntimePlan({ ...plan, base }, session)).toThrow('does not match');
  for (const modules of [['emulator'], [...plan.modules, 'extra']]) {
    expect(() => validateRuntimePlan({ ...plan, modules }, session)).toThrow('does not match');
  }
});

test('symbol lookup accepts aliases of one address, including mangled Rust names, but rejects ambiguity or absence', () => {
  const symbols = { 'a:DK_ABOUT': 1, 'b:DK_ABOUT': 1, _8DK_ABOUT: 1, 'a:DK_ABOUT_N': 2 };
  expect(planSymbol({ ...plan, symbols }, 'DK_ABOUT')).toBe(1);
  expect(planSymbol({ ...plan, symbols: { _8DK_ABOUT: 1 } }, 'DK_ABOUT')).toBe(1);
  expect(() => planSymbol({ ...plan, symbols: { ...symbols, 'c:DK_ABOUT': 3 } }, 'DK_ABOUT')).toThrow('2 addresses');
  expect(() => planSymbol({ ...plan, symbols: {} }, 'DK_ABOUT')).toThrow('0 addresses');
});

test('setup reuses the Python patch command, its own daemon and a bounded tool deadline, then installs natively', async () => {
  const { calls, runtime } = fakeDk();
  expect(await prepareRemote(runtime)).toEqual(plan);
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
    '/run/own-daemon/runtime-dt2.syx',
    '--plan',
    '/run/own-daemon/runtime-dt2.plan.json',
  ]);
  expect(fake.exec.mock.calls.map((call) => call[2].timeout)).toEqual([30_000]);
  expect(fs.readFileSync).toHaveBeenCalledWith('/run/own-daemon/runtime-dt2.plan.json', 'utf8');
  expect(calls).toEqual(['put', 'verifyFile', 'select', 'restart', 'waitForBoot', 'modules']);
});

for (const stage of ['patch', 'install', 'plan'] as const) {
  test(`setup ${stage} failure cannot continue to navigation`, async () => {
    const { dk, runtime } = fakeDk();
    if (stage === 'patch') fake.exec.mockRejectedValueOnce(new Error('patch failed'));
    if (stage === 'install') dk.put.mockRejectedValueOnce(new Error('install failed'));
    if (stage === 'plan') fake.read.mockReturnValue(JSON.stringify({ ...plan, modules: ['wrong'] }));
    await expect(prepareRemote(runtime)).rejects.toThrow();
    expect(fake.exec).toHaveBeenCalledTimes(1);
    expect(dk.put).toHaveBeenCalledTimes(stage === 'install' ? 1 : 0);
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
  const { dk, runtime } = fakeDk();
  dk.put.mockRejectedValueOnce(new Error('install failed'));
  const action = vi.fn();
  await expect(
    withRemoteRelease(dk as unknown as DkSession, async () => {
      await prepareRemote(runtime);
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
      validateRuntimePlan(plan, {
        options: session.options,
        loadResult: { ...session.loadResult, runtime: { ...identity, modules } },
      }),
    ).not.toThrow();
  }
  expect(fake.read).not.toHaveBeenCalled();
  expect(() =>
    validateRuntimePlan(plan, {
      options: session.options,
      loadResult: { ...session.loadResult, runtime: { ...identity, modules: ['core/digikit-menu', 'different'] } },
    }),
  ).toThrow('module membership');
});

test('plan and actual loaded image require present matching SHA-256 digests', () => {
  for (const output of [null, { syx_sha256: '' }, { syx_sha256: 'b'.repeat(64) }]) {
    expect(() => validateRuntimePlan({ ...plan, output }, session)).toThrow('output hash');
  }
  for (const syx_sha256 of [undefined, '', 'b'.repeat(64)]) {
    expect(() =>
      validateRuntimePlan(plan, {
        options: session.options,
        loadResult: { ...session.loadResult, syx_sha256 },
      }),
    ).toThrow('output hash');
  }
});

test('negative setup installs natively on its own device-set daemon without patching or reading a symbol plan', async () => {
  const { calls, dk, runtime } = fakeDk();
  const negative = {
    ...runtime,
    session: { ...session, options: { ...session.options, runtime: { patchSet: 'device' } } },
  } as PatchedSession;
  await installRemote(negative);
  expect(fake.exec).not.toHaveBeenCalled();
  expect(fake.read).toHaveBeenCalledExactlyOnceWith(expect.stringMatching(/dt2\/remote\.dkm$/));
  expect(calls).toEqual(['put', 'verifyFile', 'select', 'restart', 'waitForBoot', 'modules']);
  expect(dk.put.mock.calls[0]?.[0]).toBe('modules/remote.dkm');
  expect(dk.verifyFile.mock.calls[0]?.[0]).toBe('modules/remote.dkm');
  expect(dk.select).toHaveBeenCalledExactlyOnceWith(['modules/remote.dkm']);
});

test('menu navigation uses named SETTINGS and DIGIKIT checkpoints before ABOUT', async () => {
  const tap = vi.fn().mockResolvedValue(undefined);
  const checkpoint = vi.fn().mockResolvedValue(undefined);
  await openDigikitMenu(tap, checkpoint);
  await openRuntimeAbout(tap);
  expect(tap.mock.calls).toEqual([
    ['NO', 2000],
    ['SETTINGS', 3000],
    ...Array.from({ length: 8 }, () => ['DOWN']),
    ['YES', 3000],
    ['YES', 3000],
  ]);
  expect(checkpoint.mock.calls).toEqual([['settings-ninth-row'], ['digikit-main']]);
});

test('ABOUT reads are bounded and assert the live protocol runtime identity without a Python formatter', async () => {
  const memPeek = vi.fn().mockResolvedValue(25);
  const menuSession = { client: { memPeek } } as unknown as Session;
  await expect(readAboutRows(menuSession, plan)).rejects.toThrow('invalid ABOUT row count');
  expect(memPeek.mock.calls).toEqual([[8192]]);
  const rows = ['DIGIKIT 0.1.0', 'RUNTIME 0.2.5'];
  const bytes = Buffer.from(rows.map((row) => row.padEnd(24, '\0')).join(''), 'latin1');
  memPeek.mockImplementation(async (address: number) =>
    address === 8192 ? rows.length : bytes.readUInt32BE(address - 4096),
  );
  const info = vi.fn().mockResolvedValue({ runtime: 0x205 });
  await expect(expectRuntimeAbout(menuSession, { info } as unknown as DkSession, plan)).resolves.toEqual(rows);
  expect(info).toHaveBeenCalledWith();
  expect(fake.exec).not.toHaveBeenCalled();
});
