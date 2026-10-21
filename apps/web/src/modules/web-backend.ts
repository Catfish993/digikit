// The site's backend for the Modules panel: Web MIDI to a device through dkupload.wasm.
import { Dk, DkClient } from './dkupload-core.ts';
import type { ModuleBackend } from './backend.ts';
import { WebMidiTransport, type MidiInputLike, type MidiOutputLike } from './webmidi.ts';

export function clientBackend(client: DkClient, label: string): ModuleBackend {
  return {
    label,
    info: (quick) => client.info(quick),
    list: () => client.list(),
    put: (path, data, onProgress) => client.put(path, data, onProgress),
    remove: (path) => client.delete(path),
    clear: () => client.clear(),
  };
}

export async function openWebMidi(
  wasmUrl: string,
  input: MidiInputLike,
  output: MidiOutputLike,
  label: string,
): Promise<{ backend: ModuleBackend; close: () => void }> {
  const [wasm, transport] = await Promise.all([
    fetch(wasmUrl).then((r) => r.arrayBuffer()),
    WebMidiTransport.open(input, output),
  ]);
  const client = new DkClient(await Dk.create(wasm), transport, { timeoutMs: 3000 });
  return { backend: clientBackend(client, label), close: () => transport.close() };
}
