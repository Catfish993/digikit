# Handover 2026-10-08: a scenario harness for DSP semantics

Base: `work/sharc-emulator` at 5803cc9. Results go to `docs/findings/`, not here.

## State

The branch delivered a native emulator that runs the real firmware, ColdFire and SHARC+
together, on both devices:

- Desktop app (`mise run emu`), browser CF core and WASM parity (`mise run web:parity`).
- Ready states built from the `.syx` alone (`mise run emu:ready -- dn2|dt2`), with a
  cold DSP start (finding 16). DT2 loads samples from a +Drive image (`--samples`).
- Control socket and hot DSP injection (`mise run emu:serve`, `tools/digi_client.py`,
  `tools/dsp_patch.py`; finding 17).
- Scenarios with pinned PCM hashes (`mise run scenarios`, `scenarios/README.md`) and run
  output in `out/runs/<scenario>/<stamp>/` (`docs/INSTRUMENTATION.md`).
- DSP performance tooling and the freeze state (`mise run perf`; finding 18): 1.58 DSP CPU
  per audio second on DN2 `trig1-taps`, target 1.0 not met.

Latest commits: 5803cc9 tidy the branch (scenarios/, grouped mise tasks, one run layout,
oxlint); 591eb1b performance state at freeze; 911b1fc volatile M registers are no longer
baked; 564e4c5 generated helper calls; f0a3180 circular tail-write fix, generated forms on
by default. The docs pass of 2026-10-08 moved handovers to `docs/handovers/` and folded
the lane notebooks into the findings.

## Goal

A scenario-based regression and oracle suite for the DSP that covers the FX paths. It has
two uses: to find semantic bugs in the SHARC+ core (our decoding or instruction semantics),
and to show which DSP code matters for performance, per scenario.

## First targets (reported by Em on DN2)

- The delay does not sound like a delay.
- Bit reduction does not sound like proper bit reduction.

Both are probably semantics gaps, not parameter problems. Suspects: circular buffer
wrap (B and L registers, post-modify and MODIFY), fixed-point conversion between float and
Q31, rounding, and saturation. Finding 18 records one circular tail-write bug in the fast
tier, already fixed; the FX code itself has not been traced.

## Plan

1. Scenarios that set parameters with encoder turns (`{"turn": ENCODER, "detents": N}` events
   in `scenarios/*.json`), one scenario per FX and setting. Start with delay time,
   feedback and send, then bit and rate reduction.
2. Oracle checks that need no hardware, computed from the rendered PCM:
   - echo spacing and decay against delay time and feedback;
   - feedback 0 gives exactly one echo; send 0 equals the dry signal;
   - quantisation: the number of distinct levels and the noise floor against the bit
     setting; aliasing products for rate reduction;
   - L == R for a centred mono source;
   - no DC offset, no NaN or infinity, no runaway growth.
3. Plots and features per run: waveform, envelope and spectrogram PNGs plus a feature
   file, written to `out/runs/<scenario>/<stamp>/`. Build this as a tool under `tools/`,
   not as one-off scripts.
4. `mise run perf` per scenario, so each FX path gets its own cost ranking of DSP
   functions.
5. Later: DT2 hardware recordings of the same settings as a reference. None exist yet.

When a check fails, trace the writers of the wrong value with `tools/sharc.py` and the
write watch (`SHARC_WWATCH`, `SHARC_PCDUMP`), as done for the DC blocker in finding 16.
Change `tools/sharc_core` only, regenerate (`mise run gen:regen`), and update the golden
hashes only for an intended change (`tests/test_sharc_golden.py`).

## Open items

- The absolute DN2 level after the cold-start reference. It is 12 times quieter than the
  old state (tap peak 0.056 against 0.693). The old state was wrong (finding 16), but the
  hardware level is unconfirmed **[O]**.
- Browser-host work is parked on the branch `wip/browser-host` (two-instance split,
  ready-state cache; finding 07).
- Performance levers, ranked in finding 18: mask-free AOT emission, chaining, class (e)
  exits.
- `mise run lint:rust` covers only the clean crates (`CLEAN_CRATES` in `tools/rust_ws.py`).
- Docs: the handovers `docs/HANDOVER-2026-10-01-rust-desktop-browser.md`,
  `docs/HANDOVER-2026-10-06-dn2-live-audio.md` and `docs/HANDOVER-2026-10-07-next.md` still
  sit in `docs/`; move them to `docs/handovers/` when committed.
