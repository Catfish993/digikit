# 34. digikit runtime: the UI task's stack, and the menu's error handling

Lane r1 (lane C), 2026-10-17, native emulator, all four images (DT2 1.16, 1.17, DN2 1.11, 1.12). Everything here is **[D]**: measured or
read once; no second agent has checked the addresses against the image bytes. Nothing ran on a device. Plan: `docs/plans/digikit-runtime.md`
(Phase 4). Menu: finding 28. Runtime: finding 29.

## 1. The UI task's stack: 160 KiB, 3.3 KiB used in a load **[D]**

**Result.** `dkrt_load` and `dkrt_unload` run in the UI task (the hook is in its event loop, the menu action is called from its key
handler). The task has a 160 KiB (0x28000) stack on all four images. The deepest store seen anywhere in the DIGIKIT proof scripts,
which include a load of the demo module, two refused loads with their popups and an unload, is 3256 bytes below the top (2.0 %);
the margin is 160 584 bytes. **A load may run on the UI task; no dedicated stack is needed.**

**Where the stack is (from the code that creates tasks).** The firmware has one task-create function, `FUN_400012c8` on all four
images (`task_create(task, entry, priority, stack base, stack size)`, cdecl; it puts the initial frame at `base + size - 12` and the
stack pointer in the task record at +0x48). Its callers push constants, which `uv run python -m tools.coldfire.task_stacks --image IMAGE`
reads from the Ghidra dump (the other tasks are in its output too). The UI task is the one whose loop holds the queue pop that the
runtime hooks (`pea queue; jsr FUN_40001928; addq.l #4,sp; moveq #0x29,d1`):

| image | created at | task record | entry | priority | stack | size |
| --- | --- | --- | --- | --- | --- | --- |
| DT2 1.16, 1.17 | `0x40033280` in `FUN_4003324e` | `0x40966ee8` | `0x400337ba` (`pea (0x542,PC)` at `0x40033276`) | 6 | `0x4096775c`..`0x4098f75c` | 0x28000 |
| DN2 1.11 | `0x4002e902` in `FUN_4002e8d0` | `0x4059d1c0` | `0x4002ed34` | 6 | `0x4059da34`..`0x405c5a34` | 0x28000 |
| DN2 1.12 | `0x4002e902` in `FUN_4002e8d0` | `0x4059e1c0` | `0x4002ed34` | 6 | `0x4059ea34`..`0x405c6a34` | 0x28000 |

(The queue buffer follows the stack on DT2: `0x4098f75c`, 0x400 bytes. DN2's loop is in code Ghidra has not made a function, so the tool
takes the pop site `0x4002f178` from `docs/design/digikit-runtime.md`; it lies in the task of entry `0x4002ed34`.) Other tasks' stacks
are 2 to 32 KiB (the sample, MIDI and file workers).

**The instrument.** `DIGI_STACK_WATCH=LO-HI` in `packages/emulator/boot/src/watch.rs`, next to the write-watch and flush check:
every CPU store into the range is tracked after the write-watch start (`DIGI_WRITE_WATCH_AFTER=cacr`; the reset-path RAM clear writes
all of bss), the lowest address and its PC and instruction count are kept, and `DIGI_STACK_WATCH_EPOCH=LO-HI` ends an epoch at each store
into the named words, so a phase is measured on its own. Interrupts push on the task's stack and are counted. Limit: a frame that is
reserved and never written is not seen. Documented in `docs/INSTRUMENTATION.md` ("Task stack high-water"); a unit test is in `watch.rs`.
The menu writes the counters `MARK[0]` (start) and `MARK[1]` (end) of every LOAD or UNLOAD, so `tools/coldfire/digikit_menu_proof.py
--stack` reports the high-water inside requests and outside them.

**Numbers** (`uv run python -m tools.coldfire.digikit_menu_proof run --image IMAGE --quick --stack`; bytes below the stack top; the
DT2 and DN2 differ only in the firmware's own frames):

| | DT2 1.16 | DT2 1.17 | DN2 1.11 | DN2 1.12 |
| --- | --- | --- | --- | --- |
| idle, hook only, SETTINGS > DIGIKIT, ABOUT | 1704 | 1704 | 1704 | 1704 |
| outside requests (navigation, rebuilds, popup dismissed) | 1872 to 2088 | 1872 to 2088 | 1788 to 1792 | 1788 to 1792 |
| inside a LOAD or UNLOAD request (menu, `dkrt_load` of dyn-demo, the refused loads, the popup) | 3256 | 3256 | 3256 | 3256 |
| deepest store, PC (the runtime's `memcpy`) | `0x47e8e1b8` | `0x47e8e1b8` | `0x467151b8` | `0x4671d1b8` |
| margin of 163 840 | 160 584 | 160 584 | 160 584 | 160 584 |

The deepest PC is the runtime's `memcpy` (`copy_forward`), the leaf of the chain `dkrt_load` > `loader::load` > `store::load_to_stage` > `Sha::update` > `Sha::block` (608-byte frame, the message schedule) that the static scan below also finds as the longest. Normal firmware use: the stock SETTINGS and project
scripts of finding 25 (`--stack-stock`, DT2 1.16, patched image) reach 1608 bytes (save, load, new, manage, format, pattern; 1984 for
`save_quick`): the project load runs in the UI task but is not deeper than the idle loop in the scripted case **[O]** (only the scripted
paths were run; a sample browse or a rename view was not measured).

**Static side** (`uv run python -m tools.coldfire.frame_depth modules/core/runtime/build/runtime.dko.json dkrt_load`): a linear scan of the
frames and calls of the runtime object gives `dkrt_load` = 988 bytes: `loader::load` 180, `store::load_to_stage` 276, `Sha::update` 20,
`Sha::block` 608 (the message schedule), `memcpy`. `dkrt_unload` 148. It does not follow the module's own `init` and slot functions
(indirect calls) or the firmware's block read; the measured 3256 includes the block read (firmware frames, on the path of the demo)
and the menu's frames above it (key handler, menu action, `do_load`; the menu's largest are `status_text` 340 and `refresh_loaded`
224 bytes).

**What to do.** Nothing for the loader. A module's `init` and slot 0 (called every UI pass) share the 157 KiB that is left: a budget of
16 KiB of stack per module function is cheap to promise and leaves a factor of 10. If a module ever needs more, the way is a dedicated
stack switched in the hook trampoline (`tramp.s`): save a7 in a static, load the top of an arena block, call, restore; about ten
instructions and the block (both devices' cf.expansion has the room); half a day with a test that fills the block with a pattern.
Not built: the margin does not ask for it.

## 2. Menu: popup, texts, UNLOAD, loading state, greyed rows **[D]**

All in `modules/core/digikit-menu`; proof `uv run python -m tools.coldfire.digikit_menu_proof run --image IMAGE [--quick]` on all four
images: 4 scripts each, frames pinned in `tests/golden/digikit_menu_frames.json`, module state read back, write-watch over all of
`cf.expansion` (except the staging buffer): 0 events; flush check 0 hits. The stock SETTINGS and project scripts of finding 25
(`--regress`) give the same frames and card writes on the patched and the stock image: DT2 1.16 (10 scripts) and DN2 1.11 (9) re-run
after this change, pass.

- **Error popup: the firmware's own.** `FUN_400a4832` (DT2 1.16) shows "LOAD OK" or "LOAD ERROR" after a project load: it builds
  `make_shared<PopupWindow>(const char *const &)` (`FUN_401b84e0`, result in A0), moves the `shared_ptr` into a `shared_ptr<View>`
  (`FUN_4019c928`), gets the Brain singleton (`FUN_40198948`) and its view manager (`FUN_40030a42`: Brain + 0x40), adds the view
  (`FUN_4011bdaa(manager, &sp, 0)`) and releases the count holders (`FUN_40197a4a`). The menu does the same with its own text. The
  helpers are templates that many callers share, so their hashes repeat (26 functions hash alike); the anchors are the call operands inside
  `FUN_400a4832` (`cf_ptr` rules, `c4d86e4e:tools/patchkit/pk_menu_rules.py`), six new anchors resolved on all four images (the call sites sit at
  the same offsets; `devices/ports/*.toml` got six `[[anchor]]` blocks each). The popup is a framed box, takes the key focus and goes
  with NO, about 25 characters wide on one line. The text is copied from a static buffer `POPUP`, which the proof reads back.
- **Texts.** A popup and the answer rows name the reason and the file: `BAD-IMPORT.DKM: NO IMPORT`; the rows read `NO IMPORT +1` (more
  failures follow) and the file name below. Codes are looked up in three tables (`TEXT_RUNTIME` -1..-19, `TEXT_REGISTRY` -20..-29,
  `TEXT_DSP` -30..-39, one per owner, so lanes add lines in their own table); a code without text, positive or outside the tables shows as
  `ERROR <n>` (for example `ERROR -23`). Plain tables again, not `match`: six dense arms made a jump table (68020 code, refused by
  `pk_build`); `dispatch` is an `if` chain.
- **UNLOAD is its own row**, below LOAD. Ticks only select: LOAD loads every ticked module the runtime does not have, UNLOAD unloads
  every ticked module it has, and a loaded module no longer starts ticked (first pass: unticking a loaded row and pressing LOAD unloaded
  it). A ticked row whose request succeeded is unticked; a failed row stays ticked. Answer rows: `LOADED n`, `UNLOADED n`, the reason
  and `+n`, `NOTHING TO DO`.
- **Loading state (lane A's asynchronous loads).** The menu reads the fields that `dkrt-sdk` of lane A (branch `lane/r4`) defines: `dkrt_load`
  and `dkrt_unload` answering `LOADING` (1), `DkrtStatus::state` (word 12: 0 idle, 1 loading, 2 unloading) and `DkrtModule::state` (byte
  offset 112, `MOD_LOADING` 1). It reads them by position from buffers larger than the struct it was built with, so the same menu works with
  the runtime of this tree (the words stay zero) and with lane A's. On `LOADING` the batch stops (the remaining ticks stay), the row
  shows `LOADING...`, and the module's row shows `L` in the marker column; while `state` is not idle a second LOAD or UNLOAD is refused
  with `LOADING...`; the end is collected by `poll` at the next menu action (opening the list, a tick, the answer row, LOAD or UNLOAD):
  state idle, `last_result` gives the answer, a failure shows its popup. The menu has no timer, so a finished load is shown after the next key;
  pressing YES on the answer row asks again. **[O] Not run against an asynchronous runtime**: lane A's runtime is not in this tree. The logic
  is in `poll`, `do_load` and `do_unload`; the proof has no script for it yet (it needs a module with DSP code on the card).
- **Modules not for this runtime or device are greyed (`[-]`) and cannot be ticked.** The menu reads the first sector of every `.dkm`
  and the sector of its register records: `runtime_min` above the runtime's ABI, or register records (`DKRG`, offset in the header at 0x90)
  for the other device (`DkrtStatus::device`). A module without register records runs on either device and is shown; a file that is not
  a `.dkm` or cannot be read is shown, so that the runtime names the real error when it is ticked. Cost: two block reads per file when
  the list opens (32 files at most). The `.dkm` header has no device field of its own (finding 29 format); the register records are the
  only place; a module with no records but DSP code for one device (lane A) will want a header flag.
- **Proof additions** (`digikit_menu_proof.py`, `panel_script.py`): the card has five modules (`bad-import`, `bad-sha`, `dyn-demo`, plus
  `foreign.dkm` and `future.dkm`, forged from dyn-demo with the header field changed and the sha made right); `digikit_errors` ticks two broken
  ones, LOADs (the firmware's popup text is read back by `--peek-at` before it is dismissed: `BAD-IMPORT.DKM: NO IMPORT`), dismisses, unticks
  one, LOADs (`BAD-SHA.DKM: BAD SHA`), UNLOADs with nothing loaded (`NOTHING TO DO`), presses YES on a greyed row (no tick);
  `digikit_gate` loads and unloads dyn-demo from the list (counter rises, stops, `fini` ran) with no popup. `--quick` runs these two only;
  `--stack` and `--stack-stock` add the stack numbers.

## Open

- **[O]** The asynchronous-loader behaviour of the menu is untested (above).
- **[O]** Not on hardware. The popup, its heap objects and the two header reads are the new firmware interactions; the flush check saw nothing.
- **[O]** The stack numbers are the scripted paths only; the firmware paths no script reaches are not measured. The 160 KiB is the
  allocation; the firmware may keep guard words in it that this did not look for.
- **[O]** More than 32 `.dkm` entries are still not listed; a popup text longer than the box (about 25 characters) would be cut by the
  firmware (file names are kept to 24 characters plus the reason).
