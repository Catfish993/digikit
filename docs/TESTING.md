# Tests

Python tests are in `tests/`, grouped by the code they test. Web tests and Rust
tests live with their code.

## Layout

| Directory | What it tests | Main code under test |
| --- | --- | --- |
| `tests/sharc/semantics` | SHARC+ instruction semantics; interpreter against native lockstep | `tools/sharc_dev/sharc_core`, `tools/sharc_dev/sharc_run.py` |
| `tests/sharc/trace` | The symbolic tracer | `tools/sharc_dev/sharc_trace.py` |
| `tests/sharc/generation` | Assembler, transpiler, Rust generator, lowering, generated p-code language | `tools/sharc_dev/sharc_transpile*.py`, `tools/sharc_dev/sharc_rsgen.py`, `tools/sharc_dev/sharc_lowergen*.py`, `tools/sharc_dev/sharcpcode.py`, `tools/sharc_dev/sharcspec` |
| `tests/sharc/analysis` | Loader, disassembler, sharcdb and the other analysis tools | `tools/sharc_dev/sharcldr.py`, `tools/sharc_dev/sharc_disasm.py`, `tools/sharc_dev/sharcdb/`, `tools/sharc_dev/sharc*.py` |
| `tests/sharc/integration` | Firmware-backed harness, replay, golden hashes, capture and peer | `tools/sharc_dev/sharc_harness/`, `tools/sharc_dev/sharc_replay.py`, `emu/sharc_capture.py`, `emu/sharc_peer.py` |
| `tests/patchkit` | Patchkit, module primitives, DSP voice rig, PLUCK, master drive, proofs runner. `smoke/` is the emulator smoke tour (`matrix.toml`, `tours/`, `goldens.json`). `pk_src/` holds small patch sources. | `tools/patchkit/patchkit.py`, `tools/patchkit/pk_*.py`, `tools/patchkit/pk_harness.py` |
| `tests/coldfire` | ColdFire analysis: cf/cfdb, Ghidra tools, symbols, RTTI, vtables, the ISA oracle | `tools/coldfire/cf.py`, `tools/coldfire/cfdb.py`, Ghidra tools |
| `tests/emulator` | Launcher, PGO, target directories, coupled runner helpers, live audio, wire traces, bootstrap | `tools/emulator/native_emu.py`, `tools/emulator/native_pgo.py`, `tools/emulator/coupled.py` |
| `tests/control` | The control-socket client: fake-socket contract tests and daemon tests (`DIGI_EMUD`) | `tools/control/digi_client.py` |
| `tests/firmware` | `.syx` container, aPLib/ELZ, device profiles, firmware atlas, snapshots, machine patch planning, +Drive format | `dt2/`, `emu/` |
| `tests/dsp` | DSP maps and diffs, FX scenario generators, audio checks | DSP tools |
| `tests/devtools` | Lint (ruff `CLEAN` list), types (mypy `TYPED` list), PyPy, benchlock, verify routing, test_outcomes | `tools/devtools/verify.py`, `tools/devtools/benchlock.py`, `tools/devtools/test_outcomes.py` |
| `tests/scenarios` | Emulator scenario oracles. Slow; run by the verify steps `check` and `dt2-check` and by `mise run scenarios:*`. | `scenarios/`, `tools/emulator/coupled.py` |
| `tests/support` | Helpers shared by tests. No `test_` prefix, not collected. `tests/support/paths.py` gives `REPO_ROOT`, `TESTS`, `TOOLS`. | |
| `tests/golden`, `tests/fixtures` | Reference hashes and small fixtures | |

Other tests:

- `apps/web/tests` holds the web app's node tests (`pnpm test`).
- Rust tests live in their crates (`cargo test`, run through `tools/devtools/rust_ws.py`).

## Writing a test

- Put the test in the directory of the code it tests.
- Import repo paths from `support.paths`. Never count parent directories.
- Import tools as `from tools.<owner> import <module>`. pytest puts the repository root and `tests/` on `sys.path` (`pythonpath` in pyproject.toml); never change `sys.path` in a test. A test that runs a tool in a subprocess uses `[sys.executable, "-m", "tools.<owner>.<module>", ...]` with `env=python_env()` (`from tools import python_env`).
- Share helpers through `tests/support`. Never import another test module.
- Test file basenames must be unique across `tests/`. pytest uses its default import mode and there is no `__init__.py`.
- Mark long firmware runs `@pytest.mark.slow`. They are skipped unless `--slow` is given.
- A test that needs firmware or generated artifacts skips with a reason when they are absent.
- A new Python file joins the `CLEAN` list in `tests/devtools/test_lint.py`.

## Inputs tests need

In a clean checkout (no firmware, no `out/`), 2005 tests pass and 554 skip. Each skip names the input it lacks.
`pytest -rs` lists the reasons.

| Input | Skip message looks like | How to provide | Tests |
| --- | --- | --- | --- |
| Stock firmware `.syx` in the repo root (DT2 1.16/1.17, DN2 1.11/1.12; 1.15C/1.10E for old analysis) | "needs Digitakt_II_OS1.16.syx in the repo root", "stock firmware images not present" | README "Firmware" (files come from Elektron's support site; `DIGI_EMU_SYX` and `DIGI_EMU_SYX_DT2` override paths) | `tests/firmware`, `tests/patchkit`, `tests/emulator/test_bootstrap.py` |
| Extracted sections `out/sections/<image>/` | "out/sections/dt2-1.16/section_7_BLOB.bin is not available", "no extracted sections", "DT2 1.16 image absent" | `uv run python -m emu.extract Digitakt_II_OS1.16.syx -o out/sections/dt2-1.16` (likewise `dn2-1.11`; README "Extract the firmware") | `tests/sharc/*`, `tests/coldfire`, `tests/dsp` |
| SHARC program database `out/sharcdb/<image>.sqlite` | "out/sharcdb/dt2-1.16.sqlite is not available", "no program database" | `uv run python -m tools.sharc_dev.sharcdb build out/sections/*/section_7_BLOB.bin` (needs the sections) | `tests/sharc/analysis` |
| ColdFire Ghidra dump `out/ghidra/<image>-emac` and its cfdb | "out/ghidra/dt2-1.16-emac missing", "no cfdb for dt2-1.17" | Needs Ghidra: a `tools/coldfire/ghidradump.py` dump, then `uv run python -m tools.coldfire.cfdb build out/ghidra/<image>-emac` (AGENTS.md "Static analysis") | `tests/coldfire`, `tests/firmware/test_fw.py` |
| Native SHARC library (`DEFAULT_LIB` in `tools/sharc_dev/sharc_transpile_run.py`; `SHARC_NATIVE_LIB` overrides) | "no native library built", "native core not built (packages/emulator/sharc)" | `mise run sharc:native-lib` (firmware-free; builds the shared core first; output in `out/native/shared-core/target`). The shared-core library is preferred when present; else the image-specific library at `out/native/opt/target-final` (perf/AOT work; may be stale, and an old one that lacks symbols makes the tests skip); `SHARC_NATIVE_LIB` overrides. | `tests/sharc/semantics`, `tests/sharc/generation` |
| Generated shared core `out/native/shared-core/gen` | "no shared core generated here" | `mise run gen:core` | `tests/sharc/generation/test_sharc_lowergen.py` |
| Module toolchains in `out/tools` (m68k gcc/binutils, Selache) | "selache not built (tools/patchkit/pk_toolchains.py selache)", "m68k toolchain not installed" | `mise run toolchains` (about 15 min; `uv run python -m tools.patchkit.pk_toolchains status` shows what is present; docs/PATCHING.md) | `tests/patchkit` |
| `digi-emud` daemon | "needs DIGI_EMUD (a built digi-emud) and the DN2 .syx" | Build it with `mise run emu:serve -- --build-only` (per `tools/control/digi_client.py`), set `DIGI_EMUD` to the binary (`DIGI_EMUD_DN2` / `DIGI_EMUD_DT2` for coupled runs; `tools/emulator/native_emu.py --daemon --build-only <syx>` prints the path), and have the `.syx` (`DIGI_EMU_SYX`) | `tests/control`, `tests/patchkit/test_master_drive_emu.py`, `tests/emulator/test_coupled_patched.py` |
| Ready inputs, `snapshots/*.snap` | "no snapshots/*.snap present", patched ready inputs missing | `mise run emu:ready -- dt2` or `-- dn2` (about 2 min; needs the `.syx`; README) | `tests/firmware/test_snapread.py`, `tests/emulator/test_coupled_patched.py` |
| Captures `out/captures/*.dt2cap` | "the fulltx play capture is not available", "no drive3 capture" | Not reproducible from the repository alone; these tests stay skipped for most contributors. | `tests/sharc/integration`, `tests/sharc/generation` |
| Opt-in switches | "set LIVE_AUDIO_DEVICE_TESTS=1 to open a real output device", "packages/emulator/live not built: cd packages/emulator/live && cargo build --release" | `LIVE_AUDIO_DEVICE_TESTS=1` opens a real audio device; `cd packages/emulator/live && cargo build --release` builds the live library; `--slow` runs the long firmware runs. A `SELACHE_DIR` oracle switch is not confirmed (see `tools/patchkit/pk_toolchains.py`, which defines `SELACHE_DIR` as a path, not an environment variable). | `tests/emulator/test_live_audio.py`, tests marked `slow` |

## Commands

| Command | What runs | When |
| --- | --- | --- |
| `mise run test:quick` | Launcher, PGO, target and coupled-tool tests in `tests/emulator`, then `tools/emulator/coupled.py test-quick` | Fast check of the tooling, audio sink and coupled runtime |
| `mise run test` | `uv run python -m tools.devtools.test_outcomes run --check --out out/runs/tests/latest tests` (each test file in its own process, 4 in parallel by default, exits 1 when a file fails), then `tools/devtools/rust_ws.py test`, `pnpm test`, every DN2 scenario pin | Everything except the slow gates |
| `mise run test:slow` | The same runner with `--pytest-arg=--slow` and a one-hour per-file timeout, then `tools/devtools/rust_ws.py gates` | Slow runs and the ignored Rust gates that need private fixtures |
| `mise run lint` | `lint:py` (ruff check and format check on the `CLEAN` list), `lint:web` (oxlint), `lint:rust` (cargo fmt check) | Before a commit |
| `mise run lint:py` | `tests/devtools/test_lint.py` only | Python files changed |
| `mise run check` | `lint` and `test:quick` | Quick local check. Not the merge gate. |
| `mise run verify` | The gate steps the changed files need (`tools/devtools/verify.py`). The `python-tests` step runs `PY_GATE`, the `PY_SUITES` whose trigger prefixes match, changed `tests/**/test_*.py` files, and users of a changed `tests/support` module. `--explain` shows the plan without running. `--rerun-failed` re-runs only the steps that failed last time (reads the run's result.json). | Before a commit and before a merge to `main`; a lane passes `--base BRANCH` |
| `mise run verify:full` | Every step, with the full 134-scenario tier sweep (about 7 min with a warm render cache, about 20 min when DSP/native code changed) | After changes to the generated core (`mise run verify` is the merge gate otherwise) |
| `mise run scenarios` | Every DN2 scenario; fails when a pinned PCM hash differs | PCM pin check (renders are cached, see Render cache) |
| `mise run scenarios:check` | `pytest tests/scenarios --slow`: DN2 FX oracles, fast subset (about 3 min) | After DSP or scenario changes |
| `mise run scenarios:dt2` | DT2 FX oracles on the one-sample card (about 2 min) | After DT2 changes |
| `mise run scenarios:sweep` | Oracles with the full sweeps (about 20 min) | Before a merge |
| `mise run patch:test` | Smoke tour and module tests over `tests/patchkit/smoke/matrix.toml` | After patchkit or module changes |
| `mise run patch:proofs` | Every emulator proof on its quick tier, in sequence | After patchkit or module changes |
| `uv run python -m tools.devtools.test_outcomes run [--check] --out DIR [--root PATH] [--jobs N] tests` | Each test file in its own process with a per-file timeout. Writes `DIR/all.xml` and `summary.tsv`. | Baselines; finding order-dependent failures |
| `uv run python -m tools.devtools.test_outcomes BEFORE.xml AFTER.xml --renames-from REF` | Compares outcomes and skip reasons test by test, following git renames | After moving or refactoring tests |

## DSP budget gate (patchkit smoke)

`pk_harness run` ends every cell with a DSP measurement: TRIG 1 held (the default voice) for 1 s of audio after the
usual settling, then the mean (and the maximum of ten 100 ms windows) of the DSP busy instructions per block, from the
`status.audio.dsp_tiers` counters (executed minus idle-skipped, per 1500 blocks per audio second). The stock baseline of
the same device and OS is measured by the same code (cached in `out/runs/patchkit/dsp/stock-DEV-FW.json`, keyed by the
daemon binary). Added instructions per block = cell minus stock. Projected real load = busy x factor / 666,667 (1 GHz /
1500 blocks per second). The factors are real cycles per block over busy instructions per block of the cycle model
(`out/clock/sharc.json`, `tools/sharc_dev/sharc_cpi.py`, finding at the end of `docs/findings/06-sharc-engine-and-startup.md`):
DN2 1.138 nominal / 1.326 high, DT2 1.134 / 1.407, kept once in `devices/cycle-factors.json` (read by `tools/patchkit/pk_dsp_budget.py` and by the live CPU meters, `status.load` in `docs/CONTROL.md`; `uv run python -m tools.patchkit.pk_load dt2|dn2` prints the meters of a held note). The cell fails when the projected
high load exceeds `--dsp-budget` (default 85 %); `--no-dsp-budget` skips the step. The table is printed at the end and
written to `out/runs/patchkit/dsp-budget.json`. The default note exercises the always-on cost of a module; the cost of a
module's own voice needs its own run (`tools/patchkit/pk_tiers.py`). Resolution is about 5 instructions per block.
Unit test: `tests/patchkit/test_pk_dsp_budget.py`.

## Render cache

Scenario renders are cached per scenario under `out/cache/renders/` (`record.json` and `audio.wav`, about 1.6 MB per entry; `tools/devtools/render_cache.py`).
The key hashes the coupled-run binary, the input files (firmware .syx, DSP image, DSP state, CF snapshot, card image, any other existing-file argument), the scenario JSON object (all fields, including `expect_sha256`), the peek specs, the device, the device TOMLs (`devices/*.toml` or `$DIGI_DEVICES_DIR`), and every `SHARC_*`, `DIGI_*` and `DSP_*` environment variable except a denylist of non-semantic ones (job counts, timeouts, log and diagnostic outputs, path selectors whose files are hashed, build-only variables). `@FILE` values are hashed by content.
A hit restores the report entry and the WAV; a miss renders as before and stores the result. Runs with `--diag` bypass the cache.
`--no-cache` (`tools/emulator/coupled.py scenario`) or `DIGI_RENDER_CACHE=0` disables it; `uv run python -m tools.emulator.coupled cache stats|clear` shows or empties it.
Effect: pins, check, check-no-aot and tier-sweep render only scenarios whose inputs changed. A change to DSP or native code changes the binary hash, so everything renders again.

Only emulator output is cached (the report entry and the WAV). Everything that judges a render runs fresh: check's Python oracles analyse the cached WAV, tier compares hashes, and the pins' expected hashes are part of the key.

Two guards protect against a stale hit:

- Fail-closed audit: `tests/devtools/test_render_inputs.py` scans the crates linked into coupled-run. Every environment read must be in the key, in `DENYLIST` or in `IGNORED_ENV` (`tools/devtools/render_cache.py`). Computed-name env reads and runtime file reads are counted per file against `tests/devtools/render_inputs.json`. Adding a render input fails the test until it is classified; after review, regenerate the counts with `uv run python -m tests.devtools.test_render_inputs --scan`.
- Spot checks: `DIGI_RENDER_CACHE_CHECK=p` re-renders each hit with probability p and fails with "stale render cache ..." on any sha256 or peek difference. `verify` sets p=0.03 (about 10 fresh re-renders per full gate).

Both guards were proven by mutation: an unclassified env read fails the audit, and a tampered cache entry fails the spot check.

Use `verify --no-cache` (or `DIGI_RENDER_CACHE=0`) for release and publication gates: it renders everything.

Measured `verify:full`: 1188 s (19.8 min) with a cold cache, 428 s (7.1 min) with a warm cache. Warm: pins 0 s, tier-sweep 3 s, check 12 s, check-no-aot 12 s. The critical path is now python-tests (`test_pluck.py`), then rust-tests (two serialized differential tests in `packages/emulator/sharc/src/tests_gen.rs`).

## Baseline (2026-10-14)

Per-file run (`test_outcomes.py run --jobs 6`) of the non-slow suite at
2f19ba4, in a lane with the local firmware inputs.

| Files | Passed | Failed | Skipped | Timeouts | Wall time |
| --- | --- | --- | --- | --- | --- |
| 157 | 2289 | 5 | 237 | 0 | about 8 min |

- The 5 failures were in `tests/firmware/test_device.py`: `devices/*-leds.toml` has no `[device]` table. Fixed by `emu/device.py` skipping sidecar files.
- After the fixes on this branch (bb912e1), one pytest process over all of `tests/` passes: 2322 passed, 237 skipped, 0 failed, in 18 minutes. The per-file runner takes about 8 minutes with 6 jobs.
- Clean checkout (no firmware, no out/): 2005 passed, 554 skipped, 0 failed after the fixes in this branch; every skip names its missing input. With `mise run sharc:native-lib` (22 s from cold), the 46 native-library skips run: 644 passed and one failure (the shift 0x50 corpus case, since fixed).
- The same run after the test moves gave the same outcome and skip reason for every test.
- Slowest files:

| File | Time |
| --- | --- |
| `tests/patchkit/test_pluck.py` | 453 s |
| `tests/sharc/integration/test_sharc_harness.py` (before the split into test_sharc_harness_{audio,voice,frame,cli}.py) | 210 s |
| `tests/patchkit/test_patchkit.py` | 111 s |
| `tests/coldfire/test_regions.py` | 86 s |
| `tests/patchkit/test_dsp_voice.py` | 73 s |

Skips depend on which firmware, sections, captures and toolchains are present.
A checkout without them skips more.

## Known issues and deferred work

- State shared between test files:
  - One pytest process over all of `tests/` used to fail (`TraceTest`) and then stall in tracer code. Three causes, all fixed:
    1. Ghidra tools removed `tools/` from `sys.path` when imported (so PyGhidra's `ghidra` package was not shadowed), and `tests/conftest.py` restored it. Since the package layout (R1) `tools/` is never on `sys.path`; the strip and the conftest guard are gone.
    2. `tools/emulator/native_emu.py` sets `SHARC_GEN_DIR` with `os.environ.setdefault`. `test_native_emu_launcher.py` now restores it after every test.
    3. `tools/sharc_dev/sharc_lowergen.py` patched `sharc_core` for good on the first trace. It now patches only inside `installed()` and restores on exit.
  - `mise run test` still runs one process per file: it is faster and a new leak between files cannot fail unrelated tests. To check for leaks, run `uv run python -m pytest -q tests` (about 18 minutes).
- Comments in `tools/sharc_dev/sharc_core/*.py` and `packages/emulator/sharc-rt/src/decode.rs` still name old test paths (`tests/test_*.py`). Editing them changes `CORE_FINGERPRINT` or rebuilds the generated core, so they change with the next core regeneration.
- `docs/findings` and `docs/handovers` keep the old test paths (historical records). `git log --follow -- tests/<new path>` shows a file's history across the move (commit "tests: move test files into domain directories").
- Not done yet:
  - The TypeScript runner for regression and module tests.
