# 43. Module upload over USB SysEx (device side, host tool, scenarios)

Lane r4, 2026-10-18. Spec: `docs/design/digikit-upload.md`; store format version 2: `docs/design/digikit-store.md`; routes and the receive
path: finding 42. Marks: **[V]** verified here, **[D]** measured or read once, **[O]** open, **[C]** corrects an earlier claim. The hook sites and the step 7 call site
are **[V]** (a second agent checked them against the image bytes, 2026-10-18); nothing ran on a device. What is **[D]** was run in the
emulator by the kept scenarios below. The emulator models USB at message level (it hands bytes to the firmware's cable 0 receive callback,
finding 42 stage 3), so no rate here is a device rate.

## What was built

- **Store version 2** (`packages/emulator/plusdrive/src/store.rs`, `modules/core/runtime/sdk` `store`): two header copies (A at sector 0,
  B at `end - selection_sectors - 1`, `devices/*.toml [card.store] header_b_sector = 0x3dffd`), `seq`, a header CRC and
  `table_first_sector`. Readers take versions 1 and 2. The builder writes version 2 and keeps its files out of header B. Unit tests: A/B
  choice by `seq` (equal: A), a torn copy of either header, a table that was not written, version 1, an empty store (CLEAR).
- **The hook** `dkrt.hook.midi_rx` and the send function `dkrt.fw.midi_send` (anchor rules in `modules/core/runtime/module.toml`, resolved
  on all four ports by `port_resolve resolve-all`). The handler is `modules/core/runtime/src/rs/src/upload.rs` (`dkrt_midi_rx`, trampoline
  `dkrt_midi_hook` in `src/cf/tramp.s`): INFO, LIST, BEGIN, DATA, END, DELETE, CLEAR, ABORT; active in safe mode; every card write goes
  through `card_write` into store sectors only.
- **The host tool** `tools/device/dkupload.py` (`info`, `ls`, `put FILE [PATH]`, `rm`, `clear`; two requests in flight; transports: the
  emulator through `midi.usb.in`/`midi.usb.out`, a real device through python-rtmidi, built and **not** run against hardware).
- **The exporter** writes the store back to `digikit/` (`store_pass` in `export.rs`), three-way.
- **Scenarios** `tests/digikit/scenarios/upload-*.toml`, `menu-upload-pluck-demo.toml`, runner `tools/patchkit/pk_digikit_upload.py`.
- The emulator's `midi.usb.in` got two optional parameters, `chunk` (1 to 384) and `period`, to model a high-speed microframe
  (`docs/CONTROL.md`).

## Hook sites per port **[V]**

All four images have the same 428-byte MIDI task function (found by its bytes; only address literals differ). Its queue-2 branch (USB
cable 0) pops the item, gathers it (`FUN_4011d5e0` on DT2 1.16: the blocks into one buffer; the pointer is NULL when the message was over
128 KiB), and calls the router through the table entry of the status nibble with `(msg, len, source)` pushed. The hook is the two
instructions that make the call, `movea.l (0,A2,D1*4),A0; jsr (A0)` (6 bytes, nothing PC-relative):

| port | MIDI task | hook (`dkrt.hook.midi_rx`) | `dkrt.fw.midi_send` | stock cable-0 callback |
|---|---|---|---|---|
| dt2-1.16 | `0x4011edae` | `0x4011ef02` | `0x401217fc` | `0x4011d76c` |
| dt2-1.17 | `0x4011f08e` | `0x4011f1e2` | `0x40121adc` | `0x4011da4c` |
| dn2-1.11 | `0x40120722` | `0x40120876` | `0x401231a4` | `0x4011f0e0` |
| dn2-1.12 | `0x40120a02` | `0x40120b56` | `0x40123484` | `0x4011f3c0` |

- The rule is `cf_words` over `20721c00 4e90 4fef000c 2f052044 4e90588f` (the call and the task's tail that pops the arguments and frees
  the blocks); it matches exactly once per image. The `hook` op with `displaced = "run"` copies the two instructions to a tail; the
  trampoline returns there (`hook.<anchor>.tail`) when the message is not ours, and to `.return` (after the call, at the `lea 12,sp`)
  when it is, so the stock tail that frees the gathered blocks always runs.
- The gathered message starts with F0 and ends with F7 and the length counts both. The stock router compares the first bytes with
  `F0 00 20 3C`; `F0 7D ..` mismatches at byte 1 and the router returns (its one special case is `F0 7E`), so the prefix is harmless even
  if the hook were missing. A message that is not ours is not touched: the trampoline saves and restores `d0/d1/a0/a1`, the stack is as
  the task left it, and the displaced router call runs.
- `dkrt.fw.midi_send(len, msg, port)` (DT2 1.16 `FUN_401217fc`, hash `2a29225a7abd75aa3be8d59d049af587`, once per image) checks the MIDI
  ready flag, takes the TX mutex, calls the TX function `FUN_40004734` (which takes a complete F0 .. F7 message) and releases. Port 2 is
  USB. The TX function spins when its 4 KiB ring is full, so replies are kept under 1 KiB on the wire.
- **[V]** second-agent check (2026-10-18, raw bytes on all four images): the 6+4+4+2+2+4+2 bytes match once per image at task+0x154; at the
  hook `0(sp)`/`4(sp)`/`8(sp)` hold msg, len and source (4 or 2) and A0/D0 hold msg and len; the trampoline pushes them in C order and the
  code after `.return` reads only D5, D4 and A0, which it loads itself. `dkrt.fw.midi_send` takes (len, msg, port) and has six stock call
  sites of that shape. The hook sees every gathered cable-0 message, not only SysEx: `dkrt_midi_rx` checks `len >= 5` and the four prefix
  bytes before anything else. Not checked statically: that the Rust handler keeps d2-d7/a2-a6 (the C ABI).
- **[D]** The Rust host (`packages/dkupload` `Session`, the code of the desktop panel and the web page) ran against this handler on all four
  ports (`upload-rust` scenario, lane r1): INFO, LIST, put with two DATA in flight, replace, delete, E_NOFILE, E_HASH, CLEAR, and a lost
  INFO, DATA and END reply each; LIST equals the Python tool's and the card readback equals the bytes sent.
- The anchor test `tests/patchkit/test_patchkit_dkrt_boot.py::test_the_upload_hook_sits_on_the_router_call...` checks the bytes at the
  hook and the `lea` after it on all four images.

## Spec changes (marked in the spec)

- INFO's port id is the **device** (`dt2`, `dn2`), not `dt2-1.16`: the runtime has no per-port constant and a host can ask the stock
  SoftwareVersion for the version (the stock route is unchanged).
- The upload buffer is a static object of the runtime (36,896 bytes), not an arena block: its address is then known to the write-watch
  of the proofs, which must leave out the card reads that land in it by DMA, as it does for `DKRT_STAGE`.
- Left open by the first draft and decided: a card with no store range content is an empty store; END closes the upload whatever its
  result; BEGIN, DELETE and CLEAR drop an open upload; at most 256 files for upload commands.

## What the scenarios show **[D]** (emulator, all four ports)

Full tier, run once at the end, 20 (scenario, image) pairs in four processes in 318 s wall; all pass. Seconds per pair on the four
ports (dt2-1.16 / dt2-1.17 / dn2-1.11 / dn2-1.12), alone-in-the-batch times are lower:

| scenario | checks | what it proves | seconds |
|---|---|---|---|
| `upload-ops` | 68 | every command and error code (E_PATH, E_FULL with the free bytes, E_HASH commits nothing, E_OFFSET, E_LENGTH, E_STATE, E_NOFILE, E_PROTO, E_MSG), replace, DELETE, CLEAR, a DATA retry; after each step the card, read by the plusdrive reader (version 2, the right header copy), holds the bytes that were sent and equals the device's LIST; no selection sector written; the +Drive round trip | 15 / 20 / 19 / 13 |
| `upload-cut` | 23 | five cut points, a new daemon on the card each time (see below) | 72 / 63 / 68 / 68 |
| `upload-stock` | 5 | stock MidiRpc replies byte for byte equal with and without the runtime | 13 / 12 / 11 / 11 |
| `upload-playback` | 7 | a note with a 100 kB upload during it (DT2 only) | 57 / 39 / - / - |
| `menu-upload-pluck-demo` | 18 | upload, menu, APPLY & RESTART, rows and PCM equal the patched-in set | 193 / 157 / 86 / 150 |
| `upload-quick` | 16 | one upload, a replace, a delete (the gate; DT2 only) | 14 / 12 / - / - |

Every scenario also checks the write-watch on `cf.expansion` (no stray write over every boot of the run) and the flush check.
The quick tier (roundtrip-dt2, equiv-dyn-pluck on both devices, equiv-dn2-set, upload-quick; three processes) took 48.4 s with
`upload-quick` and 49.2 s without it (a first run without it took 66 s on a busy machine): **the quick tier cost is within the noise**;
the scenario is 12 to 21 s of one process.

- **Cut points** use the test knob `DKRT_UPLOAD_CUT` (the upload path refuses every card write after the Nth), so the card is exactly
  the state a power cut after that write leaves. An upload of a three-chunk file does six writes (three DATA chunks, the table, header B,
  header A). After a cut during staging, after all chunks and after the table the next boot reads the old store; after header B (A
  still old, `seq` lower) it reads the new store from B; after A the new store from both. Every case then took another upload and committed.
- **Stock route** : the Ping, SoftwareVersion and DeviceUID replies are byte for byte the same with the runtime and on the unpatched
  firmware (including the build string `1.17`), and `F0 7D 01 02 03 F7` (not our prefix) went the stock way without a reply.
- **Through the menu** (`menu-upload-pluck-demo`): two modules uploaded to a card with no store appear in SETTINGS > DIGIKIT > MODULES
  (rows counted from the menu's own table), are ticked, APPLY & RESTART loads them, the machine rows equal the patched-in
  `machines/pluck`'s and the PCM of a note equals its PCM. This is the Phase 7 gate on both devices.
- **During playback** (`upload-playback`, DT2): a 100 kB module is uploaded while a note sounds. The PCM equals the note without the
  upload from the onset on (the capture starts one block later, 64 samples, in both kinds of run the same way `equiv` shows), and the
  card sectors added by the upload are store sectors only (no selection sector).
- **+Drive round trip**: after the `ops` run, `plusdrive export` writes the store's files to `digikit/`, a second export changes nothing, and
  `plusdrive build` of that folder gives a card whose store lists them. Unit tests cover the three-way rule: a file changed in the folder
  after the card was built is kept and reported (`kept`), the card's version is in `export/<stamp>/`.

## Rate in the emulator **[D]**

A 1 MiB file, CF-only rig, DT2 1.17 and DN2 1.12 (the same numbers on both), delivery of 384 bytes per 16,500 instructions (a high-speed
microframe) and 384 per 1,500 give the same count, so the device's work is the limit, not the delivery:

| phase | instructions per file byte |
|---|---|
| DATA (stock assembler from the cable-0 callback, gather, 8-in-7 unpack, block write) | about 49 |
| END (read the staged file back, SHA-256, table, headers) | about 519 |
| whole upload | about 570 |

At the emulated 132 M instructions per second that is 0.23 MB/s, and at a real core's 190 M instructions per second about 0.33 MB/s.
**This is not a device rate**: the emulator does not model the eMMC program time, the cache misses, the USB packet split (stage 2) or the
other firmware load. **SHA-256 is 91 % of the cost** (my Rust implementation: bytewise buffer, a 64-word schedule, no unrolling);
a module is 4 to 100 kB, so a complete upload takes well under a second of CPU, and the check cannot be dropped (the END reply is the proof
that the card holds the file). A faster hash loop is a small job if it ever matters **[O]**.

## Step 7: can the runtime take its own SysEx before the firmware's chain? (static, report only) **[D]**

Verdict: **possible, not needed for modules, worth it only for samples; two to three agent days, medium risk, hardware-only proof.**

- **Need.** 1.4 MB/s of file data is 1.6 MB/s of MIDI bytes (8-in-7) and 2.13 MB/s on the wire (USB-MIDI event packing, 3 of 4 bytes):
  17 Mbit/s, about 4,200 packets of 512 bytes per second. High speed carries that easily; full speed (64-byte packets, 1 ms) does not.
  Whether the port runs at high speed is **[O]** (finding 42 section 1).
- **Stock cost.** Per packet: the deferred-call task `FUN_40004504` (about 95 instructions per 4-byte event, 12.2 k per packet), the
  assembler (about 24 per byte), then gather and unpack (about 30 per byte): about 100 instructions per payload byte, 74 % of a core at
  1.4 MB/s with no other load. That is the 1.0 to 1.4 MB/s ceiling of finding 42.
- **Own path.** An event unpack that keeps 3 payload bytes per event (6 to 8 instructions per event), an 8-in-7 unpack (20 to 25 per 8
  bytes) and a store into a ring: about 2.5 k instructions per packet, **8 to 10 per data byte**, 7 % of the core at 1.4 MB/s. The CPU
  ceiling is then 15 to 19 MB/s; the limits become the host's MIDI driver and the eMMC write rate (both **[O]**).
- **Hook site, tier 1 (cheap).** The cable-0 callback is registered once by `FUN_4000434a(cb0, cb1)`; one call site on all four ports
  (`4eb9 4000 434a` after two `pea`s; DT2 1.16 `0x400cc97e`, 1.17 `0x400ccae2`, DN2 1.11 `0x400cedb2`, 1.12 `0x400cef16`; a `cf_words`
  rule `[0x4eb94000, 0x28444879, *, 0x48794011, *, 0x4000434a]` at offset 18 matches once per image; **[V]** by the second agent: one
  `jsr` and one literal of `0x4000434a` per image, cb0 four bytes before it). A `retarget` to a runtime function
  that keeps the stock cb0 and substitutes its own passes everything that is not `F0 7D 64 6B` to the stock callback unchanged. It runs
  in the deferred-call task after the stock event split: it removes the assembler, the gather and the unpack but not stage 2 (27 % of the
  core at 1.4 MB/s).
- **Hook site, tier 2.** The entry of `FUN_40004504` (`lea -0x60,sp; movem.l ...`, cf_func hash `21029fc0c01b893af505129422dd0647`, two
  whole instructions, unique on all four): handle our packets before the CIN-table copy and the split, then release the slot with
  `FUN_400044ca(buf)` (hash `06ab7507d8209e133af1e892da50b5bc`). Foreign events in a mixed packet need a scratch packet for stock.
- **Risks.** (1) The prefix can span packets: a hold of up to 3 bytes, replayed to stock when it turns out not to be ours. (2) A stock
  SysEx in flight when ours starts leaves a stale partial message in the stock assembler: send it a harmless `F0 7D F7`. (3) The
  deferred-call task is the firmware's only one (128 entries): no card write there; unpack into a ring (64 to 128 kB) and let a runtime
  task or the MIDI-task hook write it with `block_write`. (4) Not in the EP1 OUT ISR: the interrupt priorities were not established.
  (5) The 4 KiB TX ring: acknowledge per chunk, small. (6) No USB model in the emulator below the callback: tier 2 cannot be
  tested there (the emulator injects at the stage-3 callback), so the first run would be on the device **[O]**. (7) Samples need the path
  filesystem's file API, not raw sectors: a different job (finding 42 route A/B).
- **Recommendation.** Not now. A module is 4 to 100 kB; the present path takes it in a fraction of a second. For samples (route B in
  finding 42, a vendor bulk interface, 4 to 20 MB/s) is the better investment than a faster SysEx.

## Open **[O]**

- Nothing ran on a device. The python-rtmidi transport is untested against hardware. Whether the real MIDI task handles a 37 kB SysEx
  without the host's pacing (the 160 KiB block pool and the DPC queue) is a hardware question; the emulator's delivery is gentler than a
  host driver.
- A reply is sent from the MIDI task through the firmware's TX function, which spins on a full ring: a host that does not read its input
  would hang the MIDI task (replies are under 1 KiB, the ring is 4 KiB).
- The menu's store reader (UI task) and an upload (MIDI task) are not synchronised: a list refreshed during a commit can see a torn
  table and show an error until it is refreshed again.
- After CLEAR the space is reused from sector 1. A cut between header B and header A of a CLEAR, followed by an upload and a torn header B
  on top of it, could leave header A pointing at a table that was overwritten (the table CRC then fails and the store is empty). It needs
  two faults in a row.
- SHA-256 speed (above). A store is limited to 256 files for upload commands.
- The exporter treats a store file named like a deleted folder file by the three-way rule only; a card built from an older folder than
  the one the export meets is reported as `kept`.
