// "Waiting for <target>…" with a Cancel, shown while a request to the device is open for longer than a moment. Cancel ends the wait
// of the session at once (`DkSession.cancelPending`); the request then fails as "Cancelled.". Used by the pages that talk to the target.
import { Show, createEffect, createSignal, onCleanup, type JSX } from 'solid-js';
import { cancelPending, target, targetLabel } from './target';
import { WAIT_SHOWN_MS, waitingLine } from './wait-core';

export default function WaitNotice(props: { active: boolean }): JSX.Element {
  const [shown, setShown] = createSignal(false);
  createEffect(() => {
    setShown(false);
    if (!props.active) return;
    const timer = window.setTimeout(() => setShown(true), WAIT_SHOWN_MS);
    onCleanup(() => window.clearTimeout(timer));
  });
  return (
    <Show when={shown()}>
      <p class="wait" role="status">
        <span>{waitingLine(targetLabel(target()))}</span>
        <button type="button" class="act small" onClick={cancelPending}>
          Cancel
        </button>
      </p>
    </Show>
  );
}
