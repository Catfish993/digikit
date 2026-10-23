# What fits: tell the user before a load whether a module will load (plan, 2026-10-23)

Status: all five steps built on the lane branch (results: docs/findings/60-what-fits.md); the status line is updated with each commit.
Lane `tasks-images`, base main 38dfbe4a.

Today a selection that is too big is refused at boot and shown afterwards (a RESULT row, the log error `E_DSP_NOSPACE`). The user should see it before
selecting or installing. Em's brief for the runtime UI: load and unload, health, storage, what can and cannot load.

## Design (approved by Em)

1. **Declared needs.** The packer writes a needs block into the `.dkm` header (docs/design/digikit-dkm.md, "Needs"): arena bytes, the relocated DSP
   image's arena bytes, DSP area bytes and alignment, machine, voice, parameter and page rows, FX slots. The layout is defined once, in `[dkm]` of
   `sdk/sdk.toml`, and generated for Rust, TypeScript and Python. Format version 2; pre-1.0, no migration: every module is packed again.
   Test: `needs-*` scenarios load every run-time module and fixture alone and compare CAPACITY and MODULES with the declared needs; `isolation-*` does
   the same after a coupled load.
2. **One rule.** `fits_now(needs, capacity)`: each need is at most the free amount of its resource (the arena and the DSP area against the largest free
   block, rows and slots against limit - used, a module record and a selection path free, the file within the staging buffer). In Rust once, in
   `dkrt_sdk::fit` (runtime and menu), with the same rule in TypeScript (host) and Python (tools); shared test vectors keep the three equal. A sum rule
   for several modules takes each module's needs out of the capacity in turn (conservative: fragmentation is not modelled).
3. **Device MODULES list.** Each row shows the memory the module takes (`259K`) and `x` as the fourth character when it will not fit next to the ticked ones (the check starts from the limits, since the next boot
   is empty); selecting such a row ticks nothing and says which resource is short (popup `NO FIT: DSP 164K > 155K FREE`, short form in the message row). The rows are mirrored in `DK_LOAD`; scenarios `fits-dt2`, `fits-dn2`.
4. **Host.** Transfer page: tag `fits` or `won't fit` per library module (live CAPACITY; the tooltip says whether it fits alone) and a line for the ticked set (the sum rule against the limits); `dk run` refuses a module that does not fit
   unless `--force`; `module:check` prints the needs and whether the module fits an empty device.
5. Docs (`digikit-dkm.md`, `module-sdk.md` 6.5, finding 60) and tests.

## Facts used

- The loader reserves, in order: the DSP range (`dsp.rs` `extent`, granule 64, first fit by alignment), the arena block of the relocated DSP image
  and the voice rows, the arena block of the ColdFire image and bss (`E_NOMEM`), then the registry rows (`E_PARAM_FULL`, `E_PAGE_FULL`).
- The module set is fixed per session (hot reload is archived): a ticked set is loaded by the next boot into an empty runtime. A fit check for a
  selection therefore starts from the device's limits and takes the needs of the other ticked modules, not from what is loaded now.

## Open

- Fragmentation is not modelled (conservative). The DSP area's first range starts at an offset that is not 8-aligned on the Digitakt II (finding 57): the empty DSP area is taken as 8 bytes short, which covers it.
  Machine type numbers (the sticky map) and a module's own `init` allocations are not modelled.
- The device list has no room for more than about 18 characters a row (finding 60): names are cut to 8 characters.
