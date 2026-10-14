# Instrumentation

Diagnostics are off by default and cost nothing when off. Tuning switches
(which tier runs what) are separate and listed at the end.

## Where output goes

Every run tool writes one directory per run:

```
out/runs/<scenario-or-kind>/<UTC stamp>/
    report.json   coupled-run report (tools/emulator/coupled.py); perf: perf.json + report.md
    audio.wav     with --wav or --diag pcm
    logs/         everything `--diag` records, plus stderr.log
out/runs/<scenario-or-kind>/latest -> <newest stamp>
```

`tools/emulator/coupled.py scenario|replay`, `tools/devtools/perf.py` (`perf`) and
`tools/devtools/fastcov.py` (`fastcov`) use it. `--out` still overrides: coupled.py puts
the report at that path and audio.wav and logs/ next to it; perf.py and
fastcov.py take a run directory. Caches stay outside runs: the perf cost
calibration and ranking (`out/native/perf/`), fastcov entry lists
(`out/native/fastcov/`), bisect references (`out/native/bisect/`).
`tools/emulator/digi_env.py` holds the layout (`run_dir`) and the diagnostics table
(`DIAG`, `diag_env(names, run_dir)`).

## `--diag LIST`

`uv run python -m tools.emulator.coupled scenario NAME --diag dspi,wwatch=1a0:4,hist`.
A name that takes a value is `name=VALUE`; a comma-separated value is fine
(`pcdump=0x100,0x200`). Setting any name also sets `DIGI_DIAG_DIR=<run>/logs`,
the default directory for every native `*_OUT` that is unset.

| name | records | file in logs/ | variables |
|---|---|---|---|
| `dspi` | DSPI traffic (binary, `DSPILOG1`) | `dspi.bin` | `DIGI_DSPI_LOG` |
| `dspwatch=ADDR[,ADDR]` | DSP words, one row per frame | `dspwatch.csv` | `DIGI_DSP_WATCH`, `_OUT` |
| `model` | per-frame DSP tier counters | `model.csv` | `DIGI_DSP_MODEL_LOG` |
| `wwatch=ADDR[:N],..` | changes to DSP words: icount, PC, unit | `wwatch.csv` | `SHARC_WWATCH`, `_OUT` |
| `pcdump=PC,..` | registers at those PCs | `pcdump.csv` | `SHARC_PCDUMP`, `_OUT`, `_ICOUNT=lo:hi`, `_LIMIT`, `_MEM=addr:words` |
| `fastcov` | fast-tier coverage, refusals, exits, interpreter histogram | `stderr.log` | `SHARC_FAST_LOG=summary`, `SHARC_FAST_COV`, `SHARC_FAST_HIST` |
| `fastlog` | fast-tier summary | `stderr.log` | `SHARC_FAST_LOG=summary` |
| `hist` | interpreted instructions per PC | `stderr.log` | `SHARC_FAST_HIST` |
| `dump` | fast-tier kernels and real calls (wasm gate input) | `fastdump/` | `SHARC_FAST_DUMP`; needs the `fast-dump` feature (coupled.py adds it) |
| `pcm` | the rendered audio | `../audio.wav` | `--wav` |

`DSP_FRAME_POKE=OFF=VAL[/TRACKS][,...]` sets the big-endian 16-bit word at frame byte OFF (with `/N`, at
OFF + 0x60*t for t < N) in every frame sent to the DSP, to drive a frame word no stock control reaches
(finding 06, "Parameter channel"). `tools/patchkit/hook_ab.py` renders and compares paired runs with a DSP patch.

`wwatch` units: `i` interpreter step, `b` generated block, `f` fast tier,
`s` idle skip; for exact writer PCs also run with `SHARC_AOT=0
SHARC_FAST_REGIONS= SHARC_BLOCKTRANS=`. `fastcov` output is read by
`tools/devtools/fastcov.py` (`Diag`); `perf.py` and `fastcov.py` run it themselves.

DSP memory dumps: `uv run python -m tools.emulator.coupled scenario NAME --dump-dsp` (or
`coupled-run --dump-dsp PATH`) writes the DSP's canonical state blob (`SHRD`,
every byte written since the image, as ranges; the format of
`tools/sharc_dev/sharc_diff.py`) to `<run>/dsp-<scenario>.bin` at the end of the run.
`tools/emulator/dspdiff.py A B [C..] [--image dn2-1.11] [--ignore-volatile --noise X Y]`
lists the differing words per range with float32 values and the functions that
reference them. Runs are deterministic, so two dumps of one scenario are equal
and scenarios with identical event times give dumps at the same DSP instruction
count.

Not in `--diag` (set the variable by hand): `SHARC_LIVE_AOT_CAPTURE_PC/_DIR/_LIMIT`
(raw block-call snapshots), `SHARC_FAST_DUMP_ALL`, `SHARC_FAST_CL_DISASM`,
`DIGI_LP0_LOG` (stderr), `DN2_PROFILE_LINK=1`,
`DIGI_EMU_SINK_WAV`, `DIGI_EMU_PCM_F32LE` + `DIGI_EMU_PCM_SECONDS` (desktop audio
captures), `DIGI_COUPLED_PCM_F32LE`, `DIGI_COUPLED_DSP_EXPORT` (the pinned test),
`DIGI_PANEL_TX_LOG=PATH` (front-panel LED bytes from UART8: one line `icount hex` per drain), `DIGI_INTC_FORCE=0` (turn off delivery of guest-forced INTC sources, INTFRCH/INTFRCL: the
sequencer step ISRs; on by default since 2026-10-13, always on in wasm; for A/B only; finding 19).

## Cost when off

- Interpreter loop: `cov`, `hist`, `wwatch` and `pcdump` are tested through one
  flag (`Engine::diag_on`) and handled in a cold function. `wwatch` is also
  tested once per dispatched block.
- Fast tier call path: the `dump` hooks exist only with the `fast-dump` cargo
  feature (without it `dump::enabled()` is constant false). Environment
  lookups on that path are read once (`OnceLock`). The other env lookups in `fast/` run at build or
  decline-reporting time only.
- Boot: `sharc_peer` reads its diagnostics once at start.

## Cargo features

| crate | feature | effect |
|---|---|---|
| `packages/emulator/sharc` (`coupled`: same names) | `fast-dump` | `SHARC_FAST_DUMP` capture hooks |
| | `region-profile` | per-entry wall time (`perf.py --regions`; needs `DIGI_COUPLED_FEATURES=coupled-audio,region-profile`) |
| `packages/emulator/boot` | `cf-profile` | host-time and PC profile of ColdFire |
| | `diagnostic-profile`, `-events` | boot event records and profile |
| `packages/emulator/periph` | `trace` (default) | DT2MMIO trace reader and `mmio-replay`; no runtime hook. Every product crate (`boot`, `card`, `host`, `machine`) takes periph with `default-features = false`, so a product build does not compile it. Turning the default off changes only a direct `cargo build -p periph`. Left as is. |

## Tuning switches (not diagnostics)

Read at start-up; defaults are what `tools/emulator/digi_env.py` and the shipped
`tools/emulator/fast_regions.json` set. Status **[D]**: read from the code, not re-run.

| variable | effect |
|---|---|
| `SHARC_AOT=0`, `SHARC_AOT_DENY` | generated block tier off / blocks denied |
| `SHARC_FAST_REGIONS`, `SHARC_BLOCKTRANS` | fast region entries, block-translator roots (`@FILE` allowed) |
| `SHARC_FAST_AUTO=a,b`, `SHARC_FAST_FOLLOW`, `SHARC_FAST_ONLY` | run-time region selection, extra entries, restrict to listed |
| `SHARC_FAST_GEN` | generated lowering; default on; `0` = hand lowering only |
| `SHARC_FAST_HELPER` | generated helper calls |
| `SHARC_FAST_NOBACKOFF`, `_NOGUARD` | switch one fast-tier mechanism off (A/B controls) |
| `SHARC_FAST_CL_NOVERIFY`, `SHARC_FAST_BACKEND` | backend checks and plug-in path |
| `DIGI_DSP_PERIOD`, `DIGI_DSP_RX_PHASE` | DSP scheduling in the coupled run |
| `DIGI_EMU_FAST=0` | tools/emulator/digi_env.py: no fast regions |
| `SHARC_FAST_SHAPE=cfg`, `SHARC_FAST_CHAIN=0` | legacy variants **[O]** likely obsolete; check before use |

## Spike log (`DIGI_SPIKE_LOG`)

Per-window timing for real-time dropouts, in the app and in `coupled-run`.
Set `DIGI_SPIKE_LOG=DIR`. The DSP worker writes `DIR/dsp.jsonl` (one record
per 0.1 audio s: busy time, worst period, time spent waiting for frames,
fast-tier build and compile time). The coupled run loop writes `DIR/cf.jsonl`
(one record per ~20 ms of wall time: `step_chunk` time, the gap between calls,
the wait for DSP replies, the audio queue). Setting it turns link timing on.
Read it with `uv run python -m tools.devtools.spikes DIR`: it names the largest cost
of every window that fell behind real time (dsp, compile, cf, ui-gap).
`tools/devtools/app_soak.py` drives the running app over its control socket (Trig 1 taps and page changes) for an unattended run.

## Browser bench (`mise run web:bench`)

Headless Chrome/Chromium throughput of the coupled browser core (DN2, local
inputs): `mise run web:bench -- --seconds 10 --runs 3 [--js-flags "..."]
[--profile]` (exclusive bench lock, own `astro dev` on port 4399;
`tools/devtools/browser_bench.mjs`, page mode `/?bench=1&seconds=N` in
`apps/web/src/runtime/bench.ts`). Prints JSON: audio s per wall s (whole
run, first and last half, per audio second), worker busy time inside the step
call, DSP and ColdFire instructions per audio s. `--profile` adds a CDP CPU
profile of the worker (DSP / ColdFire / idle split by wasm function name, top
functions). Needs the firmware at the repo root or `DIGI_EMU_SYX`, and
`apps/web/.local/emulator-core-sharc.wasm`. Chrome is used if installed,
else Chromium or Edge (`--chrome PATH`).

The page runs the ColdFire and the DSP in two workers (SharedArrayBuffer link,
dev server sends COOP/COEP) when cross-origin isolated; `--split 0` forces the
single worker. The JSON has `mode` (`single`/`split`) and, in split mode,
`cf_worker_busy_per_audio_s` (step time minus time blocked on the DSP),
`cf_worker_blocked_per_audio_s` and `dsp_worker_busy_per_audio_s`; `pcm_peak`
and `missing_blocks` check that audio is produced. Exactness of the split:
`node tools/devtools/wasm_parity.mjs --scenario NAME --split 1` (two instances in two
threads) must print the same PCM hash as without `--split`.
