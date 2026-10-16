# The `.dkm` module file

Status: built, 2026-10-16 (packer `packages/patchkit/src/dkm.rs`, loader `modules/core/runtime/src/rs/src/loader.rs`). Plan:
`docs/plans/digikit-runtime.md` (Module file, Load sequence). Runtime: `docs/design/digikit-runtime.md`. Store the file lives in:
`docs/design/digikit-store.md`.

A `.dkm` is one ColdFire module in a form the runtime can load at run time. It holds no firmware address and no host path: the
same file serves every image whose runtime has the imports it names. It is made from the module's `.dko` object (the object
`patchkit` links at patch time) by `patchkit pack`:

    patchkit pack test/dyn-demo -o plus_drive/dt2/digikit/modules/dyn-demo.dkm [--name NAME] [--runtime-min N]
    patchkit dkm-info FILE.dkm          # checks the sha256 and lists the contents

`pack` takes exactly one ColdFire object of the module (the module id names it; `--modules-dir DIR` adds module roots). The name
defaults to the module id (`test/dyn-demo`), at most 31 characters of `A-Z a-z 0-9 - _ . /`; `unload` takes this name. The file is
deterministic: the same object gives the same bytes.

## Format

All integers are big endian. The file is at most 64 KiB in v0 (the runtime's staging buffer). Offsets are from the start of the
file; the loaded image starts at a 16-byte aligned file offset.

| Offset | Size | Field |
| --- | --- | --- |
| 0x00 | 4 | magic `DKMF` (0x444b4d46) |
| 0x04 | 4 | format version = 1 |
| 0x08 | 4 | file size in bytes |
| 0x0c | 4 | header size = 0xa0 |
| 0x10 | 32 | sha256 of the whole file with these 32 bytes read as zeros |
| 0x30 | 4 | `runtime_min`: the lowest runtime version that can load it (`dkrt_sdk::RUNTIME_VERSION`, now 1) |
| 0x34 | 4 | module version, `major << 16 \| minor << 8 \| patch` |
| 0x38 | 8 | flags, reserved (0) |
| 0x40 | 32 | name, NUL padded |
| 0x60 | 4 | `image_off` (multiple of 16) |
| 0x64 | 4 | `image_size`: text and data, as the patch-time linker lays them out at base 0 |
| 0x68 | 4 | `bss_size`: zeroed memory right behind the image |
| 0x6c | 4 | alignment: 4, 8 or 16 (the arena aligns every block to 16) |
| 0x70 | 4 | `reloc_off` |
| 0x74 | 4 | `reloc_count` (at most 4096) |
| 0x78 | 4 | `import_off` |
| 0x7c | 4 | `import_count` (at most 64) |
| 0x80 | 4 | `init`: image offset of `dkm_init`, or 0xffffffff |
| 0x84 | 4 | `fini`: image offset of `dkm_fini`, or 0xffffffff |
| 0x88 | 4 | `slot_off` |
| 0x8c | 4 | `slot_count` (at most 8) |
| 0x90 | 4 | `register_off` |
| 0x94 | 4 | `register_size` (0: none) |
| 0x98 | 8 | reserved (0) |

After the header, in this order, each table where its offset says: imports, relocations, slot bindings, the `register` records,
padding to 16, then the image.

- **Imports**: `import_count` entries of 32 bytes, a NUL-padded name. Every undefined symbol of the object that a relocation
  names. The names the v0 runtime provides: `fw_block_read`, `fw_alloc`, `fw_free`, `memcpy`, `memset`, `memcmp`, `__muldi3`,
  `__udivdi3`, `__umoddi3`. A name the runtime does not have makes the load fail with `E_IMPORT` before anything is written.
- **Relocations**: entries of 8 bytes: `u32 offset` (into the loaded memory; the image only, never bss; any byte alignment, since
  m68k code holds 32-bit operands at even addresses) and `u32 kind << 24 | index`. Kind 0: the 32-bit word at the offset is a
  module-relative address; the loader adds the module's load address. Kind 1: the word is an addend; the loader adds the address
  of import `index`. The word is stored big endian. Only 32-bit absolute relocations (`R_68K_32`) can be loaded; PC-relative
  references between the module's own sections are resolved by the packer (the base cancels), and a PC-relative reference to an
  import is refused by the packer (the distance to the firmware would depend on where the module is loaded).
- **Slot bindings**: entries of 8 bytes: `u32 slot`, `u32 function offset`. They come from exported symbols
  `dkm_slot_<n>`, n = 0 to 7. The loader publishes each with one aligned 32-bit store after `init` returned 0.
- **`register` records**: typed machine and parameter records (`docs/design/digikit-registry.md`; layout in
  `packages/patchkit/src/register.rs`). `patchkit pack MODULE --device dt2` lowers a `dynamic = true` module's `[[machine]]` and `[[param]]`
  sections into them; the loader checks them with the rest of the file (before anything is written) and hands them to the registry
  (`registry.rs`), which fills the rows `core/dynreg` reserved. A module with no code at all packs with a four-byte image (`dkm_init` is
  `moveq #0,d0; rts`).
- **Image**: the linked bytes at base 0; every word named by a relocation holds `S + A` (symbol offset plus addend) for kind 0 and
  `A` for kind 1.

Exports by name (symbols of the object): `dkm_init(const Api *) -> i32` (0 = ok), `dkm_fini(const Api *)`,
`dkm_slot_N(const Api *)`. At least one must exist. `init` runs once after the copy, relocation and cache flush; if it returns
non-zero the load is undone (`E_INIT`).

## What the loader checks, in order (nothing is written to the arena before the last one)

1. Store: header, table checksum, the path, the table's sha256 of the file (`E_NOT_FOUND`, `E_BAD_STORE`, `E_SHA`, `E_TOO_BIG`).
2. Magic, format 1, size fields equal the file (`E_FORMAT`); the file's own sha256 (`E_SHA`).
3. `runtime_min` not above the runtime's version (`E_RUNTIME`).
4. Name, alignment, table bounds, reloc kinds and offsets, slot numbers and function offsets (`E_FORMAT`).
5. Every import resolves (`E_IMPORT`); every slot it binds is free (`E_SLOT`); no module of that name is loaded (`E_LOADED`); a
   free module record (`E_TABLE_FULL`).
6. The `register` records, if any: `E_NO_REGISTRY` (the image has no `core/dynreg`), `E_REG_DEVICE`, `E_FORMAT`, `E_TYPE_RANGE`, `E_TYPE_USED`,
   `E_PARAM_FULL` (-20 to -24; docs/design/digikit-registry.md).
7. Then: allocate `image_size + bss_size` (`E_NOMEM`), copy, zero bss, relocate, flush both caches over the range, record, `init`
   (`E_INIT`: the block is freed again), publish the slots. The registry rows are written after the copy and before `init` (`init` may reuse the
   staging buffer, which holds the records); a failed `init` removes them again.

## Check

`packages/patchkit/src/dkm.rs` has a reference loader (`Dkm::memory`) and a test that its result at any base equals the
patch-time linker's output at that base. The runtime's loader is checked in the emulator (`tools/patchkit/pk_dkrt.py proof`,
`tests/patchkit/test_patchkit_dkrt.py`).

## Open

- Streaming load for files over 64 KiB; compression.
- Signing: the sha256 is an integrity check, not authentication (the store is on the user's own card).
- Pages, `own_dsp` and DSP code in the `register` section (Digitone II, Phase 5 SHARC side).
