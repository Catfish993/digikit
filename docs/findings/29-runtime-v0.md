# 29. digikit runtime v0 (Phase 3)

Plan: `docs/plans/digikit-runtime.md` (Phase 3). Design: `docs/design/digikit-runtime.md`, `docs/design/digikit-dkm.md`. Lane r4,
2026-10-16, one agent, native emulator. Everything is **[D]** (measured here once; no second agent has checked it against the
image bytes). Nothing ran on a device. Inputs: stock `Digitakt_II_OS1.16.syx` (sha-256 278541e4...), also DT2 1.17, DN2 1.11 and
1.12. Proof tool: `uv run python -m tools.patchkit.pk_dkrt proof [--image IMAGE]` (about 25 s per image); tests
`tests/patchkit/test_patchkit_dkrt.py` (5 tests, 104 s).

## Result

**[D]** `core/runtime` v0 loads a `.dkm` module from the card module store, runs it, and unloads it, on DT2 1.16, DT2 1.17, DN2 1.11
and DN2 1.12, with the write-watch on `cf.expansion` showing 0 events and the flush check 0 hits. Per image, in one boot with
the patched firmware and a card built by `plusdrive build` from a `digikit/` folder, driven through the runtime's mailbox
(host pokes, serviced by the hook in the UI task):

| Step | Result |
| --- | --- |
| runtime started by its hook (`dkrt_tick` in the UI loop) | magic `DKRT`, 41 passes by the time the script starts (DT2; DN2 9) |
| load `modules/bad-sha.dkm` (one byte of the image changed after packing) | `E_SHA` (-4); the whole 512 KiB arena byte for byte unchanged; no module, no slot, `loads` = 0 |
| load `modules/bad-import.dkm` (a relocation against `fw_does_not_exist`) | `E_IMPORT` (-7); arena unchanged |
| load `modules/nope.dkm` | `E_NOT_FOUND` (-1) |
| load `modules/dyn-demo.dkm` | 0; module record at arena base + 16, slot 0 published |
| the module's counter, sampled every 25 M instructions | 1, 6, 12 (magic `DYNM`): rises while the UI task runs |
| unload | 0; slot cleared; `fini` ran (marker in the freed memory); counter 12, 12, 12 afterwards |
| arena after unload | one free block of the whole arena (0x80000 bytes minus the header): blocks merged |
| load again, count 6, unload; unload a third time | 0, then `E_NOT_FOUND` |
| write-watch (all of `cf.expansion` except the staging buffer and the harness stack; allowed writers: the runtime's code and the whole arena) | 0 events (control with the allowed range moved elsewhere: events > 0) |
| flush check | 0 hits; 170 993 fetches checked, 1.03 M CPUSHL (DT2); DN2 122 036 fetches, 726 CPUSHL |

**[D]** The flush check is live for this code: with `dkrt_flush` made a bare `rts` the same run reports hits on the loaded
module's lines (for example line 0x47e9e060, fetched at 0x47e9e066, written by the loader's copy loop at 0x47e8bede), and with
the flush it reports none. This is the first run of our own cache maintenance under the instrument.

## Numbers (DT2 1.16; DT2 1.17, DN2 1.11 and 1.12 give the same instruction counts)

- **[D]** Runtime object: 9 976 bytes of code, 972 of read-only data and data, 591 188 of bss (arena 524 288, staging 65 536,
  state 1 256, mailbox 108); trampoline 78. The patch uses 602 324 of the 1 396 736 bytes of `cf.expansion` on DT2 1.16 (794 412
  left). About 1 850 bytes of the code are `core::fmt` and panic machinery pulled in by bounds-checked indexing, and 900 are the
  64-bit divide of `compiler_builtins`; neither is needed by the logic **[O]** (trim later).
- **[D]** `dkrt_load` of the 404-byte `dyn-demo.dkm` (image 148 + bss 16, 11 relocations), called directly with interrupts masked:
  516 936 guest instructions, about 4 ms at 132 M instructions per second. A load refused at the file's sha256: 513 346. The
  firmware's block read of one 512-byte sector: 355 instructions (the emulator models the DMA). So the cost is the runtime's own
  work (the table CRC-32 by bits, two SHA-256 passes, the directory scan); where exactly is not split out **[O]**. Unload: 606.
  The loader on a real card will add the eMMC read time, not modelled in instructions.
- **[D]** The UI-loop hook runs about 0.2 times per million instructions in an idle emulator (counter steps of 5 to 6 per 25 M,
  about 30 per second at 132 M instructions per second): the loop pass rate is the timer-tick event rate, not a frame rate.
  Slot 0 is therefore a slow hook (tens of hertz); audio-rate hooks need a different site (Phase 5).
- Proof wall time with both instruments on: 25 s (boot to ready plus about 130 M instructions).

## Decisions and deviations

- **Hook site** (docs/design/digikit-runtime.md quotes the code). The event loop's `jsr` to the queue pop in `FUN_400337ba`
  (DT2 0x40033d06). Chosen over `FUN_4010ecf2` (the view offer, which runs only when an event reaches views) because it runs for
  every item the UI task takes, in task context, before the task blocks. On DN2 the same loop is in code Ghidra has not turned into
  a function (0x4002f178), so the anchor is a masked signature of the surrounding words (`cf_words` with the new `as = "code"`
  key in `tools/patchkit/port_resolve.py`); the one rule resolves on all four images.
- **Arena = the object's bss.** No new patchkit feature: a 512 KiB `static mut` in the runtime crate is placed with the object, so
  the patch-time allocator cannot overlap it, and the reset-path loader zeroes it. Cost: that zeroing at every boot, and the size is
  fixed in the crate (`ARENA_BYTES`), the same on both devices.
- **Slot bindings are exported symbols** (`dkm_slot_<n>`) published by the loader after `init`, not calls to a `register_slot` API
  inside `init`; the plan says "publish each slot binding with one aligned 32-bit store", and a declarative binding lets the
  packer check them. `init` may still do its own set-up.
- **Store table CRC covers the zero-padded table sectors** (what the builder writes); `docs/design/digikit-store.md` said "of the
  table" and now says so.
- `tools/patchkit/pk_link.py` accepts `cpushl` (privileged) in module code; `pk_rust.py` skips archive members with no sections
  (a types-only crate such as `dkrt-sdk`) and hashes path dependencies into the crate's source hash; `link.rs` exposes the section
  places (`Linked::places`) for the packer.

## Not done, risks **[O]**

- DIGIKIT menu, autoload, safe-mode key: Phase 4. No `register` records are read (the `.dkm` has the empty section).
- `E_BUSY` (a task still inside the module at unload) is written but not exercised: only the UI task calls slot 0 today.
- Stack use of `dkrt_load` in the UI task is about 1 KiB (the SHA-256 schedule and the import table are on the stack); the UI task's
  stack size is not known to me.
- A module built by the repo's Rust pipeline still carries its own `compiler_builtins` helpers; the runtime's copies are offered
  (`Api`, imports) for C modules and for a future module build that leaves them undefined.
- DN2: few CPUSHL on DN2 images (finding 25), so the 0-hit flush result there is weaker evidence than on DT2.
- The module store was only read through the emulator's eMMC model; the real card's read latency and the block read's mutex under
  load are untested. No hardware.
- Files over 64 KiB, more than 8 modules, more than 8 slots: refused.
