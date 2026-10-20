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
| `tests/sharc/integration` | Firmware-backed harness, replay, golden hashes, capture and peer | `tools/sharc_dev/sharc_harness/`, `tools/sharc_dev/sharc_replay.py`, `tools/sharc_dev/sharc_capture.py`, `tools/sharc_dev/sharc_peer.py` |
| `tests/patchkit` | Patchkit, module primitives, DSP voice rig, PLUCK, master drive, proofs runner. `smoke/` is the emulator smoke tour (`matrix.toml`, `tours/`, `goldens.json`). `pk_src/` holds small patch sources. | `tools/patchkit/patchkit.py`, `tools/patchkit/pk_*.py`, `tools/patchkit/pk_harness.py` |
| `tests/modules` | The machine conformance suite (`test_machine_conformance.py`, section "Machine conformance"): every machine module, from its manifest, on the concrete SHARC+ core | `tools/patchkit/pk_conform.py`, `tests/support/machine_rig.py` |
| `tests/coldfire` | ColdFire analysis: cf/cfdb, Ghidra tools, symbols, RTTI, vtables, the ISA oracle | `tools/coldfire/cf.py`, `tools/coldfire/cfdb.py`, Ghidra tools |
| `tests/emulator` | Launcher, PGO, target directories, coupled runner helpers, live audio, wire traces, bootstrap | `tools/emulator/native_emu.py`, `tools/emulator/native_pgo.py`, `tools/emulator/coupled.py` |
| `tests/control` | The control-socket client: fake-socket contract tests and daemon tests (`DIGI_EMUD`) | `tools/control/digi_client.py` |
| `tests/firmware` | `.syx` container, aPLib/ELZ, device profiles, firmware atlas, snapshots, machine patch planning, +Drive format | `dt2/`, `emu/` |
| `tests/dsp` | DSP maps and diffs, FX scenario generators, audio checks | DSP tools |
| `tests/devtools` | Lint (ruff `CLEAN` list), types (mypy `TYPED` list), PyPy, benchlock, verify routing, test_outcomes | `tools/devtools/verify.py`, `tools/devtools/benchlock.py`, `tools/devtools/test_outcomes.py` |
| `tests/digikit` | The digikit runtime's boot-time scenarios: data files (`scenarios/*.toml`) and the runner `tools/patchkit/pk_digikit_scen.py`; routed by the verify suite `digikit-boot`. Slow (coupled cold boots). | `modules/core/runtime`, `core/dynreg`, `core/dsp-link`, `core/dyndsp`, `core/digikit-menu`, `tools/patchkit/pk_dkrt_boot.py` |
| `tests/scenarios` | Emulator scenario oracles. Slow; run by the verify steps `check` and `dt2-check` and by `mise run scenario:*`. | `tests/scenarios/`, `tools/emulator/coupled.py` |
| `tests/support` | Helpers shared by tests. No `test_` prefix, not collected. `tests/support/paths.py` gives `REPO_ROOT`, `TESTS`, `TOOLS`. | |
| `tests/golden`, `tests/fixtures` | Reference hashes and small fixtures | |

Other tests:

- `apps/web/tests` holds the web app's node tests (`pnpm test`).
- Rust tests live in their crates (`cargo nextest run`, run through `tools/devtools/rust_ws.py`).

## Writing a test

- Put the test in the directory of the code it tests.
- Import repo paths from `support.paths`. Never count parent directories.
- Import tools as `from tools.<owner> import <module>`. pytest puts the repository root and `tests/` on `sys.path` (`pythonpath` in pyproject.toml); never change `sys.path` in a test. A test that runs a tool in a subprocess uses `[sys.executable, "-m", "tools.<owner>.<module>", ...]` with `env=python_env()` (`from tools import python_env`).
- Share helpers through `tests/support`. Never import another test module.
- Test file basenames must be unique across `tests/`. pytest uses its default import mode and there is no `__init__.py`.
- Mark long firmware runs `@pytest.mark.slow`. They are skipped unless `--slow` is given.
- A test that needs firmware or generated artifacts skips with a reason when they are absent.
- A new Python file joins the `CLEAN` list in `tests/devtools/test_lint.py`.
- `tests/devtools/test_pathcheck_repo.py` (in the verify gate) runs `uv run python -m tools.devtools.pathcheck`: every path-like token in tracked text files must name a tracked file or directory, a generated or git-ignored path, or an allowed token. A deleted file is cited as `REF:path`, checked with `git cat-file` (`unicorn-reference-final:emu/edma.py`, `<commit>^:path`). Files under `history_docs` (findings, plans, design, archive, refs, contracts) may name paths that were once tracked. Add an exception to `allow` in `tools/devtools/pathcheck.json` with a required reason; an allow that matches nothing fails the run. `--fix` and `--fix-deleted REF` rewrite stale tokens (dry run unless `--apply`).

## Inputs tests need

In a clean checkout (no firmware, no `out/`), 2005 tests pass and 554 skip. Each skip names the input it lacks.
`pytest -rs` lists the reasons.

| Input | Skip message looks like | How to provide | Tests |
| --- | --- | --- | --- |
| Stock firmware images in `firmware/` (DT2 1.17, DN2 1.12; older ones for analysis only) | "needs Digitakt_II_OS1.17.syx in firmware/", "stock firmware images not present" | README "Firmware" (files come from Elektron's support site; `DIGI_EMU_SYX` and `DIGI_EMU_SYX_DT2` override paths) | `tests/firmware`, `tests/patchkit`, `tests/emulator/test_bootstrap.py` |
| Extracted sections `out/sections/<image>/` | "out/sections/dt2-1.17/section_7_BLOB.bin is not available", "no extracted sections", "DT2 1.17 image absent" | `uv run python -m tools.firmware.extract firmware/Digitakt_II_OS1.17.syx -o out/sections/dt2-1.17` (likewise `dn2-1.12`; README "Extract the firmware") | `tests/sharc/*`, `tests/coldfire`, `tests/dsp` |
| SHARC program database `out/sharcdb/<image>.sqlite` | "out/sharcdb/dt2-1.17.sqlite is not available", "no program database" | `uv run python -m tools.sharc_dev.sharcdb build out/sections/*/section_7_BLOB.bin` (needs the sections) | `tests/sharc/analysis` |
| ColdFire Ghidra dump `out/ghidra/<image>-emac` and its cfdb | "out/ghidra/dt2-1.16-emac missing", "no cfdb for dt2-1.17" | Needs Ghidra: a `tools/coldfire/ghidradump.py` dump, then `uv run python -m tools.coldfire.cfdb build out/ghidra/<image>-emac` (AGENTS.md "Static analysis") | `tests/coldfire`, `tests/firmware/test_fw.py` |
| Native SHARC library (`DEFAULT_LIB` in `tools/sharc_dev/sharc_transpile_run.py`; `SHARC_NATIVE_LIB` overrides) | "no native library built", "native core not built (packages/emulator/sharc)" | `mise run gen:native-lib` (firmware-free; builds the shared core first; output in `out/native/shared-core/target`). The shared-core library is preferred when present; else the image-specific library at `out/native/opt/target-final` (perf/AOT work; may be stale, and an old one that lacks symbols makes the tests skip); `SHARC_NATIVE_LIB` overrides. | `tests/sharc/semantics`, `tests/sharc/generation` |
| Generated shared core `out/native/shared-core/gen` | "no shared core generated here" | `mise run gen:core` | `tests/sharc/generation/test_sharc_lowergen.py` |
| Module toolchains in `out/tools` (m68k gcc/binutils, Selache) | "selache not built (tools/patchkit/pk_toolchains.py selache)", "m68k toolchain not installed" | `mise run patch:toolchains` (about 15 min; `uv run python -m tools.patchkit.pk_toolchains status` shows what is present; docs/PATCHING.md) | `tests/patchkit` |
| ColdFire libgcc in `out/tools/m68k-elf/lib/libgcc-cf54418.a` | "pk_toolchains libgcc-cf not run" | `uv run python -m tools.patchkit.pk_toolchains libgcc-cf` (under a minute; also run by `pk_toolchains m68k`); `tests/patchkit/test_patchkit_cmodules.py` (ISA checker over every committed ColdFire object, the stock libgcc refused, `test/c-demo` 64-bit results through the runtime on dt2-1.17 and dn2-1.12) | `tests/patchkit/test_patchkit_cmodules.py` |
| `digi-emud` daemon | "needs DIGI_EMUD (a built digi-emud) and the DN2 .syx" | Build it with `mise run emu:serve -- --build-only` (per `tools/control/digi_client.py`), set `DIGI_EMUD` to the binary (`DIGI_EMUD_DN2` / `DIGI_EMUD_DT2` for coupled runs; `tools/emulator/native_emu.py --daemon --build-only <syx>` prints the path), and have the `.syx` (`DIGI_EMU_SYX`) | `tests/control`, `tests/patchkit/test_master_drive_emu.py`, `tests/emulator/test_coupled_patched.py` |
| Ready inputs, `out/snapshots/*.snap` | "no out/snapshots/*.snap present", patched ready inputs missing | `mise run emu:ready -- dt2` or `-- dn2` (about 2 min; needs the `.syx`; README) | `tests/firmware/test_snapread.py`, `tests/emulator/test_coupled_patched.py` |
| Captures `out/captures/*.dt2cap` | "the fulltx play capture is not available", "no drive3 capture" | Not reproducible from the repository alone; these tests stay skipped for most contributors. | `tests/sharc/integration`, `tests/sharc/generation` |
| Opt-in switches | "set LIVE_AUDIO_DEVICE_TESTS=1 to open a real output device", "packages/emulator/live not built: cd packages/emulator/live && cargo build --release" | `LIVE_AUDIO_DEVICE_TESTS=1` opens a real audio device; `cd packages/emulator/live && cargo build --release` builds the live library; `--slow` runs the long firmware runs. A `SELACHE_DIR` oracle switch is not confirmed (see `tools/patchkit/pk_toolchains.py`, which defines `SELACHE_DIR` as a path, not an environment variable). | `tests/emulator/test_live_audio.py`, tests marked `slow` |

## Commands

| Command | What runs | When |
| --- | --- | --- |
| `mise run test:quick` | Launcher, PGO, target and coupled-tool tests in `tests/emulator`, then `tools/emulator/coupled.py test-quick` | Fast check of the tooling, audio sink and coupled runtime |
| `mise run test` | `uv run python -m tools.devtools.test_outcomes run --check --out out/runs/tests/latest tests` (each test file in its own process, 4 in parallel by default, exits 1 when a file fails), then `tools/devtools/rust_ws.py test`, `pnpm test`, every DN2 scenario pin | Everything except the slow gates |
| `mise run test:slow` | The same runner with `--pytest-arg=--slow` and a one-hour per-file timeout, then `tools/devtools/rust_ws.py gates` | Slow runs and the ignored Rust gates that need private fixtures |
| `mise run lint` | `lint:py` (ruff check and format check on the `CLEAN` list), `lint:web` (oxlint on the JS/TS workspaces), `fmt:check` (Prettier check; `mise run fmt` formats in place), `lint:rust` (cargo fmt check) | Before a commit |
| `mise run lint:py` | `tests/devtools/test_lint.py` only | Python files changed |
| `mise run verify:pre` | Quick lane checks (`tools/devtools/verify_pre.py`, 1 to 2 min): ruff on the changed Python files, mypy on the changed `TYPED` files, pathcheck, `pk_build build-all --check` when modules, patchkit tools or ports changed, the changed test files, the Rust tests of the affected crates (`rust_ws.affected_packages`). No emulator, pin or scenario; it prints that it is not the merge gate. | In a lane, while iterating and before handing the branch over |
| `mise run verify` | The merge gate: the steps the changed files need (`tools/devtools/verify.py`, routing below). `--explain [--head REV]` shows the routing and the plan without running (`--base A --head B` explains a range from the current tree). `--rerun-failed` re-runs only the steps that failed last time. `--jobs N` sets the CPU budget, `--foreground` normal priority, `--all-no-aot` widens the coverage (see "CPU budget" and "AOT versus interpreter equality"). | The coordinator runs it once per merge to `main` (`-- --base main` for a lane branch); a lane checks the routing with `--explain` |
| `mise run verify:full` | Every step (about 7 min with a warm render cache, about 20 min when DSP/native code changed). The AOT = no-AOT sweep runs the fixed subset; `-- --all-no-aot` runs all 134 scenarios and all 61 oracle tests. | Only after changes to the generated core |
| `mise run scenario:all` | Every DN2 scenario; fails when a pinned PCM hash differs | PCM pin check (renders are cached, see Render cache) |
| `mise run scenario:check` | `pytest tests/scenarios --slow`: DN2 FX oracles, fast subset (about 3 min) | After DSP or scenario changes |
| `mise run dev:plusdrive-check` | The firmware's own view (mount, readdir, lookup, read) of an image built from a folder: nested folders, plain files, a multi-page directory, multi-extent files (about 1 min; `verify` runs it in the `card-fs` step when `packages/emulator/plusdrive/` changes) | After +Drive builder changes |
| `mise run scenario:check:dt2` | DT2 FX oracles on the one-sample card (about 2 min) | After DT2 changes |
| `mise run scenario:sweep` | Oracles with the full sweeps (about 20 min) | Before a merge |
| `mise run patch:test` | Smoke tour and module tests over `tests/patchkit/smoke/matrix.toml` | After patchkit or module changes |
| `mise run patch:proofs` | Every emulator proof on its quick tier, in sequence | After patchkit or module changes |
| `uv run python -m tools.devtools.test_outcomes run [--check] --out DIR [--root PATH] [--jobs N] tests` | Each test file in its own process with a per-file timeout. Writes `DIR/all.xml` and `summary.tsv`. | Baselines; finding order-dependent failures |
| `uv run python -m tools.devtools.test_outcomes BEFORE.xml AFTER.xml --renames-from REF` | Compares outcomes and skip reasons test by test, following git renames | After moving or refactoring tests |

## Digikit boot scenarios (`tests/digikit`)

The runtime loads its module set at boot, so what must hold about a module set is tested as scenarios: a TOML file names the device,
the module sets and the expectations; one runner (`uv run python -m tools.patchkit.pk_digikit_scen list|run [NAME ...] [--image IMG] [--tier quick|full]`)
boots the patched firmware in the coupled emulator through `pk_dkrt_boot` (`Env`, `BootRig`) and checks. Kinds:

| Kind | Checks |
| --- | --- |
| `equiv` | the machine, parameter and page rows (decoded: names, ranges, defaults, slots, knobs) and the voice rows of a boot with set S equal those of the image patchkit builds with S's static twin (`modules/test/twin-*`, `machines/pluck`, `test/dn2-sine`); the PCM of a note equal where the set has a sound. A set with no twin leaves every table as a boot with no module. |
| `roundtrip` | a project with run-time machines on tracks, parameter values, a p-lock and a preset lock is saved under set A, loaded and saved under set B (a machine absent) and under A again: the MmcFs slot is byte-identical to the first save but for one header stamp word; the sound records keep the machine types. Controls: the panel script changed the project; the stamp changes once. |
| `upload` | module upload over USB SysEx (`[upload] part`): `ops` (every command and error code; the card read back by plusdrive equals LIST and the bytes sent; the +Drive round trip), `cut` (a power cut after the Nth card write: during staging, after the table, between header B and A, after A), `stock` (the stock MidiRpc Ping, SoftwareVersion and DeviceUID byte for byte the same with and without the runtime), `playback` (PCM of a note with an upload during it equals the note without; the writes the upload added are store sectors), `rust` (the Rust host `Session` of packages/dkupload, via its `emu_session` example over the control socket, against the device side: INFO, LIST, put of a built module and of a three-chunk file with two DATA in flight, replace, delete, E_HASH, a lost INFO, DATA and END reply, CLEAR; each step compared with the Python tool's LIST and the card read by plusdrive), `screenshot` (the stock MidiRpc Screenshot 0x04 read through `tools/device/dkscreen.py` equals the emulator's frame capture, pixel for pixel), `quick` (one upload, a replace, a delete: the gate). `menu-upload-pluck-demo` is the `menu` kind with the store filled by the upload. |
| `menu` | a set ticked in SETTINGS > DIGIKIT > MODULES on a boot with no selection, APPLY & RESTART (two presses): the next boot loaded the whole set (the record, the results), the machine is in the list with the patched-in set's rows, a note plays PCM equal to the patched-in set. Full tier (about 2.5 minutes per image). `menu-dkrun-pluck` (`[menu] dkrun = true`) replaces the menu with `tools/device/dkrun.py` (runtime 4 SELECT and RESTART over USB SysEx) and checks the next boot's MOD_LOAD (result 1) and MOD_DSP (result 0), the record and the pluck PCM. |
| `safe` | a module whose `init` resets the machine: the third boot is safe mode (reason counter), nothing loaded, selection kept; the safe-once flag. |
| `cut` | the card states a power cut leaves between the two copies of the selection record, and one copy written by the runtime itself. |

Every scenario ends with the write-watch over `cf.expansion` (0 stray writes over every emulator it ran), the flush check (0 hits), and the card
sectors the guest wrote (in the store range only the two selection sectors).

### What verify runs for a change

Routing is in `tools/devtools/verify.py` (path prefixes, `PY_SUITES`) and `tools/devtools/verify_route.py` (finer rules). Check it with
`mise run verify -- --explain` instead of running the gate.

| Changed | Steps | Python test files |
| --- | --- | --- |
| `mise.toml` | A changed task routes like the tool its `run` names (`-m tools.x.y`, a script, a pytest path, `pnpm`). Broad (every step): a change outside `[tasks]`, a `verify`, `build`, `gen` or `setup` task, a task key other than run/depends/description/alias, a `run` that names no repo file (`dev:plusdrive`). | those of the tool |
| a module of `tools/device` | `python-tests` | the tests that import it (directly or through another `tools/device` module) and the digikit tests of the scenario kinds whose runners call it (`DEVICE_KINDS`) |
| a file of a module under `modules/` | `python-tests`, `rust-tests` (affected crates), `web` only for what the gallery bundle reads (not `src/`, `sdk/`, `tests/`, `*.md`). Never a PCM step. | Framework modules (`FRAMEWORK`: runtime, dsp-voice, dsp-link, machines, params, dynreg, dyndsp, digikit-menu, health, expansion): all of `tests/patchkit` and `tests/digikit`. Other modules: the test files that name the module or a module that requires it or builds from its sources, and the digikit test files of the scenarios that name it (TOML or runner code) |
| a scenario file of `tests/digikit/scenarios` | `python-tests` | the test files that run its kind, and `test_scenarios.py` |
| `tests/digikit/test_*.py`, `tests/patchkit/test_*.py` | `python-tests` | the file itself |
| `packages/dkupload/` | `rust-tests`, `web` | |
| `tools/devtools/verify.py`, `verify_route.py` | every step | every suite |
| generated-core inputs, emulator crates, `sharc_dev` | as before (broad) | as before |

`PY_GATE` runs in every case (it includes `pk_build build-all --check`). Not run by any pytest file: the scenario kinds `rungc` and `pages`, and the module smoke
cells (`uv run python -m tools.patchkit.pk_harness run`, run by the coordinator). A new `tools/device` module must be listed in `DEVICE_KINDS`
(a test fails until it is).

Tiers: `DIGIKIT_TIER=quick` (default; the scenarios marked `tier = "quick"`, the newest image of each device) is what `mise run verify` runs when
a framework module (see the routing table), a scenario, a module a scenario names, the `.dkm` packer, `plusdrive`, the boot tools or `tests/digikit` changed (the
suite `digikit-boot` in `tools/devtools/verify.py` and `verify_route.py`; one pytest file per group, so they run in parallel); `verify --full` sets `DIGIKIT_TIER=full`: every
scenario, on the newest image of each device (DT2 1.17, DN2 1.12). The quick tier is the pluck equivalence (both devices), the DN2 set and the DT2 round trip (under a minute in parallel), and since sticky machine ids `sticky-types` (the type numbers, the map, eviction) and the MODULES part of `debug-log`; `debug-guard` (full tier) runs on both images;
safe mode and the power cut run in the full tier. Cards are cached by content in `out/cache/digikit/cards/` (read-only for the guest; its writes live in an overlay),
so a repeated scenario reuses the daemon's boot cache; most of the time of a scenario is emulated panel time (a project save and load take about 10 s each), not the boots.
A scenario file is `tests/digikit/scenarios/NAME.toml`: `[scenario] name, kind, devices, tier`, and a table named like the kind (the runner's docstring lists the keys).
Speed (finding 39, "Time: where it went"): the coupled daemon runs the panel at about a quarter of real time, the ColdFire alone at real time and faster, so the panel script of
the `roundtrip` kind runs on ColdFire-only daemons (`engine = "cf"`, the default; `engine = "coupled"` is `roundtrip-dt2-coupled`, full tier) while a coupled boot of the same card checks the
DSP part of the modules. Independent boots of a scenario run in threads; the cases of a test file run side by side (`DIGIKIT_JOBS`, default 3 outside `verify`, which sets it from the python-tests share; `pk_digikit_scen run --jobs N`).
The `equiv` keys `trig_frame` (the frame the note starts at; the rigs must be before it) set it close to the end of the setup. Timing: `DIGIKIT_PROFILE=1` prints the phase table of a run,
`DIGIKIT_RUN_SALT=x` forces cold boots, `DIGIKIT_SETTLE_MS` changes the 300 ms the rigs run after their boot. Measured on a quiet machine: the quick tier (three scenarios at once) 38 s with warm boot caches and 44 s cold;
the full tier (five test files at once) 250 s.

## Machine conformance (`tests/modules`)

One contract for every machine module, tested the way a plugin validator tests any plugin: the suite reads the module's manifest and runs
its DSP routine on the concrete SHARC+ core, through the wrapper `core/dsp-voice`, block by block as the engine calls it. A module's own
tests (what its sound should be) stay with the module; this suite only asks what any machine must satisfy.

```
mise run module:check NAME [--device dt2|dn2]        # NAME: machines/pluck or pluck
DIGI_CONFORM_MODULES=machines/pluck uv run python -m pytest tests/modules/test_machine_conformance.py -rf     # the suite alone
```

`module:check` runs the suite for the module's devices, then the module's own tests: `modules/<group>/<name>/tests/test_*.py` and, until
the tests move there, the files under `tests/` named after the module (`tests/patchkit/test_pluck*.py`). A module is a machine when it
has a DSP object for the device and a `[[voice]]` or a voices `[[entry]]` (`tools/patchkit/pk_conform.py`); `core/*` never is.

What a machine must satisfy, in every block of every case:

| Check | Rule |
| --- | --- |
| returns | the call comes back to the caller within the rig's step limit (400,000 instructions) |
| context | M5, M6, M7, M13, M14, M15, L0 to L5 and MODE1 are as they went in (the rig draws a hostile context per call) |
| finite | all 32 output samples are finite; the output block is NaN before the call, so a sample the routine did not write fails too |
| bound | `abs(sample)` is at most 8.0 (18 dB over full scale: a runaway state, not a loud sound) |
| budget | at most `headroom / 16` instructions in a block, and at most `headroom` in the block of a note-on (DN2: the edge block and the one after it) |

The budget rule (`pk_conform.budget`): the block is `dsp_clock_hz / dsp_blocks_per_second` cycles (`devices/*.toml`, `[board]`); the stock
firmware with one held note busy instructions per block (Digitakt II 208,640, Digitone II 334,529) project to cycles with the "high" factor of
`devices/cycle-factors.json`; the module may bring the load to the DSP budget gate's 85 % (`pk_dsp_budget.DEFAULT_BUDGET_PCT`). The instructions
left are the headroom (Digitakt II 194,244; Digitone II 92,796); 16 voices share it, so one steady voice-block gets 12,140 (Digitakt II) or 5,799
(Digitone II), and a note-on may take the whole headroom once. A manifest can declare `[conformance] voice_block = N`, `note_on_block = M` to replace
either number (the module schema does not know the table yet). The measured worst case of each machine is printed by
`test_worst_case_instructions_are_reported` (`-rA`) and written to `out/runs/conformance/<module>@<device>.json` with every case's timing.

Cases (`pk_conform.cases`, generated from the parameters in the manifest: `[[param]]` min, max, default and the stock TUNE row 0xca of a
Digitakt II knob list). Parameter kinds do not exist yet, so every parameter is treated as possibly pitch-like.

| Group | Cases |
| --- | --- |
| `limits` | each parameter at min, max and mid with the others at default, and all at default; notes 0, 24, 48, 60, 72, 96, 127; a note-on block and 3 more |
| `corners` | all parameters together at every combination of min and max (2^n); notes 0, 60, 127 |
| `modulation` | 500 blocks, every parameter moved each block: a seeded random walk over its full range (notes 24, 100) and a square wave between its limits (note 60) |
| `long` | one note of 2000 blocks at the defaults |
| `retrigger` | a trigger every 7 blocks for 280 blocks, at velocity 1 and at velocity 127 |

A failure message names module, device, case, block, the parameter words, note and velocity, and the first bad sample, for example
`machines/pluck@dt2 case limits/tune=min/note24 block 0: note 24 velocity 100 words [tune=0x0 pluck_decay=0x4000 ...]: output sample 0 of 32 is inf`.
A case stops at its first failure. Modules built from the same sources and parameters (`test/dyn-pluck` and `machines/pluck`) run once.
`test/dyn-dsp-hang` (a routine that never returns) is a negative fixture: `test_the_suite_rejects_a_machine_that_hangs` expects it to fail.

How it runs: the rig is `tests/support/machine_rig.py` (`MachineRig(module, device)`: builds the wrapper and the module's DSP sources, places the
`.PKDATA`, `.PKBSS` and `.VAR` symbols in free L2, sets parameter words by manifest id, `set_note`, `trigger`, `block()`; the core keeps its memory between
blocks, `pk_sharc.CallSession`). One worker process per machine and device (`tests/support/conform_worker.py`) runs all cases, under PyPy 3.11 when
`uv` can start it (3 to 4 times faster than CPython on the concrete core), else under the test's Python; workers of different machines run at once
(`DIGI_CONFORM_JOBS`, default cpus / 4). Measured on a loaded machine: all 8 machine/device cells in 24 s wall, 50 s CPU; one cell takes 3 to 20 s of CPU.
The verify route: a change in a machine module, a module it depends on or builds from, `core/dsp-voice`, or in the rig and `pk_conform.py` selects the suite
(`tools/devtools/verify_route.py`, suite `machine-conformance` in `verify.py`).

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

## CPU budget of the gate

`verify` has one budget of cores for everything it runs (`tools/devtools/verify_budget.py`). Before it, every step had its own pool (tier sweep cpus - 4 coupled-run
processes, python-tests one process per file with 3 digikit daemons each, cargo and libtest on all cores) and up to 8 steps ran at once, so the pools multiplied: a
`verify:full` after a core regeneration ran at load average 141 on a 16-core M3 Max (2026-10-19: build 1131 s instead of 21 s, rust-tests 3963 s instead of 190 to 790 s,
check-no-aot 1333 s).

- Budget: min(physical cores - 2, memory limit), at least 1, or `verify --jobs N`, or `DIGI_VERIFY_BUDGET`. Physical cores: macOS `sysctl hw.physicalcpu` (14 on a 16-core machine with 12 performance and 4 efficiency cores);
  Linux unique (package, core) pairs of `/sys/devices/system/cpu/cpu*/topology` (else `lscpu -p`) within `sched_getaffinity`, capped by a cgroup CPU quota (v2 `cpu.max`, v1 `cfs_quota_us`); Windows and any fallback: logical CPUs / 2 (they count SMT threads).
  Memory limit: (RAM - `RESERVE_GB` 4) at `GB_PER_EMULATOR` 1 GB per emulator process (2 cores), RAM = macOS `hw.memsize`, Linux `MemTotal` or a lower cgroup `memory.max`, Windows `GlobalMemoryStatusEx`; unknown RAM = no limit.
  Cargo jobs of every step are also capped at (RAM - 4 GB) / `GB_PER_CARGO_JOB` 6 GB (a rustc job of the generated core, measured 5.9 GB; a digi-emud 0.5 to 1.0 GB), at least 1. Examples: 16 cores / 128 GB = 14 (cores), cargo jobs 20; 8 cores / 16 GB = 6 (cores), cargo jobs 2; 4 cores = 2; 2 cores = 1; 32 cores / 16 GB = 24 (memory: 16 GB); 8 cores / 8 GB, cargo jobs 1. The start of a run and `--explain` print `budget 14 (cores: 16 physical)` or `budget 24 (memory: 16 GB)`.
- Admission: each step asks for a fraction of the budget (`WANT`: builds 45 %, rust-tests 30 %, python-tests: one job per file of 2 cores (below), scenario pools 50 %, tier steps 75 %, desktop, web, card 15 %). It starts when at least half of its ask is free (alone it always starts) and gets what is free, at most the ask.
  At most 6 steps run at once. The shares of the running steps never add up to more than the budget (`tests/devtools/test_verify_budget.py` checks it for budgets from 1 to 64).
- The share sets every pool of the step through the environment: `CARGO_BUILD_JOBS`, `RUST_TEST_THREADS`, `RAYON_NUM_THREADS` (ruff, oxlint), `DIGI_JOBS` and `DIGI_TIER_JOBS` (coupled-run processes), `DIGI_PY_JOBS` (test files), `DIGIKIT_JOBS`, `DIGI_CONFORM_JOBS` and `DIGI_PK_JOBS` (daemons inside a file), `DIGIKIT_PART_JOBS=1` (the 2 to 4 parts of one digikit scenario, each its own daemon, run one at a time; without it a file with `DIGIKIT_JOBS=1` ran 3 daemons at once, measured on `card-safety`), `--test-concurrency` of the node tests.
  One coupled emulator process keeps two threads busy (the ColdFire loop and the `sharc-dsp` worker; no variable changes that), so a pool of processes has share // 2 workers (`EMULATOR_CORES`). python-tests is not one share: every test file is a job of its own in the same queue (2 cores, started only on all of them, not counted in the 6 running steps), so the cores a finished step frees go to the next file. The jobs run longest first by the seconds of the newest earlier runs (`out/runs/verify/*/python-tests/summary.tsv`; `tools/devtools/verify_times.py`; a file never timed counts 60 s); files of 100 s or more are queued before the steps, the rest after them, and a step that cannot start for lack of cores holds the later file jobs back. The `python-tests` node itself holds no cores: it judges the files when they all ended. A pool variable you set yourself replaces the share (verify prints which).
- `--explain` and the start of a run print the worker plan. Its waves are an estimate; a run starts a node as soon as the cores are free.
- Priority: verify, `verify:pre` and every child run at nice 10 (`os.setpriority`; `--foreground` leaves the priority alone). Not `taskpolicy -b`: it pins a process to the efficiency cores.
- Builds: `build` (coupled-run, the prod core in `out/native/target/prod`), then `build-daemons` (`digi-emud` of both devices: the PGO daemon of `pgo-emud-<device>` when a profile exists, else the plain one; a device with a pinned PGO daemon never uses the plain build, so it is not built; `tools/devtools/verify_build.py`), then `build-dt2` (coupled-run, the DT2 core in `target/g-<hash>`) run one after the other, never beside each other. Steps wait for the build of their binary (the digikit and smoke test files and card for `build-daemons` only, the other test files for every build, rust-tests for `build`), run with `--no-build`, and python-tests runs the PGO daemon copies the build made (`DIGI_EMUD_<DEV>`, immutable copies by `tools/devtools/stablebin.py`), so no test file asks cargo. The target dirs differ for a reason (generated core, PGO flags, `cargo check`), none is a duplicate; the launcher's `pgo-use` is not built here.
- `rust-tests` runs `cargo nextest run --workspace --release` (each test its own process; JUnit at `<target-dir>/nextest/default/junit.xml`; the `gates` mode stays on `cargo test`) without `digiemu-desktop` (the app crate links the generated core into its own test binary and needs the console build); the `desktop` step builds the console and runs `cargo test` of the app crate in `out/native/target/check-desktop` (dev profile; 36 s from scratch with 6 jobs), and a routed run whose Rust crates include the app gets the `desktop` step (`with_desktop`).
- Not measured yet: the shares come from the old pools and the timings above. Compare one quiet-machine `verify:full` with the previous baseline before changing `WANT` or `EMULATOR_CORES`.

### AOT versus interpreter equality (a fixed subset)

Em approved the reduction on 2026-10-19. `check-no-aot` and the no-AOT halves of `tier-quick` / `tier-sweep` (DN2, and the interpreter-only DT2 halves) run `NO_AOT_SUBSET` / `CHECK_NO_AOT_K` of `tools/devtools/verify.py`, in routed `verify` and in `verify:full`:
17 of the 134 DN2 FX scenarios (and 8 of 60 DT2 ones) for the PCM comparison, 17 of the 61 oracle tests for `check-no-aot`. The pick has one or two scenarios per DSP path, at the strongest setting: the voice alone (`tf-dry-hold`), bit reduction, overdrive (with the filter and routing-1 variant), SRR, track and send delay (feedback above unity, pingpong width), reverb (decay, shimmer), chorus (speed) and the master compressor (`sf-mst-ratio`, the mixer's last stage).
`verify:full --all-no-aot` runs the old sets (tier-sweep: all 134; tier-quick: every seventh; check-no-aot: all 61). Since tier-sweep compares the same scenarios as tier-quick now, the default `--full` sweep and the routed quick tier cover the same set.

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

- The 5 failures were in `tests/firmware/test_device.py`: `devices/*-leds.toml` has no `[device]` table. Fixed by `tools/firmware/device.py` skipping sidecar files.
- After the fixes on this branch (bb912e1), one pytest process over all of `tests/` passes: 2322 passed, 237 skipped, 0 failed, in 18 minutes. The per-file runner takes about 8 minutes with 6 jobs.
- Clean checkout (no firmware, no out/): 2005 passed, 554 skipped, 0 failed after the fixes in this branch; every skip names its missing input. With `mise run gen:native-lib` (22 s from cold), the 46 native-library skips run: 644 passed and one failure (the shift 0x50 corpus case, since fixed).
- The same run after the test moves gave the same outcome and skip reason for every test.
- Slowest files:

| File | Time |
| --- | --- |
| `tests/patchkit/test_pluck.py` | 453 s |
| `tests/sharc/integration/test_sharc_harness_*.py` (before the split into test_sharc_harness_{audio,voice,frame,cli}.py) | 210 s |
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
