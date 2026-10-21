// The Transfer panel (desktop only): the repo's run-time modules that load on the running emulator, in groups, with a tick box each;
// Install builds what changed, uploads, selects the ticked set and restarts (tools/device/dktransfer.py through the native commands).
// "Add .dkm" and the drop zone put a module from outside the repo in the store.
import { For, Show, createSignal, onMount, type JSX } from 'solid-js';
import { hexSha256, storePath } from '@web/modules/dkupload-core';
import { errorLine, formatBytes } from '@web/modules/backend';
import { emulatorModules } from './modules-backend';
import { transferBackend } from './transfer-backend';
import {
  differsFromLoaded,
  eventText,
  groupRows,
  initialTicks,
  installRequest,
  rowKey,
  stateText,
  warningText,
  type TransferRow,
  type TransferView,
} from './transfer-core';
import '@web/components/modules-panel.css';
import './transfer-panel.css';

export default function TransferPanel(): JSX.Element {
  const [view, setView] = createSignal<TransferView>();
  const [ticks, setTicks] = createSignal<Set<string>>(new Set());
  const [showTests, setShowTests] = createSignal(false);
  const [busy, setBusy] = createSignal('');
  const [error, setError] = createSignal('');
  const [log, setLog] = createSignal<string[]>([]);
  const [over, setOver] = createSignal(false);
  let input: HTMLInputElement | undefined;

  const refresh = async () => {
    try {
      const v = await transferBackend.list();
      setView(v);
      setTicks(initialTicks(v));
    } catch (e) {
      setView(undefined);
      setError(errorLine(e));
    }
  };
  const run = async (what: string, f: () => Promise<void>) => {
    if (busy()) return;
    setBusy(what);
    setError('');
    try {
      await f();
    } catch (e) {
      setError(errorLine(e));
    } finally {
      setBusy('');
    }
  };
  onMount(() => void run('Reading', refresh));

  const toggle = (r: TransferRow) => {
    const k = rowKey(r);
    const next = new Set(ticks());
    if (next.has(k)) next.delete(k);
    else next.add(k);
    setTicks(next);
  };

  const install = () =>
    run('Installing', async () => {
      const v = view();
      if (!v) return;
      setLog([]);
      await transferBackend.install(installRequest(v, ticks()), (e) => setLog((l) => [...l, eventText(e)]));
      await refresh();
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
      await refresh();
    });

  const remove = (r: TransferRow) =>
    run('Deleting', async () => {
      await emulatorModules.remove(r.path);
      await refresh();
    });

  const label = (r: TransferRow) => r.label ?? r.name;
  const pending = () => {
    const v = view();
    return !!v && differsFromLoaded(v, ticks());
  };

  return (
    <section
      class="modules transfer"
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
      <header>
        <strong>Transfer</strong>
        <span>Emulator</span>
      </header>
      <Show when={view()}>
        {(v) => (
          <p class="meta">
            <span>{v().device}</span>
            <span>runtime {v().runtime}</span>
            <span>{formatBytes(v().free_bytes)} free</span>
          </p>
        )}
      </Show>
      <label class="tests">
        <input type="checkbox" checked={showTests()} onChange={(e) => setShowTests(e.currentTarget.checked)} /> show
        test modules
      </label>

      <Show when={view()} fallback={<p class="empty">{busy() ? `${busy()}…` : 'No module list yet.'}</p>}>
        {(v) => (
          <For each={groupRows(v(), showTests())}>
            {(g) => (
              <div class="group">
                <h3>{g.group}</h3>
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
                          <span class="path" title={r.id ?? r.path}>
                            {label(r)}
                            <Show when={r.test}>
                              <em> test</em>
                            </Show>
                            <Show when={!r.id}>
                              <em> card</em>
                            </Show>
                          </span>
                          <span class="size">{r.version ?? ''}</span>
                          <span class="size" title={r.arena !== undefined ? `arena ${r.arena} B, DSP ${r.dsp} B` : ''}>
                            {formatBytes(r.file_size)}
                          </span>
                          <span class="state">{stateText(r)}</span>
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
        )}
      </Show>

      <Show when={log().length}>
        <pre class="log" role="status">
          {log().join('\n')}
        </pre>
      </Show>
      <Show when={error()}>
        <p class="err" role="alert">
          {error()}
        </p>
      </Show>

      <div class="acts">
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
        <button
          type="button"
          class="act primary"
          disabled={!!busy() || !view() || !pending()}
          title={pending() ? 'Install the ticked modules and restart the emulator' : 'The ticks equal what is loaded'}
          onClick={() => void install()}
        >
          {busy() === 'Installing' ? 'Installing…' : 'Install'}
        </button>
        <button type="button" class="act" disabled={!!busy()} onClick={() => void run('Reading', refresh)}>
          Refresh
        </button>
        <button type="button" class="act small" disabled={!!busy()} onClick={() => input?.click()}>
          Add .dkm…
        </button>
        <span class="drop">or drop files</span>
      </div>
    </section>
  );
}
