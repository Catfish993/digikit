// The desktop app's Modules backend: the native commands (src-tauri/src/modules.rs) that talk to the running
// emulator's USB MIDI path. A transfer reports progress through `mod_progress`, polled while `mod_put` runs.
import { invoke } from '@tauri-apps/api/core';
import type { ModuleBackend } from '@web/modules/backend';
import type { DkEntry, DkInfo } from '@web/modules/dkupload-core';

export const emulatorModules: ModuleBackend = {
  label: 'Emulator',
  info: () => invoke<DkInfo>('mod_info'),
  list: () => invoke<DkEntry[]>('mod_list'),
  remove: (path) => invoke('mod_delete', { path }),
  clear: () => invoke('mod_clear'),
  async put(path, data, onProgress) {
    let stop = false;
    const poll = window.setInterval(async () => {
      try {
        const p = await invoke<{ running: boolean; done: number; total: number }>('mod_progress');
        if (p.running && onProgress({ done: p.done, total: p.total }) === false && !stop) {
          stop = true;
          await invoke('mod_cancel');
        }
      } catch {
        /* the next poll */
      }
    }, 150);
    try {
      await invoke('mod_put', data, { headers: { 'x-path': encodeURIComponent(path) } });
      onProgress({ done: data.length, total: data.length });
    } finally {
      window.clearInterval(poll);
    }
  },
};
