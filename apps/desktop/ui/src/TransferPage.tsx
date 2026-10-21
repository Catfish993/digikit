// The Transfer page (desktop only): what is on the emulated device, and the repo's run-time modules that could be (tools/device/dktransfer.py
// through the native commands). Install builds what changed, uploads and verifies each file on the card; that is all. The device's selection
// is the device's own: the user selects the module in MODULES on the device, then applies and restarts there. Restart only restarts the
// device and reports what loaded. "Add .dkm" and the drop zone put a module from outside the repo on the card.
import { For, Show, createSignal, onMount, type JSX } from 'solid-js';
import { hexSha256, storePath } from '@web/modules/dkupload-core';
import { errorLine, formatBytes } from '@web/modules/backend';
import { emulatorModules } from './modules-backend';
import { transferBackend } from './transfer-backend';
import {
  applyEvent,
  applyVerified,
  available,
  DONE_ON_CARD,
  eventText,
  installModules,
  onDevice,
  progressFraction,
  rowKey,
  startProgress,
  stateText,
  warningText,
  type TransferEvent,
  type TransferRow,
} from './transfer-core';
import {
  lastView,
  log,
  progress,
  setLastView,
  setLog,
  setProgress,
  setShowTests,
  setTicks,
  setVerified,
  showTests,
  ticks,
  verified,
} from './transfer-state';
import './transfer-page.css';

export default function TransferPage(): JSX.Element {
  const [busy, setBusy] = createSignal('');
  const [error, setError] = createSignal('');
  const [over, setOver] = createSignal(false);
  let input: HTMLInputElement | undefined;

  /** Read the list again. */
  const refresh = async () => {
    setLastView(await transferBackend.list());
  };
  /** Run `f` as the page's one busy action; an error shows, the list is read again either way (a failed refresh does not hide it). */
  const run = async (what: string, f: () => Promise<void>) => {
    if (busy()) return;
    setBusy(what);
    setError('');
    try {
      await f();
    } catch (e) {
      setError(errorLine(e));
    } finally {
      try {
        await refresh();
      } catch (e) {
        if (!error()) setError(errorLine(e));
      }
      setBusy('');
    }
  };
  onMount(() => void run('Reading', async () => {}));

  const onEvent = (e: TransferEvent) => {
    const p = progress();
    if (p) setProgress(applyEvent(p, e));
    setVerified(applyVerified(verified(), e));
    setLog((l) => [...l, eventText(e)]);
  };
  const install = () =>
    run('Installing', async () => {
      const v = lastView();
      if (!v) return;
      const modules = installModules(v, ticks());
      setProgress(startProgress(modules, false));
      setLog([]);
      try {
        await transferBackend.install(modules, onEvent);
      } catch (e) {
        const p = progress();
        if (p) setProgress({ ...p, message: errorLine(e) });
        throw e;
      }
      const p = progress();
      if (p) setProgress({ ...p, message: DONE_ON_CARD });
      setTicks(new Set<string>());
    });
  const restart = () =>
    run('Restarting', async () => {
      setProgress(startProgress([], true));
      setLog([]);
      try {
        await transferBackend.restart(onEvent);
      } catch (e) {
        const p = progress();
        if (p) setProgress({ ...p, message: errorLine(e) });
        throw e;
      }
      setVerified({});
    });

  const addFiles = (files: File[]) =>
    run('Uploading', async () => {
      const picked = files.filter((f) => /\.dkm$/i.test(f.name));
      if (!picked.length) {
        setError('Only .dkm files can be added.');
        return;
      }
      const onCard = new Map((await emulatorModules.list()).map((e) => [e.path, e.sha256]));
      const lines: string[] = [];
      for (const file of picked) {
        const data = new Uint8Array(await file.arrayBuffer());
        const path = storePath(file.name);
        if (onCard.get(path) === (await hexSha256(data))) {
          lines.push(`${path}: already on the card`);
          continue;
        }
        await emulatorModules.put(path, data, () => true);
        lines.push(`Added ${path}`);
      }
      setLog(lines);
    });
  const remove = (r: TransferRow) =>
    run('Deleting', async () => {
      await emulatorModules.remove(r.path);
    });
  const toggle = (r: TransferRow) => {
    const k = rowKey(r);
    const next = new Set(ticks());
    if (next.has(k)) next.delete(k);
    else next.add(k);
    setTicks(next);
  };

  const label = (r: TransferRow) => r.label ?? r.name;
  const ticked = () => {
    const v = lastView();
    return v ? installModules(v, ticks()).length : 0;
  };
  const verifiedMark = (r: TransferRow) => verified()[r.path];
  const pct = () => Math.round(100 * progressFraction(progress()!));

  return (
    <section
      class="transfer-page"
      classList={{ over: over() }}
      aria-label="Transfer"
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        void addFiles(Array.from(e.dataTransfer?.files ?? []));
      }}
    >
      <header class="head">
        <h2>Transfer</h2>
        <Show when={lastView()}>
          {(v) => (
            <span class="meta">
              Emulator · {v().device} · runtime {v().runtime} · {formatBytes(v().free_bytes)} free
            </span>
          )}
        </Show>
        <span class="sp" />
        <Show when={busy() === 'Reading' && lastView()}>
          <span class="meta">refreshing…</span>
        </Show>
        <input
          ref={input}
          type="file"
          accept=".dkm"
          multiple
          hidden
          onChange={(e) => {
            const files = Array.from(e.currentTarget.files ?? []);
            e.currentTarget.value = '';
            void addFiles(files);
          }}
        />
        <button type="button" class="act small" disabled={!!busy()} onClick={() => input?.click()}>
          Add .dkm…
        </button>
        <span class="drop">or drop files</span>
        <button type="button" class="act" disabled={!!busy()} onClick={() => void run('Reading', async () => {})}>
          Refresh
        </button>
        <button
          type="button"
          class="act"
          disabled={!!busy() || !lastView()}
          title="RESTART the emulated device (it loads what its MODULES menu has selected)"
          onClick={() => void restart()}
        >
          {busy() === 'Restarting' ? 'Restarting…' : 'Restart'}
        </button>
      </header>

      <Show when={error()}>
        <p class="err" role="alert">
          {error()}
        </p>
      </Show>

      <Show when={lastView()} fallback={<p class="empty">{busy() ? `${busy()}…` : 'No module list yet.'}</p>}>
        {(v) => (
          <div class="cols">
            <section class="col" aria-label="On device">
              <h3>On device</h3>
              <Show when={onDevice(v()).length} fallback={<p class="empty">The card holds no modules.</p>}>
                <ul class="rows">
                  <For each={onDevice(v())}>
                    {(r) => (
                      <li>
                        <span class="name" title={r.path}>
                          {label(r)}
                          <Show when={!r.id}>
                            <em> not from this repo</em>
                          </Show>
                        </span>
                        <span class="dim">{r.version ?? ''}</span>
                        <span class="dim">{formatBytes(r.file_size)}</span>
                        <span class="state">{stateText(r)}</span>
                        <Show when={verifiedMark(r)}>
                          <span
                            class="check"
                            classList={{ ok: verifiedMark(r) === 'ok', bad: verifiedMark(r) === 'failed' }}
                          >
                            {verifiedMark(r) === 'ok' ? 'verified' : 'verify failed'}
                          </span>
                        </Show>
                        <Show when={!r.id}>
                          <button
                            type="button"
                            class="mini"
                            disabled={!!busy()}
                            aria-label={`Delete ${r.path}`}
                            onClick={() => void remove(r)}
                          >
                            Delete
                          </button>
                        </Show>
                      </li>
                    )}
                  </For>
                </ul>
              </Show>
            </section>

            <section class="col" aria-label="Available">
              <div class="colhead">
                <h3>Available</h3>
                <label class="tests">
                  <input
                    type="checkbox"
                    checked={showTests()}
                    onChange={(e) => setShowTests(e.currentTarget.checked)}
                  />{' '}
                  show test modules
                </label>
                <span class="sp" />
                <button
                  type="button"
                  class="act primary"
                  disabled={!!busy() || ticked() === 0}
                  title={
                    ticked()
                      ? 'Build, upload and verify the ticked modules on the card; select them in MODULES on the device'
                      : 'Tick a module to install'
                  }
                  onClick={() => void install()}
                >
                  {busy() === 'Installing' ? 'Installing…' : ticked() ? `Install ${ticked()}` : 'Install'}
                </button>
              </div>
              <For each={available(v(), showTests())}>
                {(g) => (
                  <div class="group">
                    <h4>{g.group}</h4>
                    <Show
                      when={g.rows.length}
                      fallback={<p class="empty">{g.group === 'Effects' ? 'No run-time effects yet.' : 'None.'}</p>}
                    >
                      <ul class="rows">
                        <For each={g.rows}>
                          {(r) => (
                            <li classList={{ warn: !!warningText(r) }}>
                              <input
                                type="checkbox"
                                aria-label={`Load ${label(r)}`}
                                checked={ticks().has(rowKey(r))}
                                disabled={!!busy()}
                                onChange={() => toggle(r)}
                              />
                              <span class="name" title={r.id ?? r.path}>
                                {label(r)}
                                <Show when={r.test}>
                                  <em> test</em>
                                </Show>
                              </span>
                              <span class="dim">{r.version ?? ''}</span>
                              <span
                                class="dim"
                                title={r.arena !== undefined ? `arena ${r.arena} B, DSP ${r.dsp} B` : ''}
                              >
                                {formatBytes(r.file_size)}
                              </span>
                              <span class="state">{stateText(r)}</span>
                              <Show when={warningText(r)}>
                                <span class="fit">{warningText(r)}</span>
                              </Show>
                            </li>
                          )}
                        </For>
                      </ul>
                    </Show>
                  </div>
                )}
              </For>
            </section>
          </div>
        )}
      </Show>

      <Show when={progress()}>
        {(p) => (
          <footer class="bar" role="status" aria-label="Transfer progress">
            <div class="segs">
              <For each={p().modules}>
                {(m) => (
                  <div class="seg" data-state={m.state} title={`${m.id}: ${m.state}`}>
                    <span>{m.id.split('/').pop()}</span>
                  </div>
                )}
              </For>
              <Show when={p().restart !== 'none'}>
                <div class="seg" data-state={p().restart} title="restart">
                  <span>restart</span>
                </div>
              </Show>
            </div>
            <span class="msg">
              {pct()}% · {p().message}
            </span>
          </footer>
        )}
      </Show>
      <Show when={log().length && !progress()}>
        <pre class="log">{log().join('\n')}</pre>
      </Show>
    </section>
  );
}
