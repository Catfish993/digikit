// The parts of the Transfer page that Modules mode and Samples mode share: the two columns with their headers, the list row with its
// progress bar or verified/failed chip, the breadcrumb, and the two-click Delete button.
import { For, Show, createEffect, createSignal, type JSX } from 'solid-js';
import TargetSelect from './TargetSelect';
import { target, targetLabel } from './target';
import { fmtRate } from './rate-core';
import { selectState, toggleAll } from './select-core';
import { deleteLabel, infoLine, type RowTag } from './transfer-core';
import { deviceInfo, liveRate } from './transfer-state';

/** What the page gives each mode: the one busy action, and the error line. */
export interface PageCtx {
  busy: () => string;
  /** Run `f` as the page's one busy action; an error shows, and both sides are read again either way. */
  run: (what: string, f: () => Promise<void>) => Promise<void>;
  setError: (m: string) => void;
  /** Changes when the page reads by itself (the target selector reads the MIDI ports again). */
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
  const rate = () => {
    const r = liveRate();
    return r ? fmtRate(r) : '';
  };
  return (
    <>
      <Show when={props.status.kind === 'waiting'}>
        <span class="check idle">waiting</span>
      </Show>
      <Show when={props.status.kind === 'verifying'}>
        <span class="check idle">verifying…</span>
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
        <span class="up" title={rate() ? `${pct()}% · ${rate()}` : `${pct()}%`}>
          <progress max={props.status.bytes} value={props.status.sent} />
          <span class="pct">{pct()}%</span>
          <span class="rate">{rate()}</span>
        </span>
      </Show>
    </>
  );
}

/** The small label after a name (`test`, `on device`, `WAV→`). */
function Tag(props: { tag: RowTag }): JSX.Element {
  return (
    <span class="rtag" classList={{ [props.tag.tone]: true }} title={props.tag.title}>
      {props.tag.text}
    </span>
  );
}

export interface ListRowProps {
  check?: { label: string; checked: boolean; disabled: boolean; onChange: () => void };
  name: string;
  /** The tooltip of the name. */
  title?: string;
  /** A folder: the name is a link that opens it. */
  open?: { disabled: boolean; onOpen: () => void };
  /** Small labels after the name; only what says something (a usual state has none). */
  tags?: RowTag[];
  /** The version (modules), a dim column before the size. */
  version?: string;
  /** The size, right-aligned. */
  size?: string;
  /** The tooltip of the size. */
  sizeTitle?: string;
  status?: ChipStatus;
}

/** One row of a list in either column, always one line: tick, name with its tags, version, the bar or chip of a transfer, size. */
export function ListRow(props: ListRowProps): JSX.Element {
  return (
    <li>
      <Show when={props.check} fallback={<span class="tick" />}>
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
      <span class="name">
        <Show
          when={props.open}
          fallback={
            <span class="nm" title={props.title}>
              {props.name}
            </span>
          }
        >
          {(o) => (
            <button type="button" class="nm link" disabled={o().disabled} title={props.title} onClick={o().onOpen}>
              {props.name}/
            </button>
          )}
        </Show>
        <For each={props.tags ?? []}>{(t) => <Tag tag={t} />}</For>
      </span>
      <Show when={props.version}>
        <span class="vers">{props.version}</span>
      </Show>
      <Show when={props.status}>{(st) => <Chip status={st()} />}</Show>
      <span class="size" title={props.sizeTitle}>
        {props.size ?? ''}
      </span>
    </li>
  );
}

/** The select-all checkbox of a pane header: none, some (the dash) or all of `keys`, the visible rows that can be ticked. */
export function SelectAll(props: {
  label: string;
  keys: string[];
  ticked: Set<string>;
  disabled: boolean;
  onChange: (next: Set<string>) => void;
}): JSX.Element {
  let box: HTMLInputElement | undefined;
  const state = () => selectState(props.keys, props.ticked);
  createEffect(() => {
    if (box) box.indeterminate = state() === 'some';
  });
  return (
    <input
      ref={box}
      type="checkbox"
      class="all"
      aria-label={props.label}
      title={state() === 'all' ? 'Clear the selection' : 'Select all'}
      checked={state() === 'all'}
      disabled={props.disabled || props.keys.length === 0}
      onChange={() => props.onChange(toggleAll(props.keys, props.ticked))}
    />
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
  /** The select-all checkbox, before the heading. */
  selectAll: JSX.Element;
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
        <div class="hl">
          {props.selectAll}
          <h3>On your computer</h3>
        </div>
        <div class="acts">{props.actions}</div>
      </div>
      {props.children}
    </section>
  );
}

/** A job that writes to the device (the target cannot change under it); reading and waiting can be left by choosing another target. */
export function writing(busy: string): boolean {
  return busy !== '' && busy !== 'Reading';
}

/** The right column: the target, the device and its list. Everything about the target is here: the selector, the info line and the buttons. */
export function DevicePane(props: {
  ctx: PageCtx;
  selectAll: JSX.Element;
  actions: JSX.Element;
  children: JSX.Element;
}): JSX.Element {
  return (
    <section class="col" aria-label="On device">
      <div class="colhead">
        <div class="hl">
          {props.selectAll}
          <h3>On device</h3>
        </div>
        <div class="acts">{props.actions}</div>
      </div>
      <div class="devhead">
        <TargetSelect disabled={writing(props.ctx.busy())} onError={props.ctx.setError} reload={props.ctx.reload} />
        <Show when={deviceInfo()}>
          {(d) => <span class="meta">{infoLine(targetLabel(target()), d().name, d().runtime, d().free)}</span>}
        </Show>
      </div>
      {props.children}
    </section>
  );
}
