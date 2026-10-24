# 55. The FX adapter core/fx and the FX contract (P3d-1a, 2026-10-21)

`core/fx` runs the effects of the FX contract (`sdk/dk_fx.h`) on the final master block of both devices. This finding records where the hook sits and why,
the ring formats it was proven with, and what it costs. Marks: **[D]** measured or read once (by the author of this finding; no second agent has checked
the image bytes yet, so nothing here is **[V]**), **[O]** open. Pieces: `modules/core/fx/` (adapter, one DSP source for both devices),
`tests/fixtures/modules/fx-gain` and `fx-nan`, `tests/patchkit/test_fx.py`, `tests/modules/test_fx_conformance.py`, `tools/patchkit/pk_proof_fx.py`,
`tools/patchkit/fx_rings.py`. It answers the open question of finding 53 (is stage 8 the USB output).

## The hook sites **[D]**

Both devices run the post-master work in functions that the block orchestrator calls once per block, in this order (DT2 `FUN_1c2b24`, DN2 `FUN_1c2712`):

| | DT2 1.17 | DN2 1.12 |
| --- | --- | --- |
| master mix | `FUN_1c207b`, call `0x1c3099` | `FUN_1c1f1e`, call `0x1c305d` |
| after it | H1 `FUN_1c14e7` (`0x1c30a0`), `0x1c3429`, `0x1c367e`, **`FUN_1c80f2` (`0x1c311f`)**, 16 x `0x1c149b`, `0x1c29fd`, pack `FUN_1c28b5` (`0x1c3160`) | **H1 `FUN_1c244d` (`0x1c307b`), which also holds the pack** (`fix_by` at `0x1c2689`) |
| converter (ring A) | `0x1c74a1`, called at `0x1c7734` after `FUN_1c2b24` returns | `0x1c9d3f`, called at `0x1c9fd2` after `FUN_1c2712` returns |
| master block | `0x25f180` (left), `0x25f200` (right) | `0x268438` |

- Chosen hook: DT2 `FUN_1c80f2` (reloc hash `7a928b18...69df`, first instruction `I7 = modify(I7, -4)`, 48 bits, one caller, a block copy that takes the master pointer);
  DN2 `FUN_1c244d` (hash `0655a833...2974`, first instruction `I7 = modify(I7, -0x88)`, one caller). Both run after the master mix and before the pack and the converter.
  The anchor rules hold on 1.16 and 1.11 (the DSP blobs are byte-identical). The hook is `displaced = "run"`: patchkit puts the displaced `MODIFY(I7, ...)` in the hook's
  tail, which jumps to the second instruction. No DO loop encloses either call site.
- Live at the entry of DN2 `FUN_1c244d`: R4, R8, R12 (arguments), M3 (set by the caller from R7), R14 and R15 (the caller needs them back); the DT2 functions read
  their arguments from R4, R8, R12 too. The adapter saves every register in a static area (`_fx_save`), the context registers separately, pins the context as
  `core/dsp-voice` does and restores both. An empty chain saves only R0, I4 and L4.
- The pack takes the master pair as its first two channels: DT2 `0x1c315c` passes `R8 = DM(I6-17)` (the `R4 = 0x25f180` of `FUN_1c2b24`), DN2 `0x1c25e7`
  stores R8 and R8 + 0x80. So an effect before the pack is in the USB audio and in the DAC; the per-track outputs (track buses) are not touched. The functions
  between the DT2 master and `FUN_1c80f2` (H1 scales the track buses; `0x1c3429`, `0x1c367e`) leave the master as the master function wrote it (proof below).
- **Why not H1 on DT2**: the site decides what the emulator's AOT core loses (next section). H1 (`FUN_1c14e7`) was the first choice and costs x1.47; `FUN_1c80f2` costs x1.03.

## H0 is not in the USB audio **[D]**

The master hooks of `master-gain` and `master-drive` retarget the call of the Q31 converter (H0), which comes after the pack. Measured with
`digikit/master-gain` at 0.5 (gain word patched), same note, same frame, DT2 833 and DN2 1650: ring A (DAC) is half of stock, **ring C is bit-identical to
stock** (also its master pair). So a master effect at H0 reaches the DAC and not the USB main output. This is why `core/fx` hooks before the pack.

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

## Proof **[D]** (`tools/patchkit/pk_proof_fx.py`, DT2 1.17 and DN2 1.12; 44 s and 56 s wall with the hook on `FUN_1c80f2`)

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
  (`verify_code`, `packages/emulator/sharc/src/lib.rs`; `CODE_RANGES` of the generated core). So the cost depends on the range of the site, not on the adapter (+18 instructions).
  Paired A/B with `tools/devtools/coupled_ab.py` (same binary, image B patched through `--dsp-patch` written by `tools/patchkit/pk_dsp_diff.py`; machine load 12-22, so only
  the paired ratio counts; PCM hash equal in A and B; the sites other than the chosen ones are the same adapter with the hook moved by hand, 3 pairs):

  | device | site (range in words) | DSP CPU ratio |
  | --- | --- | --- |
  | DT2 | **`FUN_1c80f2` (25)**, chosen (5 pairs, 1.250 -> 1.294 per audio s, 1.017-1.035) | **1.03** |
  | DT2 | `FUN_1c367e` (133) | 1.09 |
  | DT2 | H1 `FUN_1c14e7` (334) | 1.47 |
  | DT2 | master mix `FUN_1c207b` (1049; before the master is final) | 1.32 |
  | DT2 | pack `FUN_1c28b5` (1510) | 2.94 |
  | DN2 | **H1 `FUN_1c244d` (3482, with the orchestrator)**, chosen (5 pairs, 1.356 -> 4.193, 3.075-3.113; about 22,200 instructions per block fall to the interpreter) | **3.09** |
  | DN2 | master function `FUN_1c1f1e` (1221; entry, so not a usable site) | 1.62 |
  | DN2 | converter entry `0x1c9d3f` (299; H0, after the pack) | 1.13 |

  On DN2 no site between the master and the pack is in a smaller range: the pack is inside H1's function, the master function's end is a run of 32-bit instructions (no 48-bit
  site) and the stages before the ramp are not the final master. **[O]** Ways to cut the DN2 cost: finer `CODE_RANGES` in the generator (splits at function entries), or the
  effect at H0 (x1.13) with the adapter re-converting the master pair of ring C itself (the format is exact, see above; not tried).

## Run-time rows (F4 step 1, 2026-10-24) **[D]**

Effects can also be loaded at run time on the Digitakt II (`docs/design/digikit-dkm.md`, DSP section version 4; runtime 0.4.0). What changed in the adapter and its neighbours:

- **Rows are 24 words** (was 16): pc, desc, state, enabled, p0 to p7 (`DK_FX_MAX_PARAMS` 8, was 4), `was` 12, `dis` 14, fault window 15 to 17. Row stride 96 bytes.
- **A free row has pc `0xffffffff`** and is skipped; pc 0 still ends the table. `core/dynfx` (Digitakt II only for now) reserves one free row for each `[limits] fx_slots` (3) and a table, `dynfx_info`, that the
  runtime reads. The registry `fx` takes the free rows and the rows of patched-in effects together (limit `fx_slots`).
- **Window 2** of `core/dsp-link` is the whole table of the registry `fx` (24 words a row, `fx.rows`, `fx.count`); the runtime writes one word per transfer and the pc last, as it does for voice rows.
- **Cost** (concrete core, `tests/patchkit/test_fx.py`): empty chain 16 instructions (no row; unchanged), 21 with one free row, 31 with three free rows, 33 with four (the quick scan costs about 5 instructions per
  free row); one effect steady 736 (was 732), a chain whose rows are all off 178 (was 174). The Digitakt II images that carry `core/dynfx` (the `device` and `emulator` patch sets) therefore always pay the hook: x1.03 DSP CPU in the
  emulator (the table above) and about 31 instructions per block on the DSP (0.005 % of a 666,667-cycle block at the emulator's model clock).
- State of a run-time effect lives in the module's own DSP range (behind its image), zeroed by the adapter when the effect switches on.
- Proof: `tests/digikit/scenarios/fx-dyn-dt2.toml` (`tools/patchkit/pk_digikit_fx.py`): the sine and `test/dyn-fx-gain` selected at boot play as 0.5 x the dry sine (within 2 ulps, from the onset on); the chain's row
  holds the record's words; MODULES, CAPACITY (`FX_SLOTS` used 1 of 3) and `dkneeds` agree; deselect and restart gives the dry PCM bit for bit; three effects play as 0.125 x dry and the fourth is refused (`E_DSP_ROWS`
  and the fit rule: `FX 1 > 0 FREE`).

## MIX and the effect pages (F4 step 2, 2026-10-24) **[D]**

- **Row word 13 is the mix** (a float, default 1.0 through the registry field): `master = dry + mix * (wet - dry)` per sample, the fade weights multiplied by the mix; mix 1.0 is the plain copy and mix 0 leaves
  the master alone, both bit for bit. Cost (concrete core): one effect steady 743 instructions per block at mix 1.0 (736 before), 1142 at another mix, 605 at mix 0 (the copy is skipped); the fades 1142.
  The empty chain (16), three free rows (31) and the all-off chain (178) are unchanged.
- The chain's row is published with its mix word (13 steps, was 12). The effect's page, its knobs and the poller that writes p_j and the mix: finding 61.

## Run-time effects on the Digitone II: the emulator cost (F4 step 5, 2026-10-24) **[D]**

`core/dynfx` now builds for the Digitone II 1.12 (`[limits] fx_slots` 3 as on the Digitakt II; the registry `fx`, window 2 of `core/dsp-link` and the hook H1 were already there). It needs `core/fx`, which
no DN2 patch set carried until now (`emulator` and `device` are `core/dynreg`, `core/dyndsp`, `core/digikit-menu`, `core/health`, `core/input`, `core/remote-open`), so adding it to a set puts the hook of finding 55
(H1 `FUN_1c244d`, range 3482 words) into the emulator image. Paired A/B on the DN2 emulator set (`tools/devtools/coupled_ab.py`, scenario `trig1-taps`, 10.5 audio s, 3 pairs, same binary, images through
`--dsp-patch` from `pk_dsp_diff`; DSP thread CPU per audio second; no other emulator running):

| image | DSP CPU per audio s | ratio | real-time factor | instructions to the interpreter per block |
| --- | --- | --- | --- | --- |
| A: the `emulator` set | 1.687 (IQR 0.007) | 1 | 0.59 | 7,470 |
| B: A + `core/dynfx` (+ `core/fx`), no effect loaded | 4.028 (IQR 0.049) | **2.38** (2.36 to 2.39) | 0.247 | 29,750 (+22,300 of the fall-back) |
| C: A + `core/fx` + `digikit/freqshift` as a patched-in effect (SHIFT 10, MIX 1; proxy for a loaded one) | 4.571 (IQR 0.015) | **2.71** (2.70 to 2.72) | 0.219 | 34,350 |

The PCM of B is the PCM of A (hash equal in all runs); C differs (the effect is on). The headless binary is not PGO-tuned, so the absolute factors are low; the ratio is what carries over. The DT2 image pays x1.03 for the same
step; on the DN2 the hook lands in the one large range. Options (not tried): finer `CODE_RANGES` (no merge of adjacent blocks at function entries) so that H1 and its caller do not share one range; the effect at H0
(x1.13, the range of the converter) with the adapter converting the master pair of ring C itself; or leaving `core/dynfx` out of the DN2 default sets and adding it with `--modules` where an effect is wanted.

## Open **[O]**

- The role of ring C words 2 and 3 (next pair) and of the rest of the frame.
- Whether the DN2 cost (x3.09) is acceptable for the desktop and browser targets, and which of the two ways to cut it is taken.
- A second agent checks the hook table against the image bytes before the marks become [V].
