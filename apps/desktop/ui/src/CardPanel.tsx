// The Card panel of the Emulator page: the host folder the emulated card's module store is mirrored to (`plus_drive/<device>/`, kept up to
// date by the emulator itself about 2 s after the guest's last card write; one way, card to folder). It shows the folder, the module files
// with their sizes, the time of the last sync, "Open folder" and "Sync now". The emulator does the work: this panel only asks
// (`card.folder`, `card.sync` through `emu_call`).
import { invoke } from '@tauri-apps/api/core';
import { For, Show, createEffect, createSignal, onCleanup, type JSX } from 'solid-js';
import { errorLine, formatBytes } from '@web/modules/backend';
import { fileRows, shortFolder, statusText, type CardFolder } from './card-core';
import { cardRefresh } from './card-state';
import './card-panel.css';

const POLL_MS = 2000;
const OPEN_KEY = 'digikit.cardPanel';
const readOpen = () => {
  try {
    return localStorage.getItem(OPEN_KEY) !== 'closed';
  } catch {
    return true;
  }
};
const call = <T,>(method: string) => invoke<T>('emu_call', { method, params: {}, session: null });

export default function CardPanel(props: { visible: boolean }): JSX.Element {
  const [open, setOpen] = createSignal(readOpen());
  const [card, setCard] = createSignal<CardFolder>();
  const [error, setError] = createSignal('');
  const [busy, setBusy] = createSignal(false);
  const [now, setNow] = createSignal(Date.now());

  const refresh = async () => {
    try {
      setCard(await call<CardFolder>('card.folder'));
      setError('');
    } catch (e) {
      setError(errorLine(e));
    }
    setNow(Date.now());
  };
  // read when the panel is shown and opened, when a Transfer job ended, and on a light poll while it is visible
  createEffect(() => {
    cardRefresh();
    if (props.visible && open()) void refresh();
  });
  const timer = window.setInterval(() => {
    if (props.visible && open() && !busy()) void refresh();
  }, POLL_MS);
  onCleanup(() => window.clearInterval(timer));

  const toggle = () => {
    setOpen(!open());
    try {
      localStorage.setItem(OPEN_KEY, open() ? 'open' : 'closed');
    } catch {
      /* the choice lasts until the window closes */
    }
  };
  const sync = async () => {
    setBusy(true);
    try {
      setCard(await call<CardFolder>('card.sync'));
      setError('');
    } catch (e) {
      setError(errorLine(e));
    } finally {
      setBusy(false);
      setNow(Date.now());
    }
  };
  const openFolder = async () => {
    try {
      await invoke('card_open_folder');
    } catch (e) {
      setError(errorLine(e));
    }
  };

  return (
    <aside class="card-panel" classList={{ closed: !open() }} aria-label="Card" hidden={!props.visible}>
      <button
        type="button"
        class="card-toggle"
        aria-expanded={open()}
        title={open() ? 'Collapse the Card panel' : 'Show the Card panel'}
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
          <strong>Card</strong>
        </Show>
      </button>
      <Show when={open()}>
        <div class="card-body">
          <p class="folder" title={card()?.folder ?? ''}>
            {card()?.folder ? shortFolder(card()!.folder) : 'No folder'}
          </p>
          <div class="acts">
            <button type="button" class="act small" disabled={!card()?.folder} onClick={() => void openFolder()}>
              Open folder
            </button>
            <button
              type="button"
              class="act small"
              disabled={!card()?.continuous || busy()}
              onClick={() => void sync()}
            >
              {busy() ? 'Syncing…' : 'Sync now'}
            </button>
          </div>
          <Show
            when={card() && fileRows(card()!).length}
            fallback={<p class="empty">{card()?.continuous ? 'No module files in the folder.' : ''}</p>}
          >
            <ul class="files">
              <For each={fileRows(card()!)}>
                {(f) => (
                  <li title={`${f.path}\nsha256 ${f.sha256}`}>
                    <span class="name">{f.path.replace(/^modules\//, '')}</span>
                    <span class="size">{formatBytes(f.bytes)}</span>
                  </li>
                )}
              </For>
            </ul>
          </Show>
          <Show when={card()}>
            <p class="status" classList={{ bad: !!card()?.error }}>
              {statusText(card()!, now())}
            </p>
          </Show>
          <Show when={error()}>
            <p class="err" role="alert">
              {error()}
            </p>
          </Show>
        </div>
      </Show>
    </aside>
  );
}
