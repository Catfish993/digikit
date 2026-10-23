# 60. What fits: the needs of a module, inferred from its contents (DT2 1.17, DN2 1.12)

Plan: `docs/plans/what-fits.md` (F3). Rule and reader: `docs/design/digikit-dkm.md`, "Needs are inferred from the contents". Marks as in AGENTS.md: **[D]** measured or read once here; nothing is **[V]** (a second agent has not checked it).

## Result

**[D]** A module does not say what a load takes; the needs are worked out from the file. The first build of this feature had the packer write a needs block into the `.dkm` header (format 2). Em decided against it:
a contributed or hand-built module must not self-report, because a field in the file can be wrong or hostile and every reader would have to trust it. The runtime enforces the same limits at load anyway,
so the needs only had to be right as a display and a pre-check, and a function of the file cannot disagree with the file. Format 1 stays (header 0xa0 bytes, no needs block); every format 1 file has needs.

**[D]** The needs are computed once, by `needs_of` of the crate `packages/dkneeds` (no_std, no dependency), through a byte source: the device feeds it from store sector reads (header, `register` records, DSP section header and voice
table; no staging of the file), a host from a slice. Its callers: the DIGIKIT menu (the card's files, once per open of MODULES), patchkit (`patchkit needs [--json]`, a line of `dkm-info`), `patchkit.wasm` (`pk_needs`, the Transfer page through
`apps/web/src/modules/dkneeds.ts`) and the Python tools through the command line (`tools/device/dkneeds.py`). The fit compare (`fits_now`) stays in three languages on shared vectors (25 cases, 6 sets and the empty-capacity case in `sdk/fit-vectors.json`: Rust `dkrt_fit`,
`dkfit.ts`, `dkfit.py`); no arithmetic of the needs exists outside Rust.

**[D]** The inferred needs equal what the runtime takes when it loads the module, on both devices. `inferred-needs-dt2` loads each of 12 modules alone on a ColdFire-only rig (the repository's `pluck` and `remote` and ten fixtures; the three that fault on purpose
are left out) and `inferred-needs-dn2` each of 10; CAPACITY (arena, DSP area, voice rows, and for a module without DSP code the machine, parameter and page rows) and the MODULES record equal `needs_of` of the file in every case. The rows of a DSP module
come in after the DSP acknowledges: `isolation-*` checks them on a coupled load of `test/sine` (arena block only, every row).
Figures: `pluck` takes arena 32, arena while sent 1392, DSP area 265600 (259.4 KiB of the 512 KiB on the Digitakt II, of 320 KiB on the Digitone II); `test/sine` arena 32, 400, DSP area 512.

**[D]** Malformed files give an error, never a panic and never a read past the end: the Rust tests of `dkneeds` cut a file at every length, edit each of its first 512 bytes eight ways, and set every size and count field to values over its limit
(truncated DSP section, 33 records, 4097 relocations, state bytes and voice counts over the loader's limits, sizes that overflow `u32`). The menu shows no size and no verdict for such a file (it can still be ticked: the runtime names the reason at boot);
`dk run` refuses it ("not one the loader accepts") unless `--force`; the host list shows no fit tag.

**[D]** A ColdFire-only rig keeps the arena block of a DSP module's relocated image (the transfer never comes), so there `ARENA` used is `arena + arena_xfer`; after a coupled load it is `arena`. The fit rule counts both blocks at once,
because a boot can hold the images of several DSP modules until their transfers have run.

**[D]** The device menu's list shows about 18 characters of a row: 14.6 of 512 screenshot pixels per character, from x = 215 to 485 (the title column takes the left third). The row `[ ]x HALF-B   165K` is 17 characters;
the old 21-character row (`[X]*` and 17 of the name) was cut off at about the 15th. A popup shows about 29 characters on one line (`NO FIT: DSP 164K > 155K FREE` is 28). Screenshots: the `fits-dt2` and `fits-dn2` scenarios (`--keep`).

## What reading the needs costs the menu

**[D]** Opening MODULES reads the needs of every file, once per open. `menu-open-dt2` and `menu-open-dn2` open a store of 12 synthetic modules (four with DSP code, version 3 states and records; eight ColdFire-only of 2 to 6 KiB)
and, as a baseline, the same names as files that are not `.dkm` (the scan reads only their first sector), measured from the press that opens the page to the last row (`DK_LOAD_N`):

| | DT2 | DN2 |
| --- | --- | --- |
| emulated ms to open the page, needs read | 28.9 | 30.2 |
| the baseline (files that are not `.dkm`) | 27.6 | 28.8 |
| extra for the needs | 1.3 | 1.4 |
| extra busy ColdFire instructions | 178,200 | 191,400 |
| sector reads by the needs reader (baseline) | 16 (12) | 16 (12) |

The emulated card answers a sector read at once, so the milliseconds are the ColdFire work only (1.3 ms for 12 modules, at the model's 132 M instructions a second); the count of sector reads (4 more than the baseline's one per file) is the figure to multiply by a device's card latency. A file costs one read when its header, `register` records and DSP header lie in the first
512 bytes, and about one more per further sector they span; the reads are in file order and one sector is cached, so each sector is read at most once per file. The most a file can cost is bounded by its tables: the header sector,
at most 32 records of 0x50 to 0x6c bytes (about 7 sectors from the table's offset) and the DSP section's header and voice table (2 sectors), about ten reads; the menu lists at most 32 files, so at most about 320 reads and 320 x 6 K busy
instructions, once per open. The scenarios bound the extra emulated time at 25 ms for the 12 modules, twenty times the measured cost.

## What the rule does not model

**[O]** Fragmentation (a set is checked by taking each module's needs out of the limits in turn); the DSP area's first range starts at an offset that is not 8-aligned (the empty DSP area is taken as 8 bytes short, the arena 16 for its first block header, so the
verdict is not generous by more than the alignment room of one module); machine type numbers (the sticky map); a module's `init` that allocates more arena (`Api.alloc`); `bss` larger than the arena (the loader refuses it; the need says so as `ARENA` over the limit).

**[D]** The module set is fixed per session (hot reload is archived), so the menu checks a ticked set against the device's limits, not against what is loaded now: a restart loads the selection into an empty runtime. The host's per-module tag
uses the live CAPACITY (next to the loaded modules) and the tooltip says whether the module fits alone. Card files that are not in the library have no needs on the host (it reads only their header, as before); the menu has needs for every file of the card.
