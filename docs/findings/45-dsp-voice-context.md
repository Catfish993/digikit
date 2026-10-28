# 45. DSP VOICE CONTEXT: the registers the machine path assumed, what the emulator holds, dl_diag

Phase 9, "Pluck on silicon" (docs/plans/digikit-runtime.md). Sources: the 2026-10 hardware report (a note to the author from a run of
patchkit's machine path on a physical Digitakt II, OS 1.17; kept outside the repository), the module sources, `sharcdb` on dt2-1.17,
and the coupled emulator. Marks as in AGENTS.md; nothing here is **[V]** yet: a second agent has not checked the image bytes.

## 1. What the report claims, and the first correction

On the device the SHARC halts as soon as a machine dispatches through `core/dsp-voice` (also `digikit/pluck`), though the image with
only the hook, and the image with the machine/parameter framework, run with full stock functionality. The report names two causes,
both "a register the firmware restores per DSP context was assumed to have a fixed value":

1. `_dv_odd` (the odd voice copies the even voice's block) stepped by `M6`, which the context restore reloads (`M6 = PM(I11 + 0x49)` at
   0xb885ea); no test had ever run an odd voice through a hostile `M6`.
2. `JUMP (M13, I12)` and `JUMP (M13, I13)` assume `M13 = 0` (restored per context: `M13 = DM(I3 + 0x50)` at 0xb88588); "stock never
   uses a jump through M13".

**[C] The last statement is wrong.** The stock image has indirect jumps through M13: 67 instructions of forms 9a/9b whose modify field
selects M13 on dt2-1.17 (`select ... from insn` over the pmm field), among them the dispatch sites 0x1c6579, 0x1c66ec, 0x1c6c25 and
0x1c7521 (`JUMP (M13, I12)`, finding 06) and 0x1c144c (`JUMP (M13, I13)`). **[D]** The same holds for M5 and M6 in the very function the
hook sits in: the stock renderer behind the hook (0x1c4f81 to 0x1c5334, 381 instructions) uses M5 20 times (post-modify 0), M6 39 times
(`modify(I4, M6)`, `DM(I4, M6)`, the filter taps) and stores M13 as the constant 0 seven times (`DM(I4, M1) = M13` at 0x1c5008,
0x1c5048, `DM(I5, M6) = M13` at 0x1c5246, ...); no
instruction of 0x1c4ecf to 0x1c5576 writes M5, M6 or M13 (`regdef`); in the whole image the only writers are the startup routine
0x1c0f24 and the context restore (finding 06, "A zero-initialiser also writes 0x254d9c"). So on the device, where "hook only" plays
stock audio, M5 = 0, M6 = 1 and M13 = 0 hold at the hook, whatever the restore reloads. **[O]** The report's own rungs agree: "Grid,
32 zeros via `M6`" and "Werp, bare `JUMP (M13, I13)`" ran on the device with the engine alive. **The two causes therefore do not explain
the halt by themselves**; the fixes below stay (they remove an unverifiable assumption, and a hostile context is cheap to test), but the
halt is still unexplained. What the rungs leave open is the path no framework-only image exercises: an added machine type (7 and up)
selected on a track, with the stock pipeline's per-type steps before and after the hook. `dl_diag` (section 6) exists to settle it with
one captured block.

## 2. Audit of the machine path

Every register the machine path touches, and what the code does now. "pinned": saved in the frame (DT2) or in `_dv_ctx` (DN2), set to
the value, restored when the routine returns. Files: `modules/core/adapters/dsp-voice/src/dsp/dsp-voice.s` (DT2), `dsp-voice-dn2.s` (DN2).

| register | used by | now |
| --- | --- | --- |
| M5 | `DM(I4, M5)` for the type-cache and frame reads (wrapper and `_dv_frame_hw/_sw`); routines (pluck) as post-modify 0 | the wrapper's own code uses `DM(0, I4)`; pinned to 0 for the routine |
| M6 | registry scan (two reads per row), `_dv_odd` copy, routines (`DM(I0, M6)`) | scan with `DM(0, I4)`, `DM(1, I4)`, `MODIFY(I4, 2) (NW)`; pinned to 1 (copy and routine) |
| M7 | routines (`DM(I1, M7)` in pluck's comb), the epilogue `I12 = DM(M7, I6)` | pinned to -1 for the routine; the epilogue runs with the context's M7 as the stock epilogue does (every stack push of the firmware uses it) |
| M13 | `JUMP (M13, I12)` into the routine, `JUMP (M13, I13)` out of it (every routine) | pinned to 0 for both jumps |
| M14 | the epilogue `JUMP (M14, I12) (DB)` | the context's (as stock); pinned to 1 for the routine |
| M15 | none (the old ABI stated -1) | pinned to -1 |
| M1-M3 | clobbered by routines | saved and restored (DT2). M4 is clobbered without a save, as the stock renderer does [O]: no caller reads it |
| L0-L5 | I0-I5 as linear pointers in the wrapper (`DM(0, I4)` etc.) and in routines | pinned to 0 (the L4 that the wrapper's own I4 needs is zeroed at the entry and put back on the stock path too) |
| B0-B5 | only with a non-zero L | unused with L = 0 |
| I6, I7, B6/B7/L6/L7 | the frame and the circular stack (`DM(-n, I6)`) | untouched; the frame reservation grew from 30 to 40 words for the new saves |
| MODE1 | float and ALU behaviour of the routine | SIMD (PEYEN, bit 21), TRUNCATE (15) and ALUSAT (13) cleared, RND32 (16) set: the startup value, `0x1011800` with the interrupt bits. BR0/BR8 and SSE (14) are never set anywhere in the image [D] and left alone; the register-bank bits (SRD/SRRF) are left as found, see section 3 |
| the DO-loop stack | routines use `LCNTR = n, DO` | untouched (the stock renderer nests the same way) |

Run-time machines use the same wrapper (the voice rows of `core/dyndsp` are rows of the same table), so `test/dyn-pluck` is covered;
the voice ABI paragraph of docs/design/digikit-dkm.md names only the imports (`_dv_exp2`, `_dv_frame_hw/_sw`), which no longer depend on
M5. Not changed and audited: `core/dsp-link`'s hook (now pins M5, M6, L2, L3 around its own code, section 6), `digikit/master-gain` and
`digikit/master-drive` (M5 = 0, M6 = 1 at the converter call they replace; the stock converter itself uses the same registers there,
finding 06 on the Q31 converter, so the site is as safe as stock) **[O]**.

The ABI is written where authors read it: the headers of both wrapper sources, `modules/core/adapters/dsp-voice/README.md` and the new section
"The register contract of a DSP voice routine" in docs/MODULE-AUTHORING.md. **[C]** The older statements "M5 = 0, M6 = 1, M7 = -1,
M13 = 0, M14 = 1, M15 = -1 hold on entry" (finding 06, sections on the routine ABI) described the emulator's and the rigs' assumption;
the wrapper now guarantees them by setting them.

## 3. What the emulator holds at the hook

Tool: `uv run python -m tools.patchkit.pk_ctxprobe [--device dt2|dn2] --modules core/dsp-voice[,core/dsp-link]`. It patches the image,
boots it coupled, runs 400 blocks and reads `_dv_diag` (the words `dl_diag` returns) from DSP L2; with `core/dsp-link` it also reads the
words from the ColdFire's copy of the reply record. **[D]**, DT2 1.17 and DN2 1.12, four samples over 400 blocks, no key pressed (the
hook runs for every voice every block):

| | M5 | M6 | M7 | M13 | M14 | M15 | I6 | I7 | B7 | L7 | MODE1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| DT2 1.17 | 0 | 1 | -1 | 0 | 1 | -1 | 0x2cb2c8 | 0x2cb2c0 | 0x2ca5a0 | 0x3e2 | 0x39011cf8 |
| DN2 1.12 | 0 | 1 | -1 | 0 | 1 | -1 | 0x2d15a8 | 0x2d15a0 | 0x2d08c0 | 0x3e2 | 0x39011cf8 |

Constant in every sample. MODE1 = IRPTEN, NESTM, RND32 and CBUFEN (the startup value) plus **all six register-bank selects**
(SRD1H/L, SRD2H/L, SRRFH, SRRFL: 0xf8 and 0x400) and bits 27 to 29 (not decoded here): the voice code runs on the secondary register
files, in an interrupt (software interrupt) context of the DSP's kernel. The emulator models the banks (the ready state is built from a cold start that sets
`bank_model`, `stack_model`, software interrupts and the core timer, tools/sharc_dev/sharc_cold_state.py), so the registers read are the
ones visible in that bank.

Why they were "friendly": the rigs did not read them from anywhere. `tests/support/dsp_voice_rig.py` wrote `M5 = 0, M6 = 1, M7 = -1,
M13 = 0, M14 = 1, M15 = -1` into every call (and the L registers and MODE1 were not set at all), so no test could see a dependency; the
ABI table "hold on entry" was then written from the rig. The emulator itself is not shown to be wrong: it holds the same values the
stock code needs (section 1), the context restore reads frames that the same firmware saved from live registers, and there is no
second source of values in the emulator that the device lacks. What the emulator cannot show is a value that only silicon produces
(SRAM power-on content in a never-saved frame, a bank initialised by the boot ROM): none of the stock paths reads one, since stock plays
on the device. **[O]** `dl_diag` on the device gives the real values.

The rig now draws the context from hostile values (`draw_context`: M5, M6, M13, M15, L0-L5 and MODE1; M7 = -1 and M14 = 1 stay because
the stock epilogue and every stack push use them), runs odd voices, and checks the registers on the way out.

## 4. Tests (kept)

- `tests/patchkit/test_dsp_voice.py`: `test_the_machine_path_does_not_depend_on_m6_or_m13` (voices 0, 1, 2, 3, 14, 15, 30, 31 x M6 in
  {0x1b8, 4, 0, -1} x M13 in {4, -2, 0x10}: same block as with friendly constants, every context register intact, I6/I7 restored);
  `test_a_drawn_context_changes_nothing` (12 seeds, an even and an odd voice, hostile M5/M6/M13/M15/L0-L5/MODE1, plus the stock
  fall-through with the context untouched); `test_the_context_is_pinned_for_the_routine` (a probe routine stores what it sees: M5 = 0,
  M6 = 1, M7 = -1, M13 = 0, M14 = 1, M15 = -1, L0-L5 = 0, MODE1 pinned bits).
- Mutation check: the pre-fix wrapper (`git show 3a0e769d:modules/core/dsp-voice/src/dsp/dsp-voice.s`) fails 117 of the 118 tests of that
  file under the new rig (even voice 0 already: the scan stepped by M6); the new wrapper passes all.
- `tests/patchkit/test_pluck.py` now runs the routine in drawn contexts and still matches the float32 model.
- DSP cost per call on the DT2 rig (instructions, friendly context): stock path +8 (voice 0: +34, it records the diagnostics), registered
  even voice +75 (126 to 201 with the note off), registered odd voice +49 (143 to 192). With all 16 tracks on a machine that is
  +2 000 of about 207 000 instructions per block; with none, +250 (below).

## 5. PCM equality, and a phase sensitivity of the DN2 proof

**[D]** `equiv-dyn-pluck` (patched-in `digikit/pluck` against the same machine loaded at run time), DT2 1.17 and DN2 1.12, quick tier,
before (HEAD) and after, WAV sha256 of the 2 s captures:

| | patched-in | run-time |
| --- | --- | --- |
| DT2 1.17 | identical | identical |
| DN2 1.12 | identical | the same samples **one DSP block (64 samples) later** |

The DN2 run-time image contains `core/dsp-link`; its hook now costs 56 more instructions per block (section 6), which moves the
block at which the scripted key press lands (the documented timing sensitivity of the coupled model: tools/patchkit/pk_dsplink.py,
"moves the note-on against the block phase"). Checked: shifting one capture by 64 samples gives max |difference| below 1e-6 over the whole
capture. With the `core/dsp-voice` change alone (before the `core/dsp-link` change) both DN2 captures were bit-identical, so the shift is
the link hook's cost and not the wrapper's. The scenario's own check (patched-in against run-time) passes in every run.

Also run and passing after the change (quick tiers): `pk_proof_voice` (DT2: patched-type0 bit-identical to stock, sine, level, filter),
`pk_proof_voice_dn2` (see below), `pk_proof_pluck` and `pk_proof_pluck_dn2`, digikit scenarios `roundtrip-dt2`, `upload-quick`,
`equiv-dn2-set`, and the unit tests of tests/patchkit (dsp_voice, pluck, dsplink, pk_sharc, dsp_budget, types, registry, mem, health,
master_drive, machines, proofs, device).

**Phase sensitivity (DN2 `pk_proof_voice_dn2` part a).** "FM TONE with the hook patched in equals stock" was bit-identical (after a
one-block capture shift) with the old wrapper and failed with the new one, though nothing in the stock path of the wrapper changed
the audio. **[D]** What differs: from the key release on (sample 36 283 of 62 197) the patched capture is the stock capture times one
constant, 0.99095, to six digits and for the whole tail; before the release the two are bit-identical. That is the release landing one
envelope step earlier or later against the oscillator, i.e. a different sub-block phase of the scripted key, not different DSP code. The
phase comes from the boot of the patched image (finding 17: a patched image boots cold, not from the stock snapshot) and so depends on the
image bytes, not on what runs: it flips with the size of **unexecuted** code. Measured, same stock path: HEAD wrapper plus 100 bytes of
data, 3 or 20 `NOP`s at the entry, 44 `NOP`s in the registered path: identical; 130 `NOP`s in the registered path: not identical;
the full new wrapper: not identical; the interpreter-only emulator (`DIGI_EMU_FAST=0 SHARC_AOT=0`) agrees, so it is not an AOT effect. The
proof's check is now phase-tolerant (`release_phase_equal` in tools/patchkit/pk_proof_voice_dn2.py): bit-identical, or bit-identical
before the release and the stock tail times one constant (spread below 1e-4) after it, with the capture shift searched. The first half of
the capture must still be bit-identical, so a wrapper that changes the audio fails it.

## 6. dl_diag: the device debug log's view of the DSP

**DSP side.** `core/dsp-voice` counts a heartbeat and copies M5, M6, M7, M13, M14, M15, I6, I7, B7, L7, MODE1 into `_dv_diag` at the entry
of the first voice of each block (DT2 voice 0, DN2 slot 0), before it pins anything. `core/dsp-link`'s block-tail hook (it runs once per
block, after the record is built) counts its own beat and copies the block, with a header, into the reply record that the ColdFire
already receives each block. 14 words (since finding 48: 17, with a breadcrumb, a dispatch count and a return count, rows 1 to 9), two per slot row in rows 1 to 7 at record bytes `0x1c + 0x54 row + 0x44` and `+ 0x48`: entries 11
and 12 of those rows, which no DSP code writes and the ColdFire reads only for parameters 36 to 39 (the acknowledgement uses the same
two words of row 0, finding 31). The word layout is in `modules/core/adapters/dsp-link/README.md` and `src/dsp/dsp-link.inc`: word 0 =
`0xD1A6 | 14 << 16`, 1 = link-hook beat, 2 = voice-hook beat, 3-8 = M5, M6, M7, M13, M14, M15, 9-13 = I6, I7, B7, L7, MODE1.

**ColdFire side.** `int dl_diag(u32 *out, u32 n)` in `core/dsp-link` (C ABI, also an optional extern for other modules): writes up to
min(n, 14) words and returns the count; -1 when the image has no record address, -2 when the record carries no diagnostic block (word 0
has no `0xD1A6`: `core/dsp-voice` absent, or never run). The record address is the literal of the block interrupt handler's first `pea`,
a new anchor `dl.cf.rec_lit` (the site core/health already uses as `health.rec_lit`; devices/ports/*.toml re-resolved, four ports). A
halted SHARC stops updating the record: the copy keeps the last block, so a frozen word 1 or 2 across two calls is the halt signal; the
last registers it holds are those of the last block that ran.

**[D]** In the emulator (`pk_ctxprobe --modules core/dsp-voice,core/dsp-link`): the words in the ColdFire's record copy equal the DSP's
`_dv_diag` on DT2 1.17 (record at 0x8000488c) and DN2 1.12 (0x800053a4), the beats advance, words 3-13 are the table above. `dl_diag`
itself runs on the ColdFire core in `tests/patchkit/test_dsplink.py` (values, a short buffer, -1, -2), and the offsets used by the two sides
are checked against each other there. **[O]** Whether the device's engine rewrites or reads those record bytes is not observed; the
emulator and finding 31 are the evidence.

**Cost, one paired A/B.** The emulator counts one cycle per instruction, so the figures are exact and a pair needs no repeats
(`pk_ctxprobe --cost`: the DSP's own block cycle count from word 0 of the record, mean of 100 blocks after 300 warm-up blocks; A = HEAD
modules, B = this change, the same image otherwise):

| | A | B | difference |
| --- | --- | --- | --- |
| DT2 1.17, cycles per block | 206 886 | 207 136 | +250 (+0.12 %) |
| DN2 1.12, cycles per block | 333 073 | 333 228 | +155 (+0.05 %) |
| `core/dsp-link` hook, idle path, instructions (concrete core) | 24 | 80 | +56 (+0.03 % of a DT2 block) |

Word 0 ends before the link hook (it is read earlier in the block handler), so the hook's 56 are counted separately on the concrete core
(`_dl_hook` with no transfer: 23 of the 56 pin and restore M5, M6, L2, L3, the rest is the diagnostic copy). Together about +0.15 % of
the DSP's block time on the Digitakt II when no machine is registered. The ColdFire side costs nothing per frame (the record address is
read in `dl_diag`, not stored per tick).

## 7. The Type 2b / 2c width tie in `pk_sharc`

`R0 = R0 + R1;` assembles to the 16-bit Type 2c word `0xc001`. The leading bits of a 32-bit Type 2b (`1100 0000 0`) match it, so the
verifier's free-running decode (`tools/sharc_dev/sharc_selache.py compare_external_bytes`, no lookahead) read it together with the next
word as a 2b and `pk_build` refused the object ("Selache lists c001, our decoder c0019808"). The emulator breaks the same tie with a
lookahead over the successors (packages/emulator/sharc-rt/src/decode.rs, `decode_width_resolved`; the Python twin
`sharc_disasm.resolve_confident_width`).

- **Verifier, fixed.** The assembler chose the form, so its extent decides a tie when a form of exactly that width matches
  (`InstructionSet.decode_words(..., max_bits)`, `pk_sharc._native_rows` and `verify`). `compare_listing` is unchanged: it stays the
  differential oracle that exposes the disagreement (tests/sharc/analysis/test_sharc_selache.py).
- **Emulator, not changed, now checked.** `verify` additionally decodes every instruction with the emulator's width resolution and refuses
  the object where the width differs from the assembler's. **[D]** The emulator reads `R0 = R0 + R1` as a 4-byte 2b whenever the wide chain
  decodes cleanly and does not rejoin (for example `R0 = R0 + R1; NOP; NOP;`, or followed by any `R0 = DM(0, I4)`): 252 of 864
  combinations tried, all with the 16-bit form first. On the silicon the width is fixed by the opcode, so the emulator would then run
  another instruction than the device. The error names the workaround (distinct registers, `R0 = R1 + R2`, a 32-bit form).
- **Our module objects.** **[D]** For every DSP object of every module (12 sources, about 2 200 instructions, including the new
  wrappers; the `core/dsp-link` objects are checked by their build) the emulator's width resolution equals the assembler's extent at every instruction: no module object is affected today.
- **In the firmware** the lookahead reads the 50 aligned `c0xx` words whose second byte has its high bit clear (the ambiguous range)
  as 2c on DT2 1.17 and never as 2b; on DN2 1.12 it reads 81 of them as 2c and 19 as 2b **[O]**: whether those 19 are real 32-bit
  instructions or the emulator's wide-wins default misreading short ones is not decided by the images. A decoder rule that prefers 2c for
  the whole `c0` range would fix the synthetic cases; it changes the decode of firmware code, so it needs the golden and PCM-pin
  approval and was not made.

## 8. Open items

**Update (finding 48).** `dl_diag` now returns 17 words: word 14 is a breadcrumb (stage, voice, machine type) that the wrapper writes straight into both reply records before each step of a
registered machine's path, word 15 counts dispatches and word 16 the routines that returned; cost +0.09 % of an idle block, +241 instructions a block for one track on a run-time machine; rung C's
halt, if it comes, is read from them. The first open item below stands.

- **[O]** The cause of the halt on the device. Run an image with `core/dsp-voice` and `core/dsp-link` (and `dl_diag` called by the
  device log) on the Digitakt II, select a machine type of 7 or more on a track, and read the words: frozen beats say where it stopped
  (word 1 moving and word 2 frozen: the block handler runs but the voice hook is not reached; both frozen: the core is halted), words 3-13
  give the real context. Candidates the rungs of the report do not exclude: the stock steps for an added type before or after the hook
  (the per-type tables the framework extended), memory the device treats differently from the emulator in the regions used for the
  copied tables.
- **[O]** M4 is clobbered by routines without a save (as the stock renderer does).
- **[O]** The master-bus modules and the SIMD/bank state at their site were not changed.
