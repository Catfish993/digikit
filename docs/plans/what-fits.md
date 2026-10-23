# What fits: tell the user before a load whether a module will load (plan, 2026-10-23)

Status: steps 1 (declared needs in the `.dkm` header), 2 (the rule) and 3 (the device MODULES list) built. Steps 4 and 5 follow; the status line is updated with each commit.
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
3. **Device MODULES list.** Each row shows the size and a mark for a module that will not fit; selecting it says which resource is short.
4. **Host.** Transfer page tag "fits" or "won't fit" with a tooltip; a line for the ticked set; `dk:run` checks before the upload; `module:check`
   prints the needs against each device's limits.
5. Docs and tests consolidated.

## Facts used

- The loader reserves, in order: the DSP range (`dsp.rs` `extent`, granule 64, first fit by alignment), the arena block of the relocated DSP image
  and the voice rows, the arena block of the ColdFire image and bss (`E_NOMEM`), then the registry rows (`E_PARAM_FULL`, `E_PAGE_FULL`).
- The module set is fixed per session (hot reload is archived): a ticked set is loaded by the next boot into an empty runtime. A fit check for a
  selection therefore starts from the device's limits and takes the needs of the other ticked modules, not from what is loaded now.

## Open

- The DSP area's first range starts at an offset that is not 8-aligned on the Digitakt II (finding 57): a module that needs the area to its last byte
  can be called a fit by up to 7 bytes too generously. Machine type numbers (the sticky map) are not modelled.
