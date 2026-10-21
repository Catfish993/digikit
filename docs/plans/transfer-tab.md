# Transfer tab v1 (W3): the thin route (2026-10-21, updated for W3c)

Status: W3a (READ), W3b (this route) and W3c (VERIFY, install without reboot, the Transfer page) are built on lane `tasks-images`. This replaces the first plan (a new Rust crate with its own catalog, build and install code), which Em and the coordinator dropped: the Python tools already do all of it.

## Goal

The desktop workbench gets a sidebar (Emulator, Transfer) and a Transfer page: tick the run-time modules to put on the card and press Install: they are built, uploaded and verified on the card. That is all: **the selection belongs to the device.** The user selects the module in SETTINGS > DIGIKIT > MODULES on the device and applies and restarts there. A Restart button on the page only restarts the emulated device and reports what loaded. Target: the running emulator only (a real device and samples come later: W5, W6, W7).

## The route

```text
TransferPanel (desktop-only TSX)  -->  3 Tauri commands (transfer.rs)  -->  python -m tools.device.dktransfer  --socket <this app's socket>
                                                                              |          |            |
                                                                          dkrun.py   dkupload.py   pk_build + patchkit pack
                                                                          (build, upload, verify, restart pieces)
```

- **`tools/device/dktransfer.py`** (`mise run transfer`) has two commands, both with `--emu` (or `--socket PATH`, `--run-dir DIR`) or `--port NAME`, and `--json`:
  - `list`: the repo's run-time modules that pack for the target's device (INFO), merged with LIST (on card, same sha256), MODULES (loaded), CAPACITY (fit warning) and the build state. Never `core/*`; `test/` modules are marked. Group from the packed header's contracts at 0x38: machine is Machines, fx is Effects, service or api only is Tools. Card files with no repo match are grouped from their READ header (runtime 0.2.4).
  - `install MOD... [--select [--keep PATH...]] [--restart]`: per module in order: build if changed, pack, upload unless the card has the same sha256, VERIFY (the device hashes the file from a fresh card read; a mismatch is an error). Nothing else by default: the device's selection is not touched and `selected` is `[]` in the `done` event. `--select` also sends one SELECT of the given modules plus `--keep` (it replaces the selection); `--restart` implies `--select`, then RESTARTs and checks that MODULES lists the modules (scripts and scenarios). One JSON line per step.
  - `delete PATH...`: one DELETE per path, then LIST to confirm each is gone; a path not on the card is an error before anything is sent. Events `start`, `delete {path}`, `done {deleted}`. The selection is not touched (see DELETE in `digikit-upload.md`).
  - `restart [MOD...]`: RESTART, wait for the boot, report which modules MODULES lists (`loaded`, `missing` for the given ids, and `not_on_card` when the card lost a file). The page gives it no ids: it only reports what loaded.
- **`tools/device/dkrun.py`** gives `build_objects`, `pack_module`, `upload_if_changed` and `restart_and_wait`; `dk run` uses the same functions.
- **`tools/patchkit/pk_build.py`** skips an object whose committed `source_sha256` equals the hash of its sources now ("unchanged"); `--force` rebuilds. The hash is the builders' own (`source_hash`), so `list` shows "needs build" without a toolchain.
- **Packer** (`packages/patchkit/src/dkm.rs`): a module whose `register` records hold a machine record carries the machine contract in its header, so data-only machines (`test/dyn-params`, `test/dyn-xslice`, `test/dyn-dn2`) group as Machines. No file or hash in the repo pinned those headers.
- **Desktop** (`apps/desktop/src-tauri/src/transfer.rs`): `xfer_list` runs `list` and returns its JSON; `xfer_install(modules)` starts `install` (upload and verify only) in the background; `xfer_delete(paths)` starts `delete`; `xfer_restart` starts `restart` with no module list; `xfer_events(since)` returns its JSON lines with a cursor. The tool is pointed at this app's own control socket (the app serves it by default; `Host.socket`). Python comes from the checkout's `.venv`, else `uv run`; the repository root is `digi_control::runtime_patch::find_root`.
- **UI** (`apps/desktop/ui/src/TransferPage.tsx`, state in `transfer-state.ts`, logic in `transfer-core.ts`, node test `apps/web/tests/transfer.test.mjs`): a collapsible left sidebar (Emulator, Transfer); the Transfer page fills the content area while the faceplate stays mounted (hidden), so the emulator, its audio and the snapshot pump run on. "Available" is the left pane and "On device" the right one. "On device": card files with name, version, size, state (loaded, on card, plus the build state) and verified ok or failed; Each row has a tick; the header's Delete button ("Delete 2", then "Confirm delete 2" for about 3 s, no modal) deletes the ticked files. A deleted module that is loaded stays loaded until the restart ("deleted; unloads at restart"). "Available": the repo modules in Machines, Effects ("No run-time effects yet." when empty) and Tools, a "show test modules" toggle, a tick box per row (ticks mean "install these" and start empty), fit warnings. Install (enabled when a repo module is ticked) and Restart buttons; one global progress bar along the bottom (the fill is the overall fraction: uploads by bytes, build and verify as small fixed shares; one line of text: the current step and the percent; red when a step failed). While a file uploads, its row in On device (or a pending row added there) shows a byte bar; the verified chip or a failed chip replaces it when VERIFY returns. `dktransfer install` emits `progress` events (`id`, `sent`, `bytes`), at most about ten per file. After an install the page says: "On the card. Select it in MODULES on the device, then apply and restart." "Add .dkm" and the drop zone stay. The last list stays in module-level state and shows at once on the next open. The shared web `ModulesPanel` and the web upload page are unchanged.

## Behaviour to know

- **Speed:** `pack_for_transfer` reuses `out/patch/transfer/<device>/<name>.dkm` when it is newer than everything a pack reads (the module directory, the patchkit sources, the device files, `sdk/sdk.toml`), and remembers a module that does not pack the same way. A warm `list` takes about 0.2 s (2.8 s before).
- A running emulator is driven in real time. A **paused** emulator is refused with a message (nothing would run the transfer); `--step` lets the tool step a paused daemon (the test rigs).
- The page never changes the device's selection (no SELECT). `install --select` and `--restart` do, for scripts.
- The store only appends; a rebuilt module leaves dead data until a CLEAR. The old panel's Repack is not in the Transfer panel; `mise run dk:store -- --emu clear` and a new install do the same.

## Tests

`tests/digikit/test_dktransfer.py` (header groups, list merge, install plan, install without select, with select, with restart, verify mismatch, restart reports, pack cache, the paused refusal, every committed object matches `source_hash`); `tests/digikit/scenarios/menu-transfer-pluck.toml` (quick, DT2 and DN2: `list`, a plain `install` of `digikit/pluck` on a boot with no module (verified, selected, not loaded), then `restart_device`: MODULES lists it, the machine plays like the patched-in twin, a second plan builds and uploads nothing, READ returns the packed header); `tests/digikit/scenarios/transfer-live-dn2.toml` (the desktop app's setup: a free-running coupled daemon with no card file, `install --restart` and `restart` through the command line); the Rust tests of `transfer.rs`; `apps/web/tests/transfer.test.mjs`; `tsc` and `astro check`.

## Open

- **[O]** The skip-if-unchanged hash does not cover ColdFire C headers or the compiler version: such a change needs `pk_build --force`.
- **[O]** The GUI itself (sidebar, page, progress bar) is built and type-checked but not tried by the lane; the live path (real-time waits, the guest's soft reset, no card file) is run through the command line against a free-running daemon (`transfer-live-dn2`).
- **[O]** The page cannot show what the device has selected (SELECT has no readback); that is the device's MODULES menu.
- **[O]** The version of the loaded firmware is not read: the list uses the device (INFO) and the packer, not the manifest `ports` versions.
