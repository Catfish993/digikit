// The target selector: the emulator, or a MIDI port with a device on it (Rust midir, midi_port.rs). Used in the headers of the Transfer and the
// Remote pages. The list of ports is read when the page opens and with the rescan button; the port in use stays in the list.
import { For, Show, createSignal, onMount, type JSX } from 'solid-js';
import { errorLine } from '@web/modules/backend';
import { listMidiPorts } from './midi-transport';
import { selectTarget, target, targetLabel, type Target } from './target';

const VALUE_EMU = 'emulator';

export default function TargetSelect(props: { disabled?: boolean; onError?: (m: string) => void }): JSX.Element {
  const [ports, setPorts] = createSignal<string[]>([]);
  const rescan = async () => {
    try {
      setPorts(await listMidiPorts());
    } catch (e) {
      props.onError?.(errorLine(e));
    }
  };
  onMount(() => void rescan());
  const options = () => {
    const t = target();
    const names = ports();
    return t.kind === 'midi' && !names.includes(t.port) ? [t.port, ...names] : names;
  };
  const pick = async (value: string) => {
    const next: Target = value === VALUE_EMU ? { kind: 'emulator' } : { kind: 'midi', port: value };
    try {
      await selectTarget(next);
    } catch (e) {
      props.onError?.(errorLine(e));
    }
  };
  return (
    <span class="target-select">
      <label>
        <span class="dim">Target</span>{' '}
        <select
          aria-label="Target"
          disabled={props.disabled}
          value={target().kind === 'emulator' ? VALUE_EMU : (target() as { port: string }).port}
          onChange={(e) => void pick(e.currentTarget.value)}
        >
          <option value={VALUE_EMU}>Emulator</option>
          <For each={options()}>
            {(name) => <option value={name}>{targetLabel({ kind: 'midi', port: name })}</option>}
          </For>
        </select>
      </label>
      <button
        type="button"
        class="act small"
        disabled={props.disabled}
        title="Look for MIDI ports again"
        onClick={() => void rescan()}
      >
        Rescan
      </button>
      <Show when={target().kind === 'midi'}>
        <span class="dim">a real device</span>
      </Show>
    </span>
  );
}
