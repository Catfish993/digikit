// The desktop developer console: the file the app runs, the live panel, and the audio and CPU meters.
// `mise run workbench -- FILE` starts it on FILE; with "watching" on, a change to FILE on disk reloads it, so a
// module can be patched from the command line and heard here a second later.
import { invoke } from '@tauri-apps/api/core';
import { Show, createSignal, onCleanup, onMount } from 'solid-js';
import LivePanel, { type LivePanelApi } from '@web/components/LivePanel';
import { panels } from '@web/data/faceplates';
import {
  nativeRuntime,
  type EmulatorRuntime,
  type FirmwareInfo,
  type NativeAudioStatus,
  type RuntimeSnapshot,
  type RuntimeUpdate,
} from '@web/runtime/runtime';
import ModulesPanel from '@web/components/ModulesPanel';
import { emulatorModules } from './modules-backend';
import { cardLine, folderFiles, startCard, type CardStatus } from '@web/runtime/cards';

type Source = { path: string; name: string; bytes: number; modified_ms: number };
type Status = RuntimeSnapshot['status'];

const message = (error: unknown) => (error instanceof Error ? error.message : String(error));

// "Boot with digikit runtime": on unless the user turned it off; kept per browser profile of the app.
const RUNTIME_KEY = 'digikit.bootRuntime';
const readRuntimePref = () => {
  try {
    return localStorage.getItem(RUNTIME_KEY) !== 'off';
  } catch {
    return true;
  }
};
const writeRuntimePref = (on: boolean) => {
  try {
    localStorage.setItem(RUNTIME_KEY, on ? 'on' : 'off');
  } catch {
    /* the choice lasts until the window closes */
  }
};

function Icon(props: { d: string }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path
        d={props.d}
        fill="none"
        stroke="currentColor"
        stroke-width="1.6"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
  );
}
const FOLDER = 'M1.5 4V12.5H14.5V5.5H7.5L6 4Z';
const RELOAD = 'M13 3V6.5H9.5M13 6.5A5.2 5.2 0 1 0 13.4 10';
const PAUSE = 'M5.5 3V13M10.5 3V13';
const PLAY = 'M4.5 2.5L12.5 8L4.5 13.5Z';
const RESTART = 'M3 2.5V6H6.5M3 6A5.2 5.2 0 1 1 2.8 9.8';
const CHART = 'M2 12.5L5.5 7.5L8.5 10L13.5 3.5M10.5 3.5H13.5V6.5';
const SPEAKER = 'M2.5 6H5L8.5 3V13L5 10H2.5ZM11 5.5a3.5 3.5 0 0 1 0 5';
const UP = 'M4 10L8 6L12 10';
const MODULES = 'M2.5 5L8 2.5L13.5 5V11L8 13.5L2.5 11ZM2.5 5L8 7.5L13.5 5M8 7.5V13.5';
const DOWN = 'M4 6L8 10L12 6';

function Meter(props: { value: number; worst?: number; tone?: 'teal' | 'amber' }) {
  const pct = (v: number) => `${Math.max(0, Math.min(100, v))}%`;
  return (
    <span class="meter" aria-hidden="true">
      <b class={props.tone ?? 'teal'} style={{ width: pct(props.value) }} />
      <Show when={props.worst !== undefined}>
        <u style={{ left: pct(props.worst!) }} />
      </Show>
    </span>
  );
}

export default function Console() {
  const [runtime, setRuntime] = createSignal<EmulatorRuntime>();
  const [outsideApp, setOutsideApp] = createSignal(false);
  const [status, setStatus] = createSignal<Status>();
  const [error, setError] = createSignal('');
  const [paused, setPaused] = createSignal(false);
  const [source, setSource] = createSignal<Source>();
  const [opened, setOpened] = createSignal('');
  const [watching, setWatching] = createSignal(true);
  const [reloadNote, setReloadNote] = createSignal('');
  const [audio, setAudio] = createSignal<NativeAudioStatus>();
  const [rate, setRate] = createSignal<number>();
  const [drawer, setDrawer] = createSignal(false);
  const [modulesOpen, setModulesOpen] = createSignal(false);
  const [card, setCard] = createSignal<CardStatus>();
  const [cardBusy, setCardBusy] = createSignal(false);
  const [bootRuntime, setBootRuntime] = createSignal(readRuntimePref());
  const [info, setInfo] = createSignal<FirmwareInfo>();
  let live: LivePanelApi | undefined;
  let fileInput: HTMLInputElement | undefined;
  let folderInput: HTMLInputElement | undefined;
  let lastAudio: { produced: number; wall: number } | undefined;
  let lastModified: number | undefined;
  let reloading = false;
  const timers: number[] = [];

  const device = () => {
    const d = status()?.device;
    return d === 'dt2' || d === 'dn2' ? d : 'dn2';
  };
  const panel = () => panels[device()];

  const update = (event: RuntimeUpdate) => {
    if (event.type === 'error') {
      setError(event.error);
      return;
    }
    live?.show(event.snapshot);
    setStatus(event.snapshot.status);
    setError(event.snapshot.status.error ?? '');
  };

  /** What the running image got from the runtime patch, for the header. */
  const refreshInfo = async () => {
    try {
      setInfo(await runtime()?.firmwareInfo?.());
    } catch {
      setInfo(undefined);
    }
  };
  /** Start a new firmware session with `start` (every load goes through the controller's `firmware.load`). */
  const boot = async (start: (r: EmulatorRuntime) => Promise<RuntimeSnapshot>) => {
    const r = runtime();
    if (!r) return;
    live?.releaseAll();
    live?.clear();
    setError('');
    try {
      update({ type: 'snapshot', snapshot: await start(r) });
      setPaused(false);
      await refreshInfo();
    } catch (e) {
      setError(message(e));
    }
  };
  const load = (bytes: Uint8Array, name: string) => boot((r) => r.load(bytes, name, { runtime: bootRuntime() }));
  /** Boot the startup file again: the controller reads it from its path. */
  const reload = async (why: string) => {
    const path = source()?.path;
    if (reloading || !path) return;
    reloading = true;
    try {
      setOpened('');
      await boot((r) =>
        r.loadPath ? r.loadPath(path, { runtime: bootRuntime() }) : Promise.reject(new Error('no reload')),
      );
      setReloadNote(`${why} ${new Date().toLocaleTimeString()}`);
    } catch (e) {
      setError(message(e));
    } finally {
      reloading = false;
    }
  };
  const pollSource = async () => {
    try {
      const file = await invoke<Source | null>('emu_source_info');
      setSource(file ?? undefined);
      if (!file) return;
      const changed = lastModified !== undefined && file.modified_ms !== lastModified;
      lastModified = file.modified_ms;
      if (changed && watching() && !opened()) await reload('Reloaded after a change at');
    } catch {
      /* the file may be mid-write; the next poll sees it */
    }
  };
  const pollAudio = async () => {
    try {
      const a = await runtime()?.nativeAudio?.();
      setAudio(a);
      const produced = a?.source_audio_seconds,
        wall = a?.session_wall_seconds;
      if (produced === undefined || wall === undefined) return;
      if (lastAudio && produced < lastAudio.produced) lastAudio = undefined;
      if (lastAudio && wall > lastAudio.wall) setRate((produced - lastAudio.produced) / (wall - lastAudio.wall));
      lastAudio = { produced, wall };
    } catch {
      /* a replaced session has no status */
    }
  };

  const onKey = (e: KeyboardEvent) => {
    // ⌘R / Ctrl+R reloads the firmware file, not the page.
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'r') {
      e.preventDefault();
      void reload('Reloaded at');
    }
  };
  onMount(() => {
    void nativeRuntime(update).then((r) => {
      if (!r) {
        setOutsideApp(true);
        return;
      }
      setRuntime(r);
      void (async () => {
        await pollSource();
        await refreshInfo();
        // The app starts with the runtime patched in; a user who turned that off gets the startup file as it is.
        if (!bootRuntime() && info()?.runtime?.set) await reload('Started without the runtime at');
      })();
      timers.push(window.setInterval(() => void pollSource(), 1000));
      timers.push(window.setInterval(() => void pollAudio(), 1000));
    });
    window.addEventListener('keydown', onKey);
  });
  onCleanup(() => {
    timers.forEach((t) => window.clearInterval(t));
    window.removeEventListener('keydown', onKey);
    void runtime()?.stop();
    runtime()?.dispose();
  });

  const togglePause = () => {
    const r = runtime();
    if (!r) return;
    if (paused()) r.resume();
    else r.pause();
    setPaused(!paused());
  };
  const restart = async () => {
    try {
      const s = await runtime()?.restart();
      if (s) update({ type: 'snapshot', snapshot: s });
      setPaused(false);
      await refreshInfo();
    } catch (e) {
      setError(message(e));
    }
  };
  const saveDiagnostics = async () => {
    try {
      const r = runtime();
      const report = await r?.diagnostics();
      if (!r?.saveDiagnostics || !report) return;
      setReloadNote(
        `Diagnostics saved to ${await r.saveDiagnostics(`digiemu-${report.device}-diagnostics.json`, JSON.stringify(report, null, 2))}`,
      );
    } catch (e) {
      setError(`Diagnostics failed: ${message(e)}`);
    }
  };
  const chooseSamples = async (list: FileList | null) => {
    const cards = runtime()?.cards;
    if (!cards || !list) return;
    const files = folderFiles(Array.from(list));
    if (folderInput) folderInput.value = '';
    if (files.length === 0) {
      setCard({ state: 'error', message: 'No files in that folder.' });
      return;
    }
    setCardBusy(true);
    try {
      await startCard(cards, files, (done, total) =>
        setCard({ state: 'working', message: `Sending samples (${done}/${total})`, fraction: 0 }),
      );
      const poll = async () => {
        const s = await runtime()!.cards!.status();
        setCard(s);
        if (s.state === 'working') window.setTimeout(() => void poll(), 400);
        else setCardBusy(false);
      };
      void poll();
    } catch (e) {
      setCardBusy(false);
      setCard({ state: 'error', message: message(e) });
    }
  };

  const fileLabel = () => opened() || source()?.path || 'No firmware file';
  /** The last three parts of the path; the full path is the tooltip. */
  const shortPath = () => {
    const parts = fileLabel().split('/');
    return parts.length > 3 ? `…/${parts.slice(-3).join('/')}` : fileLabel();
  };
  const phase = () => {
    const s = status();
    if (!s) return '';
    if (s.frame_revision === 0) return 'booting';
    return s.frame_source === 'intro' ? 'startup display' : s.phase;
  };
  const load_ = () => audio()?.load ?? undefined;
  const sink = () => audio()?.sink ?? undefined;

  return (
    <div class="console" style={{ '--accent': panel().accent, '--func-legend': panel().func_legend }}>
      <header class="topbar">
        <span class="file" title={fileLabel()}>
          <Icon d={FOLDER} />
          <span class="path">{shortPath()}</span>
        </span>
        <Show when={status()}>
          <span class="tag device">
            {status()!.device} {status()!.version}
          </span>
        </Show>
        <Show when={info()}>
          {(i) => (
            <span
              class="tag runtime"
              classList={{ on: !!i().runtime?.patched }}
              title={
                i().runtime?.patched
                  ? `${i().runtime?.modules?.length ?? 0} modules of the "${i().runtime?.set}" set, ${i().runtime?.cache_hit ? 'from the image cache' : 'patched now'}`
                  : i().runtime
                    ? 'The image has a patch record: it is loaded as it is'
                    : 'The image is loaded as it is'
              }
            >
              {i().runtime?.patched
                ? `runtime ${i().runtime?.set}${i().runtime?.cache_hit ? ' (cached)' : ''}`
                : i().runtime
                  ? 'patched image'
                  : 'stock'}
            </span>
          )}
        </Show>
        <Show when={source() && !opened()}>
          <button
            type="button"
            class="tag watch"
            classList={{ off: !watching() }}
            aria-pressed={watching()}
            title="Reload when the file changes on disk"
            onClick={() => setWatching(!watching())}
          >
            <i />
            {watching() ? 'watching' : 'not watching'}
          </button>
        </Show>
        <Show when={opened()}>
          <span class="tag" title="Opened from the file picker; only the startup file is watched">
            opened
          </span>
        </Show>
        <span class="sp" />
        <label
          class="tag opt"
          title="Patch the digikit runtime into a stock firmware when it is opened or reloaded (the next Open or Reload)"
        >
          <input
            type="checkbox"
            checked={bootRuntime()}
            onChange={(e) => {
              setBootRuntime(e.currentTarget.checked);
              writeRuntimePref(e.currentTarget.checked);
            }}
          />
          Boot with digikit runtime
        </label>
        <input
          ref={fileInput}
          type="file"
          accept=".syx,application/octet-stream"
          hidden
          onChange={async (e) => {
            const f = e.currentTarget.files?.[0];
            e.currentTarget.value = '';
            if (!f) return;
            setOpened(f.name);
            await load(new Uint8Array(await f.arrayBuffer()), f.name);
          }}
        />
        <button type="button" class="tb" onClick={() => fileInput?.click()}>
          <Icon d={FOLDER} />
          Open…
        </button>
        <button
          type="button"
          class="tb"
          disabled={!source()}
          title="Reload the startup file (⌘R)"
          onClick={() => void reload('Reloaded at')}
        >
          <Icon d={RELOAD} />
          Reload
        </button>
        <button type="button" class="tb" disabled={!status()} onClick={togglePause}>
          <Icon d={paused() ? PLAY : PAUSE} />
          {paused() ? 'Resume' : 'Pause'}
        </button>
        <button
          type="button"
          class="tb"
          disabled={!status()}
          title="Reset the running firmware to its ready state (the emulator's reset; not the device's RESTART command)"
          onClick={() => void restart()}
        >
          <Icon d={RESTART} />
          Reset (emulator)
        </button>
        <button
          type="button"
          class="tb"
          disabled={!status()}
          aria-pressed={modulesOpen()}
          title="List, upload and delete the modules in the module store"
          onClick={() => setModulesOpen(!modulesOpen())}
        >
          <Icon d={MODULES} />
          Modules
        </button>
        <button
          type="button"
          class="tb icon"
          disabled={!status()}
          aria-label="Save diagnostics"
          title="Save diagnostics"
          onClick={() => void saveDiagnostics()}
        >
          <Icon d={CHART} />
        </button>
      </header>

      <Show when={error() || outsideApp()}>
        <p class="alert" role="alert">
          {outsideApp() ? 'This page is the desktop app’s console; start it with mise run workbench.' : error()}
        </p>
      </Show>

      <main class="stage" aria-label={`${panel().name} front panel`}>
        <div class="surface">
          <LivePanel panel={panel()} runtime={runtime} onError={(e) => setError(e)} ref={(api) => (live = api)} />
        </div>
      </main>

      <Show when={modulesOpen()}>
        <div class="modules-sheet">
          <ModulesPanel
            backend={emulatorModules}
            hint="Uploads go to the store of the running emulator; they load after APPLY & RESTART."
          />
        </div>
      </Show>

      <Show when={drawer()}>
        <section class="drawer" aria-label="Metrics">
          <header>
            <strong>Metrics</strong>
            <span>1 s poll</span>
          </header>
          <dl>
            <dt>Firmware</dt>
            <dd>{status() ? `${status()!.device} ${status()!.version} · ${phase()}` : '—'}</dd>
            <dt>Instructions</dt>
            <dd>{status()?.icount.toLocaleString() ?? '—'}</dd>
            <dt>Audio output</dt>
            <dd>{audio()?.output_device ?? (audio()?.setup_needed ? 'no audio for this firmware' : '—')}</dd>
            <dt>Speed</dt>
            <dd>{rate() !== undefined ? `${rate()!.toFixed(2)}× real time (recent)` : '—'}</dd>
            <dt>Underruns</dt>
            <dd classList={{ warn: (sink()?.underrun_events ?? 0) > 0 }}>{sink()?.underrun_events ?? 0}</dd>
            <dt>DSP cost</dt>
            <dd>
              {typeof audio()?.dsp_tiers?.dsp_cpu_per_audio_s === 'number'
                ? `${audio()!.dsp_tiers!.dsp_cpu_per_audio_s!.toFixed(2)} CPU s / audio s`
                : '—'}
            </dd>
            <dt>Produced</dt>
            <dd>{audio() ? `${audio()!.source_audio_seconds.toFixed(2)} s` : '—'}</dd>
            <dt>CF load</dt>
            <dd>{load_() ? `${Math.round(load_()!.cf.real_pct)} %` : '—'}</dd>
            <dt>DSP load</dt>
            <dd>
              {load_()?.dsp
                ? `${Math.round(load_()!.dsp!.real_pct)} % (worst ${Math.round(load_()!.dsp!.real_high_pct)} %)`
                : '—'}
            </dd>
          </dl>
          <Show when={reloadNote()}>
            <p class="note">{reloadNote()}</p>
          </Show>
          <Show when={card()}>
            <p class="note">{cardLine(card())}</p>
          </Show>
          <div class="acts">
            <button
              type="button"
              class="tb"
              disabled={!status() || !audio()?.flow_started}
              onClick={() =>
                void runtime()
                  ?.tap?.(25)
                  .catch((e) => setError(message(e)))
              }
            >
              Audition Trig 1
            </button>
            <Show when={device() === 'dt2' && runtime()?.cards}>
              <input
                ref={(el) => {
                  folderInput = el;
                  el.setAttribute('webkitdirectory', '');
                }}
                type="file"
                multiple
                hidden
                onChange={(e) => void chooseSamples(e.currentTarget.files)}
              />
              <button
                type="button"
                class="tb"
                disabled={!status() || cardBusy()}
                title="Pick a plus_drive-style folder: WAVs become samples (track N plays file N, sorted by name), .mmcfs/ and digikit/ are used as they are"
                onClick={() => folderInput?.click()}
              >
                Sample folder…
              </button>
            </Show>
          </div>
        </section>
      </Show>

      <footer class="statusbar">
        <span>
          <i class="dot" classList={{ paused: paused() || !status() }} />
          {!status() ? 'no firmware' : paused() ? 'paused' : phase() || 'running'}
        </span>
        <Show when={audio()?.output_device}>
          <span>
            <Icon d={SPEAKER} />
            {audio()!.output_device}
          </span>
        </Show>
        <Show when={rate() !== undefined}>
          <span title="Emulated audio seconds per wall second">
            {rate()!.toFixed(2)}× RT
            <Meter value={rate()! * 100} tone="amber" />
          </span>
        </Show>
        <Show when={sink()}>
          <span classList={{ warn: (sink()?.underrun_events ?? 0) > 0 }}>underruns {sink()!.underrun_events}</span>
        </Show>
        <Show when={load_()}>
          <span title="Estimated load of the real chips, from the manuals' cycle tables">
            CF {Math.round(load_()!.cf.real_pct)}%
            <Meter value={load_()!.cf.real_pct} />
          </span>
          <Show when={load_()!.dsp}>
            <span>
              DSP {Math.round(load_()!.dsp!.real_pct)}%
              <Meter value={load_()!.dsp!.real_pct} worst={load_()!.dsp!.real_high_pct} />
            </span>
          </Show>
        </Show>
        <span class="sp" />
        <Show when={reloadNote() && !drawer()}>
          <span class="muted">{reloadNote()}</span>
        </Show>
        <button type="button" class="sb" aria-expanded={drawer()} onClick={() => setDrawer(!drawer())}>
          Metrics
          <Icon d={drawer() ? DOWN : UP} />
        </button>
      </footer>
    </div>
  );
}
