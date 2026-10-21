// The desktop Transfer backend: the commands of src-tauri/src/transfer.rs, which run tools/device/dktransfer.py against this
// workbench's control socket. A job (install, restart) is polled for the tool's JSON lines with a cursor (like `mod_progress`).
import { invoke } from '@tauri-apps/api/core';
import type { InstallRequest, TransferEvent, TransferView } from './transfer-core';

interface Events {
  events: TransferEvent[];
  next: number;
  running: boolean;
}

/** Poll the running job until it ends; calls `onEvent` for each line and rejects with the tool's error message. */
async function follow(onEvent: (e: TransferEvent) => void): Promise<TransferEvent[]> {
  const all: TransferEvent[] = [];
  let since = 0;
  for (;;) {
    await new Promise((r) => window.setTimeout(r, 250));
    let ev: Events;
    try {
      ev = await invoke<Events>('xfer_events', { since });
    } catch {
      continue; // the next poll
    }
    since = ev.next;
    for (const e of ev.events) {
      all.push(e);
      onEvent(e);
      if (e.event === 'error') throw new Error(String(e.message));
    }
    if (!ev.running) return all;
  }
}

export const transferBackend = {
  list: () => invoke<TransferView>('xfer_list'),
  /** Build, upload, verify and select; the modules load at the next restart. */
  async install(req: InstallRequest, onEvent: (e: TransferEvent) => void): Promise<TransferEvent[]> {
    await invoke('xfer_install', { modules: req.modules, keep: req.keep });
    return follow(onEvent);
  },
  /** RESTART the emulated device, wait for the boot and report which of `modules` loaded. */
  async restart(modules: string[], onEvent: (e: TransferEvent) => void): Promise<TransferEvent[]> {
    await invoke('xfer_restart', { modules });
    return follow(onEvent);
  },
};
