# Firmware declarations, module SDK and Syntakt

Branch: `sdk/firmware-decls`. Started 2026-10-27. Targets: DT2 OS 1.17, DN2 OS 1.12, Syntakt OS 1.42.

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
- Syntakt: ColdFire CPUs only. Never modify, emulate or reconfigure the FPGA (syx section 1). Runs in parallel as
  read-only work; DT2/DN2 come first.

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
- [ ] Agents pre-label the top ~300 rows (role guess from decompilation, [O]).
- [ ] Review page for the top ~300 rows (keep / skip / later, with name and note). Reviewed rows seed step 3.

Proof: the tool runs in seconds on both images; one unit test of the rule suggestion.

Result 2026-10-27 [D]: 5 s for both images. DT2 5381 rows (1486 anchor-grade), DN2 5198 (1468). The top rows are
unnamed helpers with many UI callers, so review needs the pre-labels.

### 1b. SHARC catalogue (read-only, before step 3)

- [ ] The same ranking for the DSP images from the sharcdb (`tools/sharc_dev/sharc.py`), seeded by the existing
      `dsp.*` and `dl.*` anchors and the voice, effect and master paths. It shows which shapes (functions, literals,
      tables, memory words) the declaration format must cover, and which stock routines step 7 could reuse.

Today's SHARC anchors: 19 on DT2 1.17 and 18 on DN2 1.12 (`dsp.fx`, `dsp.master`, `dsp.voice`, `dsp.machines`, `dl`),
rule kinds `sharc_func`, `sharc_literal`, `sharc_table`, `sharc_mem_addr`.

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
- [ ] `port_resolve.py` reads `devices/fw/`; the 201 rules move there mechanically.
- [ ] Replace the `pk_*_rules*.py` generators with `catalog emit-rule ADDR`; retire literal tables in
      `machineprofile.py` and `cf_names.py` as their consumers move to anchors.
- [ ] Add a vtable-slot rule kind (about 63% of candidates have no unique hash).

Proof: every `devices/ports/*.toml` is byte-identical before and after the move.

### 4. Rust build speed (before the next m68k Rust build)

- [ ] One shared target dir keyed by the toolchain pin; a prebuilt `core`/`compiler_builtins` sysroot instead of
      `-Zbuild-std`; a release build of the codegen backend; a stable staging path per module.

Proof: cold and warm build of `core/digikit-menu` before and after; objects byte-identical.

### 5. Generated bindings

- [ ] `patchkit gen` emits Rust (`#[link_name = "<anchor>"]`), C headers (`__asm__("<anchor>")`) and assembly `.equ`
      files from `devices/fw/`, with size and offset asserts for structs.
- [ ] Hook trampolines are generated from hook declarations. Hand assembly remains only where code enters at a patch
      site with the displaced code's registers.
- [ ] `[object.externs]` is removed.

Proof: applied-patch bytes identical for the core modules.

### 6. Content-module SDK in C

- [ ] Generated C header for the runtime `Api` table and `dkrt_*` exports.
- [ ] Generated per-module header from `module.toml` (parameter ids, knob order) for C, assembly and Rust.
- [ ] `module:new` templates: ColdFire C, SHARC assembly machine, effect.
- [ ] Move firmware codes (`template`, stock knob codes, `type_hint`) from author manifests to device profiles. This is
      what makes module source device-agnostic.
- [ ] One authoring guide; old SDK docs folded in or deleted.

Proof: `module:check` for pluck and freqshift; a new C module built in under a second.

### 7. SHARC C pilot (bounded, 1 day)

- [ ] Control code in C (note_on, parameter mapping) plus block-level assembly kernels from the SDK. A call costs about
      25 instructions per 32-sample block, under one per sample.
- [ ] Pluck's string loop in C against the assembly, cycles measured in the emulator.

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

- [ ] Menus, lists and scrollbars exposed through adapters with a device-agnostic API, from the reviewed catalogue.

Sized after step 1.

### Syntakt track (parallel, read-only first)

- [x] S1. Static onboarding: identify the images (MAIN at 0x40000400; section 7, raw ColdFire code, possibly the second
      CPU), build cfdb and a separate Ghidra project, dry-run all rules on 1.42, and propose `devices/syntakt.toml`.
- [x] S2. Static trace of how MAIN talks to the second CPU and starts the FPGA (interface only), what the boot waits
      for, and how the container seal key is derived. **Decision point.**
- [ ] S3. Emulator boot to the main screen, with the FPGA interface stubbed (1-2 days, after S2).
- [ ] Write the S1/S2 results to `docs/findings/` (one file, all marks [D]/[O]).

S1/S2 results 2026-10-27 [D]: cfdb `st-1.42-emac` built (13,073 functions, under `out/`). 27 of 172 ColdFire rules
resolve on 1.42 (DT2 and DN2 controls: all hit); `cfmatch` pairs 67% of DT2 functions. Section 7 is a separate
ColdFire program linked at 0x40000400. MAIN does not configure the FPGA: the bootstrap does, and MAIN only reads a status
nibble. MAIN talks to its peer through a mailbox at 0x10000000 (FlexBus) with IRQ4 and a GPIO doorbell. The container
has no HMAC seal, so patchkit needs a no-seal path instead of a key.

## Estimates

Steps 1-8: about 7-10 agent days. Steps 1-5 high confidence (mechanical, byte-identical proofs, much deletion);
6 and 8 medium; 7 decides itself in a day; 9 depends on the catalogue. Syntakt S1-S2: 1.5-2 days; audio beyond the
ColdFire side is out of scope.

## Status at end of session

2026-10-27: plan written. Step 1 tool done (not committed); pre-labels and review page next. S1/S2 done.
