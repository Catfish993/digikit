# 61. Effect pages in the FX view (F4 step 2, 2026-10-24)

A run-time effect (`docs/design/digikit-dkm.md`, finding 55) can have a page of its own in the FX view of the Digitakt II, with up to 7 knobs and MIX, built from records
and no effect-specific code. This finding records the firmware facts the page machinery rests on (DT2 1.17; the hooks also resolve by hash on 1.16), how the values travel,
and what was measured. Marks: **[D]** measured or read once by the author (no second agent has checked the image bytes yet, so nothing here is **[V]**), **[O]** open.
Pieces: `modules/core/fxpages` (patch time), `modules/core/runtime/src/rs/src/fxpage.rs` (run time), `[fx]` in `devices/digitakt-ii.toml`, the EFFECT record
(`packages/patchkit/src/register.rs`), the scenario `tests/digikit/scenarios/fx-pages-dt2.toml` (`tools/patchkit/pk_digikit_fxpage.py`).

## The page machinery **[D]**

| What | DT2 1.17 | Notes |
| --- | --- | --- |
| page descriptor table | RAM `0x4293ba94`, 33 rows of `0x2c` bytes | built by the static constructor `FUN_401be1be` before the boot hook; a row is the name and the title as char pointers, then nine parameter codes (eight knobs and the level slot; 0 = empty cell). Page 30 is a sentinel; no row is free |
| page accessor | `FUN_400c898a(page) -> row`, `moveq 0x20,D1` (bound) and `addi.l #0x4293ba94,D0` | the only reader of the table's address; 18 callers, all view code |
| FX view page list | rodata `0x401f7848` = {13, 14, 15} (DEL, REV, CHO) | the view builder `FUN_40066584` pushes the count (`moveq 3,D1` at `0x40066d6e`) and loads the list (`move.l #0x401f7848,D0` at `0x40066d7c`); the page vector copies them. A page whose cells are all empty is not skipped, so the list must hold exactly the loaded effects' pages. The title shows `(n/N)`, N the list size |
| tab label | `FUN_40074d88`, called by `FxPageView::vfunc_4` | draws a hard-coded string for the pages 14, 15 and 13 (tests at `0x40074dc0..0x40074dce`); every other number draws no tab. The view's `vfunc` at `+0x88` returns the page number |
| filter graph | `FxPageView::vfunc_4` at `0x40074ff8` (`cmp.l D0,D5; beq.w`, D5 = 15) | the box over the knobs 5 and 6 is drawn on every page except CHO |

`core/fxpages` therefore does not relocate the table (as the first design had it): `fxp_acc` replaces the first six bytes of the accessor and answers the page numbers 33 and up
(one per effect slot) with rows of its own table; everything else runs the stock code. That avoids copying 33 rows at boot and any assumption about when the constructor has run.
`fxp_list` replaces the load of the list (and the pushed count), `fxp_label` the third page test of the tab label (an effect page pushes its row's name and runs the label code's
own tail), `fxp_graph` the compare before the graph. The four sites are anchors by function hash (`cf_func`, `cf.fxpages.*`), resolved on 1.16 and 1.17; the stock list and
count are anchors too (`cf.fxpages.stock`, `stock_n`), so the runtime copies them in front of its pages. With no effect loaded the FX view is pixel for pixel the view of an
image without the module, and the PCM is the same bit for bit (scenario `fx-pages-dt2`, parts `step1` and `stock`).

Rows: the three effect pages are 33, 34 and 35. The name and the title are std::string character pointers (the runtime builds the representations in its state, as for machine rows).
The level slot (the ninth code) is the page's MIX knob.

## Parameter rows and the value array **[D]**

- A parameter row is 15 words (finding 02): owner, mirror index, min, max, default, flag, `(id << 16) | kind`, key, two template words, name, a template word, short, formatter, a
  template word. For the FX parameters the owner is 0x12 (chorus), 0x13 (reverb), 0x14 (delay) and **the mirror index is the index in the kit's effect value array**: delay 0x20 to
  0x27, reverb 0x29 to 0x2f, chorus 0x19 to 0x1f (the array has 71 s16 values; 25 to 69 are these and the other stock effect parameters). The display class is the formatter word:
  `0x400e1588` is a plain 0 to 127, `0x400e17ac` a bipolar -64 to +63 (Delay "Stereo Width", code 0x73); the delay owner's rows are the templates (`[fx] template_*`: 0x75, 0x73 and the level
  row 0x79 for MIX). The raw value is an s16 from 0 to 0x7f00; the display shows raw / 256.
- The indices 1 to 24 are used by no stock parameter (and 70): the kit's values there are the LFO default pattern at start, nothing but copies touches them, and the DSP does not get them
  (`out/runs/lanes/f4-static.log`, probes A1 and E1 to E3: all 25 poked, PCM identical). Effect row k owns `value_indices[8k .. 8k + 8]` of the device file: knob j (word p_j) and, as the
  eighth, MIX. A kit load (Load Kit) resets them to 0 before the loader runs; a pattern change gives a new kit object, all 0 **[D]**: the runtime puts its values into a new kit object
  (and polls); keeping them across a Load Kit is persistence, the next step **[O]**.
- **The live kit is `*0x80004704`**, not the object of the firmware's kit accessor: `fw_kit_of(fw_project())` answered `0x44fb9d34` where `*0x80004704` was `0x426532b8`; the encoders
  edit the second, whose array (`+0x4534`) equals the SRAM mirror at `0x80003c42` index by index. Turning Delay Time changed index 32 of that array and nothing else **[D]**. So
  the device file has `[fx] kit_ptr` (the SRAM word) and `array_off`.
- The first knob row copies owner, flag, formatter and class from its template, takes the effect's index as mirror index and keeps the template's kind and key words (`write_fx_param_row`):
  the machine rows' `0x10 + slot` ids would collide with stock effect parameters. Several rows share one template, so one value-class object (`pf_t2` answers the template's); turning the GAIN
  knob changed the kit array at its index only (check in the scenario) **[D]**.
- An encoder detent moves the raw value by one step per slow event (about 0x1c) and by about 100 for a fast burst; the range 0 to 0x7f00 is therefore used at full resolution (a knob
  float is `min + (max - min) * raw / 0x7f00`, MIX is `raw / 0x7f00`: 0 and 0x7f00 are exactly 0.0 and 1.0).

## MIX is the adapter's **[D]**

`core/fx` row word 13 is the mix (a float, default 1.0 in the registry's field): the effect writes only the wet signal and the adapter makes `master = dry + mix * (wet - dry)`. Mix 1.0
copies the wet block (bit for bit), mix 0 leaves the master alone (the dry block bit for bit, the effect still runs and is checked), anything else blends per sample; in the fades the weight
is multiplied by the mix. Cost on the concrete core (`tests/patchkit/test_fx.py`): one effect steady 743 instructions per block at mix 1.0 (736 before: +7), 1142 at an intermediate mix
(+399), 605 at mix 0 (the copy is skipped); fades 1142. The empty chain (16), three free rows (31) and the all-off chain (178) are unchanged.

## Values to the DSP **[D]**

`fxpage::tick` runs once per UI-loop pass (`dkrt_tick`): when no load is in progress it reads the values of the loaded effects from the live kit's array and sends a changed one to the
effect's row, one word per transfer through window 2 of `core/dsp-link` (knob j to p_j as a float, MIX to word 13). The state `DKRT_FXP` shows it (`sent`, `errors`, `seeds`).
The scenario turns GAIN and MIX on the page and compares the PCM with the dry sound: the default 0.5 gives 0.5 x dry (0.50 ulp), GAIN 0.98 gives 0.98 x (1.97 ulps), MIX at 0.52
gives the linear blend (1.87 ulps against 1 + mix * (gain - 1)), MIX 0 the dry PCM bit for bit, MIX 127 the wet PCM again; deselecting the effect and restarting brings the three stock pages
(pixel for pixel) and the dry PCM (bit for bit) back. Screenshots (the fourth page: tab GAN, title `TEST GAIN (4/4)`, the GAIN knob, MIX on the level slot, `Gain=98` while turning) are
in the scenario's work folder; `DIGIKIT_SHOTS=DIR` copies them.

## Open **[O]**

- Persistence across Load Kit and project save (step 3): the values live in the kit array; a Load Kit zeroes them, and the poll then sends 0 to the effect (silence for a gain).
- A page whose effect fails to load its DSP part after the boot hook stays in the view (the view is built once); `remove` empties its cells.
- A second agent checks the sites against the image bytes before the marks become [V]. The Digitone II (step 5) needs its own page table, list and label code (finding notes in `out/runs/lanes/f4-static.log`).
