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

## Open

- The range is a choice with 0x2000 sectors of margin on each side, not a measurement. That nothing in the firmware writes
  it (save, load, format, OS update, the Data RPC) is checked for a boot and a project save only
  (`docs/findings/23-digikit-runtime-spikes.md`, S5); Phase 2 runs the full scenario list with the write-watch.
- The format has a version field and no compression. Module files larger than the range need a different place.
