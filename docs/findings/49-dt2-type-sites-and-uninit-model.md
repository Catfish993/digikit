# 49. Where the DSP uses a track's machine type, and the emulator with unwritten memory filled: rung C still passes

Lane b, 2026-10-18. Question: on the Digitakt II 1.17 device a track on a run-time machine type (`test/dyn-pluck`, type 18) stops the SHARC
(finding 47, "Rung C"), while `core/dsp-voice`'s hook never sees the type (`dispatches=0`), and the same flow passes in the emulator (scenario
`rung-c`). Emulator and static work only; nothing was sent to a device. Marks as in AGENTS.md: **[D]** measured or read once here, **[O]** open;
nothing is **[V]**. **No cause was found.** This finding gives the sites, rules out two causes in the emulator, narrows where the halt is, and
lists the next steps.

## 1. What the device log fixes (reading of finding 47 and `out/runs/device-logs/rungC-3.log`) **[D]**

- The last full block shows the wrapper's stage-1 breadcrumb (`HOOK_ENTERED voice=31 type=0`) and the two reply records differ by one block
  (beats 233,838 and 233,839). Stages 2 to 7 write their word **straight into both reply records** (finding 48). Had the first type-18 voice
  (voice 2, track 1) reached stage 2, that word and a non-zero `dispatches` would sit in the records, and the DMA keeps sending them. They do
  not. So the core stopped **before the stage-2 write of the first type-18 voice of its block**: in the block handler's frame handling, in the
  stock voice code of voices 0 and 1 (track 0), or in the wrapper between its entry and the stage-2 `CALL` (about 30 instructions, section 5).
  Stopping inside the routine, or on the way into it, is excluded by the same argument (stage 4 writes the record before `JUMP (M13, I12)`).
- The ColdFire block counter (`blocks` in every log line) keeps rising at 1,500 per second after the stop (233,093 at the stop, 257,094 at
  171 s), so the ColdFire keeps its audio block cycle and its frame exchange running. The frozen beats are the DSP's, not a stalled ColdFire
  task. **[O]** A stalled frame builder with a still-running block counter cannot be excluded from the log alone.
- `MOD_LIST ... slots=0` at the stop is the module's ColdFire **callback slot mask** (`loader.rs`, `slot_mask`), which `dyn-pluck` does not use.
  It says nothing about the machine row.

## 2. Where the DSP code uses a track's machine type, DT2 1.17 **[D]**

Found with `sharcdb` (`literals`, `dataref`, `readers`/`writers` of `tools/sharc_dev/sharc.py`) and `sharcfn --listing` of the functions
around each site; the loader image gives the table bytes (the table at 0x2567c0 is initialised data of the boot stream: its words are the same
on the device). `type` is `cache[t]`, the short word at 0x255970 + 2t (the DSP's copy of frame word 0x94 + 2t); "selector" is the word the stock
code stores at voice-record +0x4c.

| # | where | what it does with the type | table | entries it covers | extended by our modules? | what a type 16 to 23 reads or does |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `FUN_1c2b24` (frame handler), 0x1c2ccb | copies frame word 0x94 + 2t into the frame mirror | none | any value | no | copied unchanged |
| 2 | `FUN_1c2b24`, 0x1c33c1 to 0x1c33f1 | change detector: `cache[t]` against the live word (`comp`); on a change stores constants (M13/M14) into the per-track arrays 0x24f0ac and 0x24f0ec and calls 0x1c60a2 (sets byte +0x1b9 of voice record t) | none | any value | no | nothing is indexed by the value |
| 3 | `FUN_1c24e9` (parameter converter), 0x1c26ce (`M2 = cache[t]`) and 0x1c273c (`S2 = DM(I3, M2)`, `I3 = 0x2567c0`) | selector = word `0x2567c0 + 4 * type`, stored in the track's voice records (+0x4c) | 0x2567c0 | **8 words belong to it**: 0 1 2 3 4 0 5 and word 7, a flag (1 in the image, cleared by 0x1c2c8b in the first frame). Words 8 to 13, 16 to 25 are pointers of a neighbouring structure (0x2567e0 on, set up by the code at 0x1c1d22), words 14, 15, 26 on are 0 | no | type 16: 0x253e78, 17: 0x253ef8, **18: 0x254178**, 19: 0x2541f8, 20: 0x254278, 21: 0x2542f8, 22: 0x254878, 23: 0x2548f8. All are 7 or more, so every guard below skips the voice |
| 4 | `FUN_1c642a`, 0x1c6553 | `compu(selector, 6)`; below 6: `I12 = DM(0x8055c840 + 4 * selector)`, `JUMP (M13, I12)` at 0x1c6579 (stage A of the sample path) | PM table 0x8055c840 | 6 | no | `JUMP IF GE` at 0x1c6561: stage A skipped, no table read |
| 5 | `FUN_1c642a`, 0x1c66d6 | `compu(selector, 7)`; below 7: table read and `JUMP (M13, I12)` at 0x1c66ec | 0x8055c858 | 7 | no | `JUMP IF GE` at 0x1c66d8: skipped |
| 6 | `FUN_1c642a`, 0x1c6c10 | `compu(selector, 7)`; below 7: table read and `JUMP (M13, I12)` at 0x1c6c25 | 0x8055c874 | 7 | no | `JUMP IF GE` at 0x1c6c13: skipped |
| 7 | `FUN_1c71ec`, 0x1c7511 | `lshift(R2, -2)` non-zero skips; below 4: table read and `JUMP (M13, I12)` at 0x1c7521 | 0x25f7b0 | 4 | no | **[O]** `R2` is a word of a record whose source was not traced; it is not shown to be the type |
| 8 | `FUN_1c24e9`, 0x1c26c5 and 0x1c26e2 (`M0 = frame word 0xb4 + 2t`, `S3 = DM(I4, M0)`, `I4 = 0x2567a0`) | indexes by the **next per-track word** (the ColdFire's second byte of the track record, finding 04), not by the type | 0x2567a0 | 8 words: 1 3 2 4 5 6 0 0 | no | the index is whatever the ColdFire stores in that byte for a run-time machine **[O]** (not read in the emulator, see section 6) |
| 9 | `core/dsp-voice` wrapper, `_dv_scan` | the registry scan (rows `{type, pc}`, 0 ends the table) | `_dv_table` | the rows `core/dsp-voice` and `core/dyndsp` define (8 free rows `0xffffffff`, then an empty row) | yes, this is the extension | a matching row is a dispatch; no match and type 7 or more is stage 3 and the stock path |

The sites run before the wrapper's hook (1 to 3, the frame handler and the parameter converter) read the type but do not branch on it beyond the
selector of site 3, and every consumer of the selector (4 to 6) guards against a value of 7 or more. A run-time type therefore never reaches a
stock jump table. This agrees with finding 06 ("Selector collision") and adds the guards for sites 5 and 6 and the full list of words an added
type reads at site 3.

## 3. The emulator with unwritten memory filled **[D]**

`SHARC_UNINIT=<hex seed>` (default off; `packages/emulator/sharc-rt/src/rt/bnd.rs`, `uninit_value`): a read of a plain-RAM byte that neither the boot
stream nor a write has set returns a seeded non-zero pattern instead of zero (little-endian per byte, so any width and alignment agrees, one
function of the byte address and the seed). The emulator's generated cores are built with `explicit_memory_model = false`, where such a read gave an
*unknown* value, not zero (`_dm_read_full`); the setting covers both cases. `SHARC_UNINIT_LOG=FILE` appends the first 24 and every power-of-two
read (`pid N read #K of W byte(s) at ADDR`). Render cache: `SHARC_UNINIT` stays in the key (the `SHARC_` prefix is kept, so a pattern run never hits
a zero run), `SHARC_UNINIT_LOG` is on the denylist of `tools/devtools/render_cache.py` (output only). Scenario `rung-c-uninit`
(`tests/digikit/scenarios/rung-c-uninit.toml`, `[rungc] uninit = "5a17"`) runs rung C with it.

- **Result: rung C passes with the pattern on** (`rung-c` and `rung-c-uninit`, DT2 1.17, full tier, about 4 minutes each): type 18 on track 1, 19,574
  dispatches and as many returns, the note equals the patched-in pluck (0 samples differ), heartbeats rise, no `DSP_STOPPED`. It does **not** halt
  the way the device did.
- **What it executes: almost nothing reads unwritten memory.** In the whole run each emulator process made fewer than 32 such reads: four
  one-byte reads at the starts of patched code blocks in L2 (0x2001ab80, 0x2009afd8, 0x2009afe8, 0x2009b5e0) and five two-byte reads at
  0x8422b5c8 to 0x8422b5d0 (SDRAM, a sample area). Nothing in the type path reads memory that no block loaded or the code did not write first: the
  firmware's tables and its zero-filled areas are blocks of the boot stream, and none of the wrapper's or the link's data words (`_dv_*`, `_dl_*`)
  was read before it was written, so the emulator's zeros there are loaded or written zeros, which the device's boot ROM loads as well. **The hypothesis "the device reads uninitialised SRAM where the emulator reads zero"
  is not supported for the DSP**; it stays open only for SDRAM and L2 words *outside* the stream, of which there are about nine reads, none in
  the type path. **[O]** The ColdFire's own RAM (SDRAM) was not given the same treatment; the ColdFire writes the frame the DSP reads (section 6).
- With the setting off the read path returns what it did (zero with the explicit model, unknown without). `verify --only build,pins --dt2` (seven DN2 PCM pins),
  `dt2-tone` and `dt2-check` (DT2 scenario oracles) pass on the edited tree. The `bnd.rs` edit rebuilds the generated core (about 2 minutes here, warm).

## 4. Hardware PC and loop stack depth in the emulator **[D]**

A candidate on silicon but not in the emulator: the wrapper is the first code in this context that executes `CALL`/`RTS` (`_dv_crumb`, and the
routines' `CALL _dv_exp2`) and a `DO` loop (the odd-voice copy); the model's PC stack holds 256 entries, the SHARC+ core's much fewer. `SHARC_PCDUMP=0`
now also records the deepest PC, loop, status and call stack of any interpreted step and the pc where each was reached (a `#stack-max` line;
`{pid}` in `SHARC_PCDUMP_OUT` becomes the process id, the rows carry the two depths, `docs/INSTRUMENTATION.md`).

DN2 1.12 (`equiv-dyn-pluck`, quick tier; interpreted steps are the patched code and the parts the model does not compile): PC stack at most 2 to 3,
loop stack at most 1 to 2, status stack 1, call stack 1. DT2 1.17 (`equiv-dyn-pluck` and the full `rung-c`, where the registered path and the
pluck routine run for 19,574 dispatches): `#stack-max pc_stack=2 loops=2 status=1 call=1`, the deepest steps at stock addresses (0x1c0bba, 0x1c2973).
So the wrapper and the routine add nothing to the stock maximum, far below the hardware's depths. **A PC-stack or loop-stack overflow is not
the cause** unless the device's firmware context carries a much deeper stack than the emulator's, which nothing in the model suggests. The
measure covers interpreted steps only (the patched code always runs interpreted here, the stock code that runs as compiled blocks does not
report its depth). **[O]** the hardware depths were not looked up in the public manual in this lane.

## 5. What the wrapper does between its entry and the stage-2 write **[D]** (reading of `dsp-voice.s`)

For a registered type: the scan (about 12 instructions), `MODIFY(I7, -40) (NW)`, 20 register stores at `DM(-6 .. -25, I6)`, 13 stores of
the context registers (`DM(-26 .. -38, I6)`), six `M` register loads, `L0-L3, L5 = 0`, `BIT CLR MODE1 0x20A000`, `BIT SET MODE1 0x10000`,
`NOP`, the breadcrumb word, then `CALL _dv_crumb` (stage 2). The same `CALL _dv_crumb` is stage 3 for an unregistered type of 7 or more; that is
the case at a reboot with track 1 still on the added type and no module loaded yet, and Em saw the DSP dead then too (finding 47). So the
common part of all failing cases is: type 7 or more, and the first `CALL` of the wrapper. The common part of all working cases (hook only,
stock voices) is that the wrapper takes the stock path, which has no `CALL`, no `MODE1` write and no stores below `I6 - 6`.

## 6. Candidates, ranked, and the experiments that separate them **[O]**

1. **The stock frame handling for a type of 7 or more, before any voice code (sites 1 to 3 and 8).** The device halts at the moment the type
   appears in a frame (finding 47: selection, not the trig). Nothing in sites 1 to 3 differs between device and emulator in code or constant
   data. What can differ is a value the ColdFire puts in the frame for a run-time machine: the next per-track word 0xb4 + 2t (site 8, an index
   into an 8-word table) and the 0x60-stride machine-parameter block (0xda + 0x60t). The emulator reads them from its ColdFire's RAM, which is
   zero where the firmware did not write it; the device's SDRAM is not. Cheapest check: in the rung C flow, read frame words 0x94, 0xb4 and
   the first words of 0xda + 0x60 for track 1 from the DSP (the type cache and mirror, base 0x255970 in the DSP's own addressing; my `dsp_peek`
   attempts through the daemon returned zeros, so the address form for an L1 word still needs to be found) and look whether the byte after the
   type is a clean 0 or 1. Then fill the ColdFire's unwritten SDRAM the same way as `SHARC_UNINIT` does for the DSP and rerun.
2. **The wrapper's first `CALL`/`RTS` (and the loop) in the voice context.** The emulator's PC stack depth is small (section 4), so a stack
   overflow is unlikely; the other `CALL` hazards on silicon (a pending interrupt, the VISA return address alignment of code in L2, a conditional
   `JUMP` directly before `RTS` in `_dv_crumb`) are not visible in the model. Experiment: a wrapper build whose breadcrumb is expanded inline at the
   two stages that matter (no `CALL`, no `RTS`), and whose registered path does not write `MODE1` (the bits are already clear or set in the
   context: PEYEN is 0 in the measured MODE1 0x39011cf8). If the halt stays, this candidate is gone.
3. **A fault outside our code on a stock pipeline step for a run-time type, found by bisecting with breadcrumbs placed in the stock path.** Only
   this gives the answer the log cannot: hook the change detector (0x1c33d7), the end of `FUN_1c2b24`, and the entry of `FUN_1c24e9` with the
   existing straight-into-the-record breadcrumb write. The record shows then in which of the four steps of the first type-18 block the core
   stopped. This needs three more hook sites in the patch plan and the pins of the stock path to stay equal (no stock behaviour changes).

A device test that needs no new code and separates 1 from 2: the rung C image **without** `core/dsp-voice`'s registered path, i.e. a ColdFire-only
machine of type 18 (a typed machine and parameter rows, no DSP routine) selected on a track. The finding-45 report lists the framework-only image
as working, but not with an added type selected. If that halts, the cause is in the stock pipeline and the wrapper is cleared.

## 7. Files

- `packages/emulator/sharc-rt/src/rt/bnd.rs`: `SHARC_UNINIT`, `SHARC_UNINIT_LOG`.
- `packages/emulator/sharc/src/pc_dump.rs`: stack depth columns and the `#stack-max` line, `{pid}` in the output path.
- `tools/devtools/render_cache.py`: `SHARC_UNINIT_LOG` on the denylist.
- `tools/patchkit/pk_digikit_rungc.py`, `tests/digikit/scenarios/rung-c-uninit.toml`: the scenario with the pattern on.
- `docs/INSTRUMENTATION.md`: the two settings.
- Pre-existing and unrelated: `tests/devtools/test_render_inputs.py::test_runtime_file_reads_are_reviewed` fails in this tree on file-read counts
  of `plusdrive`/`patchkit` (untracked `packages/desktop`, `packages/web` are present); the environment-variable checks of that file pass.

## 8. Follow-up: the type >= 7 path against the public manuals, and the bisect build (Lane b, 2026-10-18, later)

Question: candidate 2 of section 6 (the wrapper's own path for types of 7 or more). Static check against the public SHARC+ manual (`out/refs/sharc-plus-prm`, "PRM"
below; page = printed page, file `pages/pNNNN.txt`; the hardware reference `sc58x-2158x-prm` has nothing on these rules), the emulator's handling of each rule,
then a bisect build that removes the path and its proof. Marks: **[D]** read or measured once here, **[O]** open; nothing is **[V]**.

### 8.1 The context the wrapper runs in **[D]**

MODE1 at the hook is `0x39011cf8` (finding 45; the device log of rung B shows the same value): decoded with PRM Table 28-19 (28-48 to 28-51, files p0613 to p0616)
it is CBUFEN (24), IPERREN, DPERREN, SPERREN (29, 28, 27), RND32 (16), IRPTEN (12), NESTM (11) and all six register-bank selects (SRRFL 10, SRRFH 7, SRD2L 6, SRD2H 5,
SRD1L 4, SRD1H 3). PEYEN (21), TRUNCATE (15), ALUSAT (13), SSE (14), BR0, BR8 are 0. So: the voice code runs on the secondary register files of both DAGs and
the data file; interrupts are enabled and nest (a higher-priority interrupt may run between any two wrapper instructions, except inside a delayed branch and its two
slots, PRM 4-18); the interrupt entry pushes MODE1 on the status stack and RTI restores it (PRM 4-9, 4-73, 28-45), and `CALL`/`RTS` never touch the status stack.
**The four bit writes of the wrapper (`BIT CLR MODE1 0x20A000`, `BIT SET MODE1 0x10000`) and the restore (`MODE1 = R0`) write the value MODE1 already has**: PEYEN,
TRUNCATE and ALUSAT are already 0, RND32 already 1, the bank selects are not in the masks and the restore writes back the saved full word. On the measured context they
change no bit. They would matter only in a context whose MODE1 differs (finding 45 draws such contexts in the unit rig; none was measured on a device).

### 8.2 Each instruction of the type >= 7 path and the rule it could break **[D]**

"Stage 3" is the path of an added type that no module registered (the case of a reboot with the track still on the type and no module loaded): the common prefix of
all failing cases. "Registered" is the further path of a type in the table.

| path | instructions | rule (PRM page) | could it break it? | emulator |
| --- | --- | --- | --- | --- |
| stage 3 | `DM(_dv_sv3) = R3`, `DM(_dv_sv5) = R5`, `DM(_dv_bcw) = R0`, ALU on R0 to R3 | none | no; the same kinds of access run on the stock-type path (`_dv_sv_l4`, `_dv_diag`) on the device | yes (functional) |
| stage 3 | `CALL _dv_crumb` (non-delayed) | PC stack is 30 deep, the almost-full interrupt SOVFI fires at 29 entries (4-8, 4-9, p0111, p0112); a non-delayed branch flushes the 11-stage pipeline (4-17 to 4-18); branch predictor: after every branch at least twelve valid 48-bit instructions that do not cross a memory boundary, and an RTS in the pipeline with a CALL or RTS is not predicted (4-13, 4-14, p0116, p0117) | depth: measured 2 to 3 in the emulator against 30 allowed (section 4), so no; the twelve instructions after the `CALL` (the stock jump follows after 6, then `_dv_crumb`) and after the `RTS` (`_dv_exp2`, 21 instructions) are code, the block boundary was not checked **[O]**; the other rules cannot be broken by the sequence | stack push/pop yes (the reference core has 30 entries and raises on overflow; the runtime core has 256 and never raises SOVFI); predictor, pipeline and stalls no |
| stage 3 | `_dv_crumb`: `I4 = _dv_diag` then `DM(12, I4) = R3`; `I4 = _dv_rec`, `R5 = DM(0, I4)`, `R5 = PASS R5`, `IF EQ JUMP`; `I4 = R5`, `DM(192, I4) = R3` ... | an index register loaded by the previous instruction stalls the next access, handled by the hardware (4-64, p0167); a store to an address outside memory raises ILADI (6-31, p0215) | the stall is interlocked; the pointers `_dv_rec` hold are the reply records at 0x266220 and 0x267220 (L1, finding 48) or 0 (skipped) | yes; the first store of this core into the reply record (outside the block tail) has run on no device **[O]** |
| stage 3 | `RTS` (non-delayed), reached by `IF EQ JUMP _dv_cc` | same as `CALL` | see above | yes |
| registered | `MODIFY(I7, -40) (NW)` and the 33 stores `DM(-6 .. -38, I6)` | I7 is a circular-buffer register: a modify that crosses B7 wraps by L7, and L7 = 0 with CBUFEN would raise CB7I at every change (6-30, 6-31, p0214, p0215) | no: B7 = 0x2ca5a0 (L7 = 0x3e2) is 0xd20 words below I7 = 0x2cb2c0; the stores stay within the 40 words reserved (I6 - 48 .. I6) | yes (no circular wrap in range) |
| registered | `M5 = 0`, `M6 = 1`, `M7 = -1`, `M13 = 0`, `M14 = 1`, `M15 = -1`, `L0-L3, L5 = 0` | register loads used by the next instruction stall (4-64); CBUFEN is on, an L of 0 is a linear register | no | yes |
| registered | `BIT CLR MODE1`, `BIT SET MODE1`, `NOP` | a MODE1 write takes effect one instruction later (alternate register sets: 6-29, p0213; 2-7, p0057; a `NOP` follows a MODE1 write in the manual's examples, 6-21, p0205); IRPTEN and NESTM up to one cycle (p0178) | two read-modify-writes in a row: if the second read the old MODE1 it would write the old bits back and undo the first; harmless on the measured MODE1 (8.1). An interrupt between them is harmless (RTI restores MODE1) | bank-select bits: yes, the change is visible after the following instruction (`bank_complete`, `rt.rs`, `state.py`); all other MODE1 bits take effect at once, so a missing `NOP` would not show |
| registered | `LCNTR = 32, DO _dv_copy UNTIL LCE` (two-instruction body), then `R3 = 6`, `CALL _dv_crumb` | loop and PC stacks are used together; a `CALL` in the last five instructions of a loop needs care (4-41, p0144); no change of flow in the body keeps it an F1-active loop (p0140) | no: the `CALL` is after the loop end, not inside; loop depth measured 2 | yes (stack depths measured) |
| registered | `JUMP (M13, I12)`, `JUMP (M13, I13)`, `JUMP (M14, I12) (DB)` with `R3 = DM(-6, I6)` and `RFRAME` in the slots | a delayed branch's two slots must not hold a branch or a PC-stack operation and run uninterrupted (4-18, 4-19, p0121, p0122) | no: the slots are a load and `RFRAME`, copied from the stock epilogue; the indirect jump through M13 exists 67 times in the stock image (finding 45) | the slots run; a forbidden slot is not diagnosed **[O]** |
| registered | `MODE1 = R0` after `R0 = DM(-38, I6)` | as the MODE1 writes above; R0 is read from the secondary file before and after (the bank bits are unchanged) | no | yes |

An observation outside the PRM rules **[D]** (`sharcdb`, dt2-1.17, table `edges`): the stock DSP image has 1,714 delayed and only 4 non-delayed `CALL`s (two in the
context-switch code at 0xb8822a and 0xb88531, two in the startup routine at 0x1c0f91 and 0x1c1417), and **1,001 returns, all `RTS (DB)`: not one non-delayed `RTS`**.
The stock renderer behind the hook has 3 delayed `CALL`s and 2 delayed returns. The wrapper's `CALL` and `RTS` are the non-delayed forms; the firmware never
runs a non-delayed `RTS`. They are valid instructions and no PRM rule is broken, but it is the one thing about the path that no stock code in this context does.

### 8.3 What the emulator models and what it does not **[D]**

Modelled: the one-instruction latency of the MODE1 register-bank selects and of PCSTKP writes; the PC, loop and status stacks (reference core: 30 entries, raising
on overflow; runtime core: 256, no SOVFI); delayed-branch slots as executed instructions; software interrupts as functional scheduling. Not modelled: pipeline timing
and stalls, the branch predictor, the L1 instruction and data caches (the stock firmware drives them: 0x1c0035 and 0x1c0923 walk SHL1C_CFG 0x3e000/0x3e002 and the
range registers), posted writes and `SYNC` (PRM 9-3), preemption of the wrapper by a nested interrupt at an arbitrary instruction, MODE1 effect latency of the
non-bank bits, and the checks on delay-slot contents. Every rule of 8.2 that is open is in that second list.

### 8.4 What the audit says about the cause

1. The **stage 3 path has no MODE1 write, no stores below the frame, no loop, no indirect jump**, and Em reported the DSP dead in that state (a reboot with the track
   still on type 18 and no module loaded; this report is not in a saved log, finding 47 does not carry it **[O]**). If that report holds, the registered-only rules
   (MODE1, I7, loop, delayed jump) cannot be the whole cause, and what is left of the wrapper is the non-delayed `CALL`/`RTS` pair and the stores into the reply
   records. If it does not hold, the registered path is in play again and so is every row of 8.2.
2. No row of 8.2 breaks a rule of the manual on the measured context. The two things the manual leaves to the program, the predictor distance (twelve valid
   instructions after a branch, inside one memory block) and the double MODE1 read-modify-write, are met or harmless on what was measured.
3. The bisect below removes the `CALL`/`RTS` pair, the reply-record stores through a pointer and every registered step from the path at once; the
   record stores can be put back later separately.

### 8.5 The bisect build **[D]**

- **Option, not fork.** `core/dsp-voice/src/dsp/dsp-voice.s` has two `#ifdef DV_BISECT_C2` blocks; `tools/patchkit/pk_build.py` gained `#ifdef`/`#ifndef`/`#else`/`#endif`
  (`apply_conditionals`; `--define NAME --out DIR` writes the objects of a variant to DIR and never over the committed ones). With the option off the text the
  assembler sees is the old text, so the committed objects are byte-identical: `pk_build build modules/core/dsp-voice --check` says current for both devices
  (the DN2 object is unchanged and does not use the option).
- **What the variant does.** For a registered type: `JUMP _dv_stock` right after the scan (no registered path at all). For a type of 7 or more: the stage 3 breadcrumb is
  written inline (the body of `_dv_crumb` without its `RTS`, two conditional jumps, no `CALL`), and the dispatch counter (`_dv_diag + 52`) counts the stage 3 passes so
  that the device log can show them (the last voice of a block overwrites the breadcrumb word with its stage 1, the counter does not get overwritten). The stock-type path
  (types below 7) is untouched; it keeps its L4 write and `MODIFY(I7, -26)`, which rung B ran on the device. The variant executes no `CALL`, no `RTS`, no `MODE1`
  or other system-register write, no store below the frame and no loop.
- **Tooling.** `tools/patchkit/pk_variant.py make MODULE OPTION LABEL` builds the variant into `out/variants/LABEL/` and prints that directory, a repository root (devices and
  firmware linked, modules linked but for the variant); `DIGI_PK_ROOT=<it>` makes `mise run patch|preflight|pack` and the proof rigs (`pk_dkrt.Tools`) use it. The
  scenario `rung-c-bisect` (`tests/digikit/scenarios/rung-c-bisect.toml`, `[rungc] variant` and `defines`) runs the rung C flow on it.
- **Proof 1, stock PCM unchanged.** `pk_proof_voice --patched out/patch/c2-voice-dt2.syx --only type0,sine60 --quick` (the variant with `core/dsp-voice,test/sine-voice`): part
  `type0` PASS (patched-type0 bit-identical to stock). Part `sine60` FAILS as it must (6.9 Hz instead of 261 Hz: the forced type 8 is registered by `test/sine-voice` and the
  variant never dispatches), which shows that the measurement saw the variant. Unit rig: `tests/patchkit/test_dsp_voice.py::test_the_bisect_build_takes_the_stock_path_for_added_types_with_no_call`
  (a breakpoint on `_dv_crumb` is never hit, the stage 3 words equal the stock build's, a registered type is not dispatched); `123 passed` for the file on the edited tree.
- **Proof 2, the rung C flow.** Scenario `rung-c-bisect` on the device image (the report carries sha256 `3a34dcf6...`, the image of 8.6): PASS in 228 s (full tier): the upload,
  the menu and the restart as for rung C; the run-time pluck chosen on track 1 (type 18); then **39,148 stage 3 passes in 19,877 blocks (1.97 a block, 2 voices; the 1.5 % shortfall is
  the 300 blocks between the sample and the TRIG press), 0 returns, the heartbeat rises (37,823 to 57,700), no `DSP_STOPPED`**. Two earlier runs of the same scenario
  helped to fix the check: the first read the last breadcrumb (always voice 31, stage 1) and so could not show stage 3; the second compared against exactly 2 passes a block.
  **A difference to the device [D]:** in the emulator the passes begin at the TRIG press, not at the selection (`dispatches` is 0 at 500 ms after the selection and the count
  matches a start at the trig to within 6 passes). The ColdFire puts the added type into the frame the DSP reads only for a track that plays. On the device the halt
  of rung C came with the selection and the held TRIG together, and the reboot case needs no TRIG at all, so in some state the device's frame carries the type earlier
  than the emulator's: that is candidate 1 of section 6 seen from the other side **[O]** (a boot with a project that has track 1 on type 18 and a look at the frame word before any trig).

### 8.6 The device image and how to read the result

- Image `out/patch/rungC2-dt2-1.17.syx` (not committed): 1,885,472 bytes, sha256 `3a34dcf60697a73fce28c67d476be3b6e421e1d4ea08ddc8f9496e1aa3350174`.
  `mise run preflight` ends with `PREFLIGHT OK` (sections 3 and 7 differ from stock, 2, 4, 5, 8 identical, checksum, HMAC, limits, base sha256 `26c22f66...`).
  The rung C image of the same tree: `ac72f220f3c2f39b22566ec9b23e5884214f4ebebdabc6ddd676d84e1c556f1a` (1,885,344 bytes). The module file is the same `dyn-pluck-dt2.dkm`.
- Procedure and reading: docs/PATCHING.md "Rung C2 (bisect)". No halt: the wrapper's call path was the cause and the stock pipeline accepts type 18. Halt (with
  `dispatches` frozen at N, or 0): the stock pipeline stops on the type with the wrapper's path out of the way; candidates 1 and 3 of section 6 are next.

### 8.7 Open items

1. Em's report of the dead DSP at a reboot with the track on type 18 and no module has no saved log; if the C2 image does not halt, repeat that reboot case with the
   C image and keep the log: it is the cleanest pair (same state, `CALL` path against inline path).
2. A C3 variant with delayed `CALL`/`RTS (DB)` (two `NOP` slots) in `_dv_crumb` would test the "firmware never runs a non-delayed `RTS`" observation if C2 does not halt and C does.
3. The reply-record stores through a pointer (`DM(192, I4)`) were never run by this core on a device outside the block tail; the C2 image keeps them, so a halt in C2 does not clear
   them. A C2b without them (breadcrumb in `_dv_diag` only) is a one-line change of the option.
4. The twelve-instruction predictor distance and its memory block were read, not measured (code at 0xbcd7f4 to 0xbcda60 in L1).
5. In the emulator the type reaches the DSP at the TRIG; on the device earlier. Find the state that makes the difference (8.5) before trusting any emulator pass for this halt.
6. Caches (L1 D-cache on the L2 variables, write-back by default, PRM Table 8-2, p0262) are not modelled; the reply records are L1 (not cached), the wrapper's variables at 0x2009b4d4 and up are L2.
