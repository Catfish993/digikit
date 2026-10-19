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
