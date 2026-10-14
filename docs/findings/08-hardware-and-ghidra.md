# Hardware and Ghidra

Identifying the MCF5441x part, eDMA, the MMIO hook, and the Ghidra tooling: RTTI/code seeds, Version Tracking, and functions Ghidra misses.

## Stock Ghidra cannot decode `movclr`; a separate language fixes it **[V][C]**

- The MCF5441x is a V4m with EMAC, and EMAC instructions use line A
  (`0xAxxx`). The line-A words in the image are EMAC instructions, not
  traps. The software float routines are ordinary `jsr` calls (see "Why
  the emulator was slow").
- The handler at `0x4002d652` saves the EMAC state at `0x4002d67c`:
  `a988 a93c 0000 0000 ab84 af85 a1c0 a3c1 a5c2 a7c3 ad86` =
  `move.l MACSR,A0`, `move.l #0,MACSR`, `move.l ACCext01,D4`,
  `move.l ACCext23,D5`, `movclr.l ACC0..ACC3,D0..D3`, `move.l MASK,D6`.
  A `movem` of D0-D6/A0 follows. Digitone II 1.11 has the same bytes at
  `0x40025e60`.
- Register moves are one word. A `#imm` source adds a 32-bit extension,
  so `move.l #imm,MACSR` is 6 bytes (CFPRM).
- Stock Ghidra 12.1.3 (`68000:BE:32:Coldfire`) decodes most EMAC
  instructions but has no `movclr` constructor, so `a1c0 a3c1 a5c2 a7c3`
  decode as bad instructions and flow analysis stops there. It also copies
  `move.l ACCy,ACCx` in the wrong direction, picks the wrong accumulator
  for MAC and MSAC with load (CFPRM p.6-4), and prints `move.l Ry,ACCx`
  and the two ACCext moves with their operands swapped. An earlier reading
  in this file, that Ghidra stops at every EMAC word, was wrong. **[V][C]**
- The image has 96 `movclr` words (Digitakt II 1.15C) and 112 (Digitone II
  1.11), mostly in accumulator saves at handler entry. **[D]**
- `tools/coldfire/ghidra/ColdfireEMAC/` is a separate language,
  `68000:BE:32:ColdfireEMAC`, with those instructions fixed; see its
  README and `tools/coldfire/ghidra/install-coldfire-emac.sh`. Imported with it
  (`~/ghidra-projects/dt2-emac`, 98 s), `FUN_4002d652` is a 5796-byte
  function whose decompile shows
  `FUN_400cf9c4(0x802,&DAT_80005348,0xabc,0x8000488c)`. `FUN_400cf9c4`
  gets its two callers, error bookmarks drop from 64 to 41, functions rise
  from 10520 to 10526, and five checked functions keep their entry and
  size. **[V]**
- Unicorn 2.1.4 with `UC_CPU_M68K_CFV4E` runs `movclr`, `mac.l` without
  load, the MASK and ACCext moves and the handler prologue as the manual
  says (`tests/test_unicorn_emac.py`). Two gaps are pinned in that test:
  `move.l MACSR,Rx` keeps bits 31..12, and `move.l ACCy,ACCx` takes an
  exception. **[V]** A linear sweep finds no `move ACC,ACC` and no MAC with
  load in either handler (the 31 and 24 uses are elsewhere), so the
  handlers can run in Unicorn. **[D]** The frame build, though, reaches a
  MAC with load in `FUN_400db9aa` at `0x400db9e0`, which stock Unicorn
  2.1.4 cannot run; `patches/unicorn-2.1.4-m68k-emac-mac-load.patch` fixes
  it (see "The frame capture runs; the frame build is switched off"). **[C]**
- **2026-09-28:** Unicorn's fractional-mode EMAC is wrong (no `<< 1` on the
  product, unsigned operands, broken mode switch, among others); the
  ColdFire's parameter smoother runs in that mode. See finding 07,
  "Fractional EMAC was wrong in patched Unicorn". **[D]** *P0 2026-09-29:*
  the missing `<< 1`, the unsigned operands and the mode switch are **[V]**
  (manual plus a run of the compat case on the stock wheel), and **[C]** the
  fix is merged and installed.

## Names from RTTI and code seeds in the EMAC Ghidra project **[V][D]**

- `tools/coldfire/codeseeds.py` finds 31016 `jsr`/`bsr` calls to 5685 targets in
  the code ranges and 22 vector-table writes. `tools/coldfire/rttiscan.py` finds
  1052 typeinfo objects (class 122, si_class 728, vmi_class 125, pointer
  50, fundamental 23, function 4), 1883 vtables with 12349 slots, 4802
  instructions that load a vtable, and 47 `Class::method` strings, 41 of
  them loaded by code. Each takes under 2 s; the output is in
  `out/symbols/`. **[V]**
- Ghidra's own `RecoverClassesFromRTTIScript` refuses the program until its
  compiler is set to gcc, and then recovers no classes. **[D]**
- `tools/coldfire/ghidraapply.py seeds --analyze` on `~/ghidra-projects/dt2-emac`
  created 32 functions, rolled back 34 (an Error bookmark or no function),
  skipped 466 targets inside data, and named 19 interrupt handlers
  `vector_<n>_handler`. `tools/coldfire/ghidraapply.py rtti` created 3677 functions
  from vtable slots and renamed 6811: 5774 `Class::vfunc_N`, 1016
  `Class::ctor_dtor`, and 21 from `Class::method` strings, such as
  `Project::updateMirror` and the other seven `updateMirror` methods. 152
  slot functions shared by several classes stay unnamed. Functions went
  from 10526 to 14257, and Error bookmarks stayed at 41. The project before
  these writes is in `~/ghidra-projects/backup-2026-09-14/`. **[V]**
- `vfunc_N` is the slot index, not the method's name. `ctor_dtor` marks a
  function that loads a vtable address; it can be a constructor or a
  destructor.
- Spot checks: `0x4002d652` and `0x400d1378` are both
  `vector_191_handler`, and `FUN_400cf9c4` still has exactly those two
  callers. The vtable at `0x402015ec` (5 slots) belongs to
  `std::_Sp_counted_ptr_inplace<Digisharc::rpcMsgHeader_t, ...>`, and
  `0x401b6316` is that class's `ctor_dtor`. The earlier raw search above
  read the vtable one word late, as `0x402015f0` with 4 slots. **[V]**

## Digitakt II 1.16 in Ghidra: the same layout, 14743 functions **[V]**

- `uv run python -m emu.extract Digitakt_II_OS1.16.syx -o out/sections/dt2-1.16`
  writes a MAIN OS of 3,275,616 bytes for `0x40000400`, sha-256 `57bb4dfa…`
  (1.15C: 3,177,312 bytes). Digitone II 1.11 and 1.10E are extracted to
  `out/sections/dn2-1.11/` and `out/sections/dn2-1.10E/`. **[V]**
- `~/ghidra-projects/elektron-emac` holds two programs.
  `/dt2-1.15C/section_3_MAIN_OS.bin` is a copy of the `dt2-emac` program made
  by `tools/coldfire/ghidracopy.py`; a dump of the copy matches
  `out/ghidra/dt2-1.15C-emac/` in every count and in each function's entry,
  name, size, body, signature, callers and callees.
  `/dt2-1.16/section_3_MAIN_OS.bin` is a ColdfireEMAC import
  (`GHIDRA_FOLDER=dt2-1.16 tools/coldfire/ghidra.sh import`, 115 s), and
  `McfLabels.java` added 87 labels and 19 blocks to it. `dt2-emac` stays the
  1.15C project. **[V]**
- A headless `-postScript` cannot pack the program it processes:
  `saveToPackedFile` fails with "Unable to lock due to active transaction",
  and the analyzer then saves the program anyway. That happened once to
  `dt2-emac`; a dump made afterwards matched the 2026-09-14 dump exactly.
  `tools/coldfire/ghidracopy.py` packs the stored file and never opens the program. **[V]**
- The image has the 1.15C layout, moved. Main code ends with an `rts` at
  `0x401e97a8` (1.15C `0x401d6108`), and the next `0x400` bytes are a
  numeric table with no `rts` or `link` word. The typeinfos and vtables start
  at `0x401ec324` (1.15C `0x401d8c84`). Near the end, the `0x4305` bytes from
  `0x4030ccfc` equal those from `0x402f4c4c` in 1.15C, a shift of `+0x180b0`;
  they hold the 60 functions at `0x4030cd04-0x4030f89e`. The last byte that
  is neither `0x00` nor `0xff` is at `0x403117c3` (1.15C `0x402f9c13`). **[V]**
- `tools/coldfire/entryhist.py` over a dump made before the seeds gives the code
  ranges `0x40000400-0x401f0400` and `0x40300400-0x40310400`: 10915
  functions, none in between. On the 1.15C dump it gives
  `0x40000400-0x401e0400` and `0x402f0400-0x40300400`. The 1.15C defaults in
  `tools/coldfire/codeseeds.py` are wider, and their `0x402c0400-0x402f4400` holds no
  `rts` or `link` word. **[V]**
- Before the seeds, the 1.16 program has 10915 functions and 40 Error
  bookmarks. With the ranges above, `tools/coldfire/codeseeds.py` finds 31726 calls to
  5345 targets and 22 vector-table writes, and `tools/coldfire/rttiscan.py` finds 1066
  typeinfo objects (class 122, si_class 738, vmi_class 129, pointer 50,
  fundamental 23, function 4), 1933 vtables, 4852 vtable loads, and 47
  `Class::method` strings, 41 of them loaded by code. **[V]**
- `tools/coldfire/ghidraapply.py seeds --analyze` created 32 functions, rolled back 7,
  skipped 56 targets inside data, and named 19 interrupt handlers (1.15C,
  with its wider ranges: 32, 34, 466, 19). `tools/coldfire/ghidraapply.py rtti` created
  3773 functions and renamed 6970: 5918 `Class::vfunc_N`, 1032
  `Class::ctor_dtor`, and 20 from `Class::method` strings, among them all
  eight `updateMirror` methods. 153 slot functions shared by several classes
  stay unnamed. Functions went from 10915 to 14743, and Error bookmarks
  stayed at 40. **[V]**
- The two vector-191 handlers are `0x4002dd0c` and `0x400cec70` (1.15C
  `0x4002d652` and `0x400d1378`). Each is installed by `move.l #handler,dN`
  then `move.l dN,$400002fc`. **[V]**
- The dump is `out/ghidra/dt2-1.16-emac/`; `ghidradump.py --image` names the
  image under `out/sections/`, so `image_matches_sections` is true. It is
  complete, with 16 decompile failures: the same 15 `MidiRpc*Response`
  `ctor_dtor` functions as in 1.15C, and one large function in each version
  (`FUN_401bdee2`; 1.15C `FUN_401ac1be`) that overflows the decompiler's
  response buffer. **[V]**

## Version Tracking carries 1.15C addresses to 1.16 **[V][D][O]**

- `tools/coldfire/ghidravt.py run` runs Ghidra's AutoVersionTrackingTask from
  pyghidra with the options of `AutoVersionTrackingScript.java` and a 32 GB
  heap, and exports the matches. The headless script, with its 2 GB heap, ran
  out of memory after 30 minutes in the duplicate function correlator on
  1.15C -> 1.16 and left the destination unchanged. The sessions are in
  `/vt/` of `~/ghidra-projects/elektron-emac`, the exports in `out/vt/`.
  **[V]**
- Digitone II 1.10E and 1.11 were imported the same way, with 13448 and
  14017 functions after seeds and RTTI; the dumps are
  `out/ghidra/dn2-1.10E-emac/` and `out/ghidra/dn2-1.11-emac/`. The
  Digitakt-to-Digitone run writes to a copy of 1.11,
  `/dn2-1.11-from-dt2/section_3_MAIN_OS.bin`. **[D]**
- Each accepted function association is one-to-one:

  | pair | seconds | accepted matches | function associations | source functions matched |
  |---|---|---|---|---|
  | Digitakt II 1.15C -> 1.16 | 1092 | 50,741 | 9778 | 68.6% of 14257 |
  | Digitone II 1.10E -> 1.11 | 897 | 47,267 | 9090 | 67.6% of 13448 |
  | Digitakt II 1.15C -> Digitone II 1.11 | 1046 | 43,414 | 8126 | 57.0% of 14257 |

  **[V]** All three runs end "with some apply markup errors", and the task
  logs nothing more about them in headless mode. **[D]**
- `tools/coldfire/vtcheck.py` checks the associations against the images and against
  dumps made before Version Tracking. For 1.15C -> 1.16, 2365 function bodies
  are byte-identical, 7332 differ at the same size, and 81 changed size. Ten
  sampled same-size pairs differ only in addresses and in branch targets that
  are themselves matched; five resized pairs are the same functions with real
  code changes. **[V]** Where both dumps give a name, 7 of 5097 names differ,
  and the callees agree for 5913 of 5925 functions. **[D]**
- Some matches are wrong. `TransposeConfigMenuView::vfunc_2` ->
  `BreakOutBoxRoutingMenuView::vfunc_2` (in both Digitone runs) and
  `SamplerLedView::vfunc_17` -> `ArpSetupMenuView::vfunc_18` (1.15C ->
  Digitone II 1.11) differ in size by 43-50% and share only boilerplate.
  Check a match before relying on it. **[V]**
- `Velocity::vfunc_18` in 1.15C is `Velocity::vfunc_19` in 1.16: the
  function is unchanged, and Velocity's vtable grew from 23 to 24 slots. A
  `vfunc_N` number can shift between versions. **[V]**
- Other name differences are storage structures with new version numbers:
  `projectStorage_v4_t` -> `projectStorage_v5_t` and `projectStorage_v15_t`
  -> `projectStorage_v16_t` in Digitakt II 1.16, `kitStorage_v3_t` ->
  `kitStorage_v4_t` and `patternStorage_v3_t` -> `patternStorage_v4_t` in
  Digitone II 1.11. **[D]**
- The frame link on 1.16, carried by the 1.15C -> 1.16 run and checked
  against both images:

  | 1.15C | 1.16 | evidence |
  |---|---|---|
  | `0x4002d652` vector-191 handler | `0x4002dd0c` | calls the driver at `0x4002dd74` (1.15C `0x4002d6ba`) |
  | `0x400d1378` vector-191 handler | `0x400cec70` | calls the driver at `0x400ceccc` (1.15C `0x400d13d4`) |
  | `FUN_400cf9c4` DSPI2 driver | `FUN_400cd2bc` | its callers are the two handlers, in both |
  | `FUN_400cef6c` SHARC boot routine | `FUN_400cc864` | 1162 bytes in both |
  | `FUN_4002d602` | `FUN_4002dcb2` | 48 bytes in both |
  | call at `0x400330f6` in `FUN_40032f5a` | `0x4003395c` in `FUN_400337ba` | `jsr` to the function above; the task changed size |
  | `FUN_400db9aa` | `FUN_400d92a2` | both return `0x80005b50` |

  `FUN_4002d63e` -> `FUN_4002dcee`, called by the vector-191 handler, gains
  a bound check (`cmp #0xf`) in 1.16. **[V]**
- Not carried: the stop-flag writer `0x4002d632` lies outside any function,
  and `FUN_400caf48` has no match. The gate variables and the frame tables are
  RAM addresses, outside the image; they are to be re-found from the
  functions above. **[O]**
- The 1.15C -> Digitone II 1.11 run agrees with "Digitone II 1.11: the same
  link and the same machine table shape": 1.15C's `0x400d1378` maps to the
  1.11 test handler `0x400d0f90`, and `FUN_400db9aa` maps to `FUN_400db12a`,
  which returns `0x800068e4`. 1.15C's handler `0x4002d652` and driver
  `FUN_400cf9c4` have no match there, although 1.11's `FUN_40025e36` and
  `FUN_400cf7be` have the same roles, and the driver's callers are the two
  handlers in both. The 1.11 handler is 7582 bytes against 5796, and its
  driver compares the TX length with `0xaf0` where 1.15C uses `0xbc0`.
  **[V]**

## Digitakt II 1.17 and Digitone II 1.12: a relink plus one new menu, and an in-house function matcher **[D][V][O]**

Facts measured 2026-10-13 (lane `b`, patchkit spike 0.1). Marks below.

- **What changed.** Against 1.16 and 1.11 only section 3 (MAIN OS) and 5 (META) differ;
  sections 2, 4, 7 (the SHARC+ blob) and 8 are byte-identical, so the DSP image, the idle-loop
  PCs and the DSP databases carry over (`out/sharcdb/dt2-1.17.sqlite`, `dn2-1.12.sqlite`). **[V]**
  The only new class in RTTI is `BreakOutBoxOutputRoutingMenuView` (with its `shared_ptr`
  control block), on both products: 1066 -> 1068 typeinfos on DT2, 1047 -> 1049 on DN2. Of 14441
  matched functions (DT2) 14421 are byte-identical after masking addresses and 8 have a changed
  loose hash; 436 destination functions (46 KB, 14 of them in the new class) and 302 source
  functions (27 KB) have no counterpart. Matched entries move by a few constants (`+0x164`,
  `+0x2dc`, `+0x2e0`, `+0x416`, `+0x540`: insertions at about five places; DN2 `+0x41a`), not
  at random. **[D]**
- **Emulator.** `devices/*.toml` gained the two firmware entries (sha256, MAIN sha256,
  `main-os-oracle-v1`, `elektron-rtos-v1`, readiness contract, DSP block copied from 1.16 / 1.11;
  `plusdrive_project_contract` for DT2). All signature-resolved anchors of the boot path resolve
  on both with no code change (`boot_anchors_resolve_on_every_supported_firmware`: softfloat ABI,
  RAM clear, fused loops, idle spins); `emu/symbols.py` leaves the same nine (DT2) / eleven (DN2)
  symbols unresolved as on 1.16 / 1.11, all on `tests/test_symbol_audit.py`'s allow-list. Both boot
  to ready natively: DT2 1.17 ready at 1,114,985,926 ColdFire instructions, DN2 1.12 at
  851,236,338. **[D]**
- **Two things were version-keyed and are not any more.** `supported_softfloat` was a
  `("dt2","1.16") | ("dn2","1.11")` match; it is now the profile field `softfloat_abi`. The built-in
  project stream that `native/plusdrive` takes from MAIN sat at a fixed offset (`0x25edd8`); in
  1.17 the same bytes are at `0x25f314`, so the stream is found by structure (length, byte sum,
  container check). Without that the DT2 card builder fails with `StreamOutOfBounds`. **[V]**
  `Registry::load()` reads `devices/*.toml` at run time (`$DIGI_DEVICES_DIR`, else `devices/` of the
  working directory or an ancestor of the executable) and falls back to the embedded copy, so a new
  firmware hash needs a TOML edit and no rebuild. **[D]**
- **Scenarios** (ready inputs from the .syx alone, `snapshots/dt2-117-ready`, `dn2-112-ready`).
  DN2 1.12: three of the four pinned PCM hashes (`exactness-100m`, `trig1-taps`,
  `trig1-tap-release`) equal the 1.11 pins; `trig1-noqa-40m` (a 40 M-instruction validation
  window) and the unpinned `dn2-rec-play` differ, which is what a different ColdFire timeline gives
  with the same DSP. All 61 DN2 scenario oracles pass. DT2 1.17: the five `dt2.json` scenarios
  run (no pins in that file); the DT2 FX oracles give 99 passed and 36 failed, all in
  `test_readback` and the tests built on it: the encoder-turn scenarios end with different
  parameter values (`tf-br-22` reads 37, `tf-over-127` reads 97). The RAM and SRAM addresses of the
  peeks did not move (below), so this is a behaviour change or a different UI state at the ready
  point, not a stale address. **[D][O]**
- **Matcher.** `tools/coldfire/cffacts.py` hashes the instruction stream of each function with the repo's
  ColdFire decoder (`cfdis --ranges`): strict (relink-moved addresses and branch targets masked),
  loose (all numbers masked), mnemonics only. `tools/coldfire/cfdb.py` stores them (`func_hash`, DB_VERSION
  2, plus RTTI `typeinfos` / `vtables`). `tools/coldfire/cfmatch.py` pairs unique hashes, then strings, then
  vtable slots, call-graph neighbours and address-order gaps, repeating until nothing changes.
  It runs in about 8 s; Version Tracking took 1092 s with a 32 GB heap.

  | pair | matched | strict | loose | strings | mnem | vtable | callee | caller | gap |
  |---|---|---|---|---|---|---|---|---|---|
  | DT2 1.15C -> 1.16 | 13924 / 14257 (97.7%) | 5681 | 229 | 186 | 3 | 3206 | 1383 | 457 | 2779 |
  | DT2 1.16 -> 1.17 | 14441 / 14743 (98.0%) | 6226 | 12 | 152 | 3 | 3264 | 1354 | 483 | 2947 |
  | DN2 1.10E -> 1.11 | 13109 / 13448 (97.5%) | 4917 | 320 | 191 | 5 | 3119 | 1422 | 449 | 2686 |
  | DN2 1.11 -> 1.12 | 13724 / 14017 (97.9%) | 5703 | 8 | 147 | 2 | 3212 | 1357 | 476 | 2819 |

  Against the Version Tracking exports (function associations, `out/vt/`): 1.15C -> 1.16 agrees on
  9762 of 9769 shared sources (precision 99.93%) and finds 99.84% of the 9778 associations; 1.10E ->
  1.11 agrees on 9054 of 9074 (99.78%), recall 99.6%. The matcher adds about 4100 pairs that Version
  Tracking did not make. Version Tracking is not ground truth, so three independent checks: (a) for
  the pairs where both images have an RTTI-derived name, the class agrees in 6735 of 6747 (99.8%; the
  `_vN_t` storage-version rename counts as the same class; the rest are byte-identical sibling
  getters); per stage the weakest is `gap` (1455 of 1461). (b) The 68 symbols that `emu/symbols.py`
  resolves by its own signatures in both images: 62 map to the right address on 1.16 -> 1.17 and 61
  of 67 on 1.11 -> 1.12, none wrong, the rest unmapped (fixed RTOS addresses outside any function,
  two char* tables, one address inside a rewritten function); on 1.15C -> 1.16 one is wrong
  (`transport`, which `emu/symbols.py` itself says is unverified). (c) the 86 addresses of
  `tools/coldfire/cf_names.py`: 85 carry to 1.17 (`tools/coldfire/cf_names_dt2_1_17.py`, generated, all [D]); 45 moved,
  40 did not. SRAM (`0x8000xxxx`) and the heap-side globals (`0x43...`, `0x44...`, `0x47...`,
  `0x405b...`) are at the same address in 1.17; only data inside the image moved.
  `machine_display_name_table` is not carried (no instruction refers to it by a literal). **[D]**
- Pipeline for a new firmware, all serial in one JVM: `tools/coldfire/ghidra.sh import` (the program in its
  own folder of `elektron-emac`), `tools/coldfire/entryhist.py` on a `--limit 1` dump for the code ranges,
  `codeseeds.py` / `rttiscan.py`, `ghidraapply.py seeds --analyze` and `rtti`, `ghidradump.py --rtti`,
  `cfdb.py build`. 1.17: 10921 functions after import, 11098 after seeds, 14877 after RTTI; 1.12:
  10261 -> 14155. The matcher needs the dump only for function boundaries; the names it ports
  (`cfmatch.py port`) go into the `ported` table of the destination cfdb. `pyghidra` is not in the
  venv: run the dump and apply tools with `uv run --with <ghidra>/Ghidra/Features/PyGhidra/pypkg/dist/pyghidra-3.1.0-py3-none-any.whl
  --with .../jpype1-1.5.2-cp312-cp312-macosx_10_9_universal2.whl python -m tools.coldfire.ghidradump ...`. **[D]**
- **[C] The 36 DT2 FX oracle failures are not a 1.17 change** (lane fastweb, 2026-10-13). DT2 1.16 fails the
  same 36 tests when its ready state is rebuilt today, and for 1.16 and 1.17 all 60 FX scenarios give identical
  decoded Sound/Fx read-backs and identical PCM hashes on the same recipe (`tools/emulator/dt2fx_compare.py`). **[V]**
  The cause is the ready state. The oracle intents (encoder-turn counts, `UPS` in `tools/emulator/gen_dt2fx.py`) were
  calibrated on a DT2 ready snapshot booted before software-forced INTC delivery became the default
  (b8d45a7, 2026-10-13 07:27). A snapshot booted with it on has a different CPU phase at ready; the encoder
  acceleration depends on the phase (ladder for `lad-N`, Bit Reduction after N events of +4, N = 12, 14, ...
  on the old and the new snapshot: `11 11 16 16 32 32 22 38 38 54 54 87 ...` against `7 12 12 20 20 37 37 26 43 43
  59 59 93 ...`, the same curve shifted by one slot), so `tf-br-22` ends at 37 instead of 22. Each snapshot is
  used by the 1.16 and 1.17 tests only through `snapshots/<ready>/by-card/<card sha>[-<syx sha>]`; since f75da50
  keys 1.16 snapshots by firmware too, every fresh 1.16 snapshot was a new, forced-IRQ boot. **[V]**
  Evidence: the old 1.16 snapshot (`by-card/1c1fed346ea9`, 90,114,376 bytes, 2026-10-08) gives 22 for `tf-br-22`
  and 272,149,997 guest instructions, a fresh one gives 37 and 271,899,997. A DT2 ready state booted with
  `DIGI_INTC_FORCE=0` and run with the default (forced delivery on) passes all 146 tests of
  `test_dt2_fx.py --device dt2 --slow --sweep` on 1.16 and on 1.17. **[V]**
  Fix: `tools/emulator/ready.py` boots ready states with `DIGI_INTC_FORCE=0` (recorded as `intc_force: false` in
  SOURCE.json). The DN2 1.12 ready state built that way also keeps all four pins of 1.11
  (`exactness-100m 826db0d1`, `trig1-noqa-40m d56d8f4b`, `trig1-taps 8b26c70c`, `trig1-tap-release f4f6d19f`), so
  the "3 of 4 pins" above had the same cause. **[V]** Existing ready directories built before the fix
  (`snapshots/dt2-117-ready`, `dn2-112-ready`, the 1.16 `by-card/*-278541e4`) must be rebuilt
  (`tools/emulator/ready.py ... --force`) to pass; a rebuilt 1.17 DT2 set is in `out/ready-fix-117`. No oracle or pin was
  changed. `tools/emulator/dt2_sample_check.py` takes `DIGI_DT2_CARD` to pin a card image. The mislabelled
  `by-card/1c1fed346ea9/SOURCE.json` (says 1.17, holds the 1.16 Oct-8 snapshot) comes from `ready.py` rewriting
  SOURCE.json when it reuses an existing snapshot. **[D]**
- **`dt2-sample-trig-noqa` had an empty PCM hash** on a ready state booted without forced delivery and run with
  it: `step_error: unexpected interrupt before idle advance deadline` after 15,171 instructions. The idle
  fast-forward (`advance_idle`) crossed a software-forced interrupt that the CPU could take; it now declines when
  `Time::forced_deliverable()`. With the fix the scenario runs to 237,749,990 instructions (172,864 PCM values).
  **[V]** The run is silent (0 non-zero samples, hash 892660e6...): without the NO prelude the boot modal is
  still open and takes the trig. That is the scenario's purpose, not a fault; it is not pinned. **[D]**
- Open: the new `BreakOutBoxOutputRoutingMenuView` has not been read. **[O]**

## What we were overlooking about ColdFire: eDMA **[V]**

Feeding bytes to the UART model in `emu/console.py` could never have produced
console input, because **the firmware never reads UDR8 to receive**. UART8
receive is done by eDMA channel 34, with no CPU involvement per byte.

Checked and ruled out first: the PIT counter registers (`PCNTR`, `0xFC08x004`)
are **never read** by MAIN OS, so they do not need modelling. eDMA is a
different story -- 14 channels are configured.

From the init at `0x40002516`:

    TCD34.SADDR  = 0xEC07000C     ; UDR8, fixed (SOFF = 0)
    TCD34.ATTR   = 0x0050         ; DMOD = 10 -> destination modulo 1024
    TCD34.DADDR  = 0x4FE1A000     ; a 1024-byte ring
    TCD34.NBYTES = 1              ; one byte per request

and the ISR at `0x40001f1a`, vector 154:

    idx  = [0x4094CDA4]                        ; consume index
    base = [0x4094CD84]                        ; ring base
    while base + idx != [0xFC045450]:          ; DADDR = live write pointer
        byte = ring[idx]; idx = (idx + 1) & 0x3FF
        [0x4094CDB4](byte)                     ; registered callback

The ATTR decode (destination modulo 1024) matches the ISR's `andi.l #$3ff`
exactly, which is what confirms the reading.

**The channel's own DADDR register is the producer pointer**, polled by the
ISR. So injecting input needs no general eDMA emulation -- write into the
ring, advance DADDR with the same modulo, raise vector 154. That is
`emu/serial.py`, and it works: feeding `#HELLO\r\n` drives the RX callback
exactly 8 times, the consume index advances 0 -> 8, and the bytes are enqueued
onto the serial message queue at `0x47D9ADC0` (count 6 -> 8).

TCD35 is the matching transmit channel; the ISR at `0x40001d00` (vector 180)
is UART8 **transmit** only, pulling from a ring at `0x4094CD80`.

### The remaining console blocker, one step further on **[O]**

Nothing drains `0x47D9ADC0` -- it already holds 6 unconsumed messages before
any input is injected. Its consumer is the task at **`0x401136EE` (prio 3)**,
one of the six that never get created. It is created lazily by the singleton
at `0x401134CC` (guard `0x44F1E070`, allocation via `0x401114A8`) on first use
of the serial service, and nothing in our boot ever asks.

`emu.serial.create_serial_task` runs that initialiser and the task **is**
created (`entry=0x401136ee prio=3 tcb=0x44dfccb4`, an 11th task). It has not
been observed draining the queue yet -- created is not the same as started and
scheduled, and that is the next thing to check (whether `task_start`
`0x40001314` runs for that TCB, and whether the scheduler ever selects it).

So the chain is now fully mapped and only its last link is missing:

    DMA ch34 -> ring 0x4FE1A000 -> vector 154 -> callback 0x40110F20
      -> queue 0x47D9ADC0 -> [task 0x401136EE, not draining]
      -> queue 0x40388EAC -> console task 0x400CD594 -> dispatch 0x400CD93E

### Other ColdFire details worth knowing

- `raise_vector` does not set SR on exception entry. Real ColdFire sets S,
  clears T and, for interrupts, raises the mask. Most ISRs here begin with
  `move.w #$2700,sr` themselves, but the PIT3 ISR at `0x400d2d70` does not, so
  this is a latent reentrancy difference rather than a proven bug.
- The exception frame's format field is written as 0; ColdFire uses 4 for a
  normal 2-longword frame. Harmless here because `rte` is implemented by hand
  and ignores it.

## The MMIO hook was global too **[V][C]**

The earlier conclusion "every hook in this project is free" was wrong, and
wrong for a methodological reason worth remembering: it compared a *minimal*
machine against a *fully hooked* one, but `install_mmio()` was in **both**, so
its cost cancelled out and never appeared in the comparison.

`install_mmio` registered `hook_add(UC_HOOK_MEM_READ, on_read)` with no
`begin`/`end` -- a Python callback, plus a loop over the mmio dict, on **every
memory read the firmware makes**. Exactly the same mistake as
`install_isa_patches`, which had already cost 3.2x.

There are only three MMIO addresses, so one narrow hook each:

| configuration | throughput |
|---|---|
| global hook (as shipped) | 2.15M instr/s |
| scoped per-address hooks | **2.86M instr/s** (1.33x) |
| no MMIO hook at all (ceiling) | 2.91M instr/s |

Scoped lands within 2% of the ceiling, so this is the whole of that cost.

**But it barely moves the GUI**, and that is the interesting part: on the
fully-emulated path it is worth 1.33x (1.32 -> 1.50 fps end to end), while on
the softfloat+bitmap HLE path the GUI actually runs it is worth ~3% (3.99 ->
4.10 fps). With HLE we execute 5.5x fewer instructions, so there are far fewer
memory reads to tax, and the bottleneck has moved from TCG to Python callback
dispatch -- ~88k HLE calls per 12 frames. Further speed has to come from
making those callbacks cheaper or fewer, not from removing more hooks.

Ordering hazard, now fixed: `restore_into` merges the snapshot's own mmio
entries, so `install_mmio` has to run *after* the restore or a
snapshot-carried address gets no hook. `longrun.build` does that now.

## The part is an NXP MCF5441x (ColdFire V4m) **[V]**

Established from the peripheral map the firmware itself uses, which is an
exact fingerprint:

| base | module |
|---|---|
| `0xEC070000` | UART8 (a part needs 10 UARTs for UART8 to live here) |
| `0xEC094000` | GPIO |
| `0xFC044000` | eDMA, TCDs at +0x1000 |
| `0xFC048000` / `0xFC04C000` / `0xFC050000` | INTC0 / INTC1 / INTC2 |
| `0xFC05C000` | DSPI0 |
| `0xFC080000`-`0xFC08C000` | PIT0-PIT3 |
| `0xFC090000` | EPORT |

### Flash and DDR capacity **[V]**, DDR corrected **[C]**

Both come out of the firmware's own code; neither needs a datasheet or a probe.

DDR is **128 MiB** **[C]** **[V]** (an earlier version of this section said 64 MiB),
from the bootstrap's own DDRMC writes and the OS's own use of RAM:

```
DDR_CR04 @0xFC0B8010 = 0x00010101   ; bit 8 8BNK=1     -> 8 banks
DDR_CR15 @0xFC0B803C = 0x02000103   ; ADDPINS=2        -> rows = 16-2 = 14
DDR_CR16 @0xFC0B8040 = 0x02000407   ; COLSIZ=2         -> cols = 12-2 = 10
```

with the controller's fixed 1 chip select and x8 datapath: `2^(14+10+3) * 1 =
134,217,728`, the geometry of a standard 1 Gbit x8 DDR2 part (14 row, 10 column
bits, 8 banks). The register values and the single x8 component are as before;
the correction is the row limit. The reference manual contradicts itself: the
MAXROW field reads 0x10 (16 row pins), while 21.5.2.2 lists 15 rows. With 15 the
result is 64 MiB, which the firmware rules out (a 123.09 MiB clear and the reset
stack at 0x48000000 would wrap) **[V]**:

- the reset entry sets the stack pointer to `0x48000000` (`movea.l
  #0x48000000,a7` at `0x400004f2`) and the bss clear runs to `0x47e28470`; on
  64 MiB the clear would wrap into the running image;
- statics and the 32 MiB C++ heap sit above `0x44000000` (heap arena
  `0x44f39c40-0x46f39c40` on DT2 1.16; framebuffers at `0x44e4a5b8`);
- ACR0 (`0x4007e020`) maps exactly `0x40000000-0x47ffffff`, 128 MiB.

Addresses `0x48000000-0x4fffffff` are an uncached alias of the same DDR (ACR0
does not cover them); the firmware's DMA buffers are reached through it (see
01, "RAM map"). `tools/coldfire/ddr_geometry.py` now uses 16 as the maximum row bits and
prints 128 MiB for both devices. A second check would read the DDR_CR15 field
width off the MCF5441x part's own register table, or find a write above
`0x44000000` that a 64 MiB part would alias onto the image.

NOR flash is **16 MiB**. The bootstrap issues RDID (`0x9F`) and dispatches on
the 5 ID bytes at `FUN_800024ec`; only the branch matching mfg `0x01`, id
`0x2018`, ext `0x00` — an S25FL127S-class part, 128 Mbit — selects the
512-byte page and 256 KB erase geometry the flash loop actually uses. Weaker
than the DDR result by one step: the firmware *recognises* the part, it never
computes a capacity. **[V]/[D]**

Against a store-only repacked image this is not close. What is staged and
flashed is the decoded container, **4.00 MB** against a stock 1.35 MB — the
5.07 MB `.syx` figure includes 8-in-7 transport framing that never lands in
memory. Headroom is 16.8x on DDR and 4.1x on flash. A real LZ77 packer is not
needed. That everything past the OS container to the end of the chip is free
is an assumption; no partition table has been located. **[O]**

**V4m, not V4e** -- MMU and EMAC but **no FPU**. That is the real reason 93%
of executed instructions were soft-float: it is not a compiler flag, the part
has no hardware float. (We run Unicorn as `UC_CPU_M68K_CFV4E`, a superset;
harmless because the firmware never issues FPU instructions.)

One loose end: the firmware's own bus-clock constant is 132 MHz
(`0x07DE2900`), while the datasheet headline is 250 MHz core. If the bus were
core/2 that implies a 264 MHz core, slightly over the published maximum. The
15 fps result does not depend on resolving this -- the PIT and UART share a
clock domain and we used the firmware's own constant, cross-checked by three
timers landing on round rates.

## Ghidra misses functions that are only ever pointed at **[V]**

Ghidra records a data reference to an address held in an immediate and stops
there. If nothing ever reaches that address with a `jsr`, no function is
created, so it gets no decompilation and does not appear in `decomp/` at all --
a blind spot that is silent rather than noisy, because a `rg` over the dump
returns nothing and looks like a clean negative. Two hours were lost to this on
2026-09-16 before the dispatcher at `0x40042fe2` was found by
`tools/coldfire/refscan.py`.

Measured on Digitakt II 1.16, MAIN OS sha-256 `57bb4dfa…`:

- Decoding is not the problem. Of the code-dominant region
  `0x40000400`-`0x401e97a7` (2,003,879 bytes, 61% of the image; the rest is a
  1.19 MB string/RTTI/data blob), **99.92%** decodes cleanly -- 1,546 undecoded
  bytes in 339 spans, 242 of them exactly two bytes. A sample of the largest
  and of ~25 scattered spans found no failed instruction: they are switch
  displacement tables (`0x40021fe4` sits right after a
  `jmp $21fe4(pc,d0.l)`) and runs of one repeated word. **[V][C]**

  The 96.78% figure quoted in earlier handovers is a whole-image number. A
  linear sweep of the data blob still "decodes" ~89% of it into plausible
  instructions, so that average says nothing about decoder quality. Writing a
  better ColdFire SLEIGH language would recover nothing. **[C]**

- Function discovery is the problem. **6.87%** of that code region, 148,862
  bytes, sat outside every `function_ranges` entry. Of the twenty largest
  in-code gaps, none look like data and at least eleven open with a prologue.

- **Ghidra's own analyzers recover none of them.** On a copy, with
  `tools/coldfire/ghidraopts.py`, `Function Start Search` was already on;
  `Function Start Search.Search Data Blocks` and `Aggressive Instruction
  Finder` were off. Turning both on and re-analysing: functions 14,743 ->
  14,743, and the two entry sets are identical in both directions. Code
  coverage unchanged at 93.43%. `0x40042fe2` still had no function. The only
  effect was seven more Error bookmarks. **[V]**

### Seeding them from the pointers **[V]**

`tools/coldfire/codeseeds.py` gained a third candidate kind, `pointers`: an address
taken as an immediate (`#$X` in any instruction) or by `pea $X.l`, which lands
in a code range on a function prologue -- `lea -N(a7),a7` (`0x4fef` with a
negative displacement), `link.w aN,#-d` (`0x4e50`-`0x4e57`), or a `movem.l`
save (`0x48e7`, `0x48d7`). `tools/coldfire/ghidraapply.py seeds` applies them through
the same `ensure_function` guards as the call targets, which skip a target
inside defined data or mid-instruction and roll the edit back if it raises an
Error bookmark.

```
uv run python -m tools.coldfire.codeseeds out/sections/dt2-1.16/section_3_MAIN_OS.bin \
  --base 0x40000400 \
  --code 0x40000400 0x401e97a8 --code 0x4030cd04 0x4030f89d \
  --json out/symbols/dt2-1.16-seeds.json
uv run python -m tools.coldfire.ghidraapply seeds out/symbols/dt2-1.16-seeds.json \
  --project ~/ghidra-projects/elektron-emac --project-name elektron-emac \
  --program /dt2-1.16-seeded/section_3_MAIN_OS.bin --analyze
```

The 1.16 code ranges above come from `function_ranges` (functions span
`0x40000410`-`0x4030f89d` with one 1.19 MB gap); `codeseeds.py`'s defaults are
1.15C's. Result: **[V]**

| | baseline | seeded |
|---|---|---|
| functions | 14,743 | **14,857** (+114) |
| entries missing from the other set | 0 | 0 |
| code coverage, `0x40000400`-`0x401e97a7` | 93.43% | **94.37%** |
| Error bookmarks | 40 | **40** |
| decompile failures | 16 | **16** (the same 16) |

1,067 pointer records to 921 distinct targets, 816 of them not a call target;
464 `link`, 456 `lea`, 1 `movem` by distinct target. Most already had functions
by another route, and 114 were new. The dry run predicted 114 and the real run
created 114.

Nothing suggests junk: Error bookmarks and decompile failures both held exactly
still, no baseline function disappeared, and the new functions run 40 to 4,694
bytes with none under 8. `0x40042fe2` came back as a single clean 558-byte
range, `0x40042fe2`-`0x4004320f`.

Worth noting as a cross-check: an independent sweep for addresses that are
data-referenced, not a function entry, and start with a prologue predicted
**115**; the pipeline created **114**. Two methods, written separately, landing
one apart.

The remaining ~130,000 uncovered bytes are mostly gaps whose first bytes are
alignment or tail data rather than the entry, plus a repeated non-standard
prologue idiom (`8f2f 0a2f 0224` after a varying first word) that the three
patterns above do not match. **[O]**

## A queryable ColdFire database and a snapshot reader **[D]**

2026-09-28. Two read-only helpers, the ColdFire counterparts of
`tools/sharc_dev/sharcdb/`/`tools/sharc_dev/sharc.py` (usage in CLAUDE.md):

- `tools/coldfire/cfdb.py build out/ghidra/dt2-1.16-emac` imports a
  `tools/coldfire/ghidradump.py` dump into `out/cfdb/<image>.sqlite` (functions,
  call/jump edges, data references, strings). `tools/coldfire/cf.py` queries it
  (`cf.load("dt2-1.16-emac")`: `func`, `callers`, `callees`, `reach`,
  `paths`, `refs_to`, `decomp`, `sql`). Ghidra's tables miss code outside
  functions (for example the "Load all samples" invoker `0x40030ce8`), so an
  empty caller list still needs `tools/coldfire/refscan.py`.
- `tools/coldfire/cf_names.py` is a hand-curated table of 1.16 addresses (live
  kit/track records, FlexBus, sample loader, boot UI) with a mark and a
  source for each; `tools/coldfire/cf.py` and `tools/emulator/snapread.py` resolve names
  against it. Entries are **[D]** unless a finding marks them **[V]**.
- `tools/emulator/snapread.py` reads registers, memory and task state from a `.snap`
  without building a Machine (`Snapshot(path).u32(addr)`, `.read`, `.regs`,
  `.tasks()`, or the CLI). `tools/snapeq.py A B` checks two snapshots for
  equal guest state (finding 07, cold-boot ladder speed).
- *P0 2026-09-29:* `cf.load("dt2-1.16-emac")` with `callers`/`func`, and
  `Snapshot(path).u32`/`.read`/`.tasks()`, work as described (used for the
  P0 checks). `callers(0x40030ce8)` is empty and `func(0x40030ce8)` is
  None, as stated; a raw scan finds its only literal at `0x4003124e`, in
  `FUN_400311d0`. `.tasks()` returns [] for snapshots saved without task
  tracking (for example `dt2-1.16-control/running.snap`).

