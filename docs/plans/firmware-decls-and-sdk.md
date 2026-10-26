# Firmware declarations, module SDK and Syntakt

Started 2026-10-27. Targets: DT2 OS 1.17, DN2 OS 1.12, Syntakt OS 1.42.

## Goal

1. Write each firmware fact (function, table, struct field) once, in one typed declaration. Generate rules, Rust, C and
   assembly from it.
2. Give content-module authors plain C against generated headers, with fast builds.
3. Make a module a readable source package that people share, and that the desktop and web Transfer tabs build and
   send to the device. Users never handle `.dkm` files.
4. Use the Syntakt as the third device that proves the SDK is device-agnostic.

## Decisions (Em, 2026-10-27)

- core/* adapters and the runtime stay Rust. Content modules use a C ABI contract, and C is the first-class language.
- SHARC DSP code stays assembly. C on SHARC is a bounded pilot (step 7). selcc puts every local in a stack slot:
  15 instructions per element against 3 by hand (`docs/findings/20-patch-toolchains.md`).
- No Rust proc macros. `module.toml` stays the authored manifest, and a per-module header is generated from it.
- Catalogue the firmware first, then design the declaration format against what the catalogue finds. Declare an anchor
  when a feature uses it, not all candidates up front.
- Syntakt: ColdFire CPUs only. Never modify, emulate or reconfigure the FPGA (syx section 1). DT2/DN2 come first; the
  Syntakt emulator boot waits until after step 9, and steps 3 and 9 report a portability count on 1.42.
- The anchor review page and the anchor reference docs come from one generator.

## Today (summary)

| Layer | Where | Problem |
|---|---|---|
| Rules | 201 `[[anchor_rule]]` in 15 `module.toml` | no central list; near-identical blocks |
| Resolver | `tools/patchkit/port_resolve.py` -> `devices/ports/<dev>-<os>.toml` | works; keep |
| Link | `[object.externs]` + `packages/patchkit/src/apply.rs:1041` | a second name per anchor |
| Code | Rust `extern` + doc comment, bare struct offsets | no type or offset check |
| Old copies | `pk_*_rules*.py`, `machineprofile.py`, `cf_names.py` | literal 1.15C/1.16/1.11 addresses, same facts again |

Other drift: `sdk/dk_machine.h` is a hand copy of `sdk.toml`; `[targets]` repeats `[module].ports`; C cache keys do not
hash headers; `MODULE-AUTHORING.md` says `build/` is not committed but the web gallery needs the committed `.dko.json`.

Rust builds are slow by layout: `out/cache/rust` holds one target dir per worktree and module (73 dirs, 3.3 GB), each
with its own `-Zbuild-std` of `core`, the codegen backend is a debug build (`pk_toolchains.py:83`), and each build copies
the crate to a new temp dir. Content modules do not compile Rust: machines and effects are SHARC assembly.

## Steps

Each step lists its proof. Commits need Em's OK. One writer in the main tree at a time; read-only work runs in
parallel.

### 1. Anchor catalogue (no Rust)

- [x] `uv run python -m tools.coldfire.atlas catalog dt2-1.17 dn2-1.12` writes a `catalog` table into each
      `out/atlas/<image>.sqlite` and a ranked TSV next to it.
- [x] Per row: address, name, class, tags (seeds), UI callers, UI strings, hash unique, hash stable since the last OS,
      pair on the other device and pair method, existing anchors, suggested rule, rule resolves on both devices, score.
- [x] `fw.py catalog` queries it (filters: tag, top N, unreviewed).
- [x] The firmware-query skill documents it; the digikit-module skill points to it for "find the firmware function
      you need".
- [ ] Graph context per row: the classes of the callers, and the named or anchored callees. A helper called from one
      class only gets a working name such as `ListView::helper_3` ([O]).
- [ ] Groups: community detection (networkx) on the UI call subgraph, about 30-50 groups, each with its classes,
      strings and members. Export the subgraph as JSON as well.
- [ ] Agents pre-label the top groups and their rows (role guess from decompilation, [O]).
- [ ] Review page: group -> rows -> call neighbourhood per row, with keep / skip / later, a name and a note. Reviewed
      rows seed step 3. The page is the review view of the reference docs in step 5 (same generator).

Proof: the tool runs in seconds on both images; one unit test of the rule suggestion.

Result 2026-10-27 [D]: 5 s for both images. DT2 5381 rows (1486 anchor-grade), DN2 5198 (1468). The top rows are
unnamed helpers with many UI callers, so review needs the pre-labels.

### 1b. SHARC catalogue (read-only, before step 3)

- [x] Read-only probe of the DSP images (sharcdb, `tools/sharc_dev/sharc.py`), seeded by the `cpu = "sharc"` anchors
      and the frame, voice and master paths of finding 53.
- [ ] Move the shared parts of the catalogue code (rule suggestion with ordinal, grade, score, sqlite/TSV writer) out
      of `atlas.py` into one module used by both CPUs. The ColdFire catalogue must stay byte-identical.
- [ ] DSP collector: hash pairing between DT2 and DN2 plus call-shape pairing, stability against 1.15C / 1.10E,
      anchors from the rules, role from the call path. The DSP score rewards role path, profile heat and a unique hash,
      not UI callers.
- [ ] `fw.py catalog` covers both CPUs; skill docs and one unit test.

Probe results 2026-10-27 [D]: DT2 1,147 functions, DN2 1,078. The DSP images are byte-identical across 1.16 -> 1.17
and 1.11 -> 1.12 (finding 53), so stability is measured against 1.15C / 1.10E. 143 / 142 catalogue rows, 87 / 90 with a
byte-identical twin on the other device. The 15 / 14 SHARC anchors (plus 4 `dl.cf.*` on the ColdFire) sit in 8 / 7
functions, and most of those functions are device-specific, so each device keeps its own rules. Shared building blocks,
byte-identical on both devices: block biquad, saturator, envelope generator, block copy/fill, one filter type, powf,
divide and reciprocal square root with Newton steps. The `functions.label` heuristic is unreliable; use finding 53.

### 2. Clean-up and deletions (no Rust)

- [ ] C object cache key includes header dependencies (gcc `-MD`).
- [ ] Generate `sdk/dk_machine.h` and `sdk/dk_fx.h` from `sdk.toml`; drop the copy test.
- [ ] One per-firmware status field in `module.toml` (`[targets]` or `[module].ports`, not both).
- [ ] Resolve `cf.params.fmt` / `cf.params.slotbuild` (in `pk_params_rules_dn2.py`, absent from the DN2 port file).
- [ ] Fix the cfdb `ghidra_dump_dir` path; fix the `build/` statement in `MODULE-AUTHORING.md`.

Proof: `patchkit gen --check`, port files byte-identical, pathcheck.

### 3. Declarations (Python, host patchkit only)

- [ ] Design `devices/fw/<subsystem>.toml` with Em: per entry name, devices, signature, rule, mark; structs with fields
      and per-device sections only where a device differs; hooks with their register contract.
- [ ] `port_resolve.py` reads the new fw directory under devices; the 201 rules move there mechanically.
- [ ] Replace the `pk_*_rules*.py` generators with `catalog emit-rule ADDR`; retire literal tables in
      `machineprofile.py` and `cf_names.py` as their consumers move to anchors.
- [ ] Add a vtable-slot rule kind (about 63% of ColdFire candidates have no unique hash) and an ordinal for
      `sharc_func` (fixes most of the 182 shared DSP hashes on DT2).
- [ ] Hooks declare their register contract: inputs, clobbered and preserved registers, and the DSP mode (modify
      registers, SIMD). Today this is prose in findings 45 and 53.
- [ ] One naming rule: `<cpu>.<subsystem>.<kind>.<name>`, named after the firmware, not after our module.
      `dkrt.*`, `dl.*`, `health.*` and `input.*` get firmware names; old names stay as aliases until the move is done.
- [ ] Portability count: how many declarations resolve on Syntakt 1.42 (today 27 of 172 ColdFire rules).

Proof: every `devices/ports/*.toml` is byte-identical before and after the move (aliases keep the old names).

### 4. Rust build speed (before the next m68k Rust build)

- [ ] One shared target dir keyed by the toolchain pin; a prebuilt `core`/`compiler_builtins` sysroot instead of
      `-Zbuild-std`; a release build of the codegen backend; a stable staging path per module.

Proof: cold and warm build of `core/digikit-menu` before and after; objects byte-identical.

### 5. Generated bindings

- [ ] `patchkit gen` emits Rust (`#[link_name = "<anchor>"]`), C headers (`__asm__("<anchor>")`) and assembly `.equ`
      files from the new fw directory under devices, with size and offset asserts for structs.
- [ ] Hook trampolines are generated from hook declarations. Hand assembly remains only where code enters at a patch
      site with the displaced code's registers.
- [ ] `[object.externs]` is removed.
- [ ] Reference docs: one page per subsystem with each declared anchor (signature, devices, rule, mark, status), from
      the same generator as the step 1 review page.

Proof: applied-patch bytes identical for the core modules.

### 6. Content-module SDK in C

- [ ] Generated C header for the runtime `Api` table and `dkrt_*` exports.
- [ ] Generated per-module header from `module.toml` (parameter ids, knob order) for C, assembly and Rust.
- [ ] `module:new` templates: ColdFire C, SHARC assembly machine, effect.
- [ ] Move firmware codes (`template`, stock knob codes, `type_hint`) from author manifests to device profiles. This is
      what makes module source device-agnostic.
- [ ] One authoring guide; old SDK docs folded in or deleted.

Proof: `module:check` for pluck and freqshift; a new C module built in under a second.

### 7. SHARC kernels, style and C pilot

- [ ] Study the shared stock building blocks from step 1b with the public manuals in `out/refs/` (see
      `docs/sharc/SOURCES.md`): 2x oversampling with a half-band decimator, per-sample parameter ramps, circular-buffer
      delay, reciprocal with Newton steps. Results in one findings file. We learn idioms only; no firmware code is
      copied into the repo.
- [ ] A short style guide for readable SHARC assembly (register naming, macros, block-loop layout).
- [ ] SDK DSP kernels (filter, saturator, envelope, ramp, delay), written from those idioms and measured in the emulator.
      Compare with calling the firmware's own kernel through a runtime import (byte-identical on DT2 and DN2): less DSP
      memory, but tied to firmware behaviour.
- [ ] C pilot (1 day): control code in C (note_on, parameter mapping) calling the block-level kernels. A call costs
      about 25 instructions per 32-sample block, under one per sample. Pluck's string loop in C against the assembly,
      cycles measured in the emulator.

Decision: offer `--lang c` for SHARC glue, or keep assembly only.

### 8. Distribution and Transfer tabs

- [ ] `.dkmod` is the shared unit: source, manifest, prebuilt objects and a reproducible-build hash.
- [ ] The desktop Transfer tab packs the `.dkm` for the connected device and firmware at upload.
- [ ] The web Transfer tab lists modules published as Pages assets and packs and sends them in the browser. ColdFire
      objects come prebuilt by CI; SHARC assembly may assemble in the browser (the assembler is Rust).
- [ ] Revisit the current web transfer flow.
- [ ] `module:import` from a path or git URL.

Proof: screenshots of both tabs before Em tries them.

### 9. UI runtime services

- [ ] A generated `<dk/ui.h>` with concepts, not firmware classes: menu and menu item, list, value, text, plus
      `dk_ui_has(cap)`. A missing feature reads as false or as a weak import of 0, never as a crash.
- [ ] A `core/ui` adapter per device implements it with that device's anchors (e.g. DT2 `SourcePageView` against DN2
      `MultiSourcePageView`). A new device adds an adapter and anchors; modules and the header do not change.
- [ ] Portability count on Syntakt 1.42 for the UI declarations.

Sized after step 1.

### Syntakt track (lower priority: S3 after step 9)

The ColdFire runtime needs about 36 of the 145 missed rules (runtime, menu, health, input). The rest are DT2/DN2 sound
engine rules (machines, parameters, FX pages, DSP link). After step 3, `catalog emit-rule` re-derives many from the
strict and vtable pairs: about 0.5-1 day.

- [x] S1. Static onboarding: identify the images (MAIN at 0x40000400; section 7, raw ColdFire code, possibly the second
      CPU), build cfdb and a separate Ghidra project, dry-run all rules on 1.42, and propose a Syntakt device file (devices, not yet written).
- [x] S2. Static trace of how MAIN talks to the second CPU and starts the FPGA (interface only), what the boot waits
      for, and how the container seal key is derived. **Decision point.**
- [x] Write the S1/S2 results to `docs/findings/70-syntakt-1-42.md`.
- [ ] S3 (after step 9). Emulator boot to the main screen, with the FPGA status and the peer interface stubbed
      (1.5-2 days).
- [ ] S4. Re-derive the ColdFire runtime anchors on 1.42; no-seal container path in patchkit.

S1/S2 results 2026-10-27 [D]: cfdb `st-1.42-emac` built (13,073 functions, under `out/`). 27 of 172 ColdFire rules
resolve on 1.42 (DT2 and DN2 controls: all hit); `cfmatch` pairs 67% of DT2 functions. Section 7 is a separate
ColdFire program linked at 0x40000400. MAIN does not configure the FPGA: the bootstrap does, and MAIN only reads a status
nibble. MAIN talks to its peer through a mailbox at 0x10000000 (FlexBus) with IRQ4 and a GPIO doorbell. The container
has no HMAC seal, so patchkit needs a no-seal path instead of a key.

## Estimates

Steps 1-8: about 8-11 agent days (1b adds 5-7 agent hours; step 7 grows with the kernel work). Steps 1-5 high confidence (mechanical, byte-identical proofs, much deletion);
6 and 8 medium; 7 decides itself in a day; 9 depends on the catalogue. Syntakt S1-S2: 1.5-2 days; audio beyond the
ColdFire side is out of scope.

## Status at end of session

2026-10-27: plan written. Step 1 tool committed (bcd88027). 1b probe, S1/S2 and finding 70 done. Next: graph context
and groups in the catalogue, the shared catalogue module and the DSP collector, then pre-labels and the review page.
