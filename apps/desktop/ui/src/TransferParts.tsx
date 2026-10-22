// The parts of the Transfer page that Modules mode and Samples mode share: the two columns with their headers, the list row with its
// progress bar or verified/failed chip, the breadcrumb, and the two-click Delete button.
import { For, Show, createSignal, type JSX } from 'solid-js';
import TargetSelect from './TargetSelect';
import { target, targetLabel } from './target';
import { deleteLabel, infoLine } from './transfer-core';
import { deviceInfo } from './transfer-state';

/** What the page gives each mode: the one busy action, and the error line. */
export interface PageCtx {
  busy: () => string;
  /** Run `f` as the page's one busy action; an error shows, and both sides are read again either way. */
  run: (what: string, f: () => Promise<void>) => Promise<void>;
  setError: (m: string) => void;
  /** Changes when Refresh was pressed (the target selector reads the MIDI ports again). */
  reload: () => number;
}

/** What the page shows beside a file while it is sent: a bar, or a chip; `message` is the tooltip of the chip. */
export interface ChipStatus {
  kind: 'waiting' | 'uploading' | 'verifying' | 'ok' | 'failed';
  sent?: number;
  bytes?: number;
  message?: string;
}

export function Chip(props: { status: ChipStatus }): JSX.Element {
  const pct = () => (props.status.bytes ? Math.round((100 * (props.status.sent ?? 0)) / props.status.bytes) : 0);
  return (
    <>
      <Show when={props.status.kind === 'waiting'}>
        <span class="check wait">waiting</span>
      </Show>
      <Show when={props.status.kind === 'verifying'}>
        <span class="check wait">verifying…</span>
      </Show>
      <Show when={props.status.kind === 'ok'}>
        <span class="check ok" title={props.status.message}>
          verified
        </span>
      </Show>
      <Show when={props.status.kind === 'failed'}>
        <span class="check bad" title={props.status.message}>
          failed
        </span>
      </Show>
      <Show when={props.status.kind === 'uploading'}>
        <span class="up">
          <progress max={props.status.bytes} value={props.status.sent} />
          <span class="dim">{pct()}%</span>
        </span>
      </Show>
    </>
  );
}

export interface ListRowProps {
  check?: { label: string; checked: boolean; disabled: boolean; onChange: () => void };
  name: string;
  /** The tooltip of the name. */
  title?: string;
  /** A folder: the name is a link that opens it. */
  open?: { disabled: boolean; onOpen: () => void };
  /** A short note after the name (`test`, `added`). */
  tag?: string;
  /** The version (modules) or the size (samples); `extra` is a second dim column. */
  detail?: string;
  extra?: string;
  extraTitle?: string;
  state?: string;
  warn?: string;
  status?: ChipStatus;
}

/** One row of a list in either column: tick, name, version or size, state text, then the bar or chip of a transfer. */
export function ListRow(props: ListRowProps): JSX.Element {
  return (
    <li classList={{ warn: !!props.warn }}>
      <Show when={props.check}>
        {(c) => (
          <input
            type="checkbox"
            aria-label={c().label}
            checked={c().checked}
            disabled={c().disabled}
            onChange={c().onChange}
          />
        )}
      </Show>
      <Show
        when={props.open}
        fallback={
          <span class="name" title={props.title}>
            {props.name}
            <Show when={props.tag}>
              <em> {props.tag}</em>
            </Show>
          </span>
        }
      >
        {(o) => (
          <button type="button" class="name link" disabled={o().disabled} title={props.title} onClick={o().onOpen}>
            {props.name}/
          </button>
        )}
      </Show>
      <span class="dim">{props.detail ?? ''}</span>
      <Show when={props.extra !== undefined}>
        <span class="dim" title={props.extraTitle}>
          {props.extra}
        </span>
      </Show>
      <Show when={props.state && props.status?.kind !== 'uploading'}>
        <span class="state">{props.state}</span>
      </Show>
      <Show when={props.warn}>
        <span class="fit">{props.warn}</span>
      </Show>
      <Show when={props.status}>{(st) => <Chip status={st()} />}</Show>
    </li>
  );
}

/** A folder path as buttons: one per level; the open level is the current location. */
export function Crumbs(props: {
  items: { name: string; path: string }[];
  current: string;
  disabled: boolean;
  label: string;
  onOpen: (path: string) => void;
}): JSX.Element {
  return (
    <nav class="crumbs" aria-label={props.label}>
      <For each={props.items}>
        {(c, i) => (
          <>
            <Show when={i() > 0}>
              <span class="dim">/</span>
            </Show>
            <button
              type="button"
              class="mini"
              aria-current={c.path === props.current ? 'location' : undefined}
              disabled={props.disabled || c.path === props.current}
              onClick={() => props.onOpen(c.path)}
            >
              {c.name}
            </button>
          </>
        )}
      </For>
    </nav>
  );
}

/** The two-click confirm of Delete: the first click arms it for about 3 s, the second (within that time) runs it. No modal. */
export function createConfirm() {
  const [confirming, setConfirming] = createSignal(false);
  let timer: number | undefined;
  const stop = () => {
    window.clearTimeout(timer);
    setConfirming(false);
  };
  const click = (go: () => void) => {
    if (!confirming()) {
      setConfirming(true);
      timer = window.setTimeout(() => setConfirming(false), 3000);
      return;
    }
    stop();
    go();
  };
  return { confirming, stop, click };
}

export function DeleteButton(props: {
  n: number;
  busy: string;
  confirming: boolean;
  title: string;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      class="act"
      classList={{ danger: props.confirming }}
      disabled={!!props.busy || props.n === 0}
      title={props.n ? props.title : 'Tick the rows to delete'}
      onClick={props.onClick}
    >
      {props.busy === 'Deleting' ? 'Deleting…' : deleteLabel(props.n, props.confirming)}
    </button>
  );
}

/** The left column: what is on the computer. `actions` are its controls (they sit in its header); a drop on it adds files. */
export function LocalPane(props: {
  actions: JSX.Element;
  onDrop: (files: File[]) => void;
  children: JSX.Element;
}): JSX.Element {
  const [over, setOver] = createSignal(false);
  return (
    <section
      class="col"
      classList={{ over: over() }}
      aria-label="On your computer"
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        props.onDrop(Array.from(e.dataTransfer?.files ?? []));
      }}
    >
      <div class="colhead">
        <h3>On your computer</h3>
        <span class="sp" />
        {props.actions}
      </div>
      {props.children}
    </section>
  );
}

/** The right column: the target, the device and its list. Everything about the target is here: the selector, the info line and the buttons. */
export function DevicePane(props: { ctx: PageCtx; actions: JSX.Element; children: JSX.Element }): JSX.Element {
  return (
    <section class="col" aria-label="On device">
      <div class="colhead">
        <h3>On device</h3>
        <span class="sp" />
        {props.actions}
      </div>
      <div class="devhead">
        <TargetSelect disabled={!!props.ctx.busy()} onError={props.ctx.setError} reload={props.ctx.reload} />
        <Show when={deviceInfo()}>
          {(d) => <span class="meta">{infoLine(targetLabel(target()), d().name, d().runtime, d().free)}</span>}
        </Show>
      </div>
      {props.children}
    </section>
  );
}
