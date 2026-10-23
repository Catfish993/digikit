# 60. What fits: the needs of a module, declared in its header (DT2 1.17, DN2 1.12)

Plan: `docs/plans/what-fits.md` (F3). Format: `docs/design/digikit-dkm.md`, "Needs". Marks as in AGENTS.md: **[D]** measured or read once here; nothing is **[V]** (a second agent has not checked it).

## Result

**[D]** The needs a packed module declares equal what the runtime takes when it loads it, on both devices. `needs-dt2` loads each of 12 modules alone on a ColdFire-only rig (the repository's `pluck`
and `remote` and ten fixtures; the three that fault on purpose are left out) and `needs-dn2` each of 10; CAPACITY (arena, DSP area, voice rows, and for a module without DSP code the machine, parameter and page rows)
and the MODULES record equal the header's block in every case. The rows of a DSP module come in after the DSP acknowledges: `isolation-*` checks them on a coupled load of `test/sine` (arena block only, every row).
Figures: `pluck` declares arena 32, arena while sent 1392, DSP area 265600 (259.4 KiB of the 512 KiB on the Digitakt II, of 320 KiB on the Digitone II); `test/sine` arena 32, 400, DSP area 512.

**[D]** A ColdFire-only rig keeps the arena block of a DSP module's relocated image (the transfer never comes), so there `ARENA` used is `arena + arena_xfer`; after a coupled load it is `arena`. The fit rule counts both blocks at once,
because a boot can hold the images of several DSP modules until their transfers have run.

**[D]** The device menu's list shows about 18 characters of a row: 14.6 of 512 screenshot pixels per character, from x = 215 to 485 (the title column takes the left third). The row `[ ]x HALF-B   165K` is 17 characters;
the old 21-character row (`[X]*` and 17 of the name) was cut off at about the 15th. A popup shows about 29 characters on one line (`NO FIT: DSP 164K > 155K FREE` is 28). Screenshots: the `fits-dt2` and `fits-dn2` scenarios (`--keep`).

**[D]** The fit rule is the same in Rust (`dkrt_sdk::fit`, the runtime and the menu), TypeScript (`apps/web/src/modules/dkfit.ts`) and Python (`tools/device/dkfit.py`): 25 cases, 6 sets and the empty-capacity case in `sdk/fit-vectors.json` give the same answers in all three.

## What the rule does not model

**[O]** Fragmentation (a set is checked by taking each module's needs out of the limits in turn); the DSP area's first range starts at an offset that is not 8-aligned (the empty DSP area is taken as 8 bytes short, the arena 16 for its first block header, so the
verdict is not generous by more than the alignment room of one module); machine type numbers (the sticky map); a module's `init` that allocates more arena (`Api.alloc`).

**[D]** The module set is fixed per session (hot reload is archived), so the menu checks a ticked set against the device's limits, not against what is loaded now: a restart loads the selection into an empty runtime. The host's per-module tag
uses the live CAPACITY (next to the loaded modules) and the tooltip says whether the module fits alone.
