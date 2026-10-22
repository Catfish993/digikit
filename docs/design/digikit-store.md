# The digikit module store

Status: built (builder and reader in `packages/emulator/plusdrive/src/store.rs`, runtime in `modules/core/runtime/sdk/src/store.rs`), 2026-10-16; version 2 (two header copies, for
module upload, `docs/design/digikit-upload.md`) 2026-10-18; **version 3** (a per-device area of 82 MiB / 22 GiB, folders, reusable space, 159-byte paths, a streamed table; runtime 0.3.0) 2026-10-22.
Plan: `docs/plans/storage.md`. Area choice: `docs/findings/59-storage-regions.md` (the older range choice, `docs/findings/23-digikit-runtime-spikes.md` S6, is superseded).

The store holds the files of `plus_drive/<device>/digikit/` on the card (modules, and samples on the DN2), in sectors the firmware never writes, so the
runtime can read them with the firmware's block read (DT2 1.16 `FUN_4012deda`, DN2 1.11 `FUN_4012c59a`; arguments sector,
byte count, buffer; the buffer 16-byte aligned, the count a multiple of 512).

## Where

The area comes from the device profile (`devices/<device>.toml`, `[card.store]`): sectors `[first_sector, end_sector)`, different per device and always
above the MmcFs write guard at sector `0x457FFF` (finding 59):

| Device | `first_sector` | `end_sector` | Size |
| --- | --- | --- | --- |
| DT2 | `0x2DF1000`, after the +Drive | `0x2E1A000`, the card end | 82 MiB |
| DN2 | `0x800000`, above everything the Data API reaches | `0x2E1A000`, the card end | about 22 GiB |

`[card] total_sectors` is the card the firmware accepts (the pSLC 32 GB part, 0x2E1A000 sectors); the area ends with it. A card the runtime finds
smaller than the area (its last sector does not read) has **no store**: nothing is read or written there, uploads answer `E_NOSTORE`, and the boot goes on
(`CARD_TOO_SMALL`, a `CARD_CHECK` event). Sector numbers below are relative to `first_sector` unless said otherwise. A device with no `[card.store]` takes no
`digikit/` files (the builder reports them as skipped).

The area is not a path filesystem: **folders are in the paths.** The runtime's modules are in `modules/`, samples (DN2) in `samples/`.

## Format

All integers are big-endian. Sector size 512. Version 3 is the only version: a header of another version is not a store (pre-1.0: no migrations;
the version 1 and 2 stores lived in the old area `0x38000..0x3E000`, which no code uses any more).

```text
sector 0                       header copy A
sectors 1 .. header_b - 1      tables and file data, anywhere: free space is reused
sector header_b = sectors - 3  header copy B
sectors - 2, sectors - 1       the selection record, two copies (below; not a store structure)

header (one sector)
  0   4   magic "DKST"
  4   4   u32 version = 3
  8   4   u32 entry_count
  12  4   u32 table_sectors      = ceil(entry_count / 2)
  16  4   u32 table_first        relative to the store; 1 when there is no table
  20  4   u32 table_crc          crc32 of the table's sectors, zero padding included (IEEE, as zlib: "123456789" gives 0xcbf43926); 0 for no table
  24  4   u32 seq                sequence number of this header
  28  4   u32 used_sectors       header A, the table and every file
  32  4   u32 area_sectors       the store's size in sectors when it was written (sectors, not limit): a store made for another area is not this one
  36  ..  zero
  508 4   u32 crc32 of bytes 0..508

table: entry_count entries of 256 bytes, sorted by (first_sector, path in byte order)
  0   160 path, NUL padded (relative to digikit/, "/" separated, at most 159 bytes)
  160 4   u32 first_sector       relative to the start of the store; 0 for an entry with no data
  164 4   u32 size_bytes
  168 32  sha256 of the file bytes (zero for a folder)
  200 1   u8 kind: 0 file, 1 folder with nothing in it
  201 55  zero
```

Rules: a path is 1 to 159 bytes of printable text (0x20 to 0x7E, no NUL), with no empty, `.` or `..` part; two entries may not have the same path; a path that is a
file may not also be the folder of another entry (`a` and `a/b`). A **folder entry** (kind 1, size 0) exists only for a folder with nothing in it (made by the
host); a folder that has files is implied by their paths. A file or folder with no data (an empty file, a folder) has `first_sector` 0 and takes no sector; it sorts before every file with data. A file is at
most 4 GiB - 1 bytes; each file starts on a sector boundary and takes `ceil(size / 512)` sectors.

A header copy is **usable** when its magic, version, CRC, area and counts hold (`table_sectors` = ceil(count / 2), the table and `used_sectors` inside the area). A reader
takes the usable copy with the higher `seq` (equal: A), and, when it checks the table's CRC (once per header: a runtime that wrote the table itself skips it), skips a copy whose table
does not check for the other one. An empty store (CLEAR, or every file deleted) is a valid store with `entry_count` 0 and no table.

**The table is never held whole.** It is read and written through small buffers (the runtime's: 8 KiB), so the number of entries is bounded only by the room on the card
(two entries per sector). A lookup is one pass over the table; a list starts at the sector of its first entry.

**Space is reused.** Free space is every gap between used extents: header A, the table, the files' data and the tail (header B and the selection record). The runtime
allocates a file's extent in the first gap that holds it (first fit) and, on every commit, writes the new table into the first gap that holds it, never over the table in use. A host's
free-space figure (`used_sectors`, INFO's free bytes) is the sum of the gaps less the room for the table of one more entry; **a file larger than the largest gap is refused with
`E_FULL` even when the sum is larger** (the free space is not one piece).

## Commits and power cuts

A commit (the end of an upload, a delete, a clear) streams the old table through two small buffers into a gap, with the entry inserted, replaced or removed, reads the new
table back and checks its CRC, and then writes header B and header A with `seq + 1`. Nothing in use is overwritten before the headers move, so a cut at any point leaves the old store or
the new one, never a mix (`dkrt_sdk::store` tests cut a commit after every write). A file's data is written to its extent before the commit; an upload that is cut is a gap that nothing names.

**Both copies agree before space is reused.** After a cut between header B and header A the copies differ: A lags and still names the previous table. That is harmless while nothing is
overwritten, but the next commit writes a table into free space, which may be exactly the sectors of that old table; if its header B then tears, the lagging A would be the only usable copy and
name overwritten sectors. So BEGIN, DELETE and CLEAR first rewrite the copy that lost the `seq` comparison (missing, torn or different) from the winner, and refuse if that write fails
(`docs/findings/43-digikit-upload.md`, "The CLEAR double fault").

**A reader that runs while an upload commits** (the menu's, in the UI task, against the MIDI task) reads a fingerprint of the two header sectors before and after it reads the table, and
starts again when it changed (`dkrt_sdk::store::stable`); no lock. A table is never overwritten in place, so a reader that began with the old header reads a complete old table, and only a
second commit during one read could reuse its sectors (the fingerprint catches that too).

## Who writes and who reads

- **The builder** (`plusdrive build`, the desktop card job): every regular file under `digikit/` (recursive; symbolic links and `.DS_Store` skipped; an empty folder is not built: folder entries are made by the runtime and exported). It writes both header copies (`seq` 1) and lays the data out in path order after the table. An image that holds store files or a selection reaches the end of the area (a sparse file).
- **The exporter** (`card.export`, the end of a session) reads the store and writes its files back to `digikit/` (a folder with nothing in it too). The firmware never writes these
  sectors; only the runtime's upload does. A path-filesystem entry named `digikit` that the firmware's user made on the device is exported as `digikit~<id>` so it cannot land in the store folder.
- **A reader** for tests: `plusdrive store ls IMAGE [--device dt2|dn2] [--cat PATH]` prints `path first_sector size sha256` by path (a folder with nothing in it as `path/ - 0 folder`; `--cat` writes one file's bytes).
  The runtime reads it with the firmware's block read; `mise run plusdrive:check` calls that function on a booted DT2 and DN2 and compares the header, table and every file with the image and the folder.
- **The runtime** (`modules/core/runtime`, Rust): `dkrt_sdk::store` is the format, the reader and the one writer; `upload.rs` is its commands (docs/design/digikit-upload.md); the device's area comes
  from its device object (`dkrt_store_first`, `dkrt_store_sectors`, written by `patchkit gen` from the device file). The runtime's one card-write function refuses a write outside the area (`E_RANGE`).

## `digikit/` and the path filesystem

`digikit/` is not also copied into the DT2 path filesystem. Reasons: the folder would show in the sample picker (finding 23 S3), which lists every file with no filter; two copies would need two places to keep equal;
the DN2 has no path filesystem, so the runtime needs the store anyway; and a path-filesystem copy is read by file name through the mutex-guarded C++ layer where the block read is a single call. The cost is that a
`digikit/` file is not visible on the device itself. **DT2 samples** are different: they belong in the sample browser, so they go to the +Drive (through the stock MidiRpc file commands, `docs/MIDI-SYSEX-RPC.md` section 12);
**DN2 samples** go to the store's `samples/` folder (the firmware cannot play them yet).

## The selection record (boot-time loading, plan decision 9)

The runtime keeps one writable record in the store range: which modules to load at the next boot, and the boot counter of safe
mode. It is the only thing the runtime ever writes to the card (finding 37). Layout fixed 2026-10-17; the +Drive round trip
(lane D) and the runtime (`modules/core/runtime`, `dkrt_sdk::DkrtSelection`) both rely on it.

**Where.** The last `selection_sectors` (2) sectors of the area (`devices/<device>.toml` `[card.store] selection_sectors`):
copy A at `end_sector - 2`, copy B at `end_sector - 1` (absolute sectors `0x2E19FFE` and `0x2E19FFF` on both devices: the area ends with the card). The store builder
keeps its files out of them (its size limit is `area - 3`: header copy B is the sector before them); a reader of the store ignores them. They are
zero (no record) on a card the builder made.

**Record** (one sector, all integers big endian):

```text
  0   4   magic "DKSL" (0x444b534c)
  4   4   u32 version = 1
  8   4   u32 seq             sequence number of this write
  12  4   u32 boot_counter    boots begun that did not stay up for the stable period (safe mode: 2 or more; below)
  16  4   u32 flags           bit 0: start in safe mode once (set by the menu, cleared by the boot that honours it)
  20  4   u32 count           selected modules, 0 to 8
  24  4   u32 stable_blocks    the stable period of this card in DSP blocks; 0 = the runtime's default, 45,000 (30 s)
  28  4   sticky type map, entry 0 (see below)
  32  448 8 paths of 56 bytes: store paths ("modules/dyn-pluck.dkm"), NUL padded, at most 55 characters, load order
  480 28  sticky type map, entries 1 to 7 (4 bytes each)
  508 4   u32 crc32 of bytes 0..508 (IEEE, as the store table)
```

**The sticky type map** (since 2026-10-20, runtime 0.2.0, docs/design/module-sdk.md 11.5 D7): up to 8 entries, each 4 bytes big endian, `hash24 << 8 | type`: the low 24 bits of FNV-1a 32 of the
machine's global id `<module id>:<machine id>` and the run-time machine type (16 to 23) the runtime gave it; all zero = empty. Entry 0 is bytes 28 to 31, entries 1 to 7 are bytes 480 to 507. A card
that was written before the map existed has zeros there (an empty map). The boot's assignment reads the map, the boot hook writes it back once after the loads when it changed (`docs/design/digikit-registry.md`,
"Machine keys and sticky type numbers"), and every other writer of the record (the menu's save, `SELECT`, the DEBUG and counter updates) carries it over. The builder writes a zero map.

**Reading.** Read both sectors; a copy is valid when the magic, version, count (<= 8), path NULs and CRC hold. Use the valid copy with
the higher `seq` (equal: A). No valid copy is an empty selection with counter 0.

**Writing** (`dkrt_selection_set`, the boot counter): the runtime writes with the firmware's block write (DT2 1.16 `FUN_4012e0c0`,
DN2 1.11 `FUN_4012c780`; anchor `dkrt.fw.block_write`, hash-found on all four ports; it takes the eMMC mutex itself) and only to these two
sectors. A new selection writes both copies with `seq + 1`, A first, then B, so a power cut between the two writes leaves one valid
copy (A new, B old: the higher seq wins; a torn A fails its CRC and B still holds the old record). **The boot counter** is raised at the
start of a boot that has modules selected and cleared when the session has been stable: `stable_blocks` (default 45,000, 30 s) DSP blocks
after the first pass of the UI loop, or at that first pass in a safe boot (no module runs there). The clearing re-reads the record first (the
menu may have saved a selection since the boot). A boot that dies earlier leaves the counter raised; two such boots in a row make the third a safe
boot (`docs/findings/37-boot-load.md`, "Safe mode after the first UI pass"). `stable_blocks` is a setting for tests and for people who debug a
module; nothing in the menu writes it, the builder writes 0, and the runtime carries it over when it writes the record. The boot counter changes write one
copy only: the one that is not the current record, with `seq + 1`.

**+Drive round trip.** The exporter reads both copies and writes the winner back as `plus_drive/<device>/digikit/selection.txt`
(one store path per line, load order); the builder turns that file into both copies (`seq` 1, counter 0); lines may be blank or start with `#`, the exporter keeps the user's file when it holds the same list, and the counter and flags are not exported (finding 40).

## The folder mirror (while the emulator runs)

The exporter above runs at the end of a session (and on `card.export`). Since 2026-10-22 the emulator also mirrors the store while it runs
(`plusdrive_format::mirror`, `docs/plans/card-folder.md`): about 2 s after the guest's last card write the files of the store go to
`plus_drive/<device>/digikit/<path>` (the DN2 too: it has no path filesystem, so only these files), `card.sync` does it at once, and a file the
card no longer has is removed from the folder (to the backup), so a DT2 card built from the folder at the next start does not bring it back. One way
only: nothing is read from the folder except by the DT2 startup build. The mirror removes only paths it has seen on the card in the session, so a session
that starts on an empty card (the DN2 has none at boot) leaves the files an earlier session mirrored. The selection record is not a store file and is not mirrored.

## Open

- **[O]** The area was chosen from a static study of the firmware (finding 59), not measured on a device. The write-watch of the emulator proofs sees no firmware write in it.
- **[O]** The ownership check reads the two headers, the two selection sectors and the first 1 MiB of the area; it cannot scan 22 GiB at every boot. Foreign data further in is not noticed.
- **[O]** The emulator's state snapshots keep at most 131072 written card sectors (64 MiB) in the card overlay, so a store with more data than that cannot be saved with a state file.
- **[O]** The folder mirror and the exporter read every store file (and hash the folder's) at every pass; with samples in the store they need a cheaper comparison (step 6 of the storage plan).
- The format has a version field and no compression.
