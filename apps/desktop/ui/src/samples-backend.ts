// The desktop Samples backend: the samples API of apps/web/src/modules over the selected target (target.ts), with the backend the device profile
// names (`[storage] samples`, read by the native `device_storage`; the page names no device). The converter is `dksample.wasm`, loaded once through
// the native `sample_wasm`. Nothing is started and nothing is built here.
import { invoke } from '@tauri-apps/api/core';
import type { DkSession } from '@web/modules/dkupload-core';
import { Sample, prepareUpload } from '@web/modules/dksample';
import { openSamples, type SamplesBackend } from '@web/modules/samples';
import { session as running } from './target';
import type { Prepare } from './samples-core';

/** The device behind the target: its name, the backend of its samples, and the open backend. */
export interface OpenedSamples {
  name: string;
  samples: string;
  backend: SamplesBackend;
  prepare: Prepare;
}

let sample: Promise<Sample> | undefined;
/** The converter, made once (a failed load is tried again at the next use). */
function converter(): Promise<Sample> {
  sample ??= invoke<ArrayBuffer>('sample_wasm')
    .then((b) => Sample.create(b))
    .catch((e) => {
      sample = undefined;
      throw e;
    });
  return sample;
}

let last: { s: DkSession; opened: OpenedSamples } | undefined;

/** The samples backend of the device on the target. One per session: the backend keeps what INFO said. */
export async function openDeviceSamples(): Promise<OpenedSamples> {
  const s = await running();
  if (last?.s === s) return last.opened;
  const device = (await s.info()).port;
  const storage = await invoke<{ name: string; samples: string }>('device_storage', { device });
  const conv = await converter();
  const opened: OpenedSamples = {
    name: storage.name,
    samples: storage.samples,
    backend: openSamples(storage.samples, { session: s, hashOf: (d) => conv.hash(d) }),
    prepare: (name, bytes) => prepareUpload(conv, name, bytes),
  };
  last = { s, opened };
  return opened;
}
