# Transfer tab v1 (W3): the thin route (2026-10-21)

Status: W3a (READ) and W3b (this route) are built on lane `tasks-images`; Em has not tried the GUI yet. This replaces the first plan (a new Rust crate with its own catalog, build and install code), which Em and the coordinator dropped: the Python tools already do all of it.

## Goal

The desktop workbench's Modules popup becomes a Transfer popup: tick the run-time modules that should run on the emulator, press Install, and they are built, uploaded, selected and loaded without a manual APPLY & RESTART. Target: the running emulator only (a real device and samples come later: W5, W6, W7).

## The route

```text
TransferPanel (desktop-only TSX)  -->  3 Tauri commands (transfer.rs)  -->  python -m tools.device.dktransfer  --socket <this app's socket>
                                                                              |          |            |
                                                                          dkrun.py   dkupload.py   pk_build + patchkit pack
                                                                          (build, upload, select, restart pieces)
```

- **`tools/device/dktransfer.py`** (`mise run transfer`) has two commands, both with `--emu` (or `--socket PATH`, `--run-dir DIR`) or `--port NAME`, and `--json`:
  - `list`: the repo's run-time modules that pack for the target's device (INFO), merged with LIST (on card, same sha256), MODULES (loaded), CAPACITY (fit warning) and the build state. Never `core/*`; `test/` modules are marked. Group from the packed header's contracts at 0x38: machine is Machines, fx is Effects, service or api only is Tools. Card files with no repo match are grouped from their READ header (runtime 0.2.4).
  - `install MOD... [--keep PATH...]`: builds only the modules whose objects changed, packs, uploads what the card lacks, SELECTs exactly the given set (plus `--keep`), RESTARTs, waits until MODULES lists them. One JSON line per step.
- **`tools/device/dkrun.py`** gives `build_objects`, `pack_module`, `upload_if_changed` and `restart_and_wait`; `dk run` uses the same functions.
- **`tools/patchkit/pk_build.py`** skips an object whose committed `source_sha256` equals the hash of its sources now ("unchanged"); `--force` rebuilds. The hash is the builders' own (`source_hash`), so `list` shows "needs build" without a toolchain.
- **Packer** (`packages/patchkit/src/dkm.rs`): a module whose `register` records hold a machine record carries the machine contract in its header, so data-only machines (`test/dyn-params`, `test/dyn-xslice`, `test/dyn-dn2`) group as Machines. No file or hash in the repo pinned those headers.
- **Desktop** (`apps/desktop/src-tauri/src/transfer.rs`): `xfer_list` runs `list` and returns its JSON; `xfer_install` starts `install` in the background; `xfer_events(since)` returns its JSON lines with a cursor. The tool is pointed at this app's own control socket (the app serves it by default; `Host.socket`). Python comes from the checkout's `.venv`, else `uv run`; the repository root is `digi_control::runtime_patch::find_root`.
- **UI** (`apps/desktop/ui/src/TransferPanel.tsx`, logic in `transfer-core.ts`, node test `apps/web/tests/transfer.test.mjs`): groups Machines, Effects ("No run-time effects yet." when empty) and Tools; a "show test modules" toggle; a tick box per row (the first ticks are what is loaded); name, version, size, state and fit warning; Install with a progress log; "Add .dkm" and the drop zone, smaller; Delete for card files the repo does not know. The shared web `ModulesPanel` and the web upload page are unchanged.

## Behaviour to know

- A running emulator is driven in real time. A **paused** emulator is refused with a message (nothing would run the transfer); `--step` lets the tool step a paused daemon (the test rigs).
- The selection is replaced by the ticked set: unticking a loaded module removes it from the next boot. Loaded modules the repo does not know are kept only when ticked.
- The store only appends; a rebuilt module leaves dead data until a CLEAR. The old panel's Repack is not in the Transfer panel; `mise run dk:store -- --emu clear` and a new install do the same.

## Tests

`tests/digikit/test_dktransfer.py` (header groups, list merge, install plan and sequence with a mock uploader, the paused refusal, every committed object matches `source_hash`); `tests/digikit/scenarios/menu-transfer-pluck.toml` (quick, DT2 and DN2: `list`, `install` of `digikit/pluck` on a boot with no module, MODULES lists it, the machine plays like the patched-in twin, a second plan builds and uploads nothing, READ returns the packed header); the Rust tests of `transfer.rs`; `apps/web/tests/transfer.test.mjs`; `tsc` and `astro check`.

## Open

- **[O]** The skip-if-unchanged hash does not cover ColdFire C headers or the compiler version: such a change needs `pk_build --force`.
- **[O]** The GUI flow (a live emulator, real-time waits, the guest's soft reset) is tried only through the scenario rigs (stepped daemons); the live path is not run against the desktop app by the lane.
- **[O]** The version of the loaded firmware is not read: the list uses the device (INFO) and the packer, not the manifest `ports` versions.
