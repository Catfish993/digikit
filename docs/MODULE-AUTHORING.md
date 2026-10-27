# Writing a digikit module

This is the guide for content modules. A content module adds a machine, an effect or a service to a Digitakt II or a Digitone II. You build it
into a `.dkm` file. The digikit runtime loads the file from the card at boot. You do not patch a firmware image for it.

Run every command from the repository root. Nothing here touches hardware. Flashing is a separate step (see "Safety and hardware" in
[PATCHING.md](PATCHING.md)). No file in the repository contains firmware bytes.
The SDK (headers, includes, templates) is described in [sdk/README.md](../sdk/README.md).

## 1. What a module is

There are three kinds of content module. The kind is `[module] type` in `module.toml`.

| Kind | What it does | Language | Entry points | Template |
| --- | --- | --- | --- | --- |
| `machine` | A new sound source. It renders one voice. | SHARC+ assembly | `render`, `note_on`, `desc` | `sdk/templates/machine` |
| `fx` | An effect on the master block or on a send bus. It has its own FX page. | SHARC+ assembly | `process`, `desc` | `sdk/templates/fx` |
| `service` | ColdFire code. It adds commands, reads the store or reacts to the UI loop. | C | `dkm_init`, `dkm_fini`, `dkm_slot_N` | `sdk/templates/service` |

Two more types exist. `raw` is code with no contract and only the generic load checks. The menu shows it as unchecked. `framework` is
the internal type of the `core/*` modules.

The `core/*` modules are patched into the firmware. They are not content modules. They are described in [modules/README.md](../modules/README.md).
The author `core` is reserved for them. The author `test` is reserved for fixtures. Use your own author, or `digikit` for the project's modules.

## 2. Set up

Install once:

```sh
mise install                  # uv, pnpm, Rust and the other pinned tools
mise run setup                # uv sync and pnpm install
mise run patch:toolchains     # the ColdFire (m68k) compiler and the SHARC+ assembler, into out/tools
```

`patch:toolchains` takes about 15 minutes. It installs into `out/tools`, never globally.

To run a module in the emulator you also need your own stock firmware in `firmware/`: `Digitakt_II_OS1.17.syx` and `Digitone_II_OS1.12.syx`.
Never commit these files.

Check the setup after you create your first module (section 3):

```sh
mise run module:build -- modules/machines/NAME --check     # fails when build/*.dko.json is stale
```

## 3. Start a module

```sh
mise run module:new -- KIND AUTHOR/NAME [--dir DIR] [--title T] [--short S] [--no-build]
```

| Part | Meaning |
| --- | --- |
| `KIND` | `machine`, `fx` or `service`. |
| `AUTHOR/NAME` | The module id. The author has lowercase letters, digits and dashes. The name is lower snake case. `core` and `framework` are refused. |
| `--dir DIR` | Write `DIR/NAME` instead of the repository folder. |
| `--title T` | Display name, 1 to 23 characters. Default: the name in Title Case. |
| `--short S` | Short name, 3 to 6 characters of A-Z and 0-9. Default: the first 4 letters of the name in upper case. |
| `--no-build` | Write the files and the generated headers, but do not compile. |

Examples:

```sh
mise run module:new -- machine alice/drone
mise run module:new -- fx alice/crunch --title Crunch --short CRU
mise run module:new -- service alice/hello --dir ~/my-modules
```

The command copies the template, replaces the names, runs `patchkit gen` for the module and builds it. You start from code that compiles.

| Kind | Folder | Files you get |
| --- | --- | --- |
| `machine` | `modules/machines/NAME` | `module.toml`, `README.md`, `src/dsp/NAME.s` (a sine voice with one level parameter) |
| `fx` | `modules/fx/NAME` | `module.toml`, `README.md`, `src/dsp/NAME.s` (a gain effect with one knob and a page) |
| `service` | `modules/tools/NAME` | `module.toml`, `README.md`, `src/c/NAME.c` (`dkm_init` logs a line, `dkm_fini` is empty) |

The command ends with the next commands: `module:check`, `module:pack` and `dk:run -- AUTHOR/NAME --emu --seconds 10` (section 8).
`dk:run --emu` needs a running emulator: start `mise run workbench` in another shell first.

For a folder outside the repository, list the parent folder in `PATCHKIT_MODULES` (several folders: separated by `:`). The module tasks find
the module through it. With `--dir`, `module:new` prints the variable you need.

```sh
export PATCHKIT_MODULES=~/my-modules
mise run module:check -- alice/hello
```

## 4. The module folder

```text
module.toml        the manifest
src/dsp/NAME.s     SHARC+ assembly (machine, effect)
src/c/NAME.c       ColdFire C (service)
tests/             the module's own tests: test_*.py
README.md          for a player: what the module does, in one or two sentences
gallery/icon.svg   optional card icon
build/             built objects: *.dko.json
build/gen/         generated headers and tables (section 6)
```

Commit `build/`, including `build/gen/`. The browser build has no compilers: it builds firmware and `.dkm` files from these objects. A diff of
`build/` also proves what a source change did.

Do not edit anything in `build/gen/`. A `.dkmod` export leaves out `build/` (section 10).

## 5. The manifest

`module.toml` has plain sections for every module and typed sections for the kind. The device profile (`devices/digitakt-ii.toml`,
`devices/digitone-ii.toml`) fills in what is specific to a device. You write one manifest for both devices.

### 5.1 Sections for every module

`[module]`:

| Key | Meaning |
| --- | --- |
| `id` | `AUTHOR/NAME`. |
| `version` | `major.minor.patch`. Change it for every release. |
| `name` | Display name. |
| `category` | `machines`, `fx`, `ui`, `tools` or `test`. A group name for the folder and the gallery. |
| `type` | `machine`, `fx`, `service` or `raw`. |
| `author` | The author part of the id. |
| `status` | `experimental` or `stable`. |
| `summary` | One sentence for players, 120 characters at most, ending with a full stop. |
| `dynamic` | `true` for a run-time `.dkm`. The machine and effect templates set it. |
| `ports` | Firmware versions the module names: `["dt2-1.17", "dn2-1.12"]`. |
| `requires`, `conflicts` | Other module ids. Empty for most content modules. |

`[targets]` records the test status for each firmware: `untested`, `experimental` or `stable`.

`[[object]]` names a built object:

| Key | Meaning |
| --- | --- |
| `path` | The built object, for example `build/NAME-dsp.dko.json`. |
| `sources` | The sources `module:build` compiles into it. All sources of one object are for one CPU. |
| `place` | `dsp.l2` for SHARC+ code, `cf.expansion` for ColdFire code. |
| `devices` | Optional: `["dt2"]`, `["dn2"]` or both. Default: both. |

### 5.2 Machine sections

`[[voice]]` names the DSP symbols. One body serves both devices.

| Key | Meaning |
| --- | --- |
| `machine` | The `id` of the `[[machine]]`. |
| `render` | The render routine's symbol. |
| `note_on` | The note-on routine's symbol. |
| `desc` | The data symbol of the `dk_machine_desc` (`<dk/machine.h>`). |

`[[machine]]`:

| Key | Meaning |
| --- | --- |
| `id` | The machine's name inside the module. |
| `name` | Display name, 23 characters at most. |
| `short` | Label in the machine list. |
| `clone_of` | The stock machine whose page layout it copies. `0` is SAMPLE. |
| `knobs` | Parameter ids, in the order of the DSP `params` words. Names only. |
| `position` | Optional: place in the machine list. Default: last. |
| `level` | Optional: level code. Default: the device profile. |
| `own_dsp` | Optional. Derived from the `[[voice]]` and the profile; set it only to override. |
| `devices` | Optional: restrict to one device. |

`[[param]]`:

| Key | Meaning |
| --- | --- |
| `id` | The parameter's name. Do not rename it after a release. |
| `name` | Name shown while the knob turns, 31 characters at most. |
| `short` | Label under the knob, 15 characters at most. |
| `template` | A template name from the device profile. Default: `"plain"`, a plain 0 to 127 value. |
| `min`, `max`, `default` | Words, 0 to 0x7f00. The UI value is the word divided by 256. |
| `devices` | Optional: restrict to one device. |

The adapter clamps each word to `min` .. `max` before your code reads it. The build does not check that `default` lies inside the range. Keep it true yourself.
`pack` refuses `min` above `max`. Names are printable ASCII. There is no `kind` key. The kind is a convention for the word:

| Kind | Word range | UI | Rest value | Use for |
| --- | --- | --- | --- | --- |
| percent | 0 .. 0x7f00 | 0 .. 127 | none, use `default` | amounts, times, mix, decay |
| bipolar | 0 .. 0x7f00 | -64 .. +63 | 0x4000 | detune, pan, tilt |
| enum | step x 256 | step number | step 0 | a switch or a mode with n steps |
| pitch | 0 .. 0x7f00 | semitone offset | 0x4000 | a pitch offset the adapter adds to `pitch` |

For an enum, set `max` to (n-1) x 256. No module uses an enum yet. Only the templates `0xd1` (Digitakt II) and `0xd5` (Digitone II) are known
to give a plain 0 to 127 display. A template with a signed or named display is not known.

Scaling the word: the stock DSP converts a word to a float with the scale 1/32512 (0x7f00 = 1.0). Your routine may use any scale. Document
it in the source header, as `modules/machines/pluck/src/dsp/pluck.s` does.

`[[page]]` (Digitone II only):

| Key | Meaning |
| --- | --- |
| `id`, `machine`, `name`, `title` | The page id, the machine it belongs to, the tab name and the page title. |
| `knobs` | Optional. Default: the machine's knobs on that device. |
| `devices` | `["dn2"]`. A page on the Digitakt II is a build error. |

### 5.3 What the device profile fills in

You write parameter names. The profile (`[modules.knobs]`, `[modules.templates]`, `[modules.frame]` and `[adapter.machine]` in
`devices/<device>.toml`) places them.

| Item | Digitakt II | Digitone II |
| --- | --- | --- |
| Where knobs live | Fields of the machine row. Engine slots 0 to 7. | Columns of a `[[page]]`. Sound-array slots 25 to 32. |
| Fixed knobs | TUNE on knob 0. LEV on knob 7. | None. |
| Knobs for your parameters | 1, 2, 4, 5 (`param_slots`). The parameters fill them in order. | 0, 1, 2, 3. |
| Reserved knob | Knob 3 of a SAMPLE clone is the sample slot. The build refuses a parameter there. | None. |
| Template `plain` | `0xd1` | `0xd5` |
| `own_dsp` | Not allowed. | Derived as true when the machine has a `[[voice]]`. |

Rules that follow:

- Only the knobs in `param_slots` reach the DSP. A machine has at most 4 contract parameters today. The build refuses more than the profile places.
- A parameter's position is its slot. Projects store values by slot. After a release, do not move a parameter to another knob.
  `module:export --previous` checks this. Removing a parameter is allowed.
- A parameter that no machine of a device uses is left out on that device.
- `type_hint` is gone. The runtime assigns the machine's type number at load. Never write a number into code or a test.

A per-device table is only for unusual layouts. `[machine.dt2]` and `[machine.dn2]` may set `name`, `short`, `clone_of`, `position`, `own_dsp`,
`knobs` and `level`. A per-device `knobs` is an explicit layout. It may hold stock codes (`0xca` TUNE, `0xd1` LEV) and `0` for an empty knob.
`[param.dt2]` and `[param.dn2]` may set `name`, `short`, `template`, `min`, `max` and `default`. A template number is allowed only there,
because it is specific to one device. Most modules need none of these tables.

### 5.4 Effect sections

An effect is declared in C, in a source file of the module (by convention `src/module.c`). List the file as an `[[object]]` with
`place = "cf.expansion"`. The file holds only the `.dk.desc` record. `patchkit pack` reads it and leaves it out of the `.dkm`; the device never sees it.
(Machines still use the manifest sections until phase 2 of `docs/plans/sdk-api.md`. A manifest `[[fx]]` is refused.)

```c
#include <dk/fx.h>
#include "freqshift.h"

DK_FX(fshift, .process = "_fs_process", .desc = "_fs_desc",
      .tab = "FSH", .title = "Freq Shifter", .mix = 127,
      .route = DK_ROUTE_SEND, .routes = DK_ROUTE_SEND | DK_ROUTE_MASTER,
      .knob = {DK_KNOB(FSHIFT_SHIFT_WORD, "Freq Shift", "SHIFT", DK_BIPOLAR, -64, 63, 0)});
```

`DK_FX(ID, ...)`: `ID` is a C identifier. It is the effect's name inside the module. With the module id it makes the effect ID of saved kits.
Never rename either. The other arguments are field initializers:

| Field | Meaning |
| --- | --- |
| `.process`, `.desc` | The global symbol of the process routine. The data symbol of its `dk_fx_desc`. Strings. |
| `.defaults` | Words p0 and up at load. Not allowed together with knobs. Default: all 0. |
| `.tab` | The page tab label, 1 to 3 characters. An effect with a `tab` has its own page after CHO in FUNC + FX. |
| `.title` | The page title, 1 to 23 characters. Use mixed case ("Freq Shifter"). |
| `.mix` | Default of the framework's MIX knob, 0 to 127. Default: 127. |
| `.route` | Where the effect starts: `DK_ROUTE_MASTER` (default) or `DK_ROUTE_SEND`. A send needs a `.tab`. |
| `.routes` | The routes the user may choose in SETTINGS > DIGIKIT > FX ROUTES, `DK_ROUTE_*` values or-ed. Left out: master only, or send and master when `.route` is send. |
| `.knob` | Up to 7 knobs: `DK_KNOB(...)` or `DK_KNOB_AT(...)`. |

`DK_KNOB(WORD, NAME, SHORT, DISPLAY, MIN, MAX, DEF)` declares knob `WORD`, the parameter word p_WORD, a float. It sits at page position `WORD`.
`DK_KNOB_AT(WORD, POS, NAME, ...)` sets the page position `POS` (0 to 7) apart from the word.

| Argument | Meaning |
| --- | --- |
| `NAME` | Name shown while the knob turns, 1 to 23 characters. |
| `SHORT` | Label under the knob, 1 to 6 characters. |
| `DISPLAY` | `DK_PERCENT` (0 to 127) or `DK_BIPOLAR` (-64 to +63). |
| `MIN`, `MAX` | The floats at the bottom and the top of the knob. |
| `DEF` | Inside `MIN` .. `MAX`. It is also the word's value at load. |

Text longer than its field is a compile error. `patchkit pack` checks the rest: lengths, ranges, routes, `mix`, distinct positions and the
knob slots of the D1 device. Knob placement is the same on both devices. The device profile does not change it.

Put the word numbers in a header in the module's `src/` (`freqshift.h`). The C file and the SHARC+ assembly both include it, because the build
searches `src/` for both. Use only decimal `#define`s and `//` comments on their own line: the SHARC+ assembler keeps a trailing comment in the macro text and upper-cases macro text.

`patchkit desc MODULE_ID` prints the effects of a module as JSON (`{"fx": [...]}`), from the `DK_FX` records.

MIX, the route and the send amounts belong to the framework. Never declare them. The effect writes the wet signal only.

### 5.5 Service sections

A service has `[module]`, `[targets]` and one `[[object]]` with `place = "cf.expansion"` and a C source. It has no `[[voice]]`, `[[machine]]`,
`[[param]]`, `[[page]]` or `[[fx]]`. The service template does not set `dynamic`.

### 5.6 Example: PLUCK

`modules/machines/pluck/module.toml` (shortened):

```toml
[[object]]
path = "build/pluck-dsp.dko.json"
sources = ["src/dsp/pluck.s"]
place = "dsp.l2"

[[voice]]
machine = "pluck"
render = "_pk_render"
note_on = "_pk_note_on"
desc = "_pk_desc"

[[machine]]
id = "pluck"
name = "Pluck"
short = "PLK"
clone_of = 0
knobs = ["pluck_decay", "pluck_pick", "pluck_bright", "pluck_color"]

[[param]]
id = "pluck_decay"
name = "Decay"
short = "DECAY"
min = 0
max = 0x7f00
default = 0x4000
# ... pluck_pick, pluck_bright, pluck_color in the same form

[[page]]
id = "pluck_syn"
machine = "pluck"
name = "PLUCK"
title = "Pluck"
devices = ["dn2"]
```

The profile turns this into the layout in `modules/machines/pluck/build/gen/dt2.inc`: DECAY on knob 1, PICK on knob 2, BRIGHT on knob 4, COLOR on
knob 5. TUNE and LEV are added on knobs 0 and 7. On the Digitone II the four parameters sit on page columns 0 to 3.

For an effect, see `modules/fx/freqshift/module.toml`. It has one bipolar knob, `route = "send"` and `routes = ["send", "master"]`.

### 5.7 Build errors, persistence and limits

Errors for `knobs` and parameters (the message starts with the module and the machine):

| Cause | Message (start) |
| --- | --- |
| More knobs than the device has | `9 knobs, but dt2 has 8` |
| Parameter on a reserved knob | `param "x" sits at knob 3, but knob 3 of a SAMPLE clone is the sample slot` |
| A name that is no `[[param]]` | `knob 1 names "x", which is not a [[param]] of this module` |
| A `[[param]]` limited to another device | `knob j names "x", which is not a param of dn2` |
| A parameter twice in one list | `knob 2 names "x", which knob 1 already names` |
| One parameter at two positions (two machines, or machine and page) | `param x sits at knob 1 and knob 2; one param has one slot` |
| A `[[param]]` with explicit `devices` that no machine of the device uses | `param x: no machine of dn2 uses it` |
| `own_dsp` on the Digitakt II | `dt2 has no own_dsp machines` |
| A stock code in a page | `a stock code on a page is not supported yet (name a param)` |
| A page on the Digitakt II | `a second SRC page for a Digitakt II machine is not supported yet` |

Not refused today: a `default` outside `min` .. `max`, and a parameter on a knob that is not in `param_slots` (it is stored, shown and saved, but
no `params` word carries it).

Layout facts:

- Digitakt II: the machine row holds the knobs as `f0..f7` and the level as `f8`. A page shows 8 of the 10 engine slots. An added machine has one
  SRC page. A SAMPLE clone has TUNE on knob 0, PLAY 1, SAMP 3, STRT 4, LEN 5, LOOP 6 and LEV 7. Any slot but knob 3 may take a module parameter.
- Digitone II: a stock page holds eight knobs and the track level. A machine with its own page sets `own_dsp`. The typed `[[page]]` gives one
  page for each machine. A second page needs hand-written `registry = "pages"` rows and is possible only in a patched-in module.
- Limits per module (run-time pack): 16 parameters, 8 machines, 8 pages, 32 records in a register section.

Persistence and modulation:

- Values persist on both devices. Projects store raw words by slot, so a released parameter must keep its knob.
- If the module is absent at load, the track keeps its type and saves it back unchanged. Stock firmware loads the track as SAMPLE
  (Digitakt II) or FM TONE (Digitone II) and loses the type on re-save.
- p-lock on a module parameter: not shown. LFO as a destination: the MOD page lists the parameter names on the Digitakt II, but that an LFO moves
  the DSP value is not known. The clamp stays on, because an LFO can drive a word out of range.
- MIDI CC and NRPN are ignored for added parameters. Effect knobs cannot be locked or modulated either.

Recipe for N knobs: N is at most 4 contract parameters. Put them on Digitakt II knobs 1, 2, 4, 5 in the order the DSP reads them, and on Digitone II
knobs 0 to 3. A fifth control on the Digitakt II is a stock row (`0xca`, `0xd1`) or a stored-only parameter. Run `module:check` with `--device dt2`
and `--device dn2`.

## 6. Generated files

`patchkit gen` writes the files in `build/gen/` from `module.toml`, the device profiles and `sdk/sdk.toml`. They hold only numbers that the module
and the profiles declare. They hold no firmware bytes.

| File | Written for | What it holds | Who includes it |
| --- | --- | --- | --- |
| `build/gen/module.h` | machine | C `#define`s of the numbers that are the same on every device: per parameter its index and range. | C code of the module. |
| `build/gen/dt2.inc`, `build/gen/dn2.inc` | machine | Per-device numbers for assembly: frame base and stride, and for each parameter its knob, slot, frame offset, mirror index and range. | The machine's `.s`: `#include "dt2.inc"`. `module:build` expands it from `build/gen/`. |
| `build/gen/consts.rs` | machine | The same per-device numbers as Rust constants. | A Rust module crate: `include!("consts.rs");`. |
| `build/gen/gallery.json` | machine | Machines with their knobs and parameters, for each device. | The web gallery. |

Device-independent numbers (`module.h`) are the same on both devices. If a value differs between devices, `patchkit gen` refuses to write it
there. Per-device numbers (`<dev>.inc`) depend on the profile. One example is the frame offset of a knob. Use the name, for example
`PLUCK_DECAY_FRAME_OFF`, and never the literal number.

The SDK headers are generated from `sdk/sdk.toml` in the same way. Modules include the public headers in `sdk/include/dk/`.
The generated files are in `sdk/include/dk/gen/` and `sdk/asm/`.

| File | What it holds | Who includes it |
| --- | --- | --- |
| `<dk/api.h>` | The `dk_api` table, the import names and the entry points of a `.dkm`. | Service modules in C. |
| `<dk/machine.h>` | The machine contract: `dk_voice_in`, `dk_machine_desc`, the entry points. | Reference for machines. Includes `machine_consts.h`. |
| `sdk/include/dk/gen/machine_consts.h` | The machine constants as C `#define`s. | `<dk/machine.h>`. |
| `sdk/asm/dk_machine.inc` | The machine constants and field offsets for assembly. | The machine's `.s`. |
| `<dk/fx.h>`, `sdk/include/dk/gen/fx_consts.h` | The effect contract in C and its constants. | Reference for effects. |
| `sdk/asm/dk_fx.inc` | The effect constants and field offsets for assembly. | The effect's `.s`. |

Do not include `dk_fx.inc` and `dk_machine.inc` in one assembly file. `DK_BLOCK` and `DK_LEVEL_BOUND_BITS` are in both.

Do not edit generated files. To write them again:

```sh
mise run gen:modules                                     # all modules and the SDK
mise run gen:modules -- --check                          # list stale files, write nothing
uv run python -m tools.patchkit.patchkit gen --modules AUTHOR/NAME    # one module
```

Run `gen:modules` after you change `module.toml`, `devices/*.toml` or `sdk/sdk.toml`.

## 7. Contracts

The headers are the reference. This section lists what you need to know before you read them.

### 7.1 Machine voice (`<dk/machine.h>`)

| Entry | When it runs | Result |
| --- | --- | --- |
| `note_on(state, in, params)` | Once per note, before the first `render`. It makes no sound. | `0`, or a negative number (a fault). |
| `render(state, in, params)` | Once per block of `DK_BLOCK` (32) samples while the voice runs. | `DK_OK`, or `DK_DONE` when the voice has ended. |
| `desc` | A `dk_machine_desc` in read-only data: magic, contract version, parameter count, voices, state bytes, bytes cleared at note-on, cycle budgets. | Read by the adapter. |

`render` writes `DK_BLOCK` floats to `in->out`. `in` also holds `pitch` (semitones shifted left by 8, the tune included), `velocity` and `block`.
`params` holds `DK_MAX_PARAMS` words. The words after your parameters are 0.

The adapter enters your routine with `I13` = return address and `JUMP (M13, I12)`. The arguments are `R4` = state, `R8` = `dk_voice_in`,
`R12` = `params`. The result is in `R0`. In `dk_voice_in` the words are: 0 `pitch`, 1 `velocity` (0 to 127, shifted left by 8), 2 `block`
(blocks since the note-on), 3 `out` (pointer to 32 floats). `render` also runs on the note-on block, after `note_on`.
Do not place `note_on` at offset 0 of the image: an offset of 0 reads as "no note_on".

The `dk_machine_desc` has 10 words: magic, contract, parameter count, voices (1 to 16), state bytes per voice (a multiple of 4), bytes cleared at
note-on, max cycles of a block, max cycles of the note-on block, max call depth, stack bytes.

The adapter in `core/dsp-voice` zeroes `state_clear_bytes` at note-on and clamps the parameter words to your ranges. After each call it checks the
output. A block with a sample that is not finite or is louder than 8.0 is zeroed. The voice ends. After 8 faults in a second the machine is
switched off.

Registers:

- You may change R0-R15, I0-I5, M1-M4 and B3.
- Keep I6-I15, M0, M5-M15, L0-L7 and MODE1.
- The adapter pins M5 = 0, M6 = 1, M7 = -1, M13 = 0, M14 = 1 and M15 = -1, L0-L5 = 0, SIMD off and RND32 set. Return with `JUMP (M13, I13)`.
- `_dv_exp2` (F0 = 2^F0) is a helper of `core/dsp-voice`. Declare it with `.EXTERN _dv_exp2;`. It clobbers R0 to R3.
- A 16-bit form `Rn = Rn op Rx` can be read differently by the emulator and the silicon. The build refuses it. Use three distinct registers.
  Other assembler traps: `.agents/skills/digikit-module/references/selas-pitfalls.md`.

Memory: your code may write `in->out`, its own voice's state, its data stack (`stack_bytes`) and the data and bss of its own object. Data and bss
are shared by all voices. State that differs per voice goes in `state`. The conformance suite records every store and fails on any other.

The template `src/dsp/NAME.s` shows all of this in about 90 lines. Read it first. Pitch tests: `tests/patchkit/test_sine.py`.

### 7.2 Effect (`<dk/fx.h>`)

| Entry | When it runs | Result |
| --- | --- | --- |
| `process(state, in, params)` | Once per block while the effect is on. | `DK_FX_OK` (0), or a negative number. A negative number is a fault: the block plays dry. |
| `desc` | A `dk_fx_desc` in read-only data: magic, contract version, `n_params`, `state_bytes`, cycle budget, call depth, stack bytes. | Read by the loader. |

- `in->in` is the input block: `DK_FX_BLOCK` (64) floats, planar. 32 left, then 32 right. It is read only.
- `in->out` is the output block of 64 floats. Write all of them.
- `params` holds `DK_FX_MAX_PARAMS` (8) words. Knob i is word p_i and arrives as a float. A page has at most 7 knobs, so p7 is free for a constant.
  `n_params` in the `desc` must cover your knobs.
- The runtime reserves `state_bytes` and passes the address in R4. It zeroes the state when the effect switches on.
- The registers and the entry convention are the same as for a machine.
- A fault, a sample that is not finite or a sample louder than 8.0 makes the block play dry. 8 faults in 1500 blocks switch the effect off.
- MIX, the route and the send amounts belong to the framework. Do not assume you are last in the chain. Write the wet signal only.
- Limits: `fx_slots` effects at a time (3 on both devices). `send_slots` of them on a send bus (2 on the Digitakt II, 1 on the Digitone II). More
  effects stay inserts.

- Arguments: `R4` = state, `R8` = `dk_fx_in` (words: 0 `in`, 1 `out`, 2 `block`), `R12` = `params`. The result is in `R0`.
- Switching on or off is a linear crossfade over one block that the adapter makes. While the effect is off it is not called, so a tail (reverb,
  delay) freezes.
- Data stack: `stack_bytes` below `I7`. Write nothing outside your own data. Include `dk_fx.inc`, not together with `dk_machine.inc`.
- The runtime reserves the state behind your image. There is no state symbol. A page needs no ColdFire code.
- Values arrive in `params` as the float `min + (max - min) * raw / 0x7f00`, about 3 ms after a turn. At load every word is its knob's `default`.
  Choose the range so that a turn is audible.
- The adapter makes `master = dry + mix * (wet - dry)` per sample, with mix = raw / 127. MIX 127 is exactly the wet signal and 0 is exactly the
  dry one. It costs about 7 DSP instructions a block at mix 1.0 and about 400 at another mix.
- Kits keep the knob values and MIX of each loaded effect, with an effect ID made from the module id and the effect `id`. Load Kit gives an
  effect its stored values only when the stored ID for its row is its own. Otherwise the effect gets its manifest defaults. Effects count by row,
  so the same effects loaded in another order get defaults. A kit saved with effects also loads on stock firmware.
- Not supported: p-locks, LFO and other modulation on a knob, MIDI CC, a second page for one effect, and a knob that does not reach the DSP as a
  float. An image without `core/fxpages` refuses a module with a page (`E_NO_REGISTRY`). The Digitone II image needs `core/dynfx` and
  `core/fxpages` in its module list (they are in its `device` patch set).
- Text is printable ASCII. Write titles in mixed case. The firmware font draws a capital Q like an O.
- Routes: a send effect gets the sum of the tracks' send amounts as its input block. Its wet output times MIX is added to the FX return. The
  user chooses the route in SETTINGS > DIGIKIT > FX ROUTES (the setting `fx/route/<module id>`). A stored route that the effect no longer allows is
  ignored. Use `routes = ["master"]` for a gain or limiter or any effect with a dry path of its own, `["send"]` for a pure return such as a reverb, and
  both for the rest. The send amounts live in the settings store and the DSP row, never in your state or in a kit file. FUNC + FX lists DEL, REV,
  CHO, the send effects, then the master effects. A changed route takes effect after a restart. `pack` refuses an unknown or repeated route, an
  empty `routes`, a `route` that `routes` does not list, and any send route without a `tab`.
- A centred knob: `display = "bipolar"`, `min = -64.0`, `max = 63.0`, `default = 0.0`. A mapping with `2^x - 1` and no constant term gives exactly 0.
  A send effect with MIX default 127 changes nothing until a track has a send amount. On the master the same default means fully wet, so say in the
  README what it means there.
- Clamp the output to +-7.9. An input at the level bound times a filter's overshoot would otherwise refuse the block. Use an integer phase
  accumulator for anything that must not drift.
- Fixtures: `tests/fixtures/modules/dyn-fx-gain` (insert), `dyn-fx-gain-send`, `dyn-fx-gain-both`, `dyn-fx-gain-sendonly`. A real effect with a
  float32 reference model: `modules/fx/freqshift`.
- The instruction budget of the conformance suite is `pk_conform.fx_budget()` (about 31,000 for the effect today, 900 for the adapter). Read it
  from the report (`-rA`).

### 7.3 Service (`<dk/api.h>`)

A service is C code for the ColdFire CPU. The runtime calls these symbols of your object:

| Symbol | When it runs |
| --- | --- |
| `int32_t dkm_init(const dk_api *api)` | Once after load. Return 0. Any other value unloads the module. |
| `void dkm_fini(const dk_api *api)` | When the module is unloaded. |
| `void dkm_slot_N(const dk_api *api)` | `N` is 0 to 7. You bind a slot by exporting its symbol. Slot 0 runs once per pass of the UI event loop, about 120 Hz. A taken slot gives `E_SLOT`. |

Export at least one of them.

The `dk_api` table that `dkm_init` receives. Fields are only appended, each is 4 bytes. Check `size` before you use a late field.

| Field | Purpose |
| --- | --- |
| `version`, `size`, `device`, `runtime` | Table layout version, table size, the device id, the packed runtime version. |
| `import(name)` | Address of a firmware function or runtime helper by name. 0 when the image has none. |
| `alloc(size)`, `free(p)` | A 16-byte aligned block from the module arena, and its release. `alloc` returns null when full. |
| `store_read(path, dst, cap)` | Read a store file. Returns its size, or a negative code. |
| `log(msg, value)` | Append to the runtime log. API 1.0. |
| `log_lv(level, msg, value)` | The same with a level. API 1.1. |
| `register_cmds(first, count, handler)` | Register SysEx commands. API 1.2. |
| `memcpy_addr`, `memset_addr`, `memcmp_addr`, `muldi3_addr`, `udivdi3_addr`, `umoddi3_addr` | Addresses of the runtime's helpers. The compiler calls the 64-bit ones by itself. |

Names a module may import (from `[[import]]` in `sdk/sdk.toml`):

| Name | Purpose |
| --- | --- |
| `fw_block_read`, `fw_alloc`, `fw_free` | The firmware's eMMC block read and allocator. |
| `memcpy`, `memset`, `memcmp`, `__muldi3`, `__udivdi3`, `__umoddi3` | Runtime helpers. |
| `input_key`, `input_turn` | Press a key or turn an encoder. They need `core/input`. Without it the import is not found. |
| `remote_enabled` | 1 when the REMOTE switch of the DIGIKIT menu is on. |
| `dkrt_blocks` | The 1500 Hz block clock. |

A load fails with `E_IMPORT` when a name is not in the runtime's table.

`register_cmds` adds SysEx commands. Call it from `dkm_init`. The range is released after `dkm_fini`. The range must lie inside a range reserved in
`sdk/sdk.toml` under `[protocol.ranges]`. Two ranges exist today, and `digikit/remote` uses both:

| Range | Numbers |
| --- | --- |
| `remote` | `0x38` to `0x3d` (guarded: refused while DIGIKIT > REMOTE is off) |
| `remote_state` | `0x3e` to `0x3f` |

Another range is a change to `sdk/sdk.toml`. Run `mise run gen:modules` after it. A handler runs in the firmware's MIDI task. Keep it short and never
wait. The request body is at most 160 bytes. The reply body is at most 879 bytes. Return the reply length, or a negative status.

## 8. Build, check, pack, run

| Task | Command | What it does |
| --- | --- | --- |
| Build | `mise run module:build -- PATH [--check]` | Compiles the sources to `build/*.dko.json`. `PATH` is the module folder. `--check` fails when the objects are stale. |
| Check | `mise run module:check -- ID [--device dt2\|dn2]` | See below. |
| Memory audit | `mise run module:audit -- NAME [--device dt2\|dn2]` | Loads the module on a ColdFire-only emulator, drives the UI and reports ColdFire stores outside the module's blocks. About 10 to 30 s per device. |
| Pack | `mise run module:pack -- ID --device dt2\|dn2 [-o FILE]` | Makes the `.dkm`. Default: `out/patch/NAME-DEVICE.dkm`. It refuses a bad page, a bad route and a send without a `tab`. |
| Pack all | `mise run module:pack-all [-- --force]` | Packs every run-time module for every device into `out/modules/<device>/<id>.dkm`. |
| Emulator | `mise run workbench` | Starts the emulator. Run it in another shell. |
| Run | `mise run dk:run -- ID --emu` | Builds, packs, uploads, selects, restarts and follows the log. |
| Run on a device | `mise run dk:run -- ID --port Digitakt` | The same on a device that has the digikit runtime. The port is a part of the MIDI port name. |
| Log | `mise run dk:log -- --emu follow` | The runtime log. See section 11. |
| Card | `mise run dk:store -- --emu info` | Lists the store. Prints one `capacity` line per resource. |

Every task takes `-- --help`. Hardware is a deliberate step: do not flash a device from these commands.

`module:check` runs one pytest process. What it runs depends on the kind:

| Kind | What `module:check` runs |
| --- | --- |
| `machine` | `tests/modules/test_machine_conformance.py` on both devices, or on one with `--device`. Every parameter at its limits, many notes, modulation, long notes and retriggers. The write-set check is part of it. |
| `fx` | `tests/modules/test_fx_conformance.py`: signals, parameter limits, modulation, switching and a long run. It checks returns, registers, the write set and the instruction budget. |
| `service`, `raw` | No conformance suite. It checks the objects against their sources. |
| all | The module's own tests: `modules/<group>/<name>/tests/test_*.py`. These may build or launch an emulator. |

After the tests pass, `module:check` packs the module for each device. It prints a `size` line and two more lines, `needs` and `fits`.

"Fits" means this: the load needs CPU memory, DSP memory, machine rows, voice rows, parameter rows and page rows. The `fits` line says whether an
**empty** device has enough. The limits come from `[limits]` in `devices/*.toml`.

| Limit | Digitakt II | Digitone II |
| --- | --- | --- |
| Modules at once | 8 | 8 |
| Module file | 64 KiB | 64 KiB |
| ColdFire memory, all modules | 512 KiB | 512 KiB |
| DSP area, all modules | 512 KiB | 320 KiB |
| Machine types and voice rows | 8 | 8 |
| Parameter rows | 16 | 16 |
| Page rows | none | 8 |
| Effects at once | 3 | 3 |

`fits an empty device` does not mean it fits next to your other modules. On a running emulator or device, `dk:store -- --emu info` shows what is
used and what is left. The runtime refuses a module that does not fit, with its own code, for example `E_NOMEM` or `E_DSP_NOSPACE`.

Device facts for authors are in `.agents/skills/digikit-module/references/devices.md`.

## 9. Versions and compatibility

- `sdk/sdk.toml` is the single source of the runtime version and the contract versions. Each contract is `[major, minor]`.

  | Contract | Version | Covers |
  | --- | --- | --- |
  | `api` | `[1, 2]` | The `dk_api` table. Every module has it, `raw` included. |
  | `machine` | `[1, 0]` | `<dk/machine.h>` |
  | `fx` | `[1, 0]` | `<dk/fx.h>` |
  | `service` | `[1, 0]` | Service modules |

- `pack` writes the contracts a module uses into the `.dkm` header.
- The runtime refuses a module when the major version of a declared contract differs, or when its minor version is above the runtime's. The error is
  `E_CONTRACT` (-48): rebuild the module.
- An added `dk_api` function is a minor bump. A changed or removed one is a major bump.
- `[module] version` is yours. Change it for every release. The export file name carries it.
- A run-time machine has no fixed type number. The runtime assigns one at load, and the card remembers it under the global id
  `<module id>:<machine id>`. Never write the number into code or a test.
- The effect ID of saved kits comes from the module id and the effect `id`. Do not rename either after users have kits.

## 10. Export and import

```sh
mise run module:export -- AUTHOR/NAME                       # out/patch/AUTHOR-NAME-VERSION.dkmod
mise run module:export -- AUTHOR/NAME --previous OLD.dkmod  # refuse a parameter that moved to another knob
mise run module:import -- FILE.dkmod --dir ~/my-modules     # unpack into ~/my-modules
```

A `.dkmod` is a deterministic zip. It holds the module folder without `build/` and without OS junk files. Entries are sorted and share one time
stamp. A top-level `package.toml` lists id, version, author, type and the SHA-256 of every file. Two exports of one tree are byte-identical.

`import` checks every hash. It refuses extra or missing files, the author `core`, the type `framework` and an id that is already known. `--replace`
allows a known id. Without `--dir` it uses the first folder of `PATCHKIT_MODULES`. After an import, run `module:check` before you trust the code.

## 11. Logging

- ColdFire code logs with `api->log(msg, value)` (level info; 24 characters of the message are kept) or `api->log_lv(level, msg, value)`.
  Levels: 0 error, 1 warn, 2 info, 3 debug, 4 trace.
- A DSP routine has no log call. The runtime logs its faults (`MACHINE_FAULT`, `MACHINE_DISABLED`) and a stopped DSP.
- Each event carries its level and its source, the runtime or a module. With DEBUG off in the menu, only errors are kept.
- Read the log:

  ```sh
  mise run dk:log -- --port Digitakt follow --level L --module NAME     # use --emu for the emulator
  mise run dk:log -- --port Digitakt level                              # print the levels
  mise run dk:log -- --port Digitakt level debug                        # set the default level
  mise run dk:log -- --port Digitakt level --module NAME=trace          # one module; NAME=default clears it
  ```

  `dk:run` follows the log for you. Levels are not stored on the card. They reset at boot.
- Details: [DEVELOPMENT.md, "Module loop"](DEVELOPMENT.md#module-loop) and `docs/findings/46-digikit-debug.md`.

## 12. Looking things up

| Question | Where |
| --- | --- |
| Version numbers, imports, log kinds, protocol numbers | `sdk/sdk.toml` |
| Machine, effect and service contracts | `<dk/machine.h>`, `<dk/fx.h>`, `<dk/api.h>` |
| What differs between the devices | `devices/digitakt-ii.toml`, `devices/digitone-ii.toml`, `.agents/skills/digikit-module/references/devices.md` |
| Knob and parameter rules | Sections 5.2, 5.3 and 5.7, `docs/findings/02-machines-and-parameters.md` |
| DSP recipes and assembler traps | `.agents/skills/digikit-module/references/dsp-recipes.md`, `selas-pitfalls.md` |
| What each reference module taught | `docs/findings/54-module-cookbook.md` |
| The SDK design | [design/module-sdk.md](design/module-sdk.md), [design/digikit-dkm.md](design/digikit-dkm.md) |
| `core/*` modules, patching, anchors | [modules/README.md](../modules/README.md), [PATCHING.md](PATCHING.md) |
| The module loop and tests | [DEVELOPMENT.md](DEVELOPMENT.md), [TESTING.md](TESTING.md) |
| The same workflow for coding agents | `.agents/skills/digikit-module/SKILL.md` |

The firmware reference (`mise run fw:reference`, which writes `out/reference/fw.html`) is for `core/*` adapters. Content modules do not use
firmware declarations or anchors.
