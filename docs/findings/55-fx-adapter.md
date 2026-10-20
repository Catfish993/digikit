# 55. The FX adapter core/fx and the FX contract (P3d-1a, 2026-10-21)

`core/fx` runs the effects of the FX contract (`sdk/dk_fx.h`) on the final master block of both devices. This finding records where the hook sits and why,
the ring formats it was proven with, and what it costs. Marks: **[D]** measured or read once (by the author of this finding; no second agent has checked
the image bytes yet, so nothing here is **[V]**), **[O]** open. Pieces: `modules/core/fx/` (adapter, one DSP source for both devices),
`tests/fixtures/modules/fx-gain` and `fx-nan`, `tests/patchkit/test_fx.py`, `tests/modules/test_fx_conformance.py`, `tools/patchkit/pk_proof_fx.py`,
`tools/patchkit/fx_rings.py`. It answers the open question of finding 53 (is stage 8 the USB output).

## The hook: H1, the first instruction of the post-master function **[D]**

| | DT2 1.17 | DN2 1.12 |
| --- | --- | --- |
| function | `FUN_1c14e7` (`0x1c14e7`-`0x1c15e3`, 98 insns), reloc hash `06bffd96...d783` | `FUN_1c244d` (`0x1c244d`-`0x1c2712`, 268 insns), hash `0655a833...2974` |
| first instruction | `I7 = modify(I7, -4)`, 48 bits | `I7 = modify(I7, -0x88)`, 48 bits |
| callers | one: `0x1c30a0` in `FUN_1c2b24` | one: `0x1c307b` in `FUN_1c2712` |
| master block | `0x25f180` (left), `0x25f200` (right) | `0x268438` |
| order in the block | master mix `0x1c3099`, **H1 `0x1c30a0`**, ..., pack `0x1c28b5` at `0x1c3160`; the converter `0x1c74a1` is called at `0x1c7734` after `FUN_1c2b24` returns | **H1** is the first instruction of the function that also holds the pack (`fix_by` at `0x1c2689`); the converter `0x1c9d3f` is called at `0x1c9fd2` after `FUN_1c2712` returns |
| live at the entry | nothing is read; the body saves I2, I3, I5 | R4, R8, R12 (arguments), M3 (set by the caller from R7), R14 and R15 (the caller needs them back) |

- The anchor rules hold on 1.16 and 1.11 (the DSP blobs are byte-identical). The hook is `displaced = "run"`: patchkit puts the displaced `MODIFY(I7, ...)` in
  the hook's tail, which jumps to the second instruction. No DO loop encloses either call site, so the adapter may use `LCNTR` loops.
- The adapter saves every register in a static area (`_fx_save`), the context registers separately, pins the context as `core/dsp-voice` does and restores
  both. An empty chain saves only R0, I4 and L4.
- The pack takes the master pair as its first two channels: DT2 `0x1c315c` passes `R8 = DM(I6-17)` (the `R4 = 0x25f180` of `FUN_1c2b24`), DN2 `0x1c25e7`
  stores R8 and R8 + 0x80. So an effect at H1 is in the USB audio and in the DAC; the per-track outputs (track buses) are not touched.

## H0 is not in the USB audio **[D]**

The master hooks of `master-gain` and `master-drive` retarget the call of the Q31 converter (H0), which comes after the pack. Measured with
`digikit/master-gain` at 0.5 (gain word patched), same note, same frame, DT2 833 and DN2 1650: ring A (DAC) is half of stock, **ring C is bit-identical to
stock** (also its master pair). So a master effect at H0 reaches the DAC and not the USB main output. This is why `core/fx` hooks H1.

## The rings **[D]**

At a block boundary (`tools/patchkit/fx_rings.py` has addresses and decoders; the half just rendered is `(flag & 1) ^ 1`):

| | DT2 | DN2 |
| --- | --- | --- |
| master | `0x25f180` | `0x268438` |
| flag word | `0x25f780` | `0x268a38` |
| ring A (DAC), 2 halves of 256 bytes | `0x261cc8` | `0x2c0478` |
| ring C (USB), 2 halves of 2048 bytes | `0x262138` | `0x2c08e8` |

- Ring A: 32 frames of (left, right) words, each `-round(sample * 2^31)`.
- Ring C: 32 frames of 16 words. Words 0 and 1 are the master left and right, `fix(f32(sample * 1.428571) * 2^31)` (factor `0x3fb6db6e`, the literal in the
  pack loop); words 2 and 3 are the next channel pair, words 4 to 15 were 0 in the note used. The ring is not a packed 24-bit stream.
- Both formats agree with the master block within 1 LSB on both devices (an exact float32 model of the conversion). DN2 addresses were read from the block
  handler `0x1c9e76` (ring A `0x2c0478 + (flag << 8)`, ring C `0x2c08e8 + (flag << 11)`, the same offsets as DT2) and confirmed by the readings.

## Proof **[D]** (`tools/patchkit/pk_proof_fx.py`, DT2 1.17 and DN2 1.12, 79 s and 30 s wall)

- Empty chain (image with `core/fx` alone): PCM bit-identical to stock (38,528 active samples on DT2, 61,814 on DN2); flag, master, ring A and ring C
  bit-identical at the snapshot frame.
- `test/fx-gain` (0.5): master exactly 0.5 x stock; ring A and the ring C master pair equal the conversions of that master (largest error 0.00 LSB on DT2, 0.48
  on DN2); both are half of stock (within 2); ring C words 2 to 15 bit-identical to stock; PCM -6.0206 dB, largest error 0.5 ulp.
- Gain word set to 1.0, and the row's `enabled` word cleared in mid-run (`dsp.patch`): master and both rings equal stock again after the fade.

## Costs **[D]**

- DSP instructions per block on the concrete core (`tests/patchkit/test_fx.py`, `tests/support/fx_rig.py`): empty chain 16; a chain whose rows are all off 174;
  one effect steady 732 (the 0.5 test effect itself about 210; copy, output check and bookkeeping about 520); a fade block 1065. In the smoke cells the DSP
  budget line shows +18 and +730 busy instructions per block (DT2 1.17), equal to the rig. The conformance suite measured 1072 as the worst block of `fx-gain`.
- **Emulator CPU of the hook (AOT barred) [D]**: patching a function bars the generated (AOT) regions of the whole hashed code range it lies in
  (`verify_code`, `packages/emulator/sharc/src/lib.rs`; ranges `CODE_RANGES` of the generated core). Paired A/B with `tools/devtools/coupled_ab.py` (same binary, image
  B patched through `--dsp-patch` written by `tools/patchkit/pk_dsp_diff.py`; 5 pairs, machine load 12-21 so only the paired ratio counts; PCM hash equal in A and B):

  | | scenario | DSP CPU per audio s, stock | with empty `core/fx` | ratio | instructions moved from AOT to the interpreter |
  | --- | --- | --- | --- | --- | --- |
  | DT2 | `dt2-sample-trig` (3 s, one-sample card) | 1.295 | 1.901 | 1.467 (1.456-1.472) | about 4,250 per block |
  | DN2 | `trig1-taps` (10.5 s) | 1.356 | 4.193 | **3.091** (3.075-3.113) | about 22,200 per block |

  The DN2 range that holds `0x1c244d` is 3482 words and covers `FUN_1c244d` and the orchestrator `FUN_1c2712`; the DT2 range at `0x1c149b` is 334 words. The cost is
  emulator time only; the firmware gets +18 instructions. **[O]** Ways to cut it: a hook site in a smaller range (DN2: the end of the master function `0x1c1f1e`, range
  `0x1c1b40` with 1221 words, still before the pack), or finer `CODE_RANGES` in the generator (splits at function entries). Not tried.

## Open **[O]**

- The role of ring C words 2 and 3 (next pair) and of the rest of the frame.
- Whether the AOT cost above is acceptable for the desktop and browser targets, and which of the two ways to cut it is taken.
- A second agent checks the H1 table against the image bytes before the marks become [V].
