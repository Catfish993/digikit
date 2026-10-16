import { For, Show, createEffect, createSignal, onCleanup, onMount } from 'solid-js';
import { panels } from '../data/faceplates';
import { deviceProfiles, localReadyFirmwareSha256, type DeviceId } from '../data/device-profiles';
import {
  browserRuntime,
  drawFrame,
  nativeRuntime,
  type EmulatorRuntime,
  type NativeAudioStatus,
  type RuntimeUpdate,
} from '../runtime/runtime';
import { LED_COUNT, defaultLedColor, ledLight, parseLeds, WARM_WHITE } from '../runtime/leds';
import { PcmSink, resumeOnNextGesture } from '../runtime/audio';
import { fetchLocalInputs, localManifest, sha256Hex } from '../runtime/local-inputs';
import CoupledAudio, { startCoupled } from './CoupledAudio';
import PanelArt from './PanelArt';
import SyxFile from './SyxFile';
import { benchRequested, runBench } from '../runtime/bench';
import { cardLine, startCard, topLevelWavs, type CardStatus } from '../runtime/cards';
import { devEnabled } from '../dev';
import { identify } from '../patch/identify';
import { takeHandoff } from '../patch/handoff';
import './faceplate.css';

function BenchStatus() {
  onMount(() => {
    void runBench();
  });
  return <output id="bench-status">bench mode: see window.__bench</output>;
}

/** The emulator page: the bench (dev only, tools/devtools/browser_bench.mjs) or the panel. */
export default function Faceplate(props: { runtime?: EmulatorRuntime } = {}) {
  return (
    <Show when={!(devEnabled() && benchRequested())} fallback={<BenchStatus />}>
      <Panel runtime={props.runtime} />
    </Show>
  );
}

function Panel(props: { runtime?: EmulatorRuntime }) {
  const dev = devEnabled();
  const [device, setDevice] = createSignal<'dt2' | 'dn2'>('dt2');
  const [pressed, setPressed] = createSignal(new Set<number>());
  const [leds, setLeds] = createSignal(new Uint8Array(LED_COUNT));
  // `?leds=<128 hex digits>` shows a fixed LED state without a running emulator (a styling aid; a loaded firmware replaces it).
  onMount(() => {
    const preview = dev ? new URLSearchParams(location.search).get('leds') : null;
    if (preview) {
      setLeds(parseLeds(preview));
      setStage('run');
    }
  });
  const [status, setStatus] = createSignal('Choose a firmware file (.syx) to start.');
  // The page walks through three stages: pick a device, choose its firmware, run it.
  const [stage, setStage] = createSignal<'pick' | 'firmware' | 'run'>('pick');
  const [running, setRunning] = createSignal<{ device: string; version: string }>();
  const [phase, setPhase] = createSignal('');
  const [fileName, setFileName] = createSignal('');
  const [native, setNative] = createSignal(false);
  const [dragging, setDragging] = createSignal(false);
  const [reportAvailable, setReportAvailable] = createSignal(false);
  const [paused, setPaused] = createSignal(true);
  const [loaded, setLoaded] = createSignal(false);
  const [browserAudioSetup, setBrowserAudioSetup] = createSignal(false);
  const [browserAudioReset, setBrowserAudioReset] = createSignal(0);
  const [chosenFirmware, setChosenFirmware] = createSignal<{ name: string; bytes: Uint8Array }>();
  const [adopted, setAdopted] = createSignal<{ sink: PcmSink; note: string }>();
  const [nativeAudio, setNativeAudio] = createSignal<NativeAudioStatus>();
  const [recentRate, setRecentRate] = createSignal<number>();
  // Samples for the Digitakt II: desktop only, where the controller builds the card (runtime/cards.ts).
  const [cardStatus, setCardStatus] = createSignal<CardStatus>();
  const [cardBusy, setCardBusy] = createSignal(false);
  const [cardNote, setCardNote] = createSignal('');
  const [runtimeSignal, setRuntimeSignal] = createSignal<EmulatorRuntime | undefined>(undefined);
  let cardTimer: number | undefined;
  let cardInput: HTMLInputElement | undefined;
  let lastAudio: { produced: number; wall: number } | undefined;
  const runtime = () => runtimeSignal() ?? activeRuntime ?? props.runtime;
  let activeRuntime: EmulatorRuntime | undefined;
  let runtimeReady: Promise<EmulatorRuntime | undefined> | undefined;
  let canvas: HTMLCanvasElement | undefined;
  const pointers = new Map<
    number,
    { code?: number; encoder?: number; y: number; pushed?: number; moved: boolean; target?: SVGGElement }
  >();
  const owners = new Map<number, number>();
  // Encoders are endless and the firmware reports no angle: the notch angle is UI-only (24 detents per turn).
  const [angles, setAngles] = createSignal<Record<number, number>>({});
  const angle = (encoder: number | undefined) => (encoder ? (angles()[encoder] ?? 0) : 0);
  const spin = (encoder: number, detents: number) =>
    setAngles((old) => ({ ...old, [encoder]: ((((old[encoder] ?? 0) + detents * 15) % 360) + 360) % 360 }));
  // A mouse cannot hold FUNC while clicking another key, and the firmware only sees a chord when FUNC
  // stays down across the other key's press and release (docs/findings/03-ui-and-panel.md). A click
  // on FUNC latches it; the next key's release (or a second click on FUNC) releases it.
  const FUNC = 17;
  let funcLatched = false;
  let suppressedKnob: SVGGElement | undefined;
  let clearSuppression: number | undefined;
  let audioTimer: number | undefined;
  let firmwareFile: File | undefined;
  const panel = () => panels[device()];
  const clearCanvas = () => {
    if (canvas) canvas.getContext('2d')?.clearRect(0, 0, 128, 64);
    setLeds(new Uint8Array(LED_COUNT));
  };
  // A key's lit state: the legend glows (the real keys are backlit through the print). Trig keys default to idx = code.
  const keyLight = (key: { code: number; style?: string; icon?: string; led?: number; led_color?: string }) => {
    const idx = key.led;
    if (idx === undefined) return undefined;
    const light = ledLight(leds()[idx] ?? 0);
    return light.on
      ? { '--lit': String(light.intensity), '--lit-color': light.color ?? key.led_color ?? defaultLedColor(key.icon) }
      : undefined;
  };
  const indicatorLight = (i: number) => {
    const idx = panel().leds.idx?.[i];
    if (idx === undefined) return undefined;
    const light = ledLight(leds()[idx] ?? 0);
    return light.on
      ? { '--lit': String(light.intensity), '--lit-color': light.color ?? panel().leds.color ?? WARM_WHITE }
      : undefined;
  };
  const update = (event: RuntimeUpdate) => {
    if (event.type === 'error') {
      setPaused(true);
      setLoaded(false);
      setStatus(event.error);
      return;
    }
    const snapshot = event.snapshot;
    setReportAvailable(true);
    if (snapshot.frame && canvas) drawFrame(canvas, snapshot.frame);
    if (snapshot.leds !== undefined) setLeds(parseLeds(snapshot.leds));
    const s = snapshot.status;
    if (s.error) {
      setPaused(true);
      setLoaded(false);
    } else if (!loaded()) {
      setLoaded(true);
      setPaused(false);
    }
    if (s.device === 'dt2' || s.device === 'dn2') setDevice(s.device);
    setRunning({ device: s.device, version: s.version });
    const display =
      s.frame_revision === 0
        ? 'Booting — waiting for display'
        : s.frame_source === 'intro'
          ? 'Booting — startup display'
          : s.phase;
    setPhase(s.error ? s.error : display);
    setStatus(
      `${s.device} ${s.version} — ${display}, ${s.icount.toLocaleString()} instructions${s.error ? `: ${s.error}` : ''}`,
    );
  };
  const emit = (code: number, down: boolean) => {
    const count = owners.get(code) ?? 0;
    const nextCount = down ? count + 1 : Math.max(0, count - 1);
    if (nextCount) owners.set(code, nextCount);
    else owners.delete(code);
    if ((down && count === 0) || (!down && count === 1)) {
      setPressed((old) => {
        const next = new Set(old);
        if (down) next.add(code);
        else next.delete(code);
        return next;
      });
      const result = down ? runtime()?.press(code) : runtime()?.release(code);
      void result?.catch((error) => update({ type: 'error', error: String(error) }));
    }
  };
  // Knob drags listen on window, not on the SVG with pointer capture: WebKit (the Tauri webview, Safari)
  // can retarget or drop captured pointer events and fire lostpointercapture early, which ended the drag
  // before the first detent. Deltas come from clientY differences (movementY is not reliable in WebKit).
  const dragMove = (e: PointerEvent) => {
    const drag = pointers.get(e.pointerId);
    if (!drag?.encoder) return;
    const detents = Math.trunc((drag.y - e.clientY) / 8);
    if (Math.abs(e.clientY - drag.y) > 3) drag.moved = true;
    if (detents) {
      spin(drag.encoder, detents);
      void runtime()
        ?.turn(drag.encoder, detents)
        ?.catch((error) => update({ type: 'error', error: String(error) }));
      drag.y -= detents * 8;
    }
  };
  const dragEnd = (e: PointerEvent) => {
    releasePointer(e.pointerId);
  };
  const detachDrag = () => {
    window.removeEventListener('pointermove', dragMove);
    window.removeEventListener('pointerup', dragEnd);
    window.removeEventListener('pointercancel', dragEnd);
  };
  const releasePointer = (id: number) => {
    const state = pointers.get(id);
    if (!state) return;
    if (state.target && (state.moved || state.pushed)) {
      suppressedKnob = state.target;
      if (clearSuppression) window.clearTimeout(clearSuppression);
      clearSuppression = window.setTimeout(() => {
        suppressedKnob = undefined;
      }, 0);
    }
    if (state.code) emit(state.code, false);
    if (state.pushed) emit(state.pushed, false);
    if (funcLatched && (state.code || state.pushed)) {
      funcLatched = false;
      emit(FUNC, false);
    }
    pointers.delete(id);
    if (![...pointers.values()].some((p) => p.encoder)) detachDrag();
  };
  const releaseAll = () => {
    funcLatched = false;
    for (const code of pressed()) runtime()?.release(code);
    owners.clear();
    setPressed(new Set<number>());
    pointers.clear();
    detachDrag();
    suppressedKnob = undefined;
  };
  onMount(() => window.addEventListener('blur', releaseAll));
  let disposed = false;
  const refreshNativeAudio = async () => {
    try {
      const status = await activeRuntime?.nativeAudio?.();
      setNativeAudio(status);
      const produced = status?.source_audio_seconds,
        wall = status?.session_wall_seconds;
      if (produced === undefined || wall === undefined) {
        lastAudio = undefined;
        return;
      }
      if (lastAudio && produced < lastAudio.produced) lastAudio = undefined;
      if (lastAudio && wall > lastAudio.wall) setRecentRate((produced - lastAudio.produced) / (wall - lastAudio.wall));
      lastAudio = { produced, wall };
    } catch {
      /* a replaced session has no status to show */
    }
  };
  const loadLine = (load: NonNullable<NativeAudioStatus['load']>) => {
    const parts = [`CF ${Math.round(load.cf.real_pct)} %`];
    if (load.dsp) parts.push(`DSP ${Math.round(load.dsp.real_pct)} % (worst ${Math.round(load.dsp.real_high_pct)} %)`);
    return parts.join(' · ');
  };
  const loadTitle = (load: NonNullable<NativeAudioStatus['load']>) =>
    `Estimated load of the real chips, from the manuals' cycle tables (not measured). In the emulator's own model: ColdFire ${load.cf.model_pct.toFixed(0)} %${load.dsp ? `, DSP ${load.dsp.model_pct.toFixed(0)} %` : ''}. "Worst" assumes the high bound of the assumed latencies. Window ${load.window_s.toFixed(1)} s of emulated time.`;
  const audioLine = (audio: NativeAudioStatus) => {
    const sink = audio.sink;
    const parts = [`Audio output active${audio.output_device ? ` on ${audio.output_device}` : ''}`];
    const rate = recentRate();
    if (rate !== undefined) parts.push(`${rate.toFixed(2)}× real time`);
    parts.push(`underruns ${sink?.underrun_events ?? 0}`);
    if (sink?.rebuffering) parts.push('refilling');
    if (audio.audio_mode === 'stretch' && sink?.stretch_rate !== undefined)
      parts.push(`speed ${sink.stretch_rate.toFixed(3)}`);
    const cost = audio.dsp_tiers?.dsp_cpu_per_audio_s;
    if (typeof cost === 'number') parts.push(`DSP ${cost.toFixed(2)} CPU s per audio s`);
    parts.push(`produced ${audio.source_audio_seconds.toFixed(2)} s`);
    return parts.join(' · ');
  };
  onMount(() => {
    runtimeReady = Promise.resolve(props.runtime ?? nativeRuntime(update)).then((runtime) => {
      const selected = runtime ?? browserRuntime(update);
      if (disposed) {
        selected.dispose();
        return undefined;
      }
      activeRuntime = selected;
      setRuntimeSignal(selected);
      setBrowserAudioSetup(!runtime);
      setNative(Boolean(runtime));
      void refreshNativeAudio();
      audioTimer = window.setInterval(() => {
        void refreshNativeAudio();
      }, 1000);
      return selected;
    });
  });
  onCleanup(() => {
    disposed = true;
    window.removeEventListener('blur', releaseAll);
    if (clearSuppression) window.clearTimeout(clearSuppression);
    if (audioTimer) window.clearInterval(audioTimer);
    releaseAll();
    void activeRuntime?.stop().catch((error) => update({ type: 'error', error: String(error) }));
    activeRuntime?.dispose();
  });
  const choose = async (file?: File) => {
    if (!file) return;
    setStage('run');
    setFileName(file.name);
    setRunning(undefined);
    setPhase('');
    // Dev-only coupled path (Digitone II): create and resume the AudioContext before any await.
    const coupledSink = dev ? new PcmSink() : undefined;
    void coupledSink?.resume().catch(() => {});
    if (coupledSink) resumeOnNextGesture(coupledSink);
    let kept = false;
    releaseAll();
    setBrowserAudioReset((value) => value + 1);
    void runtime()?.stop();
    clearCanvas();
    setReportAvailable(false);
    setLoaded(false);
    firmwareFile = file;
    setStatus(`Loading ${file.name}…`);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (firmwareFile !== file) return;
      setChosenFirmware({ name: file.name, bytes: bytes.slice() });
      const identity = identify(await sha256Hex(bytes));
      if (identity) setDevice(identity.device);
      const selected = await runtimeReady;
      if (firmwareFile !== file || !selected) return;
      let coupledError = '';
      if (coupledSink) {
        try {
          const manifest = await localManifest();
          if (manifest?.dspImage && manifest.dspState && (await sha256Hex(bytes)) === localReadyFirmwareSha256) {
            const inputs = await fetchLocalInputs();
            if (inputs) {
              const snapshot = await startCoupled({
                runtime: selected,
                firmware: { name: file.name, bytes },
                image: inputs.image,
                dsp: inputs.state,
                snapshot: inputs.snapshot,
                sink: coupledSink,
              });
              if (firmwareFile !== file) return;
              kept = true;
              setAdopted({ sink: coupledSink, note: `firmware: ${file.name} (local dev inputs)` });
              update({ type: 'snapshot', snapshot });
              setPaused(false);
              setLoaded(true);
              setStatus('Digitone II with audio (local dev inputs) — press NO, then Trig 1');
              return;
            }
          }
        } catch (error) {
          coupledError = String(error);
        }
        if (firmwareFile !== file) return;
      }
      const snapshot = await selected.load(bytes, file.name);
      if (firmwareFile !== file) return;
      update({ type: 'snapshot', snapshot });
      setPaused(false);
      setLoaded(true);
      if (coupledError) setStatus(`Local audio start failed (${coupledError}); booted without audio.`);
    } catch (error) {
      if (firmwareFile === file) {
        setPaused(true);
        setLoaded(false);
        setStatus(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (!kept) coupledSink?.close();
    }
  };
  // /patch hands a patched image over through IndexedDB (never the network): boot it as if it had been chosen.
  onMount(() => {
    if (new URLSearchParams(location.search).get('handoff') !== '1') return;
    setStage('run');
    void takeHandoff()
      .then((item) => {
        if (!item) {
          setStatus('Nothing to boot: open the patch page and press Boot in emulator.');
          return;
        }
        if (item.device === 'dt2' || item.device === 'dn2') setDevice(item.device);
        void choose(new File([item.bytes], item.name));
      })
      .catch((error) =>
        setStatus(`Could not read the patched firmware: ${error instanceof Error ? error.message : String(error)}`),
      );
  });
  const pollCard = () => {
    const cards = runtime()?.cards;
    if (!cards) return;
    cardTimer = window.setTimeout(async () => {
      try {
        const status = await cards.status();
        setCardStatus(status);
        if (status.state === 'working') {
          pollCard();
          return;
        }
        setCardBusy(false);
        if (status.state === 'ready') {
          setPaused(false);
          setLoaded(true);
        }
      } catch (error) {
        setCardBusy(false);
        setCardNote(`Card status failed: ${String(error)}`);
      }
    }, 400);
  };
  const chooseSamples = async (list?: FileList | null) => {
    const cards = runtime()?.cards;
    if (!cards || !list) return;
    const files = topLevelWavs(Array.from(list));
    if (cardInput) cardInput.value = '';
    if (files.length === 0) {
      setCardNote('No .wav files directly in that folder.');
      return;
    }
    setCardBusy(true);
    setCardNote('');
    setCardStatus({
      state: 'working',
      message: `Sending ${files.length} sample${files.length === 1 ? '' : 's'} to the emulator`,
      fraction: 0,
    });
    try {
      await startCard(cards, files, (done, total) =>
        setCardStatus({ state: 'working', message: `Sending samples (${done}/${total})`, fraction: 0 }),
      );
      pollCard();
    } catch (error) {
      setCardBusy(false);
      setCardStatus({ state: 'error', message: String(error) });
    }
  };
  // A DT2 that has just started may have mounted the last card on its own: show it, or the hint.
  createEffect(() => {
    const cards = runtime()?.cards;
    if (!cards || !loaded() || device() !== 'dt2' || cardBusy()) return;
    cards.status().then(
      (status) => {
        if (!cardBusy()) setCardStatus(status);
      },
      () => undefined,
    );
  });
  onCleanup(() => {
    if (cardTimer) window.clearTimeout(cardTimer);
  });
  const exportDiagnostics = async () => {
    try {
      const report = await runtime()?.diagnostics();
      if (!report) return;
      const name = `digiemu-${report.device}-diagnostics.json`,
        json = JSON.stringify(report, null, 2);
      const save = runtime()?.saveDiagnostics;
      if (save) {
        setStatus(`Diagnostics saved to ${await save(name, json)}`);
        return;
      }
      const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = name;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      setStatus(`Diagnostic export failed: ${String(error)}`);
    }
  };
  const changeFirmware = () => {
    releaseAll();
    setBrowserAudioReset((value) => value + 1);
    void runtime()?.stop();
    clearCanvas();
    firmwareFile = undefined;
    setReportAvailable(false);
    setLoaded(false);
    setPaused(true);
    setRunning(undefined);
    setPhase('');
    setStatus('Choose a firmware file (.syx) to start.');
    setStage('firmware');
  };
  const base = import.meta.env.BASE_URL.replace(/\/?$/, '/');
  const accepted = (d: DeviceId) => deviceProfiles[d].firmware.filter((f) => f.main_sha256).map((f) => f.version);
  const runningName = () => {
    const r = running();
    if (!r) return panel().name;
    const name = r.device === 'dt2' || r.device === 'dn2' ? panels[r.device].name : r.device;
    return `${name} · OS ${r.version}`;
  };
  const restart = async () => {
    try {
      const snapshot = await runtime()?.restart();
      if (snapshot) update({ type: 'snapshot', snapshot });
      setPaused(false);
    } catch (error) {
      setPaused(true);
      setLoaded(false);
      setStatus(String(error));
    }
  };
  return (
    <main
      class="emulator"
      classList={{ running: stage() === 'run' }}
      data-loaded={loaded() ? 'true' : 'false'}
      style={{ '--accent': panel().accent, '--func-legend': panel().func_legend }}
    >
      <input
        id="emu-file"
        class="visually-hidden"
        type="file"
        accept=".syx,application/octet-stream"
        onChange={(e) => {
          void choose(e.currentTarget.files?.[0]);
          e.currentTarget.value = '';
        }}
      />
      <Show when={stage() === 'pick'}>
        <div class="wrap emu-pick">
          <header>
            <h1>Run your firmware in the browser</h1>
            <p>Pick your device, then choose its firmware file: stock, or patched with digikit. Nothing is uploaded.</p>
          </header>
          <div class="devices">
            <For each={['dt2', 'dn2'] as const}>
              {(d) => (
                <button
                  type="button"
                  class={`devcard ${d}`}
                  style={{ '--accent': panels[d].accent, '--func-legend': panels[d].func_legend }}
                  onClick={() => {
                    setDevice(d);
                    setStage('firmware');
                  }}
                >
                  <span class="devcard-art" aria-hidden="true">
                    <PanelArt panel={panels[d]} />
                  </span>
                  <span class="devcard-meta">
                    <span>
                      <span class="devcard-name">{panels[d].name}</span>
                      <span class="accepts">
                        OS <For each={accepted(d)}>{(v) => <span class="ver">{v}</span>}</For>
                      </span>
                    </span>
                    <span class="go">
                      Choose
                      <svg viewBox="0 0 16 16" aria-hidden="true">
                        <path
                          d="M3 8H13M9 4L13 8L9 12"
                          fill="none"
                          stroke="currentColor"
                          stroke-width="2"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                        />
                      </svg>
                    </span>
                  </span>
                </button>
              )}
            </For>
          </div>
        </div>
      </Show>
      <Show when={stage() === 'firmware'}>
        <div class="wrap emu-fw">
          <button type="button" class="back" onClick={() => setStage('pick')}>
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path
                d="M13 8H3M7 4L3 8L7 12"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
            Change device
          </button>
          <h1>{panel().name} firmware</h1>
          <div
            class="emu-drop"
            classList={{ dragging: dragging() }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              void choose(e.dataTransfer?.files[0]);
            }}
          >
            <SyxFile />
            <strong>Drop a {panel().name} .syx here</strong>
            <span>
              OS {accepted(device()).join(' or ')}, stock or patched with digikit. Download stock firmware from
              Elektron's support site.
            </span>
            <label class="btn primary" for="emu-file">
              Choose file
            </label>
          </div>
          <p class="emu-alt">
            Want new machines first? <a href={`${base}patch/`}>Patch your firmware</a>, then run it here.
          </p>
        </div>
      </Show>
      <Show when={stage() === 'run'}>
        <div class="wrap runbar-wrap">
          <div class="runbar">
            <span class="dot" classList={{ paused: paused() || !loaded() }} aria-hidden="true" />
            <div class="who">
              <strong>{runningName()}</strong>
              <small>
                {fileName()}
                {phase() ? ` · ${phase()}` : ''}
              </small>
            </div>
            <Show when={!native()}>
              <span class="sound-note" title="The browser emulator has no audio yet. The desktop app plays it.">
                No sound yet
              </span>
            </Show>
            <button
              type="button"
              class="btn sm quiet"
              disabled={!loaded()}
              onClick={() => {
                if (paused()) {
                  runtime()?.resume();
                  setPaused(false);
                } else {
                  runtime()?.pause();
                  setPaused(true);
                }
              }}
            >
              {paused() ? 'Resume' : 'Pause'}
            </button>
            <button type="button" class="btn sm quiet" disabled={!loaded()} onClick={() => void restart()}>
              Restart
            </button>
            <button type="button" class="btn sm" onClick={changeFirmware}>
              Change firmware
            </button>
          </div>
          <Show when={!loaded() && !status().startsWith('Loading') && !status().startsWith('Choose')}>
            <p class="emu-error" role="alert">
              {status()}
            </p>
          </Show>
        </div>
      </Show>
      <section class="panel-wrap" hidden={stage() !== 'run'} aria-label={`${panel().name} front panel`}>
        <PanelArt
          panel={panel()}
          ledStyle={indicatorLight}
          keyLight={keyLight}
          isPressed={(code) => pressed().has(code)}
          angle={angle}
          onSvgPointerUp={(e) => releasePointer(e.pointerId)}
          onSvgPointerCancel={(e) => releasePointer(e.pointerId)}
          onSvgLostPointerCapture={(e) => {
            if (pointers.get(e.pointerId)?.code) releasePointer(e.pointerId);
          }}
          onKnobPointerDown={(knob, e) => {
            if (!knob.encoder) return;
            e.preventDefault();
            const pushed = e.shiftKey ? knob.push : undefined;
            if (pushed) emit(pushed, true);
            pointers.set(e.pointerId, {
              encoder: knob.encoder,
              y: e.clientY,
              pushed,
              moved: false,
              target: e.currentTarget,
            });
            detachDrag();
            window.addEventListener('pointermove', dragMove);
            window.addEventListener('pointerup', dragEnd);
            window.addEventListener('pointercancel', dragEnd);
          }}
          onKnobClick={(knob, e) => {
            if (suppressedKnob === e.currentTarget) {
              suppressedKnob = undefined;
              return;
            }
            if (knob.push) {
              emit(knob.push, true);
              // oxlint-disable-next-line solid/reactivity -- runs inside the knob's click handler (a PanelArt prop)
              queueMicrotask(() => emit(knob.push!, false));
            }
          }}
          onKnobWheel={(knob, e) => {
            if (!knob.encoder) return;
            e.preventDefault();
            const detents = e.shiftKey ? Math.sign(-e.deltaY) * 10 : Math.sign(-e.deltaY);
            if (detents) spin(knob.encoder, detents);
            runtime()?.turn(knob.encoder, detents);
          }}
          onKeyPointerDown={(key, e) => {
            if (key.code === FUNC) {
              funcLatched = !funcLatched;
              emit(FUNC, funcLatched);
              return;
            }
            e.currentTarget.setPointerCapture(e.pointerId);
            pointers.set(e.pointerId, { code: key.code, y: e.clientY, moved: false });
            emit(key.code, true);
          }}
        >
          <canvas
            ref={canvas}
            class="oled"
            width="128"
            height="64"
            aria-label="128 by 64 OLED display"
            style={{
              left: `${(panel().glass[0] / 215) * 100}%`,
              top: `${(panel().glass[1] / 176) * 100}%`,
              width: `${((panel().glass[2] - panel().glass[0]) / 215) * 100}%`,
              height: `${((panel().glass[3] - panel().glass[1]) / 176) * 100}%`,
            }}
          />
        </PanelArt>
      </section>
      <Show when={stage() === 'run'}>
        <div class="wrap emu-foot">
          <p class="hint">
            Click keys to press them. Drag or scroll knobs to turn them. Click FUNC, then a key, for a FUNC combination.
          </p>
          <Show when={dev || native()}>
            <details class="devtools" open={native()}>
              <summary>Developer tools</summary>
              <div class="devtools-row">
                <output class="status" aria-live="polite">
                  {status()}
                </output>
                <Show when={device() === 'dt2'}>
                  <button
                    class="samples"
                    disabled={!runtime()?.cards || !loaded() || cardBusy()}
                    title={
                      runtime()?.cards
                        ? 'Pick a folder of .wav files: track N plays file N (sorted by name)'
                        : 'Sample cards need the desktop app for now'
                    }
                    onClick={() => cardInput?.click()}
                  >
                    Choose samples
                  </button>
                  <input
                    ref={(el) => {
                      cardInput = el;
                      el.setAttribute('webkitdirectory', '');
                    }}
                    type="file"
                    multiple
                    hidden
                    onChange={(e) => void chooseSamples(e.currentTarget.files)}
                  />
                  <Show when={!runtime()?.cards}>
                    <small class="samples-note">desktop only for now</small>
                  </Show>
                </Show>
                <Show when={dev}>
                  <button disabled={!reportAvailable()} onClick={() => void exportDiagnostics()}>
                    Export diagnostics
                  </button>
                </Show>
              </div>
              <Show when={cardNote() || cardLine(cardStatus())}>
                <output class="card-status" aria-live="polite">
                  {cardNote() || cardLine(cardStatus())}
                </output>
              </Show>
              <Show when={dev && browserAudioSetup()}>
                <CoupledAudio
                  reset={browserAudioReset()}
                  firmware={chosenFirmware}
                  adopted={adopted}
                  runtime={() => runtimeReady ?? Promise.resolve(undefined)}
                  onStarted={(snapshot) => {
                    releaseAll();
                    clearCanvas();
                    update({ type: 'snapshot', snapshot });
                    setPaused(false);
                    setLoaded(true);
                  }}
                  onError={(error) => setStatus(error)}
                />
              </Show>
              <Show when={nativeAudio()}>
                {(audio) => (
                  <div
                    class="coupled"
                    style={{
                      display: 'flex',
                      'flex-wrap': 'wrap',
                      gap: '0.5rem',
                      'align-items': 'center',
                      padding: '0.4rem 0',
                    }}
                  >
                    <output>
                      {audio().setup_needed
                        ? 'Audio setup is unavailable for this firmware.'
                        : !audio().playback_requested
                          ? 'Audio output muted.'
                          : (audio().device_error ??
                            audio().sink?.error ??
                            (audio().sink_connected
                              ? audio().sink?.stream_started
                                ? audioLine(audio())
                                : 'Audio output ready; buffering.'
                              : 'Audio output is unavailable.'))}
                    </output>
                    <Show when={audio().load}>
                      {(load) => (
                        <output class="cpu-load" title={loadTitle(load())}>
                          {loadLine(load())}
                        </output>
                      )}
                    </Show>
                    <button
                      disabled={!loaded() || audio().setup_needed || !audio().flow_started}
                      onClick={() =>
                        void runtime()
                          ?.tap?.(25)
                          .catch((error) => update({ type: 'error', error: String(error) }))
                      }
                    >
                      Audition Trig 1
                    </button>
                  </div>
                )}
              </Show>
            </details>
          </Show>
        </div>
      </Show>
    </main>
  );
}
