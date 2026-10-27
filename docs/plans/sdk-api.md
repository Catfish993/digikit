# SDK API and module build

Status: agreed with Em on 2026-10-27.
This plan replaces step 9 (UI runtime services) of
[firmware-decls-and-sdk.md](firmware-decls-and-sdk.md) and the prebuilt-object part of its step 8.
It comes before the own-DSP work (`sdk/dsp`, the C-on-SHARC replacement).

Tick each item with its commit when it lands. Stop at the end of each phase for Em's review;
no commit without Em's OK.

Each migration deletes what it replaces in the same phase: the old manifest sections, their
generated files, the code that read them and the docs that describe them. Fixtures move too.
A manifest that still uses a removed form is refused with a message that names the new form.

## Goal

- Module authors write C against a stable, device-agnostic API of macros and functions
  for machines, effects, parameters, pages, menus and dialogs.
- The API calls a per-device adapter in `core/*`. The adapter uses the generated firmware
  bindings (`devices/fw/` to `modules/core/fw/`) and the device profile. A new device adds an
  adapter and anchors; it changes no module and no SDK header.
- The runtime menu (C) uses the same API as content modules. The runtime core stays in Rust.
- `module.toml` holds metadata only.
- A `.dkmod` is a source package. The desktop and web Transfer pages compile it, pack a `.dkm`
  and send it, with the same code (Rust, and the same code built for wasm).

## Decisions

1. Static module structure (machines, parameters, pages, FX, knobs, a module settings menu) is
   declared with C macros. The macros put versioned records in a `.dk.desc` section. The packer
   lowers them to the `.dkm` register records used today, so the runtime does not change.
   Every machine and effect has a small C file for this.
2. Run-time UI (confirm, choice, toast, open a menu, `dk_ui_has(cap)`) is reached through named
   runtime imports. A missing feature resolves to 0 and `dk_ui_has` returns false.
3. One spec, `sdk/sdk.toml`, generates the C headers, the runtime import table and the SHARC
   `.inc` files. It stays one file: about 50 tools and crates read it.
4. `core/ui` is the UI adapter, in C, one per device, on the generated `dk_fw_*.h` bindings.
   `core/digikit-menu` is rewritten in C on `<dk/ui.h>` and is its first user. The runtime
   (loader, store, upload, arena) stays in Rust. Reason: fast gcc builds, the adapter is
   firmware glue where Rust would be mostly `unsafe`, and one API for core and content modules.
5. The Rust runtime and the C UI layer share one ABI spec, `abi.toml` (new) in `modules/core/runtime/`.
   `patchkit gen` writes a C header and a Rust `gen_abi.rs` from it, with size and offset
   asserts on both sides and a compile-time signature check of each Rust export.
6. `module.toml` keeps id, version, name, author, summary, category, status, ports, requires and
   conflicts. Sources are found by convention: `src/*.c` is ColdFire, `src/dsp/*.s` is SHARC.
   `[[object]]`, `[[op]]` and `[[claim]]` remain for `core/*` only.
7. A `.dkmod` holds source only, never prebuilt objects. The receiver compiles it, so what runs is
   what the user can read.
8. The binary `.dko` replaces `.dko.json`. Content-module objects are build output in `out/` and
   are not committed. `core/*` objects stay committed.
9. ColdFire C stays on gcc 16.2 `m68k-elf`. For the browser, first try gcc `cc1` and `as` built
   for wasm; fall back to an own C compiler for ColdFire in Rust.
10. Content modules follow module-sdk.md D1 (knob slots by position) and D4 (one compiled module
   for every device). The adapter decides per device where pages go.

## Layers

```
C module  --  <dk/fx.h> <dk/machine.h> <dk/ui.h>
  |  DK_* macros write .dk.desc records; dk_ui_*() calls are named imports
  v
.dkm  --  register records (lowered from .dk.desc) + imports
  v
core/runtime (Rust)  --  import table -> core/ui
  v
core/ui adapter per device (Rust, on dkfw and the device profile)
  v
firmware: VerticalMenuView, MenuItem, ConfirmWindow, MultiChoiceWindow, Toast, parameter pages
```

## Phases

### Phase 1: spec, headers and descriptors; freqshift

- [x] Generated headers move to `sdk/include/dk/gen/` behind hand-written `sdk/include/dk/*.h`;
      SHARC includes to `sdk/asm/`, vectors to `sdk/vectors/`; `sdk/README.md`.
- [x] Define the `.dk.desc` record format in the spec (`[desc]` of `sdk/sdk.toml`, generated
      `<dk/gen/desc.h>`), with `_Static_assert` on every layout.
- [x] `DK_FX`, `DK_KNOB` and `DK_KNOB_AT` macros in `<dk/fx.h>`.
- [x] The packer reads `.dk.desc` records (`packages/patchkit/src/desc.rs`) and lowers them to
      register records; `patchkit desc MODULE` prints the effects as JSON.
- [x] freqshift: `src/module.c` replaces `[[fx]]` and `[[fx.knob]]` in `module.toml`; the module's
      `src/` is an include directory for C and SHARC+ (shared `src/freqshift.h`). fx template too.
- [x] Proof: the freqshift `.dkm` is byte-identical to the manifest build on dt2 and dn2; every
      rebuilt object differs only in `source_sha256`.
- [x] Cleanup: the five `dyn-fx-gain*` fixtures use `DK_FX`; the manifest `[[fx]]` form is deleted
      (a manifest that has it is refused), with modgen's fx outputs and `pk_conform`'s manifest
      branch. All ten fixture `.dkm` files are byte-identical.
- [ ] Follow-up: an editor config (clangd) with the SDK include paths, so authors see no false
      errors in `module.c`.

### Phase 2: pluck

- [ ] `DK_MACHINE`, `DK_PAGE` and `DK_PARAM` macros in `<dk/machine.h>`.
- [ ] Page placement per device comes from the device profile, not from the module.
- [ ] pluck: `src/module.c` replaces `[[voice]]`, `[[machine]]`, `[[param]]` and `[[page]]`.
- [ ] Proof: same register records; pluck plays in the emulator on dt2 and dn2.

### Phase 3: `<dk/ui.h>`, `core/ui` in C, and the runtime menu in C

- [ ] `abi.toml` (new) in `modules/core/runtime/`: the `dkrt_*` exports and structs. `patchkit gen` writes
      the C header and the Rust `gen_abi.rs` (layout asserts, signature checks).
- [ ] Anchors in `devices/fw/` for ConfirmWindow, MultiChoiceWindow, Toast and text drawing,
      checked on DT2 1.17 and DN2 1.12.
- [ ] Trace the toggle menu item, value formatting and the key ids for YES and NO (10/12 or 0xb/0xc).
- [ ] `<dk/ui.h>`: menus, menu items (action, toggle, value, submenu), confirm, choice, toast,
      `dk_ui_has(cap)`.
- [ ] `core/ui` C adapter on `dk_fw_*.h`.
- [ ] Host build of `<dk/ui.h>` with a fake adapter that records calls, for unit tests.
- [ ] `core/digikit-menu` rewritten in C on `<dk/ui.h>`: a ConfirmWindow replaces the two-press
      APPLY & RESTART row; toggle items for MODULES and REMOTE.
- [ ] Proof: host tests; emulator screenshots on dt2 and dn2.

### Phase 4: UI for content modules; remote

- [ ] `<dk/ui.h>` calls from `.dkm` modules reach `core/ui` through runtime imports.
- [ ] A module settings menu (`DK_MENU`) shown under SETTINGS > DIGIKIT.
- [ ] Migrate `digikit/remote`.
- [ ] Proof: host tests; emulator on dt2 and dn2.

### Phase 5: `patchkit build` in Rust

- [ ] `patchkit build MODULE` builds the ColdFire and SHARC objects in one command. gcc, as and
      selas run as child processes. This replaces `pk_build.py`, `pk_link.py` and `pk_sharc.py`.
- [ ] Binary `.dko`. Content objects go to `out/`; `build/` leaves content modules.
- [ ] `patchkit pack` reads sources or `.dko` directly.
- [ ] Proof: the objects of every module are byte-identical to the Python build.

### Phase 6: build in the browser

- [ ] 6a. Own SHARC+ assembler in Rust. Proof: byte-identical against `selas` on every module.
- [ ] 6b. Spike, at most one day: gcc `cc1` and `as` for `wasm32-wasi`. If it fails, plan an own
      C compiler for ColdFire in Rust.
- [ ] 6c. `pk_build` and `pk_pack` in `patchkit.wasm`. One Transfer flow for desktop and web:
      drop a `.dkmod`, compile, pack, send.
- [ ] Optional: a `.dkmod` carries the expected `.dkm` hash per device so a receiver can
      compare builds.

### Phase 7: migrate the rest

- [ ] bd, analog, pd, master-gain, master-drive.
- [ ] Remove the typed `module.toml` sections and the legacy Python build tools.
- [ ] Proof: byte-identical patched images.

### Later

Own DSP work (`sdk/dsp`, the C-on-SHARC replacement), text entry, module-owned views and the
Syntakt adapter (S3, S4).

## Status at end of session

2026-10-27: plan agreed. Phase 1 done, not committed, waiting for Em's review. Next: phase 2 (pluck).
