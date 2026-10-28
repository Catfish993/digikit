// The workbench log: one list for the window, written by every part of the app (`logEvent`) and shown by the Log drawer in Console.tsx.
// In memory only; the rules are in log-core.ts.
import { createSignal } from 'solid-js';
import { addEntry, autoOpens, clearEntries, emptyLog, type LogInput, type LogLevel, type LogSource } from './log-core';

export const [logState, setLogState] = createSignal(emptyLog());
/** The drawer is open. An error that `autoOpens` sets it. */
export const [logOpen, setLogOpen] = createSignal(false);

export function logInput(input: LogInput): void {
  setLogState((s) => addEntry(s, input, Date.now()));
  if (autoOpens(input)) setLogOpen(true);
}
export const logEvent = (source: LogSource, level: LogLevel, text: string, detail?: string): void =>
  logInput({ source, level, text, ...(detail ? { detail } : {}) });
export const clearLog = (): void => {
  setLogState(clearEntries);
};
