// The Transfer page (desktop only): what is on the emulated device, and the built modules of the library folder that could be
// (out/modules/<device>/, filled by `mise run module:pack-all`; Refresh reads it again). The page speaks the upload protocol itself, in
// TypeScript (apps/web/src/modules), over the controller's USB capture; no process is started and nothing is built. Install uploads and
// verifies each file on the card; that is all. The device's selection is the device's own: the user selects the module in MODULES on the
// device, then applies and restarts there. Restart only restarts the device and reports what loaded. Delete (in the On device header)
// removes the ticked files from the card; a loaded module stays loaded until the restart. "Add .dkm" and the drop zone put a module from
// outside the library on the card.
import { For, Show, createSignal, onMount, type JSX } from 'solid-js';
import { errorLine, formatBytes } from '@web/modules/backend';
import { bumpCardRefresh } from './card-state';
import { transferBackend } from './transfer-backend';
import {
  applyEvent,
  applyVerified,
  available,
  DONE_ON_CARD,
  deleteDoneText,
  deleteLabel,
  deletePaths,
  eventText,
  installModules,
  onDeviceWithPending,
  progressFailed,
  rowStatus,
  type RowStatus,
  progressFraction,
  rowKey,
  startProgress,
  stateText,
  warningText,
  type TransferEvent,
  type TransferRow,
} from './transfer-core';
import {
  delTicks,
  lastView,
  log,
  progress,
  setLastView,
  setDelTicks,
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
      // the Card panel reads the folder now and again after the emulator's quiet period (the folder follows the card about 2 s later)
      bumpCardRefresh();
      window.setTimeout(bumpCardRefresh, 2500);
    }
  };
  onMount(() => void run('Reading', async () => {}));

  const onEvent = (e: TransferEvent) => {
    const p = progress();
    if (p) setProgress(applyEvent(p, e));
    setVerified(applyVerified(verified(), e));
    if (e.event !== 'result' && e.event !== 'progress') setLog((l) => [...l, eventText(e)]);
  };
  const install = () =>
    run('Installing', async () => {
      const v = lastView();
      if (!v) return;
      const modules = installModules(v, ticks());
      setProgress(
        startProgress(
          modules,
          false,
          Object.fromEntries(v.modules.filter((r) => r.id).map((r) => [r.id as string, r.file_size])),
        ),
      );
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
      const files2 = await Promise.all(
        picked.map(async (f) => ({ name: f.name, data: new Uint8Array(await f.arrayBuffer()) })),
      );
      const lines = await transferBackend.add(files2);
      setLog(lines);
    });
  // Delete: the first click asks for a confirmation for about 3 s, the second runs it (no modal).
  const [confirming, setConfirming] = createSignal(false);
  let confirmTimer: number | undefined;
  const stopConfirm = () => {
    window.clearTimeout(confirmTimer);
    setConfirming(false);
  };
  const nDelete = () => {
    const v = lastView();
    return v ? deletePaths(v, delTicks()).length : 0;
  };
  const removeSelected = () =>
    run('Deleting', async () => {
      const v = lastView();
      if (!v) return;
      const paths = deletePaths(v, delTicks());
      setProgress(startProgress(paths, false));
      setLog([]);
      try {
        await transferBackend.remove(paths, onEvent);
      } catch (e) {
        const p = progress();
        if (p) setProgress({ ...p, message: errorLine(e) });
        throw e;
      }
      const p = progress();
      if (p) setProgress({ ...p, message: deleteDoneText(v, paths) });
      setDelTicks(new Set<string>());
    });
  const clickDelete = () => {
    if (!nDelete() || busy()) return;
    if (!confirming()) {
      setConfirming(true);
      confirmTimer = window.setTimeout(() => setConfirming(false), 3000);
      return;
    }
    stopConfirm();
    void removeSelected();
  };
  const toggleDelete = (r: TransferRow) => {
    const next = new Set(delTicks());
    if (next.has(r.path)) next.delete(r.path);
    else next.add(r.path);
    setDelTicks(next);
    stopConfirm();
  };
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
  const status = (r: TransferRow) => rowStatus(progress(), verified(), r.path);
  const upBytes = (st: RowStatus) => (st.kind === 'uploading' ? st.bytes : 0);
  const upSent = (st: RowStatus) => (st.kind === 'uploading' ? st.sent : 0);
  const upPct = (st: RowStatus) => (st.kind === 'uploading' && st.bytes ? Math.round((100 * st.sent) / st.bytes) : 0);
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

            <section class="col" aria-label="On device">
              <div class="colhead">
                <h3>On device</h3>
                <span class="sp" />
                <button
                  type="button"
                  class="act"
                  classList={{ danger: confirming() }}
                  disabled={!!busy() || nDelete() === 0}
                  title={
                    nDelete()
                      ? 'Delete the ticked files from the card (a loaded module stays loaded until the next restart)'
                      : 'Tick the files to delete'
                  }
                  onClick={clickDelete}
                >
                  {busy() === 'Deleting' ? 'Deleting…' : deleteLabel(nDelete(), confirming())}
                </button>
              </div>
              <Show
                when={onDeviceWithPending(v(), progress()).length}
                fallback={<p class="empty">The card holds no modules.</p>}
              >
                <ul class="rows">
                  <For each={onDeviceWithPending(v(), progress())}>
                    {(r) => (
                      <li>
                        <Show when={r.on_card !== 'no'}>
                          <input
                            type="checkbox"
                            aria-label={`Delete ${label(r)}`}
                            checked={delTicks().has(r.path)}
                            disabled={!!busy()}
                            onChange={() => toggleDelete(r)}
                          />
                        </Show>
                        <span class="name" title={r.path}>
                          {label(r)}
                          <Show when={!r.id}>
                            <em> not in the library</em>
                          </Show>
                        </span>
                        <span class="dim">{r.version ?? ''}</span>
                        <span class="dim">{formatBytes(r.file_size)}</span>
                        <Show when={status(r)?.kind !== 'uploading'}>
                          <span class="state">{stateText(r)}</span>
                        </Show>
                        <Show when={status(r)}>
                          {(st) => (
                            <>
                              <Show when={st().kind === 'uploading'}>
                                <span class="up">
                                  <progress max={upBytes(st())} value={upSent(st())} />
                                  <span class="dim">{upPct(st())}%</span>
                                </span>
                              </Show>
                              <Show when={st().kind === 'verifying'}>
                                <span class="check wait">verifying…</span>
                              </Show>
                              <Show when={st().kind === 'ok'}>
                                <span class="check ok">verified</span>
                              </Show>
                              <Show when={st().kind === 'failed'}>
                                <span class="check bad">failed</span>
                              </Show>
                            </>
                          )}
                        </Show>
                      </li>
                    )}
                  </For>
                </ul>
              </Show>
            </section>
          </div>
        )}
      </Show>

      <Show when={progress()}>
        {(p) => (
          <footer class="bar" role="status" aria-label="Transfer progress">
            <progress class="global" classList={{ failed: progressFailed(p()) }} max="100" value={pct()} />
            <span class="msg">
              {p().message || 'Starting…'} · {pct()}%
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
