// The Modules panel: the module store of a running device (or the emulator), over the upload protocol
// (docs/design/digikit-upload.md). List, add .dkm files (picker or drag and drop), delete, repack.
import { For, Show, createSignal, onMount, type JSX } from 'solid-js';
import { hexSha256, repackMissing, storePath, type DkEntry, type DkInfo } from '../modules/dkupload-core.ts';
import { errorLine, formatBytes, isCancelled, isTimeout, type ModuleBackend } from '../modules/backend.ts';
import './modules-panel.css';

interface Held {
  data: Uint8Array;
  sha256: string;
}

const NO_RUNTIME =
  'No digikit runtime answered. Load a firmware patched with the runtime (mise run patch -- --firmware X.syx --modules <runtime set>), or wait for Boot with runtime.';

export interface ModulesPanelProps {
  backend: ModuleBackend;
  /** Extra line under the title (where the files go). */
  hint?: string;
}

export default function ModulesPanel(props: ModulesPanelProps): JSX.Element {
  const [info, setInfo] = createSignal<DkInfo>();
  const [entries, setEntries] = createSignal<DkEntry[]>([]);
  const [busy, setBusy] = createSignal('');
  const [note, setNote] = createSignal('');
  const [error, setError] = createSignal('');
  // The last probe got no answer: the device runs no digikit runtime (stock firmware).
  const [absent, setAbsent] = createSignal(false);
  const [progress, setProgress] = createSignal<{
    name: string;
    done: number;
    total: number;
    index: number;
    count: number;
  }>();
  const [over, setOver] = createSignal(false);
  // Files this page has seen (added or uploaded) by store path: what a repack can send again.
  const held = new Map<string, Held>();
  let cancelled = false;
  let input: HTMLInputElement | undefined;

  const fail = (e: unknown) => {
    if (isCancelled(e)) setNote('Cancelled.');
    else setError(errorLine(e));
  };
  // Until a runtime has answered, INFO is a short probe (about 1 s, no retries): stock firmware never answers, and
  // the normal retries would take about 18 s. One request at a time (the backends queue them anyway).
  const refresh = async () => {
    const probing = !info();
    try {
      const i = await props.backend.info(probing);
      setAbsent(false);
      setInfo(i);
      setEntries(await props.backend.list());
    } catch (e) {
      setInfo(undefined);
      if (probing && isTimeout(e)) {
        setAbsent(true);
        setEntries([]);
      } else fail(e);
    }
  };
  const run = async (what: string, f: () => Promise<void>) => {
    if (busy()) return;
    setBusy(what);
    setError('');
    setNote('');
    cancelled = false;
    try {
      await f();
    } catch (e) {
      fail(e);
    } finally {
      setProgress(undefined);
      setBusy('');
      await refresh();
    }
  };
  onMount(() => void run('Reading', async () => {}));

  const sendFiles = async (files: File[]) => {
    const picked = files.filter((f) => /\.dkm$/i.test(f.name));
    if (picked.length < files.length) setNote(`Skipped ${files.length - picked.length} file(s) that are not .dkm.`);
    if (!picked.length) return;
    await run('Uploading', async () => {
      const onDevice = new Map((await props.backend.list()).map((e) => [e.path, e.sha256]));
      let sent = 0;
      for (const [index, file] of picked.entries()) {
        const data = new Uint8Array(await file.arrayBuffer());
        const path = storePath(file.name);
        const sha256 = await hexSha256(data);
        held.set(path, { data, sha256 });
        if (onDevice.get(path) === sha256) continue; // already there
        await props.backend.put(path, data, ({ done, total }) => {
          setProgress({ name: file.name, done, total, index, count: picked.length });
          return !cancelled;
        });
        sent++;
      }
      setNote(
        sent === picked.length
          ? `Uploaded ${sent} file${sent === 1 ? '' : 's'}. They load after APPLY & RESTART.`
          : `Uploaded ${sent}; ${picked.length - sent} already on the device.`,
      );
    });
  };

  const remove = (path: string) =>
    run('Deleting', async () => {
      await props.backend.remove(path);
      held.delete(path);
      setNote(`Deleted ${path}.`);
    });

  const canRepack = () => {
    const missing = repackMissing(
      entries(),
      [...held].map(([path, h]) => ({ path, sha256: h.sha256 })),
    );
    return missing;
  };
  const repack = () =>
    run('Repacking', async () => {
      const listing = await props.backend.list();
      const missing = repackMissing(
        listing,
        [...held].map(([path, h]) => ({ path, sha256: h.sha256 })),
      );
      if (missing.length) {
        throw new Error(
          `Repack erases the store and sends the files again, and this page does not hold ${missing.join(', ')}. Add those files first.`,
        );
      }
      const files = listing.map((e) => ({ path: e.path, data: held.get(e.path)!.data }));
      await props.backend.clear();
      for (const [index, f] of files.entries()) {
        await props.backend.put(f.path, f.data, ({ done, total }) => {
          setProgress({ name: f.path, done, total, index, count: files.length });
          return !cancelled;
        });
      }
      setNote(`Repacked ${files.length} file${files.length === 1 ? '' : 's'}.`);
    });

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    void sendFiles(Array.from(e.dataTransfer?.files ?? []));
  };
  const pct = () => {
    const p = progress();
    return p && p.total ? Math.round((100 * p.done) / p.total) : 0;
  };

  return (
    <section
      class="modules"
      classList={{ over: over() }}
      aria-label="Modules"
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
    >
      <header>
        <strong>Modules</strong>
        <span>{props.backend.label}</span>
      </header>
      <Show when={info()}>
        {(i) => (
          <p class="meta">
            <span>{i().port}</span>
            <span>{formatBytes(i().free_bytes)} free</span>
            <Show when={i().safe_mode}>
              <span class="warn">safe mode</span>
            </Show>
            <Show when={i().upload_open}>
              <span class="warn">upload open</span>
            </Show>
          </p>
        )}
      </Show>
      <Show when={props.hint}>
        <p class="hint">{props.hint}</p>
      </Show>

      <Show
        when={entries().length}
        fallback={
          <p class="empty">
            {info()
              ? 'The store holds no files.'
              : absent()
                ? NO_RUNTIME
                : 'No device answered.'}
          </p>
        }
      >
        <ul class="files">
          <For each={entries()}>
            {(e) => (
              <li>
                <span class="path" title={e.path}>
                  {e.path.replace(/^modules\//, '')}
                </span>
                <span class="size">{formatBytes(e.size)}</span>
                <code title={e.sha256}>{e.sha256.slice(0, 8)}</code>
                <button
                  type="button"
                  class="mini"
                  disabled={!!busy()}
                  aria-label={`Delete ${e.path}`}
                  onClick={() => void remove(e.path)}
                >
                  Delete
                </button>
              </li>
            )}
          </For>
        </ul>
      </Show>

      <Show when={progress()}>
        {(p) => (
          <div class="progress" role="status">
            <span>
              {p().name} ({p().index + 1}/{p().count}) {pct()}%
            </span>
            <progress max="100" value={pct()} />
            <button type="button" class="mini" onClick={() => (cancelled = true)}>
              Cancel
            </button>
          </div>
        )}
      </Show>
      <Show when={error()}>
        <p class="err" role="alert">
          {error()}
        </p>
      </Show>
      <Show when={note() && !error()}>
        <p class="note">{note()}</p>
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
            void sendFiles(files);
          }}
        />
        <button type="button" class="act primary" disabled={!!busy() || absent()} onClick={() => input?.click()}>
          Add .dkm…
        </button>
        <button type="button" class="act" disabled={!!busy()} onClick={() => void run('Reading', async () => {})}>
          Refresh
        </button>
        <button
          type="button"
          class="act"
          disabled={!!busy() || !entries().length}
          title={
            canRepack().length
              ? `This page does not hold: ${canRepack().join(', ')}`
              : 'Erase the store and send every file again; the space of replaced files comes back'
          }
          onClick={() => void repack()}
        >
          Repack
        </button>
        <span class="drop">or drop files here</span>
      </div>
    </section>
  );
}
