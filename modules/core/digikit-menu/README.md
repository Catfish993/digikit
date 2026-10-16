# core/digikit-menu

Adds **SETTINGS > DIGIKIT** (the ninth row, after SYSTEM). Emulator only: nothing here has run on a device.

- **LOAD MODULES** lists the `.dkm` files of the +Drive module store (`digikit/` of the `plus_drive/` folder,
  [docs/design/digikit-store.md](../../../docs/design/digikit-store.md)), up to 32 of the first 128 table entries. A row reads
  `[X]` when ticked and `[ ]` when not; a fourth character `*` (drawn as a cross) marks a module the runtime has loaded, `L` one it is
  still loading. `[-]` is a module whose header says this runtime or device cannot load it (`runtime_min` above the runtime, or register
  records for the other device); it cannot be ticked. YES ticks or unticks a row; ticks only select. **LOAD** loads every ticked module
  the runtime does not have; **UNLOAD** unloads every ticked module it has. A success unticks the row. A failure keeps the tick, shows a
  popup (the firmware's own message window) with the file and the runtime's result text (`BAD-SHA.DKM: BAD SHA`; a code without a text
  is `ERROR <n>`), and leaves the same two lines below UNLOAD (`BAD SHA`, the file name; `+n` when more failed). While the runtime
  loads asynchronously (`LOADING`, `DkrtStatus::state`) the answer row reads `LOADING...` and a second LOAD or UNLOAD is refused; the end
  is picked up at the next menu action (YES on the answer row asks again).
- **ABOUT** shows the version, the runtime's ABI, the number of modules loaded and the free arena in KB.

Firmware: DT2 1.16 and 1.17, DN2 1.11 and 1.12 (the same sites and helpers, found by function hash).

## The interface to the runtime

The module `requires` `core/runtime` and names four of its exported C functions as undefined symbols; patchkit links them:

| function | |
| --- | --- |
| `int dkrt_load(const char *path)` | 0 = loaded. `path` is the store path relative to `digikit/`, for example `modules/dyn-demo.dkm` |
| `int dkrt_unload(const char *name)` | 0 = unloaded. `name` is the module name from its header (`DkrtModule.name`) |
| `void dkrt_status(DkrtStatus *)` | ABI (`runtime`), `modules` loaded, `arena_free` |
| `int dkrt_module(u32 index, DkrtModule *)` | the loaded modules; `DkrtModule.path` is the store path each was loaded from, which the menu matches against its rows |

`DkrtStatus`, `DkrtModule`, the result codes and the store reader (`store::open`, `path`,
`file`) are in the shared crate `modules/core/runtime/sdk` (`dkrt-sdk`); the menu and the runtime use the same definitions and the
same reader. The menu has its own tables of result texts (`TEXT_RUNTIME`, `TEXT_REGISTRY`, `TEXT_DSP`, one per code range). There is no stand-in runtime.

## Notes

- The menu uses the firmware's own classes: a `MenuItem` from the firmware's builder, `VerticalMenuView` objects (created on first
  use, kept for ever), `std::function` objects with our own manager and invoker, the view stack push. Findings:
  [28](../../../docs/findings/28-digikit-menu.md). Anchors: `tools/patchkit/pk_menu_rules.py` writes the rules of `module.toml`.
- The table buffer (16 KiB) and the view objects come from the firmware heap, so the module RAM is written by this code only. The
  runtime's staging buffer is written by the card DMA, by design (the proof leaves it out of the write-watch).
- Proof: `uv run python -m tools.coldfire.digikit_menu_proof run [--image dn2-1.11] [--regress] [--only digikit_gate]`.
