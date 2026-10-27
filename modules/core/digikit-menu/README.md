# core/digikit-menu

Adds **SETTINGS > DIGIKIT** (the ninth row, after SYSTEM). Emulator only: nothing here has run on a device. Rows: MODULES, FX ROUTES, ABOUT, HEALTH, MEMORY, DEBUG, REMOTE.

- **MODULES** lists the `.dkm` files of the +Drive module store (`digikit/` of the `plus_drive/` folder,
  [docs/design/digikit-store.md](../../../docs/design/digikit-store.md)), up to 32 of the first 128 table entries. A row reads `[X]` when it is in
  the pending selection and `[ ]` when not (the pending selection starts as the saved one; ticking only edits it); a fourth character `*`
  (drawn as a cross) marks a module this boot loaded, `L` one whose DSP part is still going, `x` one that will not fit next to the ticked ones. The row ends with the memory the
  module takes (arena and DSP area, from the needs inferred from its contents, docs/design/digikit-dkm.md, "Needs are inferred": `dkneeds::needs_of` reads the header, the `register` records and the DSP section's header by store sectors, once per open of the menu, and stages nothing): `[ ]x HUGE      321K`; the name is the file name without `.dkm`, cut to 8 characters
  (the list shows about 18). The next boot loads the ticked modules into an empty runtime, so fit is checked against the limits with the ticked modules taken out, in list
  order (`dkrt_fit`, docs/plans/what-fits.md); selecting a row that will not fit ticks nothing and shows a popup `NO FIT: DSP 164K > 155K FREE` and the short form in the
  message row. The names in these texts are the page's own: `CPU` (the arena: CPU memory on the desktop), `DSP` (the DSP area: DSP memory), `MACHINES`, `PARAMS`, `PAGES`.
  **The load line:** while at least one module is ticked, one line directly above APPLY & RESTART (or where it would be) gives the DSP and the CPU memory of the ticked set (always these two), `DSP 85% CPU 1%`
  (`dkrt_fit` `load_line`: the limits with the ticked modules taken out, the same capacity the `x` marks use; labels `DSP`, `CPU`; percents rounded up, so a set that takes
  anything never reads 0%; at most 17 characters). It changes with every tick and goes with the last one. `[-]` is a module whose header says this runtime
  or device cannot load it; it cannot be ticked. **APPLY & RESTART** appears only when the pending selection differs from the saved one (or this
  boot is in safe mode): the first press shows a popup that the device restarts, the second saves the selection (`dkrt_selection_set`) and
  restarts (`dkrt_restart`); a refusal is shown. **CLEAR SELECTION** unticks all. In safe mode the DIGIKIT view and the list show `SAFE: <reason>`
  and a popup says why. Modules are loaded by the runtime at boot, not by the menu.
- **FX ROUTES** (after MODULES, FX routing step 4, Digitakt II; the page is on both devices) lists the loaded effects that have a page of their own, one row each in the order of the effect chain: `<TAB>  SEND` or `<TAB>  MASTER` (the effect's 3-character tab and the route
  it will have), for example `GAN  MASTER`, `GSN  SEND`. YES on a row switches the **pending** route between the routes the effect allows (its manifest's `routes`); nothing is saved yet. An effect that allows one route says so in the message row below the rows (the line
  is cleared by the next key): `GAN: MASTER ONLY`, `GSO: SEND ONLY`. A switch to SEND is refused with `NO SEND SLOT` when the pending routes already use every send slot (CAPACITY `SEND_SLOTS`, counted over the pending routes, so moving one send effect to the master frees its slot
  for another); nothing changes. When the pending routes differ from the saved ones (the route each effect has in the settings now: the stored one when the effect allows it, else its default) the row **APPLY & RESTART** appears, like MODULES: the first press shows a popup (`DEVICE WILL RESTART`) and the row reads
  CONFIRM RESTART; the second writes the changed routes (`dkrt_settings_put` of `fx/route/<module id>`, one byte, 1 send, 0 master), writes the settings to the card at once (`dkrt_settings_flush`; otherwise they would wait 2 s) and restarts (`dkrt_restart`); a refusal is shown (`NOT SAVED: ...`,
  `NO RESTART: ...`). The route takes effect at the restart (the FX key's pages are fixed when the main view is built). With no effect loaded the page says `NO EFFECTS LOADED`. A row is at most 11 characters (about 18 fit); the longest message is `GAN: MASTER ONLY`, the popup line `DEVICE WILL RESTART`.
  An effect without a page (no `tab` in its manifest) is not listed: it is an insert. Two effects of one module share the module's setting. Finding 65.
- **HEALTH** shows CPU load, the DSP's block time (average and maximum of the last second), the free firmware heap and its largest block, the free CPU memory (the runtime arena) and
  the free DSP area, and refreshes itself about twice a second without a key press (the page registers a function with `dkrt_poll_set`, which the UI loop calls
  once per pass; it stops when the page is closed). The figures need `core/health`; without it the page says so. Finding 41.
- **MEMORY** shows the runtime's capacity (the CAPACITY command's entries, `dkrt_capacity`) as text rows with a ten-character bar: `DSP` and `CPU` (the arena) as a percent, `MODS`, `MACH`, `VOICE`, `PARAM` and `PAGE` as used/limit. Finding 57.
- **ABOUT** shows the version, the runtime's ABI, safe mode and why, this boot's modules with each result text, and the free arena in KB.

The DIGIKIT rows and pages draw no glyph (the SETTINGS row keeps its own), and the ABOUT, HEALTH and MEMORY pages show no highlighted row (finding 28, "Glyphs and text pages"). MODULES keeps its cursor bar.

Firmware: DT2 1.17 and DN2 1.12 (the same sites and helpers, found by function hash).

## The interface to the runtime

The module `requires` `core/runtime` and names its exported C functions as undefined symbols; patchkit links them (the table lists the first six; the later ones are in the code: `dkrt_fx`, `dkrt_settings_put`, `dkrt_settings_flush` for FX ROUTES, `dkrt_capacity`, `dkrt_health`, `dkrt_diag`, `dkrt_store_*` and `dkrt_remote_*` for the other pages):

| function | |
| --- | --- |
| `int dkrt_selection_get(DkrtSelection *)` | the saved selection: `count` store paths (for example `modules/dyn-demo.dkm`) |
| `int dkrt_selection_set(const DkrtSelection *)` | save it (two copies in the store range); 0 or an `E_SEL_*` code; effective at the next boot |
| `int dkrt_restart(void)` | soft reset; returns only when it refuses |
| `void dkrt_boot_info(DkrtBootInfo *)` | safe mode and why, the modules selected at this boot and each one's result |
| `void dkrt_status(DkrtStatus *)` | ABI (`runtime`), device, `arena_free` |
| `int dkrt_module(u32 index, DkrtModule *)` | the modules this boot loaded; `DkrtModule.path` is the store path, which the menu matches against its rows |
| `int dkrt_fx(u32 index, DkrtFx *)` | the loaded effects that have a page: tab, module id, allowed routes, default, the route in force and the route the settings give (FX ROUTES) |

`DkrtStatus`, `DkrtModule`, the result codes and the store reader (`store::open`, `path`,
`file`) are in the shared crate `modules/core/runtime/sdk` (`dkrt-sdk`); the menu and the runtime use the same definitions and the
same reader. The menu has its own tables of result texts (`TEXT_RUNTIME`, `TEXT_REGISTRY`, `TEXT_DSP`, one per code range). There is no stand-in runtime.

## Notes

- The menu uses the firmware's own classes: a `MenuItem` from the firmware's builder, `VerticalMenuView` objects (created on first
  use, kept for ever), `std::function` objects with our own manager and invoker, the view stack push. Findings:
  [28](../../../docs/findings/28-digikit-menu.md), [34](../../../docs/findings/34-digikit-robustness.md), [38](../../../docs/findings/38-digikit-menu-restart.md). Anchors: the rules, signatures and struct layouts are declared in `devices/fw/` (`menu`, `string`, `view`, `mem`). The Rust crate calls
  the firmware through the generated crate `dkfw` (`modules/core/fw/rs`, written by `patchkit gen`), so its undefined symbols are the anchor
  names and `module.toml` has no `[object.externs]` for the Rust object; struct offsets are `offset_of!` on the generated structs. The assembly
  shim (`src/cf/shim.s`) still has its own externs table.
- The table buffer (16 KiB) and the view objects come from the firmware heap, so the module RAM is written by this code only. The
  runtime's staging buffer is written by the card DMA, by design (the proof leaves it out of the write-watch).
- Proof: `uv run python -m tools.coldfire.digikit_menu_proof run [--image dn2-1.12] [--regress] [--quick] [--only-boot]`.
