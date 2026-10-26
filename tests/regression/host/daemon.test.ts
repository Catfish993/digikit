import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { expect, test, vi } from 'vitest';

const fake = vi.hoisted(() => ({ spawn: vi.fn(), connect: vi.fn() }));
vi.mock('node:child_process', () => ({ spawn: fake.spawn }));
vi.mock('../../../packages/control-client/src/client.ts', () => ({ connect: fake.connect }));
import { spawnDaemon } from '../../../packages/control-client/src/daemon.ts';

test('unresponsive quit cannot postpone the owned child shutdown deadline', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'digi-daemon-test-'));
  const child = new EventEmitter() as EventEmitter & { kill: ReturnType<typeof vi.fn> };
  child.kill = vi.fn(() => child.emit('exit', null));
  const client = { quit: vi.fn(() => new Promise(() => {})), close: vi.fn() };
  fake.connect.mockResolvedValue(client);
  fake.spawn.mockImplementation(() => {
    const socket = path.join(dir, 'test.sock');
    fs.writeFileSync(socket, '');
    fs.writeFileSync(path.join(dir, 'current.json'), JSON.stringify({ socket }));
    return child;
  });
  vi.useFakeTimers();
  try {
    const daemon = await spawnDaemon({ binary: process.execPath, runDir: dir });
    const closing = daemon.close();
    expect(daemon.close()).toBe(closing);
    expect(client.quit).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10_000);
    await closing;
    expect(child.kill).toHaveBeenCalledExactlyOnceWith('SIGKILL');
    expect(client.close).toHaveBeenCalled();
  } finally {
    vi.useRealTimers();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('startup abort kills and awaits its child while the socket handshake is pending', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'digi-daemon-abort-'));
  const child = new EventEmitter() as EventEmitter & { kill: ReturnType<typeof vi.fn> };
  let finishExit!: () => void;
  child.kill = vi.fn(() => {
    finishExit = () => {
      child.emit('exit', null);
    };
  });
  fake.spawn.mockImplementation(() => {
    const socket = path.join(dir, 'test.sock');
    fs.writeFileSync(socket, '');
    fs.writeFileSync(path.join(dir, 'current.json'), JSON.stringify({ socket }));
    return child;
  });
  fake.connect.mockImplementation(
    (_socket, options) =>
      new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true });
      }),
  );
  const controller = new AbortController();
  try {
    const started = spawnDaemon({ binary: process.execPath, runDir: dir, signal: controller.signal });
    let rejected = false;
    const result = expect(started)
      .rejects.toThrow('startup cancelled')
      .then(() => {
        rejected = true;
      });
    controller.abort(new Error('startup cancelled'));
    await Promise.resolve();
    expect(child.kill).toHaveBeenCalledExactlyOnceWith('SIGKILL');
    expect(rejected).toBe(false);
    finishExit();
    await result;
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('startup abort closes a client returned after cancellation', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'digi-daemon-late-'));
  const child = new EventEmitter() as EventEmitter & { kill: ReturnType<typeof vi.fn> };
  child.kill = vi.fn(() => child.emit('exit', null));
  fake.spawn.mockImplementation(() => {
    const socket = path.join(dir, 'test.sock');
    fs.writeFileSync(socket, '');
    fs.writeFileSync(path.join(dir, 'current.json'), JSON.stringify({ socket }));
    return child;
  });
  let connected!: (client: object) => void;
  fake.connect.mockImplementation(
    () =>
      new Promise((resolve) => {
        connected = resolve;
      }),
  );
  const client = { close: vi.fn() };
  const controller = new AbortController();
  try {
    const started = spawnDaemon({ binary: process.execPath, runDir: dir, signal: controller.signal });
    const result = expect(started).rejects.toThrow('startup cancelled');
    controller.abort(new Error('startup cancelled'));
    connected(client);
    await result;
    expect(client.close).toHaveBeenCalledTimes(1);
    expect(child.kill).toHaveBeenCalledExactlyOnceWith('SIGKILL');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
