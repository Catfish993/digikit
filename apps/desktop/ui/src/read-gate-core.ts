// When a panel reads its device, and never twice at once (pure; apps/desktop/ui/tests/read-gate.test.ts). The panels of the Emulator and Remote pages
// share one session whose requests take turns, so a second read started while one runs would only queue up behind it and, when the device is
// slow, pile up: the gate runs one read at a time. A trigger that arrives meanwhile (a Transfer job ended, the window got the focus) is kept
// as ONE read to run after it, however many arrive. A read that fails is tried again later with a growing gap, a few times, then the panel
// waits for the next trigger.

export interface ReadGate {
  /** Something asked for a read: start it now, or after the one that runs (the triggers meanwhile make one). */
  request(): void;
  /** The panel is hidden or closed: drop the read that was kept and the retry that was scheduled. A read that runs is left to end. */
  stop(): void;
  /** The panel is gone: like `stop`, and the answer of a read that runs is dropped (`current()` turns false for it). */
  dispose(): void;
}

export interface ReadGateOptions {
  /** One read. Resolve true when it worked; false (or throw) when it failed. `current()` is false once the panel is gone: leave the screen alone. */
  run(current: () => boolean): Promise<boolean>;
  /** The gaps before the 1st, 2nd, ... retry of a failed read. */
  retryMs?: readonly number[];
  setTimer?(fn: () => void, ms: number): unknown;
  clearTimer?(id: unknown): void;
}

export const RETRY_MS: readonly number[] = [3000, 9000, 27000];

export function createReadGate(o: ReadGateOptions): ReadGate {
  const retryMs = o.retryMs ?? RETRY_MS;
  const setTimer = o.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = o.clearTimer ?? ((id) => clearTimeout(id as ReturnType<typeof setTimeout>));
  let running = false;
  let again = false;
  let attempt = 0;
  let timer: unknown;
  let gone = false;
  let stopped = 0; // bumped by stop(): a read that began before it is no longer wanted

  const clear = () => {
    if (timer !== undefined) clearTimer(timer);
    timer = undefined;
  };
  const start = async () => {
    running = true;
    const mine = stopped;
    let ok = false;
    try {
      ok = await o.run(() => !gone && mine === stopped);
    } catch {
      ok = false;
    }
    running = false;
    if (gone) return;
    if (mine !== stopped) {
      // stopped while it ran: its answer is not wanted, but a read asked for since (the panel was shown again) still is
      if (again) {
        again = false;
        void start();
      }
      return;
    }
    if (ok) {
      attempt = 0;
      if (again) {
        again = false;
        void start();
      }
      return;
    }
    again = false; // a kept trigger does not make a failed read start at once: the retry below is the next one
    if (attempt < retryMs.length)
      timer = setTimer(() => {
        timer = undefined;
        void start();
      }, retryMs[attempt++]);
  };
  return {
    request() {
      if (gone) return;
      if (running) {
        again = true;
        return;
      }
      clear();
      attempt = 0;
      void start();
    },
    stop() {
      stopped++;
      again = false;
      attempt = 0;
      clear();
    },
    dispose() {
      gone = true;
      this.stop();
    },
  };
}
