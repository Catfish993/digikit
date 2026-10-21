// The Transfer page's state, kept for the life of the window: closing the page and opening it again shows the last list at once
// (it is refreshed behind it), and an install's verify results stay until the next restart.
import { createSignal } from 'solid-js';
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
