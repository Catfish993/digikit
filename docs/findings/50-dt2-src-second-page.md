# 50. Digitakt II: the second SRC page of an added machine draws almost nothing

Lane r1, 2026-10-18, emulator and static work only (DT2 1.16 and 1.17; nothing sent to a device). Marks: **[D]** measured here once, **[O]** open.
Report from the device (finding 47, rung C, 1.17): with the run-time pluck (`test/dyn-pluck`, type 18) on track 1 the first SRC page is right, the
second "draws almost blank (only the TUNE glyph at the top and small bar indicators)" and turning encoders there shows popups with the first page's
parameter names.

## Cause **[D]**

- **It is not a run-time registry problem.** The patched-in `machines/pluck` (type 9) draws the same two frames, byte for byte (PNG equal on every frame of
  the scenario `pages-dyn-pluck`, before the fix). The first page is right on both.
- The SRC view `SourcePageView` has two pages for every machine of the sample family (SAMPLE, WERP, STRETCH, REPITCH, SLICED SMP, MANUAL SLICE; the
  page dots at the right edge). Page index 0 is the generic knob page (`ParameterPageView::vfunc_4`). **Page index 1 is the sample view**: a row of
  mini widgets (glyph for TUNE, PLAY, the sample box; bars for LEV) and in the middle the waveform with its markers, or MANUAL SLICE's slice grid
  (stock frames: `out/runs/lanes/r1-pages/stock{0,1,3}-p1.png`, not committed).
- `SourcePageView::vfunc_4` (`0x40016eac`, hash `cbe8bf9e...`) draws that centre by `FUN_4005f6bc(view)` (the track's real type, a signed long) in a mask test
  that only knows types 0 to 6 (`if (type < 7) { m = 1 << type; m & 0x12 -> widget at view+0x204; m & 0xd -> view+0x1d0; m & 0x40 -> view+0x234 }`).
  An added type (7 and up) draws the glyph frame and no centre. The same type is used by `FUN_40017728` (widget data per type, called every tick) and
  by four more sites of the view through the table below.
- The popups: the knobs of the sample view are the same eight parameter codes as on the knob page (the page descriptor is the machine's row), so a
  turn names `Decay=73` on both pages. That is as designed; it only looked wrong because the page showed no labels.

## Fix **[D]**

An added machine has no sample to show (its knobs are its own parameters), so **both pages are the knob page**. `core/machines` (Digitakt II) hooks the test
at `SourcePageView::vfunc_4 + 0x22` (`addq.l #4,SP; tst.b D0; bne.w knob_page`, 8 bytes, after `jsr FUN_4005b076`): `mf_srcpage` takes the knob-page branch also when
`FUN_4005f6bc(view)` is `>= machines.first` (signed, so -1 without a track stays on the stock path). Types 0 to 6 fall through to the stock code
(the frames of SAMPLE, WERP and REPITCH are still the sample view, checked). Data: three anchor rules in `modules/core/machines/module.toml`
(`cf.machines.srcpage` +0x22 len 8, `.alt` +0x1fe the branch target, `.type` the function `FUN_4005f6bc`; the rules are also in
`tools/patchkit/pk_machine_rules.py`; resolved into `devices/ports/dt2-1.1{6,7}.toml`, 1.16 and 1.17 have the same addresses, each hash unique). No
registry field and no module data: every type from `machines.first` up takes the knob page, which includes the run-time rows 16 to 23 and the gap rows.
Digitone II is untouched (its machines have their own SYN pages, finding 33; no change to the DN2 image: its pin did not move).

Tests kept:

- Scenario `pages-dyn-pluck` (`tests/digikit/scenarios/pages-dyn-pluck.toml`, tool `tools/patchkit/pk_digikit_pages.py`, `uv run python -m tools.patchkit.pk_digikit_scen run
  pages-dyn-pluck`): three boots (patched-in `machines/pluck` as the twin, the run-time `test/dyn-pluck`, and stock types listed by position). Checks: all nine
  frames (select, SRC, three encoder turns, SRC again, three turns) of the run-time pluck equal the twin's; none of them is the sample view (a long
  line at pixel row 157); the stock SAMPLE, WERP and REPITCH second pages still are. PASS on dt2-1.16 and dt2-1.17 (about 150 s each); frames in
  `out/runs/lanes/r1-pages/`.
- Unit test `test_src_page_hook_draws_the_knob_page_for_added_types_only` in `tests/patchkit/test_pk_machines_fw.py` (native core, a stand-in for `FUN_4005f6bc`): types 0, 3, 6 and -1 take the stock path, 7, 18 and 23
  the knob page, a true stock condition stays; the view register and the stack are as the stock test leaves them.
- Moved by the hook (section 3 only): every Digitakt II image with `core/machines`; the six DT2 pins of `packages/patchkit/tests/typed.rs` are updated (the DN2 pin is
  the same). Smoke cells `dt2-1.17:core/machines+machines/xslice` and `...+machines/pluck` pass with unchanged frame goldens (31 checks each); `equiv-dyn-pluck` on
  dt2-1.17 passes (PCM equal after the onset alignment as before). Images built before this change (rung C) do not have the fix: rebuild.

## ColdFire tables indexed by machine type, and what covers types 16 to 23

| Table or test | Where | Types 7 and up | **[D]** / **[O]** |
| --- | --- | --- | --- |
| machine list order, rank pairs, group, count bounds | list builder `FUN_40052a08`, comparator | `core/machines` rows (`mf_list_buf`, `mf_rank_buf`), reserved rows listed in type order, shown only when the name is set | **[D]** (findings 02, 30) |
| descriptor dispatch (`FUN_400c898a`, nine knob codes, names) | machine rows, `mf_dispatch` | the row | **[D]** |
| name, short name, header hint (three accessors, 12-byte rows) | `mf_names` | row copied from `clone_of` | **[D]** |
| permission mask, per-type byte (6 sites), persistence bound `(u8)(type+1) < 8+N` | `mf_permit`, `mf_pertype`, `cf.machines.persist` | rows to type 23, bound 8+17 | **[D]** |
| `type == 6` tests (7 sites, MANUAL SLICE) and the `(type & ~2) == 4` test | `clone.0` to `clone.6` | pass for clones of SLICE only | **[D]** |
| parameter code per (slot, type), 7 x 10 table in bss (`FUN_400da3a2`) | `mf_typearg` | the row's knob codes | **[D]** |
| DSP type -> selector word table (DM `0x2567c0`, `FUN_001c24e9`) | `mf_selector`, copied into `dsp.l2`, literal repointed | one word per row 7 to 23, the clone's (0 for the reserved rows: the DSP renders them as SAMPLE) | **[D]** |
| **sample view of the SRC page** (mask test on type 0 to 6) | `SourcePageView::vfunc_4` and `FUN_40017728` | **no widget; this finding** | fixed for the page by `mf_srcpage` |
| **`DAT_401eda30`, 7 bytes: the parameter code of the sample slot per type** (read with `lea 0x401eda30` at `0x40016cf0`, `0x40016daa`, `0x4001774c`, `0x40017d5a`, `0x40017f78`, `0x400183f8`, `0x40018450`; each as `type < 7 ? table[type] : 0`) | `SourcePageView`: the YES key (sample picker), `FUN_40017ed0`, `FUN_40017b50` (the "Preset N / sample name" popup), `FUN_40016d60` | code 0 for an added type: **not patched** | **[O]** not exercised: YES on the SRC page of an added machine may open the sample picker on code 0, or do nothing. A table copy with one byte per row (the clone's, or 0 for machines without a sample) would cover it; no device report on it yet |
| the other `== 6` or `< 7` tests reached from the type reader | `FUN_40048fb2`, `FUN_4005c8e0`, `FUN_4005f822`, `FUN_40030d12`, `MachineParameterPageView::vfunc_2` | all lie in the seven clone sites' functions | **[D]** (hash of the sites' functions) |

Nothing else was found in the 35 functions that call the type getter `FUN_4005063e` (list above) and the 60 functions that read the type byte (`+0xa2`, `FUN_4003134e`): the rest only pass the type to the accessors above. **[D]** for what
was read; an empty caller list is not evidence for the functions that reach the type through a virtual call **[O]**.

## What the ColdFire sends the DSP per track for an added type (for lane b) **[D]** (static, `vector_191_handler` `0x4002dd0c`, the 16-track loop that fills the TX frame at `0x80005348`)

- **The type is sent as it is.** TX frame word `0x94 + 2t` = the track's type byte (`DAT_80003cd0[0x9a t]`, the SRAM copy of the sound's type), no table, no clone mapping on the
  Digitakt II (the Digitone II has the `own_dsp` shim, finding 02). A run-time pluck on track 1 therefore puts 18 in word `0x94`.
- Also copied unchanged per track: blocks of the sound array (frame offsets `+0xda`, `+0xfa`, `+0x116`, `+0x130`, 20, 28, 26 and 10 bytes), which include the engine slots 25 to 34.
- **The only type-dependent extra is MANUAL SLICE's slice table** (frame words `+0xee .. +0xf8`), sent for type 6 (and for clones of 6 through `clone.6`). A clone of SAMPLE, like the pluck, gets none.
- Type-dependent data is selected **on the DSP**: `FUN_001c24e9` indexes the table at `0x2567c0` with the type (`DM 0x255970 + 2t` is the DSP's copy, set after the first
  frame that follows a trig) and stores the word as the per-track selector (`track_base + 0x4c`). For types 16 to 23 `core/machines` relocated that table and wrote 0 (the clone of the
  gap or reserved row), so the DSP chooses the SAMPLE path for the track; `core/dsp-voice` then looks at the type for its own dispatch. Stock words 8 and up are loader data pointers (finding 02), so a table
  that was **not** relocated would hand a pointer to the selector. The rung C image's plan (`out/patch/rungC-dt2-1.17.plan.json`) has `mf_selector` in `dsp.l2` with 0x60 bytes = 24 words (7 stock and 17 appended rows,
  `machines.count` 17), so types up to 23 are covered **[D]** (static; the table's contents on silicon are what lane b reads).
- No other ColdFire table that feeds the DSP depends on the machine type.

## Open

- **[O]** The sample picker keys of the SRC page for added machines (`DAT_401eda30`, above).
- **[O]** The device: the fix changes page 2 only; confirm on the Digitakt II that both pages show the knob labels and that the encoders name the machine's parameters.
- **[O]** A clone of SAMPLE, WERP, STRETCH, REPITCH or SLICE that keeps the clone's own knobs (xslice-like) could keep the stock sample view if `FUN_4005f6bc` returned `clone_of` at the three call sites; not done because pluck-like machines repurpose the slots the
  view reads as START, LENGTH and LOOP. A per-machine flag would be the way, if a module ever wants it.
