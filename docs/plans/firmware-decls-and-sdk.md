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
- [x] Graph context per row: the classes of the callers, and the named or anchored callees. A helper called from one
      class only gets a working name such as `ListView::helper_3` ([O]).
- [x] Groups: community detection (networkx) on the UI call subgraph, about 30-50 groups, each with its classes,
      strings and members. Export the subgraph as JSON as well.
- [x] Agents pre-label the top groups and their rows (role guess from decompilation, [O]).
- [x] Score: down-weight destructors and thunks (step 2); the review page also shows labelled rows from small groups.
- [x] Review page: group -> rows -> call neighbourhood per row, with keep / skip / later, a name and a note. Reviewed
      rows seed step 3. The page is the review view of the reference docs in step 5 (same generator).

Proof: the tool runs in seconds on both images; one unit test of the rule suggestion.

Result 2026-10-27 [D]: 5 s for both images. DT2 5381 rows (1486 anchor-grade), DN2 5198 (1468). The top rows are
unnamed helpers with many UI callers, so review needs the pre-labels.

Groups 2026-10-27 [D]: Louvain at resolution 2.0 gives 108 groups on DT2 (54 with 10+ rows). Big groups mix string and
menu plumbing; many single-class groups are destructor families. Two labellers working separately both name the same
drawing primitives: formatted text draw 0x40113dc8, rectangle fill 0x401131ae, bitmap blit 0x40114168, and the status
message printf 0x4005b23a (DT2 1.17; all [O] until checked against image bytes). The RTOS mutex and task functions at
the vector base have the same address on DN2. Score follow-up: down-weight destructors (`vfunc_0`) and libc helpers.

Review 2026-10-27: the page covers the 54 groups with 10+ functions, 910 rows, 119 of them labelled; decisions are
stored with the page and read back for step 3. Its generator is a scratch script for now; step 5 turns it into the
reference-docs generator. Two labellers labelled the same 60 rows separately: they agree on what most functions do,
but chose the same subsystem for only 21 and the same anchor name for 12. Step 3 therefore needs a fixed subsystem
vocabulary. Most high-scoring class rows are destructors or 12-byte thunks; the real menu base class is `Menu`
(destructor chain EncoderFilter, View, Menu).

### 1b. SHARC catalogue (read-only, before step 3)

- [x] Read-only probe of the DSP images (sharcdb, `tools/sharc_dev/sharc.py`), seeded by the `cpu = "sharc"` anchors
      and the frame, voice and master paths of finding 53.
- [x] Move the shared parts of the catalogue code (rule suggestion with ordinal, grade, score, sqlite/TSV writer) out
      of `atlas.py` into one module used by both CPUs. The ColdFire catalogue must stay byte-identical.
- [x] DSP collector: hash pairing between DT2 and DN2 plus call-shape pairing, stability against 1.15C / 1.10E,
      anchors from the rules, role from the call path. The DSP score rewards role path, profile heat and a unique hash,
      not UI callers.
- [x] `fw.py catalog` covers both CPUs; skill docs and one unit test.

Probe results 2026-10-27 [D]: DT2 1,147 functions, DN2 1,078. The DSP images are byte-identical across 1.16 -> 1.17
and 1.11 -> 1.12 (finding 53), so stability is measured against 1.15C / 1.10E. 143 / 142 catalogue rows, 87 / 90 with a
byte-identical twin on the other device. The 15 / 14 SHARC anchors (plus 4 `dl.cf.*` on the ColdFire) sit in 8 / 7
functions, and most of those functions are device-specific, so each device keeps its own rules. Shared building blocks,
byte-identical on both devices: block biquad, saturator, envelope generator, block copy/fill, one filter type, powf,
divide and reciprocal square root with Newton steps. The `functions.label` heuristic is unreliable; use finding 53.

Collector 2026-10-27 [D]: `uv run python -m tools.sharc_dev.sharc_catalog dt2-1.17 dn2-1.12` runs in 0.5 s; 117 / 114
rows, 66 / 67 anchor-grade (profile heat only with `--profile`). The ColdFire catalogue stayed byte-identical after the
shared module moved out.

### 2. Clean-up and deletions (no Rust)

- [x] Fix the `build/` statement in `docs/MODULE-AUTHORING.md`; document `[module].ports` (restricts patching, read
      by patchkit) against `[targets]` (status per firmware, read by `module:audit`). Both stay: they are not duplicates.
- [x] Catalogue score: destructors (vtable slots 0 and 1) and thunks of 12 bytes or less score 2 lower each.
- Moved to where they pay off: the C cache key with header dependencies to step 6 (it rebuilds every committed C
  object, and headers change often only once they are generated); generating `sdk/dk_machine.h` / `dk_fx.h` to step 5
  (`patchkit gen` changes there anyway); `cf.params.fmt` / `cf.params.slotbuild` to step 3 (they exist only in a
  generator that step 3 deletes). The cfdb dump path needs no fix: `fw.py decomp` resolves it.

### 3. Declarations (Python, host patchkit only)

- [x] Design `devices/fw/<subsystem>.toml` with Em (`docs/design/firmware-decls.md`, agreed 2026-10-27): per entry name, devices, signature, rule, mark; structs with fields
      and per-device sections only where a device differs; hooks with their register contract.
- [x] `port_resolve.py` reads `devices/fw/`; the 201 rules moved there mechanically (146 declarations, a2b60660).
- [ ] Replace the `pk_*_rules*.py` generators with `catalog emit-rule ADDR` (drop the unused `cf.params.fmt` and
      `cf.params.slotbuild` sites); retire literal tables in
      `machineprofile.py` and `cf_names.py` as their consumers move to anchors.
- [ ] Add a vtable-slot rule kind (about 63% of ColdFire candidates have no unique hash) and an ordinal for
      `sharc_func` (fixes most of the 182 shared DSP hashes on DT2).
- [ ] Hooks declare their register contract: inputs, clobbered and preserved registers, and the DSP mode (modify
      registers, SIMD). Today this is prose in findings 45 and 53.
- [x] One naming rule with a fixed subsystem vocabulary (`devices/fw/subsystems.toml`): `<cpu>.<subsystem>.<kind>.<name>`, named after the firmware, not after our module.
      `dkrt.*`, `dl.*`, `health.*` and `input.*` get firmware names; old names stay as aliases until the move is done.
- [ ] Portability count: how many declarations resolve on Syntakt 1.42 (today 27 of 172 ColdFire rules).

Proof: every `devices/ports/*.toml` is byte-identical before and after the move (aliases keep the old names).

Result 2026-10-27 [D]: the move kept every port anchor (only the rule provenance text changed). The rename gave 143
declarations (three merges: operator new, block read, the link record literal); every old anchor is still in the port
files unchanged except the hash window of two merged call targets (2 -> 4 bytes, same address), and all 924 alias
pairs carry the same values as their new names. The rule generators are deleted.

### 4. Rust build speed (before the next m68k Rust build)

- [ ] One shared target dir keyed by the toolchain pin; a prebuilt `core`/`compiler_builtins` sysroot instead of
      `-Zbuild-std`; a release build of the codegen backend; a stable staging path per module.

Proof: cold and warm build of `core/digikit-menu` before and after; objects byte-identical.

### 5. Generated bindings

- [ ] Generate `sdk/dk_machine.h` and `sdk/dk_fx.h` from `sdk.toml`; drop the test that compares the copies.
- [ ] `patchkit gen` emits Rust (`#[link_name = "<anchor>"]`), C headers (`__asm__("<anchor>")`) and assembly `.equ`
      files from the new fw directory under devices, with size and offset asserts for structs.
- [ ] Hook trampolines are generated from hook declarations. Hand assembly remains only where code enters at a patch
      site with the displaced code's registers.
- [ ] `[object.externs]` is removed.
- [ ] Reference docs: one page per subsystem with each declared anchor (signature, devices, rule, mark, status), from
      the same generator as the step 1 review page.

Proof: applied-patch bytes identical for the core modules.

### 6. Content-module SDK in C

- [ ] C object cache key includes header dependencies (gcc `-MM`); rebuild the committed C objects once.
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
- [x] Static inventory of the UI framework, drawing primitives, menus and lists, windows and dialogs:
      `docs/findings/71-ui-components-1-17.md`.

Inventory 2026-10-27 [D]/[O]: almost every component needed has a firmware class whose code is byte-identical on DT2
and DN2: view stack (ViewController push, close, invalidate), drawing (text, text width, rectangle, fill, blit, scroll
bar, toast), Menu and MenuItem (lists are Menus; value items are a draw plus an adjust function), NameView text entry,
ConfirmWindow, MultiChoiceWindow, MessageWindow, PopupWindow, ProgressWindow. Main risk: module views and dialogs need
a trampoline View class (copied vtables calling module callbacks) and a valid `shared_ptr<View>` control block; prove
that first with one confirm dialog in the emulator. Not traced yet: page switching, the View encoder slot, key names
for codes 10/12 against 0xb/0xc.

Order of work: (1) drawing and toast calls, no new objects; (2) menus through the existing `cf.menu.fn.*` path;
(3) confirm and choice dialogs (the shared_ptr proof); (4) text entry; (5) module-owned views.

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

2026-10-27: plan written. Steps 1 and 1b done; S1/S2 and findings 70 and 71 done. Review page generator in the repo
(`tools/firmware/catalog_page.py`); Em reviews later. Step 2 done. Next: the step 3 declaration design, from finding 71 and the existing 201 rules.
