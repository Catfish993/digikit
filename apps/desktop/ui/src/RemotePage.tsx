// The Remote page (desktop): the front panel of the selected target with its real screen, the keys and the encoders working on the device.
// The screen is the firmware's own Screenshot (stock MidiRpc 0x04), polled; keys and encoders are the commands of digikit/remote (KEY, TURN),
// which work only while DIGIKIT > REMOTE is on at the device. The page speaks the protocol itself in TypeScript over the target of
// target.ts: the emulator's USB capture or a MIDI port. No process is started.
import { For, Show, createSignal, onCleanup, onMount, type JSX } from 'solid-js';
import LivePanel, { type LivePanelApi } from '@web/components/LivePanel';
import { panels } from '@web/data/faceplates';
import { errorLine } from '@web/modules/backend';
import type { DkSession, RemoteState } from '@web/modules/dkupload-core';
import { deviceOfPort, remoteNotice, remoteRuntime } from './remote-core';
import TargetSelect from './TargetSelect';
import { cancelPending, resetSession, session, target, targetLabel } from './target';
import { failureLine } from './wait-core';
import './remote-page.css';

const SCREEN_MS = { emulator: 250, midi: 400 };
const STATE_MS = 2000;

export default function RemotePage(props: { fallbackDevice: 'dt2' | 'dn2' }): JSX.Element {
  const [state, setState] = createSignal<RemoteState>();
  const [stateError, setStateError] = createSignal<unknown>();
  const [screenError, setScreenError] = createSignal('');
  // oxlint-disable-next-line solid/reactivity -- the emulator's device is only the first guess; the target's INFO names the device
  const [device, setDevice] = createSignal<'dt2' | 'dn2'>(props.fallbackDevice);
  let live: LivePanelApi | undefined;
  let alive = true;
  const panel = () => panels[device()];
  const runtime = remoteRuntime(() => session());
  const notice = () => remoteNotice(state(), stateError());

  /** Screen loop: one Screenshot after the other; an error shows and the loop tries again slowly. */
  const screenLoop = async () => {
    while (alive) {
      const t = target();
      try {
        const s: DkSession = await session();
        const shot = await s.screen();
        live?.showFrame(shot);
        setScreenError('');
        await new Promise((r) => setTimeout(r, SCREEN_MS[t.kind]));
      } catch (e) {
        setScreenError(failureLine(e, targetLabel(target())));
        resetSession();
        await new Promise((r) => setTimeout(r, 1500));
      }
    }
  };
  const stateLoop = async () => {
    while (alive) {
      try {
        const s = await session();
        const info = await s.info(true);
        const d = deviceOfPort(info.port);
        if (d && d !== device()) setDevice(d);
        setState(await s.remoteState());
        setStateError(undefined);
      } catch (e) {
        setState(undefined);
        setStateError(e);
      }
      await new Promise((r) => setTimeout(r, STATE_MS));
    }
  };
  onMount(() => {
    void screenLoop();
    void stateLoop();
  });
  onCleanup(() => {
    alive = false;
    // a poll that waits for a silent device must not hold up the next page's requests (they take turns on the session)
    cancelPending();
    live?.releaseAll();
    // let go of the keys the page pressed (the module remembers them)
    if (state())
      void session()
        .then((s) => s.remoteRelease())
        .catch(() => {});
  });

  const encoders = () => panel().knobs.filter((k) => k.encoder !== undefined);
  const turn = (encoder: number, detents: number) =>
    void runtime.turn(encoder, detents).catch((e) => setScreenError(errorLine(e)));

  return (
    <section class="remote-page" aria-label="Remote">
      <header class="head">
        <h2>Remote</h2>
        <span class="meta">
          {targetLabel(target())} · {device()}
        </span>
        <span class="sp" />
        <TargetSelect onError={setScreenError} />
      </header>
      <Show when={notice()}>
        <p class="note" role="status">
          {notice()}
        </p>
      </Show>
      <Show when={screenError()}>
        <p class="err" role="alert">
          {screenError()}
        </p>
      </Show>
      <div class="stage">
        <LivePanel panel={panel()} runtime={() => runtime} onError={setScreenError} ref={(api) => (live = api)} />
      </div>
      <div class="encoders" aria-label="Encoders">
        <For each={encoders()}>
          {(k) => (
            <span class="enc">
              <span class="dim">{k.label}</span>
              <button type="button" class="act small" title={`${k.label} down`} onClick={() => turn(k.encoder!, -1)}>
                −
              </button>
              <button type="button" class="act small" title={`${k.label} up`} onClick={() => turn(k.encoder!, 1)}>
                +
              </button>
            </span>
          )}
        </For>
      </div>
      <p class="hint dim">
        Click a key to press it; drag or scroll on a knob to turn it (Shift: ten detents, and the knob's push key). The
        screen is the device's own.
      </p>
    </section>
  );
}
