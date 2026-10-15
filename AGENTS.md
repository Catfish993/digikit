# digitakt2: agent instructions

Reverse engineering and emulation of the Elektron Digitakt II (OS 1.16) and Digitone II (OS 1.11): a ColdFire
MCF5441x main CPU and a SHARC+ DSP, run natively (desktop app) and in the browser (wasm). The device on the desk is
a Digitakt II on 1.16. Setup: README.md. Results: docs/findings/ (one file per topic, indexed from
docs/FINDINGS.md). Current state and next steps: the newest docs/handovers/HANDOVER-*.md.

## Rules

- Results go in docs/findings/, not in handovers. Marks: **[V]** verified here, **[D]** measured or read once,
  **[O]** open, **[C]** corrects an earlier claim. A second agent checks a finding against the image bytes before
  it is marked [V]. An empty Ghidra caller list is not evidence of dead code.
- Firmware and anything derived from it (`*.syx`, `sections/`, `out/`, `snapshots/`, `apps/web/.local`, card
  images) and users' sample audio: never commit it.
- Never name, copy or quote the vendor DSP toolchain or its files. Cite only public manuals
  (docs/sharc/SOURCES.md).
- Commit verified work at breaking points. There is no remote yet; `main` takes changes through a PR.
- Performance: give the ceiling first, then one paired A/B (tools/devtools/coupled_ab.py) per claim.
- Before work estimated at more than about a day, state its payoff and confidence; recommend stopping when they
  are weak.
- Run the emulator only when a static answer is not enough, and bound runs (`--limit`, `--seconds`). Em runs the
  emulator in the same tree: `sections/.source-sha256` is the SHA-256 of the .syx the sections came from (coupled
  DN2/DT2 runs use per-device ready inputs, not `sections/`).

## Working with agents

- Roles: a reader explores code and returns quotes; an editor applies fully specified edits (then check
  `git diff`); a runner builds, tests and measures. The coordinating agent keeps decisions and orchestration.
- Parallel work runs in persistent lane worktrees: `tools/devtools/lane.sh NAME [BASE]` creates or resets
  `../lanes/NAME` on branch `lane/NAME` and runs `tools/devtools/worktree-setup.sh`, which links `out/`, `sections/`,
  `snapshots/`, `.venv`, the firmware `*.syx` and `apps/web/.local` from the main tree and installs
  `node_modules` with `pnpm install --offline`. Lanes are reused so their Rust build directories stay warm (a
  fresh worktree costs about 25 minutes of cold builds). Use a lane only for parallel native edits or long
  measurements; reading and small edits happen in the main tree.
- A lane may edit and test in its worktree against an oracle (pins, fuzz, PCM equality) and commits on its branch;
  the coordinator reviews the diff and merges. Sections that two lanes append to the end of a findings file
  conflict: resolve them with `git merge-file --union`.
- Lanes log progress, one line per step, to `out/runs/lanes/NAME.log` (`date  step  result  elapsed`), and check
  in after about an hour.
- Never wait with `tail -f` or any other command that does not exit: it blocks the tool call until its timeout. Poll
  with a bound (`sleep N; tail -n 5 FILE`) or use a monitor.
- Lane agents iterate and finish on the quick proof tier (`--quick`, budget about 60 s per tool). The coordinator
  integrates with `mise run verify` plus the smoke matrix (`uv run python -m tools.patchkit.pk_harness run --jobs N`), not by re-running
  every proof tool. Full-tier proofs run only before a merge to `main`. Do not re-run old full proofs to measure a
  baseline that is already recorded.
- Stop only processes you started (by PID or run directory). Never `pkill`/`killall` emulator processes: other lanes
  and Em run them on the same machine.
- Each lane runs its own dev server (own port; `tools/devtools/browser_bench.mjs --url`). Do not change Python dependencies
  in a lane (the virtualenv is shared).

## Verify

- Test layout, commands and the current baseline: docs/TESTING.md.
- `mise run verify` runs the gate steps that the changed files need (`tools/devtools/verify.py`; default: uncommitted
  changes; a lane passes `--base <its base branch>`; `--only STEPS` picks steps; `--explain` prints the routing and plan without running; `--rerun-failed` re-runs only the steps that failed in the last run). `mise run verify:full` runs every
  step with the full 134-scenario tier sweep (about 7 minutes with a warm render cache, about 20 minutes when DSP/native code changed): only after changes to the
  generated core. Otherwise `mise run verify` (routed by the changed files) is the gate, also for a merge to `main`.
- Steps: build, desktop (app crate check), web (node tests and astro check), pins (seven DN2 PCM pins), check (61
  DN2 scenario oracles), check-no-aot, tier-quick / tier-sweep (AOT = no-AOT PCM) (one shared queue of small chunks, DIGI_TIER_JOBS workers, default cpus-4), python-tests (the PY_GATE files, the PY_SUITES the change
  triggers and the changed test files; not all of tests/, which `mise run test` runs) — one process per file (DIGI_PY_JOBS, DIGI_PY_TIMEOUT), rust-tests (cargo test
  --workspace), card
  (in-process DT2 card: trigs play the files), dt2-tone, dt2-check (DT2 scenario oracles), dt2-tier-quick / dt2-tier-sweep (DT2 AOT = interpreter PCM; the full sweep with --full --dt2).
- Scenario renders are cached by content (out/cache/renders; tools/devtools/render_cache.py): a render reruns only when the binary, input files, scenario, peeks, device TOMLs or a SHARC_/DIGI_/DSP_ variable changed. `--no-cache` or DIGI_RENDER_CACHE=0 forces renders; `uv run python -m tools.emulator.coupled cache clear` empties it. Only emulator output is cached; oracles, hash compares and pins run fresh. Guard 1: tests/devtools/test_render_inputs.py fails when a render input (env read or file read) is not classified; regenerate counts with `uv run python -m tests.devtools.test_render_inputs --scan` after review. Guard 2: verify sets DIGI_RENDER_CACHE_CHECK=0.03, which re-renders about 3% of hits and fails on any difference. Use `verify --no-cache` for release gates.
- While working, run only the tests for the code you changed.
- Lint: `tests/devtools/test_lint.py` runs ruff on its `CLEAN` list (new files start clean and join it). Types:
  `tests/devtools/test_types.py` (mypy, Python 3.11) on its `TYPED` list. The SHARC core must also run under PyPy 3.11
  (`tests/devtools/test_pypy.py`, slow).
- Paths: tools/devtools/pathcheck.py fails on references to missing paths; cite a deleted file as `REF:path` (e.g. `unicorn-reference-final:emu/edma.py`).
- `tests/sharc/integration/test_sharc_golden.py` hashes six SHARC core outputs: a refactor keeps them; an intended change updates
  them with `--update` and says why. PCM pins in `tests/scenarios/*.json` change only with Em's approval.

## Build and run

- Python tools: `tools/` is a package with one subpackage per owner (docs/LAYOUT.md); the repository root is the only import root. Run a tool as `uv run python -m tools.<owner>.<module>` from the repository root and import it as `from tools.<owner> import <module>`. Never change `sys.path`. A tool that starts another tool runs `[sys.executable, "-m", "tools.<owner>.<module>", ...]` with `env=tools.python_env()`.
- mise tasks: `mise run emu [-- FIRMWARE.syx]` (desktop app; `-- --build-only` builds without launching), `emu:ctl`, `web` (browser dev page),
  `web:bench`, `scenario NAME`, `scenarios`, `scenarios:check`, `scenarios:dt2`, `perf`, `pgo` (both devices, then rebuilds the launcher), `pgo:dn2`, `pgo:dt2`, `gen:core`.
- Paths, toolchain and fast-region tables: tools/emulator/digi_env.py, tools/emulator/fast_regions.json.
- SHARC crates: packages/emulator/sharc-rt (runtime), packages/emulator/sharc-gen (the generated core from `SHARC_GEN_DIR`; relative
  paths resolve from the repo root), packages/emulator/sharc (engine and fast tier), packages/emulator/sharc-fast-cl and
  packages/emulator/sharc-fast-wasm (kernel backends). Editing the engine or fast tier rebuilds in about 20 s; editing
  sharc-rt rebuilds the generated core (4 to 12 minutes).
- A change to tools/sharc_dev/sharc_core, tools/sharc_dev/sharc_transpile/, tools/sharc_dev/sharc_transpile_infer.py or tools/sharc_dev/sharc_rsgen.py changes `CORE_FINGERPRINT` (it hashes those files' paths relative to tools/sharc_dev and their bytes):
  regenerate every core (DN2 and DT2 from out/native/{dn2,dt2}-audio/gen/PROVENANCE.txt, the shared core with
  `mise run gen:core`), backing up each gen dir first.
- `out/native/target/*/pgo-use` belongs to the launcher; experiments build elsewhere (`DIGI_EMU_PGO_VARIANT`).
  PGO profiles are per device (`mise run pgo:dn2`, `mise run pgo:dt2`, or `mise run pgo` for both; underlying
  `uv run python -m tools.emulator.native_pgo train [--device dt2]`); retrain at milestones.
- Builds and timed runs share the machine through tools/devtools/benchlock.py (shared for builds, exclusive for
  benchmarks). tools/devtools/coupled_ab.py, native_pgo.py and sharc_regionbench.py take the lock themselves (wrapping them
  deadlocks).
- The shell is zsh: an unquoted `$VAR` is one word. There is no `timeout` binary.
- Extract firmware: `uv run python -m tools.firmware.extract SYX -o DIR`.
- The Python Unicorn reference emulator was removed; the last commit with it is tagged `unicorn-reference-final`.
- Manuals: read `out/refs/<pdf stem>/` (`toc.md`, `pages/pNNNN.txt`, `all.txt`), built with
  `uv run --with pymupdf python -m tools.devtools.refstext`.

## Instrumentation (details in docs/INSTRUMENTATION.md)

- App real time: `DIGI_SPIKE_LOG=DIR mise run emu`, then `uv run python -m tools.devtools.spikes DIR`; unattended runs:
  `uv run python -m tools.devtools.app_soak`; app audio capture: `DIGI_EMU_SINK_WAV=FILE`.
- Browser throughput: `mise run web:bench`. DSP performance: `mise run perf`, tools/devtools/perf.py, tools/devtools/coupled_ab.py.

## Static analysis

- SHARC: `uv run python -m tools.sharc_dev.sharcdb build out/sections/*/section_7_BLOB.bin` writes
  `out/sharcdb/<image>.sqlite`; use it through tools/sharc_dev/sharc.py (`img = sharc.load("dt2-1.16")`, then `func`,
  `callers`, `callees`, `reach`, `xref_table`, `trace`, `img.sql(...)`) before writing a script. New kinds of fact
  go into tools/sharc_dev/sharcdb/. SHARC+ encodings: tools/sharc_dev/sharcspec/ (from the public ADI manuals).
- SHARC+ instruction semantics live once, in tools/sharc_dev/sharc_core/; tools/sharc_dev/sharc_trace.py (symbolic) and
  tools/sharc_dev/sharc_run.py (concrete) are drivers only.
- ColdFire (MAIN OS): a `uv run python -m tools.coldfire.ghidradump` dump (out/ghidra/<image>-emac/), then
  `uv run python -m tools.coldfire.cfdb build out/ghidra/dt2-1.16-emac` and tools/coldfire/cf.py (`cf.load("dt2-1.16-emac")`);
  hand-curated 1.16 names in tools/coldfire/cf_names.py. tools/emulator/snapread.py reads `.snap` files without a machine.
- Ghidra: one JVM per project. Live 1.16 analysis: `~/ghidra-projects/elektron-emac`; `dt2-emac` holds 1.15C. Use
  the language `68000:BE:32:ColdfireEMAC` (the stock ColdFire language stops at `movclr`). Read the dump first;
  live queries with tools/coldfire/ghidraq.py (`--then` chains queries in one JVM). Ghidra's reference tables miss code
  outside functions: confirm "no caller" with tools/coldfire/refscan.py. A pyghidra tool runs as a module like every tool; by
  script path its directory, which holds the Java scripts in `ghidra/`, would shadow PyGhidra's `ghidra` package. After a Ghidra upgrade,
  re-run tools/coldfire/ghidra/install-coldfire-emac.sh. For the generated SHARC+ language, measure before and after with
  `uv run python -m tools.sharc_dev.sharcpcode measure` and `compare` (one language version per JVM).
