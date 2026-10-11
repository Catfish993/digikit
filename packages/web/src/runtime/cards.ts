// Sample cards for the Digitakt II (desktop app only for now): the files of a chosen folder go to the
// native controller, which builds a +Drive card in process, boots the DT2 with it and reports progress.

/** What the controller's `card.status` reports. */
export interface CardStatus {
  state: 'idle' | 'working' | 'ready' | 'error';
  phase?: 'scanning' | 'building' | 'booting' | 'restoring' | 'ready';
  message?: string;
  /** 0..1 */
  fraction?: number;
  samples?: number;
  card_cached?: boolean;
  ready_cached?: boolean;
  seconds?: number;
}

/** The files `<input webkitdirectory>` returns that sit directly in the chosen folder and end in `.wav`. */
export function topLevelWavs(files: Iterable<File>): File[] {
  const out: File[] = [];
  for (const file of files) {
    const parts = (file.webkitRelativePath || file.name).split('/');
    const direct = parts.length <= 2;
    if (direct && !file.name.startsWith('.') && file.name.toLowerCase().endsWith('.wav')) out.push(file);
  }
  return out.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

/** One line for the toolbar. */
export function cardLine(status: CardStatus | undefined): string {
  if (!status || status.state === 'idle') return '';
  if (status.state === 'error') return `Card failed: ${status.message ?? 'unknown error'}`;
  const percent = Math.round(100 * (status.fraction ?? 0));
  if (status.state === 'ready') {
    const note = status.ready_cached ? ' (cached)' : '';
    return `Card ready${note}: ${status.samples ?? '?'} sample${status.samples === 1 ? '' : 's'}`;
  }
  return `${status.message ?? 'Working'}${status.message?.includes('%') ? '' : ` (${percent}%)`}`;
}

/** The native bridge: stage the files, start the job, poll its status. */
export interface CardBridge {
  reset(): Promise<void>;
  stage(name: string, bytes: Uint8Array): Promise<void>;
  load(): Promise<unknown>;
  status(): Promise<CardStatus>;
}

/** Send `files` to the controller and start the card job; returns when the job has started. */
export async function startCard(bridge: CardBridge, files: File[], onStaged?: (done: number, total: number) => void): Promise<void> {
  await bridge.reset();
  let done = 0;
  for (const file of files) {
    await bridge.stage(file.name, new Uint8Array(await file.arrayBuffer()));
    onStaged?.(++done, files.length);
  }
  await bridge.load();
}
