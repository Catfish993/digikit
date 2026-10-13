# Writing patchkit modules

A module is our own code (ColdFire C or assembly, SHARC+ assembly) plus a manifest, `module.toml`. patchkit links the
code into a stock Elektron `.syx`, places it in a free region, hooks firmware sites and writes a patched `.syx`.
How patchkit works and how to run it: [docs/PATCHING.md](../docs/PATCHING.md). This file is the authoring guide
and the manifest reference. Rules that apply to every module:

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
  gallery/README.md    the card text of the web gallery
```

Sources may sit in subfolders (`src/cf/dt2/`, `src/cf/dn2/`) when one module serves both devices. The directory path
must equal the module id.

## Conventions

- **Module id**: `category/name`, lower case, hyphens (`fx/master-gain`). The id is the path under `modules/`;
  patchkit refuses a manifest whose id differs from its directory.
- **Categories**: `core` (frameworks other modules require), `fx`, `machines`, `ui`, `tools`, `test` (fixtures and
  research probes; they are not for users). `mise run patch:new` accepts these.
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
- **Dependencies**: `requires` lists framework modules. The command line needs them listed in `--modules`; the
  gallery adds them.
- **`gallery/README.md`**: text for the gallery card. Write for a player: what it adds, which firmware, how to hear
  it, limits, "emulator only". The gallery renders a small Markdown subset: headings, paragraphs, `-` lists, fenced
  code blocks, `` `code` `` and `**bold**`. It has no tables, no links and no HTML. No firmware bytes, no vendor
  tool names.
- **Comments in sources** say what the code does at each firmware site and which registers are free there, and cite
  the finding that shows it (`docs/findings/NN-...`).
- **Findings**: write results into `docs/findings/` with the marks of AGENTS.md, one file per topic.

## Manifest reference

Numbers can be written as TOML integers or as strings holding an *expression* (see
[Expressions](#expressions)). patchkit ignores unknown keys in most tables, so a misspelled key is silent: check each key against the tables below. (Unknown fields of an `[[entry]]` are an error.)

### `[module]`

| Key | Meaning |
|---|---|
| `id` | `category/name`, equal to the directory path under `modules/` |
| `version` | module version, `major.minor.patch` |
| `name` | display name in the gallery |
| `category` | `core`, `fx`, `machines`, `ui`, `tools` or `test` |
| `status` | `experimental` or `stable` |
| `summary` | one sentence for the gallery card |
| `requires` | module ids that must be selected too (frameworks) |
| `conflicts` | module ids that must not be selected |
| `ports` | optional: only these ports, for example `["dt2-1.16", "dt2-1.17"]` (empty: any firmware whose anchors resolve) |

```toml
[module]
id = "fx/master-gain"
version = "0.1.0"
name = "Master gain"
category = "fx"
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
| `sources` | source files `patch:build` compiles into it (`.c`, `.s`), relative to the module; all of one CPU |
| `place` | region: `dsp.l2` (SHARC+ code and data), `cf.expansion` (ColdFire code, data, bss) or `cf.flash_cave` (only the built-in loader) |
| `devices` | optional: `["dt2"]`, `["dn2"]` or both (default: both) |
| `[object.externs]` | undefined symbol of the object = anchor name (a symbol named exactly like an anchor needs no entry) |

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

How `mise run port:resolve` finds a firmware site. It writes the result (address, length, SHA-256) into
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

To write a SHARC rule for a site: `uv run python tools/port_resolve.py rule-sharc dt2-1.17 0x1c7734 --len 6` prints the
hash and offset. ColdFire rules come from the function hashes of the `cfdb` database (`tools/cf.py`,
`tools/pk_machine_rules.py` as an example generator). Then run `mise run port:resolve` and check that the diff of
`devices/ports/` shows your anchors on every firmware you claim. Full syntax: the docstring of `tools/port_resolve.py`.

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
| `[[registry.field]]` | `name`, `kind`, `max_len` (`cstr`), `default` |

Field kinds: `u8`, `u16`, `u32`, `cstr` (a pointer to a string placed behind the rows; `max_len` checks the length)
and `sym` (an expression: a symbol of any module or `<registry>.<id>.index`). Rows use natural alignment, the row
size is rounded to the widest field, the byte order is the region's CPU. A field with no `default` must be given by
every entry.

patchkit defines `<reg>.first`, `<reg>.count`, `<reg>.rows`, `<reg>.row_size` and `<reg>.<id>.index` (= first + k,
entries in canonical module order).

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

A content module adds a row to a framework's registry. Keys: `registry`, `id`, `devices`, and one key per registry
field. Numbers, strings for `cstr` fields and expressions or symbols for `sym` and numeric fields.

```toml
[[entry]]
registry = "voices"
id = "pluck"
type = "machines.pluck.index"     # the type number core/machines assigns to the machine below
pc = "_pk_render"                 # the routine, a symbol of this module's DSP object
devices = ["dt2"]
```

patchkit refuses an entry for an unknown registry, an unknown or missing field, a value that does not fit, a
`cstr` that is too long and an id used twice.

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
quick = true                # also run by `patch:dev` after every edit

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
main-loop stall. The complete list is the docstring of `tools/pk_harness.py`.

Workflow:

1. `mise run patch:new -- category/name` and fill in the manifest and the sources.
2. `mise run patch:dev -- --modules category/name`: edit, see one summary line per save.
3. Add module tests; the template's `smoke.toml` only checks that the patched firmware is audible and does not fault.
4. Add a cell to `tests/patchkit_smoke/matrix.toml` for each device and firmware you support, then
   `mise run patch:test -- --cell "dt2-1.17:category/name" --update` records the frame goldens (hashes only) once,
   and later runs compare against them. A change that is invisible on screen must give the stock frames.
5. A module that changes sound needs a proof against a reference (PCM level, pitch, bit-identity at the neutral
   setting). The proofs are `tools/pk_proof_*.py`; write one the same way and report numbers in a finding.
6. `mise run patch:build -- --check` must pass (committed objects equal fresh builds).

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
mise run patch:build -- modules/fx/master-gain --check
mise run patch -- --firmware Digitakt_II_OS1.17.syx --modules fx/master-gain -o out/patch/mg.syx
mise run patch:explain -- --firmware Digitakt_II_OS1.17.syx --modules fx/master-gain
mise run patch:dev -- --modules fx/master-gain
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
- **No numbers of the firmware's types.** patchkit assigns the type number and the parameter indices in canonical
  module order. The DSP entry and the machine entry both name `machines.pluck.index`, so they always agree.
- **Tests.** `tests/select.toml` opens the machine list, selects PLUCK, checks the committed type, the SRC page frame,
  that a trig sounds, that the DSP knows the type and that turning a knob changes the page. `tests/smoke.toml` checks
  that the module leaves the stock voice alone.
- **Gallery.** `gallery/README.md` lists the knobs and what projects saved with PLUCK do on stock firmware.

The frameworks themselves are ordinary modules: `modules/core/machines/` (about 24 ColdFire anchors, six copied
tables, the hooks in `src/cf/dt2/machines.s` and `src/cf/dn2/machines.s`), `modules/core/params/` and
`modules/core/dsp-voice/`. Their routines are unit-tested on the emulated ColdFire core (`tools/pk_cfunit.py`,
`tests/test_pk_machines_fw.py`, `tests/test_pk_params_fw.py`), and the proofs are `tools/pk_proof_machines.py` and
`tools/pk_proof_pluck.py`. Contract and evidence: [finding 02](../docs/findings/02-machines-and-parameters.md).
