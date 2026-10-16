# core/digikit-menu

Adds **SETTINGS > DIGIKIT** (the ninth row, after SYSTEM). Emulator only: nothing here has run on a device.

- **LOAD MODULES** lists the `.dkm` files of the +Drive module store (`digikit/` of the `plus_drive/` folder,
  [docs/design/digikit-store.md](../../../docs/design/digikit-store.md)), up to 32 table entries. YES ticks a row (`[X]`) or
  unticks it; the last row, LOAD, hands each ticked path to the runtime. Without the runtime it answers NO RUNTIME.
- **ABOUT** shows the version and, when the runtime is present, its ABI, the number of modules loaded and the free bytes.

Firmware: DT2 1.16 and 1.17, DN2 1.11 and 1.12 (the same sites and helpers, found by function hash).

## The interface to the runtime

The module owns a 20-byte table in its bss, `DKRT_API` (`#[no_mangle]`, five 32-bit words). The runtime (`core/runtime`) fills
it at its init, `magic` last, and `requires` this module:

| word | field | |
| --- | --- | --- |
| 0 | `magic` | `0x444b5254` ("DKRT"); zero until the runtime is ready |
| 1 | `abi` | the runtime's interface version |
| 2 | `load` | `int dkrt_load(const char *path)`: 0 = loaded. `path` is the store path, relative to `digikit/`, for example `modules/a.dkm` |
| 3 | `unload` | `int dkrt_unload(const char *path)` (not used by the menu yet) |
| 4 | `status` | `void dkrt_status(struct { u32 abi, state, loaded, free_bytes } *)` (ABOUT reads it; 0 = none) |

Why a table and not a symbol: patchkit refuses an unresolved symbol, so the menu cannot name `dkrt_load` and still work without
the runtime. The direction "the runtime fills a slot the menu owns" keeps the menu usable alone and needs no new linker feature.
`test/digikit-rt-stub` stands in for the runtime in the proof.

## Notes

- The menu uses the firmware's own classes: a `MenuItem` from the firmware's builder, `VerticalMenuView` objects (created on first
  use, kept for ever), `std::function` objects with our own manager and invoker, the view stack push. Findings:
  [28](../../../docs/findings/28-digikit-menu.md). Anchors: `tools/patchkit/pk_menu_rules.py` writes the rules of `module.toml`.
- The read buffers and the view objects come from the firmware heap, so the module RAM is written by this code only.
- Proof: `uv run python -m tools.coldfire.digikit_menu_proof run [--image dn2-1.11] [--regress]`.
