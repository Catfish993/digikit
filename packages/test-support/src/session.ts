import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  RpcError,
  spawnDaemon,
  type Client,
  type Daemon,
  type EmuEvent,
  type FirmwareLoadResult,
  type LedsResult,
  type StatusResult,
} from '@digi/control-client';
import { resolveInputs, validateRuntimeInputs, type Device } from './inputs.ts';
import { button, encoder } from './keys.ts';

/** ColdFire instructions per emulated millisecond (the oracle clock). */
export const MS = 132_000;
const MAX_STEP = 200_000_000;
const FAULT_KINDS = new Set(['halt', 'fault', 'stall', 'stall.end', 'lagged']);

export interface SessionOptions {
  device: Device;
  firmware: string;
  /** "zero": no DSP (`--dsp zero --cf-snapshot`); "sharc": coupled DSP audio from the ready inputs. */
  dsp: 'zero' | 'sharc';
  /** Card sample directory for `card.load` (DT2). */
  card?: string;
  /** Run directory; default a fresh temporary directory. */
  runDir?: string;
  /** Stall threshold in emulated ms for the watchdog (default 1000, as the Python harness). */
  stallMs?: number;
  binary?: string;
  /** Runtime boot uses firmware.load's existing patch/cache path, then runs freely. Stock defaults stay stepped. */
  runtime?: { patchSet?: string };
}

export class Session {
  readonly options: SessionOptions;
  #daemon: Daemon;
  #events: EmuEvent[] = [];
  #ownedDir: string | null;
  #load: FirmwareLoadResult | undefined;
  #closing: Promise<void> | undefined;

  private constructor(options: SessionOptions, daemon: Daemon, ownedDir: string | null) {
    this.options = options;
    this.#daemon = daemon;
    this.#ownedDir = ownedDir;
  }

  get client(): Client {
    return this.#daemon.client;
  }

  get loadResult(): FirmwareLoadResult {
    if (!this.#load) throw new Error('firmware not loaded');
    return this.#load;
  }

  static async start(options: SessionOptions): Promise<Session> {
    const inp = resolveInputs(options.device, options.firmware);
    if (options.runtime) validateRuntimeInputs(inp);
    const ownedDir = options.runDir ? null : fs.mkdtempSync(path.join(os.tmpdir(), 'digi-session-'));
    const runDir = options.runDir ?? ownedDir!;
    const args =
      options.dsp === 'zero'
        ? ['--dsp', 'zero', '--cf-snapshot', inp.cfSnapshot]
        : [
            inp.syx,
            '--auto-coupled',
            '--audio-profile-syx',
            inp.syx,
            '--dsp-image',
            inp.dspImage,
            '--dsp-state',
            inp.dspState,
            '--cf-snapshot',
            inp.cfSnapshot,
            '--no-audio',
          ];
    const controller = options.runtime ? new AbortController() : undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const expired = controller
      ? new Promise<never>((_resolve, reject) => {
          // Leave time for the owned daemon's ten-second close before Vitest's 150s runtime hook deadline.
          timer = setTimeout(() => {
            const error = new Error('runtime startup timed out after 120000 ms');
            controller.abort(error);
            reject(error);
          }, 120_000);
        })
      : undefined;
    let session: Session | undefined;
    const started = (async () => {
      const daemon = await spawnDaemon({
        binary: options.binary,
        args,
        runDir,
        timeoutMs: options.runtime ? 120_000 : 300_000,
        signal: controller?.signal,
      });
      session = new Session(options, daemon, ownedDir);
      controller?.signal.throwIfAborted();
      const checked = async <T>(operation: Promise<T>): Promise<T> => {
        const value = await operation;
        controller?.signal.throwIfAborted();
        return value;
      };
      const c = daemon.client;
      await checked(c.eventsSubscribe());
      let card: string | undefined;
      if (options.card) card = (await checked(c.cardLoad(options.card))).card;
      session.#load = await checked(
        c.firmwareLoad({
          syx: inp.syx,
          start: options.runtime ? 'auto' : 'snapshot',
          ...(options.runtime ? { runtime: true, patch_set: options.runtime.patchSet } : {}),
          force: true,
          auto_no: true,
          ...(card ? { card } : {}),
        }),
      );
      await checked(c.watchdogConfig(options.stallMs ?? 1000));
      if (options.runtime) await checked(c.call('run.mode', { mode: 'free' }));
      return session;
    })();
    try {
      return await (expired ? Promise.race([started, expired]) : started);
    } catch (err) {
      controller?.abort(err);
      if (session) await session.close();
      else {
        // A spawn still connecting owns the child: its abort path waits for exit. If a handle
        // arrives after expiry, the signal check rejects and we close that late handle too.
        const lateSession = await started.catch(() => session);
        if (lateSession) await lateSession.close();
        else if (ownedDir) fs.rmSync(ownedDir, { recursive: true, force: true });
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  /** Back to the ready state of the running firmware, modal closed; clears collected events. */
  async restart(): Promise<void> {
    if (this.options.runtime) throw new Error('runtime sessions require a fresh daemon; restart is stock-only');
    await this.client.restart(true);
    this.#collect();
    this.#events = [];
    await this.wait(300);
  }

  /** Tap a key (or a chord: all but the last are held), hold HOLDMS, release. */
  async press(key: string | number | (string | number)[], holdMs = 50): Promise<void> {
    const seq = Array.isArray(key) ? key : [key];
    const last = seq[seq.length - 1];
    if (last === undefined) throw new Error('press: empty key list');
    const held = seq.slice(0, -1).map(button);
    for (const code of held) {
      await this.client.inputButton(code, true);
      await this.wait(5);
    }
    await this.client.inputTap(button(last), Math.floor(holdMs * MS));
    await this.wait(holdMs + 5);
    for (const code of held.reverse()) {
      await this.client.inputButton(code, false);
      await this.wait(5);
    }
  }

  async turn(enc: string | number, detents: number): Promise<void> {
    await this.client.inputTurn(encoder(enc), detents);
    await this.wait(10);
  }

  /** Run MS emulated milliseconds. A guest fault is recorded in `faults()` and rethrown. */
  async wait(ms: number): Promise<void> {
    if (this.options.runtime) throw new Error('runtime sessions run freely; wait() steps stock sessions only');
    let n = Math.floor(ms * MS);
    while (n > 0) {
      const chunk = Math.min(n, MAX_STEP);
      try {
        await this.client.step(chunk, { timeout_wall_s: 300 });
      } catch (err) {
        if (err instanceof RpcError && err.code === 6) {
          this.#collect();
          this.#events.push({ kind: 'fault', message: err.message, data: err.data });
        }
        throw err;
      }
      n -= chunk;
    }
  }

  /** Hex SHA-256 of the 1024 decoded OLED bytes. */
  frameHash(): Promise<string> {
    return this.client.frameHash();
  }
  leds(): Promise<LedsResult> {
    return this.client.ledsGet();
  }
  status(): Promise<StatusResult> {
    return this.client.status();
  }

  #collect(): void {
    this.#events.push(...this.client.drainEvents().filter((e) => e.kind !== 'frame'));
  }

  /** Fault, halt, stall, guard and lagged events collected since the last restart. */
  async faults(): Promise<EmuEvent[]> {
    await this.client.status(); // events can follow their reply; a round trip flushes them
    this.#collect();
    return this.#events.filter((e) => FAULT_KINDS.has(e.kind) || e.kind.startsWith('guard'));
  }

  /** Throws when `faults()` is non-empty or status shows a fault, DSP halt or stall. */
  async expectNoFault(where = ''): Promise<void> {
    const status = await this.client.status();
    const faults = await this.faults();
    const problems: string[] = faults.map((e) => JSON.stringify(e));
    const { cf, heartbeat } = status;
    if (cf.error || cf.phase === 'fault') problems.push(`cf fault: ${cf.error} pc=${cf.pc.toString(16)}`);
    if (status.audio?.halted) problems.push(`DSP halt: ${JSON.stringify(status.audio.halted)}`);
    if (heartbeat.stalled || heartbeat.stall_count) {
      problems.push(`stall: count=${heartbeat.stall_count} threshold=${heartbeat.stall_ms} ms`);
    }
    if (problems.length) throw new Error(`fault${where ? ` at ${where}` : ''}: ${problems.join('; ')}`);
  }

  /** Quit the daemon and remove an owned temporary run directory. */
  close(): Promise<void> {
    return (this.#closing ??= (async () => {
      try {
        await this.#daemon.close();
      } finally {
        if (this.#ownedDir) fs.rmSync(this.#ownedDir, { recursive: true, force: true });
      }
    })());
  }
}
