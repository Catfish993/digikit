// The Transfer page's state, kept for the life of the window: closing the page and opening it again shows the last list at once
// (it is refreshed behind it), and an install's verify results and selection stay until the next list says otherwise.
import { createSignal } from 'solid-js';
import type { Progress, Selected, TransferView, Verified } from './transfer-core';

export const [lastView, setLastView] = createSignal<TransferView>();
export const [ticks, setTicks] = createSignal<Set<string>>(new Set());
/** What the last install selected; the loaded modules are not that until a restart. Cleared by a restart. */
export const [selected, setSelected] = createSignal<Selected>();
export const [verified, setVerified] = createSignal<Verified>({});
export const [progress, setProgress] = createSignal<Progress>();
export const [showTests, setShowTests] = createSignal(false);
export const [log, setLog] = createSignal<string[]>([]);
