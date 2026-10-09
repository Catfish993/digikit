# Scenarios

Scripted panel inputs for headless coupled runs (`coupled-run`, the desktop
app's `--scenario` mode and the browser parity check). One file per device:

- `dn2.json` is embedded into `coupled-run` as its default set.
- `dt2.json` is passed with `--scenarios` (done for you by `--device dt2`).

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
- `qa_script` also plays the shared QA sequence of `native/boot/examples/common`
  (NO, NO, encoder A).
- `expect_sha256` pins the SHA-256 of the PCM. A mismatch fails the run with
  exit code 2. Update a pin only for an intended change, and say why in the
  commit. Scenarios with `"validation": true` are checks, not loads.

## Run

```
mise run scenarios                      # every DN2 scenario, exit 1/2 on a bad pin
mise run scenario trig1-taps [--wav]    # named scenarios
uv run python tools/coupled.py scenario --all --device dt2
mise run scenario:replay FILE           # a desktop "Export diagnostics" file
mise run scenario:bisect NAME           # which DSP tier changes the PCM
```

`--jobs N` (scenario command) splits the scenarios over N concurrent
`coupled-run` processes by their `seconds`, then merges the reports. Each scenario
restores its own ready snapshot, so the PCM hashes equal the single-process ones.
Not combinable with `--seconds` or `--dump-dsp`. Default 1.

The runs need the private ready inputs (`mise run emu:ready -- dn2|dt2`). Pins are
only valid for the firmware and ready snapshot they were recorded with.

## Output

`out/runs/<scenario>/<UTC stamp>/report.json` (or `--out`), plus `audio.wav` with
`--wav`. The report lists, per scenario, the PCM hash, RMS windows and gates.

## Oracle tests

`tests/scenarios/` renders DN2 FX scenarios headless and asserts relationships
that were measured (docs/findings/15) and checked against the manual, so a
translator or performance change cannot silently break an effect. All of it is
marked `slow`, so plain `pytest tests` skips it.

```
mise run scenarios:check      # fast subset (61 tests, about 3 min)
mise run scenarios:sweep      # plus the full sweeps (every tf-* and sf-* scenario)
uv run python -m pytest tests/scenarios --slow -k delay      # only what you select is rendered
```

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
`tools/fxcheck.py` row) and renders all scenarios that the selected tests
declare with `@pytest.mark.scn(...)` in one `coupled-run` process per scenario
file, together with their dry references (`fxcheck.reference_for`).

Asserted: the read-back of the intended parameter and no other stored
parameter changed; controls at 0 are bit-identical to dry; L == R for mono
chains; delay spacing (TIME + 1) x 15.625 ms (2 ms), one echo at feedback 0,
-6 dB per repeat at the default (1 dB), WID minimum puts the delay in L
(R 40 dB lower); bit reduction and overdrive error rise, SRR effective rate
falls, reverb RT60 rises with decay, chorus rate rises with speed and L/R
decorrelate with width; every scenario is finite, DC below 1e-3 and bounded
(growth only where the feedback is above unity).

Add a test: put the scenario in `scenarios/dn2-*.json` (generators in
`tools/gen_*.py`), add its name to the fast or sweep list in
`tests/scenarios/test_dn2_fx.py` (`params(...)`, `series(...)`, or a
`@pytest.mark.scn(name)` on the test), and assert on `render(name).row`
(a measure from `tools/fxcheck.py`; add new measures there, not in the test).
A known-bad case gets `xfail(strict=True)`.
