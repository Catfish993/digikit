// The workbench target: the emulator, or a MIDI port with a real device on it. Every page that talks the digikit protocol (Transfer, Remote)
// uses the one selected here. The protocol client is the same in both cases (`DkSession`); only the transport differs.
import { createSignal } from 'solid-js';
import { DkSession } from '@web/modules/dkupload-core';
import { instrument } from './activity';
import { EmuUsbTransport, emulatorMode } from './desktop-transport';
import { closeMidiPort, MidiPortTransport, openMidiPort } from './midi-transport';

export type Target = { kind: 'emulator' } | { kind: 'midi'; port: string };

const KEY = 'digikit.target';
const read = (): Target => {
  try {
    const t = JSON.parse(localStorage.getItem(KEY) ?? 'null') as Target | null;
    if (t?.kind === 'midi' && typeof t.port === 'string' && t.port) return t;
  } catch {
    // no storage: the emulator
  }
  return { kind: 'emulator' };
};

export const [target, setTargetSignal] = createSignal<Target>(read());

export const targetLabel = (t: Target): string => (t.kind === 'emulator' ? 'Emulator' : `MIDI: ${t.port}`);
export const sameTarget = (a: Target, b: Target): boolean =>
  a.kind === b.kind && (a.kind === 'emulator' || a.port === (b as { port: string }).port);

export const PAUSED =
  'The emulator is paused: nothing would run the transfer. Resume it (the Run button of the workbench), then try again.';

let shared: DkSession | undefined;
let opened: string | undefined;

/** Change the target: the next request uses a new session (and opens the port). A MIDI port that cannot be opened leaves the old target. */
export async function selectTarget(next: Target): Promise<void> {
  if (sameTarget(next, target())) return;
  if (next.kind === 'midi') await openMidiPort(next.port);
  else if (opened) await closeMidiPort();
  opened = next.kind === 'midi' ? next.port : undefined;
  cancelPending();
  shared = undefined;
  setTargetSignal(next);
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // the choice is not kept
  }
}

/** One session for the window and target: its requests take turns on the one reply stream. A saved MIDI target is opened at its first use. */
export async function session(): Promise<DkSession> {
  const t = target();
  if (t.kind === 'emulator') {
    if ((await emulatorMode()) === 'paused') throw new Error(PAUSED);
    return (shared ??= new DkSession(instrument(new EmuUsbTransport()), { timeoutMs: 6000 }));
  }
  if (opened !== t.port) {
    await openMidiPort(t.port);
    opened = t.port;
    shared = undefined;
  }
  return (shared ??= new DkSession(instrument(new MidiPortTransport()), { timeoutMs: 6000 }));
}

/** Drop the session (a failed port, a restarted device): the next request makes a new one. */
export function resetSession(): void {
  shared = undefined;
}

/** Give up on every request of the session that waits for the device (the Cancel button); each fails with the `cancelled` error. */
export function cancelPending(): void {
  shared?.cancelPending();
}
