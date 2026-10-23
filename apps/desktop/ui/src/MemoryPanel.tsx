// The Memory panel: what the resources a device limits are used for. A collapsible panel in the Card panel's look (card-panel.css), on the
// Emulator page next to the Card panel and on the Remote page. One bar per resource the device limits (memory-core.ts); each bar is split
// into one segment per loaded module, coloured by the module's type, then free space; hover a segment for the module and its amount.
// The panel reads CAPACITY, MODULES and the local library (memory-backend.ts) over the session it is given: when it is shown or opened, when a
// Transfer job ended or the emulator restarted (the same signal the Card panel reads), when the window gets the focus, and with its Refresh
// button. A read that fails is tried again a few times (a device that is still booting after a restart). Nothing pending or ticked is shown.
import { For, Show, createEffect, createSignal, onCleanup, onMount, type JSX } from 'solid-js';
import { panels } from '@web/data/faceplates';
import type { DkSession } from '@web/modules/dkupload-core';
import { cardRefresh } from './card-state';
import { deviceOfPort } from './remote-core';
import { freeTitle, segmentTitle, usedText, widthPct } from './memory-core';
import { readMemory, type MemoryReading } from './memory-backend';
import { focusReadDue } from './refresh-core';
import { failureLine } from './wait-core';
import './card-panel.css';
import './memory-panel.css';

const OPEN_KEY = 'digikit.memoryPanel';
/** A failed read is tried again after this long, this many times. */
const RETRY_MS = 3000;
const RETRIES = 3;
/** Below this window width the panel starts closed (two panels would leave the faceplate no room), unless the user chose. */
const WIDE_PX = 1100;
const readOpen = () => {
  try {
    const saved = localStorage.getItem(OPEN_KEY);
    if (saved) return saved !== 'closed';
  } catch {
    /* no choice kept */
  }
  return window.innerWidth >= WIDE_PX;
};
const LEGEND = [
  { kind: 'machine', text: 'Machine' },
  { kind: 'effect', text: 'Effect' },
  { kind: 'tool', text: 'Tool' },
  { kind: 'other', text: 'Other' },
] as const;

/**
 * `session`: the device to read (the emulator, or the selected target). `track`: read again when this changes (the selected target).
 * `where`: its name for an error line ("No reply from Emulator").
 */
export default function MemoryPanel(props: {
  visible: boolean;
  session: () => Promise<DkSession>;
  where: () => string;
  track?: () => unknown;
}): JSX.Element {
  const [open, setOpen] = createSignal(readOpen());
  const [reading, setReading] = createSignal<MemoryReading>();
  const [error, setError] = createSignal('');
  const [busy, setBusy] = createSignal(false);
  let seq = 0;
  let retry: number | undefined;

  const read = async (attempt = 0) => {
    window.clearTimeout(retry);
    const mine = ++seq;
    setBusy(true);
    try {
      const r = await readMemory(await props.session());
      if (mine !== seq) return;
      setReading(r);
      setError('');
    } catch (e) {
      if (mine !== seq) return;
      setError(failureLine(e, props.where()));
      if (attempt < RETRIES) retry = window.setTimeout(() => void read(attempt + 1), RETRY_MS);
    } finally {
      if (mine === seq) setBusy(false);
    }
  };
  // read when the panel is shown and opened, when the target changed, when a Transfer job ended or the emulator restarted
  createEffect(() => {
    cardRefresh();
    props.track?.();
    if (props.visible && open()) void read();
  });
  let lastFocusRead: number | undefined;
  const back = () => {
    if (document.visibilityState !== 'visible' || !props.visible || !open()) return;
    const now = Date.now();
    if (!focusReadDue(lastFocusRead, now)) return;
    lastFocusRead = now;
    void read();
  };
  onMount(() => {
    window.addEventListener('focus', back);
    document.addEventListener('visibilitychange', back);
  });
  onCleanup(() => {
    seq++; // an answer after the panel is gone is dropped
    window.clearTimeout(retry);
    window.removeEventListener('focus', back);
    document.removeEventListener('visibilitychange', back);
  });

  const toggle = () => {
    setOpen(!open());
    try {
      localStorage.setItem(OPEN_KEY, open() ? 'open' : 'closed');
    } catch {
      /* the choice lasts until the window closes */
    }
  };
  const device = () => {
    const d = deviceOfPort(reading()?.port ?? '');
    return d ? panels[d].name : '';
  };

  return (
    <aside class="card-panel memory-panel" classList={{ closed: !open() }} aria-label="Memory" hidden={!props.visible}>
      <button
        type="button"
        class="card-toggle"
        aria-expanded={open()}
        title={open() ? 'Collapse the Memory panel' : 'Show the Memory panel'}
        onClick={toggle}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path
            d={open() ? 'M6 3L11 8L6 13' : 'M10 3L5 8L10 13'}
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        <Show when={open()}>
          <strong>Memory</strong>
        </Show>
      </button>
      <Show when={open()}>
        <div class="card-body">
          <Show when={device()}>
            <h3 class="device">{device()}</h3>
          </Show>
          <Show when={reading()}>
            {(r) => (
              <>
                <For each={r().bars}>
                  {(bar) => (
                    <div class="mem-bar">
                      <div class="mem-head">
                        <span>{bar.label}</span>
                        <span class="used">{usedText(bar)}</span>
                      </div>
                      <div class="bar" role="img" aria-label={`${bar.label}: ${usedText(bar)}`}>
                        <For each={bar.segments}>
                          {(s) => (
                            <span
                              class={`seg ${s.kind}`}
                              style={{ width: `${widthPct(bar, s.amount)}%` }}
                              title={segmentTitle(bar, s)}
                            />
                          )}
                        </For>
                        <span class="seg free" title={freeTitle(bar)} />
                      </div>
                    </div>
                  )}
                </For>
                <Show when={r().loaded === 0}>
                  <p class="empty">No module is loaded.</p>
                </Show>
                <ul class="legend" aria-label="Module types">
                  <For each={LEGEND}>
                    {(l) => (
                      <li>
                        <span class={`swatch ${l.kind}`} />
                        {l.text}
                      </li>
                    )}
                  </For>
                </ul>
              </>
            )}
          </Show>
          <Show when={error()}>
            <p class="err" role="alert">
              {error()}
            </p>
          </Show>
          <div class="acts">
            <button type="button" class="act small" disabled={busy()} onClick={() => void read()}>
              {busy() ? 'Reading…' : 'Refresh'}
            </button>
          </div>
        </div>
      </Show>
    </aside>
  );
}
