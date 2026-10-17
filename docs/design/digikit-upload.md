# digikit module upload over USB SysEx

Status: built (device side, Python tool, exporter; lane r4, finding 43), 2026-10-18; spec (plan `docs/plans/digikit-runtime.md` Phase 7, decision 11; routes in
`docs/findings/42-usb-transfer-routes.md`). The device side is `core/runtime`; the hosts are a Python tool (tests, terminal),
the desktop app and the web app. Every host and the device build against this file; change it first, then the code.

## What it does

A host writes, replaces, deletes and lists the files of the module store (`docs/design/digikit-store.md`) while the
firmware runs. Nothing is loaded: an uploaded module appears in DIGIKIT > MODULES and loads after APPLY & RESTART, as
any store file does. The upload works in safe mode (that is how a bad module is replaced). It writes only store sectors;
it never touches the path filesystem, a project or the selection record.

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
  A reply is at most 1 KiB on the wire (the firmware's TX ring is 4 KiB and spins when full).

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

## Commands

| cmd | name | request body | reply body after status |
|---|---|---|---|
| 0x01 | INFO | (none) | `u16 protocol 1, u16 RUNTIME_VERSION, device id NUL ("dt2" or "dn2"), u32 free_bytes, u32 max_chunk 32768, u8 flags` (bit 0 safe mode, bit 1 upload open) |
| 0x02 | LIST | `u32 first` | `u32 total, u8 n`, then n entries `path NUL, u32 size, sha256[32]` from index `first`, as many as fit the reply |
| 0x10 | BEGIN | `path NUL, u32 size, sha256[32]` | (none). Opens an upload; a BEGIN while one is open drops the old one. |
| 0x11 | DATA | `u32 offset, bytes` | (none). |
| 0x12 | END | (none) | (none). Verifies, then commits (below). |
| 0x13 | DELETE | `path NUL` | (none). Commits a table without the path. |
| 0x14 | CLEAR | `u32 0x434c5221` ("CLR!") | (none). Commits an empty store. |
| 0x15 | ABORT | (none) | (none). Drops an open upload. |

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
