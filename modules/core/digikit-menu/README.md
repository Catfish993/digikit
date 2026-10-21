# core/digikit-menu

Adds **SETTINGS > DIGIKIT** (the ninth row, after SYSTEM). Emulator only: nothing here has run on a device.

- **MODULES** lists the `.dkm` files of the +Drive module store (`digikit/` of the `plus_drive/` folder,
  [docs/design/digikit-store.md](../../../docs/design/digikit-store.md)), up to 32 of the first 128 table entries. A row reads `[X]` when it is in
  the pending selection and `[ ]` when not (the pending selection starts as the saved one; ticking only edits it); a fourth character `*`
  (drawn as a cross) marks a module this boot loaded, `L` one whose DSP part is still going. `[-]` is a module whose header says this runtime
  or device cannot load it; it cannot be ticked. **APPLY & RESTART** appears only when the pending selection differs from the saved one (or this
  boot is in safe mode): the first press shows a popup that the device restarts, the second saves the selection (`dkrt_selection_set`) and
  restarts (`dkrt_restart`); a refusal is shown. **CLEAR SELECTION** unticks all. In safe mode the DIGIKIT view and the list show `SAFE: <reason>`
  and a popup says why. Modules are loaded by the runtime at boot, not by the menu.
- **HEALTH** shows CPU load, the DSP's block time (average and maximum of the last second), the free firmware heap and its largest block, the free runtime arena and
  the free DSP area, and refreshes itself about twice a second without a key press (the page registers a function with `dkrt_poll_set`, which the UI loop calls
  once per pass; it stops when the page is closed). The figures need `core/health`; without it the page says so. Finding 41.
- **MEMORY** shows the runtime's capacity (the CAPACITY command's entries, `dkrt_capacity`) as text rows with a ten-character bar: DSP area and arena as a percent, modules, machine, voice, parameter and page rows as used/limit. Finding 57.
- **ABOUT** shows the version, the runtime's ABI, safe mode and why, this boot's modules with each result text, and the free arena in KB.

Firmware: DT2 1.16 and 1.17, DN2 1.11 and 1.12 (the same sites and helpers, found by function hash).

## The interface to the runtime

The module `requires` `core/runtime` and names six of its exported C functions as undefined symbols; patchkit links them:

| function | |
| --- | --- |
| `int dkrt_selection_get(DkrtSelection *)` | the saved selection: `count` store paths (for example `modules/dyn-demo.dkm`) |
| `int dkrt_selection_set(const DkrtSelection *)` | save it (two copies in the store range); 0 or an `E_SEL_*` code; effective at the next boot |
| `int dkrt_restart(void)` | soft reset; returns only when it refuses |
| `void dkrt_boot_info(DkrtBootInfo *)` | safe mode and why, the modules selected at this boot and each one's result |
| `void dkrt_status(DkrtStatus *)` | ABI (`runtime`), device, `arena_free` |
| `int dkrt_module(u32 index, DkrtModule *)` | the modules this boot loaded; `DkrtModule.path` is the store path, which the menu matches against its rows |

`DkrtStatus`, `DkrtModule`, the result codes and the store reader (`store::open`, `path`,
`file`) are in the shared crate `modules/core/runtime/sdk` (`dkrt-sdk`); the menu and the runtime use the same definitions and the
same reader. The menu has its own tables of result texts (`TEXT_RUNTIME`, `TEXT_REGISTRY`, `TEXT_DSP`, one per code range). There is no stand-in runtime.

## Notes

- The menu uses the firmware's own classes: a `MenuItem` from the firmware's builder, `VerticalMenuView` objects (created on first
  use, kept for ever), `std::function` objects with our own manager and invoker, the view stack push. Findings:
  [28](../../../docs/findings/28-digikit-menu.md), [34](../../../docs/findings/34-digikit-robustness.md), [38](../../../docs/findings/38-digikit-menu-restart.md). Anchors: `tools/patchkit/pk_menu_rules.py` writes the rules of `module.toml`.
- The table buffer (16 KiB) and the view objects come from the firmware heap, so the module RAM is written by this code only. The
  runtime's staging buffer is written by the card DMA, by design (the proof leaves it out of the write-watch).
- Proof: `uv run python -m tools.coldfire.digikit_menu_proof run [--image dn2-1.11] [--regress] [--quick] [--only-boot]`.
