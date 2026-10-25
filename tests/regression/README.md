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
DIGI_EMUD=/path/to/emud pnpm --filter @digi/regression test      # or: pnpm test:regression at the root
```

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

`pnpm --filter @digi/regression test:host` runs the mocked startup, cleanup and USB wiring checks without
firmware, native builds or daemons. These checks and package/type checks run in the existing `web` step.
Consumer typecheck/check scripts explicitly generate `@digi/dk-session` declarations first; its runtime
export still resolves to the existing TypeScript source. The normal `test` command retains the stock DT2 suite; runtime acceptance has a separate configuration.

The `runtime-fixture` gate step runs only the two runtime boot acceptance cases, sequentially, with one
fresh coupled daemon per device. It requests `firmware.load {runtime:true, start:"auto"}` and the existing
`emulator` patch set/cache, then selects free mode. It checks reported patch identity, ready/free status
and health; host tests cover the `DkSession`/`EmuUsbTransport` wiring and pending-request cancellation.
It does not install modules or assert remote-control behavior. Stock restart and instruction-step waits
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

The step shares the gate CPU budget (one coupled daemon, two cores; use a budget of at least two).
Selecting it alone adds no build prerequisite. When another selected step already requires
`build-daemons`, it waits for that build and uses its published per-device binary paths. Changes to the runtime
cases/configuration, shared regression fixture, session lifecycle, input provenance or daemon shutdown
select acceptance as well as host checks; ordinary host test edits select host checks only. Broader gate
selection retains its existing conservative smoke rules. Module installation, scenario migration,
reuse/reset and snapshots remain later work.
