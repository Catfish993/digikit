// The Transfer page: two tabs on one target. Modules is the library and the card's module store; Samples is the device's sample folders.
import { For, Show, type JSX } from 'solid-js';
import SamplesPage from './SamplesPage';
import { TABS } from './samples-core';
import TransferPage from './TransferPage';
import { setTab, tab } from './transfer-state';
import './transfer-page.css';

export default function TransferTabs(): JSX.Element {
  return (
    <div class="transfer-tabs">
      <nav role="tablist" aria-label="Transfer">
        <For each={TABS}>
          {(t) => (
            <button
              type="button"
              role="tab"
              aria-selected={tab() === t.id}
              classList={{ on: tab() === t.id }}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          )}
        </For>
      </nav>
      <Show when={tab() === 'modules'} fallback={<SamplesPage />}>
        <TransferPage />
      </Show>
    </div>
  );
}
