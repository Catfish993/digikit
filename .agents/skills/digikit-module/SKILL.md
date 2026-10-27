---
name: digikit-module
description: Create, build, check, pack, run, export or import a digikit content module (a machine, an effect with its own FX page and master or send routes, or a ColdFire service loaded from the card as a .dkm) for the Digitakt II or Digitone II. Use for module:new, module:check, module:pack, module:export, module:import, dk:run, the module.toml manifest, the machine contract (render, note_on, desc), the effect contract ([[fx]], [[fx.knob]], route, routes) and knob and parameter rules. Patched-in core modules: references/patched-in.md. Not for firmware analysis.
---

# digikit modules

The guide is `docs/MODULE-AUTHORING.md`. Read the section you need there; this file only routes you. Run every command from the repository root.

## Module kinds

`[module] type` sets the kind (guide section 1).

| Kind | What it is | Template |
| --- | --- | --- |
| `machine` | A SHARC+ voice: `render`, `note_on`, `desc` (guide 5.2, 7.1) | `sdk/templates/machine`; a real one: `modules/machines/pluck` |
| `fx` | A SHARC+ master or send effect with its own page: `process`, `desc` (guide 5.4, 7.2) | `sdk/templates/fx`; a real one: `modules/fx/freqshift` |
| `service` | ColdFire C: `dkm_init`, `dkm_fini`, `dkm_slot_N` (guide 5.5, 7.3) | `sdk/templates/service` |
| `raw` | No contract, only the generic load checks | `tests/fixtures/modules/c-demo` |

`framework` is the internal type of the `core/*` modules. Changes to `core/*` are firmware work and need Em.

## Workflow

1. Create: `mise run module:new -- KIND AUTHOR/NAME [--dir DIR] [--title T] [--short S] [--no-build]` (guide 3). `KIND` is `machine`, `fx` or `service`.
   With `--dir`, set `PATCHKIT_MODULES=DIR`.
2. Edit `module.toml` and the source (guide 4 to 7). Reuse `references/dsp-recipes.md`, avoid `references/selas-pitfalls.md`, size with `references/devices.md`.
3. After a change to `module.toml`, `devices/*.toml` or `sdk/sdk.toml`: `mise run gen:modules` (`-- --check` lists stale files). Never edit `build/gen/`.
4. Build: `mise run module:build -- PATH [--check]`.
5. Check, no emulator: `mise run module:check -- AUTHOR/NAME [--device dt2|dn2]` (guide 8).
6. Pack: `mise run module:pack -- AUTHOR/NAME --device dt2|dn2 [-o FILE.dkm]`.
7. Run: start `mise run workbench` in another shell, then `mise run dk:run -- AUTHOR/NAME --emu --seconds 10`. On a device with the runtime:
   `mise run dk:run -- AUTHOR/NAME --port Digitakt`. Hardware is Em's decision every time; never flash anything.
8. Log: `mise run dk:log -- --emu follow --module NAME` (guide 11).
9. Share: `mise run module:export -- AUTHOR/NAME [--previous OLD.dkmod]` and `mise run module:import -- FILE [--dir DIR] [--replace]` (guide 10).

Each task takes `-- --help`.

## Rules

- **No firmware bytes in the repository.** Modules hold our code and hashes only. Patched images and anything derived from firmware stay under `out/`.
  Never name, copy or quote the vendor DSP toolchain's files.
- **Reserved authors.** `core` and `test` are reserved. Use `digikit` or your own author. `module:new` refuses `core` and `framework`.
- **Commit `build/`**, including `build/gen/`. Do not edit `build/gen/` by hand (guide 4, 6).
- **Knobs and parameters.** Parameter names only in `knobs`; the device profile places them. The position is the slot, so a released parameter keeps its
  knob. No parameter on knob 3 of a Digitakt II SAMPLE clone. At most 4 contract parameters (guide 5.3, 5.7).
- **No fixed type number** for a run-time machine. The runtime assigns it at load. Never write a number into code or a test.
- **Effects.** MIX, the route and the send amounts belong to the framework. Write the wet signal only and do not assume you are last (guide 5.4, 7.2).
  Never rename the module id or the effect `id` after release.
- **Elektron files stay stock.** Digikit data lives in the runtime's settings store, not in kit, project or pattern files.
- **Versions.** Contract versions are in `sdk/sdk.toml`. A contract major mismatch is refused (`E_CONTRACT`): rebuild (guide 9).
- **Tests.** Use the owner's checks (`mise run test:changed -- --explain`). Do not run the emulator when a static answer is enough, and bound every run.
- Facts go in `docs/findings/` with the marks [V], [D], [O], not in handovers.

## Firmware functions for a core adapter

Only `core/*` adapters need firmware addresses. Use the firmware-query skill: `fw.py atlas TERM`, then `fw.py catalog TERM -i IMAGE --grade`. Check the
candidate on both devices and confirm callers with `tools/coldfire/refscan.py`. Content modules never use anchors.

## References

- `references/dsp-recipes.md`: tested assembly from pluck and master-drive (parameter to float, block loop, noise, one-pole, all-pass, delay line, comb, silence, ramp).
- `references/selas-pitfalls.md`: assembler forms that are wrong on the silicon and what to write instead.
- `references/devices.md`: Digitakt II against Digitone II (budget, voices, parameters, memory, loader limits, effects).
- `references/patched-in.md`: patched-in modules (anchors, claims, `patch`, `module:dev`, `patch:test`).
- `docs/findings/54-module-cookbook.md`: what each reference module taught. Add to it with every module.
- `docs/design/module-sdk.md`, `modules/README.md`, `docs/PATCHING.md`.
