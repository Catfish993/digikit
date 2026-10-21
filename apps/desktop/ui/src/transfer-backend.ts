// The desktop Transfer backend: the protocol client and the Transfer logic of apps/web/src/modules (TypeScript), over the controller's
// USB MIDI capture (`emu_call`, desktop-transport.ts). The only native part is the library folder of built modules (src-tauri/src/library.rs).
// Nothing is built here and no process is started: `mise run module:pack-all` (run by `mise run workbench`) fills the folder.
import { invoke } from '@tauri-apps/api/core';
import { DkSession, hexSha256, storePath } from '@web/modules/dkupload-core';
import {
  buildView,
  deleteFiles,
  install,
  libraryModule,
  restartDevice,
  type LibraryModule,
  type TransferEvent,
  type TransferView,
} from '@web/modules/dktransfer';
import { EmuUsbTransport, emulatorMode } from './desktop-transport';

export const PAUSED =
  'The emulator is paused: nothing would run the transfer. Resume it (the Run button of the workbench), then try again.';

let shared: DkSession | undefined;
/** One session for the window: its requests take turns on the one reply stream. */
const session = (): DkSession => (shared ??= new DkSession(new EmuUsbTransport(), { timeoutMs: 6000 }));

/** Every device operation first checks that the emulator runs. */
async function running(): Promise<DkSession> {
  if ((await emulatorMode()) === 'paused') throw new Error(PAUSED);
  return session();
}

/** The built modules of the device's folder, read again each time (a rebuild in a terminal shows up at the next Refresh). */
export async function loadLibrary(device: string): Promise<LibraryModule[]> {
  const files = await invoke<{ path: string; size: number }[]>('lib_scan', { device });
  const out: LibraryModule[] = [];
  for (const f of files) {
    const bytes = new Uint8Array(await invoke<ArrayBuffer>('lib_read', { device, path: f.path }));
    const m = await libraryModule(bytes);
    if (m) out.push(m);
  }
  return out;
}

export const transferBackend = {
  /** The merged list: the library folder and what the device holds (INFO, LIST, MODULES, CAPACITY, READ). */
  async list(): Promise<TransferView> {
    const s = await running();
    const info = await s.info();
    return buildView(s, await loadLibrary(info.port));
  },
  /** Upload and VERIFY the modules (the same sha256 is not sent again). The device's selection is not touched. */
  async install(modules: string[], onEvent: (e: TransferEvent) => void): Promise<void> {
    const s = await running();
    const info = await s.info();
    await install(s, modules, await loadLibrary(info.port), onEvent);
  },
  /** DELETE the files from the card (one request each, confirmed with LIST). The device's selection is not touched. */
  async remove(paths: string[], onEvent: (e: TransferEvent) => void): Promise<void> {
    await deleteFiles(await running(), paths, onEvent);
  },
  /** RESTART the emulated device, wait for the boot and report what loaded. */
  async restart(onEvent: (e: TransferEvent) => void): Promise<void> {
    await restartDevice(await running(), onEvent);
  },
  /** Put .dkm files from outside the library on the card (the same sha256 is not sent again); returns what was done, one line each. */
  async add(files: { name: string; data: Uint8Array }[]): Promise<string[]> {
    const s = await running();
    const onCard = new Map((await s.list()).map((e) => [e.path, e.sha256]));
    const lines: string[] = [];
    for (const f of files) {
      const path = storePath(f.name);
      if (onCard.get(path) === (await hexSha256(f.data))) {
        lines.push(`${path}: already on the card`);
        continue;
      }
      await s.put(path, f.data);
      lines.push(`Added ${path}`);
    }
    return lines;
  },
};
