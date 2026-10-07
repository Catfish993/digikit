# Lane D notebook: DT2 coupled audio (2026-10-07)

Worktree reset to ac04ac5 first (my earlier diff was already inside it; a
patch of the pre-reset state is in the session scratchpad). Nothing committed.

## State

Works: DT2 ready state from the .syx alone, device profile, link port 0 sample
delivery (bit-exact), launcher per device, DT2 scenario, DN2 hashes unchanged.
Open: the DT2 render of the card sample is not a clean tone (details below).

## Files

- `devices/*.toml` + `native/device-profile`: `dsp = { image, idle_start,
  idle_end, idle_head, sample_link }` per firmware; `Registry::dsp_for(device,
  version)`. DT2 idle loop = DN2's code moved by -0x26dc (function hashes of
  0xb88a49/0xb88aa6 match 0xb8636d/0xb863ca): range 0xb8636d..0xb863df, head
  0xb863cf. Test `dsp_profiles_name_the_idle_loops_and_the_sample_link`.
- `native/coupled/src/lib.rs`: the DN2-only guard is gone; the profile decides
  (idle loop, sample link). `AudioOptions` unchanged. `coupled-run --card-image`.
- `native/boot/src/sharc_peer.rs`: `IdleLoop`, `DN2_IDLE`/`DT2_IDLE`,
  `open_engine_patched(.., idle)`; link port 0 delivery in `Core` (below);
  `Shared::{lp0_units, lp0_pending}`.
- `native/boot/src/card_file.rs`: `card_from_image` for hosts without the
  control crate. `native/boot/examples/{dsp_cold,sharc_live}.rs`: DSP_DEVICE=dt2,
  DSP_SAMPLE_LINK=lp0, CARD_IMAGE, SAVE_QUIET_FRAMES.
- `native/periph` (`dsp.rs`, `dspi.rs`, `spilink.rs`): the FlexBus byte latch
  and `Peer::link_bytes`.
- `native/sharc-rt/src/rt/periph.rs` + `tools/sharc_core/periph.py` (twin):
  LP0 DMA channel (0x30FFF000, SEC 81) in `DMA_BASES`; a CFG write with EN set
  and stop flow starts the work unit (RUN = transfer). `native/sharc/src/lib.rs`:
  `Engine::lp0_ready`, `Engine::lp0_transfer`. DN2 does not touch LP0.
  **Cost:** `tools/sharc_core/periph.py` is part of `core_hash`, so the Python
  tools now call the generated DN2 core "stale" (`SHARC_NATIVE_ALLOW_STALE=1`
  works); the Rust build does not check the hash.
- `tools/ready.py` (`mise run ready -- dn2|dt2 [--card-image IMG]`, replaces
  ready_dn2.py), `tools/digi_env.py` (`device_for_syx`, `firmware_for`,
  `ready_inputs(device=)`, `ready_dirs` in `tools/ready_inputs.json`),
  `tools/native_emu.sh` (per-device inputs; a DT2 card builds its ready state
  once, `ready_for`), `tools/dt2_sample_check.py`,
  `tools/sharc_frame_diff.py` (native vs Python reference over one frame).
- `native/coupled/scenarios-dt2.json`: `dt2-sample-trig` (+ `-noqa`, silent
  like DN2 without the NO prelude).

## DT2 start (step 1)

`tools/sharc_cold_state.py dt2-1.16`, then `dsp_cold` (DSP_DEVICE=dt2): rests in
the idle loop after 238M instructions (17 s). With the ColdFire cold boot and
that state attached at frame 0: no halt, ready at 1.0237G CF instructions with
the default card. Interpreter only (the generated core's AOT blocks are DN2's):
about 15x slower than real time during boot, 30-80 s of DSP CPU per audio
second while a voice plays. Real time needs a DT2 block library (the ~30 min
pipeline) or the fast tier.

## Link port 0 (step 3)

CF side: `dsp::Fifo` latches a byte on the falling edge of bit 7 of a halfword
write to 0x8C000002 (findings 04 "The wire"); `DmaLink` hands the bytes to
`Peer::link_bytes` before each DSPI2 exchange. `Core::deliver_lp0` (DT2 only,
`DspProfile.sample_link`): per frame up to 4 whole 0x401-word units
(4100 bytes): `Engine::lp0_transfer` (lands at the channel's ADDRSTART = the
buffer the firmware armed, completes DMA, SEC 81), then the DSP runs until the
firmware's own ISR and callback have re-armed the port (`lp0_ready`), counted in
that frame's instructions. A unit that finds the port unarmed waits.
Evidence (test-sine-1khz card): slot table at DM 0x257810 holds 297 slots
`{startL 0x40, startR 0x40, len 0x17700, rate 0xbb80, 0}` and the 48,000
samples at SDRAM 0x8422b9c8+0x40 equal the WAV with maximum error 0. The ready
snapshot is taken when the loader has been quiet for 400 frames
(`SAVE_QUIET_FRAMES`), because the sample loader keeps sending after the
panel is ready (153 of ~1300 units at ready).
`DIGI_LP0_LOG=1` prints each delivered unit (tag, first words).

## The render (step 4): open

Trig 1 with the card: voices 0 and 1 start, their buffers hold a clean sine
(the sample at -3 semitones = 840 Hz, position advance 26.9 input samples per
frame = 64 samples of 96 kHz internal rate x 0.42; the DSP's own decimator
produces the 32 output samples). The master output is not clean: 10 frames
after the onset the left channel gets a large extra component (block means
+-0.5, a 1.5 kHz ringing decaying over about 20 frames) while the right
channel decays smoothly. `tools/dt2_sample_check.py`: peak 843.75 Hz, peak
share 0.50, h3 -37.6 dB. What was ruled out:
- the sample data, the slot table, the voice buffers (voice 0 and 1 identical);
- engine disagreement: `tools/sharc_frame_diff.py` (native vs Python reference,
  250k instructions through a trig frame, state compared every 25k): identical;
- the number of SPORT blocks per frame (a 2-block / 96 kHz model was built and
  removed: the firmware decimates itself).
Trace so far (write watches, interpreter exact): master L/R words 0x25f180 /
0x25f200 are written by 0x1cb3d8 (a gain ramp; two calls L then R, SISD,
PEYEN off); its inputs, the bus words 0x254978 / 0x2549f8, differ from frame
697 (word 9) because the first accumulate (0x1cc4b7, the result of 0x1cc204 =
DN2 0xb809f2, the first-order filter whose coefficients finding 16/laneA
covered) already differs; the filter inputs (0x254894 / 0x2548f8) differ from
the onset frame. Both records' coefficients (0x252ae0 / 0x252b88) are equal.
Next: follow 0x254894 / 0x2548f8 upstream (what writes them, and why L and R
differ for a mono sample); the hypotheses are another lane-asymmetric SIMD
semantic, or a DT2 parameter (pitch -3 semitones is also unexplained).

## DN2 regression

DN2 `exactness-100m` b3f5230f.., `trig1-noqa-40m` 2861ce03.. and the
`coupled_ready_exactness` gate pass unchanged after the sharc-rt rebuild.

## Suggested findings text

**[V]** DT2 link port 0 sample delivery: ColdFire FlexBus bytes (falling edge of
bit 7, byte in bits 15:8 of 0x8C000002) become 0x401-word work units delivered
to the LP0 DMA channel (0x30FFF000, SEC 81); the firmware's ISR and callback
fill the slot table and SDRAM exactly (48,000-sample WAV, error 0).
**[V]** DT2 idle loop = DN2's, at -0x26dc. **[O]** DT2 render not clean: left
channel disturbance 10 frames after a trig; engines agree.

## Round 2

### (1) DT2 AOT core (done)

`mise run aot-gen -- dt2 --card-image IMG` (`tools/aot_gen.py`): interpreter
profile of a trig run from the card's ready snapshot (`sharc_live` with
DSP_PROFILE), `tools/sharc_rsgen.py dt2-1.16` with the DN2 model-safe flags
(1112 blocks, 11,336 instructions, 72 s), then the gen dir is entered in
`out/native/gen-index.json` under the SHA-256 of the packed DSP image
(`digi_env.register_gen` / `gen_for_image`). `digi_env.generated_dir(image=)`
resolves SHARC_GEN_DIR, then the registry, then the DN2 default;
`tools/native_emu.sh` passes the `--dsp-image` of the launch, so a DT2 launch
builds the player against the DT2 core in its own target dir
(`out/native/target*/g-<hash>`; never prod/pgo-use). The first build compiled
`sharc-gen` in 2 min 9 s. Tests: `tests/test_gen_index.py`.
dt2-sample-trig, 1.2 s, same snapshot: PCM sha256 537cff05.. both ways
(blocks exact); DSP CPU per audio second 3.74 (blocks) against 41.3
(interpreter), 11x. Still 3.7x real time: one trig profile only covers the
render path of that run; a second profile cycle (tools/sharc_dn2_aot.py style)
and DT2 fast regions are the next speed steps.

### (2) The left channel burst: localised, not fixed

Not a MODE1/SIMD complement case: all writers below run SISD (MODE1 PEYEN
clear) except the bus mix loop 0x1c2103 (SIMD, symmetric). Chain, each step
checked with SHARC_WWATCH + SHARC_PCDUMP (interpreter exact):
- master L/R (0x25f180/0x25f200) <- gain ramp 0x1cb3d8 <- bus planes
  0x254978/0x2549f8 <- DC filter 0x1cc204 <- bus 0x254878/0x2548f8, which the
  SIMD mix loop 0x1c20ef builds from the tracks (symmetric) and, at frame 697,
  the send loop 0x1c21b6 adds the reverb return plane 0x254778/0x2547f8:
  L 0.0222, R ~1e-7.
- that plane is written by the reverb/lattice loop 0x1c1f01 (state in
  0x25457c.. L / 0x2545fc.. R, written by 0xb82325 in 0xb822c0); the L state
  starts at 3.5e-7 in frame 688 and grows 3.2x per frame (an unstable
  feedback), the R state stays zero.
- seed: at 0xb8231c the multiplier operand F0 = 5.96e-6 (L) is a send gain /
  input scaling that is exactly 0 for R; the input plane of the reverb
  (0x254278) is written non-zero for L only (0xb80d29), R plane never.
  (An earlier comparison in this notebook that called the filter inputs
  asymmetric from the onset paired planes 0x254894/0x2548f8 wrongly: the planes
  are 0x254878 and 0x2548f8, 32 words apart; they are symmetric until 697.)
So the asymmetry is a per-channel parameter (the reverb send / its smoother)
that is nonzero for L only. Not yet found: which instruction produces 5.96e-6
in L and 0 in R. The value is not in any memory word of the ready state or the
end state (transient register value, probably 1 - x of the send smoother).
Next: PCDUMP the instructions that compute F0 at 0xb8231c (2-3 instructions
earlier in the call 0xb822c0, argument R4/R8/R12) for the L and R iterations.
No sharc_core change was made, so no regeneration or golden run was needed
for (2); `tests/test_sharc_golden.py` and the DN2 scenarios are unaffected
(DN2 hashes re-run after the LP0 change in round 1).
