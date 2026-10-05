# DN2 real-time audio: measurement baseline and the plan to own the Rust DSP core

Date: 2026-10-05. Workspace `/Users/em/src/digi/digitakt2`, branch
`work/sharc-emulator`, baseline HEAD `bd7f0f9`. Em authorized a local source
and results checkpoint commit on 2026-10-05. It replaces
`docs/HANDOVER-2026-10-04-dn2-realtime-progress.md` for state
and next steps. Findings from the earlier part of this session are in
`docs/findings/07-emulator.md` (sections dated 2026-10-04/05) and
`docs/findings/06-sharc-engine-and-startup.md` ("Circular buffering for
ordinary loads and stores").

## Goal and Em's direction (agreed this session)

- **Goal:** faithful real-time audio for the DN2 (and later DT2). It must work
  in the native desktop app and in a browser build published on GitHub Pages.
  The Pages build must contain **no Elektron firmware or anything derived from
  it**: the user uploads a `.syx`, and later may apply patches in the browser.
- **Own the Rust code.** The Python→Rust transpiler (`tools/sharc_transpile.py`,
  `tools/sharc_rsgen.py`) was the migration path, not the future. From here on:
  - the generic Rust SHARC core (the transpile-only output `core_g.rs`,
    `core_i.rs`, `syms.rs`, `tables.rs`, 17,640 lines, firmware-free) is to be
    checked in and maintained by hand;
  - `tools/sharc_core` (Python) becomes a reference oracle for differential
    tests only;
  - the per-firmware AOT generated core (`out/native/dn2-audio/gen`, 80 MB of
    Rust, firmware-derived, never committed) is **frozen**. No new work on it.
    Remove it from the default path once its replacement is at least as fast.
    Today it is still what makes the desktop app fastest.
  - The CLAUDE.md rule "SHARC+ instruction semantics live once, in
    tools/sharc_core/" must change when the Rust core is adopted. Get Em's OK
    on the wording.
- **Optimise from evidence only.** Rank by measured leverage. Measure a
  candidate's speedup on captured states before building it.
- **Workflow:** fast builds and long, deterministic, unattended runs. Agents
  should not wait on each other or on Em, and should not repeat 4-minute
  compiles.
- **Commits and pushes:** Em requested a local checkpoint commit after the
  descriptor/sequencing controls. Earlier experiment sections describe their
  no-commit execution at the time. Em retains publication authority; no push
  is authorized. Firmware, generated AOT, captures and out/ remain excluded.

## Bottom line now

In the historical tested setup, DSP computation was the proven real-time
blocker and a constant background cost: mixing, effects and voice processing
ran every period, playing or not. Current headless observations place 74–75%
of elapsed wall in DSP-thread CPU, leaving 25–26% unattributed rather than
proving a scheduler or bridge cause.

**[V]** Independent audit of the PGO baseline, using the app's own run loop
through the headless runner, found 1.34766 DSP CPU seconds per audio second for
idle and 1.37166 for `kb-chord16`; ColdFire was 0.61 and 0.62 respectively.
All end-to-end periods missed the 0.667 ms deadline. These are two 10-second
repeats in `out/native/runs/20261005-073811`.

| Scenario | Real-time factor (≥1 needed) | DSP CPU per audio second | ColdFire CPU per audio second | Periods over the 0.667 ms deadline | Period wall p50 / p99 / max |
|---|---|---|---|---|---|
| idle | 0.74 | 1.34766 s | 0.61 s | 100% | 0.79 / 1.47 / 8.7 ms |
| kb-chord16 (16 notes) | 0.73 | 1.37166 s | 0.62 s | 100% | 0.81 / 1.51 / 7.2 ms |

- **Target:** DSP ≤ about 0.7 s CPU per audio second, for headroom. That is
  about **1.9× faster** than the PGO build.
- **ColdFire** already has headroom (0.61 s). Its execution policy is
  `reference`; an optional softfloat HLE policy exists, `ExecutionPolicy::SoftfloatAbiV1` in
  `native/boot/src/softfloat.rs`. That is a later candidate if ColdFire headroom
  is ever needed. It changes instruction counts, so check fidelity first.
- **DSP instruction split** (historical three-region baseline): 49.8% idle-loop instructions skipped
  cheaply, 31.9% in AOT-generated blocks, 18.0% in the fast tier (3 regions),
  0.3% in the interpreter. Time is dominated by AOT block code (69% of DSP
  thread time in the live profile) plus the fast-tier loops.
- **Without PGO** (the same matrix, `out/native/runs/20261005-071422`): DSP
  1.91 s per audio second, real-time factor 0.52. Its dirty revision and high
  load make the observed PGO comparison non-isolated; the 1.78% idle/chord
  difference is within ordinary run noise and does not prove a constant cost.
- **Browser-like configurations** (generic gen, no AOT, same matrix):
  - interpreter only: DSP 54 s CPU per audio second, real-time factor 0.02;
  - with automatic fast-tier region selection (`SHARC_FAST_AUTO=256,64`): DSP
    about 28 s per audio second;
  - the interpreter costs about 100 ns per instruction because it executes the
    transpiled known/unknown-mask model;
  - the browser's ColdFire speed under WebAssembly has never been measured.

### How the live app was measured, and what was ruled out

- **Diagnostics export** now saves to `out/native/diagnostics/<epoch>-digiemu-dn2-diagnostics.json`.
  (It used to be an `<a download>` link, which the macOS webview ignores.) The
  status line shows the path only briefly, so look in that directory.
- **Session `1791143264`** was launched with `DN2_PROFILE_LINK=1 mise run emu`,
  with PGO stale (no PGO), and left idle at the MMC prompt for 66.9 s:
  - 34.36 s of audio produced (0.51×), 5,698 underruns;
  - `worker_dsp_ns` 64.6 s: the DSP thread was inside `run_period` 96.5% of
    wall time;
  - `host_reply_wait_ns` 36.1 s: the ColdFire waited on DSP replies;
  - the JS pump gaps between `emu_step` calls totalled 1.0 s (1.5%), so **the
    IPC/`setTimeout(0)` pump is not a bottleneck**.
- **System trace** (`xctrace` System Trace, attached for 10 s,
  `out/native/diagnostics/live-sys-1/`): `sharc-dsp` was Running 98.615% of
  the window (9.86146 s). The ColdFire actor thread `3457` was Running 3.05090
  s (30.509%) and Blocked 6.85727 s (68.573%). **The live app is DSP-bound.**
- **[C] Time Profiler is not a verified comparison.** The previous 64% versus
  99% interpretation and the derived "threads take turns, 36% lost" claim are
  retracted: the original Time Profiler evidence is unavailable. Use System
  Trace, exact thread CPU clocks and the link timers instead.
- **Audio production tracks ColdFire progress exactly.** About 132–133M
  ColdFire instructions per audio second; ready is at ColdFire instruction
  851,236,347. So produced audio ≈ (ColdFire instructions − 851M) / 132.5M.
- **The DSP period** is 666,667 DSP instructions = 0.667 ms = 32 samples at
  48 kHz. The DSP clock is modelled at 1 GHz, one instruction per cycle.

## What exists now (tools and architecture)

### Crates (one Cargo workspace, root `Cargo.toml`, one `Cargo.lock`)

- `native/sharc-rt`: runtime (`St`, `Rf`, `V{b,m}`, `Mem`, `rt/*`, decode,
  `EXIT_*`). Editing it rebuilds the generated core (about 4 min).
- `native/sharc-gen`: only `include!`s the generated dir from `SHARC_GEN_DIR`
  (`build.rs` sets `cfg(sharc_gen/sharc_image)`). It re-exports `sharc_rt::*`
  so the generated files stay unchanged.
- `native/sharc` (`sharc-native`): Engine, dispatch, fast tier (`src/fast/`),
  glue, canon, C ABI. Editing it rebuilds in about 20 s.
- `native/sharc-fast-cl`: Cranelift backend for the fast tier, plus the
  examples `fast_diff`, `fast_blockers` and `region_bench` deps.
- `native/coupled` (`digi-coupled`, feature `coupled-audio`): **the shared
  coupled run loop** (`CoupledRuntime`, `STEP_CHUNK=250_000`, DSP peer, PCM,
  link timing), the `coupled-run` headless runner, `scenarios.json`, and the
  ignored gates in `digi_coupled::gates`. The desktop crate wraps those gates
  under the same test paths (`desktop_runtime::coupled::tests::*`).
- `packages/desktop/src-tauri`: thin Tauri shell over `digi-coupled`.
- Excluded from the workspace: `native/sharc-jit` (wasmtime and wasm-only
  runtime; it has its own lock). It holds P1 work on splitting AOT WebAssembly
  into per-region modules that share memory and a table. That is relevant to a
  future WebAssembly backend for the fast tier.

### Build directories and locking

- `tools/native_target.py` maps each generated core to a target dir
  `out/native/target/<key>`:
  - `prod` = `out/native/dn2-audio/gen`;
  - `generic` = `out/native/dn2-audio/gen-generic` (transpile-only);
  - `nogen`, or `g-<hash>` for other gens;
  - PGO builds go in `<key>/pgo-instrument` and `<key>/pgo-use`.
- Inside a git worktree the root is `out/native/target-wt/<name>`. A worktree
  **cannot** reuse the main tree's compiled `sharc-gen` (tested: it recompiles).
  So build A/B variants one after another in one target dir and copy the
  binaries out (CLAUDE.md "Agents").
- Measured costs:
  - cold desktop build with the prod gen: about 4 min;
  - `sharc-gen` alone: 172 s wall, 1,198 s CPU;
  - opt-level 1 for `sharc-gen` costs about 2.5× more CPU, so no `fasttest`
    profile was kept;
  - opt-level 0 overflows the `sharc-dsp` stack (and runs about 100× slower);
  - the generic gen compiles fast.
- `tools/benchlock.py` is a readers–writer lock on `out/.benchlock`:
  - builds take it shared, timed runs exclusive;
  - a waiting exclusive holder now blocks new shared holders (writer
    preference);
  - `uv run python tools/benchlock.py status`;
  - `... run --exclusive -- CMD` for ad-hoc commands;
  - `native_pgo.py`, `sharc_regionbench.py` and `coupled_matrix.py` take it
    themselves.

### PGO (`tools/native_pgo.py`)

- **Commands:** `train`, `check`, `plan`, `build`, `run`, `bench --pairs N`,
  `bench-bins A B --pairs N [--fast-a/--fast-b REGIONS] [--env-a/--env-b ...]`.
- **Key:** only the emulation crates (everything under `native/`; `packages/` is
  excluded), the gen dir contents, `rustc -vV`, triple, features, fast-region
  list, and the root `Cargo.toml`/`Cargo.lock`. A change to the desktop crate
  alone gives `ok (desktop crate changed; profile reused)`.
- **Launcher** (`tools/native_emu.sh`, a Python script, run by `mise run emu`):
  - coupled path: if the profile is stale it **retrains automatically**
    (about 6 min, prints a line first);
  - `DIGI_EMU_PGO_TRAIN=0` skips training and falls back;
  - `DIGI_EMU_PGO=0` disables PGO;
  - every fallback prints a `WARNING:` line;
  - builds with `rustup run 1.98.1 cargo run --release --locked ... --target aarch64-apple-darwin --target-dir out/native/target/prod/pgo-use`.
- **Training** runs the ignored test `coupled_ready_exactness` four times with
  the launcher's fast regions on.
- **Current profile:**
  `out/native/pgo/dn2/aarch64-apple-darwin/profiles/2e5e5e7a394fa29b812ec96eef6a3cf9179d3b781e14509f9bc970eddcd02d9f.profdata`
  (key `db75672997bf7863`). The manifest is stale after the native edits. The
  current measurements deliberately reused this explicit existing profile; no
  PGO retraining is claimed or should be inferred.
- **Toolchain:** only the rustup 1.98.1 distribution. Homebrew rustc has the
  same version string but is incompatible for PGO.
- **Measured gain:** about 0.65–0.81× of plain release DSP time across
  configurations.

### Headless runner and matrix (the measurement tool from now on)

```sh
# one scenario, by hand
out/native/target/prod/release/coupled-run --syx Digitone_II_OS1.11.syx \
  --dsp-image snapshots/dn2-audio-ready-2026-10-03/digi-audio-dn2-image.bin \
  --dsp-state snapshots/dn2-audio-ready-2026-10-03/digi-audio-m5.snap.dsp \
  --cf-snapshot snapshots/dn2-audio-ready-2026-10-03/digi-audio-m5.snap \
  --scenario idle --out report.json        # exit 2 = gate failed
# the matrix: configs prod, generic, generic-auto x scenarios; writes out/native/runs/<ts>/
uv run python tools/coupled_matrix.py [--configs prod] [--scenarios idle,kb-chord16] \
  [--repeats 2] [--seconds 10] [--pgo <absolute or relative profdata>] [--list]
# diagnostic ranking of the current launcher configuration; pass the old profile explicitly
P=out/native/pgo/dn2/aarch64-apple-darwin/profiles/2e5e5e7a394fa29b812ec96eef6a3cf9179d3b781e14509f9bc970eddcd02d9f.profdata
uv run python tools/coupled_matrix.py --configs prod --scenarios idle,kb-chord16 \
  --repeats 2 --seconds 3 --pgo "$P" --profile-regions
```

The matrix now uses four default fast regions. The saved
`out/native/runs/20261005-region-cost/final_measure.py` and
`candidate_measure.py` pin the historical three-region control explicitly and
use retained binaries to replay the reported experiments without builds.

- **Scenarios** (`native/coupled/scenarios.json`, guest-clock events):
  - `idle`;
  - `trig1-hold`: a QA prelude (NO, NO, encoder A), then Trig 1 held;
  - `kb-chord16`: the QA prelude, keyboard mode (button 18), all 16 trig keys
    held; RMS 0.566 against 0.377 for one note;
  - validation scenarios `exactness-100m` (PCM `38d2a322…`) and
    `trig1-noqa-40m` (`a50586e8…`).

  Recording a pattern by button script and playing it was unreliable (only the
  first note sounded).
- **Each report includes:**
  - exact per-thread CPU (`CLOCK_THREAD_CPUTIME_ID`) per audio second for the
    DSP worker and the ColdFire step thread;
  - link timers;
  - DSP instruction split by tier (`Engine::tier_counters()`,
    `FastTier::fast_stats()`);
  - the ColdFire split (interpreted, idle fast-forward, RAM-clear);
  - a per-period wall histogram against 0.667 ms;
  - PCM hash/count/RMS and gates;
  - load average, with runs started under load flagged.
- **Measurement overhead:** link timers about 2% of DSP CPU; the PCM tap under
  0.01 s of ColdFire time per audio second. Single runs carry about ±5% noise,
  so repeat.

### Fast tier (load-time translator, Cranelift)

- **Design notes:** `out/native/fasttier/DESIGN.md` (ignored dir). Inventory of
  the top-19 work set: `out/native/fasttier/inventory/`.
- **What it does:** translates regions of the loaded image at run time into
  typed kernels:
  - plain `u32`/`f32` values;
  - an entry guard;
  - memory windows bounded once per call;
  - lazy flags, written from their last writers at exit;
  - a budget check per iteration;
  - exact side exits.

  Shapes: DO-loop body, and CFG (jumps, delayed branches, nested and
  in-region DO loops; it ends at CALL/RTS or an indirect jump). It lowers
  most hot forms: float/fixed ALU, multiplier, multifunction, shifter, the
  immediate-offset memory forms, conditions, and circular-buffer
  post-modify/MODIFY wrap.
- **Env vars:**
  - `SHARC_FAST_REGIONS=0x1c399a,...` lists explicit entries; the launcher's
    table holds the DN2 1.11 entries `0x1c399a,0x1c3862,0x1c364f,0x1c5765`, keyed by
    the `.syx` SHA-256 `2af43e65…`;
  - `SHARC_FAST_AUTO=threshold,min_insns[,blocks]` turns on run-time hot-region
    selection (default 256,64,0; off unless set);
  - `SHARC_FAST_SHAPE=cfg`, `SHARC_FAST_LOG`, `SHARC_FAST_HIST`;
  - `SHARC_FAST_NOCIRC=1`;
  - `DIGI_EMU_FAST=0` stops the launcher setting regions.
- **Per-call cost:** small regions about 19–28 ns (parity with AOT); large
  regions about 70 ns. Remaining cost: requirements, windows, the loop exit
  through the generated `_advance`, flags, prologue/epilogue.
- **Per-call speed against AOT:** the big loops gain 2–4.5×; regions of 3–5
  instructions only reach parity.
- **Enabling more regions on top of AOT does not pay.** All 32 verified entries
  were 1.27× slower than the 3 regions, because of exits into AOT or the
  interpreter, transitions, and chain capping: while the tier is installed the
  engine ends AOT chains at the first link, which costs about 3.7%.
- **Hand-written speed of light** (`out/native/regionbench/sol/sol.rs`,
  firmware-derived, never commit): `r_1C399A` 292 ns against about 3,060 ns
  AOT; `r_1C3862` 538 against 4,170. The fast tier reaches 1.3–1.8× of
  hand-written on those loops.
- **Verification commands** (Cranelift, production gen built into the
  `sharc-fast-cl` examples):

  ```sh
  F=snapshots/dn2-audio-ready-2026-10-03
  B=<target>/release/examples/fast_diff
  $B forms $F/digi-audio-dn2-image.bin $F/digi-audio-loop2-fresh.bin --trials 2000 [--cond] [--circ]
  $B cfg   $F/digi-audio-dn2-image.bin $F/digi-audio-loop2-fresh.bin --cases 300 [--stride --shape cfg] [--stack-model 0] [--simple] [--circ]
  $B region $F/digi-audio-dn2-image.bin out/native/regionbench/states 1C399A --inject 150   # SHARC_FAST_SHAPE=cfg for CFG shape
  ```

  All showed 0 mismatches at the last run. Mutation checks were done for each
  lowering family.

### Other tools from this session

- `tools/sharc_regionbench.py` (`gen`/`capture`/`bench`/`compare`/`check`/`fast-bench`):
  - builds a mini generated core of the selected regions and times one region
    call from captured entry states;
  - about 50 s per variant; noise about ±0.7% with `compare --rounds 9 --calls 20000`;
  - captured states are in `out/native/regionbench/states`.
- `tools/sharc_entries_merge.py`: augments rsgen entries from a fallback
  profile. That proved useless under `--model-safe`.
- `tools/sharc_circstats.py`: counts circular post-modifies and wraps in an
  instrumented gen.
- `out/native/diagnostics/live_prof.py`: groups an `xctrace` time-profile
  export by thread and cost class. Use it with System Trace for states.
- **Reproducing the generator:** `out/native/gen-provenance-20261003/`
  (`PROVENANCE.txt`, inputs `cov6.txt`/`ent9.txt`/`trans9.txt`) rebuilds the
  accepted AOT cache code-identically. The production gen was regenerated
  2026-10-05 after the circular-buffer change. The previous gen is
  `out/native/dn2-audio/gen-pre-circ-20261005`.

## Corrections and lessons from this session (don't repeat these)

- **The JS pump/IPC is not the live bottleneck** (1.5% of wall time).
- **The attached Time Profiler under-reports busy threads.** Trust System
  Trace, thread CPU clocks and the link timers.
- **The L7 = 0x3e2/0x1fd/0xc2 "delay lines" are the DSP software stack.** The
  coupled workload makes 5.77M circular post-modifies, all on I7, and none
  wraps. Circular-buffer support changed no hash or PCM.
- **Static host-instruction cuts in the AOT loops don't buy time.** The
  NaN-check collapse plus the mult-flag fast path removed about 10% of
  instructions and gained 0.5%. Native f32 add/sub/mul gave 0.94× and was kept.
  i64 truncation and branchless flag merges gave nothing (reverted).
- **Adding block entries at interpreter resume points does nothing under
  `--model-safe`.** Two more unknown-value fallbacks gave about 1%.
- **The aligned period stop** (`Engine::step_until_in_aligned`) gave 0.83×. It
  removed 1.2M interpreted instructions per 1,000-frame replay.
- **Most of the time went to rebuilding the 80 MB generated core** in new
  target dirs or with a different `SHARC_GEN_DIR`, and to agents waiting on
  benchmark locks. The gen-keyed target dirs and benchlock writer preference
  address that. Prompts must tell agents to reuse one target dir per gen.

## Step 3b: bounded fresh candidate measurements (complete)

Step 3a remains the diagnostic feature described above. Its normal generated
core remains frozen. Step 3b used the production generated core and complete
canonical captured states; it did not rebuild or modify the generated core.
The retained old PGO profile was passed explicitly and was not retrained.

### Coupled decision

The launcher keeps four DN2 1.11 defaults: `1C399A`, `1C3862`, `1C364F`, and
`1C5765`. The bounded whole-coupled ABBA experiment for `1C5765` used the same
retained binary and two three-second repeats per mode. It showed idle DSP CPU
medians of 1.217581 -> 1.164295 seconds per audio second (4.376% lower) and
`kb-chord16` 1.229413 -> 1.174277 (4.485% lower); PCM, guest instruction and
frame counts matched across configurations, and the separately pinned
validation hashes passed. The separate bounded full coupled exactness gate
also passed (`candidate-full-exactness.log` under
`out/native/runs/20261005-region-cost/`): ColdFire `fbace0f0…`, DSP export
`05ac2ac2…`, PCM `38d2a322…`, 72,896 samples and 1,139 frames. This justified
the fourth default. The performance estimate remains a short paired result;
the current roughly 1.16 DSP CPU seconds/audio-second still exceeds the 0.7
target.

`B8276C` was rejected as a fifth default: the same retained-binary ABBA
experiment (two three-second repeats per mode) increased DSP CPU by 3.7172%
on idle and 4.7300% on `kb-chord16`. Timed PCM hashes and counts matched across
configurations, and the five-region validation run passed both pinned
scenarios. No full fifth-region ColdFire/DSP export gate was run because this
candidate was rejected on performance.

### Fast tier: fresh captured-state matrix

`out/native/runs/20261005-step3b/` retains the binary, source snapshot,
commands, raw stdout/stderr, hashes, build metadata, captures and JSON reports.
`summary.json` condenses the audited comparisons and coverage refusals.
The immutable pre-cache-optimisation binary/build report is retained alongside
the measured cache-optimised binary (`region_bench-cache-opt-measured`) and
the final binary. Example-only builds took about two seconds after the first
library compile; the generated cache stayed unchanged. The production wrapper
clears inherited release-profile, SOL and Rust/LLVM flag variables, uses its
explicit PGO flags only when requested, and defaults to 16 calls / 2 warmups / 3
repetitions. The retained `build_cached.py` guard stops an unexpected generated-
core rebuild. Explicit CGU=16/LTO=false overrides had caused a different Cargo
fingerprint despite nominally matching defaults; production builds now omit
those overrides. The wrapper itself has no build-reuse guard or run timeout.
The final example also passed an offline no-generated-core `cargo check`
(23.31 s) after its concrete module/CLI were gated by `sharc_gen`. The final
generated smoke passed all 1,900 form trials and the full-loop gate; its source
hashes match the final files. These checks are recorded in `nogen-check.json`
and `concrete-gen-smoke.json`; their smoke timings do not replace the measured
three-repetition results.

A capture-only capped dispatch collected all thirteen ranked entry states in
`states-capped`; it caps AOT chaining for capture and does not execute fast
kernels. Each fast result restored the same complete state before a call.
Compiled kernels were built before timed samples. Full canonical export plus
the specified control state, end PC, and retired count gated every proved row.
Raw steps and chain are recorded because they are tier-dependent; both are
excluded from cross-tier equality, and steps are checked within each mode.
Reset/import (about 15 ms) and export proof are outside the timed interval and
dominate process wall time. Finish bookkeeping runs only at each bounded call end, not
through a full `Engine` dispatch at every instruction boundary. Timings use the
median of the three repetition medians.

| entry | AOT ns/instr | fast total ns/instr | fast/AOT cost | status |
| --- | ---: | ---: | ---: | --- |
| B8276C | 5.6082 | 12.7704 | 2.277x | proved, CFG prefix plus AOT successor |
| 1C5765 | 2.9128 | 4.5576 | 1.565x | proved |
| B80C54 | 7.1785 | 13.9108 | 1.938x | proved, CFG prefix plus AOT successor |
| 1C399A | 3.8800 | 3.5412 | 0.913x | proved |
| 1C1884 | 9.0132 | 14.7174 | 1.633x | proved |
| B823DB | 9.2346 | 15.0540 | 1.630x | proved |
| 1C364F | 3.1709 | 2.8775 | 0.907x | proved |
| 1C3862 | 3.3455 | 2.5499 | 0.762x | proved |

The pilot covered eight proved entries and five coverage refusals, so its
`complete=false` records that coverage is incomplete. `B8B526` declined before
building a kernel; `1C253F` built but failed entry requirements (the report's
`req` counter, not its budget counter);
`1C1FAB` has an unsupported access-width loop shape; `1CDBE1` and `1C0CB1`
lack a generated successor at their required boundary. The successor refusals
are harness-coverage limits, not semantic failures. `B8276C`'s whole loop
refuses at 3a instruction 20 because its address range is unknown, leaving a
20-instruction CFG prefix; its loop-carried M4 copies R0 from a previous memory
load, so treating that entry-known guard as safe would be unsound. The
conservative fallback is retained.

### Optional guarded-read experiment

**[O]** `out/native/runs/20261005-full-loop/` records a narrow, default-off
extension for that `B8276C` load. `SHARC_FAST_GUARDED_READS=1` admits only a
read-only pre-modified 3a access whose index is independently known, plain and
unmodified at entry. The host resolves one aligned, present, plain-NW span
containing that anchor through the existing effective-address aliasing and
`Mem::window` contract, then the kernel guards the wrapping final address
before its load or pending commit. The selected span is cached by memory layout
generation. This is generic metadata and contains no firmware-specific address
or function.

**[D]** Static capture inspection found the anchor's architectural page aliases
to `0x2824_0000`; its loaded/overlay image has a 240-byte gap, while the upper
32 KiB aligned subspan containing the captured anchor is present. This corrects
the earlier assumption that absence of the architectural page alone meant a
window refusal. Nine direct compiled guard bounds, including wrapped in-span
and out-of-span results and the `u32` endpoint, passed; the executable check
also verifies that a dynamic read window cannot share a static load/store slot.
The focused unit tests for alias/cache invalidation remain unrun.

**[O]** The extension is not a full `B8276C` loop implementation. With the
environment unset the strict one-call smoke retains the old 20-instruction CFG
prefix; with it set, the prefix grows to 21 instructions and whole-loop
lowering declines at the next instruction. Both smokes matched the full
832-instruction canonical/reference boundary through CFG plus AOT successors.
The next instruction (`B827A8`) is a
pre-modified 3a store of R0 through I2/M3, and M3 was made dynamic by an earlier
move from R14. Dynamic stores are outside this read-only experiment, so neither
smoke is performance evidence or proof of a 26-operation/832-retirement loop.
The missing tests are exact first/later guard-exit architectural state,
adversarial host-cache/reset cases, and focused CFG-host guard-exit coverage.
Three retained successful builds took 47.56 seconds combined; the worker
reported three failed compile attempts totaling another 43.26 seconds.
All generated-core caches remained unchanged. No coupled performance run was
performed. Inventory every
body memory access, including dynamic stores, before attempting another
whole-loop lowering.

For planning only, applying `w * (1 - fast/AOT)` to the historical ranked AOT
wall lead `w` yields negative estimates for the unpromoted AOT leads: B8276C
−66.5 ms/audio-second, B80C54 −32.4, 1C1884 −16.8, B823DB −15.8. These are not
CPU claims and are not summed: the ranking used an earlier diagnostic baseline's
wall time, captured-state microbenchmarks have restored-capture
latency, and host load was roughly 17–25. The current-default rows are N/A for
this calculation because their historical profile tier differs. The negative
results do not promote a new candidate; the positive fast rows are existing
defaults, not new promotions. Preserve the coupled evidence when deciding
desktop defaults rather than substituting cold microbenchmarks.

### Concrete prototype

The firmware-independent prototype decodes once into cached operations, holds
ordinary registers as `u32`, and uses generic exact helpers for flags and
memory; it calls no specialised firmware-generated routine. It currently
covers forms 3a, 4a, 3c, 2a, 5a_move and 5b_move, with scalar add/sub/mul,
float pass, fixed pass and increment. On `1C5765` it executed 19 operations ×
64 trips = 1,216 instructions and passed 1,900 seeded random/corner trials
in total (100 per decoded operation), then full-capture canonical/control gates
on every timed sample. The final three-repetition, eight-call result was 14.5633
ns/instruction, against generic 80.3183 (5.515x faster) and AOT 2.9811
(4.885x slower). The primary comparison is this same-run result. The earlier
18.09 ns/instruction result predates cache-flush work and is only an unpaired
exploratory improvement.

A static oracle review permits the terminal path to leave ordinary cached
registers in place: under the admitted no-MODE1/L/B/M/control/bank/pending
writes, `_advance` touches only special registers and stacks. The executor
therefore flushes ordinary registers once at final exit, rather than flushing
and reloading all 96 each trip. It is deliberately not resume-capable after a
refusal. Coverage probes correctly refuse `1C253F` (unknown R0), `B8276C`
(15b), and `B823DB`/`1C1884` (multifunction compute).

### Next decision

Finish the local checkpoint, then refresh a short unprofiled baseline on the
retained fixed-four runner under settled system load before quantifying the
remaining real-time gap. The recent 1.265–1.376 CPU/audio observations were
profiled; older unprofiled current-runner reports also vary with load. They
are not interchangeable controlled speedup comparisons. Reuse the binary and
input hashes; no Rust build or additional broad hardware recording is needed.
Judge candidates by paired coupled DSP CPU/audio, deadlines and fidelity.

The no-build PC mapping and cycle-based CPU Profiler are now complete (see
below). The selected DSP slice places 58.26% of cycle weight in generated
blocks, 12.02% in `exec_insn`, and 14.46% in `Engine::step`. A paired register
control shows owned direct handlers remain 5.247x faster than generic with
architectural, journalled registers; caching accounts for only a roughly 6%
cost difference on this admitted loop. Configured CPU Counters now establish
thread-level bandwidth fractions, with recurring delivery warnings in the
steady-render slice (see the guided counter sections below). The targeted
delivery run and stack association are also complete: instruction-cache delay
is a measured contributor, but the join has not singled out an actionable
function. Stop further broad profiling for this question; keep the frozen
AOT/runtime/generator. The same-handler form-dispatch control is now complete
and negative: preselected function-pointer routing costs 13.58% more on 1C5765.
Reject that design; it does not bound all dispatch cost. The separate component
controls are now complete: descriptor reparsing costs 2.679× the predecoded
architectural owned lane, while full sequencer advancement costs 1.093× on
1C5765. Keep predecoded descriptors; these are owned-path effects, not a
partition of generic overhead or a global speedup. That owned lane remains
5.749× slower than AOT. Next inventory all missing semantics/guards for the
retained hot B8276C loop before a broader paired owned/AOT comparison; it also
needs 15a, 6b_shiftimm and two multifunction 4a computes. Prove the memory and
timer interval guards as well as opcode coverage. This is an owned-core
feasibility check, not a demonstrated desktop fix. The known trap/range experiments
remain candidates with unmeasured global return. The earlier chain-cap control
missed its CPU gate, so the engine symbol does not justify a dispatch rewrite.

Keep the current four-region desktop hybrid. The fast coverage and concrete
fallback are promising evidence for an owned, firmware-free Rust path, but
neither can replace AOT yet and no browser or global performance claim follows
from these measurements. Step 3c is the next decision point; only the semantics
ownership wording change requires Em's approval. Compare direct handlers,
flags and memory on retained hot-loop fixtures, then prove broader coverage and resumable fallback before
adoption. Keep Python as the differential oracle and change the semantics
ownership wording only with
Em's approval.

### Whole-body inventory: bounded loop ranges (2026-10-05)

The source-only pass examined all thirteen retained entry captures and decoded
all eleven active DO bodies completely. The other two captures, `1C0CB1` and
`B8B526`, have no active DO body at entry. Raw words, decoder fields, complete
bodies, provenance and conservative annotations are retained under
`out/native/runs/20261005-loop-inventory/`. No Rust build, emulator, benchmark
or prebuilt executable ran in this pass. Some other-body width/compute
annotations remain explicitly unresolved; they are not proof of admission.

**[C]** The earlier account of `B8276C`'s modifiers missed their final writers.
Its 26-operation body contains 22 memory transfers and has 32 remaining trips.
Independent capture-byte decoding and a separate lowering-source review found:

- Instruction 13 copies R0 to M4. Instruction 18 loads R0, but instruction 25
  overwrites it with a zero-extending two-bit FEXT, `R0 = R1 & 3`, before the
  next trip copies R0 to M4.
- Instruction 15 copies R14 to M3. Instruction 22 writes
  `R14 = R10 & 3`, before the next trip copies R14 to M3.
- Entry R0 and R14 are both 1. Thus both address modifiers can remain in
  `0..3` if the loop-head entry values are guarded. The first read and store
  both use `I2+4`; raw captured M4=4 and M3=137 are overwritten before use.

This is a bounded loop-carried value, not an invariant single entry value.
It does **not** establish that general dynamic stores or new deoptimization
machinery are necessary. The existing AND lowering already tracks `[0,3]`
within an iteration, but `Plan` retains only written-register information and
affine strides. Strict `init_abs` loses the non-stride interval at the loop
head. That is the smallest identified proof gap.

**[D]** Static capture inspection found that the bounded accesses need
`[I2,I2+16)`. Merging them with existing same-base accesses needs
`[I2,I2+48)`. All 48 bytes are present and overlay-dirty in one contiguous
effective alias span at `0x2824EBB0`; there are no gaps. This meets the
fixture's existing read/write window byte conditions, subject to runtime
plain-address and entry checks. It is not a runtime or performance result.

The next proposed implementation is a generic, straight-loop terminal interval
summary. Assume only conservative nonnegative, nonwrapping terminal ranges at
the loop head, add entry `Req::Within` checks, and verify that final-pass
terminal ranges are subsets of those assumptions. Refuse unstable proofs and
keep CFG propagation unchanged. Validate bounds 0/3, rejection of 4/unknown,
intervening loads and invalidating final writes, alias/overflow boundaries,
then the complete 26-operation/832-instruction comparison with guarded reads
disabled. Only then measure performance; the roughly 40% global CPU reduction
still needed is not established by this local opportunity.

Other entries are different problems: `B80C54` has a captured delayed jump
(`stack_model=true`) and an immediate memory store, requiring control coverage;
`1C1FAB` needs encoded width/space review; `1C253F` has an entry-knownness
requirement involving pre-instruction operands. Missing AOT successors at
`1CDBE1`/`1C0CB1` remain harness coverage limits. `1C1884` and `B823DB` already
ran complete loop shapes and lost to AOT in the restored-capture measurements;
they need cost work, not loop completion. Current defaults remain unchanged.

## Open issues and loose ends

**Recorded findings.** The audited baseline, live-app corrections and step 3a
profiling record are in `docs/findings/07-emulator.md`. Independent audit
confirmed the final step-3a values and top-ten rank. Fourth-region promotion
passed its bounded coupled gate; the fifth-region decision remains negative.

**SHARC core semantics.**
- Type 7a conditional MODIFY with (sw)/(nw) scales M but not L
  (`tools/sharc_core/forms_dag.py` about lines 106–110). Fix:
  `length.value * scale_now`, also in the |M| > L test. Recorded [O] in
  findings 06.
- Types 1b and 10a (`ELSE DM(Ia,Mb)`) have no core handler; they stop as
  unsupported.
- Bit-reverse addressing (BR0/BR8) and the CB7I/CB15I wrap interrupts are not
  modelled.
- Post-modify with |M| > L applies one correction, unlike MODIFY. The manual
  doesn't cover it.

**Engine and tests.**
- The generated-block-only run of entry `0x1c0ca9` differs from the
  interpreter in `at_loaded_entry`. The fast tier matches the interpreter
  there. Not investigated.
- `coupled_ready_exactness` in a debug build overflows the `sharc-dsp` stack.
  Use release.
- `tests/test_sharc_transpile.py::test_native_compute_corpus_matches` fails on
  a stale default dylib (`out/native/opt/target-final/...`). Run with
  `SHARC_NATIVE_LIB=` pointing at a fresh cdylib.
- `tests/test_native_emu_launcher.py::test_normal_launcher_arguments_get_audio_without_replacing_firmware`
  is order-sensitive if `tools/` is left on `sys.path`.

**App.**
- The Export diagnostics status line is overwritten within a second. Show the
  saved path persistently, or briefly in a toast.

**Workspace leftovers.**
- The repo-root `Cargo.toml` excludes `.claude` (agent worktrees).
- The workspace refactor repointed some tools outside its assigned files to
  the new target dirs. Check them: `live_audio.py`, `cf_lockstep.py`,
  `cfisa/oracle.py`, `native_boot.sh`, `native_plusdrive.sh`,
  `tests/test_live_sharc.py`, `tests/test_live_audio.py`.
- **Em can delete these:**
  - old per-crate `native/*/target` dirs;
  - `out/native/pgo/dn2/*/{instrument-target,target,control-target}`;
  - `out/native/fasttier/wt-*` and `out/native/crates/`;
  - `out/native/fallback-map-20261003/{scratch*,target*}`;
  - `out/native/codegen-fast-20261004/`;
  - the `.claude/worktrees/agent-*` worktrees and their branches. Em removes
    worktrees; give the cleanup command, don't run it.
- **Dirty files that are not ours, left alone:**
  `docs/HANDOVER-2026-10-01-rust-desktop-browser.md` (modified),
  `docs/HANDOVER-2026-10-02-sharc-audio-checkpoint.md` and
  `docs/HANDOVER-2026-10-03-dn2-audio-realtime.md` (untracked).

## Working conventions that served well

- **Delegation:** use the personal configured roles from AGENTS.md with fresh
  context. Scouts read; workers implement scoped changes and run bounded
  checks; reviewers/oracles independently assess evidence. Root owns decisions
  and integration. Keep one writer in this cwd; children do not spawn children.
  Reuse the main generated-core cache rather than create new worktrees or
  target directories. This session authorizes no commits.
- **Every engine change** must pass:
  - `fast_diff forms`/`cfg`/`region` (0 mismatches);
  - the coupled gates: `coupled_ready_exactness` with PCM `38d2a322…`, CF state
    `fbace0f0…` and DSP export `05ac2ac2…`, or the runner's validation
    scenarios;
  - a paired benchmark (`bench-bins` or the matrix with repeats) reporting the
    median ratio.
- **Firmware-derived material** stays under `out/`: generated cores, captured
  states, hand-written loop translations, profiles and WAVs. Never commit it.
  Never name the vendor DSP toolchain. Cite only public manuals; read them as
  text under `out/refs/<pdf stem>/`.


### Current retained-runner attribution and playback observation (2026-10-05)

A single cached production build of the retained four-default runner took 27.344s with the old explicit PGO profile; generated-core fingerprints did not change. The copied binary `699c7312…` passed `--list`, `exactness-100m` and `trig1-noqa-40m`. Guarded reads were off.

The one paired 10.0013-audio-second `trig1-hold` run has identical PCM hash (`500dee3d…7704d`), guest counts and source frames headless and with playback. Headless reported 1.5362 DSP CPU seconds/audio-second and 20.559s wall; playback reported 1.4595 and 18.133s under distinct high per-run starting loads (22.5366/26.0742). That same-binary pair does not identify a scheduling cause or throughput change. Playback used WH-1000XM6 at 44.1 kHz for the 48 kHz source, started without errors, reached a 512-frame callback, and ended with 1,526 underruns. Its period p99/max were 2.954/225.186 ms, versus headless 2.577/8.425 ms.

Playback-only health snapshots from 1.020 to 17.599 wall seconds show 1,428 underrun increments across all 158 intervals, a 0–441 queued-device-frame range, and at most a 128-frame producer/receiver difference. They are asynchronous feeder observations that exclude in-flight channel messages; they do not establish played-frame counts or silence duration. Raw artifacts, manifests and the analysis are under `out/native/runs/20261005-current-attribution/`.

Authorised `/usr/bin/sample` runs sample all thread states. Their stack counts are not CPU shares or a closed partition: DSP roots were 7,938/7,959 idle/chord; generated-code samples 4,295/4,445 (AOT subset 4,086/4,225); `exec_insn` 1,218/1,185 and `Engine::step` 1,103/1,006. Main-thread waits (4,888/4,802) are not DSP CPU. The B827 block leaf (251/246) is only a small sampled lead. The subsequent intrusive `SHARC_FAST_HIST` run records interpreter-PC attempts only; it cannot apportion Engine dispatch or bridge cost.


### Intrusive interpreter-PC diagnostic (2026-10-05)

The current `coupled_run.rs` and `lib.rs` hashes match the playback-pair manifest and predate the 27.344 s build of copied binary `699c7312…`; there is no unbuilt-source divergence. A single exclusive, same-process headless run executed idle and `kb-chord16` for two audio seconds each with the retained four fixed regions, guarded reads off, `SHARC_FAST_HIST=1`, `SHARC_FAST_LOG=1`, and an AUTO threshold of 2^32−1. Both runtime gates passed with four built and zero auto-armed regions, zero refusals, and finite PCM. The raw logs, exact environment, input hashes, report and decode are in `out/native/runs/20261005-current-attribution/`.

`SHARC_FAST_HIST` counts pre-execution interpreter attempts, rather than retirements or CPU time. It reported 4,068,630 attempts across 825 PCs for idle and 4,043,749 across 840 PCs for chord16. The leading decoded PC was `0x1c06fe` (21c system/NOP: 156,000/126,116 attempts); the following `0x1c2554..0x1c2542` 15a/2a/4a/3a/4a sequence had 48,000/48,032 each. Decode used the ready-state overlay plus its SHIM image, with retained capture overlays checked for conflicts. This is intrusive diagnostic evidence only: it cannot isolate `Engine::step` dispatch or bridge cost and has no no-hist two-second PCM comparator.

Current ten-second headless baselines were 1.5149/1.5309 DSP CPU seconds per audio second and 73.99%/74.71% DSP-thread busy time for idle/chord16. The historical 1.16 figure is context, not a same-host regression claim. These observations show a DSP execution deficit plus roughly 25–26% elapsed wall outside DSP-thread CPU; they do not assign a cause.

### Option-26 execution-frequency diagnosis (2026-10-05)

A cached production build of the diagnostic runner took 4.951 s and left the
generated-core cache fingerprint unchanged. Its copied binary `2bad6e98…`
passed default-profile-off `--list`, `exactness-100m` and `trig1-noqa-40m`
gates. One intrusive two-audio-second idle/chord process with option 26 enabled
then passed all ordinary gates in 6.303 s. This is a frequency diagnostic, not
a throughput benchmark. Raw TSV maps, source/input hashes, the checked
end-minus-start ModelStats deltas and a reproducible parser are in
`out/native/runs/20261005-dispatch-diagnosis/`.

**[V]** In idle/chord, `1C2518` recorded 45,000/45,030 non-trap zero-progress
entry bails. Its observed unknown mask includes R0/R1/R2/R15, all of which
intersect its generated full-known guard, so that constant guard fails.
`1C254E` recorded 48,000/48,032 zero-progress bails with pending clear; R0
intersects its full-known guard. `1C253F` recorded 48,000/48,032 kind-1 exits
at `TRAP_BLOCK_UNKNOWN`, followed by the same number of transitions to
`1C254E`. The maps do not identify the exact unknown operand/variant or show
whether a trap retired work.

The `1C2518 -> 1C253F` transition occurs 3,000/3,002 times, while the five
interpreted body PCs and the successful exits occur 48,000/48,032 times: 16
per period. That is consistent with one interpreted warm-up iteration followed
by AOT progress for each of 16 enclosing iterations. It is not evidence that a
whole generated 30-trip loop completed, and retired instructions per AOT call
remain unmeasured. The option-26 entries are interpreter sequence entries, not
AOT dispatch attempts.

Across the two scenarios the engine recorded 14.457M/14.827M block entries;
non-trap zero-progress bails are about 3%. `ModelStats.gated` equals those
admitted model-on calls, not refusals. Source inspection found no independent
model-driven chain initialization in the examined path: `CHAIN_MAX` is assigned
when fast declines and a generated entry is called. Its relationship to the
observed call counts remains unmeasured. `SHARC_FAST_CHAIN=0` is a possible
no-build diagnostic control, but it can bypass retained fast-entry consultation,
so it is not a clean same-four-fast comparison unless counters first verify
reachability. The next attribution should isolate successful generated
dispatch, model checks and chain-cap work before ranking a broader
unknown-aware executor or the bounded-range candidate. No CPU share or speedup
follows from these counts.

### No-build chain-cap control (2026-10-05)

The retained `2bad6e98…` diagnostic binary was compared without a build using
`SHARC_FAST_REGIONS=0`, which retains FastRuntime but ran no fast kernels.
Capped (`SHARC_FAST_CHAIN=1`) and uncapped (`=0`) controls cleared every other
`SHARC_FAST_*` setting. Both pinned PCM gates passed in two processes (3.606 s
combined); the two idle/chord two-audio-second processes took 11.824 s. Every
control row had FastRuntime present with zero fast calls, runs, instructions,
builds, refusals and arms. PCM, ColdFire instruction counts, total DSP
instruction counts, frames and audio duration matched exactly per scenario.
Raw commands, environment, loads, hashes and analysis are under
`out/native/runs/20261005-chain-cap-control/`.

**[V]** Uncapping reduced outer block entries from 14.457M to 4.897M for idle
and 14.827M to 4.819M for chord (66.13%/67.50%). The corresponding short-run
DSP-CPU/audio ratios, uncapped over capped, were 0.983116 and 0.997470. The
predeclared promotion condition required both ratios at most 0.97 with lower
entries, so it failed; no confirmation/ABBA repeat ran.

This control includes dispatch, failed lookup and model housekeeping rather
than isolating one chain-call cost. It deprioritizes chain-cap removal as an
immediate optimization under this control, not all dispatch costs. Both arms
retain necessary dispatch/model work, and one short ordered pair cannot bound a
small effect. It is not a production four-fast-region ROI ranking or an
unknown-fallback versus generated-runtime ranking. The next narrow diagnosis is
unknown fallback/generated-runtime work. No implementation follows from this
control.

### Source-only `1C253F` trap localization (2026-10-05)

The option-26 `1C253F` self-transition plus `TRAP_BLOCK_UNKNOWN` now
localizes the failing instruction, without running a build or workload.
`Engine::step` records the post-call `pc_sw`; both the known and unknown
`r_1C253F` wrappers assign `rf.pc=1C253F` on an error from the first operation,
and their final flush copies it to `pc_sw`. The error path has no outgoing
chain. The first operation is 3a: it passes R0, DM-loads through I0 (internal
UREG 16), post-modifies, and synchronizes flags/loop state.

This does not imply a zero instruction-count delta because earlier loop
iterations may have completed. The selected known/unknown variant, exact helper,
load/write outcome and progress remain unobserved. Candidate paths include loop
`rf_set`, known-variant missing DM load and index/result/flag `rf_set`; the
unknown variant permits some. The minimum next diagnostic records per trapping
call the selected variant, completed count, helper/error identity and
load/write outcome. Artifacts and precise source references are in
`out/native/runs/20261005-trap-localization/`; no optimization or ROI follows.

### Live `1C253F` AOT-call capture (2026-10-05) **[V] [O]**

A default-off diagnostic captured raw generated-AOT-call boundaries after the
fast tier declined and after dispatcher bank/model/chain preparation, but before
post-call handling. One cached production build took 17.570 s and left the
generated-core fingerprint unchanged; copied coupled and `fast_diff` binaries
were `7be63891…` and `f7a88298…`. `fast_diff` forms/cfg/region passed with zero
errors (39.33/98.63/22.69 s), as did default-off `--list`, exactness and trig1
gates. Two intrusive two-audio-second runs, idle and chord16, each captured four
calls and matched the previous four-default runner's PCM and full count checks.
The raw create-new canonical snapshots, TSV boundary fields, hashes, commands
and reproducible parser are in `out/native/runs/20261005-live-trap-capture/`.
This is diagnostic I/O, not a throughput measurement; canonical overlay memory
requires the manifest's base image.

**[V]** Both early scenario samples were identical. Calls 1/3 entered `1C253F`
with the source-reviewed 13 guard codes and R0/R15 known, then returned
`TRAP_BLOCK_UNKNOWN` at the same PC with zero `icount` delta
(321183→321183 and 321356→321356). Their next captured entries occurred five
instructions later with inner-loop remaining 30. Calls 2/4 had R0/R15
nonconstant, returned `EXIT_NEXT`, advanced 150 instructions to `1C254E`, and
removed that inner loop. The resulting live pattern is one interpreted first
trip followed by 30 AOT five-operation trips: 31 total, not 29 AOT trips.

**[O]** The known/unknown wrapper inference is supported by boundary masks and
source guards, but the precise helper, operand, failed load/write outcome and
rollback identity remain unobserved. These two identical early samples do not
establish frequency or state coverage. Historic successful captures with
R0/R15 unknown are consistent with success-only capture selection, not proof
that a live entry is unknown. The next narrow observation should expose helper
identity and load/write outcome per trapping call before any fallback rewrite.

#### Causal qualification for the bounded live captures **[V] [O]**

The raw boundary data now supports a source-and-state cause for these eight
bounded early calls. Before the first load, the loop-slot inputs are constant;
the known path has R0=23 and normal-word modifier scale four. Generated
`out/native/dn2-audio/gen/spec_07.rs:10955` calls `_load_normal_ureg_rf` for
that first 3a DM load at I0=4. The reproducible read-only parser finds neither
direct `[4,8)` nor alias `[0x28000004,0x28000008)` data in the matching SHIM
base or any pre-call overlay. The four trapping records use I0=4; the four
successful unknown records use I0=8, and the parser checks both actual four-byte
ranges and aliases.

`block_base_ok=1` requires `assume_nw32=true` and this generated image's
`explicit_memory_model=false`; the live NativeDsp options independently set
10=0 and 12=1. Thus the explicit-memory-model zero-fill branch does not apply.
The known wrapper's missing DM load returns `TRAP_BLOCK_UNKNOWN`; its unknown
wrapper accepts `None` as `V::UNK`, explaining the observed split. This is a
source-and-state causal reconstruction, not a helper trace, a hardware claim,
or a firmware-memory-bug claim. It covers only eight identical early calls;
rollback details and broader state coverage remain open. A shadow differential
test retaining masks, model and budget semantics is a candidate, deferred by
the subsequent CPU attribution review; no production routing or ROI claim.

### Current System Trace: strategic CPU attribution (2026-10-05)

One retained `699c7312…` runner completed ten audio seconds of `kb-chord16`
under System Trace, with no Rust build or native-code edits. It retained the
four fixed regions and disabled intrusive profiles/captures. PCM and all guest,
frame and DSP tier counts exactly match the earlier ten-second chord reference.
That reference uses a different retained binary and is not an overhead control.
Whole-scenario DSP CPU/audio is 1.36297, CF 0.62541; DSP CPU occupies 99.715%
of scenario wall. This current headless run is DSP-compute constrained and does
not explain the earlier run's 25–26% non-CPU wall remainder.

The recording's default rolling window retains ten seconds, shorter than the
13.6704-second measured scenario. There is no shared boundary marker. The
selected trace-relative [2,8) execution slice contains 5,986 running DSP samples,
with all symbolized leaf PCs verified against raw PCs (+1 export convention).
Its independently checked scheduler partition is Running 98.483%, Blocked
2.126 ms, Runnable 0.140 ms, Interrupted 80.425 ms and Preempted 8.316 ms.
Sample weight exceeds Running duration by 1.3%; percentages are approximate,
not exact whole-scenario CPU shares or an overhead/performance comparison.

Leaf sample origins partition into generated blocks 54.51%, `exec_insn` 15.89%,
`Engine::step` 14.93%, generic semantics 3.36%, runtime helpers 2.97%, fast host
0.79%, numeric/unassigned/possible JIT 3.96%, other 3.59%. Shared helpers and
inlining mean these are not execution-tier shares; the engine symbol does not
mean pure dispatch. `B8276C` leads individual generated leaves at 3.78%.
Successful `1C253F_unknown` is 0.67%, which does not bound its trap/fallback cost.
The trace exposes only the DSP thread's CPU samples, not separate CF CPU costs,
desktop scheduling or device callbacks.

Reproducible scripts, raw trace/XML, hashes, count/PC audits and qualified
analysis: `out/native/runs/20261005-system-trace-current/analysis.md`.
Next use cached disassembly and saved sampled-PC histograms, then CPU Profiler
where needed, before choosing one optimization. No commits or frozen-core edits.

### Python-to-Rust efficiency concern: preserved direction and next comparison

Em explicitly reiterated the original concern that Python-to-Rust generation
may produce inefficient Rust. The owned-Rust/Python-oracle direction above is
unchanged. Current leaf shares establish where work executes, not that the
generator caused the cost. Generic semantic transpilation and specialized
firmware AOT are separate implementations. The earlier controlled `1C5765`
prototype is 5.515x faster than generic but 4.885x slower than AOT, establishing
optimization potential without proving that hand maintenance alone is faster.

No-build mapping now resolves 48 sampled PCs across engine/interpreter and the
two largest generated leaves. The mapper uses installed Capstone to decode only
3,264 bytes of instruction context, after command-line disassemblers ignored
range selectors; oversized temporary outputs were removed. Bounded scripts,
readable snippets, exact PC mappings and the scoped comparison plan are under
`out/native/runs/20261005-system-trace-current/`, particularly
`generator-overhead-plan.md` and `disasm/pc-map.json`.

Next test a repeated implementation pattern against identical captured-state
generic and AOT execution. Source/cycle profiling must first distinguish needed
guest computation from mask, memory, state-copy or guard overhead. One scoped
owned-Rust experiment must preserve masks, flags, aliases, models and budgets;
it must beat the relevant baseline and demonstrate whole-run relevance before
integration. Keep source/mapping work parallel, timing serialized, one writer,
one cached host build when justified; no frozen-core changes or commits.

### Cycle profile and controlled register comparison (2026-10-05) **[V] [O]**

The retained `699c7312…` runner completed a five-second chord scenario under
CPU Profiler in 19.07 seconds including exports, without a build. The attempt
to add CPU Counters failed before emulator launch because no counting mode was
selected; the configured cycle-based profiler then worked alone. This is not a
hardware-stall/IPC profile. All scenario gates passed, with four built regions
and zero auto-arm/refusals. DSP CPU/audio is 1.26548 and busy/wall 99.768%;
these profiled observations are not a controlled improvement over earlier runs.

Independent raw-XML and analyzer checks agree on the trace-relative [2,6) DSP
slice: 14,579 samples, 14,547,943,921 cycle weight. Leaf origins are generated
blocks 58.26%, engine 14.46%, interpreter entry 12.02%, generic semantics 2.34%,
runtime helpers 3.75%, fast host 0.68%, numeric/unassigned/possible JIT 4.93%,
other 3.57%. The trace also captures the main thread's ColdFire CPU leaves.
Cycle weights are not elapsed time or exact scenario/tier shares; no shared
audio/trace boundary exists. No unresolved frame records does not identify
numeric symbols. Artifacts: `out/native/runs/20261005-cpu-profiler-current/`.

One example-only cached build took 1.64 seconds; frozen runtime/generated rlibs
retained identical size/mtime snapshots, and a watchdog guarded against either
crate compiling. One paired 22.69-second comparison executes the same captured
`1C5765` loop (19 ops × 64 trips, 1,216 instructions) in AOT, generic, cached
owned Rust and owned Rust using architectural reads plus journalled writes.
The owned variants share handlers and per-instruction begin/commit; no runtime
mode branch is added inside operations. Mean ns/instruction over three reps ×
eight calls: AOT 2.7697, generic 76.1004, cached 13.6848, state-backed 14.5029.
All 1,900 seeded/corner cases and every timed full-state/host/boundary gate pass.
Accessible undo/probe state is checked; private jn/snap clearing follows the
unchanged commit implementation. Refusals discard trials; this subset is not
a resumable or general replacement.

State-backed owned execution retains a 5.247x advantage over generic; its cost
is only 5.98% above cached execution. Thus register caching/journal avoidance
does not explain most of this loop's gain. Other direct-handler, repeated field
extraction and dispatch effects remain combined. Cached owned execution still
costs 4.941x AOT. Preserve production routing and the Python-oracle/owned-core
direction; a generic-only change is unlikely to close the current desktop gap.
Artifacts and limitations: `out/native/runs/20261005-register-control/summary.md`.
No commits, frozen source edits, retraining or production-default changes.

### Guided hardware counters and steady-render mode choice (2026-10-05) **[V] [O]**

The configured stock CPU Counters template completed one five-second chord16
recording on the retained `699c7312…` binary, without a Rust build or native
change. All playback-health gates passed, four fixed fast regions built and
zero auto-arm/refusals; no expected PCM hash was supplied. DSP CPU/audio is
1.35732 and busy/wall 99.574%, an observation rather than a speedup comparison.
The wrapper's legacy-schema check stopped after recording; guided Swift tables
were exported from the existing trace without rerunning the emulator.

In the audited [2,6) DSP window, 9,174 disjoint intervals carry 14,377,289,991
cycle weight and cover 99.63% of the window. Cycle-weighted sustainable micro-op
bandwidth fractions are useful 65.7524%, processing 16.6211%, delivery 14.6496%,
discarded 2.9769%. These are not CPU-time, removable overhead or speedup bounds;
useful includes bookkeeping. No per-function stall cause or separate ColdFire
hardware-counter result follows. Time Profiler ancestry confirms the DSP TID.

The first processing remark includes initialization. Within [2,6), precise
delivery warnings span 624 rows / 547.562 ms union coverage, versus processing
37 / 3.419 ms and discarded 72 / 10.587 ms. Precise and imprecise results are
kept separate; warning coverage is not CPU time. Next choose Instruction
Delivery Bottlenecks on the same binary/configuration, then one supported
owned-core/host experiment. The mode was selected in UI, but Save As Template
has not yielded a usable sheet; do not guess archived mode identifiers.

Evidence, scripts, schema recovery and limitations:
`out/native/runs/20261005-cpu-counters-current/analysis.md`.
No commits or frozen-core/default changes. The small benchmark-only generic
pre-resolved-handler control is feasible, preserving transactions/helpers,
but remains a separate owned-core question, not a proven desktop fix.

### Targeted instruction-cache result and stack association (2026-10-05) **[V] [O]**

The delivery recording and exports completed without a build, using unchanged
binary/inputs/fixed-four routing. A workspace template changes only the stock
named JSON mode selection, preserving numeric next/suggested mode settings.
The new UI document and recorded TOC both confirm `bottleneck delivery EL0`;
the single-run wrapper now handles guided tables and rejects mode mismatch.
This resolves the save-sheet limitation and avoids future UI setup.

In complete disjoint [2,6) DSP intervals, cycle-weighted sustainable bandwidth
losses are L1 instruction-cache latency 9.6094%, other latency 3.5935%, delivery
bandwidth 1.3818% and instruction-TLB latency 0.1180% (sum 14.7026%). Sixteen
sentinel intervals are excluded, not zero-filled, accounting for 0.01775% of
observed cycle weight. These are not CPU-time/speedup bounds. All health gates
pass; no expected PCM hash supplied. Scenario CPU/audio 1.37618 is a profiled
observation rather than a speedup comparison.

The same-trace pointwise join matches 3,970 of 3,980 DSP stack points to complete
containing counter intervals, with no nearest-neighbor fill. Conditional iCache
fractions are similar for AOT leaves (9.20%), Engine (8.62%) and exec_insn
(8.77%); this is association, not per-function miss attribution or a partition
of full counters. No actionable single function has been identified. Stop
further broad profiles for this question. The next cheap owned-core control is
pre-resolved same-generic-handler dispatch with unchanged transaction/timer/
loop/probe semantics, seeded differential and captured boundary gates, and one
guarded cached example build. It remains separate from a proven desktop fix.

Evidence and reusable setup:
`out/native/runs/20261005-cpu-delivery-current/analysis.md`.
This counter phase used zero Rust builds and made no production/frozen-core
changes or commits.

### Same-handler form-dispatch control: negative result (2026-10-05) **[V] [O]**

The benchmark-only fifth lane resolves actual Insn.type_name once, then calls
the same generic form-level handler through a function pointer. Both wrappers
are inline-never; field/compute routing, masks, transactions, loop sync, probes,
rollback and timer semantics remain. Source review and all 1,900 seeded cases
plus every warm/measured full-state/host/boundary gate pass. Three repetitions
× eight calls × five lanes give 120 measured calls.

Paired means ns/instruction: AOT 2.9340, generic 76.2747, resolved generic 86.6300,
cached owned 13.4976, architectural/journalled owned 15.1568. Preselected routing
is 13.576% slower in aggregate, and slower in all repetitions. Reject this design
on the tested loop; compiler/PGO/outlined-handler effects are included, so this
does not prove dispatch is free. The owned architectural lane remains 5.032×
faster than generic and 5.166× slower than AOT. No desktop/global speedup.

One initial 0.413-second compiler failure caught a missing import; corrected
without a frozen rebuild. The successful guarded cached example build took
2.665 seconds, frozen runtime/gen rlib snapshots unchanged. One exclusive paired
run took 28.649 seconds including setup/checks. No production/frozen/default
edits, new recording, PGO retraining or commits.

Next batch two separate owned-path controls into one build: repeated field/
compute descriptor parsing versus predecoding, and generic sequencer advance
every instruction versus admitted nonterminal direct advance. Keep canonical
registers, semantic helpers and transaction/timer/budget handling fixed. Source
shows possible repeated work, not measured machine-code overhead. Reuse current
fixtures; reduce redundant oracle export copies without removing comparisons.
Any winner must be competitive with AOT on representative hot work, then earn
coupled CPU/audio/deadline/PCM gates. Evidence:
`out/native/runs/20261005-form-dispatch-control/summary.md`.

### Separate descriptor and sequencing controls (2026-10-05) **[V] [O]**

Two benchmark-only lanes share the owned semantic body and architectural,
journalled registers. One reparses actual instruction fields/compute bits;
the other retains predecoding and invokes the generic sequencer every
instruction. Transactions, semantic helpers, timer ownership and existing
admission guards remain. Complete descriptor equality is checked before timing.

Paired mean ns/instruction on retained 1C5765: predecoded architectural owned
17.3270, reparsing 46.4154, full advance 18.9446, generic 80.8978, AOT 3.0140.
Reparsing costs 2.6788× baseline (+29.0885 ns); full advance costs 1.0934×
(+1.6176 ns), both worse in all three repetitions. Parsing is the larger tested
effect, but neither delta is exact removable generic cost and they must not be
added as a cost partition. The owned lane still costs 5.7488× AOT. No production
or end-to-end improvement is demonstrated.

All 1,900 seeded/corner cases compare six engines (generic plus five controls);
AOT is covered by captured/warm/timed full-state and host/progress gates. All
seven captured lanes pass, including 168 measured calls and 21 warm calls.
The fixture contains only 3c, 5a_move, 3a and 2a; parser 4a/5b_move mappings are
not exercised. Source and result review completed. One guarded cached build
took 3.280 seconds with frozen rlibs unchanged; one exclusive paired run took
41.074 seconds including checks. No recording, frozen/default edit or commit.

Timer coverage is bounded: seeded instruction cases disable core_timer. The
captured SHRD v4 state has known MODE2=0x21 (timer enabled), TPERIOD=1,000,000
and TCOUNT=621,719. Generic execution ticks each instruction; AOT/owned paths
apply the same aggregate decrement in finish_call, reaching 620,503 after
1,216 retirements. begin_call caps work below expiry, and admitted instructions
cannot read/write timer registers. Full-export gates support this non-expiring,
no-timer-write interval only. Expiry/reload/latch/interrupt effects, timer writes
and timer-related trap/rollback remain untested; full_advance isolates the
sequencer, not all generic timer behavior. Static state evidence is retained in
`out/native/runs/20261005-component-controls/timer-coverage.json`.

Keep predecoded descriptors. Next scope all missing semantics/guards for the
retained hot B8276C body (15b, 15a, 6b_shiftimm and multifunction 4a), then
compare broader owned coverage against AOT before coupled testing. Reuse
fixtures and cached builds; stop repeating broad profiles for this question.
Evidence and limitations:
`out/native/runs/20261005-component-controls/summary.md`.
