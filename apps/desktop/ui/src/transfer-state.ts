// The Transfer page's state, kept for the life of the window: closing the page and opening it again shows the last list at once
// (it is refreshed behind it), and an install's verify results stay until the next restart.
import { createSignal } from 'solid-js';
import type { SampleEntry, SampleSpace } from '@web/modules/samples';
import { parseTab, type TabId, type Uploads } from './samples-core';
import type { Progress, TransferView, Verified } from './transfer-core';

export const [lastView, setLastView] = createSignal<TransferView>();
/** The library modules ticked for install. */
export const [ticks, setTicks] = createSignal<Set<string>>(new Set());
/** The store paths ticked in the On device pane (for Delete). */
export const [delTicks, setDelTicks] = createSignal<Set<string>>(new Set());
export const [verified, setVerified] = createSignal<Verified>({});
export const [progress, setProgress] = createSignal<Progress>();
export const [showTests, setShowTests] = createSignal(false);
export const [log, setLog] = createSignal<string[]>([]);

const TAB_KEY = 'digikit.transferTab';
const readTab = (): TabId => {
  try {
    return parseTab(localStorage.getItem(TAB_KEY));
  } catch {
    return 'modules';
  }
};
/** The tab of the page (kept across restarts of the app). */
export const [tab, setTabSignal] = createSignal<TabId>(readTab());
export const setTab = (t: TabId) => {
  setTabSignal(t);
  try {
    localStorage.setItem(TAB_KEY, t);
  } catch {
    // the choice lasts until the window closes
  }
};

/** The Samples tab: the open folder ('' is the top), its listing, the free space, the ticked paths, and the uploads of this window. */
export const [sampleFolder, setSampleFolder] = createSignal('');
export const [sampleEntries, setSampleEntries] = createSignal<SampleEntry[]>();
export const [sampleSpace, setSampleSpace] = createSignal<SampleSpace>();
export const [sampleTicks, setSampleTicks] = createSignal<Set<string>>(new Set());
export const [uploads, setUploads] = createSignal<Uploads>({});
