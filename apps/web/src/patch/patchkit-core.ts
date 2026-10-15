// A thin, host-neutral wrapper over patchkit.wasm (packages/patchkit/src/wasm.rs). Used by the patch
// worker in the browser and by the node tests, so both run the same code. Everything stays in memory:
// the firmware is never sent anywhere. Written without TS-only runtime syntax so node can import it.

export interface CheckModule { id: string; ok: boolean; error: string | null }
export interface CheckResult { device: string; sha256: string; port: string | null; modules: CheckModule[] }
export interface PatchPlan {
  base: { device: string; version: string; syx_sha256: string };
  modules: string[];
  writes: { cpu: string; addr: number; len: number; kind: string; owner: string; what: string }[];
  regions: Record<string, [number, number]>;
  record: { modules: string[] };
  output?: { syx_sha256: string; syx_bytes: number; container_bytes: number; changed_sections: [number, number, number][] };
}
export interface VerifyReport { device: string; container_bytes: number; data_messages: number; sections: [number, number, number][]; record: { modules: string[] } | null; port: string | null; checks: string[] }
export interface PatchResult { syx: Uint8Array; plan: PatchPlan; verify: VerifyReport; sha256: string }

interface Exports {
  memory: WebAssembly.Memory;
  pk_alloc(n: number): number;
  pk_free(p: number, n: number): void;
  pk_apply(...a: number[]): number;
  pk_check(...a: number[]): number;
  pk_verify(...a: number[]): number;
  pk_out_ptr(): number; pk_out_len(): number;
  pk_plan_ptr(): number; pk_plan_len(): number;
  pk_err_ptr(): number; pk_err_len(): number;
}

export class PatchKit {
  private x: Exports;
  private bundle: Uint8Array;
  private constructor(x: Exports, bundle: Uint8Array) { this.x = x; this.bundle = bundle; }

  static async create(wasm: BufferSource, bundleJson: string | Uint8Array): Promise<PatchKit> {
    const { instance } = await WebAssembly.instantiate(wasm, {});
    const bundle = typeof bundleJson === 'string' ? new TextEncoder().encode(bundleJson) : bundleJson;
    return new PatchKit(instance.exports as unknown as Exports, bundle);
  }

  private put(bytes: Uint8Array): [number, number] {
    const p = this.x.pk_alloc(bytes.length);
    new Uint8Array(this.x.memory.buffer, p, bytes.length).set(bytes);
    return [p, bytes.length];
  }
  private free(blocks: [number, number][]) { for (const [p, n] of blocks) this.x.pk_free(p, n); }
  private get(ptr: number, len: number): Uint8Array { return new Uint8Array(this.x.memory.buffer.slice(ptr, ptr + len)); }
  private text(ptr: number, len: number): string { return new TextDecoder().decode(this.get(ptr, len)); }
  private plan(): string { return this.text(this.x.pk_plan_ptr(), this.x.pk_plan_len()); }
  private fail(): never { throw new Error(this.text(this.x.pk_err_ptr(), this.x.pk_err_len())); }

  /** Which bundle modules can be used on this image (a read-only pass over the manifests and the port). */
  check(firmware: Uint8Array): CheckResult {
    const m = [this.put(firmware), this.put(this.bundle)];
    try {
      if (this.x.pk_check(m[0][0], m[0][1], m[1][0], m[1][1]) !== 0) this.fail();
      return JSON.parse(this.plan()) as CheckResult;
    } finally { this.free(m); }
  }

  /** Apply the modules (patchkit orders them) and verify the result against the stock image. Throws patchkit's error text. */
  apply(firmware: Uint8Array, ids: string[]): PatchResult {
    const m = [this.put(firmware), this.put(this.bundle), this.put(new TextEncoder().encode(ids.join(',')))];
    try {
      if (this.x.pk_apply(m[0][0], m[0][1], m[1][0], m[1][1], m[2][0], m[2][1]) !== 0) this.fail();
      const syx = this.get(this.x.pk_out_ptr(), this.x.pk_out_len());
      const plan = JSON.parse(this.plan()) as PatchPlan;
      const v = [this.put(syx), this.put(firmware)];
      try {
        if (this.x.pk_verify(v[0][0], v[0][1], m[1][0], m[1][1], v[1][0], v[1][1]) !== 0) this.fail();
        const verify = JSON.parse(this.plan()) as VerifyReport;
        return { syx, plan, verify, sha256: plan.output?.syx_sha256 ?? '' };
      } finally { this.free(v); }
    } finally { this.free(m); }
  }
}

/** The file name of a patched image: `Digitakt_II_OS1.17+fx-master-gain.syx`. */
export function patchedFileName(deviceName: string, version: string, ids: string[]): string {
  const base = `${deviceName.replace(/\s+/g, '_')}_OS${version}`;
  return `${base}+${ids.map((id) => id.replace(/\//g, '-')).join('+')}.syx`;
}
