# The manifest and the module folder

```
module.toml        manifest
src/dsp/NAME.s     SHARC+ assembly (algebraic syntax; every assembled word is re-decoded by our decoder)
src/cf/NAME.c      ColdFire C (m68k-elf gcc, freestanding), or Rust; optional
tests/             smoke.toml (panel steps), test_*.py
README.md          what it does, for a player
gallery/icon.svg   the gallery card icon (48 grid, currentColor lines, one var(--icon-accent) highlight)
build/             generated; never committed, never exported
```

## `[module]`

| key | meaning |
| --- | --- |
| `id` | `AUTHOR/NAME`: lowercase, digits, dashes. `core/` and `test/` are reserved. |
| `version` | major.minor.patch |
| `name`, `summary` | shown in the menu and the gallery; `summary` is one short sentence |
| `category` | `machines`, `fx`, `ui`, `tools`, `test` |
| `type` | `machine`, `fx`, `service`, `raw`; `framework` is internal (the `core/` modules) |
| `author` | the author part of the id for content modules |
| `dynamic` | `true`: a run-time `.dkm` |
| `ports`, `[targets]` | firmware versions the module names and its test status per firmware |
| `requires`, `conflicts` | other modules (patched-in) |

`raw` has no contract: only the generic load checks (format, SHA-256, imports, bounds); the menu shows it as unchecked.

## `[[object]]`, `[[voice]]`, `[[machine]]`, `[[param]]`, `[[page]]`

- `[[object]]`: `path` (`build/NAME-dsp.dko.json`), `sources`, `place = "dsp.l2"` (SHARC+) or `"cf.expansion"` (ColdFire), optional `devices`.
- `[[voice]]`: `machine`, `render`, `note_on`, `desc` (symbols of the DSP object). One body serves both devices.
- `[[machine]]`: `id`, `name` (23 characters at most), `short`, `clone_of = 0` (SAMPLE), `knobs` (8 entries at most). `[machine.dn2]`
  may set `own_dsp = true` and its own `knobs` (parameter names only).
- `[[param]]`: `id`, `name`, `short`, `template` (stock row to copy: `0xd1` on the Digitakt II, `[param.dn2] template = 0xd5`), `min`,
  `max` (words, UI value x 256, 0 to 0x7f00), `default`. The adapter clamps the value to `min` .. `max` before your code sees it.
- `[[page]]` (Digitone II): `id`, `machine`, `name`, `title`, `devices = ["dn2"]`: the machine's SYN page.

## `[[fx]]` and `[[fx.knob]]` (a run-time effect: `type = "fx"`, `dynamic = true`)

An effect has an `[[object]]` with `place = "dsp.l2"` and `devices = ["dt2", "dn2"]` and one `[[fx]]` for each effect. Example:
`tests/fixtures/modules/dyn-fx-gain/module.toml`; rules and limits: `contract-fx.md`.

| key of `[[fx]]` | meaning |
| --- | --- |
| `id` | the effect's name inside the module; with the module id it makes the effect ID of saved kits (never rename) |
| `process`, `desc` | the global text symbol of the process routine, the data symbol of its `dk_fx_desc` |
| `defaults` | the words p0 ... at load, at most `n_params`; not allowed with a page (the knobs' `default` is used) |
| `tab`, `title` | give the effect a page after CHO in FUNC + FX: tab 1 to 3 characters, title 1 to 23 characters (mixed case) |
| `mix` | the default of the framework's MIX knob, 0 to 127 (default 127 = wet) |
| `route` | where the effect starts: `"master"` (default) or `"send"`; a send needs a `tab` |
| `routes` | the routes the user may choose in DIGIKIT > FX ROUTES; left out: `["master"]`, or `["send", "master"]` when `route = "send"`; `["send"]` is send only |
| `devices` | `["dt2", "dn2"]` |

`[[fx.knob]]` (up to 7; knob i is the parameter word p_i, a float): `name` (1 to 23 characters, shown while the knob turns), `short` (1 to 6,
the label), `min`, `max` (the floats at the bottom and the top of the knob), `default` (inside `min` .. `max`), `display` (`"percent"` 0 .. 127
or `"bipolar"` -64 .. +63), `position` (0 to 7, the place on the page; default: the place in the list). MIX, the route and the send amounts are
the framework's: never declare them. `patchkit pack` refuses a bad text, a default outside the range, two knobs at one position, a bad
route and a send without a `tab`. A patched-in effect has an `[[entry]] registry = "fx"` row instead (`contract-fx.md`).

## Knobs

`knobs[j]` is the knob j (0 to 7). An integer is a stock row (`0xca` TUNE, `0xd1` LEV); `0` is empty; a string is a `[[param]]` id. The
position fixes the slot. Build errors: a parameter on knob 3 of a Digitakt II SAMPLE clone, a name that is no `[[param]]`, a parameter
named twice. Digitakt II slots: knob j has mirror index 25 + j + (j / 4) * 2.

## Generated files

`mise run gen:modules` (or `patchkit gen`) writes `build/gen/` (constants for the DSP source, `gallery.json`) from the manifest and the
device files. `module:new --machine` runs it for you.

Full key reference with an example for each key: `modules/README.md`.
