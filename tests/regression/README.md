# @digi/regression

Vitest suite that drives the emulator daemon (`emud`) through `@digi/test-support` and `@digi/control-client`.

## What it checks

- No guest fault, DSP halt or stall (`Session.expectNoFault`).
- No stock-firmware write into the patchkit free regions: each test arms `guard.set` with the `cpu = "cf"`
  `[[region]]` blocks of `devices/ports/<device>-<fw>.toml` (`cf.flash_cave`, `cf.expansion`, minus
  `excludes`), with an empty `allow_pc`, and asserts `guard.report` has total 0.
- Frame (SHA-256) and LED goldens in `goldens/dt2-1.17.json`: `dt2/rec-play` (scenario `dt2-rec-play` of
  `tests/scenarios/dt2.json`, exact instruction counts, 6 s; reference is `coupled-run --dsp zero --trace` on the
  same scenario), `dt2/pages` (SRC, FLTR, AMP, FX, MOD), `dt2/transport` (PLAY, STOP).

Each test file starts one daemon (`withSession` in `support.ts`) and restarts it before each test.

## Run

```
DIGI_EMUD=/path/to/emud mise run test:stock
DIGI_EMUD=/path/to/emud mise run test:stock -- dt2/pages  # native Vitest filter
```

The stock task uses the existing package test command (`pnpm test:regression` at the root remains available). It never builds: supply a current `DIGI_EMUD` binary, stock DT2 1.17 firmware and the complete ready-input set validated by `resolveInputs` (CF snapshot, DSP image and DSP state, even though the run uses zero DSP). Missing inputs fail. Host, stock FX and patched-runtime acceptance have separate `test:host`, `test:fx` and `test:runtime` tasks.

The routed gate's `stock` step selects affected native filenames for stock cases/configuration/goldens,
and all stock files for shared session/control/emulator changes. It reuses a selected `build-daemons`
DT2 binary as `DIGI_EMUD` and bounds zero-DSP workers to one core each; `--only stock` requires an
existing binary and adds no daemon build. Existing host and smoke coverage remains.

Env: `REGRESSION_JOBS` (parallel files, default 4), `REGRESSION_JUNIT=1` (writes `out/regression/junit.xml`).

## Update goldens

```
REGRESSION_JOBS=1 DIGI_EMUD=... pnpm --filter @digi/regression test:update
```

Use one job: the files all write `goldens/dt2-1.17.json`. Review the diff before committing; a golden changes
only with a reason.

## Zero-DSP mode

The tests run with `--dsp zero` (no SHARC): the UI, sequencer and ColdFire side run, the DSP replies are stubbed.
There is no audio, so nothing here checks sound, DSP faults or audio-dependent UI. See
`docs/findings/07-emulator.md`, "Zero DSP replies for UI scenarios".

## Patched runtime fixture

`pnpm test:host` at the repository root runs five named projects from `vitest.host.config.ts`:
`control-client`, `test-support`, `dk-session`, `regression-host` and `desktop-host`. The package-local
`pnpm --filter @digi/regression test:host` selects only `regression-host`, covering mocked startup,
cleanup and USB wiring without firmware, native builds or daemons. Package-local `test` scripts for
the other three packages and desktop `test:host` likewise select their own project. The root config uses explicit host-only
includes and defaults to one worker; `mise run test:host -- --maxWorkers N` (or
`pnpm test:host --maxWorkers N`) overrides it. Optional JUnit output uses Vitest's
native CLI, for example `pnpm test:host --reporter=default --reporter=junit --outputFile=out/host-junit.xml`.
The `host` step delegates to the native mise sequence with that one-worker default while retaining
its outer CPU allocation. The sequence runs all five host projects once, global lint/format
and control-client, shared-session, test-support, regression and desktop-host test type checks. Explicit `--only host`
requires no native/WASM build or device inputs. `web` retains browser tests, Astro and patchkit WASM,
and depends on `host`, including with `--only web`. Normal host-path routing skips smoke only for the eight audited test files and desktop UI test directory described in
[TESTING.md](../../docs/TESTING.md), while retaining `host`. Non-UI helpers, sources, manifests, configuration, new neighboring tests outside the exact
exemptions, and unknown paths still select conservative smoke, including mixed changes. Desktop
UI tests select only `host` and retain the existing UI-only smoke exemption; production desktop
helpers retain application checks.
Consumer typecheck/check scripts and runtime imports resolve `@digi/dk-session` TypeScript source directly. The normal `test` command retains the stock DT2 suite; runtime acceptance has a separate configuration.

The `runtime-fixture` gate step runs the remote, service-command and transfer acceptance cases sequentially,
with one fresh coupled daemon per file/device. It requests `firmware.load {runtime:true, start:"auto"}` and the existing
`emulator` patch set/cache, then selects free mode. It checks reported patch identity, ready/free status
and health, then installs digikit/remote and checks ABOUT text and the encoder-driven OLED change.
Host tests cover the `DkSession`/`EmuUsbTransport` wiring and pending-request cancellation. Stock restart and instruction-step waits
are rejected for runtime sessions. Cleanup cancels protocol requests and closes the owned daemon; an
unresponsive quit has a ten-second child termination deadline. Runtime startup expires after 120 seconds,
actively aborting socket/handshake setup and awaiting owned-daemon cleanup before the 150-second hook
limit; concurrent close calls share the same cleanup promise.

Before execution, review binary freshness against the native sources/generated inputs (or explicitly
record a compatible baseline), and supply each device's existing daemon binary. Missing binaries or
ready inputs fail; this fixture never builds them. Runtime input validation checks the stock SHA-256
and `intc_force:false` in the ready directory's `SOURCE.json`; its legacy image label is not provenance.
Use the existing ready inputs selected by `resolveInputs` and the generated DT2 samples used by
`tools.emulator.card_check.make_samples` (normally `out/runs/patchkit/samples`).

```sh
DIGI_EMUD_DT2=/path/to/dt2-emud DIGI_EMUD_DN2=/path/to/dn2-emud \
  DIGI_RUNTIME_CARD="$PWD/out/runs/patchkit/samples" \
  mise run verify -- --only runtime-fixture
```

For a focused native Vitest run, use the same reviewed binaries, ready inputs and generated card samples:

```sh
DIGI_EMUD_DT2=/path/to/dt2-emud DIGI_EMUD_DN2=/path/to/dn2-emud \
  DIGI_RUNTIME_CARD="$PWD/out/runs/patchkit/samples" \
  mise run test:runtime -- service-commands
```

`test:runtime` passes filters directly to the runtime Vitest configuration, runs one coupled daemon at a time,
and never builds native binaries. Omitting the filter runs all runtime acceptance files.

The step shares the gate CPU budget (one coupled daemon, two cores; use a budget of at least two).
Selecting it alone adds no build prerequisite. When another selected step already requires
`build-daemons`, it waits for that build and uses its published per-device binary paths. The step runs
for relevant runtime cases, shared protocol/session/patch code, runtime framework dependencies and
unknown module paths, as well as `--full` or explicit `--only runtime-fixture`. Direct test edits select
their native filename; shared helpers retain their consumers conservatively on both devices. Sample
client changes select the sample cases. Host-only test edits remain host-only. Existing Python/Rust,
FX and smoke owners are retained. Native filenames are printed by `verify --explain`.
When input-adapter cases are selected, the gate checks `test/input-probe` objects with `pk_build --check`
and packs fresh per-device DKMs under its own run directory before launching Vitest. These bounded
preparation commands may build module toolchains or the patchkit CLI, and fail on stale objects.
The direct `test:runtime` task still requires prepared artifacts; it adds no preparation.
Python acceptance dependencies follow the existing import graph from the consumed preparation/client
entry points, with explicit consumed function-local dependencies. Unrelated analysis/device tools keep
their Python and smoke owners. The two device `*-leds.toml` faceplate maps do not select native acceptance.

The separate `dt2-remote-control.test.ts` and `dn2-remote-control.test.ts` files explicitly request the
`device` patch set, where REMOTE starts disabled. They install only digikit/remote, assert exact raw
STATE/status/body replies, toggle REMOTE with physical emulator buttons, and check malformed commands,
TURN clamping/immediate pacing/negative recovery and the final disabled KEY. Original 12/45-second boot,
10-second post-install, 400 ms panel holds, UI settles and 3-second pacing waits remain. Enabled-state
cleanup releases remote inputs before switching off; fixture teardown covers every path. Both devices passed
acceptance before deletion of the replaced Python scenario, runner and dispatch; sample/input-adapter
coverage now uses the managed runtime fixture.
The five mocked protocol/screenshot tests moved from the web suite to
`packages/dk-session/tests/remote.test.ts` under the root host project; mixed browser tests stay with web.
No mise task semantics changed. Reuse/reset and snapshots remain later work.


The coordinator's serial four-case acceptance passed in 608 seconds using existing pinned daemon
binaries, without a native build: [acceptance log](../../out/runs/verify/20261026-001204/runtime-fixture.log).
File/body times were DT2 negative 163.893/117.436 s, DT2 positive 149.687/97.149 s,
DN2 negative 162.688/151.364 s and DN2 positive 130.387/121.804 s. The coordinator confirmed no owned
daemons remained. The host suite passed 46 tests in seven files; focused routing/discovery/selected
Python checks passed 471 tests, and relevant types, lint/format and pathcheck passed. No speedup is claimed.


CLI transfer acceptance lives in `runtime/transfer-live.ts` with one fresh daemon per device case.
Run `mise run test:runtime -- transfer-live` with the binaries and DT2 card described above. The shared
`runtime/transfer-cli.ts` invokes `tools.device.dktransfer` for install, delete and protocol restart;
it saves raw CLI output on success and failure. Assertions cover the install result, epoch increase,
store persistence, mirror SHA-256 and `card.folder` metadata, delete/empty listing, `card.sync` removal,
and the next boot's `MOD_LOAD E_NOT_FOUND`, `UI_FIRST` and empty MODULES. Artifacts are saved under
`out/regression/transfer-live/<device>/`; the mirror itself lives under the owned Session run directory
and is removed at teardown. The 12-second free-running settle, 30-second mirror bound and exactly one
post-delete protocol restart preserve the original boot history. Each CLI command has a 300-second
bound and each transfer test body a 1000-second bound; shared startup/teardown bounds still apply.

Transfer acceptance is owned by the native runtime suite and is now selected on relevant changes.
Both device cases passed before the old transfer runner body and TOML were deleted. Python's full-tier
sample and input-adapter coverage now uses native runtime cases; the Python live wrappers, scenarios
and dispatch have been removed. Fixtures, including `digikit/pluck`, remain in place.


The coordinator's serial transfer acceptance passed on 2026-10-26 with existing preflighted binaries:
`mise run test:runtime -- transfer-live` completed in 123.28 seconds. DT2 file/body times were
79.078/48.250 seconds; DN2 file/body times were 43.876/37.180 seconds. Persistent evidence is the
per-device CLI logs and store/folder/boot JSON in `out/regression/transfer-live/`; these are artifact
records, not a raw Vitest output log. No performance comparison is claimed.

## Sample acceptance prerequisites

The managed DT2 and DN2 sample cases require the existing converter artifact
`apps/web/public/dksample.wasm`; prepare it explicitly with `mise run wasm:dksample` when missing or
stale. The tests do not build it. Supply the preflighted per-device daemon and complete current ready
inputs described above. DT2 also needs `DIGI_RUNTIME_CARD` and `DIGI_PLUSDRIVE` naming an existing
plusdrive binary for the independent card-image oracle. These are coupled runtime cases.

With those prerequisites set, use the native Vitest filename filters:

```sh
mise run test:runtime -- dt2-samples-live.test.ts
mise run test:runtime -- dn2-samples-store-live.test.ts
```


Input-adapter cases require checked, freshly packed `test/input-probe` artifacts at
`DIGI_RUNTIME_MODULES/<device>/input-probe.dkm` (default root `out/regression/modules`). The gate prepares
these in its own directory; direct `test:runtime` needs them prepared separately. An earlier focused
DN2 comparison preserved the original three-distinct-screen failure (two observed), with matching
complete panel/probe hashes, five zero probe statuses and invalid replies of -1. That is a historical
focused result, not a parity claim for the current broad gate. `expect.soft` retains later checks and
the failure without a skip or threshold change. Focused reference report:
`out/regression/legacy-input-reference-20261026/digikit0/input-adapter-dn2-1.12/report.json`.

## Runtime module-load acceptance

`mise run test:modules` uses `vitest.modules.config.ts` and the existing patched-session fixture,
separately from `test:runtime`. Three production runtime uploads (`digikit/pluck`, `digikit/freqshift`
and `digikit/remote`) on DT2 1.17 and DN2 1.12 define six lifecycle cells. Each cell owns a fresh
coupled daemon, mirrored store and selection; cells run serially. The `module-load` gate step selects
native filenames from the existing manifest dependency graph: a production leaf runs its two device
cells; known static leaves retain their existing owners. Runtime patch-set/ABI/shared dependencies and
unknown module paths conservatively select all three uploads. `--only module-load` and `--full` run all.
The gate checks selected module objects with `pk_build build ID --check`, then packs fresh DKMs for both
devices under its own run directory and supplies `DIGI_RUNTIME_MODULES`. Stale objects fail before any
acceptance starts. The check can invoke module toolchains and packing may build the patchkit CLI;
no daemon build is added by this step. A daemon build already selected elsewhere is reused.

Supply the same reviewed `DIGI_EMUD_DT2`/`DIGI_EMUD_DN2`, provenance-checked stock ready inputs and
`DIGI_RUNTIME_CARD` sample directory described above. Prepare current module objects separately
with their existing build owner, then explicitly pack the artifacts before acceptance. For example:

```sh
uv run python -m tools.patchkit.patchkit pack digikit/pluck --device dt2 -o out/regression/modules/dt2/pluck.dkm
uv run python -m tools.patchkit.patchkit pack digikit/freqshift --device dt2 -o out/regression/modules/dt2/freqshift.dkm
uv run python -m tools.patchkit.patchkit pack digikit/remote --device dt2 -o out/regression/modules/dt2/remote.dkm
```

Repeat those pack commands with `--device dn2` and the output directory `out/regression/modules/dn2/`.
`patchkit pack` is the existing preparation tool and may rebuild its own CLI when stale; the acceptance
command never builds or packs modules, daemons or generated cores. Missing prerequisites fail.
`DIGI_RUNTIME_MODULES` optionally replaces the artifact root `out/regression/modules`; preserve its
per-device directories and `pluck.dkm`, `freqshift.dkm`, `remote.dkm` names.

Native Vitest filename and test-name filters select a module/device without a custom runner:

```sh
DIGI_EMUD_DT2=/path/to/dt2-emud DIGI_RUNTIME_CARD="$PWD/out/runs/patchkit/samples" \
  mise run test:modules -- pluck.test.ts --testNamePattern dt2
```

Omit filters, and supply both binaries, to run all six cells. The body timeout is 300 seconds per cell.
Routing is implemented in `b50bdc2a`; broad gate `20261026-045943` passed all six
`module-load` cells in 597 seconds. DN2 DSP acceptance waits up to 30 seconds for the selected module's `MOD_DSP`
completion before requiring status zero and checking registration; timeout logs are retained.
No lifecycle speed comparison is claimed. Assertions cover native
`DkSession`/`EmuUsbTransport` upload, fresh card-byte verification, selection and protocol reboot,
persistence, reported SDK contracts/version, DSP-loader completion, PLUCK voice/type registration,
module guard counters, runtime log and guest faults. Remote also checks absent-before-upload service
refusal and real STATE, malformed KEY and RELEASE dispatch. Diagnostic JSON remains under
`out/regression/module-load/<device>/<module>/`.

These cells establish load/registration/fault observations, not rendered audio correctness or physical
device acceptance. They do not claim coverage of all 23 module manifests. Framework/static modules
are firmware prerequisites rather than upload candidates; their existing Python/Rust, patchkit smoke,
boot-scenario and module conformance checks remain separate. DSP functional/conformance coverage also
remains separate. Fixtures retain fresh coupled sessions; zero-DSP patched restart and independent
session reset/reuse remain unsupported or unproven.

Broad gate `20261026-045943` completed in 3263 seconds (54m23s), reporting runtime failure while
other steps passed, including smoke (576 seconds). Its Python PASS was misleading: two deleted live
wrappers exited pytest with code 4 and empty valid JUnit. Follow-up fixes stop routing deleted direct
test files and make every nonzero exit fail the aggregate, preserving testcase details and adding a
file-level error for unexplained exits. The existing 16 outcome tests and targeted verify reporting
check pass; these fixes have not had another broad gate.

DT2 samples' repeat-run artifact collision is fixed: migrated live workflows retain unique
`out/regression/<workflow>/<device>/run-*` directories, reused by the same Session/workflow. Two identical
live reruns passed in 57.66 and 56.84 seconds, preserving both artifact directories. Four runtime assertions remain
unresolved: DN2 input first-frame panel/probe mismatch, DT2 input two distinct screens instead of three,
DN2 remote pacing second reply 30 instead of 0, and DN2 remote keys no OLED change after TURN.
Assertions are unchanged and semantic causes are not fully established. The earlier focused DN2 input
parity result does not characterize this broad run. Known chorus-width and zero-DSP restart limitations
remain separate.

## Stock coupled audio

DN2 1.12 trackFX has its own `vitest.fx.config.ts`, separate from the stock CF-only UI and patched runtime configurations. `mise run test:fx` runs 34 quick assertions; `DIGI_FX_SWEEP=1 mise run test:fx` runs all 79 assertions across 47 canonical cases, without building. It needs preflighted stock coupled inputs and an existing binary; the caller supplies `DIGI_JOBS` from the shared CPU budget. Native test-name filters render only declared cases and their references. Python still owns cached rendering and NumPy analysis. See [the coverage map, lifecycle and commands](scenarios/README.md).


DT2 1.17 stock FX uses `vitest.dt2-fx.config.ts` and the native `mise run test:fx:dt2` command:
135 quick assertions, or all 146 across 60 scenarios with `DIGI_FX_SWEEP=1`. It uses the existing
one-sample-card ready inputs and preflighted coupled-run binary, without an implicit build. Filename
and test-name filters pass directly to Vitest; see [coverage and lifecycle](scenarios/README.md).
The canonical task runs quick by default; use
`DIGI_FX_SWEEP=1 mise run test:fx:dt2` for the former DT2 task's full selection. Gate `dt2-check`
preserves quick unless `DIGIKIT_TIER=full`, and resets ambient sweep/subset flags. DT2 sources/config
retain their device oracle/tier coverage; shared `fx-support.ts` retains both DN2 and DT2 routes.
Clock sweeps retain the previous workload selections (DN2 quick, DT2 full), using native JUnit output
and DT2 reports under `out/regression/dt2-fx/`. The lane's full native DT2 run passed all 146
assertions in 420.48 seconds with 60 renders and zero cache hits; all expanded inputs matched the
retired source. The integrated filtered routing assertion passed in 7.90 seconds with a five-scenario closure
(145 assertions skipped); artifacts remain under `out/regression/dt2-fx/1792987837855-11088/`.
The broad branch gate remains separate.
The known DN2 chorus-width failure is unchanged; no performance comparison is claimed.
