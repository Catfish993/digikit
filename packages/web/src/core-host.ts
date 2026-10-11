// Instantiates the browser core (`elektron_native_boot.wasm` built with the
// `sharc` and `fast-wasm` features) and provides what it imports:
//
// * `host_wasm_compile/run/free`: the DSP fast tier emits one wasm module per
//   region (native/sharc-fast-wasm); the kernels address the core's own
//   linear memory, so each is instantiated over it;
// * whatever the caller adds (`host_dspi_exchange` and `host_dspi_link` for
//   the ColdFire instance of a two-worker session, see cf-worker.ts).
//
// Only erasable TypeScript (it also runs under Node's type stripping).

export interface KernelStats {
  compiled: number;
  compileMs: number;
  wasmBytes: number;
  runs: number;
}

export interface Core {
  exports: Record<string, any>;
  memory(): ArrayBuffer;
  kernels: KernelStats;
  /** Run an ABI function that leaves a JSON result (`digi_result_ptr`). */
  call(name: string, ...args: number[]): any;
  /** Copy bytes into the core; returns the pointer (free with `core.free`). */
  put(bytes: Uint8Array): number;
  free(ptr: number, len: number): void;
}

export async function instantiateCore(
  wasm: BufferSource | Response | Promise<Response> | WebAssembly.Module,
  extra: Record<string, (...a: number[]) => any> = {},
): Promise<Core> {
  const kernels: KernelStats = { compiled: 0, compileMs: 0, wasmBytes: 0, runs: 0 };
  const table: ((ctx: number, st: number) => number)[] = [];
  let exports: Record<string, any> = {};
  const memory = () => (exports.memory as WebAssembly.Memory).buffer;
  const env = {
    host_wasm_compile(ptr: number, len: number): number {
      const bytes = new Uint8Array(memory(), ptr, len).slice();
      const t = performance.now();
      try {
        const instance = new WebAssembly.Instance(new WebAssembly.Module(bytes), {
          env: { memory: exports.memory },
        });
        table.push(instance.exports.k0 as (ctx: number, st: number) => number);
      } catch {
        return -1;
      }
      kernels.compileMs += performance.now() - t;
      kernels.compiled++;
      kernels.wasmBytes += len;
      return table.length - 1;
    },
    host_wasm_run(id: number, ctx: number, st: number): number {
      kernels.runs++;
      return table[id](ctx, st);
    },
    host_wasm_free(id: number): void {
      table[id] = undefined as never;
    },
    // The core imports these whichever role it has; only the ColdFire
    // instance of a two-worker session ever calls them.
    host_dspi_exchange(): void {
      throw new Error('host_dspi_exchange: this instance has no remote DSP');
    },
    host_dspi_link(): void {
      throw new Error('host_dspi_link: this instance has no remote DSP');
    },
    ...extra,
  };
  let instance: WebAssembly.Instance;
  if (wasm instanceof WebAssembly.Module) {
    instance = await WebAssembly.instantiate(wasm, { env });
  } else if (wasm instanceof ArrayBuffer || ArrayBuffer.isView(wasm)) {
    instance = (await WebAssembly.instantiate(wasm as BufferSource, { env })).instance;
  } else {
    instance = (await WebAssembly.instantiateStreaming(wasm as Response | Promise<Response>, { env })).instance;
  }
  exports = instance.exports as Record<string, any>;
  const core: Core = {
    exports,
    memory,
    kernels,
    call(name, ...args) {
      const code = exports[name](...args) as number;
      const value = JSON.parse(
        new TextDecoder().decode(
          new Uint8Array(memory(), exports.digi_result_ptr() as number, exports.digi_result_len() as number),
        ),
      );
      if (code !== 0 || value.error) throw new Error(`${name}: ${value.error ?? code}`);
      return value.snapshot ?? value;
    },
    put(bytes) {
      const ptr = exports.digi_alloc(bytes.length) as number;
      if (!ptr && bytes.length) throw new Error('digi_alloc failed');
      new Uint8Array(memory(), ptr, bytes.length).set(bytes);
      return ptr;
    },
    free(ptr, len) {
      exports.digi_dealloc(ptr, len);
    },
  };
  return core;
}
