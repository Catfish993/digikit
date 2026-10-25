// Web MIDI (with SysEx permission) as a DkTransport. The access object is passed in, so the tests give it a mock.
// Written without TS-only runtime syntax so node can import it.

import type { DkTransport } from '@digi/dk-session';

/** The parts of the Web MIDI API this file uses. */
export interface MidiPortLike {
  id: string;
  name?: string | null;
  manufacturer?: string | null;
  state?: string;
}
export interface MidiInputLike extends MidiPortLike {
  onmidimessage: ((e: { data: Uint8Array | null }) => void) | null;
  open?(): Promise<unknown>;
}
export interface MidiOutputLike extends MidiPortLike {
  send(data: Uint8Array | number[]): void;
  open?(): Promise<unknown>;
}
export interface MidiAccessLike {
  inputs: Iterable<[string, MidiInputLike]> | { values(): Iterable<MidiInputLike> };
  outputs: Iterable<[string, MidiOutputLike]> | { values(): Iterable<MidiOutputLike> };
}

const values = <T>(m: { values(): Iterable<T> } | Iterable<[string, T]>): T[] =>
  'values' in m && typeof m.values === 'function'
    ? Array.from(m.values())
    : Array.from(m as Iterable<[string, T]>, (e) => e[1]);

export const portLabel = (p: MidiPortLike) => [p.manufacturer, p.name].filter(Boolean).join(' ') || p.id;

export async function requestAccess(): Promise<MidiAccessLike> {
  const nav = globalThis.navigator as unknown as {
    requestMIDIAccess?: (o: { sysex: boolean }) => Promise<MidiAccessLike>;
  };
  if (!nav?.requestMIDIAccess) throw new Error('This browser has no Web MIDI. Use Chrome, Edge or the desktop app.');
  return nav.requestMIDIAccess({ sysex: true });
}

export function listPorts(access: MidiAccessLike): { inputs: MidiInputLike[]; outputs: MidiOutputLike[] } {
  return { inputs: values(access.inputs), outputs: values(access.outputs) };
}

/**
 * One input and one output. SysEx arrives as one event or in pieces; a message is complete at F7. Real-time bytes
 * (F8..FF) inside it are dropped.
 */
export class WebMidiTransport implements DkTransport {
  private input: MidiInputLike;
  private output: MidiOutputLike;
  private buf: number[] | null = null;
  private queue: Uint8Array[] = [];
  private waiter: ((m: Uint8Array | null) => void) | null = null;

  constructor(input: MidiInputLike, output: MidiOutputLike) {
    this.input = input;
    this.output = output;
    input.onmidimessage = (e) => this.feed(e.data);
  }
  static async open(input: MidiInputLike, output: MidiOutputLike): Promise<WebMidiTransport> {
    await input.open?.();
    await output.open?.();
    return new WebMidiTransport(input, output);
  }

  private feed(data: Uint8Array | null) {
    for (const b of data ?? []) {
      if (b >= 0xf8) continue;
      if (b === 0xf0) this.buf = [b];
      else if (this.buf) {
        this.buf.push(b);
        if (b === 0xf7) {
          const m = Uint8Array.from(this.buf);
          this.buf = null;
          if (this.waiter) {
            const w = this.waiter;
            this.waiter = null;
            w(m);
          } else this.queue.push(m);
        } else if (b >= 0x80) this.buf = null; // another status byte: not a SysEx message any more
      }
    }
  }

  send(msg: Uint8Array): void {
    this.output.send(msg);
  }
  recv(timeoutMs: number): Promise<Uint8Array | null> {
    const q = this.queue.shift();
    if (q) return Promise.resolve(q);
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.waiter = null;
        resolve(null);
      }, timeoutMs);
      this.waiter = (m) => {
        clearTimeout(timer);
        resolve(m);
      };
    });
  }
  close() {
    this.input.onmidimessage = null;
  }
}
