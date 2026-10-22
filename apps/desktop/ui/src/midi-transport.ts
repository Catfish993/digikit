// The SysEx path of a real device: a MIDI port the app opens through its Rust side (`midi_port.rs`, midir), as a transport of the TypeScript
// protocol client (`DkSession`), shaped like desktop-transport.ts. The native side moves whole messages and starts no process.
import { invoke } from '@tauri-apps/api/core';
import type { DkTransport } from '@web/modules/dkupload-core';

/** The MIDI ports that exist as input and as output (a device appears once under one name). */
export const listMidiPorts = () => invoke<string[]>('midi_ports');
export const openMidiPort = (name: string) => invoke<void>('midi_open', { name });
export const closeMidiPort = () => invoke<void>('midi_close');
/** End the pending `recv` on the native side now (it answers as if its time had run out). */
const cancelMidiRecv = () => invoke<void>('midi_cancel');

export class MidiPortTransport implements DkTransport {
  async send(msg: Uint8Array): Promise<void> {
    await invoke('midi_send', msg);
  }

  /** The next complete message the device sent, or null after `timeoutMs` or when `signal` aborts (the native wait ends too). */
  async recv(timeoutMs: number, signal?: AbortSignal): Promise<Uint8Array | null> {
    if (signal?.aborted) return null;
    const onAbort = () => void cancelMidiRecv().catch(() => {});
    signal?.addEventListener('abort', onAbort, { once: true });
    try {
      const got = await invoke<ArrayBuffer>('midi_recv', { timeoutMs: Math.max(1, Math.round(timeoutMs)) });
      return got.byteLength ? new Uint8Array(got) : null;
    } finally {
      signal?.removeEventListener('abort', onAbort);
    }
  }
}
