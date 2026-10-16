import { For, Show, createMemo, createSignal, onCleanup, onMount } from 'solid-js';
import type { CheckResult, PatchPlan, VerifyReport } from '../patch/patchkit-core';
import { patchedFileName } from '../patch/patchkit-core';
import { putHandoff } from '../patch/handoff';
import { identify, deviceName, type Identity } from '../patch/identify';
import { selectionProblems, withRequired, withoutDependents, type CardInfo } from '../patch/selection';
import './patch.css';

type PatchRun = {
  name: string;
  version: string;
  ids: string[];
  plan: PatchPlan;
  verify: VerifyReport;
  sha256: string;
  bytes: ArrayBuffer;
  url: string;
  file: string;
  device: string;
};
type Reply = {
  type: string;
  id?: number;
  result?: CheckResult;
  error?: string;
  plan?: PatchPlan;
  verify?: VerifyReport;
  sha256?: string;
  syx?: ArrayBuffer;
};
type Block =
  | { t: 'p'; runs: Run_[] }
  | { t: 'ul'; items: Run_[][] }
  | { t: 'h'; level: number; runs: Run_[] }
  | { t: 'code'; text: string };
type Run_ = { text: string; code?: boolean; bold?: boolean };
type Card = CardInfo & {
  version: string;
  category: string;
  status: string;
  summary: string;
  targets: { port: string; status: string }[];
  gallery: Block[];
};

const base = import.meta.env.BASE_URL.replace(/\/?$/, '/');
const MAX_BYTES = 16 * 1024 * 1024;
const CATEGORY_LABEL: Record<string, string> = {
  fx: 'Effects',
  machines: 'Machines',
  core: 'Framework',
  test: 'Test fixtures',
};
const HIDDEN = new Set(['core', 'test']);

/** Module ids the user picked: patchkit lists `id@version` and adds framework modules itself. */
const userModules = (tags: string[]) => tags.map((t) => t.split('@')[0]).filter((id) => !id.startsWith('core/'));

const Runs = (props: { runs: Run_[] }) => (
  <For each={props.runs}>
    {(r) => (r.code ? <code>{r.text}</code> : r.bold ? <strong>{r.text}</strong> : <>{r.text}</>)}
  </For>
);

export default function PatchGallery() {
  const [cards, setCards] = createSignal<Card[]>([]);
  const [loadError, setLoadError] = createSignal('');
  const [ready, setReady] = createSignal(false);
  const [file, setFile] = createSignal<{ name: string; bytes: ArrayBuffer }>();
  const [check, setCheck] = createSignal<CheckResult>();
  const [identity, setIdentity] = createSignal<Identity>();
  const [fileError, setFileError] = createSignal('');
  const [selected, setSelected] = createSignal<string[]>([]);
  const [category, setCategory] = createSignal('all');
  const [showDev, setShowDev] = createSignal(false);
  const [busy, setBusy] = createSignal(false);
  const [patchError, setPatchError] = createSignal('');
  const [result, setResult] = createSignal<PatchRun>();
  const [dragging, setDragging] = createSignal(false);
  let worker: Worker | undefined;
  let nextId = 1;
  const pending = new Map<number, { resolve: (r: Reply) => void; reject: (e: Error) => void }>();
  let resultUrl: string | undefined;

  const send = (message: Record<string, unknown>, transfer: Transferable[] = []) =>
    new Promise<Reply>((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      worker?.postMessage({ ...message, id }, transfer);
    });

  onMount(() => {
    worker = new Worker(new URL('../patch/patch-worker.ts', import.meta.url), { type: 'module' });
    worker.onmessage = ({ data }: MessageEvent<Reply>) => {
      if (data.type === 'ready') {
        setReady(true);
        return;
      }
      const waiter = data.id === undefined ? undefined : pending.get(data.id);
      if (!waiter) {
        if (data.type === 'error') setLoadError(data.error ?? 'patch tool failed');
        return;
      }
      pending.delete(data.id!);
      if (data.type === 'error') waiter.reject(new Error(data.error));
      else waiter.resolve(data);
    };
    worker.onerror = (event) => setLoadError(`patch tool failed to start: ${event.message}`);
    worker.postMessage({ type: 'init', wasmUrl: `${base}patchkit.wasm`, bundleUrl: `${base}modules/bundle.json` });
    void fetch(`${base}modules/index.json`)
      .then((r) => {
        if (!r.ok) throw new Error('module list is missing from this site');
        return r.json();
      })
      .then((index: { modules: Card[] }) => setCards(index.modules))
      .catch((e) => setLoadError(String(e)));
  });
  onCleanup(() => {
    worker?.terminate();
    if (resultUrl) URL.revokeObjectURL(resultUrl);
  });

  const categories = createMemo(() => [
    'all',
    ...new Set(
      cards()
        .filter((c) => showDev() || !HIDDEN.has(c.category))
        .map((c) => c.category),
    ),
  ]);
  const visible = createMemo(() =>
    cards().filter(
      (c) =>
        (showDev() || !HIDDEN.has(c.category) || selected().includes(c.id)) &&
        (category() === 'all' || c.category === category()),
    ),
  );
  const compat = (id: string) => check()?.modules.find((m) => m.id === id);
  const problems = createMemo(() => selectionProblems(cards(), selected()));
  const reasonBlocked = (card: Card): string | undefined => {
    const c = compat(card.id);
    if (check() && c && !c.ok) return c.error ?? 'not compatible with this firmware';
    if (selected().includes(card.id)) return undefined;
    return problems().blockers.get(card.id);
  };
  const canPatch = () =>
    ready() &&
    !busy() &&
    Boolean(file()) &&
    Boolean(check()?.port) &&
    selected().length > 0 &&
    problems().conflicts.length === 0;

  const toggle = (card: Card) => {
    setResult(undefined);
    setPatchError('');
    const on = selected().includes(card.id);
    setSelected(on ? withoutDependents(cards(), selected(), card.id) : withRequired(cards(), selected(), card.id));
  };

  const loadFile = async (picked?: File) => {
    if (!picked) return;
    setFileError('');
    setResult(undefined);
    setPatchError('');
    setCheck(undefined);
    setIdentity(undefined);
    if (picked.size > MAX_BYTES) {
      setFile(undefined);
      setFileError('That file is too large for a firmware update (the limit here is 16 MB).');
      return;
    }
    const bytes = await picked.arrayBuffer();
    setFile({ name: picked.name, bytes });
    try {
      const reply = await send({ type: 'check', firmware: bytes.slice(0) });
      const found = reply.result!;
      setCheck(found);
      setIdentity(identify(found.sha256));
      // drop selections that do not work on this firmware
      setSelected((old) => old.filter((id) => found.modules.find((m) => m.id === id)?.ok));
    } catch (error) {
      setFile(undefined);
      setFileError(
        `This does not look like an Elektron firmware update file: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  };

  const patch = async () => {
    const f = file(),
      c = check();
    if (!f || !c) return;
    setBusy(true);
    setPatchError('');
    setResult(undefined);
    try {
      const ids = selected();
      const reply = await send({ type: 'apply', firmware: f.bytes.slice(0), modules: ids });
      const plan = reply.plan!;
      const syx = reply.syx!;
      if (resultUrl) URL.revokeObjectURL(resultUrl);
      resultUrl = URL.createObjectURL(new Blob([syx], { type: 'application/octet-stream' }));
      const name = identity()?.deviceName ?? deviceName(c.device);
      const version = identity()?.version ?? plan.base.version;
      setResult({
        name,
        version,
        ids: userModules(plan.modules),
        plan,
        verify: reply.verify!,
        sha256: reply.sha256!,
        bytes: syx,
        url: resultUrl,
        file: patchedFileName(name, version, userModules(plan.modules)),
        device: c.device,
      });
    } catch (error) {
      setPatchError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const boot = async () => {
    const r = result();
    if (!r) return;
    try {
      await putHandoff({ name: r.file, device: r.device, bytes: r.bytes.slice(0) });
      location.assign(`${base}emulator/?handoff=1`);
    } catch (error) {
      setPatchError(`Could not pass the file to the emulator: ${String(error)}`);
    }
  };

  const idLine = () => {
    const c = check(),
      i = identity();
    if (!c) return '';
    if (i) return `${i.deviceName}, OS ${i.version}`;
    return `${deviceName(c.device)}, ${c.port ? c.port : 'version not recognised'}`;
  };
  const statusKind = () => {
    const c = check();
    return !c ? '' : c.port ? 'ok' : 'warn';
  };

  return (
    <div class="page patch">
      <header class="intro">
        <h1>Patch your firmware</h1>
        <p>
          Pick modules, apply them to your own Digitakt II or Digitone II firmware, and download the patched{' '}
          <code>.syx</code>. Everything happens in this browser tab.{' '}
          <strong>Your firmware stays on your device:</strong> it is never uploaded, and this site hosts no firmware.
        </p>
      </header>

      <section
        class="dropzone"
        classList={{ dragging: dragging() }}
        aria-labelledby="fw-title"
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          void loadFile(e.dataTransfer?.files[0]);
        }}
      >
        <h2 id="fw-title">1. Your firmware</h2>
        <Show
          when={file()}
          fallback={
            <p class="muted">
              Drop the stock <code>.syx</code> from Elektron here, or choose it.
            </p>
          }
        >
          <p class="file-line">
            <strong>{file()!.name}</strong>{' '}
            <span class={`pill ${statusKind()}`}>{check() ? idLine() : 'checking...'}</span>
          </p>
          <Show when={check() && !check()!.port}>
            <p class="note warn">
              This firmware version has no patch support yet, so no module can be applied. Supported versions are listed
              in the module cards below.
            </p>
          </Show>
        </Show>
        <label class="button primary">
          {file() ? 'Choose another file' : 'Choose firmware file'}
          <input
            type="file"
            accept=".syx,application/octet-stream"
            onChange={(e) => {
              void loadFile(e.currentTarget.files?.[0]);
              e.currentTarget.value = '';
            }}
          />
        </label>
        <Show when={fileError()}>
          <p class="note bad" role="alert">
            {fileError()}
          </p>
        </Show>
      </section>

      <section aria-labelledby="mod-title">
        <div class="section-head">
          <h2 id="mod-title">2. Modules</h2>
          <div class="filters" role="group" aria-label="Category">
            <For each={categories()}>
              {(c) => (
                <button class="chip" aria-pressed={category() === c} onClick={() => setCategory(c)}>
                  {c === 'all' ? 'All' : (CATEGORY_LABEL[c] ?? c)}
                </button>
              )}
            </For>
          </div>
          <label class="check-line">
            <input type="checkbox" checked={showDev()} onChange={(e) => setShowDev(e.currentTarget.checked)} /> show
            framework and test modules
          </label>
        </div>
        <Show when={loadError()}>
          <p class="note bad" role="alert">
            {loadError()}
          </p>
        </Show>
        <Show when={cards().length > 0 && visible().length === 0}>
          <p class="muted">No modules in this category.</p>
        </Show>
        <ul class="grid">
          <For each={visible()}>
            {(card) => {
              const blocked = () => reasonBlocked(card);
              const on = () => selected().includes(card.id);
              const required = () =>
                on() &&
                selected().some(
                  (o) =>
                    o !== card.id &&
                    cards()
                      .find((c) => c.id === o)
                      ?.requires.includes(card.id),
                );
              return (
                <li class="card" classList={{ on: on(), blocked: Boolean(blocked()) && !on() }}>
                  <div class="card-head">
                    <h3>{card.name}</h3>
                    <span class={`pill status-${card.status}`}>{card.status}</span>
                  </div>
                  <p class="meta">
                    <code>{card.id}</code> v{card.version} - {CATEGORY_LABEL[card.category] ?? card.category}
                  </p>
                  <p>{card.summary}</p>
                  <Show when={card.gallery.length > 0}>
                    <details>
                      <summary>More</summary>
                      <For each={card.gallery}>
                        {(b) =>
                          b.t === 'p' ? (
                            <p>
                              <Runs runs={b.runs} />
                            </p>
                          ) : b.t === 'ul' ? (
                            <ul>
                              <For each={b.items}>
                                {(item) => (
                                  <li>
                                    <Runs runs={item} />
                                  </li>
                                )}
                              </For>
                            </ul>
                          ) : b.t === 'code' ? (
                            <pre>{b.text}</pre>
                          ) : (
                            <h4>
                              <Runs runs={b.runs} />
                            </h4>
                          )
                        }
                      </For>
                    </details>
                  </Show>
                  <p class="targets">
                    <For each={card.targets}>
                      {(t) => (
                        <span class={`pill small t-${t.status}`} title={`Test status on ${t.port}: ${t.status}`}>
                          {t.port} {t.status}
                        </span>
                      )}
                    </For>
                  </p>
                  <Show when={card.requires.length > 0}>
                    <p class="meta">
                      Needs:{' '}
                      <For each={card.requires}>
                        {(r, i) => (
                          <>
                            {i() > 0 ? ', ' : ''}
                            <code>{r}</code>
                          </>
                        )}
                      </For>{' '}
                      (added automatically)
                    </p>
                  </Show>
                  <Show when={card.conflicts.length > 0}>
                    <p class="meta">
                      Conflicts with:{' '}
                      <For each={card.conflicts}>
                        {(r, i) => (
                          <>
                            {i() > 0 ? ', ' : ''}
                            <code>{r}</code>
                          </>
                        )}
                      </For>
                    </p>
                  </Show>
                  <Show when={check() && compat(card.id)}>
                    {(c) => (
                      <p class={`badge ${c().ok ? 'ok' : 'bad'}`}>
                        {c().ok ? 'Works with your firmware' : 'Not available for your firmware'}
                      </p>
                    )}
                  </Show>
                  <Show when={blocked()}>
                    <p class="note warn">{blocked()}</p>
                  </Show>
                  <button
                    class="button"
                    classList={{ primary: !on() }}
                    aria-pressed={on()}
                    disabled={(Boolean(blocked()) && !on()) || required()}
                    onClick={() => toggle(card)}
                  >
                    {required() ? 'Required' : on() ? 'Remove' : 'Add'}
                  </button>
                </li>
              );
            }}
          </For>
        </ul>
      </section>

      <section class="actions" aria-labelledby="go-title">
        <h2 id="go-title">3. Patch</h2>
        <p class="muted">
          Selected:{' '}
          <Show when={selected().length > 0} fallback="none">
            <For each={selected()}>
              {(id, i) => (
                <>
                  {i() > 0 ? ', ' : ''}
                  <code>{id}</code>
                </>
              )}
            </For>
          </Show>
        </p>
        <Show when={problems().conflicts.length > 0}>
          <For each={problems().conflicts}>
            {(c) => (
              <p class="note bad" role="alert">
                {c}
              </p>
            )}
          </For>
        </Show>
        <button class="button primary big" disabled={!canPatch()} onClick={() => void patch()}>
          {busy() ? 'Patching...' : 'Patch'}
        </button>
        <Show when={!file()}>
          <span class="muted"> Choose your firmware first.</span>
        </Show>
        <Show when={patchError()}>
          <p class="note bad" role="alert">
            {patchError()}
          </p>
        </Show>
      </section>

      <Show when={result()}>
        {(r) => (
          <section class="result" aria-labelledby="res-title" aria-live="polite">
            <h2 id="res-title">Result</h2>
            <p class="note ok">Patched and verified in this browser. {r().verify.checks.length} checks passed.</p>
            <dl>
              <dt>Device</dt>
              <dd>
                {r().name}, OS {r().version}
              </dd>
              <dt>Modules</dt>
              <dd>
                <For each={r().plan.record.modules}>
                  {(m, i) => (
                    <>
                      {i() > 0 ? ', ' : ''}
                      <code>{m}</code>
                    </>
                  )}
                </For>
              </dd>
              <dt>Output</dt>
              <dd>
                {r().file} - {r().bytes.byteLength.toLocaleString()} bytes
              </dd>
              <dt>SHA-256</dt>
              <dd>
                <code class="hash">{r().sha256}</code>
              </dd>
              <dt>Changed sections</dt>
              <dd>
                {r()
                  .plan.output?.changed_sections.map((s) => `#${s[0]}`)
                  .join(', ') || 'none'}
              </dd>
            </dl>
            <details>
              <summary>Verification</summary>
              <ul>
                <For each={r().verify.checks}>{(c) => <li>{c}</li>}</For>
              </ul>
            </details>
            <p class="buttons">
              <a class="button primary" href={r().url} download={r().file}>
                Download patched firmware
              </a>
              <button class="button" onClick={() => void boot()}>
                Boot in emulator
              </button>
            </p>
            <p class="muted small">
              The emulator boots the patched firmware with its screen and controls but without sound: the browser has no
              DSP audio yet.
            </p>
          </section>
        )}
      </Show>

      <section class="safety" aria-labelledby="safe-title">
        <h2 id="safe-title">Before you flash</h2>
        <ul>
          <li>
            <strong>Flashing is at your own risk.</strong> No patched image has been tested on hardware yet. Elektron
            does not support patched firmware: do not report problems to them.
          </li>
          <li>
            <strong>
              Keep the stock <code>.syx</code>.
            </strong>{' '}
            You need it to go back.
          </li>
          <li>
            <strong>Recovery:</strong> hold <kbd>FUNC</kbd> while powering on for the startup menu, then{' '}
            <kbd>TRIG 4</kbd> for OS UPGRADE. This path accepts a firmware over <strong>MIDI DIN only</strong> (USB MIDI
            is not available there), so have a DIN MIDI interface ready.
          </li>
          <li>
            The patcher never changes the bootstrap, the updater or the language section, and it checks the result
            before it offers the download.
          </li>
        </ul>
      </section>
    </div>
  );
}
