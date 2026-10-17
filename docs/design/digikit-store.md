# The digikit module store

Status: built (builder and reader in `packages/emulator/plusdrive/src/store.rs`), 2026-10-16. Plan:
`docs/plans/digikit-runtime.md` (decision 7, Phase 1). Range choice: `docs/findings/23-digikit-runtime-spikes.md` S6.

The store holds the files of `plus_drive/<device>/digikit/` on the card, in sectors the firmware never writes, so the
runtime can read them with the firmware's block read (DT2 1.16 `FUN_4012deda`, DN2 1.11 `FUN_4012c59a`; arguments sector,
byte count, buffer; the buffer 16-byte aligned, the count a multiple of 512).

## Where

The range comes from the device profile (`devices/<device>.toml`, `[card.store]`): sectors
`[first_sector, end_sector)`, `0x38000..0x3E000` (12 MiB) on both devices. Sector numbers below are relative to
`first_sector` unless said otherwise. A device with no `[card.store]` takes no `digikit/` files (the builder reports
them as skipped).

## Format

All integers are big-endian. Sector size 512.

```text
sector 0, the header
  0   4   magic "DKST"
  4   4   u32 version = 1
  8   4   u32 entry_count
  12  4   u32 table_sectors      = ceil(entry_count * 128 / 512)
  16  4   u32 used_sectors       header + table + data
  20  4   u32 crc32 of the table sectors, zero padding included (IEEE 802.3, as zlib: check value of "123456789" is 0xcbf43926)
  24  ..  zero to 512 bytes

sectors 1 .. table_sectors, the table: entry_count entries of 128 bytes, sorted by path (byte order)
  0   88  path, NUL padded, relative to digikit/, "/" separated
  88  4   u32 first_sector       relative to the start of the store
  92  4   u32 size_bytes
  96  32  sha256 of the file bytes

then the data: each file starts on a sector boundary, in table order, zero padded to the sector.
```

Rules: a path is 1 to 87 bytes (so it always ends in a NUL), with no empty, `.` or `..` part and no NUL; two files may
not have the same path. A file is at most 4 GiB - 1 (in practice the range is 12 MiB). An empty file takes no data
sector. The builder refuses (an error, nothing is written) when `used_sectors` would exceed the range. A card without a
store has zero bytes at sector 0 of the range; a reader reports "no store".

The format has no free-space map and no update in place: the builder writes the whole store at once, and a changed
`digikit/` means a new card image.

## Who writes and who reads

- **The builder** (`plusdrive build`, the desktop card job): every regular file under `digikit/` (recursive; symbolic
  links and `.DS_Store` skipped) of the folder.
- **The exporter never touches it.** The firmware never writes these sectors, so `card.export` / `plusdrive export` do not
  read the range, do not list `digikit/` files as changes and do not report them as deleted. A path-filesystem entry
  named `digikit` that the firmware's user made on the device is exported as `digikit~<id>` so it cannot land in the store
  folder.
- **A reader** for tests: `plusdrive store ls IMAGE [--device dt2|dn2] [--cat PATH]` prints `path first_sector size sha256`
  (`--cat` writes one file's bytes). The runtime reads it with the firmware's block read; `mise run plusdrive:check` calls
  that function on a booted DT2 1.16 and DN2 1.11 and compares the header, table and every file with the image and the
  folder.

## `digikit/` and the path filesystem

`digikit/` is not also copied into the DT2 path filesystem. Reasons: the folder would show in the sample picker (finding 23
S3); two copies would need two places to keep equal; the DN2 has no path filesystem, so the runtime needs the store anyway;
and a path-filesystem copy is read by file name through the mutex-guarded C++ layer where the block read is a single
call. The cost is that a `digikit/` file is not visible on the device itself.

## The selection record (boot-time loading, plan decision 9)

The runtime keeps one writable record in the store range: which modules to load at the next boot, and the boot counter of safe
mode. It is the only thing the runtime ever writes to the card (finding 37). Layout fixed 2026-10-17; the +Drive round trip
(lane D) and the runtime (`modules/core/runtime`, `dkrt_sdk::DkrtSelection`) both rely on it.

**Where.** The last `selection_sectors` (2) sectors of the range (`devices/<device>.toml` `[card.store] selection_sectors`):
copy A at `end_sector - 2`, copy B at `end_sector - 1` (absolute sectors `0x3dffe` and `0x3dfff` on both devices). The store builder
must keep its files out of them (its size limit is `range - selection_sectors`); a reader of the store ignores them. They are
zero (no record) on a card the builder made.

**Record** (one sector, all integers big endian):

```text
  0   4   magic "DKSL" (0x444b534c)
  4   4   u32 version = 1
  8   4   u32 seq             sequence number of this write
  12  4   u32 boot_counter    boots begun since the UI loop last ran (safe mode: 2 or more)
  16  4   u32 flags           bit 0: start in safe mode once (set by the menu, cleared by the boot that honours it)
  20  4   u32 count           selected modules, 0 to 8
  24  8   reserved, zero
  32  448 8 paths of 56 bytes: store paths ("modules/dyn-pluck.dkm"), NUL padded, at most 55 characters, load order
  480 28  reserved, zero
  508 4   u32 crc32 of bytes 0..508 (IEEE, as the store table)
```

**Reading.** Read both sectors; a copy is valid when the magic, version, count (<= 8), path NULs and CRC hold. Use the valid copy with
the higher `seq` (equal: A). No valid copy is an empty selection with counter 0.

**Writing** (`dkrt_selection_set`, the boot counter): the runtime writes with the firmware's block write (DT2 1.16 `FUN_4012e0c0`,
DN2 1.11 `FUN_4012c780`; anchor `dkrt.fw.block_write`, hash-found on all four ports; it takes the eMMC mutex itself) and only to these two
sectors. A new selection writes both copies with `seq + 1`, A first, then B, so a power cut between the two writes leaves one valid
copy (A new, B old: the higher seq wins; a torn A fails its CRC and B still holds the old record). The boot counter changes write one
copy only: the one that is not the current record, with `seq + 1`.

**+Drive round trip.** The exporter reads both copies and writes the winner back as `plus_drive/<device>/digikit/selection.txt`
(one store path per line, load order); the builder turns that file into both copies (`seq` 1, counter 0); lines may be blank or start with `#`, the exporter keeps the user's file when it holds the same list, and the counter and flags are not exported (finding 40).

## Open

- The range is a choice with 0x2000 sectors of margin on each side, not a measurement. That nothing in the firmware writes
  it (save, load, format, OS update, the Data RPC) is checked for a boot and a project save only
  (`docs/findings/23-digikit-runtime-spikes.md`, S5); Phase 2 runs the full scenario list with the write-watch.
- The format has a version field and no compression. Module files larger than the range need a different place.
