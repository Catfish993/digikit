// The SysEx path of the desktop app: the controller's USB MIDI capture, through the webview's `emu_call` (docs/CONTROL.md,
// `midi.usb.in` and `midi.usb.out`), as a transport of the TypeScript protocol client (`DkSession`). Nothing here talks to a MIDI device.
import { invoke } from '@tauri-apps/api/core';
import { splitMessages, type DkTransport } from '@web/modules/dkupload-core';

/** Messages are handed over in the high-speed model (384 bytes per 16,500 instructions, a microframe), as the kept upload scenarios do. */
const USB_CHUNK = 384;
const USB_PERIOD = 16_500;
const POLL_MS = 10;

const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
const bytesOf = (h: string) => Uint8Array.from(h.match(/../g) ?? [], (x) => parseInt(x, 16));
const call = <T>(method: string, params: Record<string, unknown> = {}) =>
  invoke<T>('emu_call', { method, params, session: null });

export class EmuUsbTransport implements DkTransport {
  private rx: Uint8Array = new Uint8Array();
  private ready: Uint8Array[] = [];

  async send(msg: Uint8Array): Promise<void> {
    await call('midi.usb.in', { bytes: hex(msg), chunk: USB_CHUNK, period: USB_PERIOD });
  }

  /** The next complete message the firmware sent, or null after `timeoutMs`. */
  async recv(timeoutMs: number): Promise<Uint8Array | null> {
    const end = Date.now() + timeoutMs;
    for (;;) {
      const next = this.ready.shift();
      if (next) return next;
      const { hex: got } = await call<{ hex?: string }>('midi.usb.out');
      if (got) {
        const joined = new Uint8Array(this.rx.length + got.length / 2);
        joined.set(this.rx);
        joined.set(bytesOf(got), this.rx.length);
        const { messages, rest } = splitMessages(joined);
        this.rx = rest;
        this.ready.push(...messages);
        continue;
      }
      if (Date.now() >= end) return null;
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
  }
}

/** The controller's run mode: a paused emulator would never answer (nothing steps it). */
export async function emulatorMode(): Promise<string | undefined> {
  return (await call<{ mode?: string }>('status')).mode;
}
