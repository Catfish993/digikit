import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  RpcError,
  spawnDaemon,
  type Client,
  type Daemon,
  type EmuEvent,
  type LedsResult,
  type StatusResult,
} from '@digi/control-client';
import { resolveInputs, type Device } from './inputs.ts';
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
}

export class Session {
  readonly options: SessionOptions;
  #daemon: Daemon;
  #events: EmuEvent[] = [];
  #ownedDir: string | null;

  private constructor(options: SessionOptions, daemon: Daemon, ownedDir: string | null) {
    this.options = options;
    this.#daemon = daemon;
    this.#ownedDir = ownedDir;
  }

  get client(): Client {
    return this.#daemon.client;
  }

  static async start(options: SessionOptions): Promise<Session> {
    const inp = resolveInputs(options.device, options.firmware);
    const ownedDir = options.runDir ? null : fs.mkdtempSync(path.join(os.tmpdir(), 'digi-session-'));
    const runDir = options.runDir ?? ownedDir!;
    const args =
      options.dsp === 'zero'
        ? ['--dsp', 'zero', '--cf-snapshot', inp.cfSnapshot]
        : [
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
    const daemon = await spawnDaemon({ binary: options.binary, args, runDir, timeoutMs: 300_000 });
    const session = new Session(options, daemon, ownedDir);
    try {
      const c = daemon.client;
      await c.eventsSubscribe();
      let card: string | undefined;
      if (options.card) card = (await c.cardLoad(options.card)).card;
      await c.firmwareLoad({
        syx: inp.syx,
        start: 'snapshot',
        force: true,
        auto_no: true,
        ...(card ? { card } : {}),
      });
      await c.watchdogConfig(options.stallMs ?? 1000);
    } catch (err) {
      await session.close();
      throw err;
    }
    return session;
  }

  /** Back to the ready state of the running firmware, modal closed; clears collected events. */
  async restart(): Promise<void> {
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
  async close(): Promise<void> {
    await this.#daemon.close();
    if (this.#ownedDir) fs.rmSync(this.#ownedDir, { recursive: true, force: true });
  }
}
