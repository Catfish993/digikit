// The Samples tab of the Transfer page: the device's sample folders and files (right), and what to add (left). The backend is the one the device
// profile names (`[storage] samples`: the stock MidiRpc file commands or the digikit store; samples-backend.ts), so the tab is the same on every
// device. A WAV is converted to the native sample file before it goes; a native sample goes as it is; any other file is uploaded unchanged.
// Each upload is verified against the bytes sent. The page speaks the protocol in TypeScript over the selected target; no process is started.
import { For, Show, createEffect, createSignal, on, onMount, type JSX } from 'solid-js';
import { errorLine, formatBytes } from '@web/modules/backend';
import { bumpCardRefresh } from './card-state';
import { openDeviceSamples, type OpenedSamples } from './samples-backend';
import {
  applyUpload,
  crumbs,
  deleteRows,
  dropUploads,
  folderNameError,
  joinPath,
  kindLabel,
  sampleRows,
  spaceText,
  storeNote,
  uploadFiles,
  uploadList,
  type SampleRow,
} from './samples-core';
import TargetSelect from './TargetSelect';
import { target, targetLabel } from './target';
import { deleteLabel } from './transfer-core';
import {
  sampleEntries,
  sampleFolder,
  sampleSpace,
  sampleTicks,
  setSampleEntries,
  setSampleFolder,
  setSampleSpace,
  setSampleTicks,
  setUploads,
  uploads,
} from './transfer-state';
import './transfer-page.css';

export default function SamplesPage(): JSX.Element {
  const [busy, setBusy] = createSignal('');
  const [error, setError] = createSignal('');
  const [note, setNote] = createSignal('');
  const [over, setOver] = createSignal(false);
  const [device, setDevice] = createSignal<{ name: string; samples: string; canMkdir: boolean }>();
  const [naming, setNaming] = createSignal(false);
  const [newName, setNewName] = createSignal('');
  let input: HTMLInputElement | undefined;
  let opened: OpenedSamples | undefined;

  /** Read the open folder, the free space and what the backend can do. */
  const refresh = async () => {
    opened = await openDeviceSamples();
    const { backend } = opened;
    setDevice({ name: opened.name, samples: opened.samples, canMkdir: await backend.canMkdir() });
    setSampleEntries(await backend.list(sampleFolder()));
    try {
      setSampleSpace(await backend.space());
    } catch {
      setSampleSpace(undefined); // the listing is what counts; the free space is a line
    }
  };
  /** Run `f` as the tab's one busy action; an error shows, the list is read again either way. */
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
  // another target: the old one's folder, ticks and uploads are not shown
  createEffect(
    on(
      target,
      () => {
        opened = undefined;
        setDevice(undefined);
        setSampleEntries(undefined);
        setSampleSpace(undefined);
        setSampleFolder('');
        setSampleTicks(new Set<string>());
        setUploads({});
        setNote('');
        void run('Reading', async () => {});
      },
      { defer: true },
    ),
  );

  const open = (folder: string) => {
    if (busy()) return;
    setSampleFolder(folder);
    setSampleTicks(new Set<string>());
    setNote('');
    stopConfirm();
    void run('Reading', async () => {});
  };

  const addFiles = (files: File[]) =>
    run('Uploading', async () => {
      if (!files.length) return;
      if (!opened) opened = await openDeviceSamples();
      const o = opened;
      const data = await Promise.all(
        files.map(async (f) => ({ name: f.name, data: new Uint8Array(await f.arrayBuffer()) })),
      );
      setNote('');
      await uploadFiles(o.backend, o.prepare, sampleFolder(), data, (e) => setUploads((u) => applyUpload(u, e)));
    });

  const createFolder = () => {
    const name = newName().trim();
    const why = folderNameError(name, sampleEntries() ?? []);
    if (why) {
      setError(why);
      return;
    }
    setNaming(false);
    setNewName('');
    void run('Creating', async () => {
      if (!opened) opened = await openDeviceSamples();
      await opened.backend.mkdir(joinPath(sampleFolder(), name));
    });
  };

  // Delete: the first click asks for a confirmation for about 3 s, the second runs it (no modal), as on the Modules tab.
  const [confirming, setConfirming] = createSignal(false);
  let confirmTimer: number | undefined;
  const stopConfirm = () => {
    window.clearTimeout(confirmTimer);
    setConfirming(false);
  };
  const rows = () => sampleRows(sampleFolder(), sampleEntries() ?? [], uploads());
  const nDelete = () => deleteRows(rows(), sampleTicks()).length;
  const removeSelected = () =>
    run('Deleting', async () => {
      if (!opened) opened = await openDeviceSamples();
      const { backend } = opened;
      const gone = deleteRows(rows(), sampleTicks());
      for (const r of gone) {
        if (r.kind === 'folder') await backend.rmdir(r.path);
        else await backend.remove(sampleFolder(), r.name);
        setUploads((u) => dropUploads(u, [r.path]));
        setSampleTicks((t) => {
          const next = new Set(t);
          next.delete(r.path);
          return next;
        });
      }
      setNote(`Deleted ${gone.length} item${gone.length === 1 ? '' : 's'}.`);
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
  const toggle = (r: SampleRow) => {
    const next = new Set(sampleTicks());
    if (next.has(r.path)) next.delete(r.path);
    else next.add(r.path);
    setSampleTicks(next);
    stopConfirm();
  };
  const pct = (sent: number, bytes: number) => (bytes ? Math.round((100 * sent) / bytes) : 0);

  return (
    <section
      class="transfer-page"
      classList={{ over: over() }}
      aria-label="Samples"
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
        <h2>Samples</h2>
        <Show when={device()}>
          {(d) => (
            <span class="meta">
              {targetLabel(target())} · {d().name}
              <Show when={sampleSpace()}> · {spaceText(sampleSpace())}</Show>
            </span>
          )}
        </Show>
        <span class="sp" />
        <TargetSelect disabled={!!busy()} onError={setError} />
        <Show when={busy() === 'Reading' && sampleEntries()}>
          <span class="meta">refreshing…</span>
        </Show>
        <button type="button" class="act" disabled={!!busy()} onClick={() => void run('Reading', async () => {})}>
          Refresh
        </button>
      </header>

      <Show when={error()}>
        <p class="err" role="alert">
          {error()}
        </p>
      </Show>

      <div class="cols">
        <section class="col" aria-label="To add">
          <div class="colhead">
            <h3>To add</h3>
            <span class="sp" />
            <input
              ref={input}
              type="file"
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
              disabled={!!busy() || !sampleEntries()}
              title="Choose files; they go into the open folder of the device"
              onClick={() => input?.click()}
            >
              {busy() === 'Uploading' ? 'Uploading…' : 'Add files…'}
            </button>
          </div>
          <div class="dropzone">
            Drop WAV files here. A WAV is converted to the device's sample format; a native sample goes as it is; any
            other file is uploaded unchanged.
          </div>
          <Show when={uploadList(uploads()).length}>
            <ul class="rows">
              <For each={uploadList(uploads())}>
                {(u) => (
                  <li classList={{ warn: u.kind === 'unchanged' }}>
                    <span class="name" title={u.source}>
                      {u.source}
                    </span>
                    <span class="dim">{formatBytes(u.bytes)}</span>
                    <span class="state">{kindLabel(u.kind)}</span>
                  </li>
                )}
              </For>
            </ul>
          </Show>
        </section>

        <section class="col" aria-label="On device">
          <div class="colhead">
            <h3>On device</h3>
            <span class="sp" />
            <Show when={device()?.canMkdir}>
              <button type="button" class="act" disabled={!!busy()} onClick={() => setNaming(!naming())}>
                New folder
              </button>
            </Show>
            <button
              type="button"
              class="act"
              classList={{ danger: confirming() }}
              disabled={!!busy() || nDelete() === 0}
              title={
                nDelete() ? 'Delete the ticked files and empty folders from the device' : 'Tick the rows to delete'
              }
              onClick={clickDelete}
            >
              {busy() === 'Deleting' ? 'Deleting…' : deleteLabel(nDelete(), confirming())}
            </button>
          </div>
          <nav class="crumbs" aria-label="Folder">
            <For each={crumbs(sampleFolder())}>
              {(c, i) => (
                <>
                  <Show when={i() > 0}>
                    <span class="dim">/</span>
                  </Show>
                  <button
                    type="button"
                    class="mini"
                    aria-current={c.path === sampleFolder() ? 'location' : undefined}
                    disabled={!!busy() || c.path === sampleFolder()}
                    onClick={() => open(c.path)}
                  >
                    {c.name}
                  </button>
                </>
              )}
            </For>
          </nav>
          <Show when={naming()}>
            <form
              class="newfolder"
              onSubmit={(e) => {
                e.preventDefault();
                createFolder();
              }}
            >
              <input
                type="text"
                aria-label="New folder name"
                placeholder="folder name"
                value={newName()}
                onInput={(e) => setNewName(e.currentTarget.value)}
              />
              <button type="submit" class="act small" disabled={!!busy()}>
                Create
              </button>
            </form>
          </Show>
          <Show when={sampleEntries()} fallback={<p class="empty">{busy() ? `${busy()}…` : 'No listing yet.'}</p>}>
            <Show when={rows().length} fallback={<p class="empty">This folder is empty.</p>}>
              <ul class="rows">
                <For each={rows()}>
                  {(r) => (
                    <li>
                      <input
                        type="checkbox"
                        aria-label={`Delete ${r.name}`}
                        checked={sampleTicks().has(r.path)}
                        disabled={!!busy() || r.pending}
                        onChange={() => toggle(r)}
                      />
                      <Show
                        when={r.kind === 'folder'}
                        fallback={
                          <span class="name" title={r.path}>
                            {r.name}
                          </span>
                        }
                      >
                        <button
                          type="button"
                          class="name link"
                          disabled={!!busy()}
                          title={r.path}
                          onClick={() => open(r.path)}
                        >
                          {r.name}/
                        </button>
                      </Show>
                      <span class="dim">{r.kind === 'file' ? formatBytes(r.size) : ''}</span>
                      <Show when={r.status}>
                        {(st) => (
                          <>
                            <Show when={st().kind === 'waiting'}>
                              <span class="check wait">waiting</span>
                            </Show>
                            <Show when={st().kind === 'verifying'}>
                              <span class="check wait">verifying…</span>
                            </Show>
                            <Show when={st().kind === 'ok'}>
                              <span class="check ok" title={st().message}>
                                verified
                              </span>
                            </Show>
                            <Show when={st().kind === 'failed'}>
                              <span class="check bad" title={st().message}>
                                failed
                              </span>
                            </Show>
                            <Show when={st().kind === 'failed' && st().message}>
                              <span class="state">{st().message.split('\n')[0]}</span>
                            </Show>
                            <Show when={st().kind === 'uploading'}>
                              <span class="up">
                                <progress
                                  max={(st() as { bytes: number }).bytes}
                                  value={(st() as { sent: number }).sent}
                                />
                                <span class="dim">
                                  {pct((st() as { sent: number }).sent, (st() as { bytes: number }).bytes)}%
                                </span>
                              </span>
                            </Show>
                          </>
                        )}
                      </Show>
                    </li>
                  )}
                </For>
              </ul>
            </Show>
          </Show>
          <Show when={device() && storeNote(device()!.samples, device()!.name)}>
            <p class="empty note">{storeNote(device()!.samples, device()!.name)}</p>
          </Show>
          <Show when={note()}>
            <p class="empty note">{note()}</p>
          </Show>
        </section>
      </div>
    </section>
  );
}
