# 24. +Drive export: reading the card back (Phase 1b)

Plan: `docs/plans/digikit-runtime.md`. Code: `packages/emulator/plusdrive/src/{reader,export}.rs`, the `plusdrive export`
command, `card.export` in `packages/emulator/control`. Run on DT2 1.16 (`out/sections/dt2-1.16`, source SHA-256
`278541e4...`, checked against `firmware/Digitakt_II_OS1.16.syx`), ColdFire only, 2026-10-16. One agent, not checked by a
second one: marks are **[D]** unless stated.

## What the firmware wrote **[D]**

Test: `packages/emulator/control/tests/card_export.rs` (card of three WAV files, boot to the main screen, NO, FUNC+SETTINGS,
YES, 3 s).

- Boot alone writes 28,685 overlay sectors: the working project at sector `0x50000` (project "slot 0x80", `FUN_4012eef2`
  special case) 28,364 sectors, the superblock sector `0x5D8000`, the first record page (64 sectors at `0x5D8180`, the
  factory link in the root) and 256 sectors at `0x5EFF80` (content pages `0x78`..`0x7F`: the root directory and index pages).
  The three samples read back byte for byte as the builder wrote them.
- The panel save writes project slot 0 (`0x58000`, 28,364 sectors = 14,522,368 bytes with the trailing zero sectors cut) and
  nothing else. The slot starts `BEEFBACE 00000005 "PRESETS 1643"` (v5 container); the stock boot project at `0x40000` is a
  `COKi` container the builder seeds. Slot 0 and the working project differ only from byte 14,521,861 on. The boot copies
  `0x40000` and `0x48000` are not rewritten.
- **The MmcFs occupancy bitmaps (sector `0x800`) are not updated by a panel save** (no write to that sector in 20 s of
  emulated time). The callers of the bit setters are the factory format and slot erase (`FUN_40131282`, `FUN_401302ee`), not
  the save. The exporter therefore decides occupancy from the slot content (first sector neither all zero nor all `0xFF`) and
  reports a set bit on an empty slot. **[O]** What the bit polarity means (`FUN_40130484` toggles, `FUN_401303a8` sets where
  a callback returns 0) and whether the firmware reads the bitmap on load (a project saved this way was not loaded again in
  the emulator here).

## Reader **[D]**

- Region 2 as in finding 14: superblock (magic, version 3 or 4, lookup3 over `[0, 0x1FC)`, page size), records at
  `0x5D8180 + (id>>8)*0x40` sectors, up to 8 inline extents or up to 24 indirect extent pages of 2,730 extents (`FUN_40157d28`),
  content page to sector `0x5EE180 + page*0x40` (pages `>= 0x1000800`: `page*0x40 - 0x3FBC8000`, the factory table at
  `0x458000`; ids `0x1000000..0x10007FF` are RAM-resident and skipped). Directory pages are walked as variable entries (id 0 =
  free slot). Checks reported, never fatal: kind byte vs record attribute, record parent, id and page bitmaps, ids set in the
  bitmap but not reachable, unmapped pages. The indirect extent path is read from the decompile, not from a card the firmware
  wrote (**[O]**: no such card yet).
- Region 1: pool slot sectors as in finding 14 (`FUN_4012eef2`, `FUN_4012f596`, `FUN_401324cc`). Slot sizes: project 0x8000
  sectors, sound 8, kit 128 (the next slot's address).
- The firmware links "factory" into the root directory at every mount; the exporter skips it.

## Folder written

Path-filesystem files by name, native bytes (no `.wav` extension: the firmware stores names without it; a folder `x.wav` that
still converts to the native bytes counts as unchanged, and if the firmware changed the sample the WAV is backed up and the
native file takes its place). `.mmcfs/{projects,sounds,kits}/NNNN.bin` (slot content up to the last non-zero sector; the
image reads the rest as zero). `.mmcfs/boot/SSSSSS.bin`: the 16 MiB regions at sectors `0x40000`, `0x48000`, `0x50000`, only
when the overlay touched them. These are not pool slots: the builder should write the file at that sector.

## Update 2026-10-16: layout as data, boot regions, store, exit export

- The card layout (pool formulas, project regions, path-filesystem superblock, store range) moved from the `Geometry`
  enum into `[card]` of `devices/*.toml` (`device-profile::CardLayout`); every formula the enum had is reproduced by a test.
  The console export passes the running device's layout (the DN2 `TODO` is gone). A device without `[card.path_fs]`
  (DN2) is exported without a path-filesystem walk.
- The builder takes `.mmcfs/boot/SSSSSS.bin` and writes it at that sector; a boot copy replaces the generated boot project
  (docs/SAMPLES.md). **[V]** `packages/emulator/control/tests/card_roundtrip.rs`: save a project from the panel, quit (the
  synced folder is exported on exit: 2 added, `.mmcfs/projects/0000.bin` and `.mmcfs/boot/050000.bin`), build a new card
  from the folder, and slot 0 (14,522,368 bytes) and the working project are on it byte for byte; the DT2 boots to the main
  screen on it. A second export adds nothing; the one file it replaces is the working project, which the firmware rewrites
  from the boot copy at every start (**[D]**: so `050000.bin` in the folder is a copy that the next boot overwrites on the
  card; only slot files carry a saved project).
- `digikit/` is ignored by the exporter (docs/design/digikit-store.md).

## Update 2026-10-16 (r2b): the working project is not exported

- `.mmcfs/boot/050000.bin` (the working project) is left out of the export by default. The firmware rewrites it from the
  boot copy at every start (**[D]**, see above), so a copy in the folder is replaced on the card at the next boot and made
  every later export differ. The two boot copies (`040000`, `048000`) and the pool slots are exported as before.
- Options: `export::Options::include_working_project`, `plusdrive export --with-working-project`, `card.export
  {working_project: true}`. The builder still takes `050000.bin` from a folder (docs/SAMPLES.md), so an old folder works.
- Tests: `export::tests::mmcfs_slots_are_exported_by_their_content` (default leaves it out, the option adds it);
  `control/tests/card_export.rs` and `card_roundtrip.rs` (a saved project: `.mmcfs/projects/0000.bin` only; the second
  export adds and replaces nothing). **[O]** the two control tests were compile-checked in this lane (`cargo check
  --tests`), not run; `mise run verify` runs them in the `card` step.
