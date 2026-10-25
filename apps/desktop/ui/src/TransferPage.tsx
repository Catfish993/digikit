// The Transfer page (desktop only): one page, two modes. A dropdown picks Modules (the built modules of the library folder and the card's
// module store) or Samples (a folder of samples and the device's sample folders). The left column is always "On your computer", the right
// column "On device"; each column carries its own controls (ModulesMode.tsx, SamplesMode.tsx; the shared parts are in TransferParts.tsx).
// This file holds what the modes share: the top bar (title, mode), the one busy action, the error line, and the progress bar along the bottom.
// There is no Refresh button: the page reads by itself (AutoRefresh.tsx, refresh-core.ts). The page speaks the upload protocol itself, in
// TypeScript, over the controller's USB capture; no process is started and nothing is built.
import { For, Match, Show, Switch, createEffect, createMemo, createSignal, on, onMount, type JSX } from 'solid-js';
import { isCancelled, isTimeout } from '@web/modules/backend';
import { noteActivity } from './activity';
import AutoRefresh from './AutoRefresh';
import { bumpCardRefresh } from './card-state';
import ModulesMode from './ModulesMode';
import SamplesMode from './SamplesMode';
import { deviceStorage, readLocal, readSamples } from './samples-backend';
import { target, targetLabel } from './target';
import { transferBackend } from './transfer-backend';
import { samplesBar } from './samples-core';
import TransferBar from './TransferBar';
import { MODES, modulesBar, parseMode, storageLine } from './transfer-core';
import { writing, type PageCtx } from './TransferParts';
import {
  log,
  mode,
  progress,
  setDelTicks,
  setDeviceInfo,
  setLastView,
  setMode,
  setSampleEntries,
  setSampleFolder,
  setSampleTicks,
  setTicks,
  setUploads,
  uploadBase,
  uploads,
} from './transfer-state';
import WaitNotice from './WaitNotice';
import { failureLine } from './wait-core';
import './transfer-page.css';

/** Read what the mode shows, both sides: the device and the computer (the library folder, or the chosen samples folder). */
async function read(): Promise<void> {
  if (mode() === 'samples') {
    // the device and the folder fail apart: both are read, the errors are shown together
    const errors: string[] = [];
    for (const step of [readSamples, readLocal])
      try {
        await step();
      } catch (e) {
        errors.push(failureLine(e, targetLabel(target())));
      }
    if (errors.length) throw new Error(errors.join('\n'));
    return;
  }
  const v = await transferBackend.list();
  setLastView(v);
  const { name, samples } = await deviceStorage(v.device);
  setDeviceInfo({ name, samples, runtime: v.runtime, storage: storageLine(v), canMkdir: false });
}

export default function TransferPage(): JSX.Element {
  const [busy, setBusy] = createSignal('');
  const [error, setError] = createSignal('');
  const [reload, setReload] = createSignal(0);

  // a read asked for while another job runs (another target or mode during a wait) runs when that job ends
  let readAgain = false;
  const run = async (what: string, f: () => Promise<void>) => {
    if (busy()) {
      if (what === 'Reading') readAgain = true;
      return;
    }
    setBusy(what);
    noteActivity();
    setError('');
    // after a cancel or a "no reply" the device is not asked again at once (that would wait the whole time a second time)
    let skipRead = false;
    try {
      await f();
    } catch (e) {
      skipRead = isCancelled(e) || isTimeout(e);
      setError(failureLine(e, targetLabel(target())));
    } finally {
      if (!skipRead)
        try {
          await read();
        } catch (e) {
          if (!error()) setError(failureLine(e, targetLabel(target())));
        }
      setBusy('');
      if (readAgain) {
        readAgain = false;
        void run('Reading', async () => {});
      }
      // the Card panel reads the folder now and again after the emulator's quiet period (the folder follows the card about 2 s later)
      bumpCardRefresh();
      window.setTimeout(bumpCardRefresh, 2500);
    }
  };
  const ctx: PageCtx = { busy, run, setError, reload };
  const readNow = () => void run('Reading', async () => {});

  onMount(() => void run('Reading', async () => {}));
  // another target: what was read from the old one is not shown; read the new one
  createEffect(
    on(
      target,
      () => {
        setLastView(undefined);
        setDeviceInfo(undefined);
        setTicks(new Set<string>());
        setDelTicks(new Set<string>());
        setSampleEntries(undefined);
        setSampleFolder('');
        setSampleTicks(new Set<string>());
        setUploads({});
        void run('Reading', async () => {});
      },
      { defer: true },
    ),
  );
  // another mode: the device line is the mode's own (its free space), so read it again
  const changeMode = (value: string) => {
    setMode(parseMode(value));
    setError('');
    setDeviceInfo(undefined);
    void run('Reading', async () => {});
  };

  // the bar along the bottom, for the install, delete or restart of Modules and the upload of Samples
  const bar = createMemo(() => {
    if (mode() === 'modules') {
      const p = progress();
      return p ? modulesBar(p) : undefined;
    }
    return samplesBar(uploads(), uploadBase(), busy() === 'Uploading');
  });
  const barRunning = () => (mode() === 'modules' ? writing(busy()) : busy() === 'Uploading');

  return (
    <section class="transfer-page" aria-label="Transfer">
      <header class="head">
        <h2>Transfer</h2>
        <label class="target-select">
          <span class="dim">Mode</span>{' '}
          <select
            aria-label="Mode"
            disabled={writing(busy())}
            value={mode()}
            onChange={(e) => changeMode(e.currentTarget.value)}
          >
            <For each={MODES}>{(m) => <option value={m.id}>{m.label}</option>}</For>
          </select>
        </label>
        <span class="sp" />
        <Show when={busy() === 'Reading'}>
          <span class="meta">reading…</span>
        </Show>
      </header>
      <AutoRefresh busy={busy} read={readNow} rescanPorts={() => setReload((n) => n + 1)} />

      <WaitNotice active={!!busy()} />

      <Show when={error()}>
        <p class="err" role="alert">
          <span class="msg">{error()}</span>
          <button type="button" class="act small" disabled={!!busy()} onClick={readNow}>
            Try again
          </button>
        </p>
      </Show>

      <Switch>
        <Match when={mode() === 'modules'}>
          <ModulesMode ctx={ctx} />
        </Match>
        <Match when={mode() === 'samples'}>
          <SamplesMode ctx={ctx} />
        </Match>
      </Switch>

      <Show when={bar()}>{(b) => <TransferBar bar={b()} running={barRunning()} />}</Show>
      <Show when={mode() === 'modules' && log().length && !bar()}>
        <pre class="log">{log().join('\n')}</pre>
      </Show>
    </section>
  );
}
