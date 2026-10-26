import fs from 'node:fs';
import { beforeEach, expect, test, vi } from 'vitest';

const fake = vi.hoisted(() => {
  const client = {
    eventsSubscribe: vi.fn(),
    cardLoad: vi.fn(),
    firmwareLoad: vi.fn(),
    watchdogConfig: vi.fn(),
    call: vi.fn(),
  };
  return { client, close: vi.fn(), spawn: vi.fn() };
});
vi.mock('@digi/control-client', async (original) => ({
  ...(await original<object>()),
  spawnDaemon: fake.spawn,
}));
vi.mock('../../../packages/test-support/src/inputs.ts', () => ({
  validateRuntimeInputs: vi.fn(),
  resolveInputs: () => ({ syx: '/stock.syx', cfSnapshot: '/ready.snap', dspImage: '/dsp.bin', dspState: '/dsp.snap' }),
}));
import { Session } from '@digi/test-support';
import { startPatchedSession } from '../support.ts';

const identity = {
  patched: true as const,
  set: 'emulator',
  cache_hit: false,
  modules: ['digikit'],
  base_sha256: 'a'.repeat(64),
  cache_file: '/cache.syx',
  seconds: 0.1,
};
beforeEach(() => {
  vi.resetAllMocks();
  fake.spawn.mockResolvedValue({ client: fake.client, close: fake.close });
  fake.client.cardLoad.mockResolvedValue({ card: '/card.img' });
  fake.client.firmwareLoad.mockResolvedValue({ runtime: identity, start_used: 'snapshot' });
  fake.client.call.mockResolvedValue({ hex: '' });
});

test('stock defaults retain snapshot load and stepped lifecycle', async () => {
  const session = await Session.start({ device: 'dt2', firmware: '1.17', dsp: 'zero' });
  try {
    expect(fake.client.firmwareLoad).toHaveBeenCalledWith({
      syx: '/stock.syx',
      start: 'snapshot',
      force: true,
      auto_no: true,
    });
    expect(fake.client.call).not.toHaveBeenCalled();
    expect(fake.spawn.mock.calls[0]?.[0].args).toEqual(['--dsp', 'zero', '--cf-snapshot', '/ready.snap']);
  } finally {
    await session.close();
  }
});

test('zero-DSP runtime startup selects the firmware before applying the runtime patch', async () => {
  const session = await Session.start({ device: 'dt2', firmware: '1.17', dsp: 'zero', runtime: {} });
  try {
    expect(fake.spawn.mock.calls[0]?.[0].args).toEqual(['/stock.syx', '--dsp', 'zero', '--cf-snapshot', '/ready.snap']);
    expect(fake.client.firmwareLoad).toHaveBeenCalledWith({
      syx: '/stock.syx',
      start: 'auto',
      force: true,
      auto_no: true,
      runtime: true,
      patch_set: undefined,
    });
    expect(fake.client.call).toHaveBeenCalledWith('run.mode', { mode: 'free' });
  } finally {
    await session.close();
  }
});

test('a mirrored runtime owns its mirror inside the temporary run directory', async () => {
  const runtime = await startPatchedSession({ device: 'dn2', firmware: '1.12', mirror: true });
  const runDir = runtime.session.runDir;
  try {
    expect(fs.existsSync(runDir)).toBe(true);
    expect(fake.spawn.mock.calls[0]?.[0].args.slice(-2)).toEqual(['--mirror-dir', `${runDir}/mirror`]);
    fs.mkdirSync(`${runDir}/mirror/digikit/modules`, { recursive: true });
    fs.writeFileSync(`${runDir}/mirror/digikit/modules/pluck.dkm`, 'owned test data');
  } finally {
    await runtime.close();
  }
  expect(fs.existsSync(runDir), 'close removes the owned mirror along with its run directory').toBe(false);
});

test('patched fixture loads the card, requests runtime auto boot and free mode, and wires USB RPC', async () => {
  const runtime = await startPatchedSession({
    device: 'dt2',
    firmware: '1.17',
    card: '/samples',
    patchSet: 'emulator',
  });
  try {
    expect(fake.client.firmwareLoad).toHaveBeenCalledWith({
      syx: '/stock.syx',
      start: 'auto',
      force: true,
      auto_no: true,
      runtime: true,
      patch_set: 'emulator',
      card: '/card.img',
    });
    expect(fake.spawn.mock.calls[0]?.[0].args).toEqual([
      '/stock.syx',
      '--auto-coupled',
      '--audio-profile-syx',
      '/stock.syx',
      '--dsp-image',
      '/dsp.bin',
      '--dsp-state',
      '/dsp.snap',
      '--cf-snapshot',
      '/ready.snap',
      '--no-audio',
    ]);
    expect(fake.client.call).toHaveBeenCalledWith('run.mode', { mode: 'free' });
    expect(runtime.session.loadResult.runtime).toEqual(identity);
    await expect(runtime.session.restart()).rejects.toThrow('fresh daemon');
    await expect(runtime.session.wait(1)).rejects.toThrow('run freely');
    const pending = runtime.dk.request(1);
    const rejected = expect(pending).rejects.toMatchObject({ kind: 'cancelled' });
    await vi.waitFor(() => expect(fake.client.call).toHaveBeenCalledWith('midi.usb.out', undefined));
    expect(fake.client.call.mock.calls.some(([method]) => method === 'midi.usb.in')).toBe(true);
    await runtime.close();
    await rejected;
    await runtime.close();
    expect(fake.close).toHaveBeenCalledTimes(1);
  } finally {
    await runtime.close();
  }
});

for (const failure of ['firmwareLoad', 'watchdogConfig', 'call'] as const) {
  test(`startup ${failure} failure closes its daemon`, async () => {
    fake.client[failure].mockRejectedValueOnce(new Error('startup failed'));
    await expect(startPatchedSession({ device: 'dn2', firmware: '1.12' })).rejects.toThrow('startup failed');
    expect(fake.close).toHaveBeenCalledTimes(1);
  });
}

test('daemon spawn failure removes the owned temporary run directory', async () => {
  fake.spawn.mockRejectedValueOnce(new Error('spawn failed'));
  await expect(startPatchedSession({ device: 'dn2', firmware: '1.12' })).rejects.toThrow('spawn failed');
  const runDir = fake.spawn.mock.calls[0]?.[0].runDir;
  expect(runDir).toBeTruthy();
  expect(fs.existsSync(runDir)).toBe(false);
});

test('runtime startup deadline closes a hung load before rejecting', async () => {
  vi.useFakeTimers();
  let finishClose!: () => void;
  fake.close.mockImplementation(
    () =>
      new Promise<void>((resolve) => {
        finishClose = resolve;
      }),
  );
  fake.client.firmwareLoad.mockImplementation(() => new Promise(() => {}));
  try {
    const started = startPatchedSession({ device: 'dn2', firmware: '1.12' });
    let rejected = false;
    const result = expect(started)
      .rejects.toThrow('runtime startup timed out')
      .then(() => {
        rejected = true;
      });
    await vi.advanceTimersByTimeAsync(120_000);
    expect(fake.close).toHaveBeenCalledTimes(1);
    expect(rejected).toBe(false);
    expect(fake.spawn.mock.calls[0]?.[0].signal.aborted).toBe(true);
    finishClose();
    await result;
    const runDir = fake.spawn.mock.calls[0]?.[0].runDir;
    expect(fs.existsSync(runDir)).toBe(false);
  } finally {
    vi.useRealTimers();
  }
});

test('runtime startup expiry closes a late spawn handle before rejecting', async () => {
  vi.useFakeTimers();
  let spawned!: (daemon: object) => void;
  fake.spawn.mockImplementation(
    () =>
      new Promise((resolve) => {
        spawned = resolve;
      }),
  );
  try {
    const started = startPatchedSession({ device: 'dn2', firmware: '1.12' });
    const result = expect(started).rejects.toThrow('runtime startup timed out');
    await vi.advanceTimersByTimeAsync(120_000);
    spawned({ client: fake.client, close: fake.close });
    await result;
    expect(fake.close).toHaveBeenCalledTimes(1);
    expect(fake.client.eventsSubscribe).not.toHaveBeenCalled();
  } finally {
    vi.useRealTimers();
  }
});

test('concurrent fixture and Session closes await the same daemon cleanup', async () => {
  let finishClose!: () => void;
  fake.close.mockImplementation(
    () =>
      new Promise<void>((resolve) => {
        finishClose = resolve;
      }),
  );
  const runtime = await startPatchedSession({ device: 'dn2', firmware: '1.12' });
  const first = runtime.close();
  const second = runtime.close();
  expect(second).toBe(first);
  const sessionClose = runtime.session.close();
  expect(runtime.session.close()).toBe(sessionClose);
  expect(fake.close).toHaveBeenCalledTimes(1);
  let closed = false;
  void second.then(() => {
    closed = true;
  });
  await Promise.resolve();
  expect(closed).toBe(false);
  finishClose();
  await Promise.all([first, second, sessionClose]);
  expect(closed).toBe(true);
});
