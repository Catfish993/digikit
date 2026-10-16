# digikit runtime: load modules from the +Drive at run time

Status: design agreed 2026-10-16; Phase 0 done (docs/findings/23-digikit-runtime-spikes.md); Phase 1 built, with the gaps listed under Phase 1. Branch `runtime/digikit-runtime`; commits after a green verify (Em, 2026-10-16).

## Goal

A resident runtime ("digikit runtime", dkrt) inside the Elektron MAIN OS. The user opens SETTINGS > DIGIKIT >
LOAD MODULES, ticks modules found on the +Drive and presses LOAD. The runtime loads them into memory that the
firmware never uses, without re-flashing. Later items in the same menu: health and diagnostics (CPU and RAM
headroom on the real device), and faster transfer of modules and samples.

The first target is the emulator only: copy modules into a `plus_drive/` folder, boot `mise run emu`, load them
from the menu. No hardware flashing until everything works in the emulator.

## Decisions (2026-10-16)

1. The runtime is installed once with patchkit, as a module on top of `core/expansion`. Every patch to firmware
   code is static and made at patch time. Loading a module never rewrites firmware code: it only fills slots and
   registry rows that the runtime owns.
2. On-device language: Rust through `rustc_codegen_gcc` (GCC's back end with `-mcpu=54418`; S1b), behind a C
   ABI. C (m68k-elf gcc, tools/patchkit/pk_toolchains.py) is the fallback. Module interfaces avoid small-struct
   results. Hook shims stay in assembly; DSP code stays SHARC assembly (no Rust back end for SHARC).
3. v1 scope: ColdFire-only modules load and unload at run time. Machine modules load at run time on both
   devices. DSP code goes in the unused tail of the per-block frame (S4b): DT2 698 bytes and DN2 60 bytes per
   block. Restart-only is the fallback.
4. The `plus_drive/` folder is the source of truth for the emulator's +Drive: the image is built from it at boot,
   and the files the firmware writes are copied back to it on exit.
5. Name: "digikit runtime" (dkrt), not "OS". It runs as a guest inside Elektron's OS.
6. `plus_drive/` layout: `plus_drive/dt2/` and `plus_drive/dn2/`, one per device, ignored by git. Files and
   folders map to the path filesystem as they are: a `.wav` is converted to a native sample, any other file is
   copied byte for byte. `.mmcfs/projects/`, `.mmcfs/sounds/` and `.mmcfs/kits/` hold raw MmcFs slots, one file
   per slot (`NNNN.bin`). The exporter writes changed files back byte for byte and keeps each replaced file under
   `out/plusdrive/backup/`.
7. Module store: card sectors 0x38000-0x3E000 on both devices (S6), read with the firmware's own block read
   function. `plus_drive/<device>/digikit/` maps into it. The DT2 path filesystem stays for samples.
8. Device facts are data: what differs between devices (card layout, store range, panel, DSP link, frame
   channel sizes) lives in `devices/<device>.toml`, and firmware-version addresses in `devices/ports/*.toml`.
   Code takes a profile, never a device enum, so another device on the same platform (for example Syntakt) is a
   new TOML file. The card layout moved there first (`[card]`, packages/emulator/device-profile).

## What is known (sources)

| Fact | Mark | Source |
| --- | --- | --- |
| patchkit (packages/patchkit) is Rust; it is built to `apps/web/public/patchkit.wasm` for the browser; the CLI runs it through cargo; the desktop app only reads the patch record | [D] | tools/emulator/native_wasm.py, tools/patchkit/patchkit.py, packages/emulator/boot/src/runtime.rs |
| Module objects are relocatable; patchkit links them to absolute addresses per image at patch time; no run-time relocation exists | [D] | modules/README.md, docs/findings/20-patch-toolchains.md |
| DDR is 128 MiB at 0x40000000, cacheable copyback through ACR0; 0x48000000 is the uncached alias | [V] | docs/findings/01-container-and-patching.md, docs/findings/08-hardware-and-ghidra.md |
| Free RAM `cf.expansion`: DT2 1.16/1.17 0x47e8b000-0x47fe0000 (1.33 MiB), DN2 1.11 0x46712000-0x47fe0000, DN2 1.12 0x4671a000-0x47fe0000 (24.8 MiB) | [D] | docs/findings/01-container-and-patching.md (RAM map) |
| That region was checked for boot, navigation, play and an idle soak only; save/load, sample record and delete, SysEx, USB and OS update are not covered | [O] | docs/findings/01-container-and-patching.md |
| Firmware heap: one buddy allocator, 32 MiB in bss (`FUN_4011e674` alloc, `FUN_4011e87a` free) | [D] | docs/findings/01-container-and-patching.md |
| Code runs from DDR; the I/D cache is not coherent on hardware; the emulator is coherent and hides a missing flush | [D] | docs/findings/07-emulator.md |
| The DSP image goes to the SHARC once at boot (`FUN_400ccf74` on DT2 1.16, over DSPI0); no later code path to the DSP is known | [O] | docs/findings/06-sharc-engine-and-startup.md |
| Free SHARC L2: DT2 from 0x2001ab7c, DN2 from 0x2008823c, to 0x200fe000 | [D] | docs/findings/06-sharc-engine-and-startup.md |
| Machine tables: `core/machines` copies the stock tables with added rows into `cf.expansion` (RAM); type numbers are permanent; unselected numbers are gap rows | [D] | modules/core/machines/types.toml, docs/findings/02-machines-and-parameters.md |
| +Drive format: Elektron's own filesystem (not FAT): MmcFs pools for projects, sounds and kits, plus a path filesystem for samples | [D] | docs/findings/14-plus-drive-format.md |
| Firmware file functions: path walker `FUN_40159f50`, readdir `FUN_40157072` (`FileSystemDirectory`), inode fetch `FUN_4015b64e`, `File::write` `FUN_4015838e` | [D] | docs/findings/14-plus-drive-format.md |
| The emulator card is copy-on-write: writes go to an in-memory sector overlay and are lost on exit | [D] | packages/emulator/card/src/lib.rs |
| UI: views are C++ classes; `VerticalMenuView` exists; no SETTINGS menu table and no text renderer are documented | [O] | docs/findings/03-ui-and-panel.md, docs/findings/07-emulator.md |
| Elektron Transfer uses SysEx over USB MIDI; no USB mass storage is known | [D] | docs/MIDI-SYSEX-RPC.md |

## Design

### Runtime (installed by patchkit)

- A new patchkit module `core/runtime` that requires `core/expansion`. Its code and data live in `cf.expansion`.
- Static parts, placed at patch time:
  - Hook trampolines at the firmware sites the runtime needs. Each one jumps through a function-pointer table in
    RAM. An empty slot runs the stock code.
  - The DIGIKIT entry in SETTINGS and its views.
  - An import table for this image: firmware name to address (file functions, allocator, UI classes, cache
    flush), resolved from the port lockfile like any anchor.
  - The module arena: a sub-region of `cf.expansion` with its own allocator, outside every region patchkit
    places static objects in.
- Dynamic parts, filled at run time: the slot table, registry rows (machines, params, pages) and the loaded
  module list.

### Module file (`.dkm`)

- Header: magic, format version, size, sha256, module name and version, required runtime version.
- Sections: code, data, bss size.
- Relocations (cf.32 and the other `.dko` types) against the module base and against imported firmware names.
- Exports: `init`, `fini`, slot bindings and registry rows.
- No firmware addresses inside, so one file works on every image whose runtime provides the imports it names.
- Built by the Rust host tooling from the existing `.dko` objects (a `pack` step next to patchkit's linker).

### Load sequence

1. Read the module from the store (card sectors 0x38000-0x3E000) with the firmware's block read function (DT2
   `FUN_4012deda`, DN2 `FUN_4012c59a`).
2. Check the header, sha256, runtime version and that every import resolves. Nothing is written before this
   passes.
3. Allocate in the arena, copy, apply relocations, zero bss.
4. Flush the ColdFire caches for the range (CPUSHL).
5. Call `init`. Then publish each slot binding with one aligned 32-bit store.

Unload (ColdFire-only modules): clear the slots, wait until no task is inside the module (an entry counter the
trampolines maintain), call `fini`, free the arena block. A module that a project references (a machine) is not
unloaded while running.

### Safety rules

- On hardware, nothing loads at boot by default; a key held at boot skips any autoload (safe mode).
- The arena is used only after phase 2 shows that no firmware path writes it.
- A module that fails a check is refused with a message; the runtime never runs a partly loaded module.

### +Drive dev loop

- `plus_drive/` replaces `samples/` as the input to `mise run emu` (samples, modules and any other files).
- At boot: build the image from the folder (packages/emulator/plusdrive). Cached by content, as today.
- On exit: read the image plus the card overlay and copy back what the firmware changed, byte for byte:
  path-filesystem files in their native format, and MmcFs project, sound and kit slots as raw blobs. The builder
  accepts these blobs again unchanged, so the round trip is lossless without decoding the sample trailer or the
  project format.
- Builder extensions needed: directories larger than one content page, files with more than one extent.

## Phases

### Phase 0: spikes (static where possible; run in parallel)

- [x] S1 Rust on ColdFire. Result: stock rustc/LLVM emits 68k-only forms (11 to 27 rejected lines per build).
- [x] S2 SETTINGS menu. Result: the item list is rebuilt at run time by `FUN_400a59a0`; one hook at its tail adds
  a DIGIKIT item, and our view is a stock `VerticalMenuView` (about 70 % confidence for the first run). Same
  structures on DN2.
- [x] S3 File reads from our code. Result: a locked C-level API (open, read, size, close, directory iteration)
  callable from the UI task on DT2. No path filesystem found on DN2.
- [x] S4 ColdFire-to-SHARC path after boot. Result: DT2 can upload bytes to DSP DDR through the sample link-port
  path. DN2: no existing channel (S4b found one of our own).
- [x] S1b `rustc_codegen_gcc` for ColdFire. Result: zero rejected lines, ColdFire ELF flags, only `R_68K_32`,
  the same calling convention as gcc except 8-byte struct results; the repo's libgcc helpers are 68020 code.
- [x] S4b A DSP channel of our own. Result: the unused tail of the per-block frame on both devices (DT2 698 bytes,
  DN2 60 bytes per block), with a full-frame borrow as a faster, muted option.
- [x] S6 Card use. Result: a module store at sectors 0x38000-0x3E000 on both devices; FsRaw is not dispatched
  on either device.
- [x] S5 Instrumentation. Result: write-watch and flush check in the emulator; a DT2 boot shows 0 writes into
  `cf.expansion` and 0 flush hits.

Gate: done (docs/findings/23-digikit-runtime-spikes.md).

### Phase 1: `plus_drive/` dev loop (host only)

- [x] 1a: `plus_drive/<device>/` folder and `mise run emu` pointing at it (ignored by git). Builder: nested
  directories, plain files, multi-page directories, inline and indirect extents, raw MmcFs slot import
  (docs/SAMPLES.md, finding 14). Checked through the firmware's own mount, readdir and read
  (`mise run plusdrive:check`).
- [x] 1b: image reader (path filesystem and MmcFs slots) and exporter (`plusdrive export`, console `card.export`),
  with backups (docs/findings/24-plusdrive-export.md). A panel project save arrives as
  `.mmcfs/projects/0000.bin`.
- [x] The builder takes the exporter's `.mmcfs/boot/SSSSSS.bin` (the copies at 0x40000 and 0x48000 and the
  working project at 0x50000); a boot copy in the folder replaces the generated boot project (docs/SAMPLES.md).
- [x] `mise run emu` exports on exit (`--card-folder`, log line `card: exported N changed files to ...`; `-- --no-sync` turns it off).
- [x] The desktop "Choose samples" path builds from a `plus_drive/` folder (nested folders, `.mmcfs/`, `digikit/`).
- [x] The builder maps `plus_drive/<device>/digikit/` into the module store (docs/design/digikit-store.md); read back through the firmware's block read on DT2 and DN2 (`mise run plusdrive:check`).
- [x] The export passes the running device; the card layout is device data (`[card]` in `devices/*.toml`).
- [ ] MmcFs bitmaps: the firmware does not set them on save; find what it reads them for.

Gate: a project saved and a sample recorded in the emulator are in `plus_drive/` after exit and are there again
after the next boot.

### Phase 2: arena safety

- [x] Scenarios for the paths not yet covered: project save, load, new, delete, rename, format, OS update (enter and
  cancel), the filesystem check, MIDI DIN input, MidiRpc over the USB callback (docs/findings/25-digikit-safety.md).
  Not covered: sample record and delete (no audio input in the emulator), the startup menu (FUNC held at power-up), real USB.
- [x] Run them with the S5 write-watch on `cf.expansion` and with the canary (tools/coldfire/arena_safety.py): 0 write
  events, 0 flush hits, canary intact, on DT2 1.16 (16 runs) and DN2 1.11 (15 runs).
- [ ] Static check of the arena with tools/coldfire/refscan.py and tools/coldfire/cf.py (no absolute references).
- [x] The card overlay after each scenario has no sector in the module store (0x38000-0x3E000).

Gate: no write into the arena by firmware on any scenario; result recorded in docs/findings/25-digikit-safety.md. Open
before the arena is used: the static check, DT2 1.17 and DN2 1.12 runs (`arena_safety suite IMAGE`), sample recording.

### Phase 3: runtime v0 (no UI)

- [x] Toolchain: `pk_toolchains codegen-gcc` (pins, patches and the target JSON under tools/patchkit/toolchain/);
  Rust modules take ColdFire-safe helpers from `compiler_builtins` (docs/findings/26-rust-on-coldfire.md). Open:
  an install from scratch, timed.
- [ ] C modules: a libgcc built for `-mcpu=54418` (the repo's libgcc helpers are 68020 code).
- [~] Module interface, schema and Rust SDK, designed together. Schema part done (finding 27): typed `[[machine]]`,
  `[[param]]`, `[[page]]`, `[[voice]]` sections with per-device knob models, generated Rust constants, SHARC include and
  gallery JSON; FX and MenuItem sections and the run-time registry interface are open. One schema (Machine, Param, Page, FX, MenuItem)
  generates the runtime's registry rows, the SDK types, the constants that SHARC assembly includes and the
  gallery metadata. A versioned runtime interface through which a loaded module registers what it adds.
- [x] One Rust module linked by patchkit and run in the emulator (`test/rust-demo`, all four ports).
- [x] `core/runtime` module: UI-loop hook trampoline, import table, arena allocator, store reader, loader (docs/design/digikit-runtime.md, finding 29). Slot 0 only; no registry yet.
- [x] `.dkm` packer in Rust (`patchkit pack`, docs/design/digikit-dkm.md).
- [x] A ColdFire-only test module: `test/dyn-demo`, a counter in slot 0.
- [x] Loading triggered from the emulator by a mailbox poke (`mem.cf.poke`; `tools/patchkit/pk_dkrt.py proof`). No autoload folder.

Gate: load, run and unload in the emulator on DT2 1.16 and DN2 1.11, write-watch clean, no flush warnings. Done on DT2 1.16/1.17 and DN2 1.11/1.12 (finding 29); open: the C-module libgcc and the interface/schema items above.

### Phase 4: DIGIKIT menu

- [x] SETTINGS > DIGIKIT entry (`modules/core/digikit-menu`, Rust): a hook before the selection restore at the end of the SETTINGS
  rebuild (DT2 `0x400a5d48`, DN2 `0x4009fe24`) adds a `MenuItem`; its action pushes a stock `VerticalMenuView` (LOAD MODULES, ABOUT).
  LOAD MODULES lists the `.dkm` files of the card store as `[ ]`/`[X]` rows (a label rebuild, not a draw callback), YES ticks, the
  last row LOAD calls `dkrt_load` through the table `DKRT_API` that the runtime fills (no runtime: NO RUNTIME). Proof in the emulator on
  all four images (docs/findings/28-digikit-menu.md, `tools/coldfire/digikit_menu_proof.py`).
- [ ] Error messages beyond the answer row (a popup), unload from the menu, the `DkrtStatus` layout agreed with `core/runtime`.

Gate: the test module loads and unloads from the menu in the emulator (the stand-in runtime is loaded from the menu; the real
runtime is Phase 3).

### Phase 5: machines

- [ ] ColdFire side: fill reserved machine, param and page rows at run time.
- [ ] SHARC side, both devices: the runtime sends the DSP code in the frame tail (S4b); a static L2 stub at the
  block handler copies it into free L2, checks it, invalidates the instruction cache range and fills the voices
  registry. Optional faster load: the full-frame borrow.
- [ ] Port `machines/pluck` through the SDK as the proof (ColdFire and DSP sides).
- [ ] Projects that name a machine that is not loaded fall back to the gap row.

Gate: a machine module selected in the menu plays in the emulator on both devices.

### Later

- Health and diagnostics module: CPU load (idle-task counter), RAM (buddy allocator bitmap), DSP load.
- Faster transfer: assess separately. USB mass storage would show Elektron's filesystem, which no host can mount,
  so it would need a FAT view on the device or a host tool.
- Browser: the web emulator has no card today; the dev loop is desktop only.
- Existing modules to Rust: only when a module gains from it (for example to load at run time).
- A DSP intermediate language: voices built from blocks (oscillator, filter, envelope) and lowered to SHARC
  assembly. Assess after two machines are written by hand through the SDK.

## Payoff, confidence, risks

- Phases 1 to 4 remove the re-flash for each module set and make modules portable across firmware versions.
  Confidence: high for 1 to 3, medium for 4 (menu reuse not proven).
- Phase 5: run-time machines on both devices through the frame tail (S4b: 8 to 10 agent hours for the channel,
  about 70 % confidence).
- Risks: the patched Rust toolchain to maintain (S1b); menu and text drawing (S2); hardware cache behaviour that the
  emulator does not show (S5 detector); firmware paths that write the arena and are not yet exercised (phase 2);
  hardware never ran `core/expansion` (no flashing until the emulator work is complete).
