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
defaults to the module id (`test/dyn-demo`), at most 31 characters of `A-Z a-z 0-9 - _ . /`; `dkrt_module` reports this name (the run-time `unload` that took it is archived). The file is
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
| 0x30 | 4 | `runtime_min`: informational, the runtime version the module was built for (packed semantic version; informational; `dkrt_sdk::RUNTIME_VERSION`, now 0.1.0); since runtime 0.1.0 the loader does not refuse on it (the contract bytes at 0x38 decide) |
| 0x34 | 4 | module version, `major << 16 \| minor << 8 \| patch` |
| 0x38 | 8 | contracts built against, a (major, minor) byte pair each: api, machine, fx, service. Major 0 = the module does not implement that contract (api is never 0). `pack` writes api always, machine when the file has a DSP section, service when it has slot functions or a real `dkm_init`/`dkm_fini`, fx 0 |
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
| 0x98 | 4 | `dsp_off`: offset of the DSP section (0: none) |
| 0x9c | 4 | `dsp_size`: its size in bytes |

After the header, in this order, each table where its offset says: imports, relocations, slot bindings, the `register` records,
the DSP section (16-byte aligned), padding to 16, then the image. A file with a DSP section carries the machine contract at 0x38 (a section of version 2; runtime 5 and later also load version 1).

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

## DSP section

A run-time machine can bring its own SHARC+ code (finding 32). `patchkit pack MODULE --device dt2|dn2` links the module's DSP object
(`[[object]] place = "dsp.l2"` for that device; at most one) into the section; the typed `[[voice]]` entries (machine id and the routine's
global symbol) or the explicit `[[dsp_voice]] type = N, entry = "_sym"` give the voice rows. The code holds no address of the image it
runs in: it is linked at the nominal base `0x20000000` (L2 byte base) and **relocated by the runtime on the ColdFire** before it sends
the bytes, so several DSP modules coexist in the DSP area (`_dl_area` of `core/dsp-link`, 512 KiB on the Digitakt II, 320 KiB on the
Digitone II; first fit, freed when a boot-time DSP part fails). Relocation is cheap here: the repository's DSP objects use only two kinds, `sharc.abs24`
(a call or jump target, a pc) and `sharc.abs32` (a 32-bit immediate: a data address, or a pc), both of which the linker already
patches into a 48-bit instruction, and 24 relocations per 3.5 KB for `pluck`.

All integers are big endian. Offsets are from the start of the section.

| Offset | Size | Field |
| --- | --- | --- |
| 0x00 | 4 | magic `DKDS` (0x444b4453) |
| 0x04 | 4 | version = 3 (voices of 64 bytes; the packer writes it only when a voice has a note_on entry or state, else version 2; versions 1 and 2 are still loaded: note_on 0, no state; version 1, voices of 16 bytes without ranges: every range undeclared) |
| 0x08 | 4 | device (1 DT2, 2 DN2): must equal the runtime's |
| 0x0c | 4 | `image_size`: bytes of text and data that are sent (a multiple of 4) |
| 0x10 | 4 | `bss_size`: bytes reserved behind the image (`.PKBSS`; not sent, not zeroed) |
| 0x14 | 4 | `align`: placement alignment in bytes, 8 to 64, a power of two |
| 0x18 | 4 | `image_off` (16-byte aligned) |
| 0x1c | 4 | `reloc_off`; 0x20: `reloc_count` (at most 4096) |
| 0x24 | 4 | `import_off`; 0x28: `import_count` (at most 32) |
| 0x2c | 4 | `voice_off`; 0x30: `voice_count` (at most 8) |
| 0x34 | 12 | reserved (0) |

- **Imports**: 32-byte NUL padded names of DSP symbols the object references and does not define: today the helpers of `core/dsp-voice`
  (`_dv_exp2`, `_dv_frame_hw`, `_dv_frame_sw`). The runtime resolves them in `dyndsp_info` (module `core/dyndsp`): a name it does not
  list makes the load fail with `E_DSP_IMPORT` (-32). Their values are in natural units (a pc for code).
- **Relocations**: 8 bytes: `u32` byte offset of the 48-bit instruction in the image, then `u32 kind << 28 | class << 24 | import`.
  Kind 0 is `abs24` (bits 23..16 of the address are the byte at +2, bits 15..0 the little-endian halfword at +4), kind 1 `abs32`
  (the halfwords at +2 and +4, high then low). Class 0: the field holds a pc of this object (add `(B - 0x20000000) / 2`, `B` the
  L2 byte address where the image is placed); class 1: a byte address of this object (add `B - 0x20000000`); class 2: the addend of
  the import `import` (add its value). `abs24` with class 1 is refused by the packer (a data address does not fit 24 bits).
  A result that does not fit its field fails the load.
- **Voices**: 48 bytes (version 2): `u32` machine type, `u32` byte offset of the entry routine in the image, eight `u32` ranges of machine
  knob 0 to 7 (`min | max << 16`, from the `[[param]]` the knob names; 0: a stock code, an empty knob or no range declared) for the runtime guard of
  `core/dsp-voice` (finding 51), two reserved words. The type is in the run-time range 16 to 23, not used by a loaded module. The routine's pc at placement `B` is `0xb80000 + (B - 0x20000000 + entry) / 2`.
  Version 3 voices are 64 bytes: 0 `type`, 4 render entry, 8 note_on entry (image byte offsets; note_on 0: none), 12 `state_bytes` (per voice),
  16 `clear_bytes` (zeroed at note-on), 20 to 51 the eight ranges, 52 `voices` (number of voice states to reserve), 56 to 63 reserved 0. The packer
  takes them from `[[voice]] render, note_on, desc` (`desc`: a `dk_machine_desc` data symbol; `entry` still means render). The runtime reserves
  `align8(image + bss)` plus, per voice record, `voices * align8(state_bytes)` for state, in record order, in the module's range.
- **Image**: the linked bytes at the nominal base; every relocated field holds `S + A`. The sections are text, data, bss in that order,
  so a `.PKBSS` buffer (pluck's 256 KiB delay lines) follows the sent bytes in the reserved range.

The runtime reserves `image_size + bss_size` rounded up to 64 bytes (`E_DSP_NOSPACE`, -33, when no range is free), copies and relocates
the image in an arena block, sends it with `dl_send` (window 0 of the link) and, when the DSP acknowledges (the DSP has the bytes in L2
and has invalidated its instruction caches), publishes the voice rows (window 1: **one word per transfer**, a row is 24 words: 0 type, 1 pc, 2 to 9 the eight ranges, 10 to 15 the guard's words, 16 note_on pc, 17 state_base, 18 stride, 19 clear, 20 gen, 21 level, 22 and 23 spare; written: the ranges, the pc, words 16 to 19, the guard's disabled-type word cleared and the type
last; a free row has type `0xffffffff`, which no machine equals, so the DSP's render scan never sees a half-written row). Only then
it writes the machine and parameter rows, runs `init` and binds the slots. When the DSP part fails at boot the module is taken down the other way: machine row, voice row (type
to `0xffffffff` first), then the range (the run-time unload that did this is archived). Refusal codes -30 to -39 (`dkrt-sdk`): `E_NO_DSP` -30 (no `core/dsp-link` and `core/dyndsp` in
the image), `E_DSP_FORMAT` -31, `E_DSP_IMPORT` -32, `E_DSP_NOSPACE` -33, `E_DSP_ROWS` -34 (no free voice row), `E_DSP_XFER` -35 (transfer
failed or timed out), `E_DSP_DEVICE` -36, `E_DSP_TYPE` -37 (a voice type outside 16..23 or in use).

The DSP's per-type **selector** word (which render path a track uses) is not set by this section: the selector of a run-time type is
type 0's (finding 30). A machine that clones SAMPLE, like `pluck`, is right already.

## What the loader checks, in order (nothing is written to the arena before the last one)

1. Store: header, table checksum, the path, the table's sha256 of the file (`E_NOT_FOUND`, `E_BAD_STORE`, `E_SHA`, `E_TOO_BIG`).
2. Magic, format 1, size fields equal the file (`E_FORMAT`); the file's own sha256 (`E_SHA`).
3. Contracts (0x38): for each contract the module declares (major not 0), its major equals the runtime's and its minor is not above the runtime's (`E_CONTRACT`: built for another SDK contract, rebuild the module). `runtime_min` is not checked.
4. Name, alignment, table bounds, reloc kinds and offsets, slot numbers and function offsets (`E_FORMAT`).
5. Every import resolves (`E_IMPORT`); every slot it binds is free (`E_SLOT`); no module of that name is loaded (`E_LOADED`); a
   free module record (`E_TABLE_FULL`).
6. The DSP section, if any (`E_NO_DSP`, `E_DSP_DEVICE`, `E_DSP_FORMAT`, `E_DSP_IMPORT`, `E_DSP_NOSPACE`, `E_DSP_ROWS`, `E_DSP_TYPE`; nothing is
   reserved yet), then the `register` records, if any: `E_NO_REGISTRY` (the image has no `core/dynreg`), `E_REG_DEVICE`, `E_FORMAT`, `E_TYPE_RANGE`,
   `E_TYPE_USED`, `E_PARAM_FULL`, `E_PAGE_FULL` (-20 to -25; docs/design/digikit-registry.md); -26 (`E_TYPE_IN_USE`, the unload refusal) is reserved: hot reload is archived.
7. Then: allocate `image_size + bss_size` (`E_NOMEM`), copy, zero bss, relocate, flush both caches over the range, record, `init`
   (`E_INIT`: the block is freed again), publish the slots. The registry rows are written after the copy and before `init` (`init` may reuse the
   staging buffer, which holds the records); a failed `init` removes them again.
   With a DSP section the DSP range, the relocated DSP image and the voice rows are reserved first, the ColdFire image is copied as
   above, and the rest (voice rows on the DSP, then the registry rows, `init`, the slots) runs over the next UI-loop passes once the DSP
   has acknowledged the code: `dkrt_load` returns `LOADING` (docs/design/digikit-runtime.md, "DSP code of a module").

## Check

`packages/patchkit/src/dkm.rs` has a reference loader (`Dkm::memory`) and a test that its result at any base equals the
patch-time linker's output at that base; `packages/patchkit/src/dkmdsp.rs` the same for the DSP section (`DspSection::relocated`, also
`patchkit dkm-dsp-image FILE BASE`), and `tools/patchkit/pk_dkrt_machine.py` compares the DSP's L2 after a load with a second
implementation of it. The runtime's loader is checked in the emulator (`tools/patchkit/pk_dkrt.py proof`,
`tests/patchkit/test_patchkit_dkrt.py`).

## Open

- Streaming load for files over 64 KiB; compression.
- Signing: the sha256 is an integrity check, not authentication (the store is on the user's own card).
- Pages and `own_dsp` machine records (Digitone II, lane B).
- The selector word of a run-time type (finding 32, open).
