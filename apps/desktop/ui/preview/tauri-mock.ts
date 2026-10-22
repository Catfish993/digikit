// A stand-in for `@tauri-apps/api/core` in the preview: just enough for the target selector and a real `DkSession` over the emulator's
// USB capture (a device that never answers: the "no reply" state). Everything else the pages ask of Tauri is answered by the two backend mocks.
import { cardCall, pickFolder } from './card-mock';
import { sim } from './sim';

export async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  switch (cmd) {
    case 'local_pick_folder':
      return (await pickFolder()) as T;
    case 'card_open_folder':
      return undefined as T;
    case 'midi_ports':
      return ['Elektron Digitakt II', 'IAC Driver Bus 1'] as T;
    case 'midi_open':
    case 'midi_close':
    case 'midi_cancel':
      return undefined as T;
    case 'emu_call': {
      const method = String(args?.method);
      if (method === 'status') return { mode: sim.paused ? 'paused' : 'running' } as T;
      if (method.startsWith('card.'))
        return (await cardCall(method, (args?.params as Record<string, unknown>) ?? {})) as T;
      return {} as T; // midi.usb.in / midi.usb.out: nothing is ever answered
    }
    default:
      throw new Error(`preview: no mock for the Tauri command ${cmd}`);
  }
}
