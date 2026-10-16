// Runs patchkit.wasm off the main thread. Messages in: init, check, apply; out: ready, checked, patched, error.
// The firmware arrives as an ArrayBuffer and never leaves this page: there is no network call here except
// fetching the (firmware-free) wasm and module bundle that the site itself serves.
import { PatchKit } from './patchkit-core.ts';

export type PatchRequest =
  | { type: 'init'; wasmUrl: string; bundleUrl: string }
  | { type: 'check'; id: number; firmware: ArrayBuffer }
  | { type: 'apply'; id: number; firmware: ArrayBuffer; modules: string[] };

let kit: Promise<PatchKit> | undefined;
const post = (message: unknown, transfer: Transferable[] = []) =>
  (self as unknown as Worker).postMessage(message, transfer);

self.onmessage = async ({ data }: MessageEvent<PatchRequest>) => {
  try {
    if (data.type === 'init') {
      kit = (async () => {
        const [wasm, bundle] = await Promise.all([fetch(data.wasmUrl), fetch(data.bundleUrl)]);
        if (!wasm.ok || !bundle.ok) throw new Error('patch tool files are missing from this site');
        return PatchKit.create(await wasm.arrayBuffer(), await bundle.text());
      })();
      await kit;
      post({ type: 'ready' });
      return;
    }
    const k = await kit;
    if (!k) throw new Error('patch worker is not initialised');
    const firmware = new Uint8Array(data.firmware);
    if (data.type === 'check') {
      post({ type: 'checked', id: data.id, result: k.check(firmware) });
    } else {
      const r = k.apply(firmware, data.modules);
      post({ type: 'patched', id: data.id, plan: r.plan, verify: r.verify, sha256: r.sha256, syx: r.syx.buffer }, [
        r.syx.buffer,
      ]);
    }
  } catch (error) {
    post({
      type: 'error',
      id: (data as { id?: number }).id,
      error: error instanceof Error ? error.message : String(error),
    });
  }
};
