---
name: digikit-module
description: Create, build, check, run, export or import a digikit module (a run-time machine, effect or UI code loaded from the card as a .dkm) for the Digitakt II or Digitone II. Use for module:new, module:check, module:dev, module:pack, module:export, module:import, dk:run, the module.toml manifest, the SDK machine contract (render, note_on, desc), knob and parameter rules, and patched-in modules. Not for firmware analysis.
---

# digikit modules

A module is our own code (SHARC+ assembly for a machine's sound, ColdFire C or Rust for UI code) and a manifest `module.toml`.
A run-time module (`dynamic = true`) is packed as a `.dkm`, put on the card and loaded by the digikit runtime at boot. A patched-in
module is linked into a stock image by patchkit (`references/patched-in.md`). Full guide: `docs/MODULE-AUTHORING.md`. Run every
command from the repository root.

## Workflow: a new machine

1. Create: `mise run module:new -- AUTHOR/NAME --machine [--dir DIR]`. It copies `tests/fixtures/modules/sine` (a contract machine,
   one DSP body for both devices, one parameter), renames ids and symbols, and builds. Without `--dir` the module is
   `modules/machines/NAME`; with `--dir` it is `DIR/NAME` and `PATCHKIT_MODULES=DIR` makes every module task find it.
2. Edit `module.toml` and `src/dsp/NAME.s` (`references/manifest.md`, `references/contract-voice.md`).
3. Build: `mise run module:build -- PATH` (PATH is the module folder; `--check` fails when `build/*.dko.json` is stale).
4. Check, no emulator: `mise run module:check -- AUTHOR/NAME [--device dt2|dn2]`. A machine runs the conformance suite (every
   parameter at its limits, many notes, modulation, long notes, retriggers) and its own tests, then prints the packed size per device.
5. Pack: `mise run module:pack -- AUTHOR/NAME --device dt2 -o FILE.dkm`.
6. Run in the emulator: start `mise run workbench` in another shell, then `mise run dk:run -- AUTHOR/NAME --emu` (builds, packs,
   uploads, selects, restarts, follows the log). On a device with the runtime: `mise run dk:run -- AUTHOR/NAME --port Digitakt`.
   Hardware is Em's decision every time; never flash anything.
7. Log: `mise run dk:log -- --port Digitakt level [debug | --module NAME=trace]`, `follow --level L --module NAME`; how a module logs: `docs/MODULE-AUTHORING.md`, "Logging".
8. Share: `mise run module:export -- AUTHOR/NAME [-o FILE] [--previous OLD.dkmod]` and
   `mise run module:import -- FILE [--dir DIR] [--replace]`. A `.dkmod` is a deterministic zip without `build/` plus `package.toml`
   (id, version, author, type, sha256 per file). `--previous` refuses a parameter that moved to another knob.

Each task takes `-- --help`. After a change to `devices/*.toml` or `sdk/sdk.toml`: `mise run gen:modules` (`-- --check` lists stale files).

## Rules

- **No firmware bytes in the repository.** Modules hold our code and hashes only. Patched images, ready states and anything derived from
  firmware stay under `out/` and are never committed. Never name, copy or quote the vendor DSP toolchain's files.
- **Reserved authors.** `core/` (framework modules) and `test/` (fixtures) are reserved; content is `digikit/*` or your own author.
  `module:new` refuses `core`; `module:import` refuses the author `core` and the type `framework`.
- **Knob rules** (build errors): no module parameter on knob 3 of a Digitakt II SAMPLE clone (the sample slot; only
  `value & 0x3ff` reaches the DSP); every name in `knobs` is a `[[param]]` of the module; a parameter appears once. The position in
  `knobs` is the slot; a released parameter keeps it.
- **Contract voice.** A machine has `[[voice]]` with `render`, `note_on` and `desc` (`sdk/dk_machine.h`). Do not write a voice row
  without `note_on`: that is the raw path, with no contract and no guard.
- **No fixed type number** for a run-time machine: the runtime assigns one at load. Never write a number into code or a test. Leave
  `type_hint` out.
- **Versions.** `version` is major.minor.patch; the contract versions are in `sdk/sdk.toml`. A contract major mismatch is refused
  (`E_CONTRACT`): rebuild the module.
- **Tests.** Run only the tests of the code you changed: `mise run test:changed`; before a commit `mise run verify:pre`. Do not run
  the emulator when a static answer is enough, and bound every run.
- Facts go in `docs/findings/` with marks ([V] verified by a second agent, [D] measured once, [O] open), not in handovers.

## References

- `references/manifest.md`: the manifest fields, the machine, parameter and page sections, the folder layout.
- `references/contract-voice.md`: the render / note_on / desc contract, registers, the guard, how the conformance suite judges a voice.
- `references/patched-in.md`: patched-in modules (anchors, claims, `patch`, `patch:inject`, `module:dev`, `patch:test`).
- `docs/MODULE-AUTHORING.md`, `docs/design/module-sdk.md`, `modules/README.md`, `docs/PATCHING.md`.
