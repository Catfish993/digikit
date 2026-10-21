# Transfer tab v1 (W3): tick modules, install them on the emulator, load without a manual APPLY & RESTART (plan, 2026-10-21)

Status: plan only, no code. Lane `tasks-images`, base main 025504c4. Facts below were read from the code on 2026-10-21 and are **[D]** (read once, nothing was run). Paths of files that do not exist yet are written without their directory prefix on purpose (pathcheck).

## Goal (agreed with Em)

The workbench gets a Transfer tab that replaces the Modules popup (`apps/web/src/components/ModulesPanel.tsx` mounted by `apps/desktop/ui/src/Console.tsx`).

- Target: the running emulator only. A target selector for a real device comes with W5.
- Content: Modules only. Samples come with W6 and W7.
- The list shows the repo's run-time modules that load on the current device (device and OS version from `status`, manifest `ports`). Never `core/*` (type `framework`). Test fixtures (id prefix `test/`) are behind a "show test modules" toggle.
- Groups come from the `.dkm` header contracts (offset 0x38): machine is Machines, fx is Effects, service or api only is Tools. A repo module that is not packed yet is grouped by its manifest `type`. Pack checks that the manifest type matches the contracts.
- A row shows name, version, size (file, arena, DSP), state (on card, loaded via MODULES, needs build), whether it fits, and a tick box.
- Install, for the ticked modules: build only the changed ones, pack in-process, cache, upload (skips files already on the card by sha256), SELECT the installed set, RESTART, so they load without a manual APPLY & RESTART, then refresh the list.
- "Add .dkm" and the drop zone stay, smaller, for modules from outside the repo.
- The panel stays shared with the web app. The web upload page must not break. W8 (web parity) reuses the panel later with prebuilt `.dkm` assets.

## What exists today (**[D]**)

- **Panel and backends.** `ModulesPanel` talks to a `ModuleBackend` (`apps/web/src/modules/backend.ts`): `info`, `list`, `put`, `remove`, `clear`. The desktop backend is `apps/desktop/ui/src/modules-backend.ts` (Tauri commands `mod_info`, `mod_list`, `mod_put`, `mod_delete`, `mod_clear`, `mod_progress`, `mod_cancel` in `apps/desktop/src-tauri/src/modules.rs`). The site backend is Web MIDI through `dkupload.wasm` (`apps/web/src/modules/web-backend.ts`). The web upload page (`UploadPage.tsx`) mounts the same panel.
- **Desktop transport.** `EmuTransport` in modules.rs sends SysEx through the controller's `midi.usb.in` and reads `midi.usb.out` on a `digi_control::Handle`. One `busy` mutex serialises conversations. Progress of a put is polled (`mod_progress`, 150 ms), cancel is a flag.
- **Rust upload client.** `packages/dkupload` has `Session` with `info`, `list`, `delete`, `clear`, `abort`, `put`, `repack`. It has no MODULES, CAPACITY, SELECT, RESTART or LOG request. `gen_proto.rs` has the command numbers, status codes, capability bits and CAPACITY kind numbers. `mock.rs` implements INFO, LIST, BEGIN, DATA, END, DELETE, CLEAR, ABORT only. The Python client has all of them (`tools/device/dkupload.py`: `modules()`, `capacity()`, `select()`, `restart()`, `log_page()`).
- **dk run.** `tools/device/dkrun.py` is the reference flow: INFO, `pk_build`, `patchkit pack --device`, PUT to `modules/<last id part>.dkm` unless LIST shows the same sha256, SELECT with that one path, RESTART, wait for the new boot, read MOD_LOAD and MOD_DSP. The selection becomes that module only. The emulator boot is awaited through the `firmware.loaded` event after `cpu.reset`.
- **Runtime restart in the emulator.** A guest soft reset (what RESTART runs) boots the same patched image again on the card as the guest left it (`soft_reset` in `packages/emulator/control/src/controller.rs`). It is not a new session. The desktop app already has this path (the device menu's APPLY & RESTART).
- **Boot with the runtime.** `firmware.load {runtime}` patches the stock image in-process (`packages/emulator/control/src/runtime_patch.rs`), reads the module library with `Library::scan_repo`, caches the patched image by a key over module digests, device files and `patchkit::SRC_ID`. It needs the repo root (`find_root`: a dev build only; a shipped app needs a bundle).
- **Module library.** `Library::scan_repo` reads `modules/` and `tests/fixtures/modules/` (`packages/patchkit/src/module.rs`). The Python twin is `tools/patchkit/modroots.py` (it also reads `PATCHKIT_MODULES`). A module's id need not equal its directory (`modules/machines/pluck` is `digikit/pluck`).
- **Packing.** `patchkit pack` is code inline in `packages/patchkit/src/main.rs` (about 100 lines: pick the one ColdFire object, lower typed sections for the device, link the DSP object, `dkm::pack_full`). The library has no function for it. `Dkm::parse` does not return the contracts at 0x38. `pack_full` writes api always, machine when there is a DSP section, service for slot functions or a real init/fini, fx never.
- **Build.** `tools/patchkit/pk_build.py` builds the committed `build/*.dko.json` objects. Each object carries `source_sha256`. It is computed differently per toolchain: ColdFire C hashes the listed source files only (no headers), SHARC hashes the text after `#include` expansion and `#ifdef` selection, Rust hashes the crate files, path dependencies and the toolchain pins (`pk_rust.source_sha256`). `--check` rebuilds everything and compares, which is too slow for a list view (Rust objects).
- **Capacity.** CAPACITY (0x04) reports 11 kinds with limit, used, largest. MODULES (0x03) reports the loaded modules with name, packed version, contracts, ColdFire block, DSP range, rows, types, faults (`docs/design/digikit-upload.md`, finding 57). The limits are 8 modules, 8 selection paths, 512 KiB arena, 64 KiB staging (so at most one `.dkm` file size), DSP area 512 KiB (DT2) or 320 KiB (DN2), 8 machine, voice and 16 parameter rows, 8 page rows on the DN2.

## Findings that shape the plan

1. **The host cannot read a card file back.** There is no GET and no HEAD command. LIST gives path, size and sha256 only. The host can read a header only for a file it holds: a repo build, a file the user added in this session, or a loaded module (MODULES gives name, version, contracts, sizes). So "files found on the card are grouped from their header" works for files the host can match by sha256, and for modules that are loaded. Other files show as "on card, unknown" without a group. Closing this needs a new runtime command (HEAD: the 0xa0 header bytes of a store file). That is a runtime change (a rebuilt framework module and the full gate); not in v1 (open point 3).
2. **The host cannot read the selection back.** SELECT writes the whole record (0 to 8 paths); there is no read. The loaded modules (MODULES) are the best view of it, and MODULES gives names, not store paths. See "SELECT semantics".
3. **The v1 list is short.** Only modules that pack for the device are loadable. Today that is `digikit/pluck` (a machine) on the Digitakt II, plus the fixtures behind the toggle (`test/sine`, `test/dyn-*`, raw ColdFire `test/*-demo`). `digikit/master-gain` and `digikit/master-drive` have type `fx` but are not `dynamic`, and run-time effects do not exist (finding 57: FX_SLOTS used 0), so `pack` refuses them. Effects stays an empty group. The tab is the road for modules that come later; it does not make many modules appear now.
4. **The manifest `ports` list is not enough to say "loads here".** A module can list a device in `ports` and still not pack for it (no DSP object for that device). The truth is a dry-run pack for the device. The plan classifies by packing.
5. **The header contract is not always truthful.** `pack` sets the machine contract only when a DSP section exists. A data-only machine module (the register records alone: `test/dyn-params`, `test/dyn-xslice`, `test/dyn-dn2`) gets api only, so the agreed grouping rule puts it in Tools, and a pack-time check "type machine needs the machine contract" would fail it. Fix: the packer also sets machine when the register records hold machine records. The loader only compares majors of the contracts a module declares, so this is safe, but the header bytes of those fixtures change (open point 1).
6. **The stale check cannot live in Rust without copying three hash rules.** The Python build owns them. Python gets a cheap `status` mode (hashes only, no toolchain). Known gap: ColdFire C headers are not in `source_sha256`, so a header-only change is not seen. The panel offers "rebuild" per module for that case.
7. **The store only appends.** A replaced file leaves dead data until CLEAR. Repeated installs of a rebuilt module fill the store. The install needs a free-space check and a repack route (CLEAR, then PUT every file again), which needs every card file to be held by the host.
8. **pathcheck and the workspace.** A new crate must be listed in the root `Cargo.toml` members and routed in `tools/devtools/verify_route.py` (the crate-to-tests map). Check with `mise run test:changed -- --explain` before the first edit.

## Design

### Layers

```text
TransferPanel (shared TSX)  --TransferBackend-->  desktop commands (thin)  -->  crate `transfer` (new)
 transfer-core.ts (pure logic, node-tested)                                      catalog, fit, stale, install, Job
                                                                                 |        |         |
                                                                       patchkit lib   dkupload   pk_build (Python, subprocess)
                                                                       pack_module    Session    status / build
                                                                       Dkm needs
```

The logic goes in a new library crate `transfer` (a workspace member), not in `apps/desktop/src-tauri`. The Tauri commands only unwrap arguments. Reasons: the crate is testable with cargo only (no webview, no Tauri state), the same crate serves W5 (a MIDI transport for a real device) and a CLI later, and the desktop crate keeps only the glue. `EmuTransport` moves from modules.rs into the crate (it depends on `digi-control` for `Handle`). The existing `mod_*` commands stay (the web upload page and Delete and Repack use that interface) and call the same session helper.

Considered and rejected:

- *Install as a controller method* (`xfer.install` in `digi-control` `METHODS`, reachable through `emu_call` and the socket): the controller thread would run an install that calls the controller (`midi.usb.in`) and deadlocks, unless it becomes a job like `firmware.load`. It also puts subprocess builds into the emulator crate.
- *Install in Python* (`dkrun` extended, the desktop shells out): fastest to write, and the SELECT and RESTART flow is proven there. But progress, cancel and the in-process `Handle` transport need plumbing across a process boundary, and W5 and W8 would need the flow a third time. Python keeps only what it owns: the build and the stale hash.

### Backend interface

`ModuleBackend` is unchanged. A new optional interface extends it, in `apps/web/src/modules/backend.ts`:

```ts
interface TransferBackend extends ModuleBackend {
  catalog(target: { device: string; version: string }, opts: { showTests: boolean }): Promise<Catalog>;
  stale(ids: string[]): Promise<Record<string, 'fresh' | 'stale' | 'unknown'>>;   // slow part, after the list is shown
  add(name: string, data: Uint8Array): Promise<CatalogRow>;                        // upload + inspect an outside .dkm
  install(req: InstallRequest): Promise<void>;                                     // starts a job
  events(since: number): Promise<{ events: InstallEvent[]; next: number; running: boolean }>;
  cancel(): Promise<void>;
}
```

Desktop commands (new, in a new file next to modules.rs): `xfer_catalog`, `xfer_stale`, `xfer_add` (raw body, like `mod_put`), `xfer_install`, `xfer_events`, `xfer_cancel`. They use the shared `busy` mutex only for the phases that talk to the device (a build of minutes must not block INFO). One install at a time (a second `xfer_install` is refused with "an install is running").

Progress is polled with a cursor (`xfer_events(since)`), as `mod_progress` is today: the crate's `Job` holds an event vector, a state and a cancel flag, so cargo tests read it without Tauri. (A Tauri `Channel` would stream, but it ties the test to Tauri.)

The web site keeps `ModulesPanel` and its page unchanged. The panel's file add and drop code is cut out into a small `FileDrop` component that `ModulesPanel` and `TransferPanel` both use. In W8 the site gets a `TransferBackend` whose `catalog` reads a JSON of prebuilt `.dkm` assets (the header facts come from patchkit's wasm build, `packages/patchkit/src/wasm.rs`), and `TransferPanel` replaces `ModulesPanel` on that page.

### Catalog row

```text
Row { id, name, version, group: machines|effects|tools|unknown, source: repo|file|card|loaded,
      path (store path), file_bytes, needs { arena, arena_transient, dsp, machine_rows, voice_rows, param_rows, page_rows },
      state { card: absent|same|differs, loaded: no|yes|older, build: fresh|stale|unknown|unavailable },
      fits { ok|no|unknown, reasons[] }, test: bool, tick_default: bool }
```

- **Discovery.** `Library::scan_repo(root)`; drop `framework`; ports: empty or containing `<device>-<version>`; dry-run pack for the device (cached); a module that does not pack is not a row. Non-test ones that fail are counted in `hidden` with the reason (one line under the list: "2 modules do not load on dn2-1.12"), so nothing disappears silently. Rows whose id starts with `test/` are marked `test` and shown only with the toggle.
- **Grouping.** From the packed header: machine major not 0 gives Machines; else fx not 0 gives Effects; else Tools (service or api only). A module with machine and service is a machine. A repo module that cannot be packed (not built yet) is grouped by manifest `type` (machine, fx, service; raw gives Tools) and marked `needs build`. A file added from outside is grouped from its parsed header. A card file the host cannot match is `unknown`.
- **Pack-time check.** `pack_module` fails when the manifest type and the contracts disagree: machine needs the machine contract, fx needs fx (so a dynamic fx module is refused until run-time effects exist), service needs service. `raw` is not checked. The message names both.
- **Size.** File bytes; arena (ColdFire image + bss, 16-byte aligned, plus the allocator header); DSP (image + bss rounded to 64-byte granules, plus voices times state bytes per voice); rows from the register records and the DSP voice records. `Dkm::needs()` computes them (one function, in patchkit, next to the loader's format code). A test pins it against MODULES and CAPACITY after a real boot (see Tests), so the model is checked, not assumed.
- **State.** `card`: LIST has the store path with the same sha256 (same), another sha256 (differs), or no entry (absent). `loaded`: a MODULES record with this name (the header name is the module id), and `older` when its version is below the packed one. `build`: from `stale`.
- **Fits.** The module alone against CAPACITY limits (not "free": a boot starts empty and the selection replaces the loaded set), and the whole ticked set live in the panel (`transfer-core.ts` sums the needs: modules, selection paths, arena, DSP area with granules, rows). A runtime without CAPACITY (bit 6) gives `unknown`. The estimate is advisory: the loader refuses cleanly and each module's real result is shown after the boot (finding 57).

### Store path and name

Path `modules/<last part of id>.dkm` (as `dk run`); header name = module id. The catalog refuses two ticked rows with the same path (for example ids `a/x` and `b/x`) and says which. An outside file keeps its own file name and path (`storePath`).

### Stale check and build (Python owns them)

- `pk_build status MODULE_DIR... --json` (new subcommand): per object, committed `source_sha256` and fresh `source_sha256`, using the same functions the build uses, without compiling. A missing object file is `missing`. Output is one JSON document. Cost: Python start plus file hashing, so the catalog returns first and `xfer_stale` fills the `build` state afterwards.
- Build: the crate spawns `<root>/.venv/bin/python -m tools.patchkit.pk_build build <module dir>` (fallback `uv run python`), cwd the repo root, with `PYTHONPATH` set to the repo root first (what `tools.python_env()` does), reads stdout and stderr by line into `log` events, and kills the child on cancel. A module that is stale is built; "rebuild" in the row menu forces it (headers, toolchain change). No Python or no `.venv`: `build: unavailable`, and an install of a stale module fails before it touches the device with a message that says so.
- The two known gaps (C headers, toolchain version) are written in the panel's rebuild tooltip.

### Pack and cache

`patchkit::dkm::pack_module(lib, root, id, device, name) -> Packed { bytes, header facts }` is cut out of main.rs; the `patchkit pack` CLI calls it. The CLI output must stay byte-identical (checked, see Tests). The transfer crate caches the result on disk, `out/dkm-cache/<key>.dkm`, with the key built like `runtime_patch::cache_key` (module digest, device, device files, `patchkit::SRC_ID`; reuse that function, not a copy), written whole or not at all. The cache's other use is the list: size and contracts for every candidate without a pack per refresh. If measurement shows scan plus pack under a second for all candidates, the disk cache is dropped and an in-process memo stays (open point 6).

### Install flow

Request: ticked ids, optional forced rebuilds, `restart` (default true). One `Job` with ordered steps and events.

1. **Plan.** Resolve the rows. Preconditions from INFO and LIST: runtime has SELECT and RESTART (version 4 or newer); card not read-only (`E_INUSE` flag); not safe mode (warn: modules do not load in safe mode); an upload not open. The final selection is the ticked set plus the "kept" set (SELECT semantics). Refuse more than 8 paths, a path clash, a file over 64 KiB. Nothing changes on the device so far.
2. **Build** each ticked repo module whose `build` state is stale or forced. A failed build stops the install here: no upload, no SELECT (atomic from the device's view). The user unticks the module and installs the rest.
3. **Pack** each (cache). The type and contract check runs here.
4. **Fit.** Set-level estimate against a fresh CAPACITY. A "no" is shown with reasons and needs a confirm (the install may still work; the loader refuses cleanly).
5. **Space.** Needed bytes (each file rounded up to 512 plus one sector for the table) against INFO `free_bytes` (only the files that will be sent). If short: when every card file is held by the host (`repack_missing` is empty), the job offers "repack and install" (CLEAR, then PUT the kept files and the new ones); else it stops and names the files it does not hold.
6. **Upload** each file whose sha256 is not already on the card at that path (LIST first). Progress per file; cancel aborts the open upload on the device (existing behaviour). A path with a different file of the same name is replaced, and the row says so.
7. **SELECT** the final path list: replaces the whole selection record.
8. **RESTART** (unless "select only"), subscribing to controller events before the request, so the `cpu.reset` and `firmware.loaded` events cannot be missed. Then wait for the boot (emulator: `firmware.loaded` ok; timeout generous, the DSP cold start is slow), then retry INFO until the runtime answers. A refused restart (`E_STATE`, or the log's UPLOAD error for cmd 0x31) is retried a few times, then reported with the hint "APPLY & RESTART on the device menu does the same".
9. **Verify.** MODULES: each selected module present? LOG from the new boot: MOD_LOAD and MOD_DSP result per module, with names of the result codes (E_NOMEM, E_DSP_NOSPACE, E_CONTRACT, ...). The job result is a list of per-module lines: loaded, loaded (DSP part pending), refused with code.
10. **Refresh** the catalog (the panel does this when the job ends).

If the ticked set equals the loaded set and every file is on the card unchanged, the job says "nothing to install" and skips SELECT and RESTART (a "restart anyway" button stays).

Cancel: any time before SELECT leaves the device as it was (files uploaded so far stay on the card; harmless). After SELECT, cancel is offered only for the wait (the selection is written; the next boot loads it). A transport failure in the middle reports the step that failed and what state the device is in ("files sent, selection unchanged").

### SELECT semantics

SELECT replaces the whole selection (that is how the protocol works; it cannot add). So the tab shows and edits the whole selection:

- **Initial ticks = what is loaded now.** A row is ticked when MODULES lists its name. Installing then keeps every loaded module the user did not untick. "What you see is the next boot."
- **Untick a loaded module and install:** it is dropped from the selection and unloads at the restart. The row says "will unload" before the install. Its file stays on the card.
- **Loaded modules the host cannot tie to a card file** (an outside module loaded before this session, no matching sha256): the host tries `modules/<last part of name>.dkm` in LIST. If that file is on the card, the row is real and keeps its tick. If not, the panel lists them under the table ("2 loaded modules have no known card file; they will not load after this install") and asks for a confirm. They are never silently dropped.
- Selected but failed to load (not in MODULES) cannot be seen; they lose their selection at the next install. The previous boot's MOD_LOAD events could show them (LOG); not in v1.

### Modules on the card that are not ticked

Not deleted, not selected. Their rows read "on card" (and "loaded" only when MODULES has them). Delete and Repack stay as per-row and per-store actions (the existing `mod_delete` and `mod_clear`), with Repack offered when the free space is low. An install never deletes. `Transfer` shows the store's free bytes and the dead-space warning from point 7.

### Error and progress reporting

- Errors keep the existing JSON shape (`error`, `kind`, `status`, `free_bytes`; `errorLine`, `isCancelled`, `isTimeout` in `backend.ts`) and add `step` (plan, build, pack, fit, space, upload, select, restart, verify) and `module` where one applies.
- Events: `step {name}`, `log {line}` (build output, capped at a few thousand lines), `progress {module, done, total, index, count}`, `module {id, result}`, `done {summary}`, `error {...}`.
- The panel shows a step list with the current one active, a collapsed log for the build, a cancel button, and one result line per module at the end. Device error codes are shown with their meaning (`Status::meaning`).
- A paused emulator: the transport polls `midi.usb.out`, which needs a running emulator, as the current panel does. The install checks `status` first and says "resume the emulator" instead of timing out.

### What does not change

The runtime (no new device command), the SysEx protocol, the store format, `.dkm` format (except open point 1), the web upload page, `ModuleBackend`, the `mod_*` commands, `firmware.load`.

## Tests (each step without the GUI)

| Step | How it is tested | Where |
| --- | --- | --- |
| Pack in the library | `patchkit pack` output byte-identical before and after the cut: record the sha256 of every packable module for both devices before the first edit (with `patchkit pack` from the current binary), compare after. A cargo test that `pack_module` equals the CLI for `digikit/pluck` and two fixtures. Contract and needs unit tests on `patchkit synth-dkm` files (exact sizes, known contracts). The type check: a table of (type, contracts) pairs, each accepted or refused with the message. | `cargo test -p patchkit` (through `test:changed`) |
| `Dkm::needs` model | Against facts that exist: the `capacity-*` sets of `tools/patchkit/pk_capacity.py` give module sizes from the profile; compare arena and DSP needs of synthetic modules with its numbers. After one real boot (the e2e test below), compare the needs of `digikit/pluck` with its MODULES record (`cf_size`, `dsp_len`, rows) and with CAPACITY used. | cargo; the e2e test |
| dkupload requests | Parse tests with the byte layouts of `docs/design/digikit-upload.md` (MODULES record 71 + n_types bytes, CAPACITY entries of 13 bytes, SELECT body, LOG events of 32 bytes), the same vectors as `tests/digikit` use. Mock: SELECT stores the list, RESTART makes the mock "boot" it (loaded = selected, log has BOOT_START), E_STATE during an open upload, 8 and 9 paths, E_INUSE. Session tests like `packages/dkupload/tests/session.rs`. A Python twin check: the Rust parser and `tools/device/dkupload.py` give the same figures on one captured reply. | `cargo test -p dkupload` |
| `pk_build status` | Over all committed modules it reports fresh (the existing `test_pk_current` already proves the objects are current, so it is the oracle). On a temp copy of one module with an edited source or an edited `#include`d file it reports stale; a deleted object reports missing. No toolchain needed. | pytest, one file (`test:changed`) |
| Catalog | Golden test over the repo for `dt2-1.17` and `dn2-1.12`: row ids, groups, hidden counts (committed objects make it deterministic). Toggle. Port filter. Path clash. Grouping by header and by manifest fallback. Fit: synthetic CAPACITY replies, set sums, granule rounding, unknown without the bit. | `cargo test -p transfer` |
| Install executor | Generic over `Transport` and an `Env` trait (builder, clock, boot waiter) with fakes: a fake builder (success, failure, cancel kill), the dkupload mock as device. Cases: fresh install, same sha skips upload, replace, E_FULL then repack route, build failure leaves the mock untouched, cancel before and after SELECT, refused restart then retry, ticked equals loaded (nothing to do), unticked loaded module leaves the selection, 9 paths refused, outside loaded module confirm. The event vector is asserted step by step. | `cargo test -p transfer` |
| Real subprocess | One test runs `pk_build status` through the crate's runner on the repo (skipped without `.venv`). | `cargo test -p transfer` |
| Desktop commands | The existing pattern in modules.rs: unit tests for argument handling with a `Handle` from `digi_control::controller::spawn`. Commands stay thin. | `cargo test` in `apps/desktop/src-tauri` |
| End to end on the emulator | An integration test in the crate: spawn the controller in-process in free-run mode (as the desktop does), `firmware.load {runtime}` of the stock DT2 1.17 image, run the real install of `digikit/pluck` with `--no-build` semantics (committed objects), wait for the restart, assert MODULES lists it, DSP part loaded, and the needs match its record. Skipped when `firmware/Digitakt_II_OS1.17.syx` is absent, like the runtime-patch test. Marked as a slow test (two boots); run once per PR, not in `test:changed`. The same flow over the socket is already covered by the scenario `menu-dkrun-pluck` (Python `dkrun --emu`). | `cargo nextest` one test, run by the lane once |
| Panel logic | `transfer-core.ts` (new, next to `dkupload-core.ts`) is plain TypeScript without runtime-only syntax (like `dkupload-core.ts`), so node tests import it: initial ticks from loaded, set fit sums, grouping order, "will unload" and "no card file" notices, the event reducer (step list, log cap, result lines), the 8-path limit. Node tests for a `TransferBackend` fake driving the panel's state functions. No DOM test of the Solid component in v1; one `astro check` and typecheck run by `test:changed`. | `transfer.test.mjs` (new, in the web tests directory) |
| Not broken | `dkupload.test.mjs` and the existing panel tests stay green; `ModulesPanel` is only touched by the `FileDrop` cut, checked by a node test of the repack and drop helpers it already has. | node |

Selection of the gate for each PR is read with `mise run test:changed -- --explain` and `mise run verify -- --explain --base main`; patchkit and dkupload changes route to the patchkit suite and rust-tests. The coordinator runs `mise run verify` once per merge. No PCM pin changes (no emulator or DSP change); open point 1 changes `.dkm` header bytes of three fixtures, so the tests that compare packed bytes or hashes are found and updated in the same PR (not the pins).

## PR steps

Three PRs. A and B are cargo-only; C holds the UI. Each step is testable alone.

**PR A: libraries (no UI).**

1. `patchkit`: `Dkm.contracts`, `Dkm::needs()`, `Group` from contracts, `pack_module` cut out of main.rs, type check, machine contract for data-only machines (if open point 1 is accepted), result-code names generated from `sdk/sdk.toml` (the runtime's `E_*` table, next to `gen_proto.rs`). Record the byte hashes first.
2. `dkupload`: `modules()`, `capacity()`, `select()`, `restart()`, `log()`; parse functions; mock support; LOG kind numbers generated into `gen_proto.rs` (the generator `sdkgen.rs`).
3. `pk_build status --json`; its test.

**PR B: the `transfer` crate and the desktop commands.**

4. Crate skeleton and `EmuTransport` moved from modules.rs; `Catalog` (scan, filter, pack, group, state, cache).
5. Fit.
6. Stale and build runner.
7. Install executor with `Job`, events, cancel; fakes and mock tests.
8. Desktop commands `xfer_*`; `mod_*` call the crate's session helper.
9. The end-to-end test.

**PR C: the tab.**

10. `transfer-core.ts` and its node tests; `TransferBackend` types; `FileDrop` cut from `ModulesPanel`.
11. `TransferPanel` (groups, rows, toggle, tick boxes, install button and step list, add and drop); `apps/desktop/ui/src/modules-backend.ts` implements `TransferBackend`.
12. `Console.tsx`: the Transfer tab replaces the Modules popup (target "Emulator" fixed, content "Modules", "Samples" shown disabled); remove the `modules-sheet`.
13. Docs: `docs/design/digikit-upload.md` (the Rust client now has MODULES, CAPACITY, SELECT, RESTART, LOG), docs/TESTING.md (the new tests), a design note for the tab in `docs/design/`. Findings only for facts verified by a run (the model of `needs`).

Order: 1 and 2 and 3 are independent (parallel lanes are possible, but each is small; one lane is enough). 4 needs 1; 7 needs 2, 4, 5, 6. C needs B's command shapes only (a fake backend is enough for 10 to 12).

## Estimate (agent time) and payoff

Payoff: today a module reaches the emulator through `dk run` on a terminal, one module at a time, and the workbench panel only uploads files that someone already built and packed by hand. The tab makes build, pack, upload, select and restart one action in the app and shows what fits. It is the same flow that W5 (device) and W8 (web) reuse, so the crate and the interface carry more than the tab. Be clear about the near-term value: the list has one real module on the Digitakt II today (`digikit/pluck`). The gain grows with the modules we add. Confidence that the flow works end to end on the emulator: medium-high (every protocol step is proven by Python and scenarios). Confidence that the UI is right on the first pass: medium.

| Step | Hours | Confidence |
| --- | --- | --- |
| 1 patchkit (cut, contracts, needs, check, names) | 5 to 7 | medium-high (the byte-identity check is the risk guard) |
| 2 dkupload requests and mock | 4 to 5 | high |
| 3 `pk_build status` | 2 to 3 | high |
| PR A total | 11 to 15 | |
| 4 to 6 catalog, fit, stale and build runner | 9 to 12 | medium (the `needs` model, subprocess edges) |
| 7 install executor and tests | 6 to 8 | medium |
| 8 and 9 desktop commands, e2e | 5 to 7 | medium (restart timing in a running app) |
| PR B total | 20 to 27 | |
| 10 to 12 UI | 10 to 14 | medium |
| 13 docs | 2 | high |
| PR C total | 12 to 16 | |
| Total | 43 to 58 hours, about 4 working days with one lane | medium |

Outcome risk, separate from time: the `needs` model is wrong for some module shape (arena copy of the DSP image, alignment of the DSP area); it only makes the advisory "fits" wrong, and the boot result is still shown. A restart that is slow or flaky inside the running app (the cold DSP start) would cost one extra day. Stopping rule: after PR B's end-to-end test, if the restart inside the in-process controller is not reliable in three runs of five, stop and report before building the UI. Recommendation: do it.

## Risks

1. **Restart in the app.** The soft reset boots coupled audio again (a DSP cold start). The wait is long and the controller refuses some commands during it; the install must retry transport errors and show "waiting for the boot". Mitigation: wait on events, not on a fixed time; the e2e test measures it.
2. **The `needs` model.** Allocator headers, 16-byte alignment, the arena copy of a DSP image, 64-byte DSP granules, the 4-byte offset of the area (finding 57). A wrong figure shows a wrong "fits". Mitigation: the pins against MODULES and CAPACITY; advisory only.
3. **Stale detection gaps.** ColdFire C headers and the compiler version are not in `source_sha256`; a stale object may pack as fresh. Mitigation: per-row "rebuild", and the gap is written in the tooltip. Fixing the hash changes every committed object, so it is out of scope.
4. **Selection unknown.** Replace semantics with initial ticks from MODULES is right when the last boot loaded the whole selection; a selected module that failed at the last boot, or a module selected in the device menu but not loaded because of safe mode, is dropped by the next install. Mitigation: warn in safe mode; the HEAD and a selection read are future protocol work (open points 2 and 3).
5. **Dead space.** Repeated installs fill the store. Mitigation: space check and repack route; Repack stays.
6. **Dev-build only.** The catalog needs the repo root (`find_root`), the build needs `.venv` and the toolchains. A shipped app has neither. The catalog degrades: no build, `unavailable` state, rows from prebuilt `.dkm` only. The `Builder` and `Library` source are traits so a bundle can come later (W8).
7. **Two sources of truth for sizes.** `needs` (Rust) and `pk_capacity.py` (Python) both compute module sizes. Mitigation: the test compares them; one of them should call the other later.
8. **Card persistence.** A new `firmware.load` (Open, Reload) resets the emulator's card unless the remembered-card path applies (DT2 only: `remember_card`). Installed modules can vanish with a reload, and the tick state then reads "not on card". The panel refreshes from LIST so it does not lie; whether to persist the card across reloads is not part of this plan.
9. **Paused emulator** and a closed session: the install checks `status` first.
10. **Module requirements.** A run-time module that lists `requires` on another run-time module is not handled (none exists). The catalog refuses to tick it with the message; auto-tick comes when a case exists.
11. **Manifest/header drift** after open point 1 changes three fixtures' headers: found by the tests that compare packed bytes or hashes; updated in PR A.
12. **Lane use.** The work is cargo, Python and TSX in the same tree; one lane (`tasks-images`) in order A, B, C. Builds of the desktop crate are cold-heavy; the worktree setup keeps the target directory warm.

## Open points (for the coordinator)

1. **Make the header truthful for data-only machines.** The packer sets the machine contract only with a DSP section, so `test/dyn-params`, `test/dyn-xslice` and `test/dyn-dn2` would group as Tools and fail the pack-time type check. Options: (a) the packer also sets machine when the register records hold machine records (three fixture headers change); (b) leave the packer, group by header with a fallback to the manifest type when the contracts are api only and the manifest says machine, and run the type check only when a DSP section exists. Recommendation: (a). The loader only compares declared majors, the header then says what the module is, and one rule (the header) serves the panel, the check and later tools.
2. **SELECT: replace or add.** The protocol only replaces, and the selection cannot be read. Options: (a) replace, with initial ticks from MODULES and a warning for loaded modules without a known card file; (b) "add" by assuming the selection equals the loaded set. Recommendation: (a). It is honest about what the protocol can do, and (b) is the same thing with the unticking removed. A selection read command is a later runtime item.
3. **A HEAD command to read a card file's header.** Without it, card files that the host does not hold stay ungrouped ("unknown") and a repack cannot keep them. Options: (a) add HEAD (0xa0 bytes of a store file) to the runtime now; (b) not in v1; the host matches files by sha256 against repo builds and files it has seen. Recommendation: (b). It is a runtime change and a full gate for a case that mostly concerns files from outside; ask for it when W5 (a real device with a card we did not fill) needs it.
4. **Does a failing build stop the whole install.** Options: (a) stop before touching the device; (b) install the modules that built. Recommendation: (a). The ticked set is what the user asked for, the device stays as it was, and (b) can be had by unticking.
5. **A "no fit" estimate: block or warn.** Options: block, or warn and ask for a confirm. Recommendation: warn with a confirm. The estimate is a model, the loader refuses cleanly and reports the code, and the machine is an emulator in v1. Hard facts (more than 8 paths, a path clash, a file over 64 KiB) block.
6. **Disk cache of packed files.** Em asked for it. The pack itself is probably milliseconds; the scan of the library costs more. Options: (a) disk cache `out/dkm-cache` keyed like the runtime cache; (b) in-process memo only. Recommendation: (a) because the key function exists and the cache gives W8 a prebuilt-asset source, but measure scan plus pack first (step 4) and drop it if all candidates take under a second.
7. **Where the stale check lives.** Rust would copy three hash rules (C sources, SHARC text after expansion, Rust crate files and toolchain pins). Recommendation: Python, as `pk_build status`, called by the crate as a subprocess. One owner of the hash, tested against `test_pk_current`.
8. **Where the code lives.** Options: in `apps/desktop/src-tauri` next to modules.rs, or a new crate. Recommendation: a new crate `transfer`, so cargo tests need no Tauri, W5 reuses it with another transport, and the app keeps thin commands.
9. **Progress channel.** Polling with a cursor (as `mod_progress`) or a Tauri `Channel`. Recommendation: polling. It matches today's code and the same `Job` serves tests.
10. **Socket tests through `emu:serve`.** A Rust socket client for the control socket would let a CLI and a test drive the install against `emu:serve`. Recommendation: not in v1. The in-process end-to-end test uses the same `Handle` the app uses, and the protocol flow over the socket is already tested by `menu-dkrun-pluck`. Add the socket transport when W5 adds a second transport anyway.
11. **Auto-resume of a paused emulator.** Options: refuse with a message, or resume during the install and restore after. Recommendation: refuse with a message (the current panel has the same need); no hidden state changes.
12. **Install from a repo module that is `fx` or otherwise cannot pack.** Today `digikit/master-gain` and `master-drive` are not loadable at run time. Recommendation: they do not appear (counted under "do not load on this device" with the reason `run-time effects do not exist`), and the Effects group stays empty with that one-line note until the first run-time effect exists.
13. **Repack on full.** Options: automatic "repack and install" when every card file is held, or just an error. Recommendation: offer it as a button on the E_FULL or space-check stop (the job already holds the files); never automatic, because CLEAR is a destructive step on the card.
14. **OS version string.** The panel passes `device` and `version` from `status` into `catalog`; the plan assumes they read `dt2` and `1.17`. Check the exact format first (step 4) and normalise in one place, since a mismatch would hide every module through the `ports` filter.
