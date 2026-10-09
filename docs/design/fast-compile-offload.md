# Fast tier: compile offload and kernel cache

Status: design, 2026-10-09, branch `work/scenario-oracles`. Nothing here is built
except the measurement hooks (end of file). Marks as in CLAUDE.md: **[V]** verified
here by a run, **[D]** measured here, not re-checked by a second agent, **[O]** open
or predicted. Numbers are DN2 1.11, `trig1-taps` unless named, Apple M-series 16
cores, exclusive benchmark lock, shared generated core (no firmware blocks).

## Problem

The fast tier (`native/sharc/src/fast`) builds a region and compiles its kernel on
the DSP thread, at the first call that reaches the entry (`FastEngine::build` from
`run_inner`). In the AOT desktop build that is nothing (22 builds, 0.02 s per run,
worst 2.3 ms **[D]**). In the no-AOT config, which is what the browser and a patched
firmware run, it is the largest single cost of a short run and a long freeze at
start.

### Measured: where compile and build time goes **[D]**

No-AOT `static-insns+follow`, flat fallback on (default), 20 audio s, timed
(`SHARC_FAST_TL=1`, summarised by `tools/fast_timeline.py`). PCM
`e1556053127f219c`; the default AOT config gives the same PCM.

Whole run: DSP thread 99.8 s = Cranelift compile 19.8 + region build 2.9 + run 77.1
(headline 5.05 per audio s). 8741 builds, 6486 compiled, 2255 refused or retried.

| audio s | builds | compiled | compile s | build s | worst build+compile | builds longer than the 0.667 ms period |
|---|---:|---:|---:|---:|---:|---:|
| 0-0.1 | 6789 | 4828 | 16.52 | 2.51 | 64.4 ms | 3380 |
| 0.1-0.25 | 50 | 50 | 0.04 | 0.01 | 2.3 ms | 25 |
| 0.25-1 | 473 | 413 | 0.60 | 0.08 | 13.8 ms | 154 |
| 1-2 | 891 | 693 | 1.22 | 0.16 | 14.1 ms | 335 |
| 2-5 | 272 | 242 | 0.71 | 0.06 | 12.3 ms | 160 |
| 5 and later | 266 | 260 | 0.74 | 0.08 | 18.0 ms | 199 |

- **Cold start is 20 s of wall time for the first 0.25 audio s** (tick at ic = 250M:
  wall 20.2 s, compile 16.6 s). Audio second 1.0 is reached at wall 22.9 s, 2.0 at
  27.0 s. Compile is 16.5 s of the first 20 s.
- **After the first audio second there are still 1429 builds** (2.67 s of compile,
  0.30 s of build): 867 first entries of code not yet run (release, FX tails, 8.5 s
  burst of 106), 229 `rebuild-exit0` (the first instruction's guard failed four
  times, so it becomes a helper call), 200 retries (no active loop yet, no compile),
  93 `rebuild-req` (baked constant, MODE1 or CACC history differs), 40 `rebuild-win`.
  Bursts, not a trickle: 1038 ms of compile in the 0.25 s window at audio 1.5 s,
  411 ms at 4.0 s, 370 ms at 6.5 s. Nothing is built after audio 13.5 s.
- By kind (all): first 4384 builds / 8.29 s compile; `rebuild-win` 1178 / 9.19 s
  (flat rebuilds are large); `rebuild-req` 1066 / 1.14 s; `rebuild-exit0` 336 / 1.19
  s; retry 1777 / 0.01 s. With `SHARC_FAST_DYNFLAT=1` there are 62 more rebuilds
  (`dynflat`, 0.52 s) and compile is 21.2 s.
- **Per-region compile time:** 6486 kernels, mean 3.06 ms, p50 1.01, p90 8.86, p99
  27.2, max 42.6 ms (0xb8305c, 8226 IR ops, about 1.9 us per IR op). **Per-region
  build time:** mean 0.42 ms, p50 0.21, p90 0.82, p99 4.4, max 27.7 ms; 906 builds
  longer than a period.
- **Stall the audio thread sees today:** build and compile in the same call: p50
  0.60 ms, p99 25.4 ms, max 64.4 ms (97 periods). 4253 builds exceed one period and
  hold 22.0 of the 22.8 s: nearly all the cost is in stalls, not in small increments.
  After audio 1 s the worst is 14-18 ms (21-27 periods).
- **Run time without compile**, DSP CPU minus compile and build, per audio s: audio
  2.5-8.5 s **2.66** (17.4M region calls/s); 12-19.75 s **4.88** (26.2M calls/s, no
  builds). The step happens at about 9.2 s and takes 0.5 s (calls 19.4M to 26.8M/s);
  the cause is not found **[O]**. The finding-18 headline for 10.5 s (5.10 = 2.16
  compile + 2.94 run) mixes both regimes. With `SHARC_FAST_DYNFLAT=1`: 1.99 and 4.28.
  AOT default: 1.42 and 1.83 (the 20 s run costs 1.65 against the 1.43 of the 10.5 s
  pin).
- `sf-chr-depth-d127` (4.25 s): no-AOT 7.83 per audio s, of which compile 19.1 s and
  build 2.8 s of 33.9 s; last 1.25 s run time 2.75 (DYNFLAT 2.09), AOT 1.52. 8310
  builds, the same shape (6789 in the first 0.1 s, 1224 after audio 1 s).
- **Waste:** in a 1.5 s run 2270 of the 7790 builds were rebuilds, and 2150 (95%)
  replaced a slot that had run no instruction: the first kernel was compiled and
  never used (4.83 s of the 17.5 s of compile, 28%; the figure counts the discarded
  kernel only). `rebuild-win` and `rebuild-req` are declines found at the first
  call, before the kernel runs.
- Kernels: 8.8% (569 of 6486) contain helper calls; mean IR 6080 words (24 KB); mean
  machine code 6.4 KB; 14% of the helper-free kernels have the same IR as an earlier
  one in the run.

So the headline cost is not run time. The gap that remains once compile is gone is
the steady state, which this document does not touch (see the last section).

## Goals

1. No compile on the audio thread: the DSP thread interprets until a kernel is
   installed, and never waits for the compiler.
2. A bounded stall: the worst call that builds a region is at most about 1 ms
   (build budget), not 64 ms.
3. Warm start from a cache: a second session with the same firmware (or a patched
   one) reuses kernels instead of compiling them; no firmware-derived code ships in
   the repo or `dist/`.
4. Identical PCM: the switch from interpreter to kernel can happen at any call.
5. The browser gets the same shape (background compile, install on a boundary) with
   its own backend.

Non-goals: steady-state speed; the AOT config (nothing to fix there); changing what
the lowering produces for a given state.

## Determinism: the switch point cannot change results **[V]**

Premise of the whole design: tiers are exact, so installing a kernel late is the
same as installing it early. Test: new measurement knob `SHARC_FAST_DEFER=N` keeps a
freshly built kernel unused for N to 4N calls of its entry (the N is scaled by the
entry PC, so slots differ), which is what a background compile that has not
finished looks like. 2 audio s, `trig1-taps`, one binary each for no-AOT and AOT:

| config | DEFER | interpreted | PCM |
|---|---:|---:|---|
| no-AOT | 0 | 0.98% | `40d829c95c1d1480` |
| no-AOT | 1 | 1.05% | same |
| no-AOT | 7 | 1.04% | same |
| no-AOT | 40 | 1.12% | same |
| no-AOT | 3000 (up to 12000 calls per slot) | 4.60% | same |
| AOT default | 0 / 7 / 300 | 0.25% | same, and equal to no-AOT |

The 20 s runs agree as well: AOT default and no-AOT both `e1556053127f219c`, with and
without `SHARC_FAST_DYNFLAT=1`. With every kernel loaded from a stored machine-code
blob (`SHARC_FAST_RELOAD=1`, see below) the 1.5 s PCM is the pin `3bf3ba3026953c22`.
The knob changes only when a built kernel is first used; it does not touch any state
the interpreter or the kernel reads.

What does change with timing is every counter that depends on which tier ran:
interpreted share, region calls, declines, rebuild decisions (declines drive
`rebuild-win/req/exit0`, so a pending kernel changes the order of builds). Pins on
PCM stay valid; coverage tools need the synchronous mode (below).

## What a compiled region depends on, and the cache key

A region has two parts. `Region` (windows, requirements, exit sites, code words,
flag replay) is derived from the live state by `build()` and is cheap (0.42 ms mean);
it is rebuilt each session and is never cached in this design. The `Kernel` (IR) is
the output of the lowering for an `Env` (known registers, DAG L/B, MODE1, flag
state, `flat_windows`, the volatile-M and forced-helper sets) and the instruction
stream; the machine code is a pure function of the IR, the entry checks (`lean`, off
by default) and the backend. **[D]** by reading `lower/mod.rs` and
`sharc-fast-cl/src/lib.rs`; the second half is checked by the following runs.

- **Machine code is position independent: 0 relocations in all 5599 kernels of a
  1.5 s run [D].** Window addresses and the engine state arrive in `ctx` and `st` at
  call time. The only host addresses are in `Op::Helper(rec)`: `rec` is a leaked
  `HelperRec` pointer in the IR itself, and `helper_exec` is an immediate in the
  code. Those 8.8% of kernels cannot be reused in another process.
- **Compile is deterministic [V]:** the same IR gives the same bytes twice, so the
  IR hash is a stable key and a cached blob can be compared with a fresh compile in
  tests.
- **A blob can be stored and loaded back [V]**: new `ClBackend::compile_blob` and
  `sharc_fast_cl::load_blob` (`define_function_bytes`, no relocations). Load takes
  4.6 us per kernel (21 ms for 4566). With `SHARC_FAST_RELOAD=1` every kernel of a
  no-AOT run executes from the reloaded blob and the PCM matches the pin. That is the
  in-process half; the cross-process half needs the helper change below.
- **Compile scales with threads [D]** (4566 helper-free kernels, `compile_par`
  example, Cranelift `speed`): 1 thread 16.1 s, 4 threads 4.2 s, 8 threads 2.15 s, 16
  threads 1.36 s. `opt_level=none`: 1.58 s at 8 threads (-27%), code 24.2 MB instead
  of 32.9 MB. In the engine, `SHARC_FAST_CL_OPT=none` gives the same PCM, compile 14.4
  instead of 19.8 s, run time 2.69 (2.66) mid-run and 4.95 (4.88) late: a free -27%
  of compile at +1% run time.

Cache key (native), per kernel record:

```
sha256( FORMAT_VERSION
      | engine fingerprint: offset_of(St.r), size_of(V), offset_of(V.b/V.m), CTX_* constants,
                            helper ABI version, usize width
      | backend id: "cranelift", cranelift-codegen version, target triple, host isa flags
                    (cranelift_native feature set), opt_level, verifier setting
      | Kernel::encode() with Op::Helper words removed (helper-free kernels only in stage 3a)
      | EntryChecks )
```

The firmware hash and the entry PC are not part of the key: the IR already contains
everything the code depends on, so a patched firmware reuses every unchanged kernel
and a different firmware that shares a routine hits too. The pack is still grouped
under a firmware directory only so it can be pruned. Every record carries a SHA-256
of its bytes, checked at load (about 2 us); a mismatch, an engine-fingerprint
mismatch or a load error means "miss": the kernel is compiled and the record replaced.
A wrong cache must never run, so there is no best-effort path.

Size: 32.9 MB for 4566 helper-free kernels (about 41 MB for a full run), before
compression. One pack file per fingerprint (append-only records plus an index rebuilt
at open), in the user cache directory (`~/Library/Caches/<app>/fast-kernels/`,
`$XDG_CACHE_HOME`; `out/native/kcache/` for tools). The cache holds machine code
derived from the firmware: never committed, never in `dist/`, same as `sections/`.
Poisoning the cache is code execution as the user, like any JIT cache: files 0600,
directory owned by the user, and no cache read from a path given by a project file.

**Helpers (stage 3).** `Op::Helper(u64)` becomes `Op::Helper(slot)`: the kernel loads
the record from a per-region table whose pointer sits in `ctx` (set per call, one
store), and `helper_exec`'s address comes from `ctx` too (set once). The table is
rebuilt from (pc, generated-body id, decoded fields) at install. Cost: two loads per
helper call (about 11 ns each today). Touches `lower/helper.rs`, `ir.rs` (encode,
decode), both backends and the glue; gated by PCM pins. Until then helper kernels are
compiled each session in the background (about 570 kernels, roughly 2 s of worker
time).

### Browser: what can be cached

The browser unit is the wasm module from `sharc_fast_wasm::emit_kernel` (14.3 KB
mean, 66.7 MB for 4566 kernels; 28.5 MB gzip, 24.7 MB zstd **[D]**). Emit is 179
us per kernel and V8's compile is 1.4 ms, so caching the bytes saves only the emit
(1.2 s per 6.5k kernels) and none of the V8 work. Storing a compiled
`WebAssembly.Module` in IndexedDB is not something to depend on (specification and
Chrome behaviour changed; **[O]**, not tested). Caching compiled code in the browser
means the browser's own code cache for `compileStreaming` over a `Cache`-stored
response (**[O]**, per-engine). So the browser's gain is background compile, not a
warm cache, in the first version. The IR-hash-keyed byte cache stays an option once
the V8 side is measured in Chrome.

## Design: native

New `native/sharc/src/fast/offload.rs`; changes in `mod.rs` and the backend trait.

**Pieces**

- `CompilePool`: N worker threads (default `clamp(cores - 3, 1, 6)`; `0` = today's
  synchronous path), each with its own `KernelBackend` (a factory replaces the single
  boxed backend). Workers run at a lower QoS than the DSP thread (macOS
  `QOS_CLASS_UTILITY`, as the DSP thread already sets its own class) so they do not
  compete with the ColdFire thread and the audio sink. Queue: a mutex-protected
  binary heap by priority, condition variable; results go through a lock-free
  multi-producer queue plus an `AtomicUsize` count the DSP thread reads.
- `Job { id, pc, slot_gen, kernel: Kernel, entry: EntryChecks, key, prio }`. The
  kernel is moved in (the `Region` keeps a copy only for reports: it is cloned only
  when `SHARC_FAST_TL`/log is on). `Done { id, slot_gen, result: Result<Blob, String>,
  compile_ns }` where the blob is machine code (`CodeBlob`). The DSP thread turns a
  blob into a callable kernel (`load_blob`, 4.6 us) at install, so the executable
  memory is created on the thread that owns the engine and a cache hit and a fresh
  compile take the same path.
- Backend trait: `KernelBackend: Send`, `CompiledKernel: Send`, plus
  `fn compile_blob(&mut self, &Kernel, &EntryChecks) -> Option<Result<Blob,String>>`
  and `fn load_blob(&Blob) -> Result<Box<dyn CompiledKernel>,String>`. Backends
  without blobs (reference interpreter, plug-in) keep `compile` and run
  synchronously. `JITModule` is `Send`; `ClKernel` already is.

**Slot protocol** (all on the DSP thread except the worker's compile)

1. `build()` runs the lowering as now. It then does everything that does not need the
   kernel: `watch_sw` on the code words, the `follow` queue, `stats.built`. It stores
   the region and sets `slot.pending = Some(job_id)`; `slot.kernel` stays as it was.
2. `run_inner`: if `slot.kernel.is_none()` (or the call is chained) return `None`:
   the interpreter runs the call. A rebuild keeps the current region and kernel
   running (`slot.next = Some(region, job)`); today's `slot.region.take()` before
   `build` goes away, which is strictly better than interpreting while the new
   variant compiles.
3. Install. `FastEngine::poll_installs` runs when `pending_jobs > 0 && done_count
   != seen` (two field reads per call, outside the hot path otherwise) and also once
   per DSP period from the peer (`FastTier::tick`). For each result with a current
   `slot_gen`: load the blob, set `slot.kernel` (and for a rebuild: swap in `next`,
   push the old variant to `alts`), count `built`. A stale result (the slot was
   dropped, rebuilt for a code change, `dyn_flat`, `drop_slot`) is discarded; the
   code check `verify_code` on the first call after install is unchanged, so a code
   patch between submit and install cannot run a stale kernel.
4. A failed compile sets `slot.refused` as today (`stats.refused`).
5. `SHARC_FAST_SYNC=1` (default in `tools/`, tests, scenario pins and `perf.py`):
   `build` submits and waits for its own job before returning, i.e. today's
   behaviour with counters that do not depend on timing. The desktop app and the
   browser turn the pool on.

**Tier-up policy** (stage 2; each item changes only which builds happen, never
results)

- *Do not compile doomed kernels.* 95% of rebuilds replaced a kernel that never ran
  an instruction (28% of compile time). After `build()` and before submit, run the
  region's entry check and window resolution against the current state without a
  kernel (`glue` already separates `check` from `kernel.run`); submit only when it
  would run, or after K = 2 interpreted calls. For `rebuild-win` the flat variant is
  then built first and the plain one never exists.
- *Hotness order.* FIFO first; then a bump message when a pending slot has been
  interpreted 2^k times (the `slot.pend_calls` counter, only incremented while
  pending), so the loops that cost most are ready first. This is what turns "20 s of
  interpreted cold start" into a short one: the first 0.1 audio s is 50M executed
  instructions, 5 s at the interpreter's 105 ns, and only hot kernels need to arrive.
- *Build budget.* Build is the remaining stall (906 builds over a period, max 27.7
  ms). Spend at most about 0.3 ms of build per DSP period; when the budget is gone
  `run_inner` returns `None` and the build is retried at a later call. The interpreter
  covers the gap.
- *Cheaper first compile.* `opt_level=none` for the queue head when the queue is deep;
  recompile at `speed` is not needed at today's numbers (+1% run time).

**Cache** (stage 3): in `build()` after the lowering, compute the key (SHA-256 of 24
KB, about 10 us; 65 ms per 6.5k kernels). Hit: `load_blob` and install on the spot,
no queue, no pending state. Miss: submit; the worker writes the record to the pack
before it reports done. The pack is opened at engine creation; lookups are in
memory.

**Expected effect (native, 8 workers) [O]:** DSP-thread compile time goes from 19.8 s
to the clone and submit cost (microseconds per kernel); build 2.9 s stays; worst
single stall from 64 ms to the build's 27.7 ms, then 1 ms with the budget. Cold start:
today 22.9 s of wall to reach audio 1.0 s; with a pool the work is bounded by
interpretation while hot kernels arrive, estimated 3-6 s. Warm start: build 2.9 s plus
cache lookup and load 0.1 s, no compile. Steady state unchanged.

## Design: browser

State of the code: the browser core (`emulator-core-sharc.wasm`) has no fast tier in
`dist`; `native/README.md` says so. The parked branch `wip/browser-host` shows the
shape that worked under Node: two wasm instances, DSP worker and ColdFire worker over
a `SharedArrayBuffer` link, `HostBackend` in `sharc-fast-wasm` with three imports
(`host_wasm_compile(ptr,len) -> id`, `host_wasm_run(id, ctx, st)`, `host_wasm_free`).
The kernel instance imports the core's own linear memory (`env.memory`), so a host
pointer of the core is an address in that memory. The DSP worker blocks in
`serve()` on `Atomics.wait` and never returns to its event loop. That last fact
decides the design.

**Measured V8 behaviour (Node 24.15, V8 13; `tools/wasm_compile_bench.mjs`, 4566
kernels) [D]**

| step | cost |
|---|---|
| `new WebAssembly.Module(bytes)` | 0.047 ms mean (functions compile lazily) |
| first call (lazy Liftoff compile) | mean 1.43 ms, p50 0.25, p99 23.8, max 45.2; 1387 above a period; 6.5 s total |
| `--no-wasm-lazy-compilation` constructor | 1.48 ms mean, p99 24.0, max 44.5 (same cost, moved) |
| async `WebAssembly.compile`, 64 in flight, eager | 2.3 s wall (V8 background threads) |
| compile worker + `postMessage(Module)`, eager | 1.9 s wall for all; 0.08 ms per Module delivery |
| compile worker compiles, instantiates, calls `k0` once, posts the Module | worker 6.6 s; **first call on the other thread 0.002 ms** |
| same bytes constructed on a second thread after the worker compiled them | 1.45 ms again: **no cross-thread cache by bytes** |

So the stall is the same shape as Cranelift's (a p99 of 24 ms) at a third of the
cost, it happens at the first call, and a transferred `Module` carries the compiled
code with it if the worker has already run the function.

**Design**

- Rust (`sharc-fast-wasm::host`): `HostBackend` becomes asynchronous. `compile`
  emits the module and calls `host_wasm_submit(job, ptr, len)` (JS copies the bytes
  and posts them to a compile worker, returns at once); `host_wasm_poll(out_ptr, cap)
  -> n` fills (job, kernel id) pairs from the results the JS side holds. The
  engine's slot protocol is the native one; there are no threads in the wasm core,
  so `poll_installs` runs at the same period tick (`digi_step`, the DSP frame
  handler).
- JS: a pool of compile workers (`navigator.hardwareConcurrency - 2`, 1-4). A worker
  does `WebAssembly.compile`, instantiates the module over a private scratch memory,
  calls `k0` once (zeroed memory: all 4566 kernels returned without a trap or a loop
  in the test), and posts the `Module` back. The DSP worker instantiates it over the
  core memory (cheap) and registers the id. Instances are created per kernel and
  imports are by reference, so a memory grow in the core does not invalidate them.
- **Receiving needs an event-loop turn.** A `Module` can only arrive through
  `postMessage`; it cannot go in a `SharedArrayBuffer`; `receiveMessageOnPort` is
  Node only. So `serve()` becomes `await Atomics.waitAsync` (Chrome, Firefox;
  Safari **[O]**) or yields through a `MessageChannel` turn after each frame batch
  while jobs are pending (about 20-50 us per yield; not the 4 ms of `setTimeout`).
  When no job is pending it keeps the blocking `Atomics.wait` path.
- `Op::Helper` is `unimplemented!` in the wasm emitter today (8.8% of kernels). For
  the first version `Env` gets a `helpers_ok` flag set false for wasm, so those
  instructions end the region as refused; the follow-on is the stage-3 helper table
  with `helper_exec` as an imported function. This lowers wasm coverage and has to be
  measured before it is accepted **[O]**.
- Compile hints (a custom section marking the function eager/optimised) are an
  option to remove the warm call; support differs by browser **[O]**, not tried.
- Cache: see above; in v1 none. OPFS/IndexedDB for the IR-hash to bytes map is a
  later step if Chrome measurements show emit matters.
- Also open **[O]**: wasm `flat_access` takes its 32-bit path on `usize == 32`, not
  run yet (finding 18, "Region-call overhead"); the V8 compile cost of the flat
  kernels is measured above on the 64-bit-lowered IR.

## Files

| file | change |
|---|---|
| `native/sharc/src/fast/offload.rs` (new) | pool, queue, job/result, priority, worker QoS |
| `native/sharc/src/fast/cache.rs` (new) | pack file, key derivation, engine fingerprint, verify |
| `native/sharc/src/fast/mod.rs` | slot `pending/next/gen`; `build()` split into lower + submit; `poll_installs`; sync mode; build budget; dry entry check |
| `native/sharc/src/fast/ir.rs` | `Send` bounds; blob methods on the trait; canonical `Kernel::key`; stage 3: `Op::Helper(slot)` |
| `native/sharc/src/fast/glue.rs`, `lower/helper.rs`, `helper.rs` | stage 3: helper table in `ctx`; dry entry check (stage 2) |
| `native/sharc-fast-cl/src/lib.rs` | `compile_blob`, `load_blob` (exist now); helper from `ctx`; worker-local backend |
| `native/sharc-fast-wasm/src/lib.rs` | async `HostBackend`; `Op::Helper` import (stage 3 / 4) |
| `native/boot/src/sharc_peer.rs`, `abi.rs` | per-period `FastTier::tick`; offload on for the app |
| `native/coupled/src/bin/coupled_run.rs`, `tools/perf.py`, `tools/fastcov.py` | `--fast-offload N`, `SHARC_FAST_SYNC` default for tools |
| `packages/web/src/dsp-worker.ts`, new `kernel-compile-worker.ts`, `core-host.ts` | async waits, compile pool, imports |
| `tools/fast_timeline.py`, `tools/wasm_compile_bench.mjs` | added now (measurement) |

## Risks

- **Counters stop being deterministic** with the pool on (interpreted share, calls,
  rebuild order). Mitigation: sync mode for all tools and pins; the app only. PCM is
  unaffected [V].
- **Cold start is slow in a different way:** the DSP interprets at 105 ns per
  instruction until hot kernels arrive. Hotness order (stage 2) is what makes it
  short; a "warming" state in the UI may be needed.
- **Worker CPU competes** with the ColdFire thread and the audio sink on small
  machines. Pool size and QoS are knobs; browsers have fewer cores.
- **Stale installs:** a result for a slot that was rebuilt or whose code changed.
  Generation counter plus `verify_code` at the first run.
- **Memory:** 6.5k JIT modules (one `mmap` each today) plus the IR clone; the pack
  adds 41 MB on disk. A shared arena (`ArenaMemoryProvider`) is an option.
- **Cache poisoning / corruption:** hash check per record, user-only permissions.
- **Helper indirection** touches both backends and the lowering; the pins must stay
  and the gain is the last 8.8% of kernels, so it goes after the first cache.
- **Cranelift ISA flags:** a cache written on one CPU feature set is rejected on
  another by the key.
- **Wasm:** Safari without `Atomics.waitAsync`; V8 lazy compilation behaviour in
  Chrome not measured (Node 24 only); a warm call executes kernel code on zeroed
  scratch memory (it returned for all 4566 tested; for other kernels it is an
  assumption, so the call needs a guard such as a zero iteration count in the
  scratch `ctx`).

## Staged plan

0. **Done:** measurements and hooks (this document).
1. **Slice proof, native, no cache.** `offload.rs`, slot protocol, sync flag, install
   at period tick. Measure on `trig1-taps` 20 s no-AOT with `SHARC_FAST_TL=1` and
   `SHARC_FAST_OFFLOAD=8`: DSP-thread compile time from 19.8 s to under 0.1 s;
   worst stall to the build's 28 ms; steady state 2.5-8.5 s within 3% of 2.66; PCM
   `e0e6c8af7a84c443` at 10.5 s and `e1556053127f219c` at 20 s, and equal across
   worker counts 1, 4, 8; with `SHARC_FAST_DEFER` still equal. Report wall time to
   audio 1.0 s (22.9 s today). Also try `SHARC_FAST_CL_OPT=none` here (free).
2. **Policy.** Dry entry check, hotness order, build budget, keep the old variant
   running during a rebuild. Gate: compile CPU down by about a quarter, worst stall
   about 1 ms, cold-start wall time to audio 1.0 s.
3. **Cache.** 3a helper-free kernels: key, pack, load. Gate: second session
   compiles at most the helper kernels; PCM equal; patched-image test (flip one
   instruction, only affected keys miss). 3b helper table, then all kernels.
4. **Browser.** Wasm `Helper` decision, async `HostBackend`, compile worker, yield in
   `serve()`; first under Node with the two-worker harness from `wip/browser-host`,
   then Chrome and Firefox. Gate: PCM equal to native, stall per period measured in
   the DSP worker.
5. **Optional.** Region build off the audio thread (snapshot `Env` and the code
   words), or a region cache keyed by (pc, `Env` hash, code hash): takes the 2.9 s
   of build out of warm start.

## What this does not fix

Compile offload moves 22.7 s of the DSP thread's 99.8 s to other threads (and, with
the cache, away). Steady state is untouched:

| config | run time per audio s | target |
|---|---:|---:|
| no-AOT, audio 2.5-8.5 s | 2.66 (DYNFLAT 1.99) | 1.0 |
| no-AOT, audio 12-19.75 s | 4.88 (DYNFLAT 4.28) | 1.0 |
| AOT desktop | 1.42-1.83 | 1.0 |

In the no-AOT config the DSP is 2.7 to 4.9 times too slow after the compile is gone,
so even a perfect offload yields audio only offline or with large buffering. The
offload matters for start-up, for the stalls once the run time is under 1.0, and for
a browser that must load without a shipped core. Levers for the gap, ranked as in
finding 18 plus what this measurement adds:

1. **The late regime (4.9 vs 2.7).** The call rate rises by 50% at 9.2 s with no
   build; understand it first, it is the largest unexplained term **[O]**. The same
   step is in the AOT run (1.42 to 1.83).
2. `SHARC_FAST_DYNFLAT=1` as default: -25% run time (2.66 to 1.99, and 4.88 to 4.28);
   exact in 18 runs earlier; costs 1.5 s more compile, which the offload hides.
3. Desktop: mask-free AOT emission (1.58 to about 0.8-0.9, low-medium confidence, high
   effort); inlining a callee into its DO loop (about 0.47 s of the no-AOT run);
   `stop` exits by form coverage (3.0M calls/s); chaining that skips the Rust loop
   (about 0.5 s of glue at 13-16M calls/s); class (e) mid-region exits; generated
   compute helper bodies with flag sync.
4. Browser: interpreted share to 0.1% (or 1% with a 10 ns generated interpreter),
   ColdFire interpreter speed (1.3 s per audio s already exceeds a core), kernel
   chaining inside one wasm module.

## Measurement hooks added (all off by default; PCM unchanged)

- `SHARC_FAST_TL=1`: `tl build ...` per build and `tl tick ...` every 250M
  instructions on stderr, with `SHARC_FAST_TL_DUMP=DIR` writing the helper-free IR
  (`kernel-<hash>.bin`, input for `wasmgen`); `tl cl` lines from the Cranelift
  backend (code bytes, relocations). Parser: `tools/fast_timeline.py ERRFILE`.
- `SHARC_FAST_DEFER=N`: interpret the first N to 4N calls of each new kernel.
- `SHARC_FAST_RELOAD=1`: run every Cranelift kernel from a stored blob.
- `SHARC_FAST_CL_OPT=none|speed_and_size`.
- `native/sharc-fast-cl/examples/compile_par.rs KDUMP [THREADS...]`;
  `tools/wasm_compile_bench.mjs WASM_DIR` (`ONLY=sync|async|worker|cross|cold|warm`).
- Checks with the hooks compiled in: default AOT `trig1-taps` 10.5 s timed, PCM
  `e0e6c8af7a84c443`, 1.429 and 1.423 DSP CPU per audio s (finding 18: 1.39-1.43);
  no-AOT 1.5 s `3bf3ba3026953c22` with TL on and with RELOAD on.
