# Scenarios

Scripted panel inputs for headless coupled runs (`coupled-run`, the desktop
app's `--scenario` mode and the browser parity check). One file per device:

- `dn2.json` is embedded into `coupled-run` as its default set.
- `dt2.json` is passed with `--scenarios` (done for you by `--device dt2`).
- DT2 FX sweeps live in `tests/regression/scenarios/dt2-trackfx.ts` and
  `tests/regression/scenarios/dt2-sendfx.ts`.
  They need the one-sample card: `coupled.py scenario --device dt2 --dt2-card
  --scenarios tests/regression/scenarios/dt2-trackfx.ts --all`, and a peek of
  the Sound and Fx arrays (finding 18, "DT2 FX scenarios"). `tools/emulator/dt2fx.py render|check|tiers`
  renders both files with the peeks, prints the `tools/emulator/fxcheck.py` tables and compares the
  PCM hashes of two renders; the oracle tests are `mise run scenario:check:dt2`.

## Format

```json
{"scenarios": [{"name": "...", "description": "...",
  "seconds": 10 | "instructions": 100000000,
  "qa_script": true, "exact_events": true, "clip_last_chunk": false,
  "expect_sha256": "...", "events": [ ... ]}]}
```

- A scenario has exactly one end: `seconds` of audio, or `instructions` of
  ColdFire.
- Times count from the restored ready point. `at_instr` is in ColdFire
  instructions, `at_s` in audio seconds (DSPI2 frames sent / 1500).
- Events: `{"at_instr"|"at_s": T, "button": CODE, "down": true|false}` for a
  button, `{"at_instr"|"at_s": T, "turn": ENCODER, "detents": N}` for an
  encoder turn.
- Events fire at chunk boundaries, before the chunk that follows, so a run is
  a function of its inputs. `clip_last_chunk` ends an `instructions` run exactly at its count. `exact_events` shortens the chunk so an `at_instr`
  event fires at exactly that count.
- `qa_script` also plays the shared QA sequence of `packages/emulator/boot/examples/common`
  (NO, NO, encoder A).
- `expect_sha256` pins the SHA-256 of the PCM. A mismatch fails the run with
  exit code 2. Update a pin only for an intended change, and say why in the
  commit. Scenarios with `"validation": true` are checks, not loads.

## Run

```
mise run scenario:all                   # every DN2 scenario, exit 1/2 on a bad pin
mise run scenario trig1-taps [--wav]    # named scenarios
uv run python -m tools.emulator.coupled scenario --all --device dt2
mise run scenario:replay FILE           # a desktop "Export diagnostics" file
mise run scenario:bisect NAME           # which DSP tier changes the PCM
```

`--jobs N` (scenario command) splits the scenarios over N concurrent
`coupled-run` processes by their `seconds`, then merges the reports. Each scenario
restores its own ready snapshot, so the PCM hashes equal the single-process ones.
Not combinable with `--seconds` or `--dump-dsp`. Default 1.

Renders are cached per scenario by content in `out/cache/renders/` (see
`docs/TESTING.md`, "Render cache"). A hit restores the report entry and the WAV;
`--diag` runs bypass the cache. Force renders with `--no-cache` or
`DIGI_RENDER_CACHE=0`. `DIGI_RENDER_CACHE_CHECK=p` re-renders each hit with
probability p and fails on a difference (`verify` uses 0.03).

The runs need the private ready inputs (`mise run emu:ready -- dn2|dt2`). Pins are
only valid for the firmware and ready snapshot they were recorded with.

## Output

`out/runs/<scenario>/<UTC stamp>/report.json` (or `--out`), plus `audio.wav` with
`--wav`. The report lists, per scenario, the PCM hash, RMS windows and gates.

## Oracle tests

DN2 trackFX/sendFX live in `tests/regression/scenarios` as compact TypeScript sources and native Vitest assertions. [The coverage map](../regression/scenarios/README.md) records all 134 scenarios and 191 assertion identities. Python retains DT2 oracles and shared rendering/NumPy analysis. Plain `pytest tests` excludes the slow DT2 tests.

```sh
mise run test:fx              # 61 quick DN2 assertions, no build
DIGI_FX_SWEEP=1 mise run test:fx # all 191 DN2 assertions, no build
mise run test:fx -- -t 'test_delay_spacing fast'  # selected cases + references only
mise run test:fx:dt2          # 135 quick DT2 assertions, no implicit build
```

DT2: `pnpm --filter @digi/regression test:fx:dt2` runs the 135 quick assertions;
`DIGI_FX_SWEEP=1 pnpm --filter @digi/regression test:fx:dt2` runs all 146.
Native Vitest file/name filters select the rendered reference/companion closure.
The 60 scenarios use DT2 1.17 ready inputs and the one-sample card. Each render
independently restores its ready snapshot. The existing native emulator binary is
required. Card preparation invokes the small +Drive helper build/check, unless
`DIGI_DT2_CARD` points to an existing compatible card image.
Reports, measurements and logs remain under `out/regression/dt2-fx/`; WAVs are
removed after the run unless `DIGI_FX_KEEP_RUNS=1`. Python keeps NumPy analysis and
the shared content render cache. No lifecycle speed improvement is claimed by
the migration. `verify.py --full --dt2` also compares DT2 FX PCM with and without
AOT; this expensive differential run remains separate.

Add cases and assertions to the corresponding DN2 or DT2 TypeScript sources in
`tests/regression/scenarios/`. Declare needed scenario names beside each Vitest
identity so selection renders only their reference/companion closure. Add numerical
measurements in `tools/emulator/fxcheck.py`. The DT2 encoder calibration ladder is
preserved: `node tests/regression/scenarios/dt2-trackfx.ts --ladder` prints its 46
fixed-slot calibration scenarios without playing a note.

DT2 migration validation: the full native suite passed all 146 assertions in 420.48 seconds,
with all 60 scenarios rendered and zero cache hits. Expanded inputs matched the retired JSON
exactly. This is acceptance evidence, not a performance comparison; the integrated filtered routing assertion also passed in 7.90 seconds (five-scenario closure,
145 assertions skipped). The broad branch gate remains separate.
