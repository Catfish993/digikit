// The upload page: pick a MIDI input and output (Web MIDI with SysEx permission) and manage the module store of a
// connected Digitakt II or Digitone II. Nothing is sent until a port pair is opened.
import { For, Show, createSignal, onCleanup, type JSX } from 'solid-js';
import {
  requestAccess,
  listPorts,
  portLabel,
  type MidiAccessLike,
  type MidiInputLike,
  type MidiOutputLike,
} from '../modules/webmidi.ts';
import { openWebMidi } from '../modules/web-backend.ts';
import type { ModuleBackend } from '../modules/backend.ts';
import ModulesPanel from './ModulesPanel';
import './upload-page.css';

const base = import.meta.env.BASE_URL.replace(/\/?$/, '/');
const likely = (p: { name?: string | null }) => /digitakt|digitone/i.test(p.name ?? '');

export default function UploadPage(): JSX.Element {
  const [access, setAccess] = createSignal<MidiAccessLike>();
  const [ports, setPorts] = createSignal<{ inputs: MidiInputLike[]; outputs: MidiOutputLike[] }>({
    inputs: [],
    outputs: [],
  });
  const [inId, setInId] = createSignal('');
  const [outId, setOutId] = createSignal('');
  const [backend, setBackend] = createSignal<ModuleBackend>();
  const [error, setError] = createSignal('');
  let close: (() => void) | undefined;
  onCleanup(() => close?.());

  const connectMidi = async () => {
    setError('');
    try {
      const a = await requestAccess();
      setAccess(a);
      const p = listPorts(a);
      setPorts(p);
      setInId(p.inputs.find(likely)?.id ?? p.inputs[0]?.id ?? '');
      setOutId(p.outputs.find(likely)?.id ?? p.outputs[0]?.id ?? '');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const open = async () => {
    const p = ports();
    const input = p.inputs.find((x) => x.id === inId());
    const output = p.outputs.find((x) => x.id === outId());
    if (!input || !output) return;
    close?.();
    setBackend(undefined);
    try {
      const r = await openWebMidi(`${base}dkupload.wasm`, input, output, portLabel(output));
      close = r.close;
      setBackend(r.backend);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div class="upload">
      <h1>Upload modules</h1>
      <p class="lead">
        Send <code>.dkm</code> files to the module store of a Digitakt II or Digitone II running digikit, over USB MIDI.
        Uploaded modules appear in DIGIKIT &gt; MODULES and load after APPLY &amp; RESTART.
      </p>
      <section class="ports" aria-label="MIDI ports">
        <Show
          when={access()}
          fallback={
            <button type="button" class="act primary" onClick={() => void connectMidi()}>
              Allow MIDI access…
            </button>
          }
        >
          <label>
            Input
            <select value={inId()} onChange={(e) => setInId(e.currentTarget.value)}>
              <For each={ports().inputs}>{(p) => <option value={p.id}>{portLabel(p)}</option>}</For>
            </select>
          </label>
          <label>
            Output
            <select value={outId()} onChange={(e) => setOutId(e.currentTarget.value)}>
              <For each={ports().outputs}>{(p) => <option value={p.id}>{portLabel(p)}</option>}</For>
            </select>
          </label>
          <button type="button" class="act primary" disabled={!inId() || !outId()} onClick={() => void open()}>
            Open
          </button>
          <Show when={!ports().inputs.length || !ports().outputs.length}>
            <p class="none">No MIDI ports found. Connect the device by USB and set its USB mode to MIDI.</p>
          </Show>
        </Show>
      </section>
      <Show when={error()}>
        <p class="error" role="alert">
          {error()}
        </p>
      </Show>
      <Show when={backend()}>
        {(b) => <ModulesPanel backend={b()} hint="Files go to modules/ in the device store." />}
      </Show>
    </div>
  );
}
