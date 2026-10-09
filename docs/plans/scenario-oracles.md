# Plan: scenario oracles for the SHARC+ DSP

Branch: `work/scenario-oracles`. Started 2026-10-08. Tick boxes as work
lands, and add a line to the status log at the end of each session.

Any agent can resume from this file. Read `CLAUDE.md` first (repo rules:
never commit firmware or anything derived from it, never name the vendor DSP
toolchain, results go in `docs/findings/`, commit only when Em asks), then
this plan, then `docs/handovers/HANDOVER-2026-10-08-scenario-harness.md`.

## Goal

A test suite that renders scenarios headless, checks the audio against
independent oracles, and reports DSP cost per scenario. It serves three
ends:

1. Find SHARC+ semantic bugs in the FX paths. Em hears that the DN2 delay
   does not sound like a delay and the bit reduction does not sound like bit
   reduction.
2. Show which DSP code each FX needs, so the runtime translator covers it.
3. Later, act as the regression gate for patched firmware.

## Decisions (agreed with Em)

- **pytest is the entry point.** A session fixture sets up the emulator:
  build `coupled-run`, check the firmware hash, and build or cache the ready
  state. Each `render()` restores the ready state, plays the events, and
  returns the PCM, the read-back parameters and the report. Today a render
  is one `coupled-run` process. A long-lived daemon with restore may replace
  it later without changing the tests.
- **`tools/coupled.py` stays as the CLI** (bisect, replay, perf). Its build,
  ready-input and run code moves into a shared module that both it and the
  fixtures use.
- **No state shared between tests.** Restore costs about 0.2 s, and audio
  rendering dominates the time (about 1.5x real time).
- **Oracles assert relationships, not snapshots.** Examples: delay 2x gives
  echo spacing 2x; feedback 0 gives one echo; send 0 equals dry; fewer bits
  give fewer levels; centred mono gives L == R; no DC, NaN or runaway
  growth. Known-bad cases use `xfail(strict=True)`. Feature baselines, with
  tolerances, are written only after Em has listened and accepted a result.
  Plots are for people and are never compared automatically.
- **Every detector is tested on synthetic signals first.**
- **Every scenario reads back the parameter it set** from firmware memory,
  and asserts it.
- **Instrumentation is off by default.** `--diag` passes through to
  `SHARC_WWATCH` and `SHARC_PCDUMP`. A test proves the PCM is unchanged with
  diagnostics on.
- **DT2 leads the FX tests.** DT2 and DN2 share the FX code (Em). On DT2
  the source is a sample we write, so any deviation is in the FX or mixer,
  each FX gets a matched test signal, and Em's device (DT2 1.16) can record
  the same input over Overbridge. DN2 keeps a Trig 1 sine smoke test and one
  cross-check per FX.
- **The no-AOT config is the tracked performance number.** It is what the
  browser and patched firmware will run (finding 18: "fast-only" 7.04 DSP
  CPU-s per audio s, 5.57% interpreted; target about 0.1% interpreted).

## Known facts (with sources)

- Scenario format: `scenarios/README.md`. Events are `button` or
  `turn` (`{"turn": ENCODER, "detents": N}`) at `at_instr` or `at_s`.
  `exact_events` fires at the exact count. No scenario uses `turn` yet.
- Runner: `native/coupled/src/bin/coupled_run.rs`. Report `pcm` and
  `gates` at about l.666-700. `--wav` writes float WAV; the channel count
  is not confirmed (check the writer, about l.489-560). 48 kHz.
- Ready inputs: `tools/digi_env.py` `ready_inputs(device)`; built by
  `tools/ready.py` (`mise run emu:ready -- dn2|dt2`, about 2 min).
- NO press before Trig 1 is NOT needed on the current DN2 ready state
  (finding 16, l.233-237). It was needed only with the 2026-10-03 snapshot
  (SLC dialog). DT2 with a card shows "FILE SYSTEM OK / PRESS Y/N" and
  needs one NO (finding 14, l.1148).
- DT2 samples: `tools/plusdrive.py build DIR -o IMG` builds a +Drive card.
  With `--main-os` it writes an active project in which every sample
  reference points at the first input file. The ready state is cached per
  card (`tools/ready.py dt2 --card-image IMG`). `tools/dt2_sample_check.py`
  does this for a 1 kHz sine. Kit and slot layout: finding 14 (about l.43,
  l.136-141).
- Control socket (`native/control`, `tools/digi_client.py`) has inputs but
  no sample capture and no state save/load.
- MIDI input is not modelled.
- Panel button codes (DT2, `docs/handovers/HANDOVER-2026-09-13.md:127-145`,
  `devices/digitakt-ii.toml`): 1 TRIG, 2 SRC, 3 FLTR, 4 AMP, 5 FX, 6 MOD,
  7 PRESET, 8 SETTINGS, 9 TEMPO, 10 YES, 11 UP, 12 NO, 13 LEFT, 14 DOWN,
  15 RIGHT, 16 TRK, 17 FUNC, 18 KEYBOARD, 19 RECORD, 20 PLAY, 21 STOP,
  22 PAGE, 23 PTN, 24 SONG, 25..40 TRIG 1..16, 41..48 encoder A..H push,
  49 LEVEL, 50 SAMPLING. FUNC chords: FUNC down, page down/up, FUNC up.
- `turn` encoders: 1..8 = A..H, 9 = LEVEL (`devices/digitakt-ii.toml:128`).
  `runtime.rs:672-697` sends one UART packet per up to 127 detents; the
  firmware reportedly clamps a flush to +/-30 (`emu/panelin.py:129`,
  unverified). Use +/-1 detent events spaced apart, and read back.
- WAV from `--wav`: stereo, 32-bit float, 48 kHz, interleaved L,R
  (`native/boot/src/wav.rs`, created at `native/coupled/src/lib.rs:541`).
- DT2 1.16 parameter descriptors (`tools/paramtable.py --codes ...`;
  range 0..0x7f00, value in the high byte):
  0x43 Chorus Send, 0x44 Delay Send (mirror 55), 0x45 Reverb Send
  (mirror 56), all owner FX, default 0; 0x71 Delay Time (owner Delay,
  mirror 32, default 0x17), 0x72 Pingpong, 0x73 Stereo Width,
  0x74 Feedback Gain (mirror 35, default 0x20), 0x75/0x76 feedback HPF/LPF,
  0x78 Delay Mix Vol.; 0x7b Pre-delay, 0x7c Decay Time (owner Reverb,
  mirror 42, default 0x1f), 0x81 Reverb Mix Vol.; 0x95 Master Overdrive.
  Track mirror: `0x80003362 + track*0x8e + idx*2` (FX sends, owner
  0x11). Delay and reverb are owned by other objects (0x14, 0x13), so their
  mirror base is still unknown.
- [C] Bit reduction and SRR ARE in the main table (an earlier grep was cut
  short). Track FX group: DT2 1.16 codes 0xff Bit Reduction, 0x100
  Sample-Rate Redux, 0x101 SRR Routing (0..256), 0x102 Overdrive, mirror
  64-67; DN2 1.11 codes 0x12d-0x130, mirror 95-98. All 0..0x7f00, default 0.
- Main table by signature (not yet second-checked): a 36-byte reader
  `20 2f 00 04 0c 80 00 00 CC CC 55 c1 41 f9 BB BB BB BB 73 01 c0 81 22 00
  e5 89 ed 88 90 81 20 30 08 04 4e 75` (CC CC = entry count, BB = base),
  one hit per image: DT2 1.16 0x4020f18c (275), DN2 1.11 0x401f7f94 (321),
  DT2 1.15C 0x401fc274, DN2 1.10E 0x401e29a0. Entry 0 is "Error"/"ERR".
  Codes 0x43..0x9f mean the same parameter on both devices; owner ids and
  some mirror indices differ, so key on names, never on owner ids.
- Where values live: `mirror_index` indexes a per-set s16 array (71
  entries on DT2, 101 on DN2). Track-level owners (machine, filter, amp,
  track FX incl. sends, BR, SRR, overdrive) are in the per-track
  SoundParameterSet (`array + 0x14 + idx*2`); Chorus, Delay, Reverb,
  Master, Ext-in and LFOs are in the kit's FxParameterSet (`array +
  idx*2`; Delay mirror 32-40, Reverb 41-48), a member of KitActive. The
  runtime address of each array is still open.
- DT2 has a second descriptor table (stride 0x34, 164 entries, base
  0x401e9d7c, reader 0x400071e4) with page-level views of SRR, Overdrive
  and Bit Reduction; DN2 has an unreferenced 0x3c-stride one at
  0x401d0acc. Not needed for read-back.
- `trig1-noqa-40m` (Trig 1, no prelude) sounds on the current DN2 ready
  state: rms 0.032, pin matches (run 2026-10-08).
- Hand-named 1.16 addresses (live kit and track records): `tools/cf_names.py`.
- Existing audio checks: whole-PCM sha256, rms, peak, non-finite, 100 ms
  window RMS. No FFT, echo, L/R or DC analysis exists.
- Perf: `tools/perf.py` (`mise run perf`), configs listed in finding 18.

## Minimal path (do this first)

Token-saving route to the first verdicts, for the two FX Em heard:

1. Phase 1, using Em's desktop recording.
2. Phase 3 (no firmware knowledge needed: Sonnet, Haiku or another tool;
   can run in parallel in a worktree).
3. Phase 4, fixture only: `tests/scenarios/conftest.py` imports
   `tools/coupled.py` functions directly; scenarios are hand-written JSON.
4. Phase 5, delay and bit reduction only.

Deferred until needed: the per-track project record (use one card per test
signal; each ready state is cached), the shared-module refactor,
`tools/scenario_dsl.py`, and the `--diag` identity test (do it in Phase 6).

Working rules for agents: start from the file:line references here instead
of exploring; read `features.json`, not plots or WAVs (Em views those);
keep scenarios to 1-2 s of audio; use the deep model only for Phase 6.

## Phases

Sizes: S = under an hour of agent work, M = one session, L = open-ended.
"Em" marks a step that needs Em.

### Phase 1: groundwork (M)

- [x] Fix the stale NO-press notes: descriptions of `trig1-hold`,
      `trig1-taps` and the scenario at about l.211 in `scenarios/dn2.json`;
      `native/README.md` l.155-159 (l.155 also calls `trig1-noqa-40m`
      silent; it sounds, rms 0.60).
- [ ] Em: does the DT2 "FILE SYSTEM OK" window appear on hardware after
      loading samples? If not, fix the emulator model instead of pressing
      NO in scenarios.
- [ ] Map encoder numbers and the FX page buttons. Em records one session in
      the desktop app (set delay, reverb, bit and rate reduction), exports
      diagnostics, and `mise run replay FILE` reproduces it. Document the
      map in `scenarios/README.md`.
- [ ] Find the DT2 1.16 addresses of the FX parameters in the live
      kit/track records (`tools/cf_names.py`, the cfdb). Add a report field
      to `coupled-run` that reads named addresses at the end of a run
      (`params`).
- [x] Confirm the WAV channel count and layout from the writer.
- [x] Find where bit reduction and sample-rate reduction live (main table,
      see Known facts).
- [x] Second agent checked the table signatures and FX entries against the
      image bytes; recorded in finding 02 ("The parameter descriptor table
      by signature, and where FX values live").
- [ ] Make `tools/paramtable.py` find the table by signature (both devices).
- [x] Find the runtime addresses of the Sound and Fx arrays: pointer
      chains from fixed globals, finding 02 (DT2 `*0x80004704` kit_t,
      `*0x80004708` Fx array; DN2 `*0x800052a0`, `*0x800052a4`).
- [ ] Add `--peek ADDR:LEN` (repeatable, read at the end of the run, also
      `*ADDR+OFF:LEN` for one pointer hop) to `coupled-run`, reported in
      `report.json`; the Python side resolves names to mirror indices
      (paramtable) and decodes values.
- [ ] Promote the scratch `DT2SNP01` snapshot reader
      (scratchpad `readback/snap2.py`) into `tools/snapread.py`, which
      currently fails on Rust snapshots.
- [ ] `ready_inputs("dt2")` names `snapshots/dt2-audio-ready-cold-2026-10-07/
      digi-audio-m5.snap`, which does not exist; the file is under
      `by-card/8ef11b5b8fc1/`. Check how coupled.py resolves DT2.
- [ ] The DT2 card ready kit ("KIT 1", written by `tools/plusdrive.py`'s
      project record) has non-default, non-round values (master overdrive
      0x7f00, compressor on, reverb decay 0, chorus depth 0x475a). A DT2 cold
      boot without a card (desktop dropdown) shows all FX at 0 (Em,
      2026-10-08). Suspect the project record writer (wrong offsets or
      source). Compare with a no-card boot's kit, fix plusdrive, rebuild the
      card ready state. DT2 FX oracles need this first.

Done when: a scenario sets delay time by encoder turns, and the report shows
the value read back from firmware memory.

### Phase 2: test card (M)

- [ ] Extend the project record written by `tools/plusdrive.py` so track N
      plays input file N (instead of every reference pointing at file 1).
- [ ] Generate test signals (extend `tools/gen_test_samples.py`): single
      click, slow full-scale ramp, sines at chosen frequencies, log sweep,
      left-only and right-only, unity-level reference.
- [ ] Build the card and its ready state; check each trig key plays its
      signal with FX off.

Done when: Trig 1..N each play a different known signal from one ready
state.

### Phase 3: analysis tool (M)

- [x] `tools/audiocheck.py`: load the run WAV, compute features (envelope,
      onsets and echo spacing, echo level ratio, decay time, distinct levels,
      spectrum and alias products, L-R difference, DC, non-finite, growth),
      write `features.json` and PNGs (waveform, envelope, spectrogram) into
      the run dir. CLI: `audiocheck.py RUN_DIR`.
- [x] `tests/test_audiocheck.py`: each detector on synthetic signals with
      known answers. No firmware needed.
- [ ] Add both files to the lint `CLEAN` list and the mypy `TYPED` list.

### Phase 4: harness (M)

- [ ] Move the build, ready-input, run and report code out of
      `tools/coupled.py` into a shared module; `coupled.py` keeps its CLI.
- [ ] `tools/scenario_dsl.py`: builders that emit scenario JSON
      (`trig(n)`, `fx_page()`, `turn(enc, n)`, per-FX setters).
- [x] `tests/scenarios/conftest.py`: session fixture `emulator(device)`;
      skip when the ready inputs are missing or the firmware hash differs;
      `--diag` and `--update-baselines` options; runs kept in
      `out/runs/<scenario>/<stamp>/`. Done except: the firmware hash is not
      compared and there is no `--update-baselines` (no baselines yet); runs
      go to `out/runs/tests/<stamp>/<file>-<n>/`; `coupled.run_batch` is the
      library entry; fixture `render`; options `--sweep`, `--keep-runs`.
- [x] mise tasks `scenarios:check` (fast subset) and `scenarios:sweep`.
- [ ] Test: PCM is identical with and without `--diag`.

### Phase 5: FX verdicts on DT2 (M)

- [ ] Delay: click; time sweep, feedback 0 and high, send 0.
- [ ] Bit reduction: ramp; several bit settings.
- [ ] Rate reduction: sines; several settings, check alias frequencies.
- [ ] Reverb: click; decay sweep.
- [ ] Mixer level: unity reference, FX off.
- [ ] Write a verdict table (plausible / wrong / cannot tell) with plots
      and WAV paths; record results in `docs/findings/`.
- [ ] Em listens and accepts or rejects each verdict.

### Phase 6: fixes (L)

- [ ] For each wrong verdict, trace the writers (`tools/sharc.py`,
      `SHARC_WWATCH`, `SHARC_PCDUMP`). Suspects from the handover: circular
      buffer wrap (B/L registers, post-modify), float/Q31 conversion,
      rounding, saturation.
- [ ] Unit tests on `tools/sharc_core` taken from the ADI manuals' worked
      examples (`out/refs/`) for those suspects.
- [ ] Fix in `tools/sharc_core` only; `mise run gen:regen`; update golden
      hashes with a reason; flip the matching `xfail` tests.
- [ ] Write accepted feature baselines.

### Phase 7: DN2 (S)

- [ ] Confirm the FX functions match across images (sharcdb cross-image
      function hashes).
- [ ] DN2 Trig 1 sine smoke test; one cross-check per FX at the same
      setting.

### Phase 8: performance per scenario (S)

- [x] `tools/perf.py` takes a scenario; report the no-AOT config per FX
      scenario: DSP CPU-s per audio s, interpreted share, uncovered
      functions ranked by cost. Done: `--scenarios FILE --scenario NAME...`,
      `tools/perf_agg.py`; results in finding 18 ("Per-FX coverage").

### Phase 9: hardware reference (M, Em)

- [ ] Em records the same test card and settings on DT2 1.16 over
      Overbridge. Compare envelopes, spectra, echo timing and absolute
      level (settles the open 12x DN2 level question).

## Later (agreed directions, not in this plan)

- Load-time SHARC+ to WASM translation in the browser, cached by .syx hash,
  so no firmware-derived code ships and patched firmware needs no rebuild.
- ColdFire in the browser (1.3 s per audio s): measure the idle and
  RAM-clear share first; idle skipping is likely the cheapest win.
- MIDI input modelling (eDMA channel 34 ring, `emu/symbols.py:830`): exact
  parameter setting by CC, and WebMIDI in the browser.

## Status log

- 2026-10-08: plan agreed; branch created; minimal path added. Phase 1:
  stale NO-press notes fixed; button/encoder map, WAV format and FX
  parameter codes recorded above. numpy and matplotlib added to the dev
  group. Phase 3 done: tools/audiocheck.py + tests/test_audiocheck.py (17
  pass, on CLEAN and TYPED); mypy stays at 3.11 with numpy/matplotlib
  stubs skipped (pyproject override). Parameter tables found by signature on
  both devices (unverified).
  Later the same day: table signatures and FX value locations verified and
  recorded in finding 02. Next: `--peek` read-back, then scenario turns.
- 2026-10-08 (cont.): DT2 output was wrong because coupled.py built DT2 runs
  against the stale DN2 core (finding 15). DN2 and DT2 cores regenerated
  (backups `gen-pre-simd-20261008`); DN2 pins unchanged. Added the
  CORE_FINGERPRINT guard and per-image core selection. Not yet guarded:
  `tools/ready.py:68-70`, `tools/perf.py:416`, `tools/native_wasm.py:47-51,
  133`, `tools/rust_ws.py:69`, `tools/sharc_regionbench.py:299,555`,
  `tools/sharc_build_dbg.py:22`. `out/native/shared-core/gen` (browser/wasm
  core?) is stale and has no fingerprint. Next: Em listens to DN2 delay and
  bit reduction in `mise run emu`.
- 2026-10-08 (cont.): DT2 card kit fixed (factory "KIT 1" replaced by
  "PRESET 1"; finding 02), new card ready state `by-card/1c1fed346ea9`,
  card-less DT2 ready state built at the default path. `tools/snapread.py`
  reads DT2SNP01. `coupled-run --peek` and `coupled.py scenario --scenarios
  FILE --peek NAME=SPEC` added; `scenarios/dn2-fx.json` (6 scenarios) and
  `tools/fxcheck.py`. DN2 results (run dir `out/runs/all/20261008-041331/`):
  bit reduction plausible (error and THD grow with the setting; output not
  on a lattice, so the level count cannot confirm it); **delay send produces
  no echo at all** (send stored as 0x0d00/0x1b00, audio bit-identical to dry
  while the note holds, nothing after release). Encoder: about 0.2 UI step
  per detent (32/64/127 detents -> 5/13/27). `mise run emu` tasks now run
  through `uv run` (system python lacked networkx when ready.py ran).
  Next: Phase 6 on the delay send path.
- 2026-10-08 (cont.): differential: on DN2 reverb send, chorus send,
  overdrive and SRR change the audio; the delay send has no effect even at
  0x7f00 (fx-del-turnmore, fx-del-hold bit-identical to dry). The send
  reaches the DSP (frame 0x154, ring, working copy 0x25c5e0, loaded at
  0x1c2c4a, float 32512.0); destination open (finding 04, DN2 TX frame).
  Next: dump all DSP memory at run end, diff dry/delay/reverb to find where
  each send is stored, then watch the delay's reader.
- 2026-10-08 (cont.): ROOT CAUSE FIXED: SIMD 6a_mem / 6b_shiftimm ran PEx
  only, so `btst` left ASTATY stale and the SIMD `JUMP IF SZ` at 0xb84228
  (AND of both PEs) was never taken; the saturator epilogue in FUN_b84099
  then doubled the delay write position (0x80459234) every frame. Fixed in
  tools/sharc_core (also 18a BTF for USTAT pairs); 11 new tests; golden
  hashes unchanged; cores regenerated (fingerprint 535b23f8..., backups
  gen-pre-6b-20261008). Delay now audible: first echo +0.375 s after note
  start (delay time 24 x 15.6 ms at 120 BPM), tail falls ~6 dB per repeat
  (feedback 32). Finding 15 section is [D]: needs a second-agent check.
  DN2 pins: trig1-taps changed d60ad339 -> e0e6c8af7a84c443a0188e3864437dbd31f474757189cd928a59c945861e1675
  (rms -3%), kb-chord16 (unpinned) changed; others unchanged. Re-pin
  trig1-taps after Em's approval (intended change from the fix).
  Next: second check of the fix; Em listens to the delay; clean echo
  oracle needs a click source (DT2 card) since sine taps overlap echoes.

- 2026-10-08 (cont.): send FX sweep (Delay, Reverb, Chorus pages, Master
  compressor): navigation map, laws and verdicts in finding 15 (last
  section). `tools/gen_sendfx.py`, `scenarios/dn2-sendfx.json`, `sf-*` measures
  in `tools/fxcheck.py`, three new generic measures in `tools/audiocheck.py`.
  Runs: `out/runs/sendfx/run1..run3`. Open: Master Overdrive page, routing
  parameters, hardware A/B for feedback gain, mix taper and Width.

Fingerprint guard now covers: tools/ready.py, tools/perf.py (shared core), tools/native_wasm.py (coupled browser core), tools/rust_ws.py, tools/sharc_regionbench.py (cmd_build), tools/sharc_build_dbg.py, tools/native_pgo.py (cmd_build), on top of coupled.py, native_emu.py and native_pgo.py require_inputs.
- 2026-10-08 (cont.): Phase 4 oracle suite: `tests/scenarios/` (conftest
  `emulator`/`render` fixtures, `test_dn2_fx.py`), `coupled.run_batch`,
  `fxcheck.reference_for`/`companions`, mise `scenarios:check` (61 tests, 173 s)
  and `scenarios:sweep` (191 tests, about 24 min). All pass; one false growth
  alarm (sf-del-time-u127: an echo in the last tenth of the run) is checked by
  peak instead. Not done: PCM-identical-with-`--diag` test, shared module
  refactor, `--update-baselines`.
- 2026-10-08 (cont.): Phase 8: `tools/perf.py` takes `--scenarios FILE
  --scenario NAME...` (`--seconds 0` = scenario length); `tools/perf_agg.py`
  aggregates. No-AOT config (`static-insns+follow`): 5.4-5.6% interpreted in all
  12 scenarios, timed 6.52 (trig1-taps) and 7.26 (sf-chr-depth-d127) CPU s per
  audio s; the FX add no new cold code except br (`FUN_b8258f`) and srr
  (`FUN_b826c8`) in the AOT core. Ranked list in finding 18.
- 2026-10-09: DN2 AOT core regenerated with FX-active profiles (`tools/aot_fxprof.py`;
  tf/sf *-127 scenarios merged into cov6/ent9/trans9). 1751 -> 2657 blocks. Default-config
  interpreted: br 1.24 -> 0.33%, srr 1.88 -> 0.24%, dry 0.25%. `mise run scenarios` pins
  unchanged, `mise run scenarios:check` 61 passed. Backup: `gen-pre-fxprof-20261009`. See
  finding 18. Dry-hold CPU not re-timed (needs an exclusive run).
- 2026-10-09 (cont.): timed exclusive A/B of the FX-profile core vs `gen-pre-fxprof-20261009`:
  br-127 2.04 -> 1.62, srr-127 1.94 -> 1.47 DSP CPU s per audio s; dry hold and trig1-taps
  +0.3-0.4% (20 pairs, noise level); PCM identical in all runs. Keep the larger core (finding 18).
- 2026-10-09: translator refusals batch 1 (finding 18, "Batch 1"): flat guarded access for
  unknown-range accesses and for roots whose static windows keep declining, plus a u32 read of
  sign-extended constant ranges (`native/sharc/src/fast`, opt-in `SHARC_FAST_FLATAUTO=1`,
  `+flat` config). No-AOT trig1-taps interpreted 5.48 -> 0.99%, timed 6.63 -> 5.00 CPU s per
  audio s, PCM `e0e6c8af7a84c443` unchanged; oracle suite 61 passed and `scenario --all` pins ok
  in that config (shared core); `cargo test` native/sharc 64 passed. Default stays off.
- 2026-10-09: batch 1 flat fallback is now the default (`cfg::flat_auto`; opt out with
  `SHARC_FAST_FLATAUTO=0`, fastcov `env:SHARC_FAST_FLATAUTO=0`). Default config (AOT + fast tier),
  same binary, exclusive lock, alternating pairs: trig1-taps median 1.393 (on) vs 1.395 (off) DSP
  CPU s per audio s (10 pairs), tf-br-127 1.433 vs 1.443 (10 pairs): no change beyond noise. No-AOT
  `static-insns+follow` trig1-taps 5.66 vs 7.32 (5 pairs, -23%). PCM identical in every run. Oracle
  suite 61 passed, `scenarios` pins ok, DT2 sample check clean, golden 6 passed, `cargo test --lib`
  48 passed (`region_bench` example does not compile: missing `flat_windows` in its `Env`).
- 2026-10-09: region-call overhead (finding 18, "Region-call overhead"). No-AOT trig1-taps: the 10.5 s
  headline (5.10) is 2.16 s compile + 2.94 s run time per audio s; 29% of the 18.66M calls/s were a
  dynamic-window guard that failed on every call (address = base + loaded modifier). Opt-in
  `SHARC_FAST_DYNFLAT=1` rebuilds such regions with flat accesses: calls 18.66M -> 13.21M, DSP CPU
  5.099 -> 4.599 (run time 2.942 -> 2.306), 3 exclusive pairs, PCM `e0e6c8af7a84c443` in all 18 runs;
  oracle suite 61 passed in the no-AOT config with the flag on, `cargo test --lib --tests` in
  native/sharc 64 passed. Default stays off. New `tools/perf_calls.py` (call sources and exit kinds);
  `tools/fastcov.py` `parse_run` now reads every exit of a root (`;` separated).
- 2026-10-09: compile offload design (`docs/design/fast-compile-offload.md`, finding 18 "Compile timeline and
  offload premises"). No-AOT trig1-taps 20 s: 19.8 s compile + 2.9 s build of 99.8 DSP s, 16.5 s of it in the
  first 0.1 audio s, worst call 64 ms; AOT default compiles 0.02 s. Switch point between interpreter and kernel
  proven PCM-neutral (`SHARC_FAST_DEFER`, 8 runs); machine code has no relocations and reloads from bytes (PCM
  equal). Nothing built beyond measurement hooks (`SHARC_FAST_TL`, `_DEFER`, `_RELOAD`, `_CL_OPT`,
  `tools/fast_timeline.py`, `tools/wasm_compile_bench.mjs`, `compile_par` example); default PCM pins unchanged.
  Next, if agreed: stage 1 native background compile.
- 2026-10-09: late-run slowdown found (finding 18 "Late-run slowdown", **[D]**). Cause: denormal filter/envelope
  state (the core does not flush denormals as the SHARC+ does) makes the fast tier's float-multiply result guard
  fail 2M times per audio s after the tail of a sound has decayed (`trig1-taps` from 9.2 s; `idle` and `trig1-hold`
  never; DT2 sample trig at 4 s); the firmware executes the same instructions. Opt-in `SHARC_FAST_DENORM=1`
  (a region built with STKYX.MUS set keeps denormal products in the kernel, `Req::StkyMus`): post-step run time
  no-AOT 4.9-5.1 -> 2.8, AOT 1.84 -> 1.41 (headline 14 s: -19% / -10%), 2 + 3 exclusive pairs; PCM equal in 12 timed
  runs, in 25 scenarios no-AOT and 25 AOT (off vs on; pins `trig1-taps` `e0e6c8af7a84c443` and `trig1-tap-release`
  `883476213933...` hold), DT2 sample trig equal. New unit test `fast_denormal_multiply_matches_the_interpreter`,
  `cargo test --lib` in native/sharc 65 passed. Default off; next: make it the default, then decide on flushing
  denormals in the core as the hardware does.
- 2026-10-09: `SHARC_FAST_DYNFLAT` on by default and the fast tier's Cranelift `opt_level` `none` (opt out
  `SHARC_FAST_DYNFLAT=0`, `SHARC_FAST_CL_OPT=speed`; fastcov `env:` form). Default AOT trig1-taps 1.476 vs 1.477 (8
  pairs, no change); no-AOT `static-insns+follow` 4.72 vs 5.89 (5 pairs, -20%). PCM `e0e6c8af7a84c443` throughout.
  `scenarios` pins ok, oracle suite 61 passed in the default and in the no-AOT config, `cargo test --lib --tests` in
  native/sharc 64 passed (needs the shared-core `SHARC_GEN_DIR`), native/sharc-fast-cl and `cargo check --examples`
  clean, golden 6 passed, DT2 sample check clean tone. Decisions: `docs/design/fast-compile-offload.md`.
