# Scenarios

Scripted panel inputs for headless coupled runs (`coupled-run`, the desktop
app's `--scenario` mode and the browser parity check). One file per device:

- `dn2.json` is embedded into `coupled-run` as its default set.
- `dt2.json` is passed with `--scenarios` (done for you by `--device dt2`).
- `dt2-trackfx.json` and `dt2-sendfx.json` are DT2 FX sweeps (generator `tools/emulator/gen_dt2fx.py`).
  They need the one-sample card: `coupled.py scenario --device dt2 --dt2-card
  --scenarios tests/scenarios/dt2-trackfx.json --all` (or `DIGI_DT2_CARD=1`), and a peek of
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
mise run scenario:check       # 61 quick DN2 assertions, no build
mise run scenario:sweep       # all 191 DN2 assertions, no build
mise run test:fx -- -t 'test_delay_spacing fast'  # selected cases + references only
mise run scenario:check:dt2   # existing Python DT2 suite
```

The following pytest fixture options and lifecycle apply to DT2:

Batches are rendered in parallel: `--jobs N` (default `DIGI_JOBS`, else
`min(cpus - 2, 6)`; `--jobs 1` is one process) coupled-run processes, each about
0.5 GB, holding the benchlock shared. Each lands in `<run>/shard-<i>/` (logs) and
the merged report and the WAVs in `<run>/`. `--no-build` uses the existing binary.

Options: `--diag LIST` (as `coupled.py --diag`), `--keep-runs` (keep the WAVs;
by default they are deleted at the end), `--device`, `--sweep` (or `-m sweep`).
Runs go to `out/runs/tests/<stamp>/<file>-<n>/` (report, WAVs, `fxcheck.json`,
logs); the path is printed. The tests skip, with the reason, when the ready
inputs, the generated core or the MAIN OS image are missing.

Every scenario restores the ready snapshot, so nothing is shared. The session
fixture `emulator` builds `coupled-run` once. The fixture `render(name, ...)`
returns `Rendered` objects (rate, `x`, report entry, decoded read-back, the
`tools/emulator/fxcheck.py` row) and renders all scenarios that the selected tests
declare with `@pytest.mark.scn(...)` in one `coupled-run` process per scenario
file, together with their dry references (`fxcheck.reference_for`).

DT2: `uv run python -m pytest tests/scenarios/test_dt2_fx.py --device dt2 --slow [--sweep]`
renders `dt2-trackfx.json` / `dt2-sendfx.json` on the card and asserts the same kinds of
relationships (read-back of the intended parameter, nothing else changed; BR/SRR/overdrive
error rises, send and delay/reverb/chorus/master parameters move the audio in the expected
direction). `--device dt2` selects these tests. `verify.py --full --dt2` also checks that every DT2 FX scenario gives the same
PCM with and without AOT (about 40 min).

Add a DN2 case and assertion in the corresponding `tests/regression/scenarios/dn2-*.ts` source and `.test.ts` file. Declare the needed scenario names beside its Vitest identity so selection renders only their reference/companion closure. Add numerical measurements in `tools/emulator/fxcheck.py`, not in the test. DT2 cases remain in the JSON files generated by `tools/emulator/gen_dt2fx.py`, with Python assertions in `tests/scenarios/test_dt2_fx.py`.
