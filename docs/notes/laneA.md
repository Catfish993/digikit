# Lane A notebook: DSP patch path and .syx-only DSP start (2026-10-07)

State: Part 1 built and verified; Part 2 answered (cold DSP start works up to
one init-computed word). Nothing committed. Scratch inputs/outputs (firmware
derived) are in the session scratchpad, not in the repo.

## Files

- `native/boot/src/dsp_patch.rs` (new): patch format and parser, `BASE` check.
- `native/boot/src/sharc_peer.rs`: `apply_dsp_patch`, `open_dn2_engine_patched`
  (`open_dn2_engine` delegates with `None`). Refuses a patch while
  `SHARC_FAST_REGIONS` is set (the fast tier has no code-identity check yet;
  `DSP_PATCH_ALLOW_FAST=1` overrides).
- `native/boot/src/lib.rs`: `pub mod dsp_patch`.
- `native/coupled/src/lib.rs`: `AudioOptions.dsp_patch`; `CoupledRuntime::new`
  checks BASE against SHA-256 of the firmware; the DSP worker opens the patched
  engine after the state import.
- `native/coupled/src/bin/coupled_run.rs`, `packages/desktop/src-tauri/src/main.rs`:
  `--dsp-patch FILE`.
- `tools/dsp_patch.py` (new): `check` / `show` / `apply` on the packed image.
- `tools/sharc_cold_state.py` (new): cold-start canonical state from section 7.
- `native/boot/examples/dsp_cold.rs` (+ `[[example]]` in `native/boot/Cargo.toml`):
  run a cold state with no ColdFire to the idle range, write a DT2DSP01 sidecar.
- `tests/test_lint.py`: the two tools join CLEAN.

## Patch format

```
# comment (also ; and //)
BASE <sha256 of the unpatched .syx>
PM <pc> <hex> [was <hex>]    4/8/12 hex digits = 16/32/48-bit instruction at short-word PC
SW <pc> <hex16>...           raw short words
DM <addr> <hex32> [was <hex32>]   DM byte address (engine address, 0x28000000 alias ok)
BYTES <addr> <hex>
```

A short word is at byte `0x28000000 + 2*pc` (LE); the first instruction word
is the most significant (`R2 = 0x1f` at sw 0x1c9d51 is `0f82 001f`). `was` is
a guard checked against live memory before the write. A patched .syx is not
written: `dt2/elz.py` only depacks, and the section 7 stream is ELZ-packed with
checksums; the patch file plus the base .syx is the input. Patch files hold
firmware words: keep them under `out/`, do not commit.

Why the overlay: a `.snap.dsp` carries its own copy of code and data (the
whole overlay is exported as ranges), so patching the packed image alone does
not change a snapshot run. The overlay is applied after the import and
`concretise_import`, with the decode cache invalidated.

## Part 1 evidence (DN2 1.11, trig1-tap-release, `--seconds 3`, `SHARC_FAST_REGIONS=`)

Firmware sha256 `2af43e65...72e0` (Digitone_II_OS1.11.syx).

| run | PCM sha256 (prefix) | rms | peak | wall |
|---|---|---|---|---|
| no patch | a0a0f42625dcb87e | 0.2096 | 0.6930 | 5.5 s |
| no-op patch (`PM 1c9d51 0f82001f was 0f82001f`) | a0a0f42625dcb87e | 0.2096 | 0.6930 | 5.5 s |
| `PM 1c9d51 0f82001e was 0f82001f` (R2 = 0x1f -> 0x1e) | 6a7f5cecc41c9385 | 0.1048 | 0.3465 | 164 s |

0x1c9d51 is the scale in the master L/R to Q31 conversion loop of 0x1c9d3f
(findings 07 ~4893); `fix_by` by 30 instead of 31 halves the output: rms and
peak are exactly 0.5x. Also checked: a wrong BASE is refused at start; a
patch with fast regions set is refused; `tools/dsp_patch.py apply` changes one
byte of the packed image (0x1f -> 0x1e at file offset 951229); parser unit
test `cargo test -p elektron-native-boot --lib dsp_patch` passes. The
`expect_sha256` of the scenario does not apply to `--seconds 3` (gates fail
with exit 2 in all three runs; the hashes above are the evidence).

**Cost to note:** one changed code word makes `verify_code` fail, which turns
off every AOT block (`aot_block 0`): 30x slower (164 s for 3 s of audio). A
data (`DM`) patch keeps the blocks. Per-range invalidation or the fast tier
(lane B) is the fix.

Commands (from the worktree, binary from `uv run python tools/coupled.py build`):

```
SHARC_FAST_REGIONS= coupled-run --syx Digitone_II_OS1.11.syx \
  --dsp-image snapshots/dn2-audio-ready-2026-10-06/digi-audio-dn2-image.bin \
  --dsp-state snapshots/dn2-audio-ready-2026-10-06/digi-audio-m5.snap.dsp \
  --cf-snapshot snapshots/dn2-audio-ready-2026-10-06/digi-audio-m5.snap \
  --scenarios native/coupled/scenarios.json --scenario trig1-tap-release \
  --seconds 3 --wav OUT.wav --dsp-patch PATCH
uv run python tools/dsp_patch.py show IMAGE.bin PATCH --syx Digitone_II_OS1.11.syx
```

Note: `out/` in a worktree is a symlink to the main tree's `out/`; write
scratch files elsewhere. `sections/.source-sha256` is DT2 1.16 (278541e4...);
coupled-run takes the DN2 .syx directly and does not use `sections/`.

## Part 2: DSP start from the .syx alone

Steps and results (DN2 1.11):

1. `tools/sharc_cold_state.py dn2-1.11 cold.state` (entry sw 0x1c12e2, HWR
   reset table, bank/stack/software-interrupt/core-timer models): 48 KB blob.
   `tools/sharc_pack_image.py dn2-1.11` is byte-identical to
   `digi-audio-dn2-image.bin`, so the image already comes from the .syx alone.
2. `dsp_cold` (native, no ColdFire, AOT off, 61 s) reaches the DN2 idle range
   at 563.75M instructions with no halt (pc 0xb88a5d; the earlier Python
   cold run saw 573.6M, that count included more steps after idle).
3. Attaching it at CF frame 0 does not work as is: with the raw cold state the
   first `spi2_exchange` traps ("peripheral model", dsp_instr=0) because the DSP
   has not run its own boot yet (CF frames start at CF instruction ~450M).
   Fix: run the cold boot first (step 2) and attach that state, as the fresh
   state was.
4. With the cold-idle state (clock base 563750000), `sharc_live` CF cold boot,
   trig, ready+100M: ready at CF instruction 851,236,444 (same as the ready
   snapshot), no halt, 5867 non-zero replies, 3.91 s of audio, DSP instructions
   identical to the reference run (3,913,335,290). Reference = same run with
   `digi-audio-loop2-fresh.bin` (base 573627620).
5. Audio vs reference: the 173,574 frames before the tap are bit-identical;
   the 14,267 frames of the tap differ: peak 0.0562 vs 0.6930 (12.3x
   quieter), same onset and length. PCM sha256 cold b3f5230f..., ref 3b600f6d....
6. State diff (cold-idle vs fresh): 6 MMRs (EMUCLK x2 and four registers
   0x31023030/b0, 0x3102d230/2b0 = 0 vs 7), ~337k differing words in
   L2 0x2002f0d0..0x200779f8 (values -1/-2/0, noise floor), DDR zero fill that
   the fresh state lacks, and a few scattered words.
7. Bisection (hybrid states, each a 100M-instruction run of about 10 s):
   cold registers + fresh memory is bit-identical to the reference PCM;
   fresh registers + cold memory is the quiet cold PCM. Delta-debugging over
   137 memory runs ends at ONE word: DM 0x24f378 (byte alias 0x2824f378):
   cold 0xbd7fd522 (-0.0623), fresh 0x80000000 (-0.0). With that single word
   from the fresh state the cold-idle state reproduces the reference audio
   bit for bit.
8. Writer of that word (write-watch, interpreter steps): icount 325,662,5xx,
   `DM(I4 + 22) = R2` at 0xb80b70 stores `fneg(F0)` = 0xbf7fd522, then
   0x1c8cf7 `DM(I2 + 21) = F2*0.0625`. F0 comes from the float division
   routine 0x1c06ba (uses `recips`). Our cold run has F0 = 0.9990; the fresh
   state implies F0 = 0.0. The cold run needs approximate `recips` seeds (the
   engine halts at 0x1c0743 with them off), so `recips` seed modelling is the
   prime suspect; the unmodelled-hardware alternative is an init input
   (a value measured or read at boot). **[O]** Either way the blocker is NOT
   the boot ROM handoff, CGU/PLL, DDR alias or interrupt delivery: the flat
   image runs its own init to idle and serves the CF boot.

Verdict: yes with one caveat. `.syx` alone (image + cold state + 61 s native
pre-run) gives a working, bit-for-bit same-timing DSP; the audio level matches
the reference after correcting one init-computed word (DM 0x24f378). Which of
the two values is the hardware's is open; the fresh state's `-0.0` yields the
audio used so far (peak 0.693).

Commands:

```
uv run python tools/sharc_cold_state.py dn2-1.11 cold.state
SHARC_FAST_REGIONS= dsp_cold IMAGE.bin cold.state cold-idle.dsp 800000000
tail -c +25 cold-idle.dsp > cold-idle.state          # sharc_live takes the raw blob
DSP_CLOCK_BASE=563750000 NOTE_EVENTS=trig CF_DIGEST=1 CF_SNAPSHOT=new.snap \
  sharc_live Digitone_II_OS1.11.syx cold-idle.state IMAGE.bin out.wav 100000000
```

Next steps: (a) find F0 at 0xb80b53's call: dump the inputs of 0x1c06ba on the
cold run and decide which result is right (the `recips` table is public in the
SHARC+ manuals); (b) if it is a seed, model it and re-run (the cold PCM should
become the reference); (c) bake the cold boot into a cached `.dsp` keyed by
image sha so a `.syx` start costs one 61 s run (AOT on, expected much less);
(d) DT2 needs the same entry (sharc_cold_state.py takes `dt2-1.16`) but not
the audio path (LP0, DN2-only guard).

## Suggested findings text (07-emulator.md)

**[V]** DSP patch overlay. `coupled-run --dsp-patch FILE` (also the desktop
launcher) overlays word-level edits on the imported DSP state after the
snapshot import: a `.snap.dsp` holds its own copy of code and data, so
patching the image would not change it. Format and tool: native/boot
`dsp_patch.rs`, `tools/dsp_patch.py`. A no-op patch gives the identical PCM
hash; `R2 = 0x1f -> 0x1e` at sw 0x1c9d51 (the master Q31 scale) halves rms
and peak exactly. Any changed code word turns off all AOT blocks (30x
slower); a fast-tier code check is needed first (the patch is refused with
`SHARC_FAST_REGIONS` set).

**[V]** DSP start from the .syx alone (DN2 1.11). The packed image equals the
snapshot image byte for byte; a cold state from `tools/sharc_cold_state.py`
run natively for 563.75M instructions (61 s) rests in the DN2 idle range, and
attached to a ColdFire cold boot it gives the same frame timing, DSP
instruction counts and silent frames as the reference; the tap frames differ
(peak 0.056 vs 0.693) because of one init-computed word, DM 0x24f378
(-0.0623 cold vs -0.0 in the fresh state), written at DSP instruction ~325.7M
from the `recips`-based division at 0x1c06ba. With that word replaced the
audio is bit-identical. **[O]** which value the hardware computes.

## Follow-up: why DM 0x24f378 differs (2026-10-07)

Trace (interpreter-exact, `SHARC_PCDUMP`/`SHARC_WWATCH` on `dsp_cold`, icount
325.66M, helper `scratchpad/dp/pcd.sh`):
- Caller 0x1c8cce -> 0xb80b4b. Divisor R12 = DM(I5) at 0x241298 = 48000.0
  (0x473b8000), stored at icount 11539 by 0x1c8e79 from an immediate
  (`I12 = 0x473b8000`). Dividend R4 = 0x41fb53d2 = 31.4159 (immediate).
  No MMR or uninitialised read is involved.
- Division 1 (0x1c06ba): 31.4159/48000 -> 6.544985e-4 (true 6.54498e-4).
  Division 2: 1/(1+6.545e-4) -> 0.9993459 (true 0.99934592). So F0 = 0.99935,
  DM(I4+22) = -F0 (0xbf7fd522), then x0.0625 at 0x1c8cf7 = 0xbd7fd522. The cold
  value is exact math; RECIPS and its operands are not the divergence.
- No later write: the word stays 0xbd7fd522 in the cold run to ready and
  stays 0x80000000 in the reference run (and in loop2-fresh, m5.snap.dsp).
- The value 0x80000000 = -0.0 needs F0 = +0.0, i.e. a quotient of 0, which no
  input here gives. So `loop2-fresh.bin` was produced by a different
  execution (Python `armed_start`/run_init path, "cannot be rebuilt") that
  computed it wrongly or skipped/replaced this init. **[O]** exact cause is
  not recoverable; the cold value is the mathematically right one.

RECIPS/RSQRTS: the PRM pp.19-16..19-18 give flags and special cases only; the
seed ROM table is not in any manual under out/refs (checked PRM, PGR, 2106x
manual, 21160). Our model keeps the special cases exact and truncates the true
reciprocal to 8 bits (rsqrts 4). Newton steps in 0x1c06ba make the quotient
independent of the seed to ~1 ulp, so the seed cannot explain 0 vs 0.999.
`tests/test_sharc_recips.py` (5 tests) pins the documented values.

Halt at 0x1c0743 with approximate recips off: `recips` then returns an
Unknown seed with unknown AV/AZ-type flags, and the next conditional SIMD
Type2a at 0x1c0743 needs a known predicate, so `_type_2a` stops ("unknown
conditional SIMD Type2a predicate"). It is a modelling stop, not a bug in the
firmware path.

Consequence: the .syx-only cold start is arguably the more correct state. No
engine change was made; goldens untouched. Decision for Em: keep fresh as the
audio reference or switch to the cold state (tap peak 0.056 instead of 0.693).

## Provenance of DM 0x24f378 (2026-10-07, second follow-up)

Resolved: the word is S2-lane data of a SIMD pair, and the fresh state predates
the engine's complementary-register handling.

- The code at 0x1c8cf1..0x1c8cf9 runs with PEYEN set (0x1c8cf9 is
  `MODE1 = clear(MODE1, 0x200000)`): `R2 = DM(I2+21)` loads the pair
  (R2 = c21 at 0x24f374, S2 = c22 at 0x24f378), `R1 = 0x3d800000` broadcasts
  0.0625, `F2 = fmul(F2,F1)` scales both lanes, `DM(I2+21) = R2` stores the
  pair. pcdump R2 before the store is +a/16 (0x3d7fd522); the write watch
  sees -a/16 land at +22 (the S2 lane). So +22 is written by the SIMD
  complement.
- Fresh vs cold: +20, +21 (0x3d7fd522), +23 are identical; only +22 differs:
  cold 0xbd7fd522 = -a/16, fresh 0x80000000 = -0.0. The record is a first-order
  DC blocker: c21 = +a, c22 = -a (a = 1/(1+2*pi*5/48000) = 0.99935), both
  scaled by 1/16 by the SIMD instruction. The reader is 0xb809f2:
  y = c21*x + c22*x1 - c23*y1 with floats (not Q31); state at +24/+25.
  Fresh breaks c22 = -c21, so DC passes through the filter: a steady-state gain
  difference, not a ramp. Per-window rms ratio ref/cold is flat to slowly
  rising 16.7-20.2 over the 14,267-frame tap (no settling transient).
- Recipe: the fresh state is from the 2026-10-02/03 native chain
  (rti -> irq -> loop -> loop2; `/private/tmp/digi-audio-loop2/states.pkl` holds
  only save/restore dicts, the script is not on disk). Its clock base 573,627,620
  is the Python cold run's idle count (findings 07 ~6063); our native cold run
  rests 12M instructions earlier (563.75M), so the old engine took a different
  path. The engine fix "Write the complementary register for Type 17 loads in
  SIMD mode" (ed02999) came after. Not skipped: 0x1c8cce/0xb80b4b ran in both
  (identical neighbours), RECIPS identical (same quotient).
  tools/sharc_harness.run_init is DT2-only (profile device="dt2") and was not
  used for DN2.
- DT2 1.16: the reader 0xb809f2 matches DT2 0x1cc204 exactly (exact_hash), but
  neither the init 0xb80b4b nor 0x1c06ba hash-match, and DT2 has no literal
  0x41fb53d2 (2*pi*5) or 0x3d026136: this 5 Hz init is DN2-specific.
- Verdict: hardware has -a/16 = 0xbd7fd522 (the cold value): the document's own
  SIMD semantics, the symmetric DC-blocker pair, and the old engine's missing
  S2 handling all agree. The 0.693 peak of the old reference included a broken
  filter; the cold-start PCM (tap peak 0.056) should become the reference.
  The level (0.056) is plausible only if the master level at default settings is
  low; no hardware capture exists to confirm **[O]**.

## Cold-start reference state (2026-10-07)

Em decided the DN2 reference is the cold-start state. Lane 0's board patch
applied (scratchpad lane0.patch).

- `tools/ready_dn2.py` / `mise run ready-dn2`: packs the image, builds the cold
  state, runs `dsp_cold` (~60 s, DSP alone to idle), then `sharc_live` boots the
  CF with that DSP state attached and saves `digi-audio-m5.snap(.dsp)` at ready
  (ready at CF instruction 851,236,444). Output
  `snapshots/dn2-audio-ready-cold-2026-10-07/` (git-ignored; SOURCE.json has the
  .syx sha256). `tools/ready_inputs.json` points there. Old dirs untouched.
- Sine check (trig1-tap-release, 8.5 s, L==R): tap 0.462-1.201 s; fundamental
  261.5-261.6 Hz (C4); rms 0.0327 at +0.1 s, peak 0.0562 overall; harmonics
  h2 -107 dB, h3 -126 dB (+0.1 s), h2 -81.5 dB at +0.2 s, all others < -93 dB;
  window mean (DC) 1.7e-4 .. -6e-5 (0.5% of rms, window not cycle-aligned),
  no offset. A clean sine with a decaying envelope.
- New pins (default fast config; no-fast render gives the same sha for
  exactness): exactness-100m b3f5230f..4d03f, trig1-noqa-40m 2861ce03..c6f21,
  trig1-taps d60ad339..6d9d3, trig1-tap-release 88347621..819cdd, gate CF digest
  55dc699e..e4e0, DSP export 4dc48019..e389b (`coupled_ready_exactness` passes
  via the new `digi-coupled` ignored test `gate_tests::coupled_ready_exactness`;
  needs NOTE_EVENTS=trig).
- Updated: native/coupled/scenarios.json, native/coupled/src/lib.rs (gate pins
  and comment), native/README.md, docs/design/emulator-control.md, findings 14
  and 16 ([C] notes), coupled_run.rs doc paths. Left: tools/sharc_regionbench.py
  (speed bench from the old fresh state, no PCM pin),
  tests/test_native_emu_launcher.py (tmp fixtures), tests/test_sharc_golden.py
  (DT2 1.16, state-independent). tools/coupled.py `--wav` fails when the run dir
  does not exist (pre-existing). Preexisting failures not from this work:
  test_native_emu_launcher target-dir test (worktree target path) and
  tests/test_lint.py format (tools/native_emu.sh:331).
