# The run-time registry: machines and parameters added by a loaded module

Status: built for Digitakt II 1.16 and 1.17 (ColdFire side only), 2026-10-16, emulator only. Plan: `docs/plans/digikit-runtime.md`
(Phase 5, ColdFire side). Results and evidence: `docs/findings/30-runtime-machines.md`. File: `docs/design/digikit-dkm.md`. Runtime:
`docs/design/digikit-runtime.md`. Code: `modules/core/dynreg` (patch time), `modules/core/runtime/src/rs/src/registry.rs` (run time),
`packages/patchkit/src/register.rs` (records, packer).

## What it does

A `.dkm` carries typed machine and parameter records in its `register` section. `dkrt_load` checks them and writes them into rows that
`core/dynreg` reserved at patch time; the machine is then in the SRC machine list and can be put on a track. `dkrt_unload` gives the rows back.
Nothing is patched at run time: no firmware code is written, only rows, tables and one pointer.

## Records (the `register` section)

Built by `patchkit pack MODULE --device dt2` from the module's typed sections (`[[machine]]`, `[[param]]`; `typed.rs` resolves them with the
device's knob model), for a module with `dynamic = true`. Big endian; format and checks in `packages/patchkit/src/register.rs`:

    0x00 magic "DKRG"   0x04 version 1   0x08 device (1 dt2, 2 dn2)   0x0c record count (at most 24)
    0x10 records: u32 kind, u32 length, body
      kind 1 PARAM   (0x50): u32 index, template, slot; i32 min, max, default; name[32]; short[16]
      kind 2 MACHINE (0x6c): u32 type, clone_of, position, own_dsp, level; u32 knobs[8]; name[24]; short[24]

- MACHINE `type` is the machine's permanent number from `modules/core/machines/types.toml`, table `[dt2.dynamic]` (key
  `"<module id>:<machine id>"`), in the range `dynamic_first ..= last` (Digitakt II 16 to 23). A project stores that number, as for a patched machine.
- `knobs[i]` (Digitakt II: the descriptor fields f0 to f7 of the machine row): a stock parameter code, or `0x80000000 | j` for PARAM record `j` of the same
  file (the runtime replaces it by the code it allocated). `level` is the ninth field (f8, the level code).
- `clone_of` is a stock type (below `machines.first`): the permission long, the per-type byte and the name hint of that type are copied into the
  tables. `position` must be 255: a run-time machine is listed after the built-in ones, in type order (the list order is fixed at patch time, below).
- PARAM: `template` is a stock code whose row supplies owner, flag, formatter and the value-class object; `slot` is the knob position (0 to 7).
  A PARAM that no knob names is allowed (it only takes a row).
- Not carried yet: `own_dsp`, pages (Digitone II), DSP code. The packer refuses them; the runtime refuses any record it does not know.
- `patchkit pack` of a module without code writes a four-byte image (`moveq #0,d0; rts` as `dkm_init`) so that the loader's rules stay as they are.

## Reserved rows (patch time, `core/dynreg`)

`core/dynreg` requires `core/runtime`, `core/machines` and `core/params`. It adds ordinary registry entries (so nothing in the frameworks changes
and an image without `core/dynreg` is byte-identical to before):

| What | How many | Where | Looks like |
| --- | --- | --- | --- |
| machine rows | 8: types 16 to 23 (`machines.dyn16.index` ...) | `machines.rows` (64 bytes each) in `cf.expansion` | a gap row (clone of type 0, no name), but **listed** (position 255) |
| parameter rows | 16: codes `params.dp0.index` ... `dp15` (`params.first` + 0 ...) | `pf_table` (60 bytes) and `params.rows` | a copy of stock row 0xd1, empty name |

So the machine tables span types 7 to 23 (`machines.count` 17): `mf_names`, `mf_permit`, `mf_pertype`, the DSP selector block, the persistence bound
(`8 + N`), the name, permission and per-type bounds. The run-time type numbers 16 to 23 are in `types.toml` (`dynamic_first = 16`; `core/dynreg:dyn16 ...
dyn23` hold the reservation); a patched machine never takes one. Numbers 10 to 15 are plain gap rows.

`core/dynreg` exports `dynreg_info`, a table of the addresses and layouts the registry needs (`dynreg.s`, 200 bytes): the machine rows and their
field offsets, the name, permission and per-type tables, the parameter table and registry rows, the first reserved type and parameter code, how to
build a parameter row (a spec of 15 words: copy template word n, or min, max, default, name, short, mirror index, kind word, 0x80 + slot), the knob
count, and two firmware-side addresses (below). The runtime links to it with `[object] optional = ["dynreg_info"]` (patchkit resolves an optional
undefined symbol to 0 when no selected module defines it); without `core/dynreg` a file with `register` records is refused (`E_NO_REGISTRY`) and a
file without records loads as before. The runtime's code names no device: the layouts are data.

## What is derived state, and how it is kept right

The machine framework builds tables lazily (`mf_ensure`: list order, rank pairs, std::string representations of the upper-cased names) and the firmware
builds more from them. What was found, and what the registry does (evidence in finding 30):

| State | Who reads it | What the registry does |
| --- | --- | --- |
| machine row (`f0..f8`, `clone_of`, `position`, name pointers, `O_LONG`/`O_SHORT` representations) | `mf_dispatch` (descriptor of a type), the clone tests, `typearg` | writes the fields; builds the two std::string representations itself in its own state (length, capacity, reference count -1, upper-cased characters); the name pointer is the last store |
| `mf_names[t]` {long, short, hint}, `mf_permit[t]`, `mf_pertype[t]` | name accessors, permission gate, per-type lookups | long and short point to strings in the registry's state; permission and per-type byte copied from the clone's entry, the hint likewise |
| `mf_list_buf`, rank pairs, rank map, static sorted list | the list getter `FUN_40052a08` (static vector sorted once at first use) | nothing: the reserved types are in all of them from patch time, in type order, at the end |
| the list the getter returns | the machine selection view | `dynreg_copy` (retargets the `std::copy` at the end of the getter) leaves out the types whose row has an empty name |
| the machine selection view's item list | the SRC machine list | **the firmware builds it once at boot and keeps it.** `dynreg_view_hook` (a hook at the entry of the function that fills it, `FUN_40060f50`) remembers the view; after a change the registry calls that function again (it clears the items first) |
| parameter row (60 bytes) in `pf_table`, `params.rows` row | the parameter accessors, `pf_t2` (value-class object by template) | written from the spec: template words, min, max, default, name and short pointers, mirror index, kind word, 0x80 + slot |
| the DSP's type -> selector table | the DSP | not touched: it is part of the DSP image (reserved types select as type 0). Open |

The defaults refresh and the other loops over parameter codes keep the stock count (core/params), so a new code does not enter them: the same as for
a patched machine.

## Writing, publishing, and what must hold

All of it runs in the UI task: `dkrt_load` and `dkrt_unload` are called from the UI-loop hook (`dkrt_tick`, mailbox) or from the menu, and the
machine list, the descriptor dispatch and the parameter accessors are the UI task's. There is no second reader.

1. **Check before any write** (`registry::check`, from the loader's check phase): header, device, record kinds and lengths, text fields, parameter
   slot and template, knob references, machine type in the reserved range and free (owner none and name empty: `E_TYPE_USED`), `clone_of` a
   built-in type, enough free reserved parameter rows (`E_PARAM_FULL`). A refused file changes no byte of any row or table (checked).
2. **Order** (`registry::apply`, before `init`): parameter rows (they carry no visibility), then per machine: the row fields, the string representations,
   `mf_names`, permission, per-type byte, owner, and last the **name pointer, one aligned 32-bit store**: the row counts as used from that store
   on (the list filter and the `E_TYPE_USED` test read it). Then the list the view keeps is rebuilt.
3. **Unload** (`registry::remove`, after `fini`): the name pointer first (the row is hidden), then the row, tables and parameter rows are reset to
   the reserved values, then the view list is rebuilt. Strings and representations live in the registry's state (about 2 KB of the runtime's bss), never in
   the arena, so no pointer in a row can dangle.
4. A project (or a track) that names a type whose row is free behaves as on a gap row: the type byte stays; the row is a clone of type 0 with no knobs
   (the SRC page shows empty boxes); the permission, per-type byte and the DSP selector are type 0's.

## Limits and open items

- Digitakt II only; 8 machines and 16 parameters at a time (numbers are in `dynreg.s` and `module.toml`; more costs 64 + about 90 bytes per type and 60 + 28 per parameter).
- Unload does not check whether a track uses the machine (the type byte stays; the track shows as on a gap row). A refusal needs a track scan, which is
  firmware layout (DT2 1.16 and 1.17 share it, but it is not an anchor yet).
- The machine plays through the DSP as type 0 (SAMPLE): the selector word of a reserved type is in the DSP image. Options: reserved rows with a fixed
  selector (a slice clone gets 5) refused for a clone of another type; or the Phase 5 SHARC side loads the selector words. A machine that is a
  data-only clone of SLICE is therefore not audio-complete yet.
- Digitone II: not done. It needs the page registry (`pages`), the DN2 list getter and view hook, and a different parameter lowering spec (the data is
  there: `dynreg_info` describes it); the packer already refuses a DN2 machine with knobs.
- `E_*` codes of the registry (-20 to -24) are local to `registry.rs`; they belong in `dkrt-sdk` with the next ABI revision.
