// The workbench target: the emulator, or a MIDI port with a real device on it. Every page that talks the digikit protocol (Transfer, Remote, the
// Memory panel) uses the one selected here, through the one session of that target. The protocol client is the same in both cases (`DkSession`); only the transport differs.
import { createSignal } from 'solid-js';
import { DkSession } from '@digi/dk-session';
import { instrument } from './activity';
import { EmuUsbTransport, emulatorMode } from './desktop-transport';
import { closeMidiPort, MidiPortTransport, openMidiPort } from './midi-transport';
import { createSessions, targetKey } from './session-core';

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

/** One session per target (session-core.ts): the emulator's, and the open MIDI port's. Every page and panel uses these, never a session of its own. */
const sessions = createSessions<DkSession>(
  (key) =>
    new DkSession(instrument(key === 'emulator' ? new EmuUsbTransport() : new MidiPortTransport()), {
      timeoutMs: 6000,
    }),
);
let opened: string | undefined;

/** Change the target: the next request uses the session of the new one (and opens the port). A MIDI port that cannot be opened leaves the old target. */
export async function selectTarget(next: Target): Promise<void> {
  if (sameTarget(next, target())) return;
  if (next.kind === 'midi') await openMidiPort(next.port);
  else if (opened) await closeMidiPort();
  cancelPending();
  if (opened) sessions.drop(targetKey({ kind: 'midi', port: opened }));
  opened = next.kind === 'midi' ? next.port : undefined;
  setTargetSignal(next);
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // the choice is not kept
  }
}

async function sessionOf(t: Target): Promise<DkSession> {
  if (t.kind === 'emulator') {
    if ((await emulatorMode()) === 'paused') throw new Error(PAUSED);
  } else if (opened !== t.port) {
    await openMidiPort(t.port);
    if (opened) sessions.drop(targetKey({ kind: 'midi', port: opened }));
    opened = t.port;
  }
  return sessions.get(targetKey(t));
}

/** The one session of the selected target: its requests take turns on the one reply stream. A saved MIDI target is opened at its first use. */
export const session = (): Promise<DkSession> => sessionOf(target());

/**
 * The emulator's session whatever the selected target is (the Emulator page's Memory panel is about the emulator). It is THE emulator session,
 * the one `session()` returns while the emulator is the target, so every reader of the emulator's USB takes its turn in one queue.
 */
export const emulatorSession = (): Promise<DkSession> => sessionOf({ kind: 'emulator' });

/** Drop the session of a failed MIDI port (the next request opens it again). The emulator's session stays: it has no port to reopen. */
export function resetSession(): void {
  const t = target();
  if (t.kind === 'midi') sessions.drop(targetKey(t));
}

/** Give up on every request of the selected target's session that waits for the device (the Cancel button); each fails with the `cancelled` error. */
export function cancelPending(): void {
  sessions.cancel(targetKey(target()));
}
