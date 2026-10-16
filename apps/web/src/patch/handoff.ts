// Hand-off of a patched firmware from /patch to the emulator page, inside this browser only:
// the bytes go into IndexedDB (too big for sessionStorage) and the emulator page takes and deletes them.
// Nothing is sent over the network.
const DB = 'digikit-handoff',
  STORE = 'firmware',
  KEY = 'pending';

export interface Handoff {
  name: string;
  device?: string;
  bytes: ArrayBuffer;
}

const open = () =>
  new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('IndexedDB is not available'));
  });

const run = async <T>(mode: IDBTransactionMode, work: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> => {
  const db = await open();
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const request = work(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB write failed'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB aborted'));
    });
  } finally {
    db.close();
  }
};

export const putHandoff = async (value: Handoff): Promise<void> => {
  await run('readwrite', (s) => s.put(value, KEY));
};

/** Reads and deletes the pending hand-off (undefined when there is none). */
export async function takeHandoff(): Promise<Handoff | undefined> {
  const value = await run<Handoff | undefined>('readonly', (s) => s.get(KEY));
  if (value) await run('readwrite', (s) => s.delete(KEY));
  return value;
}
