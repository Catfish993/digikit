# 28. digikit runtime: the SETTINGS > DIGIKIT menu (Phase 4)

**Update 2026-10-16 (lane r1, second pass): the menu now calls the real runtime and loads and unloads from the list; see "Joined to the runtime" at the end. The sections below describe the first pass, where a stand-in runtime and the table `DKRT_API` were used; that table and `test/digikit-rt-stub` no longer exist [C].**

**Update 2026-10-17 (lane r1, third pass; finding 34): the list now has a separate UNLOAD row, an error popup made with the firmware's own
PopupWindow, the file name in the failure text, a text for every result code (unknown ones as `ERROR <n>`), a loading state for
asynchronous loads, and greyed `[-]` rows for modules built for the other device or a newer runtime. Ticks only select: LOAD loads, UNLOAD unloads,
a loaded module no longer starts ticked [C]. The stack question of "Still open" is answered: the UI task has 160 KiB and a load used 3.3 KiB (finding 34).**

Plan: `docs/plans/digikit-runtime.md` Phase 4. Lane r1, 2026-10-16, native emulator (`regions_probe`), module
`modules/core/digikit-menu` (Rust through rustc_codegen_gcc, finding 26). Everything here is **[D]**: measured or read once; no
second agent has checked the addresses against the image bytes. Nothing ran on a device. Addresses are DT2 1.16 unless stated;
the same functions are found by hash on DT2 1.17, DN2 1.11 and DN2 1.12 (table at the end).

## Result

**[D]** SETTINGS gets a ninth row, DIGIKIT. It opens a stock `VerticalMenuView` with LOAD MODULES and ABOUT. LOAD MODULES reads the
store header and table with the firmware's block read and shows one row per `.dkm` entry (`[ ] A.DKM`), YES toggles it, and the
last row, LOAD, calls the runtime for each ticked path (`LOADED 2 FAILED 0`) or answers `NO RUNTIME`. ABOUT shows `DIGIKIT 0.1.0`
and `RUNTIME: NONE`, or ABI, loaded count and free KB from the runtime's status call. All four images (DT2 1.16, 1.17, DN2 1.11, 1.12): 5 runs each, all
pass (frames pinned by SHA-256 in `tests/golden/digikit_menu_frames.json`, module state read back, write-watch 0 events with
`ALLOW_PC` = the module, flush check 0 hits). The stock SETTINGS scripts of finding 25 (10 on DT2 1.16, 9 on DN2 1.11: no CHECK FILESYSTEM) give the same frames and the
same card writes on the patched and the stock image (`--regress`; not run on 1.17 and 1.12). PNGs: `out/runs/lanes/r1-shots/` (local).

## What is hooked **[D]**

- The SETTINGS item list is rebuilt by `FUN_400a59a0` (finding 23, S2). The hook is **not** at the tail (`0x400a5d58`) but 16 bytes
  earlier, at `0x400a5d48` (function + 0x3a8): `movea.l (a2),a0; pea (2).w; move.l d4,-(sp)`, the restore of the saved selection
  (`vslot 0x1c(this, sel, 2)`). The hook replaces the first two instructions with a `jmp.l` to our shim, which calls
  `dk_add_item(this)` (a2 = this, d4 = selection, both kept: our Rust code is callee-saved) and `jmp`s to the tail where the two
  displaced instructions run, then back into the rebuild. Reason: the restore sees the DIGIKIT row, so a rebuild with DIGIKIT
  selected (the Project observer rebuilds) keeps the cursor on it. All four images have the same instruction stream in this function
  (identical masked hash), so one rule serves them.
- Anchors (`tools/patchkit/pk_menu_rules.py`, `cf_func` rules by function hash; unique and equal on all four images): the hook site,
  operator new `FUN_4011e674`, item builder `FUN_401158b0`, `VerticalMenuView` constructor `0x40119c8c`, the shared_ptr helper
  `0x40119dda`, push `0x4011a2b2`, string constructor and destructor `0x401e7a64`/`0x401e5fb4`, block read `0x4012deda`,
  `View::invalidate` `0x4011ae46`, and the `pea (icon).l` instruction of the SYSTEM row (`cf.menu.icon_ref`).
- The icon is an object built at run time (bss, 0x44f35854), so `cf_ptr` cannot anchor it (the literal is not an address in the
  image). The anchor is the 6-byte instruction; the module reads its operand (two `u16`) at run time and uses the SYSTEM icon for
  every row and every title.

## ABI details learned **[D]**

- **`std::function<..>`** is 16 bytes: functor storage at +0 (8 bytes), manager at +8 (0 = empty), invoker at +0xc. The firmware's own
  lambdas keep a pointer to a heap cell holding the captured `this`. Ours keep two words inline (action code, context); our manager
  `(dest, src, op) -> bool` does what `FUN_400a537c` does for the other ops: 0 type info (a dummy), 1 `*dest = src`, 2 clone (copy
  the two words), 3 destroy (nothing). The firmware copies with the manager's clone (op 2) and destroys through `FUN_40197a04`
  (manager op 3). The action invoker is `void (const _Any_data *)`.
- **`MenuItem`** (0x54 bytes): `FUN_401158b0(item, label cstr, &action, icon, &fn4)` builds the label as a function that draws the
  text (`FUN_401155b6`: a COW string held in a heap cell; the invoker takes four drawing arguments and calls `FUN_40113e88`), copies
  the action to +0x1c and builds the icon function (`FUN_401156dc`) from the icon object. It copies everything it is given; the
  caller destroys its own temporaries (stock order: fn4, then action). `icon` must point to a real icon object (null would be copied
  from); `fn4` may be an empty function. The constructor is `MenuItem(this, labelfn, actionfn, iconfn, fn4, -1, 8)`.
- **Who calls the action:** `VerticalMenuView::vfunc_34` (`0x4011a380`, the key handler) on YES (code 10) or an encoder push
  (0x31): it fetches the selected item as a `shared_ptr` (slot +0x10, hidden result pointer in a0), calls `FUN_40115550`, which calls
  the item's invoker at +0x28 with `&item+0x1c`, then destroys the pointer. LEFT/RIGHT call fn4 (`FUN_4011556a`).
- **`VerticalMenuView`**: `new(0x1ac)`, then `FUN_40119c8c(this, name cstr, reserve 8, const std::string *title, icon)`; the name is
  kept as a pointer (use a constant). The View sub-object is at +0x38. The title is drawn with a limited width: `LOAD MODULES`
  was cut to `LOAD MODUL`, so the view is titled `MODULES`. The icon word is stored as a pointer (+0xa4) and drawn at the bottom left.
- **`FUN_40119dda`** (A0 = out, stack: parent, view): builds a `shared_ptr<View>` from the raw `View*` (count 1, control block 0x10
  bytes), and registers a function in the view (View slot +0x40) that captures `parent`: when the view is popped it clears the
  child pointer `parent+0x90`. The parent must be the object that pushes the view. **`FUN_4011a2b2(this, &sp)`** stores a copy of
  `sp` in `this+0x90` (`FUN_401d1a76`), bumps the use count and pushes it onto the manager at `this+0x64`.
- **Lifetime:** we never destroy a view or its pointer. The `shared_ptr` cell (8 bytes) is a firmware heap block held by the module,
  so the count never reaches zero; a push copies the pointer and a pop drops that copy. The spare member slots `this+0x1f8` and
  `+0x208` of the settings menu were not needed. The `MenuItem`s are owned by the menu; a rebuild (clear, slot +0x14) destroys
  them, and `fn_manager`'s destroy is a no-op, so nothing of ours is freed twice. The settings menu object lives as long as the main
  screen (it is created once), so the actions' captured parent stays valid **[O]**: not checked past a reset.
- **Rebuild from inside an action** is the firmware's own pattern (`KitVoiceMuteConfigMenuView`'s toggle calls its rebuild
  `FUN_4007c974`, which is selection, clear, add, restore selection). We do the same for the ticks and the LOAD answer.
- **Redraw:** the view manager redraws only when a view was invalidated (`FUN_4011b9b6`, the render of the main UI, tests a dirty
  byte at +0x20 of the manager). The key handler does not invalidate after YES, so a rebuild alone leaves the old rows on the
  screen until the next key (measured: 17 draws of a script with 7 YES and 6 moves, the YES presses drew nothing). Fix: call
  `View::invalidate(view sub-object)` (`FUN_4011ae46`, 322 callers) after the rebuild. The ticks then show at once.
- **Calling Rust from the firmware** works: `extern "C"` functions with integer and pointer arguments, called through function
  pointers stored in `std::function` objects and through a `jmp` shim. The only assembly is the hook shim and the a0 adapter for
  `FUN_40119dda`.

## The runtime table (the "weak" mechanism), first pass, replaced **[C]**

`DKRT_API` in the module's bss: `magic, abi, load, unload, status` (5 words, README of the module). The runtime fills it, `magic`
last. patchkit has no optional symbol, so the menu cannot name `dkrt_load` and still patch without the runtime; a slot the menu owns
needs no linker change and the runtime `requires` the menu. The proof uses `test/digikit-rt-stub` (a `dkrt_load` that logs the
first 16 bytes of each path, a status call) and pokes the table from the host (`regions_probe --poke`): LOAD with a, b ticked and
then b only gave 3 calls, `modules/a.dkm`, `modules/b.dkm`, `modules/b.dkm`, and the ABOUT screen showed `RUNTIME ABI 1`,
`LOADED 0`, `FREE 72 KB`. **[O]** `DkrtStatus` is my reading (`abi, state, loaded, free_bytes`); the runtime lane (r4) must agree
or the menu changes.

## Proof and instruments **[D]**

- `uv run python -m tools.coldfire.digikit_menu_proof run [--image dn2-1.11] [--update] [--regress]` patches, builds the card
  (`digikit/modules/a.dkm`, `b.dkm`, `readme.txt`), boots, runs `panel_script` (`digikit_main`, `digikit_load`, `digikit_about`,
  `digikit_load_script(pokes)`), compares pinned frames, reads `NENT`, `STORE`, `TICK`, `RESULT` and the stand-in's log back with
  `--peek`, and with `--regress` runs the finding 25 scripts stock against patched.
- `tools/coldfire/arena_safety.py run/suite` take `--syx` and `--plan` for a patched image. **The canary fill is switched off for a
  patched image**: it would overwrite the module's code and data in the region (first run: illegal instruction at the first
  rebuild, stacked PC inside the module's buffer). `ALLOW_PC` is the range the patch placed in `cf.expansion` (`plan_allow_pc`).
- First run with the watch on showed 3 write events at the firmware's shared_ptr helper writing into the module's bss and 2 DMA
  events from the card read into the module's buffers. Both are by design but make the watch noisy, so the pointer cell and the two
  read buffers are firmware heap blocks; then 0 events.
- `regions_probe` got `--poke T:ADDR:VALUE`, a fault report (registers and 12 stack words) and valid JSON for its error line.
- Harness: matrix cells `dt2-1.16`, `dt2-1.17`, `dn2-1.12` with `[core/digikit-menu]`, quick test `menu` (the frames of the DIGIKIT
  row, the view, MODULES showing NO STORE on the harness card, ABOUT); the whole smoke tour (save and reload, stress) also passes.

## Ports

| image | rebuild | hook site | proof |
| --- | --- | --- | --- |
| DT2 1.16 | `0x400a59a0` | `0x400a5d48` | 5 runs, regression 10 scripts |
| DN2 1.11 | `0x4009fa7c` | `0x4009fe24` | 5 runs, regression 9 scripts |
| DT2 1.17 | `0x400a5b04` | `0x400a5eac` | 5 runs, harness cell (smoke tour and `menu`) |
| DN2 1.12 | `0x4009fbe0` | `0x4009ff88` | 5 runs, harness cell (smoke tour and `menu`) |

## Open

- **[O]** Hardware: the module code is in place before the caches are on (core/expansion), the buffers are cacheable heap read by
  DMA through the firmware's own block read (bounce buffer when unaligned); the emulator is coherent and the flush check saw no hit.
- **[O]** The OUTBOX 8 row is conditional (finding 23): with it visible DIGIKIT is the tenth row; the scripts assume 8 DOWN.
- **[O]** More than 32 table entries are ignored; names longer than 17 characters show their last 17; non-`.dkm` entries are skipped.
  The icon of every row and title is the SYSTEM one **[C]** (2026-10-21: no glyph on the DIGIKIT rows and pages any more; see "Glyphs and text pages").
- **[O]** After LOAD the last row shows the answer text and still loads when pressed again.
- **[O]** `dkrt_load` runs on the UI task and blocks it for the duration of the load.

## Joined to the runtime (Phase 4 gate) **[D]**

Lane r1, 2026-10-16, native emulator, all four images. Everything below is **[D]**: measured here once.

- **The menu calls the runtime by symbol.** `core/digikit-menu` has `requires = ["core/runtime"]` and its crate declares
  `dkrt_load`, `dkrt_unload`, `dkrt_status` and `dkrt_module` as `extern "C"`; patchkit links them to the runtime's exports. The table
  `DKRT_API` and `modules/test/digikit-rt-stub` are gone. The layouts `DkrtStatus` and `DkrtModule` are the runtime's, from the
  shared crate `dkrt-sdk` (the first pass's four-word `DkrtStatus` was a guess and is dropped). `DkrtModule` gained `path` (64
  bytes, the store path the module was loaded from; the runtime keeps it in `DKRT_PATHS`, not in `State`).
- **One store reader.** The header, table CRC, bounds and entry parsing are `dkrt_sdk::store` (no_std; the caller passes the
  firmware's block read and a 16-byte aligned buffer). The runtime reads into its staging buffer, the menu into a 16 KiB heap block.
  Changes in behaviour: the menu now checks the table CRC too (it did not), and it shows `BAD STORE` for a store that fails a check
  or has a table over 16 KiB (more than 128 entries).
- **Rows and actions** (first version of this pass; replaced [C]: see finding 34, ticks no longer unload). `[X]` ticked, `[ ]` not, and `*` (the OLED draws it as a cross) in the fourth character when the runtime
  has that file loaded; a loaded module starts ticked, so unticking it and pressing LOAD unloads it. LOAD loads each ticked row that
  is not loaded and unloads each unticked row that is; the answer row reads `LOADED n`, `UNLOADED n`, `LOADED n UNLOAD m`,
  `NOTHING TO DO`, or the runtime's result text for the first failure plus `+n` (`BAD SHA`, `NO IMPORT`, ...; table in `dkrt-sdk`
  `result_text`). Failed rows stay ticked and unmarked. The row is about 17 characters wide, so the text carries no file name.
  There is no separate UNLOAD item. ABOUT: `RUNTIME ABI 1`, `LOADED n`, `FREE 511 KB`.
- **Cursor** of a reused view stays where it was on re-entry (the second visit to MODULES opens with the cursor on LOAD).
- **Compiler trap.** A dense `match` (here `result_text`) compiled to a PC-relative jump table inline in `.text` (`movew
  pc@(..,d1:l:2)`, `jmp pc@(..)`), which is 68020 code, not ColdFire; `pk_build` refused the object. The function is now a table lookup.
- **Gate** (`uv run python -m tools.coldfire.digikit_menu_proof run --image IMAGE`): a card from a `plus_drive`-style folder with
  `digikit/modules/dyn-demo.dkm` (packed by `patchkit pack`), `bad-sha.dkm`, `bad-import.dkm`. `digikit_gate`: SETTINGS > DIGIKIT >
  LOAD MODULES, tick dyn-demo, LOAD (answer `LOADED 1`, the row `[X]*`), the demo's counter read by the host at three times
  (`regions_probe --peek-at T:ADDR:OFF`, new): magic `DYNM`, then 14, 37, 60 on DT2 1.16 and rising on every image; ABOUT `LOADED 1`;
  back to the list: the row is marked loaded; untick, LOAD (`UNLOADED 1`), the counter 152 at two later times and the marker `fini`
  leaves (0xF1F10001) in the freed memory; ABOUT `LOADED 0`. `digikit_errors`: two broken modules ticked, LOAD answers `NO IMPORT +1`,
  then one unticked: `BAD SHA`. All four images, 4 scripts each: pass, frames pinned in `tests/golden/digikit_menu_frames.json`,
  `DKRT_STATE` `loads` = 1 and slot 0 empty at the end. The write-watch covers all of `cf.expansion` except the runtime's staging
  buffer (the card read lands there by DMA, by design: 3 events per list read before it was excluded) with `ALLOW_PC` = the patch's
  code range (menu, runtime, arena): 0 events; the flush check: 0 hits. The stock SETTINGS scripts of finding 25 on DT2 1.16 (10) and
  DN2 1.11 (9) give the same frames and card writes stock and patched, writes 0, flush 0. PNGs: `out/runs/lanes/r1-shots/`.
- **Smoke harness.** The cells `dt2-1.16`, `dt2-1.17`, `dn2-1.12` with `[core/digikit-menu]` now install the runtime too (requires);
  the harness card has no store, so MODULES still shows NO STORE; only the ABOUT frame of the `menu` test changed.

- **Later pages.** HEALTH (finding 41) and MEMORY (finding 57, the runtime's CAPACITY entries as text rows and bars) were added after this finding; DEBUG toggles the debug log (finding 46).

### Still open
- **[O]** Not on hardware. The staging buffer, the arena and the menu's heap block are the only memory the load touches; the flush
  check is strong on DT2, weak on DN2 (finding 25).
- **[O]** `dkrt_load` runs inside a menu action on the UI task, so the display freezes for the load (about 4 ms of guest time for
  dyn-demo, more with a real card); the stack use of the nested calls (menu, runtime about 1 KiB) is not measured [C]: measured in finding 34, 3256 bytes of the UI task's 160 KiB inside a request.
- **[O]** More than 32 `.dkm` entries, names longer than 17 characters (their last 17 are shown), and a store with more than 128
  entries are not handled (`BAD STORE`).
- **[O]** A file renamed in the store but loaded earlier: the match is by store path, so the row of the new name is unmarked.

## Glyphs and text pages (2026-10-21, W2c) **[D]**

Read once from the decompile and run in the emulator (DT2 1.17 and DN2 1.12); no second agent has checked the addresses. DN2 1.12 addresses in brackets.

**Where the glyphs come from.**
- Every MenuItem has an icon `std::function` at `+0x2c` (manager `+0x34`, invoker `+0x38`). The row paint draws it through `FUN_40115808` [`0x4011717c`]:
  `if (item[0x34] != 0) item[0x38](item+0x2c, item, canvas, 0x2a, y-2)`. An empty function draws nothing, and the destructor `FUN_40197ce0`
  [`0x40188366`] and the MenuItem constructor `0x40115a44` [`0x401173b8`] accept one (the constructor copies the icon function only when its manager is not 0).
- The stock item builder `FUN_40115b90` [`0x40117504`] always builds a non-empty icon function from a `Bitmap*` (`FUN_401159bc` copies the bitmap, so null would
  crash). All 30 [28] stock call sites pass a real icon object (bss `0x44f3....`), each row its own (RENAME `0x44f34354`, SYSTEM `0x44f35854`). Stock items without a
  glyph are built with the MenuItem constructor and an empty icon function: `SamplePicker::vfunc_41`, call `0x4001d6aa` (the icon function and the fourth
  function are temporaries with `clr.l` on their managers).
- The page's glyph at the bottom left is the view constructor's `icon` argument (`VerticalMenuView` ctor `0x40119f6c` [`0x4011b8e0`], stored at `this+0xa4`):
  `VerticalMenuView::vfunc_31` `0x4011a234` [`0x4011bba8`] draws it only `if (this[0x29] != 0)`, at (1, 1). Stock passes null in `TotalRecallMenuView`
  (`0x4010dd7a` [`0x4010f6ee`], `clr.l` as the icon).

**What the module does now.** `ensure_view` passes null as the view icon. `make_item` runs the builder (it needs a valid icon) and then destroys the icon function through
its own manager (`op 3`) and stores 0 at `item+0x34`: no new anchor, only the offsets above. The DIGIKIT row in SETTINGS keeps its glyph (`glyph = true`), as
every stock SETTINGS row has one. The `digikit` frame of the menu cells changed in the glyph boxes only (318 px in x 1..50, y 2..61: a box left of each of the five rows and the one at
the bottom left); `settings_digikit` and the other frames of the cells are unchanged. Frame hash on both devices `b52b1981...`.

**No stock text-only view.** Of the stock view classes (about 130 by name) none shows free text or a list without selection: `FreeSpaceMenuView` (STORAGE, ctor `0x401550f4`)
paints a fixed layout of its own, `ShowMsgView` is the LED message view, and the popup the menu already uses shows one message and takes the key focus. The
non-selectable alternative does not exist either: a MenuItem has no disabled flag the paint or the key handler reads (the byte at `+0x50` is never read there).
**[O]** `OsUpgradeMenuView`, `PlusDriveFormatMenuView`, `TimeoutView` and the like were not read.

**The highlight and the cursor.** The row bar is the paint's: with the Menu's marker (`this+0x24`, `-1` after the constructor) negative, it inverts the row whose index in
the visible window equals the cursor (`this+0x18`, read through vtable slot 15). Parking the cursor does not hold: the selection getter `FUN_40114bf6` recomputes
`cursor = selection - scroll` on every call, and the move function `FUN_40114aac` sets `selection = cursor + scroll` on every key, so the first key brings the bar back,
and a page of more than five rows needs the keys to scroll. What works is hiding the cursor only during the paint, on the three text pages (ABOUT, HEALTH, MEMORY; MODULES stays selectable):
- The page's View sub-object (`obj+0x38`) reaches the paint through slot 4 of its vtable, a thunk (`moveq #-0x38,d0; add.l d0,4(a7); bra.w vfunc_31`, bytes `70c8 d1af 0004 6000`).
  The pages get a copy of that vtable in module RAM (17 slots) whose slot 4 is `text_paint`: it saves the cursor, sets it to `0x7fffffff`, calls the stock slot, restores it.
  The keys, the selection and the scroll are the stock ones; only the bar is gone.
- The copy must include the two words before the slots (`offset_to_top`, the type info; 19 words, the object points at word 2): the first try, 17 words only, faulted with
  `UnsupportedGuestPc(0)` on opening ABOUT, because the view manager reads them (a cast through the vtable).
- Checked on both devices: every page (ABOUT, HEALTH with `core/health`, MEMORY) opened and popped twice, with no fault; HEALTH scrolled seven rows down and seven up (the window
  moves, the scroll bar follows, no row inverted, the frame after seven UP equals the first); the smoke `menu` tour pins the MEMORY frame (`digikit_memory`) and requires it to be
  the same after one DOWN.
- This is a module-owned vtable copy, so it breaks finding 23's "no new vtable" for these pages. The module does not call it with a stock slot it does not recognise: the first
  word of slot 4 must be `0x70c8`, otherwise the pages keep the bar. **[O]** Hardware: not run on a device; the paint-time cursor write assumes one UI task paints.
**[O]** The alternative (c), a page class of our own, was not built.
