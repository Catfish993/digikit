# Writing patchkit modules

A module is our own code (ColdFire C or assembly, SHARC+ assembly) plus a manifest, `module.toml`. patchkit links the
code into a stock Elektron `.syx`, places it in a free region, hooks firmware sites and writes a patched `.syx`.
The short path (create, build, patch, inject, test): [docs/MODULE-AUTHORING.md](../docs/MODULE-AUTHORING.md). How
patchkit works and how to run it: [docs/PATCHING.md](../docs/PATCHING.md). This file is the authoring guide and the
manifest reference. Rules that apply to every module:

- **No firmware bytes in a module.** A `write` carries the SHA-256 of the bytes it replaces, never the bytes.
  Tables of the stock firmware are copied from the user's image at patch time (`[[section]]`).
- **No addresses in a module.** Every firmware site is a named anchor, found by an `[[anchor_rule]]`.
- **Do not name or copy the vendor DSP toolchain or its files.** Cite public manuals only
  ([docs/sharc/SOURCES.md](../docs/sharc/SOURCES.md)).
- Nothing patched has run on a device. Say "emulator only" in the gallery text until it has.

## Directory layout

```
modules/<category>/<name>/
  module.toml          the manifest (this file's reference)
  src/cf/*.c|*.s       ColdFire sources (m68k-elf gcc, -mcpu=54418, freestanding, relocatable)
  src/dsp/*.s          SHARC+ sources (algebraic syntax; every assembled word is re-decoded by our decoder)
  build/*.dko.json     built objects; committed
  tests/*.toml         harness tests
  README.md            notes: what it does, devices, limits
  gallery/icon.svg     the gallery card's icon
```

Sources may sit in subfolders (`src/cf/dt2/`, `src/cf/dn2/`) when one module serves both devices.

## Conventions

- **Module id**: `category/name`, lower case, hyphens (`fx/master-gain`). The id need not equal the directory; patchkit
  refuses two modules with one id. Module roots: `modules/`, `tests/fixtures/modules/` (test fixtures, ids `test/NAME`) and the
  extra roots of `--modules-dir` and `PATCHKIT_MODULES`.
- **Categories**: `core` (frameworks other modules require), `fx`, `machines`, `ui`, `tools`, `test` (fixtures and
  research probes; they are not for users). `mise run module:new` accepts these, except `core`.
- **Entry ids** (in `[[entry]]`): `[a-z][a-z0-9_]*`. They become symbols (`machines.pluck.index`).
- **Symbols** in expressions: `[A-Za-z_.][A-Za-z0-9_.]*`.
- **Version**: `major.minor.patch`; raise it when the module's behaviour or its interface to other modules changes.
  The version goes into the patch record of the image.
- **`status`**: `experimental` for everything today. Use `stable` only after hardware tests.
- **`[targets]`**: one line per port, `untested`, `experimental` (works in the emulator) or `stable` (works on a device).
  patchkit does not read it; the gallery shows it as a badge. Compatibility itself comes from the anchors: a module
  fits a firmware when all its anchors resolve there. Use `ports = [...]` to restrict a module to some firmware.
- **Claims**: declare every scarce resource the module takes with `[[claim]]`, for example the one hook site that a
  module takes (`dsp.master.hook`). Two modules with the same claim cannot be patched together, and the gallery
  explains it. Registry entries claim themselves.
- **Dependencies**: `requires` lists framework modules. The command line and the gallery add them for you (the
  command line prints `added by requires: ...`); a missing module or a `requires` cycle is refused.
- **`summary` and `gallery/icon.svg`**: what the gallery card shows. `summary` is one short sentence for players (at
  most 120 characters, ending with a full stop). The icon is line art on a 48 grid: `currentColor` lines and one
  highlight in `style="stroke: var(--icon-accent)"`. `apps/web/tests/icons.test.mjs` checks it.
- **`README.md`**: the module's notes. Write for a player: what it adds, which firmware, how to hear it, limits,
  "emulator only". No firmware bytes, no vendor tool names.
- **Comments in sources** say what the code does at each firmware site and which registers are free there, and cite
  the finding that shows it (`docs/findings/NN-...`).
- **Findings**: write results into `docs/findings/` with the marks of AGENTS.md, one file per topic.

## Manifest reference

Numbers can be written as TOML integers or as strings holding an *expression* (see
[Expressions](#expressions)). patchkit ignores unknown keys in most tables, so a misspelled key is silent: check each key against the tables below. (Unknown fields of an `[[entry]]` are an error.)

### `[module]`

| Key | Meaning |
|---|---|
| `id` | `category/name`, unique in the library |
| `version` | module version, `major.minor.patch` |
| `name` | display name in the gallery |
| `category` | `core`, `fx`, `machines`, `ui`, `tools` or `test` |
| `type` | `machine`, `fx`, `service`, `raw` or `framework` (`framework` is internal, for core modules) |
| `author` | who wrote the module (`digikit` for the modules of this repository) |
| `status` | `experimental` or `stable` |
| `summary` | one sentence for the gallery card |
| `requires` | module ids that must be selected too (frameworks) |
| `conflicts` | module ids that must not be selected |
| `ports` | optional: only these ports, for example `["dt2-1.16", "dt2-1.17"]` (empty: any firmware whose anchors resolve) |
| `dynamic` | optional, default false: a run-time module. It is never patched into an image (patchkit refuses it); its typed sections are not lowered into rows; `patchkit pack MODULE --device dt2 -o X.dkm` lowers them into the `.dkm` `register` records that the digikit runtime writes into the rows `core/dynreg` reserved (docs/design/digikit-registry.md). Its machines have no number in `core/machines/types.toml`: the runtime assigns a type in 16 to 23 when it loads the module and the card remembers it (the sticky map of the selection record; docs/design/digikit-registry.md "Machine keys") |

```toml
[module]
id = "fx/master-gain"
version = "0.1.0"
name = "Master gain"
category = "fx"
type = "fx"
author = "digikit"
status = "experimental"
summary = "Scales the final stereo block before the Q31 conversion by a gain word (1.0 = bit-identical)"
requires = []
conflicts = []
```

### `[targets]`

Test status per port (`dt2-1.16`, `dt2-1.17`, `dn2-1.11`, `dn2-1.12`): `untested`, `experimental` or `stable`.

```toml
[targets]
"dt2-1.17" = "experimental"
"dn2-1.12" = "experimental"
```

### `[[object]]`

A built object that patchkit places in a region. One module may have several (for example one per device).

| Key | Meaning |
|---|---|
| `path` | built object, relative to the module (`build/x.dko.json`) |
| `sources` | source files `module:build` compiles into it (`.c`, `.s`), relative to the module; all of one CPU |
| `place` | region: `dsp.l2` (SHARC+ code and data), `cf.expansion` (ColdFire code, data, bss) or `cf.flash_cave` (only the built-in loader) |
| `devices` | optional: `["dt2"]`, `["dn2"]` or both (default: both) |
| `[object.externs]` | undefined symbol of the object = anchor name (a symbol named exactly like an anchor needs no entry) |
| `optional` (key of `[[object]]`) | undefined symbols that resolve to 0 when no selected module defines them (an optional link to another module; the runtime uses it for `core/dynreg`) |

```toml
[[object]]
path = "build/master-gain.dko.json"
sources = ["src/dsp/master_gain.s"]
place = "dsp.l2"

[object.externs]
_fw_q31_convert = "dsp.master.q31_converter"
_fw_master_buf = "dsp.master.float_buf"
```

An object placed in `cf.expansion` makes patchkit add the built-in `core/expansion` module (loader, segment chain).
Other undefined symbols resolve to symbols of other modules, registries (`voices.rows`), sections and hook tails.

### `[[op]]`

An edit at a firmware site. Common keys: `op`, `anchor`, `offset` (bytes from the anchor, default 0), `devices`.

`op = "write"`: replace bytes.

| Key | Meaning |
|---|---|
| `hex` | the new bytes (hex string); or |
| `value` + `width` | an expression and its width in bytes (1, 2 or 4), range checked; not both with `hex` |
| `expect_sha256` | SHA-256 of the bytes being replaced. Needed when the write reaches outside the anchor's own bytes |
| `anchors` | `"PREFIX.*"` instead of `anchor`: repeat a `value` write for every anchor `PREFIX.<n>` in numeric order; `this` in the expression names the current anchor |
| `[[op.reloc]]` | relocations to module symbols inside `hex`: `offset`, `type` (`cf.32`, `cf.pc32`, `cf.pc16`, `cf.pc8`, `sharc.abs24`, `sharc.abs32`), `symbol`, `addend` |

```toml
[[op]]                       # core/expansion: `jsr copy; jsr clear` becomes `jsr pk_loader; nop`
op = "write"
anchor = "cf.reset.init_calls"
hex = "4eb9000000004e71"
[[op.reloc]]
offset = 2
type = "cf.32"
symbol = "pk_loader"
```

```toml
[[op]]                       # raise every stock bound by the number of added rows (see core/params)
op = "write"
anchors = "cf.params.bound.*"
offset = 2
value = "image.u32(this + 2) + params.count"
width = 4
```

`op = "retarget"`: change the target of an existing call or jump at the anchor to a symbol of the module.
ColdFire `jsr`/`bsr`/`jmp` and the SHARC+ direct `CALL`.

| Key | Meaning |
|---|---|
| `to` | the module symbol the call now goes to |

```toml
[[op]]
op = "retarget"
anchor = "dsp.master.q31_call"
to = "_mg_hook"
```

`op = "hook"`: overwrite a site with a jump (or a call) to a routine of the module and keep the displaced
instructions.

| Key | Meaning |
|---|---|
| `to` | the routine (symbol or expression) |
| `kind` | `jump` (default) or `call` (ColdFire `jsr.l`; the routine returns to the end of the site; implies `displaced = "drop"`) |
| `displaced` | `run` (default for a jump): the displaced instructions are copied to `hook.<anchor>.tail`, which jumps back to `hook.<anchor>.return`; `drop`: they are not kept |
| `cpu` | `cf` or `sharc`; optional, must agree with the anchor |
| `place` | region of the tail (default `cf.expansion` or `dsp.l2`) |
| `expect_sha256` | needed when the site reaches past the anchor |

```toml
[[op]]
op = "hook"
cpu = "sharc"
anchor = "dsp.voice.render_entry"
to = "_dv_entry"
displaced = "drop"
```

On ColdFire a hook needs a site of whole instructions; `jmp.l` is 6 bytes and the rest is padded with NOPs. PC-relative
operands in displaced instructions are re-encoded as absolute, except `(d8,PC,Xn)`, which is refused. A SHARC+ hook is
one 48-bit jump at a parcel-aligned offset; keep it out of delayed-branch slots and loop ends.
What registers are free at a hook site is the module's business: document it in the source header.

### `[[claim]]`

| Key | Meaning |
|---|---|
| `resource` | name of the resource, for example `dsp.master.hook` or `cf.reset.init_calls` |
| `devices` | optional device filter |

### `[[anchor_rule]]`

How `mise run patch:port` finds a firmware site. It writes the result (address, length, SHA-256) into
`devices/ports/*.toml`. The same anchor name may have one rule per device (`devices = [...]`). A rule that finds no
match or two matches is an error.

Common keys: `name`, `cpu` (`cf` or `sharc`), `kind`, `devices`, `mark` (`"D"`: measured or read once; use `"V"` only
after a second agent checked it), `offset`, `len` (bytes).

| `kind` | Extra keys | Finds |
|---|---|---|
| `sharc_func` | `reloc_hash`, `offset` in parcels | `len` bytes at a fixed offset in a function with this relocation-insensitive hash |
| `sharc_literal` | `reloc_hash`, `offset` | the constant loaded by the instruction at that offset (a data address) |
| `sharc_mem_addr` | `reloc_hash`, `offset` | the absolute data address of a direct DM access |
| `sharc_table` | `reloc_hash`, `offset`, `len` | the table a literal points to |
| `cf_func` | `hash`, `ordinal` (default 0), `offset` | `len` bytes at an offset in a function with this address-masked hash |
| `cf_ptr` | `hash`, `ordinal`, `offset`, `len` | the table behind an address literal in a function |
| `cf_words` | `words = [u32 or "*", ...]` | a table row by masked signature |
| `cf_params_t1`, `cf_params_refs`, `cf_params_bounds`, `cf_params_sites` | | the parameter table and its readers (used by `core/params`; `exclude_anchors` drops sites inside named anchors) |
| `cf_reset_init_calls`, `cf_reset_helper` | `which = "copy"\|"clear"` | the reset-path calls (used by `core/expansion`) |

```toml
[[anchor_rule]]
name = "dsp.master.q31_call"
cpu = "sharc"
kind = "sharc_func"
reloc_hash = "65c7ebdfe23c9a760bc6077a912e16c0474c182ee9849925cb0c10af0689f984"
offset = 0x15c
len = 6
mark = "D"
```

To write a SHARC rule for a site: `uv run python -m tools.patchkit.port_resolve rule-sharc dt2-1.17 0x1c7734 --len 6` prints the
hash and offset. ColdFire rules come from the function hashes of the `cfdb` database (`tools/coldfire/cf.py`,
`tools/patchkit/pk_machine_rules.py` as an example generator). Then run `mise run patch:port` and check that the diff of
`devices/ports/` shows your anchors on every firmware you claim. Full syntax: the docstring of `tools/patchkit/port_resolve.py`.

### `[[registry]]` and `[[registry.field]]`

A framework declares a table that other modules add rows to.

| Key | Meaning |
|---|---|
| `name` | registry name (`machines`, `params`, `voices`) |
| `first` | index of the first entry (an expression over anchors and image reads; no module symbols) |
| `max` | most entries the framework can take |
| `region` | where the rows are placed |
| `devices` | optional; a registry with one name may be declared once per device |
| `terminator` | `true`: one all-zero row follows the entries (not counted), for a routine that scans until a zero row |
| `index_table` | path of a TOML file with permanent entry numbers (see below); entries are numbered by it, not by order |
| `[registry.gap]` | with `index_table`: the field values of a gap row (the field defaults fill the rest) |
| `[[registry.field]]` | `name`, `kind`, `max_len` (`cstr`), `default` |

Field kinds: `u8`, `u16`, `u32`, `cstr` (a pointer to a string placed behind the rows; `max_len` checks the length)
and `sym` (an expression: a symbol of any module or `<registry>.<id>.index`). Rows use natural alignment, the row
size is rounded to the widest field, the byte order is the region's CPU. A field with no `default` must be given by
every entry.

patchkit defines `<reg>.first`, `<reg>.count`, `<reg>.entries`, `<reg>.rows`, `<reg>.row_size` and
`<reg>.<id>.index` (= first + k, entries in canonical module order; with an `index_table` see below).

**Permanent numbers (`index_table`).** A project stores only a machine's type number, so the number must not depend
on which other modules are selected. `core/machines` therefore ships `types.toml`:

```toml
[dt2]
last = 23                          # the highest number the framework supports on this device
[dt2.alloc]
"machines/pluck:pluck" = 9         # "<module id>:<entry id>" = number
```

The numbers are allocated by hand (or by `mise run module:new -- machines/NAME --machine`, which takes the next free
number on each device and prints it) and committed with the module. A number is never reused or changed, also not
after its machine is removed. Digitakt II numbers start at 7, Digitone II at 5 (stock types are never allocated); `last`
is 23 on both (Digitakt II: the DSP selector table was read to entry 23). patchkit refuses an entry without a number,
two entries with one number (the table itself is checked at load) and a number outside `first ..= last`. The table
spans `first` to the highest *selected* number: a number nobody selected is a **gap row** built from `[registry.gap]`
(for machines: a clone of type 0 with position `0xffffffff`, which the machine list does not show; a stored project
that names it plays as type 0). `<reg>.count` counts the rows including gaps, `<reg>.entries` the real entries; plan.json
lists the gap indices as `gaps`. Give a new framework with a persistent number the same two things: a table file and
a gap row that its routines can skip.

```toml
[[registry]]
name = "voices"
first = "0"
max = 16
region = "dsp.l2"
terminator = true

[[registry.field]]
name = "type"
kind = "u32"
[[registry.field]]
name = "pc"
kind = "u32"
```

### `[[entry]]`

(Machine modules use the typed sections below instead.) A content module adds a row to a framework's registry. Keys: `registry`, `id`, `devices`, and one key per registry
field. Numbers, strings for `cstr` fields and expressions or symbols for `sym` and numeric fields.

```toml
[[entry]]
registry = "voices"
id = "pluck"
type = "machines.pluck.index"     # the permanent type number of the machine below (core/machines types.toml)
pc = "_pk_render"                 # the routine, a symbol of this module's DSP object
devices = ["dt2"]
```

patchkit refuses an entry for an unknown registry, an unknown or missing field, a value that does not fit, a
`cstr` that is too long and an id used twice.

### Typed sections: `[[machine]]`, `[[param]]`, `[[page]]`, `[[voice]]`

A machine module is written in four small typed sections instead of `[[entry]]` rows. patchkit lowers them per
device into the rows of `core/machines`, `core/params` and `core/dsp-voice` (`packages/patchkit/src/typed.rs`;
the lowering also runs in the browser). What a knob means on a device is device data: `[modules.knobs]` and
`[modules.frame]` in `devices/<device>.toml`. A device file that has no `[modules]` section takes no typed
sections, and a new device is a new TOML file. `machines/pluck` and `machines/xslice` are the examples.

```toml
[[voice]]                     # machine id -> the DSP routine's symbol (core/dsp-voice)
machine = "pluck"
entry = "_pk_render"

[[machine]]
id = "pluck"
name = "Pluck"                # at most 23 characters
short = "PLK"
clone_of = 0                  # the stock type it copies
knobs = [0xca, "pluck_decay", "pluck_pick", 0, "pluck_bright", "pluck_color", 0, 0xd1]
#        ^ knob 0 .. 7: a stock parameter code, the id of a [[param]] of this module, or 0 for none
level = 10                    # optional; default: the device model's level_default
type_hint = 18                # optional, run-time modules only (per device: [machine.dt2] type_hint = 18): the number the machine takes when the card has none for it
devices = ["dt2"]             # optional; default: every device with a model
[machine.dn2]                 # per-device override of any key above, plus position and own_dsp
position = 4                  # place in the machine list (default: last)
own_dsp = true                # the engine renders nothing for it; only where the model allows it
knobs = ["pluck_decay", "pluck_pick", "pluck_bright", "pluck_color"]

[[param]]
id = "pluck_decay"
name = "Decay"                # at most 31 characters (the page header)
short = "DECAY"               # at most 15 characters (the label under the knob)
template = 0xd1               # stock code whose row is copied
min = 0
max = 0x7f00
default = 0x4000
[param.dn2]                   # per-device override of name, short, template, min, max, default
template = 0xd5

[[page]]                      # only where the device keeps knobs in a page (Digitone II)
id = "pluck_syn"
machine = "pluck"
name = "PLUCK"
title = "Pluck"
devices = ["dn2"]             # knobs default to the machine's knobs on that device
```

Rules:

- A parameter's slot is its knob position: `slot_base + position` (Digitakt II engine slots 0 to 7, Digitone II
  sound-array slots 25 to 32). A param used at two positions (two machines, or a machine and its page) is refused.
  A param that no machine of a device uses is left out there; with an explicit `devices` it is an error.
  On a Digitakt II SAMPLE clone, slot 3 passes only the sample bits (`value & 0x3ff`): put no parameter there. Slot 6 is usable
  (docs/findings/02-machines-and-parameters.md, last section).
- Parameter codes follow the order of the `[[param]]` sections (`params.<id>.index`, as before). Type numbers stay
  permanent (`core/machines/types.toml`); the lowering needs none.
- Lowered rows, per device: `voices` (`type = machines.<id>.index`, `pc = entry`), `machines`, `params`, `pages`.
  On a device whose knobs go to the machine row (`to = "machine"`: Digitakt II) the machine row gets `f0..f7` and the
  level `f8`. On a device whose knobs go to a page (`to = "page"`: Digitone II) the `[[page]]` gets `c0..c7`, the slots
  `s0..` and the level `c8`; a stock code on a page is refused (the slot-to-code mapping is not understood yet).
- A typo is an error: unknown keys of a device table, a device table for a device without a model, more knobs than
  the device has, a page on a device that keeps knobs in the machine row.
- Hand-written `[[entry]]` rows still work, also next to typed sections (for example for a registry the typed form does not cover).

The device model, from `devices/digitakt-ii.toml` (comments in the files):

```toml
[modules.knobs]   # count, slot_base, to = "machine" | "page", knob_field, slot_field, level_field, level_default, own_dsp
[modules.frame]   # base, stride, group, gap, mirror_base: where the DSP finds knob j of track t
```

The frame word of knob `j` of track (voice) `t` is halfword `base + stride * t + j + (j / group) * gap`; the parameter
table gives it the mirror index `mirror_base + j + (j / group) * gap`.

#### Generated files (`patchkit gen`)

`mise run gen:modules` (`uv run python -m tools.patchkit.patchkit gen`, or the binary; add `-- --check` to only report stale files) writes
`modules/<id>/build/gen/` for every module with typed sections; `gen --check` fails when a committed file is
stale (`packages/patchkit/tests/typed.rs` runs it). The files hold no firmware bytes.

| File | For |
|---|---|
| `consts.rs` | Rust SDK users: `include!("consts.rs");` gives `pub mod dt2 { ... }` and `pub mod dn2 { ... }` with `FRAME_BASE`, `FRAME_STRIDE`, `<MACHINE>_TYPE`, per parameter `<PARAM>_KNOB`, `_SLOT`, `_FRAME_OFF`, `_MIRROR`, `_MIN`, `_MAX`, `_DEFAULT`, and `frame_halfword(track, offset)` |
| `<device>.inc` | SHARC assembly: `#include "dt2.inc"` (pk_build expands it from `build/gen/`); the same names as `#define`s, in decimal (the assembler upper-cases macro text, so `0x` literals break after `+`) |
| `gallery.json` | the gallery: machines with their knobs, per device (type, position, page), and the parameters with ranges; it is part of the web bundle (`files["build/gen/gallery.json"]`) and shown on the module card |

A module can also ask for constants from the device files (`devices/*.toml`: `[board]`, `[digikit]`, `[card.store]`) and
`core/machines` `types.toml` with `[[gen]]` entries in its `module.toml` (`kind` = `rust`, `gas-shared`, `gas`, `sharc` or `sharc-adapter` (only the `ADM_*` defines of `[adapter.machine]`), and a
`path`; `{dev}` in the path makes one file per device). They are written by the same `patchkit gen` and checked by `gen --check`;
what each kind holds, which fact lives where and what a new device needs are in `docs/design/device-data.md`.

`sdk/sdk.toml` is the one source of the runtime version (0.1.0, packed `major<<16|minor<<8|patch`; runtimes 1 to 5 read as 0.0.1 to 0.0.5), the
contract versions (api, machine, fx, service) and the log kinds. `gen` writes `modules/core/runtime/sdk/src/gen_sdk.rs` and
`tools/device/dklog_kinds.py` from it, and the per-type limits (`[limits]` in `devices/*.toml`: modules, machine_rows, arena_bytes,
stage_bytes) into the runtime's `device.rs`. The patchkit tests fail on a stale file. `runtime_min` (header byte 0x30) is informational;
a module is refused only on a contract mismatch (`E_CONTRACT` -48: rebuild the module). The runtime refuses further loads when its
module table is inconsistent (`E_TABLE` -49). `patchkit pack` and `dkm-info` print a `size` line by section. In a SHARC
include the note of each constant is on the line before its `#define`: a trailing comment would become part of the macro.

Example (pluck, Digitakt II): `R1 = FRAME_BASE;` and `R0 = R13 + PLUCK_DECAY_FRAME_OFF;` instead of `0x6d` and `1`.
`tests/patchkit/test_pk_gen_include.py` shows that this assembles to the committed object, byte for byte.

### `[[section]]`

Bytes copied from the user's image when the patch is applied, optionally extended with one row per registry entry,
then placed in a region. The firmware table is never stored in the repository.

| Key | Meaning |
|---|---|
| `name` | section name; the symbol `<name>` is its address and `<name>.size` its length |
| `region` | where to place it |
| `from` | `{ anchor, offset = 0, len = EXPR, expect_sha256 }`; `len` defaults to the rest of the anchor; `expect_sha256` is needed when the range reaches outside the anchor |
| `append` | `{ registry, row = [EXPR, ...] }`: for each entry append the row; a word is `EXPR` (32-bit), `u8:EXPR` or `u16:EXPR`; `$field` is an entry field and `$index` its index |
| `align` | bytes, default 4 |
| `devices` | optional device filter |

```toml
[[section]]
name = "mf_names"
region = "cf.expansion"
from = { anchor = "cf.machines.tab.names", len = "(image.u8(cf.machines.dispatch + 1) + 1) * 12" }
append = { registry = "machines", row = ["$name", "$short", "image.u32(cf.machines.tab.names + 12 * $clone_of + 8)"] }
```

### Expressions

Allowed wherever a number is accepted (`value`, `len`, `first`, section rows, field values): integers (decimal or
`0x`), symbols, `+ - * / & | << >>`, parentheses, and `image.u8(ANCHOR + OFF)`, `image.u16(...)`, `image.u32(...)`:
a read from the user's image at patch time. A read must lie inside the anchor (its SHA-256 is checked), and the
CPU of the anchor sets the byte order. `first`, `len` and table sizes must be static: anchors and image reads only.

## Tests

Module tests live in `tests/*.toml` and run in the smoke matrix for every module set that contains the module.

```toml
[test]
name = "audible"            # unique within the module
devices = ["dt2", "dn2"]    # which devices
quick = true                # also run by `module:dev` after every edit

[[step]]
tap = "TRIG 1"              # one action per step
[[step]]
wait_ms = 400
[[step]]
expect_pcm_db = { min = -60, max = -3 }
[[step]]
expect_no_fault = true
```

Actions: `tap` (a button name or a chord list), `hold` + `ms`, `turn` + `by` (+ `events` for a burst), `wait_ms`;
checks: `expect_frame` (a name; the golden is the frame's SHA-256, `differs_from` compares with another name),
`expect_pcm_db`, `expect_no_fault`, `expect_mem = { addr, mask, eq }` (a ColdFire word), `expect_dsp = { addr, hex }`,
`stress = { seed, actions }`; `device = "dt2"` on a step limits it. Every input also checks fault, DSP halt and
main-loop stall. The complete list is the docstring of `tools/patchkit/pk_harness.py`.

Workflow:

1. `mise run module:new -- category/name` and fill in the manifest and the sources.
2. `mise run module:dev -- --modules category/name`: edit, see one summary line per save.
3. Add module tests; the template's `smoke.toml` only checks that the patched firmware is audible and does not fault.
4. Add a cell to `tests/patchkit/smoke/matrix.toml` for each device and firmware you support, then
   `mise run patch:test -- --cell "dt2-1.17:category/name" --update` records the frame goldens (hashes only) once,
   and later runs compare against them. A change that is invisible on screen must give the stock frames.
5. A module that changes sound needs a proof against a reference (PCM level, pitch, bit-identity at the neutral
   setting). The proofs are `tools/patchkit/pk_proof_*.py`; write one the same way and report numbers in a finding.
6. `mise run module:build -- --check` must pass (committed objects equal fresh builds).

## Worked example 1: `fx/master-gain` (SHARC+ only, both devices)

`modules/fx/master-gain/` scales the 64 floats of the final stereo block by a gain word before the firmware converts
them to the 24-bit output.

- **Object.** `src/dsp/master_gain.s` is placed in `dsp.l2`. It has two undefined symbols, the firmware's Q31 converter
  and the master float buffer. `[object.externs]` maps them to the anchors `dsp.master.q31_converter` and
  `dsp.master.float_buf`.
- **Hook.** One `retarget` op changes the converter's single `CALL` (anchor `dsp.master.q31_call`) to `_mg_hook`. The
  routine multiplies the block by the gain word and ends with a jump to the converter, so the converter's frame and
  arguments stay as the firmware expects. The header of the source lists the registers that are free at this site
  ([finding 06](../docs/findings/06-sharc-engine-and-startup.md), "Master hook").
- **Claim.** `dsp.master.hook`, so that it cannot be combined with `fx/master-drive`, which uses the same call.
- **Anchor rules.** Three `sharc_func`/`sharc_literal` rules, found by the relocation-insensitive hash of two DSP
  functions. These functions are identical on the Digitakt II and the Digitone II, so the same rules and the same
  object serve both.
- **Result.** Gain 1.0 (as built) is bit-identical to stock; a gain word of 0.5 gives -6.0206 dB
  ([finding 21](../docs/findings/21-patchkit-core.md)).
- **Test.** `tests/smoke.toml`: tap TRIG 1, expect audible output and no fault.

```sh
mise run module:build -- modules/fx/master-gain --check
mise run patch -- --firmware firmware/Digitakt_II_OS1.17.syx --modules fx/master-gain -o out/patch/mg.syx
mise run patch:explain -- --firmware firmware/Digitakt_II_OS1.17.syx --modules fx/master-gain
mise run module:dev -- --modules fx/master-gain
mise run patch:test -- --cell "dt2-1.17:fx/master-gain,test/cf-demo"
```

## Worked example 2: `machines/pluck` (both CPUs, built on frameworks)

`modules/machines/pluck/` adds a PLUCK machine (Karplus-Strong string) to the Digitakt II. It needs no ColdFire code
of its own: three frameworks do the firmware work, and the module only declares data.

- **`requires = ["core/dsp-voice", "core/machines", "core/params"]`** and `ports = ["dt2-1.16", "dt2-1.17"]`. Patch it
  with all four modules: `--modules core/dsp-voice,core/machines,core/params,machines/pluck`.
- **The DSP voice.** `[[object]]` places `src/dsp/pluck.s` in `dsp.l2` (`devices = ["dt2"]`). An `[[entry]]` in the
  `voices` registry of `core/dsp-voice` gives the machine type (`machines.pluck.index`) and the routine (`_pk_render`).
  The framework hooks the sample renderer once; a track whose type is registered renders with the routine, every
  other type renders as stock.
- **The machine.** An `[[entry]]` in the `machines` registry of `core/machines`: `name` and `short` (list and page
  header), `clone_of = 0` (take SAMPLE's permission, per-type byte and DSP selector), and `f0` to `f8`, the nine
  descriptor fields that choose which parameter occupies each knob of the SRC page. Fields are stock descriptor ids
  (`f0 = 0xca` is TUNE) or `"params.<id>.index"` for a parameter this module adds.
- **The parameters.** Four `[[entry]]` rows in the `params` registry of `core/params`: `name`, `short`, a `template`
  stock row to copy the formatter and value class from, `slot` (the engine slot the value travels in), `min`, `max`
  and `default`.
- **No numbers of the firmware's types.** The machine's type number is permanent: its row in
  `modules/core/machines/types.toml` (`mise run module:new -- machines/NAME --machine` adds the next free one). The
  parameter indices are assigned in canonical module order. The DSP entry and the machine entry both name `machines.pluck.index`, so they always agree.
- **Tests.** `tests/select.toml` opens the machine list, selects PLUCK, checks the committed type, the SRC page frame,
  that a trig sounds, that the DSP knows the type and that turning a knob changes the page. `tests/smoke.toml` checks
  that the module leaves the stock voice alone.
- **Gallery.** `README.md` lists the knobs and what projects saved with PLUCK do on stock firmware.

The frameworks themselves are ordinary modules: `modules/core/machines/` (about 24 ColdFire anchors, six copied
tables, the hooks in `src/cf/dt2/machines.s` and `src/cf/dn2/machines.s`), `modules/core/params/` and
`modules/core/dsp-voice/`. Their routines are unit-tested on the emulated ColdFire core (`tools/patchkit/pk_cfunit.py`,
`tests/patchkit/test_pk_machines_fw.py`, `tests/patchkit/test_pk_params_fw.py`), and the proofs are `tools/patchkit/pk_proof_machines.py` and
`tools/patchkit/pk_proof_pluck.py`. Contract and evidence: [finding 02](../docs/findings/02-machines-and-parameters.md).
