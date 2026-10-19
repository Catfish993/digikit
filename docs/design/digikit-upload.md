# digikit module upload over USB SysEx

Status: built (device side, Python tool, exporter; lane r4, finding 43), 2026-10-18; SELECT and RESTART added 2026-10-19 (lane dkrun; runtime 4, protocol version still 1: commands were only added; host `tools/device/dkrun.py`, `dk run MODULE`); LOG and PEEK, the card-safety codes and INFO's log range added
2026-10-18 (lane r4, finding 46; runtime 3, protocol version still 1: commands were only added); spec (plan `docs/plans/digikit-runtime.md` Phase 7, decision 11; routes in
`docs/findings/42-usb-transfer-routes.md`). The device side is `core/runtime`; the hosts are a Python tool (tests, terminal),
the desktop app and the web app. Every host and the device build against this file; change it first, then the code.

## What it does

A host writes, replaces, deletes and lists the files of the module store (`docs/design/digikit-store.md`) while the
firmware runs. Nothing is loaded: an uploaded module appears in DIGIKIT > MODULES and loads after APPLY & RESTART, as
any store file does. The upload works in safe mode (that is how a bad module is replaced). It writes only store sectors;
it never touches the path filesystem or a project. Only SELECT (runtime 4) writes the selection record, and RESTART (runtime 4) resets the device; nothing else does.

## Framing

```text
F0 7D 64 6B 01 <cmd> <seq> <body, 8-in-7 packed> F7          host -> device
F0 7D 64 6B 01 <cmd | 0x40> <seq> <body, 8-in-7 packed> F7   device -> host, one reply per request, in order
```

- `7D` is the MIDI non-commercial ID; `64 6B` is "dk"; `01` is the protocol version. A message with another version gets
  the reply status `E_PROTO` and nothing else happens.
- `seq` (0..127) is the host's; the reply echoes it.
- Packing (as the firmware's MidiRpc, finding 42 section 2.5): each group of up to 7 data bytes becomes a collector byte and
  then the 7 bytes with bit 7 cleared; data byte j (1..7) of the group takes its bit 7 from collector bit (7 - j). The last
  group may be short. Example: `80 01 02 03 04 05 06` packs to `40 00 01 02 03 04 05 06`; `FF` packs to `40 7F`.
- Integers in the unpacked body are big-endian. Paths are bytes plus a NUL, under the store's path rules (1 to 87 bytes, `/`
  separated, no empty, `.` or `..` part).
- A request is at most 40 KiB on the wire (the firmware drops a SysEx message over 128 KiB and holds about 150 KiB in its pool).
  A reply is at most 1 KiB on the wire (the firmware's TX ring is 4 KiB and spins when full), and **a reply is sent only when the ring has room**:
the runtime reads the firmware's count of bytes handed to the USB stack and not yet sent (anchor `dkrt.fw.tx_inflight_lit`) and drops the
reply when it is above 256 or when the reply would not fit below 0xff0 with it; a host that does not read its input cannot hang the MIDI task.
The host's retry asks again (`docs/findings/43-digikit-upload.md`, "No unbounded spin on a reply").

Every reply body starts with `i8 status` (0 = ok). Codes, in the runtime's code space (registry -20.., DSP -30.., boot -45):

| code | name | meaning |
|---|---|---|
| -50 | E_MSG | body too short or malformed |
| -51 | E_PATH | path breaks the store rules |
| -52 | E_FULL | not enough free sectors (reply carries `u32 free_bytes`) |
| -53 | E_STATE | no upload open (DATA, END) |
| -54 | E_OFFSET | offset is neither the next byte nor a retry of the previous chunk |
| -55 | E_LENGTH | chunk length breaks the rule below, or the file would pass its size |
| -56 | E_HASH | the staged file read back from the card does not match the sha256 of BEGIN |
| -57 | E_WRITE | the firmware's block write failed |
| -58 | E_READ | the firmware's block read failed |
| -59 | E_NOFILE | DELETE of a path that is not in the store |
| -60 | E_PROTO | unknown protocol version or command |
| -61 | E_NOSTORE | the device has no `[card.store]` or the store header is unreadable (CLEAR still works) |
| -62 | E_INUSE | BEGIN, DELETE or CLEAR on a card whose store area is not ours and not blank: the runtime writes nothing there (see "Card safety") |
| -63 | E_PEEK | PEEK of a range that the device file does not list, or a length of 0 or over 512 |

SELECT also answers the selection record's own codes (runtime codes -40 to -49): -40 `E_SEL_PATH` (a path is empty, too long (over 55 bytes) or has a character outside 0x21 to 0x7e), -41 `E_SEL_COUNT` (more than 8 paths), -42 `E_SEL_WRITE` (the firmware's block write failed), -46 `E_RANGE`, -47 `E_CARD_RO`.

## Commands

| cmd | name | request body | reply body after status |
|---|---|---|---|
| 0x01 | INFO | (none) | `u16 protocol 1, u16 RUNTIME_VERSION (4 since SELECT and RESTART, 5 since the DSP guard: voice rows of 16 words, DSP sections of version 2, 24 DSP diagnostic words, log events 27 and 28; the commands are unchanged), device id NUL ("dt2" or "dn2"), u32 free_bytes, u32 max_chunk 32768, u8 flags, u32 log_oldest, u32 log_newest` (flags: bit 0 safe mode, bit 1 upload open, bit 2 card read-only, bit 3 debug log ON, bit 4 a crash record was found at this boot; the two sequence numbers are those of the oldest and newest event in the log ring, 0 and 0 when empty; runtime 2 stops after the flags) |
| 0x02 | LIST | `u32 first` | `u32 total, u8 n`, then n entries `path NUL, u32 size, sha256[32]` from index `first`, as many as fit the reply |
| 0x10 | BEGIN | `path NUL, u32 size, sha256[32]` | (none). Opens an upload; a BEGIN while one is open drops the old one. |
| 0x11 | DATA | `u32 offset, bytes` | (none). |
| 0x12 | END | (none) | (none). Verifies, then commits (below). |
| 0x13 | DELETE | `path NUL` | (none). Commits a table without the path. |
| 0x14 | CLEAR | `u32 0x434c5221` ("CLR!") | (none). Commits an empty store. |
| 0x15 | ABORT | (none) | (none). Drops an open upload. |
| 0x20 | LOG | `u32 from_seq` | `u32 oldest, u32 newest, u8 n`, then `n` events of 32 bytes from `from_seq` (or from `oldest` when `from_seq` is older), as many as fit one reply (27). An event is `u32 seq, u32 time, u16 kind, u16 flags, u32 a0..a4`, big endian; `time` is the firmware's block counter (1500 per second), `flags` bit 0 an error. The host polls: ask again from `last seq + 1` until `n` is 0. Nothing is ever sent unless a host asks. |
| 0x21 | PEEK | `u32 addr, u16 len` | `u32 addr, u16 len`, then `len` bytes of RAM. `len` is 1 to 512 and `[addr, addr + len)` must lie wholly inside one range of `[digikit] peek` in the device file (`devices/*.toml`: the firmware's data and the runtime's own region with the crash-record area); no peripheral registers. |
| 0x30 | SELECT (runtime 4) | `u8 n` (0 to 8), then `n` store paths, each followed by a NUL | (none). Saves the selection record (what the next boot loads): `boot::selection_set`, the function the menu's APPLY & RESTART calls through `dkrt_selection_set`, so the card range check and the "blank or ours" rule apply and the boot counter and the DEBUG bit carry over. `E_INUSE` on a card that is not ours and not blank. The paths are not looked up in the store (a missing file is reported by the boot as `E_NOT_FOUND`, as for the menu). `n` 0 clears the selection. No effect until the next boot. |
| 0x31 | RESTART (runtime 4) | (none) | (none). The reply is sent first; the soft reset (`dkrt_restart`, the one APPLY & RESTART runs) follows from the UI task's next passes, once the TX ring has drained (at most 4000 passes). `E_STATE` while an upload is open. When `dkrt_restart` refuses (a card transfer is in flight) the request is dropped and the log gets an UPLOAD error event `{cmd 0x31, status}`; the host sees the runtime still running and asks again. A host that wants the new boot reads LOG: the new boot's log holds no UPLOAD event for cmd 0x31 (the ring starts again), and holds BOOT_START. |

The device id is the device, not the firmware version: the runtime has no per-port constant (a port is a set of anchors, and a
constant would be a fourth copy of a fact `devices/ports/` holds). A host that wants the version asks the stock MidiRpc
SoftwareVersion (the stock route is unchanged). *Changed 2026-10-18 (lane r4); the first draft said "dt2-1.16".*

Behaviour the first draft left open (lane r4, built and tested):

- A card with nothing in the store range is an empty store: INFO, LIST and BEGIN work, and the first END writes both headers.
  `E_NOSTORE` is for a header that is there but unreadable (no usable copy); CLEAR still works then.
- END closes the upload whatever its result (a failed END needs a new BEGIN). BEGIN, DELETE and CLEAR drop an open upload. ABORT with
  none open is ok.
- A store holds at most 256 files for upload commands (the table is held in the 32 KiB buffer); BEGIN answers `E_FULL` (free 0) past that.
  LIST and the loader read what is there.
- The buffer is a static object of the runtime, 32 KiB plus 16 bytes of body offset and 4 KiB scratch (36,896 bytes), 16-byte aligned. The first
  draft said "from its arena"; a static address lets the write-watch leave out the card reads that land in it by DMA, as it does for
  the staging buffer, and the buffer cannot fail to allocate.
- The reply status is a signed byte; `body` is what follows it.

DATA rules: `offset` equals the next expected byte, or the offset of the previous chunk with the same length (a retry; the
reply is ok and nothing is written again). The length is 1 to 32768 and a multiple of 512, except for the chunk that ends the
file. The host may keep at most 2 DATA requests in flight; replies come in order.

## The debug log (LOG and PEEK; finding 46)

The runtime keeps a RAM ring of 512 events (`modules/core/runtime/src/rs/src/dlog.rs`): boot steps, safe mode and why, the card check, each module's
load result, upload commands, menu actions, a health snapshot every ten seconds, the DSP diagnostics once a second (raw words of `dl_diag`), the DSP
heartbeat watch, the crash record of the boot before. With DEBUG OFF (SETTINGS > DIGIKIT > DEBUG; bit 1 of the selection record's `flags`, so the zero
value means ON) only error events are kept. The argument words of each kind are listed in `docs/findings/46-digikit-debug.md`; the host tool is
`tools/device/dklog.py` (`follow`, `crash`, `peek`). LOG and PEEK follow the framing and the TX guard of every other command: a reply is sent only when the
firmware's TX ring has room, so a host that does not read its input cannot hang the MIDI task (the host asks again). LOG, PEEK and INFO are not logged.

## Card safety (finding 46)

Every card write of the runtime goes through one function, which refuses (a) any write outside the store range of the device file (`[card.store]`; error
`E_RANGE` inside the runtime, counted, logged) and (b) every write when the store area is neither ours nor blank. At boot (or at the first write when no boot
hook ran) the runtime looks: the area is **ours** when store header A or B is a valid header or a selection copy is valid; otherwise every sector of the range
must read all 0x00 or all 0xFF (**blank**, read once, 64 KiB at a time, stopping at the first sector that is not); else it is **foreign** (or **unreadable**
when a read failed) and the runtime is read-only for this card: no boot counter (so no safe mode by counter), no selection, no upload (BEGIN, DELETE and CLEAR
answer `E_INUSE`; INFO, LIST, LOG and PEEK still work), ABOUT shows `CARD READ-ONLY` and where the foreign data starts, and the log has one error event with the
first non-blank sector and a crc32 of it. A write of data or a table into a blank range (an upload's staging) first writes an empty store header pair, so a cut
upload leaves a card that the next boot calls ours; the selection record and the store headers are themselves marks of ours and need nothing first.

## Store writes and the commit

The store format becomes version 2 (`docs/design/digikit-store.md` is updated with it; readers take 1 and 2):

```text
header, version 2: as version 1, plus
  24  4   u32 table_first_sector   (version 1: always 1)
  28  4   u32 seq                  sequence number of this header
  32  ..  zero
  508 4   u32 crc32 of bytes 0..508
```

There are two header copies: A at store sector 0 and B at the sector just before the selection record (`end_sector -
selection_sectors - 1`; recorded in `devices/<device>.toml` `[card.store]`, not hard-coded). The builder writes both with
`seq` 1. The builder's limit becomes `range - selection_sectors - 1`.

- **Reading:** a copy is valid when the magic, version, header CRC and table CRC hold (a version 1 copy has no header CRC:
  it is valid only when B is not). Use the valid copy with the higher `seq`; equal: A.
- **Equal headers before staging:** BEGIN first makes the two header copies equal (the lagging, torn or version 1 copy is rewritten from
  the winner); CLEAR makes the space reusable, and a cut between B and A must not leave a stale copy that names a table the next upload
  overwrites ("The CLEAR double fault", finding 43). If that write fails BEGIN answers `E_WRITE` and stages nothing.
- **Staging:** BEGIN reserves `ceil(size / 512)` sectors from `used_sectors` on, plus room for the new table. DATA writes whole
  sectors there at once (the last partial sector is zero padded). Nothing the current header points at is written.
- **Commit (END, DELETE, CLEAR):** END first reads the staged file back from the card and checks its sha256. Then: write the
  new table after the staged data (sorted by path, a replaced path takes the new entry), read it back and check its CRC,
  write header B with `seq + 1`, then header A with `seq + 1`. A power cut before B leaves the old store; between B and A
  the next reader takes B. `used_sectors` is the end of the new table; old file data is not reused until CLEAR.
- **Full:** BEGIN answers E_FULL with the free bytes. The host's way out is CLEAR and an upload of the files it wants
  (the hosts offer this as "repack"); a module missing at boot is skipped and reported, as today.
- Every card access goes through the firmware's block read and block write (`dkrt.fw.block_read`, `dkrt.fw.block_write`; the
  write takes the eMMC mutex), from the MIDI task, one request at a time.

## Where the device handles it

In the firmware's MIDI task, after the SysEx gather (finding 42 section 2, stage 4), before the router: a message that
starts `F0 7D 64 6B` goes to the runtime; every other message takes the stock route unchanged. The exact hook is a ported
anchor on all four ports (`dkrt.fw.*`). The runtime's upload buffer is a static 16-byte aligned object of 36,896 bytes (see "Behaviour the first draft left open").

## Tests (kept; `tests/digikit/scenarios`)

- upload, list, restart, load: equal to a card built with the file in `digikit/` (rows, tables, PCM).
- replace, delete, CLEAR, E_FULL, E_HASH (a wrong sha256 commits nothing), DATA retry and bad offset.
- power cut during staging, between header B and A, and after A: the next boot reads a valid store (old or new, never mixed).
- an upload during playback: PCM equal to the same run without it; the write-watch shows store sectors only.
- stock MidiRpc unchanged: a Ping and a SoftwareVersion reply are byte-identical with and without the runtime.
- the +Drive round trip: after an upload in the emulator, the exporter writes the store's files back to
  `plus_drive/<device>/digikit/`, so the next card build holds them.

## `dk run` (runtime 4; `tools/device/dkrun.py`)

One command from a module id to a running module: INFO (the runtime must be 4 or newer; its device id picks the pack target), `pk_build` and
`patchkit pack`, PUT to `modules/<name>.dkm` unless LIST shows the same sha256, SELECT with that one path, RESTART, wait for the new boot (poll LOG
until the old boot's RESTART event is gone and BOOT_START is there; the USB port may vanish and return, the tool connects again), print MOD_LOAD and
MOD_DSP (MOD_LOAD result 1 is LOADING, MOD_DSP result 0 the DSP part loaded), then follow the log for `--seconds`. The selection becomes this module
only; the other store files stay. Against the emulator the same code runs (`--emu`; the scenario `menu-dkrun-pluck` proves it: upload, SELECT, RESTART,
the next boot loads the module and the machine plays like the patched-in pluck).
