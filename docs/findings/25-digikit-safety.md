# 25. digikit runtime: arena and card-store safety (Phase 2)

Plan: `docs/plans/digikit-runtime.md` Phase 2. 2026-10-16, one agent, native emulator (`regions_probe`), DT2 1.16 first and
DN2 1.11. Everything is **[D]**: measured or read once here; no second agent has checked the addresses against the image
bytes. Inputs: `firmware/Digitakt_II_OS1.16.syx` (sha-256 278541e4...), `firmware/Digitone_II_OS1.11.syx`, the DT2 card built
from `samples/` (3 WAV files, `plusdrive build`), the built-in empty card on DN2. Tools: `tools/coldfire/arena_safety.py`
(runs, judgement, card check), `tools/coldfire/panel_script.py` (key scripts), docs/INSTRUMENTATION.md (options),
docs/CONTROL.md (`midi.in`, `midi.out`, `midi.usb.in`, `midi.usb.out`).

## Result

**[D]** In 16 runs on DT2 1.16 and 15 on DN2 1.11 (table below) the firmware did not write the module RAM `cf.expansion`
(DT2 0x47e8b000-0x47fe0000, DN2 0x46712000-0x47fe0000) and did not write a card sector in the module store 0x38000-0x3E000.
Per run: write-watch from the first CACR write (CPU stores and DMA) with 0 events; the canary pattern filled into the
region (cached and uncached views) with 0 changed words; the flush check with 0 hits; the card overlay with 0 sectors in the
store. The scripts reach the screens and replies named in the last column (frames checked by eye, then pinned by SHA-256 in
`tests/golden/arena_safety_frames.json`). Control: the same save run with a second watch range on the display buffer
(0x44e4a5b8+0x400) gives 710,377 write events and 256 changed canary words there, so the instruments were live.

What this does not show **[O]**: sample recording and delete (no audio input), Transfer over real USB (the USB hardware is
not modelled; MidiRpc enters at the firmware's USB receive callback), the startup menu (EMPTY RESET and FACTORY RESET need
FUNC held at power-up; a probe input is timed from ready), writing RPCs (Data write, file write, OS upgrade were not sent),
the Data object RPC payloads (below), DN2 1.12 and DT2 1.17 (the same tools run them: `arena_safety suite IMAGE`). A pass is
evidence for the paths run, not a proof; the static check of the arena (refscan, no absolute references) is still open in the
plan.

## Runs

Each run: one process, `DIGI_WRITE_WATCH` on `cf.expansion`, `DIGI_WRITE_WATCH_AFTER=cacr`, `DIGI_FLUSH_CHECK=1`, canary fill,
`--overlay-out`. Instructions are guest instructions in total (ready is about 1.0 G on DT2, 0.85 G on DN2); a run takes about
2 to 4 minutes of wall time with the watch on. "Card sectors written" is the size of the copy-on-write overlay at the end (512-byte
sectors): the firmware writes the working project region at every boot (DT2: 0x50000-0x56ecb, 0x5d8000, 0x5d8180-0x5d81bf,
0x5eff80-0x5f007f; DN2: 0x50000-0x56258) and a save adds project slot 0 (0x58000-0x5eecb). Raw output:
`out/runs/arena-r1/IMAGE/SCRIPT/` (local).

| script | DT2 1.16 instructions | DN2 1.11 instructions | write events (both) | card sectors written DT2 / DN2 | in store (both) | what it reached |
|---|---|---|---|---|---|---|
| boot | 1313345912 | 1147728821 | 0 | 28685 / 25177 | 0 | boot to the main screen, 300 M more instructions |
| nav | 1887340100 | 1721723229 | 0 | 28685 / 25177 | 0 | every page, mode and arrow key, encoder turns and pushes |
| play | 2454344334 | 2288727505 | 0 | 28685 / 25177 | 0 | PLAY, 16 trigs, RECORD, a second PLAY/STOP cycle |
| save_project | 1430343247 | 1264726278 | 0 | 57049 / 50354 | 0 | SETTINGS > PROJECT > SAVE PROJECT AS > slot 001 > name > SAVE OK; project slot 0 (sectors 0x58000-0x5eecb on DT2) written |
| save_quick | 1638341792 | 1472724847 | 0 | 57049 / 50354 | 0 | save as, then FUNC+SETTINGS: SAVE ALL PATTERNS AND PROJ SETTINGS? > YES > DONE |
| load_project | 2106342245 | 1940725312 | 0 | 57049 / 50354 | 0 | save as, then LOAD PROJECT > slot 001 > YES: LOADING bar, back in the PROJECT menu |
| new_project | 1434344310 | 1268727361 | 0 | 28685 / 25177 | 0 | LOAD PROJECT > CREATE NEW: INITIALIZING PROJECT bar (no slot written) |
| manage_delete | 1782341988 | 1616725062 | 0 | 57049 / 50354 | 0 | save as, MANAGE PROJECTS > RIGHT > DELETE > DELETE ...? y/n > YES |
| manage_rename | 1804341793 | 1638724822 | 0 | 57049 / 50354 | 0 | save as, MANAGE PROJECTS > RIGHT > RENAME > PROJECT NAME screen > YES |
| format_pool | 2135342501 | 1969725759 | 0 | 28687 / 25179 | 0 | SYSTEM > FORMAT +DRIVE > PROJ+PST+KIT > ERASE SELECTED DATA > DATA WILL BE ERASED! > YES; sectors 0 and 0x800 (MmcFs header and table) rewritten |
| os_update | 1505343233 | 1339726337 | 0 | 28685 / 25177 | 0 | SYSTEM > OS UPGRADE: OS UPGRADE WAITING FOR SYSEX, then NO back to SYSTEM (no data sent) |
| check_filesystem | 1717343143 | - | 0 | 28685 / - | 0 | SYSTEM > CHECK FILESYSTEM: INFO dialog A: 00000004 (DT2 only; the DN2 menu has no such entry) |
| pattern_temp | 1128345298 | 962728354 | 0 | 28685 / 25177 | 0 | FUNC+YES: Ptn saved to Temp Area |
| din_midi | 1201345735 | 1035728787 | 0 | 28685 / 25177 | 0 | 28 MIDI DIN bytes at wire rate: note on/off, CC, Sample Dump inquiry, a MidiRpc Ping (28 interrupts, parser hit 28 times on DT2) |
| midirpc_info | 1376345755 | 1210728810 | 0 | 28685 / 25177 | 0 | MidiRpc over the USB callback: Ping, SoftwareVersion, DeviceUID, StorageSpace, TempoRead, Query, Screenshot |
| midirpc_files | 1296345759 | 1130728812 | 0 | 28685 / 25177 | 0 | MidiRpc ReadDir /, ReadDir /factory, DataList x2, DataReadOpen |

Write events and flush hits were 0 in every run and the canary never changed. No run wrote a sector in 0x38000-0x3E000.

## MIDI input and output (item 1) **[D]**

The firmware's MIDI driver is `FUN_40002844` (DT2 1.16; the same code at the same address on DN2 1.11; called from
`FUN_400cc864` with divisor 0x7a12, 31250 baud). It uses UART9 (0xec074000, MCF5441x name; the bootstrap crate calls it
"UART3"), not the panel UART (UART8, 0xec070000).

- Receive: eDMA channel 36 (TCD 0xfc045480) moves each byte from the UART9 data register to a 32 KiB modulo ring
  (ATTR 0x78, destination 0x4fe30000 on DT2, 0x466b8000-based on DN2; the uncached alias of DDR below `cf.expansion`), one
  byte per major loop, an interrupt per byte (INTC1 source 28, vector 156, `vector_156_handler` 0x400021a0 on DT2). The handler
  clears the channel interrupt, then drains the ring from its consume index (`0x40964d60`) up to the channel's DADDR, calling
  the driver callback (`0x40964d70` = `0x4011d758`, which runs `FUN_4011d1f6(byte, 0x44d4e8d8)`, the per-byte parser).
- Transmit: `FUN_40002746` stores bytes straight into the data register while its status register (a plain write/read
  register in the emulator; the driver wrote 0xdd) says ready; the TX ring path (vector 181, `0x40001f86`, a USR&4 test and a ring at 0x4fe38000) was not seen in these runs, which had no DIN output.
- Emulator: there is no UART9 model. `Emulator::midi_in` (`packages/emulator/boot/src/midi.rs`, `runtime.rs service_midi`)
  does what the channel would do: it stores the byte at the channel's DADDR, advances DADDR within the ring and offers
  vector 156 when the guest's INTC mask, IPL and stack allow it, one byte per 42,240 instructions (320 us at 132 M
  instructions per second) unless told otherwise. `Board` records byte stores to 0xec07400c (`take_midi_tx`). The queue
  disables the idle and RAM-clear fast paths while it is not empty. Not in a checkpoint.
- Proof: `din_midi` sends 28 bytes (note on/off, CC, a Sample Dump inquiry `F0 7E 7F 06 01 F7`, a MidiRpc Ping): 28
  interrupts, the parser `FUN_4011d1f6` 28 times, consume index equal to the byte count. The Sample Dump inquiry got no reply
  on DIN out (no DIN output bytes in any run: the firmware sent none).
- **MidiRpc is not taken from DIN.** The queue task (`0x4011edae`) tags a message with its source (8 or 0x10 for messages of its first
  queue, 2 or 4 for its second; read from the code, which parser feeds which queue was not traced). The device 0x10 handler
  `FUN_4011efb8` returns unless that source is 2 or 4. In `din_midi` it is called once (the Ping) and `FUN_40125184`, the receive chain it jumps to, is never
  reached. So DIN does not reach the MidiRpc handlers. (The source values are read from the code, not printed from a run.)
- USB: the endpoint receive handler `FUN_40004504(len, packets)` takes 4-byte USB-MIDI event packets and passes the plain bytes
  of cable 0 to `FUN_4011d76c(buf, len)` and of cable 1 to `FUN_4011d9a0` (registered by `FUN_4000434a`, called at
  0x400cc972/0x400cc97e on DT2 1.16; DN2 1.11 at 0x400cedb2, callback `0x4011f0e0`). Replies leave through
  `FUN_40004734(len, buf)` (the same prologue at 0x40004734 on all four images), which writes USB packets to a ring at
  0x4fe3c000. USB hardware is not modelled and not built here. `Emulator::usb_midi_in` takes an interrupt at an instruction
  boundary (level 5, vector 156's frame), builds the chunk and the callback arguments below the interrupt frame and jumps to
  the callback; it returns through an `rte` of the MIDI ISR, where the emulator pops the arguments and restores d0, d1, a0, a1.
  The reply is captured at the entry of the transmit function. A MidiRpc request built with `devrpc.build_request` goes in
  unchanged.
- Proof: a Ping (`F0 00 20 3C 10 00 00 00 01 00 00 01 F7`) gets a reply of 66 payload bytes ending in the name string
  "Digitakt II" (79 bytes with the frame); DN2 1.11 answers with 41 payload bytes.

MidiRpc replies seen (type: payload bytes):

| request | DT2 1.16 | DN2 1.11 |
|---|---|---|
| Ping 0x01 | 66 | 41 |
| SoftwareVersion 0x02 | 12 ("00", "79", "1.16") | 12 |
| DeviceUID 0x03 | 5 | 5 |
| StorageSpace 0x05 (drive 1) | 20 | no reply (no handler) |
| TempoRead 0x06 | 24 | 24 |
| Query 0x09 | 1 | 1 |
| Screenshot 0x04 | no reply, and no later request is answered (sent last) | 1181 |
| ReadDir 0x10 `/` | 95 (the "factory" entry and the samples of the card) | no reply (no handler) |
| ReadDir 0x10 `/factory` | 0 | no reply |
| DataList 0x53, DataReadOpen 0x54 (path guesses) | "Invalid path" / "Error: Could not resolve path" (16 and 36 bytes, 7-bit-spread text) | the same |

The DT2 Screenshot stall **[O]** may be the emulator (the USB transmit ring is never drained: the transmit function waits on
it) or the firmware waiting for something else; nothing it does touched the arena. The Data object RPC payloads are not
documented (finding in docs/MIDI-SYSEX-RPC.md names "a path + object fields"): a plain path string reaches the resolver and is
refused, so no project or sound was read by RPC **[O]**.

## Menu paths (item 2) **[D]**

Panel codes as in `devices/*.toml`: SETTINGS 8, FUNC 17, YES 10, NO 12, UP 11, DOWN 14, LEFT 13, RIGHT 15. The same paths work on
DT2 and DN2 except where noted. Read from the OLED frames of the runs (the frames are the proof; `contact_sheet()` joins them).

- SETTINGS (code 8): PROJECT, SONG, PATTERN, MIDI CONFIG, SYSEX DUMP, AUDIO ROUTING, PERSONALIZE, SYSTEM (7 DOWN). FUNC+SETTINGS:
  "SAVE ALL PATTERNS AND PROJ SETTINGS? Y/N", YES gives DONE.
- PROJECT: LOAD PROJECT (the slot list, CREATE NEW last), SAVE PROJECT AS (slot list, YES, a PROJECT NAME screen proposing
  "PRESETS nnnn", YES, SAVE OK), MANAGE PROJECTS (slot list, RIGHT opens CLEAR, DELETE, RENAME, LOAD FROM, SAVE TO, TOGGLE, PURGE ALL).
- SYSTEM: USB CONFIG, OS UPGRADE, FORMAT +DRIVE, CHECK FILESYSTEM (DT2), STORAGE; DN2 shows MASTER TUNE in place of CHECK
  FILESYSTEM. OS UPGRADE shows "OS UPGRADE WAITING FOR SYSEX"; NO leaves. FORMAT +DRIVE: RIGHT ticks PROJ+PST+KIT (DT2 also has
  SAMPLES), DOWN to ERASE SELECTED DATA, YES, "DATA WILL BE ERASED! ARE YOU SURE? Y/N", YES.
- FUNC+YES on the main screen: "Ptn saved to Temp Area". FUNC+19, +20, +21: copy, clear, paste a pattern.
- Not scripted **[O]**: the startup menu (FUNC held at power-up: TEST MODE, EMPTY RESET, FACTORY RESET), the sampling menu
  (no audio input in the emulator; recording is out of scope), a real OS file over MidiRpc.

## Observations

- **[D]** A save writes exactly project slot 0 (sectors 0x58000 + 0x8000 * 0) in the card overlay, on both devices; LOAD
  PROJECT and CREATE NEW write no slot; FORMAT rewrites sectors 0 and 0x800 and trims (erase commands leave no overlay sector).
- **[D]** The write-watch and the flush check work with the MIDI input and the USB injection active: the injection writes
  the stack, the receive ring and DADDR from the host, which the watch does not see (host writes are outside it, finding 23 S5);
  the canary would show a host write into the region, and none happened (the DN2 ring is below `cf.expansion`).
- **[D]** DN2 runs show 21 CPUSHL against 1.1 to 3 M on DT2 (`watch_stats` in `facts.json`), so the flush check covers little
  on DN2; why is **[O]**. The write-watch itself is not affected.
- The boot writes about 14 MiB of the working project at every start (DT2 28,685 sectors, DN2 25,177): a host that keeps the
  card overlay must expect it.
