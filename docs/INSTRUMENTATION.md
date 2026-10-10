# Instrumentation

Diagnostics are off by default and cost nothing when off. Tuning switches
(which tier runs what) are separate and listed at the end.

## Where output goes

Every run tool writes one directory per run:

```
out/runs/<scenario-or-kind>/<UTC stamp>/
    report.json   coupled-run report (tools/coupled.py); perf: perf.json + report.md
    audio.wav     with --wav or --diag pcm
    logs/         everything `--diag` records, plus stderr.log
out/runs/<scenario-or-kind>/latest -> <newest stamp>
```

`tools/coupled.py scenario|replay`, `tools/perf.py` (`perf`) and
`tools/fastcov.py` (`fastcov`) use it. `--out` still overrides: coupled.py puts
the report at that path and audio.wav and logs/ next to it; perf.py and
fastcov.py take a run directory. Caches stay outside runs: the perf cost
calibration and ranking (`out/native/perf/`), fastcov entry lists
(`out/native/fastcov/`), bisect references (`out/native/bisect/`).
`tools/digi_env.py` holds the layout (`run_dir`) and the diagnostics table
(`DIAG`, `diag_env(names, run_dir)`).

## `--diag LIST`

`uv run python tools/coupled.py scenario NAME --diag dspi,wwatch=1a0:4,hist`.
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

`wwatch` units: `i` interpreter step, `b` generated block, `f` fast tier,
`s` idle skip; for exact writer PCs also run with `SHARC_AOT=0
SHARC_FAST_REGIONS= SHARC_BLOCKTRANS=`. `fastcov` output is read by
`tools/fastcov.py` (`Diag`); `perf.py` and `fastcov.py` run it themselves.

DSP memory dumps: `uv run python tools/coupled.py scenario NAME --dump-dsp` (or
`coupled-run --dump-dsp PATH`) writes the DSP's canonical state blob (`SHRD`,
every byte written since the image, as ranges; the format of
`tools/sharc_diff.py`) to `<run>/dsp-<scenario>.bin` at the end of the run.
`tools/dspdiff.py A B [C..] [--image dn2-1.11] [--ignore-volatile --noise X Y]`
lists the differing words per range with float32 values and the functions that
reference them. Runs are deterministic, so two dumps of one scenario are equal
and scenarios with identical event times give dumps at the same DSP instruction
count.

Not in `--diag` (set the variable by hand): `SHARC_LIVE_AOT_CAPTURE_PC/_DIR/_LIMIT`
(raw block-call snapshots), `SHARC_FAST_DUMP_ALL`, `SHARC_TRAP_REGS` (registers
not fully known at a trap), `SHARC_FAST_KEYS` (with `SHARC_FAST_COV`),
`SHARC_FAST_LEAN_STATS`, `SHARC_FAST_CL_DISASM`, `SHARC_FAST_DEBUG_WIN|REQ|ABS|HELPER`,
`SHARC_CONCRETISE_ON_BAIL`, `DIGI_LP0_LOG` (stderr), `DN2_PROFILE_LINK=1`,
`DIGI_EMU_SINK_WAV`, `DIGI_EMU_PCM_F32LE` + `DIGI_EMU_PCM_SECONDS` (desktop audio
captures), `DIGI_COUPLED_PCM_F32LE`, `DIGI_COUPLED_DSP_EXPORT` (the pinned test).

## Cost when off

- Interpreter loop: `cov`, `hist`, `wwatch` and `pcdump` are tested through one
  flag (`Engine::diag_on`) and handled in a cold function. `wwatch` is also
  tested once per dispatched block.
- Fast tier call path: the `dump` hooks exist only with the `fast-dump` cargo
  feature (without it `dump::enabled()` is constant false). Environment
  lookups on that path are read once (`OnceLock`), including
  `SHARC_FAST_DEBUG_REQ`. The other env lookups in `fast/` run at build or
  decline-reporting time only.
- Boot: `sharc_peer` reads its diagnostics once at start.

## Cargo features

| crate | feature | effect |
|---|---|---|
| `native/sharc` (`coupled`: same names) | `fast-dump` | `SHARC_FAST_DUMP` capture hooks |
| | `region-profile` | per-entry wall time (`perf.py --regions`; needs `DIGI_COUPLED_FEATURES=coupled-audio,region-profile`) |
| | `fast-prof` | per-phase ticks of the region call, printed with `SHARC_FAST_LOG` |
| `native/boot` | `cf-profile` | host-time and PC profile of ColdFire |
| | `diagnostic-profile`, `-events`, `-trace` (`diagnostics` = all three) | boot event and trace records |
| `native/periph` | `trace` (default) | DT2MMIO trace reader and `mmio-replay`; no runtime hook. Every product crate (`boot`, `card`, `host`, `machine`) takes periph with `default-features = false`, so a product build does not compile it. Turning the default off changes only a direct `cargo build -p periph`. Left as is. |

## Tuning switches (not diagnostics)

Read at start-up; defaults are what `tools/digi_env.py` and the shipped
`tools/fast_regions.json` set. Status **[D]**: read from the code, not re-run.

| variable | effect |
|---|---|
| `SHARC_AOT=0`, `SHARC_AOT_DENY` | generated block tier off / blocks denied |
| `SHARC_FAST_REGIONS`, `SHARC_BLOCKTRANS` | fast region entries, block-translator roots (`@FILE` allowed) |
| `SHARC_FAST_AUTO=a,b`, `SHARC_FAST_FOLLOW`, `SHARC_FAST_ONLY` | run-time region selection, extra entries, restrict to listed |
| `SHARC_FAST_GEN` | generated lowering; default on; `0` = hand lowering only |
| `SHARC_FAST_HELPER`, `_HELPER_INTERP`, `_HELPER_NODYN` | generated helper calls and their fall-backs |
| `SHARC_FAST_NOBACKOFF`, `_NOCIRC`, `_NOCONT`, `_NOEXIT0`, `_NOVOLATILE`, `_NOGUARD` | switch one fast-tier mechanism off (A/B controls) |
| `SHARC_FAST_CTXCLEAR`, `SHARC_FAST_CL_NOVERIFY`, `SHARC_FAST_BACKEND` | backend checks and plug-in path |
| `DIGI_DSP_PERIOD`, `DIGI_DSP_RX_PHASE` | DSP scheduling in the coupled run |
| `DIGI_EMU_FAST=0` | tools/digi_env.py: no fast regions |
| `SHARC_FAST_FLAT`, `SHARC_FAST_LEAN`, `SHARC_FAST_SHAPE=cfg`, `SHARC_FAST_CHAIN=0` | opt-in or legacy variants **[O]** likely obsolete: flat access gave no coverage gain (findings 18); check before use |

## Spike log (`DIGI_SPIKE_LOG`)

Per-window timing for real-time dropouts, in the app and in `coupled-run`.
Set `DIGI_SPIKE_LOG=DIR`. The DSP worker writes `DIR/dsp.jsonl` (one record
per 0.1 audio s: busy time, worst period, time spent waiting for frames,
fast-tier build and compile time). The coupled run loop writes `DIR/cf.jsonl`
(one record per ~20 ms of wall time: `step_chunk` time, the gap between calls,
the wait for DSP replies, the audio queue). Setting it turns link timing on.
Read it with `uv run python tools/spikes.py DIR`: it names the largest cost
of every window that fell behind real time (dsp, compile, cf, ui-gap).
