---
name: digikit-module
description: Create, build, check, run, export or import a digikit module (a run-time machine, an effect with its own FX page and master or send routes, or UI code loaded from the card as a .dkm) for the Digitakt II or Digitone II. Use for module:new, module:check, module:dev, module:pack, module:export, module:import, dk:run, the module.toml manifest, the SDK machine contract (render, note_on, desc), the effect contract ([[fx]], [[fx.knob]], route, routes), knob and parameter rules, and patched-in modules. Not for firmware analysis.
---

# digikit modules

A module is our own code (SHARC+ assembly for a machine's sound, ColdFire C or Rust for UI code) and a manifest `module.toml`.
A run-time module (`dynamic = true`) is packed as a `.dkm`, put on the card and loaded by the digikit runtime at boot. A patched-in
module is linked into a stock image by patchkit (`references/patched-in.md`). Full guide: `docs/MODULE-AUTHORING.md`. Run every
command from the repository root.

## Module types

`[module] type` says which contract a module has. Pick the type first; each type has its own manifest sections and its own check.

| type | what it is | contract and manifest | copy this |
| --- | --- | --- | --- |
| `machine` | a sound source: SHARC+ code that renders one voice | contract voice (`render`, `note_on`, `desc`; `references/contract-voice.md`); `[[voice]]`, `[[machine]]`, `[[param]]` | `module:new --machine` (a copy of `tests/fixtures/modules/sine`); a real one: `modules/machines/pluck` |
| `fx` | an effect: SHARC+ code that processes the master block. Run time (`dynamic = true`): `[[fx]]` with `process`, `desc` and optional `defaults`; with `tab`, `title` and `[[fx.knob]]` it gets its own FX page; `route` and `routes` choose master insert or send. Patched in: an `[[entry]] registry = "fx"` row | `references/contract-fx.md`; `[[fx]]`, `[[fx.knob]]` (`references/manifest.md`) | a page and a route: `tests/fixtures/modules/dyn-fx-gain` (insert), `dyn-fx-gain-send`, `dyn-fx-gain-both`, `dyn-fx-gain-sendonly`; a real one: `modules/fx/freqshift` |
| `service` | ColdFire code (C or Rust) that adds UI or commands: `dkm_init`, `dkm_fini`, `dkm_slot_n`; no contract check on the sound | `docs/design/module-sdk.md`, `docs/design/digikit-dkm.md` | `modules/tools/remote` (patched in) |
| `raw` | code with no contract: only the generic load checks (format, SHA-256, imports, bounds); the menu shows it as unchecked, a raw DSP routine has no guard | none | `tests/fixtures/modules/c-demo`, `rust-demo` (ColdFire), `svc-echo` |

`framework` is the internal type of the `core/` modules (hooks, registries, the runtime); `module:new` and `module:import` refuse it. A
machine or effect is content; a `core/*` change is firmware work and needs Em.

## Workflow: a new machine

1. Create: `mise run module:new -- AUTHOR/NAME --machine [--dir DIR]`. It copies `tests/fixtures/modules/sine` (a contract machine,
   one DSP body for both devices, one parameter), renames ids and symbols, and builds. Without `--dir` the module is
   `modules/machines/NAME`; with `--dir` it is `DIR/NAME` and `PATCHKIT_MODULES=DIR` makes every module task find it.
2. Edit `module.toml` and `src/dsp/NAME.s` (`references/manifest.md`, `references/params.md`, `references/contract-voice.md`;
   reuse `references/dsp-recipes.md`, avoid `references/selas-pitfalls.md`, size for `references/devices.md`).
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

## Workflow: a new effect

`module:new` has no run-time effect template. `module:new -- fx/NAME --dsp` writes a patched-in master-bus stub (`references/patched-in.md`),
not a run-time effect. Start from a copy:

1. Copy `modules/fx/freqshift` (a real effect, a page with one bipolar knob, `route = "send"`) or the smaller
   `tests/fixtures/modules/dyn-fx-gain` (one knob, a master insert) to `modules/fx/NAME` (or `DIR/NAME` with `PATCHKIT_MODULES=DIR`).
   Do not copy `build/`: it is generated. Rename, and keep the names consistent:
   - `module.toml`: `[module] id` to `AUTHOR/NAME` (`test/` and `core/` are reserved: use `digikit` or your own author), `name`,
     `summary`, `category = "fx"`, `author`, `ports` and `[targets]` (`dt2-1.17`, `dn2-1.12`), the `[[object]]` `path`
     (`build/NAME-dsp.dko.json`) and `sources` (`src/dsp/NAME.s`; rename the file too).
   - In `[[fx]]`: `id` (a short name; it and the module id make the effect ID of saved kits, so never rename them after release),
     `process` and `desc` (the global symbols in the `.s`), `tab` (1 to 3 characters), `title`, `mix`.
   - In the `.s`: the symbol prefix (`_dg_` in `dyn-fx-gain`, `_fs_` in `freqshift`) in every label, and `n_params`, `state_bytes`,
     `max_cycles_block` in `dk_fx_desc`.
2. Edit `[[fx.knob]]` (up to 7: `name`, `short`, `min`, `max`, `default`, `display`, `position`; knob i is parameter word p_i, a float in
   `min` .. `max`) and write `process` (`references/contract-fx.md`, `references/dsp-recipes.md`, `references/selas-pitfalls.md`). The `desc`
   `n_params` covers the knobs. A page needs no ColdFire code.
3. Choose the route: `route` (the default) and `routes` (the allowed ones). A master-only effect leaves both out. A send needs a `tab`
   and writes the wet signal only (a send has no dry path). Rules and defaults: `references/contract-fx.md`, "Insert or send".
4. Build and check: `mise run module:build -- PATH`, then `mise run module:check -- AUTHOR/NAME`. `module:check` runs the fx conformance suite
   (`tests/modules/test_fx_conformance.py`: signals, parameter limits, switching, a long run, the instruction budget) and the module's own
   tests. A float32 reference model with a test on the rig (`tests/patchkit/test_freqshift.py`) is how a real effect is held to its maths.
5. Pack: `mise run module:pack -- AUTHOR/NAME --device dt2 -o FILE.dkm` (`--device dn2` for the Digitone II). It refuses a bad page, a bad
   route and a send without a `tab`.
6. Run: `mise run dk:run -- AUTHOR/NAME --emu` (as for a machine). The Digitone II image needs `core/dynfx` and `core/fxpages` (its
   `device` patch set has them, `contract-fx.md`).
7. What you see on the device: the effect's page after CHO in FUNC + FX (and its knobs and MIX), for a send effect a knob on the second page
   of the track's FX key (the amount of each track), and SETTINGS > DIGIKIT > FX ROUTES (the route of each effect; a change needs a restart,
   the row offers it).

Each task takes `-- --help`. After a change to `devices/*.toml` or `sdk/sdk.toml`: `mise run gen:modules` (`-- --check` lists stale files).

## Rules

- **No firmware bytes in the repository.** Modules hold our code and hashes only. Patched images, ready states and anything derived from
  firmware stay under `out/` and are never committed. Never name, copy or quote the vendor DSP toolchain's files.
- **Reserved authors.** `core/` (framework modules) and `test/` (fixtures) are reserved; content is `digikit/*` or your own author.
  `module:new` refuses `core`; `module:import` refuses the author `core` and the type `framework`.
- **Knob rules** (build errors): no module parameter on knob 3 of a Digitakt II SAMPLE clone (the sample slot; only
  `value & 0x3ff` reaches the DSP); every name in `knobs` is a `[[param]]` of the module; a parameter appears once. The position in
  `knobs` is the slot; a released parameter keeps it.
- **Framework-owned controls of an effect.** MIX (the dry/wet crossfade, and the return level of a send), the route (`master` or `send`)
  and the send amounts of the tracks belong to the framework. The effect writes the wet signal only and never sees them; it cannot read or
  set them, and it must not assume it is last in the chain.
- **Effect limits.** 3 run-time effects at a time (`fx_slots`); send effects: Digitakt II 2, Digitone II 1 (`send_slots`; more stay
  inserts); at most 7 knobs and MIX; no p-locks, LFO, other modulation or MIDI CC on an effect knob; one page for each effect. An effect that
  returns a fault, a non-finite sample or a magnitude above 8.0 plays dry (8 faults in 1500 blocks switch it off). Numbers per device:
  `references/devices.md`.
- **Elektron files stay stock.** A kit, project or pattern file saved with a digikit effect also loads on stock firmware. Digikit data
  (the route, the send amounts) lives in the runtime's settings store, not in Elektron files; only the knob values and MIX go into the kit's
  spare bytes, with the effect ID.
- **Contract voice.** A machine has `[[voice]]` with `render`, `note_on` and `desc` (`sdk/dk_machine.h`). Do not write a voice row
  without `note_on`: that is the raw path, with no contract and no guard.
- **No fixed type number** for a run-time machine: the runtime assigns one at load. Never write a number into code or a test. Leave
  `type_hint` out.
- **Versions.** `version` is major.minor.patch; the contract versions are in `sdk/sdk.toml`. A contract major mismatch is refused
  (`E_CONTRACT`): rebuild the module.
- **Tests.** Use owner checks for the code you changed (`mise run test:changed -- --explain` shows affected Python/Rust/module
  tests). Selected tests may build or launch a device/emulator. `mise run verify:pre` is an optional combined preflight that
  repeats selected tests, not a mandatory follow-up. Keep the module build, conformance and proof checks required above;
  report any deferred coverage to the coordinator for the routed `mise run verify` gate. Do not run the emulator when a static
  answer is enough, and bound every run.
- Facts go in `docs/findings/` with marks ([V] verified by a second agent, [D] measured once, [O] open), not in handovers.

## References

- `references/manifest.md`: the manifest fields, the machine, parameter, page and effect (`[[fx]]`, `[[fx.knob]]`) sections, the folder layout.
- `references/contract-voice.md`: the render / note_on / desc contract, registers, the guard, how the conformance suite judges a voice.
- `references/contract-fx.md`: the effect contract (`process`, `dk_fx_in`, `dk_fx_desc`), the chain, the fade and the fault rules, run-time effects, effect pages (`[[fx.knob]]`), master and send routes, the conformance checks.
- `references/params.md`: from "the user wants these knobs" to `[[param]]` and knob entries; what reaches the DSP (4 words today), what persists.
- `references/dsp-recipes.md`: tested assembly from pluck and master-drive (parameter to float, block loop, noise, one-pole, all-pass, delay line, comb, silence, ramp).
- `references/selas-pitfalls.md`: assembler forms that are wrong on the silicon and what to write instead.
- `references/devices.md`: Digitakt II vs Digitone II (budget, voices, parameters, memory, stock patterns).
- `docs/findings/54-module-cookbook.md`: what each reference module taught (costs, traps, patterns); add to it with every module.
- `references/patched-in.md`: patched-in modules (anchors, claims, `patch`, `patch:inject`, `module:dev`, `patch:test`).
- `docs/MODULE-AUTHORING.md`, `docs/design/module-sdk.md`, `modules/README.md`, `docs/PATCHING.md`.
