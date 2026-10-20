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
| `category` | `machines`, `fx`, `ui`, `test` |
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

## Knobs

`knobs[j]` is the knob j (0 to 7). An integer is a stock row (`0xca` TUNE, `0xd1` LEV); `0` is empty; a string is a `[[param]]` id. The
position fixes the slot. Build errors: a parameter on knob 3 of a Digitakt II SAMPLE clone, a name that is no `[[param]]`, a parameter
named twice. Digitakt II slots: knob j has mirror index 25 + j + (j / 4) * 2.

## Generated files

`mise run gen:modules` (or `patchkit gen`) writes `build/gen/` (constants for the DSP source, `gallery.json`) from the manifest and the
device files. `module:new --machine` runs it for you.

Full key reference with an example for each key: `modules/README.md`.
