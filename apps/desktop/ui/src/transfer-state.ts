// The Transfer page's state, kept for the life of the window: closing the page and opening it again shows the last list at once
// (it is refreshed behind it), and an install's verify results stay until the next restart.
import { createSignal } from 'solid-js';
import type { Prepared } from '@web/modules/dksample';
import type { SampleEntry } from '@web/modules/samples';
import type { LocalEntry, Uploads } from './samples-core';
import { parseMode, type Progress, type TransferView, type Verified } from './transfer-core';

export interface DeviceInfo {
  name: string;
  runtime: string;
  free?: number;
  samples: string;
  canMkdir: boolean;
}

export const [lastView, setLastView] = createSignal<TransferView>();
/** The library modules ticked for install. */
export const [ticks, setTicks] = createSignal<Set<string>>(new Set());
/** The store paths ticked in the On device pane (for Delete). */
export const [delTicks, setDelTicks] = createSignal<Set<string>>(new Set());
export const [verified, setVerified] = createSignal<Verified>({});
export const [progress, setProgress] = createSignal<Progress>();
export const [showTests, setShowTests] = createSignal(false);
export const [log, setLog] = createSignal<string[]>([]);

/** A value kept across restarts of the app in localStorage (a private window or blocked storage: it lasts until the window closes). */
function remembered<T extends string>(key: string, read: (v: string | null) => T): [() => T, (v: T) => void] {
  let first: string | null = null;
  try {
    first = localStorage.getItem(key);
  } catch {
    // nothing remembered
  }
  const [value, set] = createSignal<T>(read(first));
  return [
    value,
    (v) => {
      set(() => v);
      try {
        if (v) localStorage.setItem(key, v);
        else localStorage.removeItem(key);
      } catch {
        // the choice lasts until the window closes
      }
    },
  ];
}

/** The mode of the page (Modules or Samples). */
export const [mode, setMode] = remembered('digikit.transferMode', parseMode);
/** The folder on the computer that Samples mode lists ('' until one is chosen). */
export const [localFolder, setLocalFolder] = remembered('digikit.samplesFolder', (v) => v ?? '');

/** What the On device column says about the device: its name (the profile's), the runtime, the free space of what the mode shows, and the
 * samples backend; `canMkdir` is known after the samples backend was opened. */
export const [deviceInfo, setDeviceInfo] = createSignal<DeviceInfo>();

/** Samples mode, On your computer: the open level of the chosen folder, its entries, what each file will become, the files added by hand,
 * and the ticks. */
export const [localPath, setLocalPath] = createSignal('');
export const [localEntries, setLocalEntries] = createSignal<LocalEntry[]>();
export const [localKinds, setLocalKinds] = createSignal<Record<string, Prepared['kind']>>({});
export const [addedFiles, setAddedFiles] = createSignal<File[]>([]);
export const [localTicks, setLocalTicks] = createSignal<Set<string>>(new Set());

/** Samples mode, On device: the open folder ('' is the top), its listing, the ticked paths, and the uploads of this window. */
export const [sampleFolder, setSampleFolder] = createSignal('');
export const [sampleEntries, setSampleEntries] = createSignal<SampleEntry[]>();
export const [sampleTicks, setSampleTicks] = createSignal<Set<string>>(new Set());
export const [uploads, setUploads] = createSignal<Uploads>({});
