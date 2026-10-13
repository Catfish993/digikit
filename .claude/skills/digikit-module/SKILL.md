---
name: digikit-module
description: Use when creating, building, patching, injecting, testing or explaining a patchkit firmware module (modules/<category>/<name>/) for the Digitakt II or Digitone II. Covers the mise tasks, the module layout, the rules (no firmware bytes, anchors by rules, claims) and one worked example (fx/master-gain).
---

# patchkit modules

A module is our own code (ColdFire C, SHARC+ assembly) plus a manifest. patchkit links it into a stock `.syx`
(`native/patchkit`), places it in a port region, hooks a firmware site and writes a patched image under `out/patch/`.
Plan and state: `docs/plans/patchkit.md`; the facts behind it: `docs/findings/21-patchkit-core.md`.
Run everything from the repo root. Work in a lane only for long or parallel runs (`tools/lane.sh`).

## Rules (not optional)

- **No firmware bytes in the repo.** Modules hold our code and hashes only: `write` ops carry `expect_sha256` of the
  bytes replaced, never the bytes; frame goldens are SHA-256 only (PNGs stay under `out/`); patched `.syx`, ready
  states and oracle modules made from firmware live under `out/` and are never committed.
- **No addresses in modules.** Every firmware site is a named *anchor* resolved by an `[[anchor_rule]]` (masked
  signature, `cfmatch` offset, or SHARC `reloc_hash` plus offset) into `devices/ports/<dev>-<ver>.toml` by
  `mise run port:resolve`. A rule that matches nothing or twice is an error: fix the rule, never type an address.
  Anchor and region claims are verified by hash at patch time.
- **Claims.** Declare each scarce resource with `[[claim]] resource = "..."` (for example `dsp.master.hook`);
  two modules claiming the same one conflict. Also: `requires`, `conflicts`, `ports = [...]` to pin a firmware.
- **Never patch sections 2, 4, 8** (patchkit refuses). Always patch from the stock image; a patched input is refused.
- Never name, copy or quote the vendor DSP toolchain's files; cite public manuals only. Marks in findings:
  [V] verified by a second agent, [D] measured once, [O] open.
- Flashing hardware is Em's decision, every time. Nothing patched has run on a device.
- Run the emulator only when a static answer is not enough, and bound runs.

## The loop

| Step | Command |
|---|---|
| New module | `mise run patch:new -- fx/my-drive` (`--cf`, `--dsp`; builds the stubs so the template is known to compile) |
| Build objects | `mise run patch:build -- modules/fx/my-drive` (`--check` fails when a committed `.dko.json` is stale) |
| Patch | `mise run patch -- --firmware Digitakt_II_OS1.17.syx --modules fx/my-drive -o out/patch/x.syx` (writes `x.plan.json`) |
| Explain | `mise run patch:explain -- --firmware ... --modules ...` (before/after listing of each site, allocation map) |
| Verify | `uv run python tools/patchkit.py verify out/patch/x.syx` (also run by `patch`) |
| Inject | `mise run inject -- out/patch/x.syx` (into a running daemon; DSP-only change hot, MAIN change by cached coupled cold boot) |
| Edit loop | `mise run patch:dev -- --modules fx/my-drive [--device dt2\|dn2]`: watches `src/` and `module.toml`; per edit builds, patches, injects, runs the quick tests, prints one line with `edit->running` and `edit->tested` |
| Test | `mise run patch:test` (matrix in `tests/patchkit_smoke/matrix.toml`; `-- --cell dt2-1.17:fx/my-drive`; `-- --update` records frame goldens) |
| pytest | `uv run python -m pytest tests/patchkit_smoke --slow` |

Measured edit to running (DT2 1.17): DSP-only change about 3 s; a ColdFire (MAIN) change about 13 s (cold boot with DSP;
DN2 about 6 s; cached by MAIN hash, so an unchanged MAIN restores in under 1 s).

## Layout

```
modules/<category>/<name>/
  module.toml          id, version, status, [targets], [[object]], [[op]], [[claim]], [[anchor_rule]]
  src/cf/*.c|*.s       ColdFire (m68k-elf gcc, -mcpu=54418, freestanding, relocatable)
  src/dsp/*.s          SHARC+ (algebraic syntax; every assembled word is re-decoded by our decoder)
  build/*.dko.json     committed objects (patch:build; patch:build --check keeps them current)
  tests/*.toml         harness tests: [test] name, devices, quick; [[step]] ...
  gallery/README.md    text for the web gallery
```

- `[[object]]`: `path`, `sources`, `place = "dsp.l2" | "cf.expansion"`, optional `devices`, `[object.externs]` (undefined
  symbol -> anchor name).
- `[[op]]`: `retarget` (rewrite the target of a ColdFire `jsr/bsr/jmp` or a SHARC direct `CALL` at an anchor to a
  module symbol) and `write` (bytes at anchor + offset with `expect_sha256`; may carry relocs to module symbols).
- A ColdFire object makes patchkit add the built-in `core/expansion` (loader in the flash cave, segment chain behind
  the MAIN OS, DKPR record); code, data and bss are in place before the reset clear. Module code must not assume a
  hosted libc; bss is zeroed by the loader.
- Toolchains: `mise run toolchains` once (pinned m68k-elf binutils/gcc and the SHARC+ assembler under `out/tools`).

## Machine modules (`core/machines`, `core/params`, `core/dsp-voice`)

The frameworks are ordinary modules (assembly + manifest) built on patchkit's primitives: registries (`[[registry]]` in the
framework, `[[entry]]` in a content module), copies of stock tables (`[[section]]`), typed writes with expressions, hooks.
A module that adds a machine type needs no code for the ColdFire side: `requires = ["core/machines"]` and one
`[[entry]] registry = "machines"` (`id`, `name`, `short`, `clone_of`, `position`, `f0` .. `f8` = the nine descriptor IDs,
optional `own_dsp`). patchkit assigns the type numbers `7, 8, ...` (Digitone II `5, ...`) in canonical module order;
`machines.<id>.index` is the type number. Parameters: `requires = ["core/params"]` and `[[entry]] registry = "params"`
(`name`, `short`, `template`, `slot`, `min`, `max`, `default`); a machine names one as `fN = "params.<id>.index"`. A DSP
object registers its routine with `[[entry]] registry = "voices"` (`type = "machines.<id>.index"`, `pc = "<symbol>"`).
`modules/machines/pluck` is the worked example (machine, four parameters, voice). The sites, the routines and their tests:
`modules/core/machines/src/cf/*/machines.s`, finding 02 ("core.machines ... as modules"), `tools/pk_cfunit.py`. Proofs:
`tools/pk_proof_machines.py`, `pk_proof_pluck.py`, `pk_proof_machines_dn2.py`, `pk_proof_voice*.py` (about 15 minutes each).
The emulator runs ColdFire code only from the MAIN image or the expansion region; framework code is in `cf.expansion`.

## Tests (tools/pk_harness.py)

Steps, one action key each: `tap = "TRIG 1"`, `tap = ["TRK", "TRIG 2"]` (chord), `hold = "PLAY"` + `ms`,
`turn = "ENC A"` + `by`, `wait_ms`, `expect_frame = "name"` (golden hash; `differs_from`), `expect_pcm_db = {min, max}`
(peak 100 ms-window RMS since the last input), `expect_no_fault`, `expect_mem = {addr, mask, eq}` (a ColdFire word), `expect_dsp = {addr, hex}`, `stress = {seed, actions}`; `device = "dt2"` limits a
step. Every input asserts no fault, no DSP halt, no main-loop stall (controller events `fault`, `halt`, `stall`;
`status.heartbeat`). Module tests join the smoke tour for the module set; goldens are keyed
`DEV-FW/<module set>/<segment>/<checkpoint>`. A patched firmware that changes nothing visible must equal the stock
frames. Ready inputs must be fresh (`tools/ready.py`, SOURCE.json `intc_force: false`); the harness refuses stale ones.

Known emulator limit: none for the DSP halt in patterns 3 and 4 any more (conditional Type 7a is modelled; finding 05). The generated cores must be regenerated after a `tools/sharc_core` change (`CORE_FINGERPRINT`).

## Worked example: fx/master-gain (SHARC, both devices)

`modules/fx/master-gain/module.toml` declares one object (`src/dsp/master_gain.s`, placed in `dsp.l2`), two externs
(`_fw_q31_convert -> dsp.master.q31_converter`, `_fw_master_buf -> dsp.master.float_buf`), one op
`retarget anchor = "dsp.master.q31_call" to = "_mg_hook"`, one claim `dsp.master.hook`, and three `[[anchor_rule]]`s
(`sharc_func` with a `reloc_hash`, an `offset` in parcels and `len`; `sharc_literal` for the buffer address). The same
rules resolve on DT2 and DN2 because the two functions are identical in both DSP images.

The source scales the 64-float master block by a gain word and tail-jumps to the firmware's converter; its register
use is justified in the file header (docs/findings/06). Gain 1.0 is bit-identical to stock; 0.5 is -6.0206 dB.

    mise run patch:build -- modules/fx/master-gain --check
    mise run patch -- --firmware Digitakt_II_OS1.17.syx --modules fx/master-gain -o out/patch/mg.syx
    mise run patch:explain -- --firmware Digitakt_II_OS1.17.syx --modules fx/master-gain
    mise run patch:dev -- --modules fx/master-gain            # edit the .s, watch the summary line
    mise run patch:test -- --cell dt2-1.17:fx/master-gain,test/cf-demo

To change the gain in a running emulator without rebuilding: `dsp.patch` (`DM <gain addr> 3f000000`), see finding 17.
A new module starts from `mise run patch:new -- fx/name --dsp`, which copies these anchor rules and a pass-through hook.
Document the result in `docs/findings/` (one file per topic) with marks, not in a handover.
