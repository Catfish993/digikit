// One module in the gallery: icon, name, one-line summary, which devices or whether it fits the chosen
// firmware, and an add action. Rendered statically on the home page and live on the patch page.
import { For, Show } from 'solid-js';
import { CATEGORY_LABEL, DEVICE_SHORT, type ModuleInfo } from '../data/modules';
import './module-card.css';

export type CardState = 'browse' | 'ok' | 'no' | 'conflict';

export interface ModuleCardProps {
  module: ModuleInfo;
  /** browse: no firmware chosen yet; ok: fits it; no: does not fit; conflict: clashes with a chosen module. */
  state?: CardState;
  /** The line under the summary for ok, no and conflict ("Works with Digitone II 1.12"). */
  note?: string;
  /** Framework modules this one adds, shown as "+N framework modules". */
  needs?: number;
  selected?: boolean;
  onToggle?: () => void;
  /** A link instead of the add button (the home page links to the patch page). */
  href?: string;
}

function Glyph(props: { d: string }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path
        d={props.d}
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
  );
}
const CHECK = 'M3 8.5L6.5 12L13 4.5';
const PLUS = 'M8 3V13M3 8H13';
const NO = 'M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2ZM4 12L12 4';
const WARN = 'M8 2L14.5 13.5H1.5ZM8 6.5V9.5M8 11.6V11.7';
const INFO = 'M8 1.8a6.2 6.2 0 1 0 0 12.4A6.2 6.2 0 0 0 8 1.8ZM8 7.5V11M8 5V5.1';

export default function ModuleCard(props: ModuleCardProps) {
  const state = () => props.state ?? 'browse';
  const blocked = () => state() === 'no' || state() === 'conflict';
  const on = () => !!props.selected && !blocked();
  return (
    <article class="card" classList={{ on: on(), off: blocked() }}>
      <div class="card-top">
        {/* oxlint-disable-next-line solid/no-innerhtml -- repository SVGs, checked by apps/web/tests/icons.test.mjs */}
        <span class="tile" innerHTML={props.module.icon} />
        <span class="eyebrow">{CATEGORY_LABEL[props.module.category]}</span>
      </div>
      <div class="card-text">
        <h3>{props.module.name}</h3>
        <p class="sum">{props.module.summary}</p>
      </div>
      <div class="grow" />
      <div class="facts-row">
        <Show when={state() === 'browse'}>
          <div class="devs">
            <For each={props.module.devices}>{(d) => <span class="dev">{DEVICE_SHORT[d]}</span>}</For>
          </div>
        </Show>
        <Show when={state() === 'ok'}>
          <span class="fit ok">
            <Glyph d={CHECK} />
            {props.note}
          </span>
        </Show>
        <Show when={state() === 'no'}>
          <span class="fit no">
            <Glyph d={NO} />
            {props.note}
          </span>
        </Show>
        <Show when={state() === 'conflict'}>
          <span class="fit warn">
            <Glyph d={WARN} />
            {props.note}
          </span>
        </Show>
        <Show when={(props.needs ?? 0) > 0 && !blocked()}>
          <span class="fit info" title="Framework modules are added automatically">
            <Glyph d={INFO} />+{props.needs} framework module{props.needs === 1 ? '' : 's'}
          </span>
        </Show>
      </div>
      <div class="card-foot">
        <span class="id" title={`${props.module.id} v${props.module.version}`}>
          {props.module.id}
        </span>
        <Show
          when={props.href}
          fallback={
            <Show
              when={!blocked()}
              fallback={
                <button type="button" class="add" disabled>
                  Unavailable
                </button>
              }
            >
              <button type="button" class="add" aria-pressed={on()} onClick={() => props.onToggle?.()}>
                <Glyph d={on() ? CHECK : PLUS} />
                {on() ? 'Added' : 'Add'}
              </button>
            </Show>
          }
        >
          <a class="add" href={props.href}>
            <Glyph d={PLUS} />
            Add
          </a>
        </Show>
      </div>
    </article>
  );
}
