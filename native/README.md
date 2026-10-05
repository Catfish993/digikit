# Native crates

One cargo workspace (root `Cargo.toml`, one `Cargo.lock`, profiles defined at
the root) holds the crates that build together: `sharc-rt`, `sharc-gen`,
`sharc`, `sharc-fast-cl`, `boot` and its path dependencies (`coldfire`,
`coldfire/cfabi`, `periph`, `card`, `machine`, `loader`, `plusdrive`,
`device-profile`), `live`, `host`, `coupled` (the run loop shared by the app and the
headless runner) and `packages/desktop/src-tauri`.

Left out: `sharc-jit` (wasmtime and wasm32-only runtime crates, its own
lockfile, nothing in the workspace depends on it) and `.claude/` (agent
worktrees live inside the tree and must not become members). A crate is never
built with its own manifest's profile: cargo reads profiles only from the root.

## Profiles

- `release`: timing and shipping profile (opt-level 3, 16 codegen units,
  unwinding panics: what the desktop app has always used; PGO is trained on it).

There is no cheaper profile for correctness-only runs. Measured on `sharc-gen`
(80 MB generated core): an opt-level 1 override costs about 2.5x the CPU of
release (5.5 min wall vs 2.9 min), and opt-level 0 compiles in about 1.4 min but
runs `coupled_ready_exactness` in 150 s instead of a few seconds (and overflows
the default thread stack). Use `release`.

## Target dirs

`sharc-gen` (the generated SHARC+ core, 80 MB of Rust) takes minutes to compile
and cargo recompiles it for each target dir and each `SHARC_GEN_DIR`. So tools
build into `out/native/target/<gen-key>` (`tools/native_target.py`):

| key        | generated core                                         |
|------------|--------------------------------------------------------|
| `prod`     | `out/native/dn2-audio/gen` (desktop app, PGO)          |
| `nogen`    | none (`SHARC_GEN_DIR` unset): CF-only builds, wasm CF  |
| `generic`  | any other transpile-only gen (no `image.rs`)           |
| `g-<hash>` | any other gen with an image: hash of its resolved path |

Everything for one gen goes into its key dir, built with plain `--release`
(the desktop app, `-p sharc-fast-cl --examples`, `native/sharc` tests, the PGO
control build), so `sharc-gen` compiles once. Builds with different RUSTFLAGS
(PGO) get a subdir of the same key: `pgo-instrument/`, `pgo-use/`. Switching gen
never touches another gen's build.

```
export SHARC_GEN_DIR=$PWD/out/native/dn2-audio/gen
T=$(uv run python tools/native_target.py dir)
cargo build --release --locked --target-dir "$T" -p sharc-fast-cl --examples
```

In a git worktree the root is `out/native/target-wt/<worktree-name>/` instead
(`out/` is linked to the main tree's, and two checkouts in one target dir would
invalidate each other). A worktree cannot reuse the main tree's compiled
`sharc-gen` -- different package paths and fresh file mtimes make cargo rebuild
it -- so it pays one compile per gen, then reuses its own dir.

A plain `cargo build` without `--target-dir` uses `./target` (ignored by git).
Wrap heavy builds in `uv run python tools/benchlock.py run --shared -- ...`.

## Headless coupled runner (`coupled-run`) and the matrix

`native/coupled` (crate `digi-coupled`) holds the coupled ColdFire + SHARC+ run
loop that the desktop app used to own: the DSP worker thread behind the DSPI2
link, the 250k-instruction step (`STEP_CHUNK`), PCM handling and link timing.
The app (`packages/desktop/src-tauri`) is a thin shell over it, and
`coupled-run` drives the same `CoupledRuntime::step_chunk` loop with no Tauri, no
webview and no audio device, so the numbers it reports are the app's.

The fixture gates (`coupled_ready_exactness` and the Trig 1 tests) live in
`digi_coupled::gates`; the desktop crate wraps them as ignored tests under their
old names (`desktop_runtime::coupled::tests::...`), with the same asserted hashes.

```
export SHARC_GEN_DIR=$PWD/out/native/dn2-audio/gen
T=$(uv run python tools/native_target.py dir)
cargo build --release --locked --target-dir "$T" -p digi-coupled --features coupled-audio --bin coupled-run
SHARC_FAST_REGIONS=0x1c399a,0x1c3862,0x1c364f,0x1c5765,0xb823db,0x1c1884,0x1cdcef,0x1c3030,0xb85347,0x1c1da8 "$T"/release/coupled-run \
  --syx Digitone_II_OS1.11.syx \
  --dsp-image snapshots/dn2-audio-ready-2026-10-03/digi-audio-dn2-image.bin \
  --dsp-state snapshots/dn2-audio-ready-2026-10-03/digi-audio-m5.snap.dsp \
  --cf-snapshot snapshots/dn2-audio-ready-2026-10-03/digi-audio-m5.snap \
  --scenario idle --scenario trig1-hold --out out/native/runs/x/report.json
```

`--list` shows the scenarios, `--seconds S` overrides the length, `--no-pcm-tap`
skips the PCM hash and statistics and `--no-link-timing` the per-period and link
timers (to A/B their cost). Exit code 2 means a gate
failed (missing blocks, halt, step error, non-finite PCM, wrong expected hash).

### Scenarios (`native/coupled/scenarios.json`)

A scenario is a length (`seconds` of audio, or `instructions` of ColdFire) and a
list of panel events. Times count from the restored ready point: `at_instr` in
ColdFire instructions, `at_s` in audio seconds (DSPI2 frames sent / 1500). Events
fire at chunk boundaries before the next chunk, so a run is a function of its
inputs. `qa_script` also plays the shared QA sequence of
`native/boot/examples/common` (NO at ready and +20M, encoder A at +40M).

| scenario | what it does |
|---|---|
| `idle` | 10 s of audio with no input. |
| `trig1-hold` | QA prelude, then Audition Trig 1 (button 25) pressed at +60M and held to the end. One voice. |
| `kb-chord16` | QA prelude, keyboard mode on (button 18 tapped at +50M), then all 16 trig keys (25..40) pressed at +60M and held: a chromatic chord of several voices. |
| `exactness-100m` | Validation: the `coupled_ready_exactness` workload (QA, Trig 1 down at +60M, up at +100M, 100M instructions). Its PCM hash must be `38d2a322...3ff1f8`. |
| `trig1-noqa-40m` | Validation: `desktop_trig1_capture_without_qa_script` (Trig 1 from ready, 40M instructions, silent). Hash `a50586e8...b75a`. |

The QA prelude matters: from the ready snapshot, Trig 1 pressed with no prelude
gives exact silence (the second validation scenario); the first NO presses
dismiss what swallows the key. `kb-chord16` is the heavy best effort. It is
heavier in audio (distinct pitches 277, 311, 370, 415, 466 Hz ... against one
262 Hz tone; PCM RMS 1.3-1.5x `trig1-hold`) but only slightly heavier in DSP
CPU: the DSP cost is dominated by a constant background (the idle-skipped RTOS
loop, the fast-tier region and the generated blocks that run with no voice at
all), so a voice adds a few percent. Pressing the 8 trig keys without keyboard
mode retriggers one pitch and is not heavier. Recording a pattern by button
script and playing it was tried and does not sound reliably (the first notes are
accepted, later button events are not), so it is not a scenario.

### Report

`report.json` has one object per scenario run: `audio_seconds`, `wall_seconds`,
`real_time_factor`; `cpu` (exact thread CPU time of the DSP worker and of the
ColdFire step thread from `CLOCK_THREAD_CPUTIME_ID`, total and per audio second);
`link_timing` (the `DN2_PROFILE_LINK` counters, as deltas over the run);
`dsp_instructions` (idle-skip / interpreter / AOT block / fast tier counts and
percentages, plus fast-tier builds and compile time); `cf_instructions`
(interpreted / idle fast-forward / ram-clear); `dsp_period_wall` (per-period DSP
wall time against the 0.667 ms deadline: count over, mean, p50/p90/p99/max);
`pcm` (SHA-256, RMS, peak, 100 ms window RMS series); `gates`.

Measurement overhead is a handful of timer reads per 0.667 ms period (the link
timing the app already has behind `DN2_PROFILE_LINK`, plus one per-period wall
time) and one hash/sum pass over the PCM on the ColdFire thread. Nothing runs
per instruction. The engine's tier counters and the worker's CPU time are read
once at the start and once at the end of a run. Measured A/B on `prod` (6 s
`trig1-hold`, three pairs, exclusive): the PCM tap changes ColdFire CPU per audio
second by under 0.01 (0.64 against 0.64); the link timers cost 0-3% of DSP CPU
(1.98 against 1.94 on average), inside the 1.84-2.07 run-to-run spread.

### Matrix (`tools/coupled_matrix.py`)

```
uv run python tools/coupled_matrix.py                       # full default matrix
uv run python tools/coupled_matrix.py --configs prod --scenarios idle --repeats 1
uv run python tools/coupled_matrix.py --pgo out/pgo/x.profdata
```

It builds `coupled-run` once per configuration (the cargo target dir from
`tools/native_target.py`, under a shared benchlock), runs every scenario under the
exclusive benchlock, one process per run, repeats outermost so drift spreads over
all configurations, and writes `out/native/runs/<timestamp>/{report.json,
summary.md,raw/}`. Configurations: `prod` (`out/native/dn2-audio/gen`,
`SHARC_FAST_REGIONS` from the launcher's table, 10 s, 3 repeats), `generic`
(`gen-generic`, interpreter only, 1.5 s, 1 repeat) and `generic-auto` (generic plus
`SHARC_FAST_AUTO=256,64`, 1.5 s, 1 repeat). The interpreter-only configurations run at
about 0.02-0.04 of real time, so their defaults are short; the full default
matrix takes about 10.5 minutes (621 s measured). The validation scenarios run once per
configuration; all configurations must produce the same hash. `--pgo PROFDATA`
builds every configuration with that profile (`pgo-use` subdir, `--target`).
Each run records the 1-minute load average; the summary flags runs that started
under load, because benchlock only excludes work that also takes it.
