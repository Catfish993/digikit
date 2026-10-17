# 40: the working state across APPLY & RESTART, the selection record in the +Drive folder, the DSP at boot (digikit runtime, Phase 6)

Plan: `docs/plans/digikit-runtime.md` (Phase 6). Spike: `docs/findings/36-boot-load-spike.md` section 2. Boot path: `docs/findings/37-boot-load.md`.
Lane D, 2026-10-17, native emulator, Digitakt II 1.17 (and 1.16 for the decompile), Digitone II 1.12 for the anchors. Nothing ran on a device.
Marks: **[D]** measured or read once here, **[O]** open, **[C]** corrects an earlier claim. Nothing is **[V]**.

## 1. Saving the working state before the restart

### What was built **[D]**

`dkrt_restart` now runs the firmware's own save sequence (`modules/core/runtime/src/rs/src/lib.rs`, `save_working_state`), selected by the bits of
`DKRT_SAVE_MODE` (default 0 since the coordinator's decision below; 3 runs both steps):

1. bit 0: `fw_project_serialize`, the firmware's "serialize the live project into the persistent arena" (finding 36: `FUN_40030c9c`, no arguments, returns 1;
   it calls `FUN_400df508(arena container 0x4099d698, Project data, 0, 0xffffffff, ...)`). **Same function hash on all four ports** (DT2 1.16/1.17
   `0x40030c9c`, DN2 1.11/1.12 `0x4002c694`); anchor `dkrt.fw.project_serialize`.
2. bit 1: `fw_boot_record_write`, the firmware's normal writer of the boot record (`FUN_400f044a`; DT2 1.17 `0x400f05ae`, DN2 1.11 `0x400f1e26`, 1.12
   `0x400f1f8a`): writes the 0xdd9714-byte arena (DN2 0xc4b114) to the next of the two record slots, zeroes the first word of the other, then sets its
   own one-shot flag. Its hash differs between the devices (constants), so the anchor `dkrt.fw.boot_record_write` has one rule per device. DT2 and DN2
   are each one hash on both versions.

The two anchors are in `module.toml`; the port lock files were regenerated (`port_resolve resolve-all`): four lines added to each of
`devices/ports/*.toml`, nothing else changed. The old `DKRT_SAVE_FN` stub is gone (the word `DKRT_SAVE_MODE` is 4-aligned so that a host can poke it).

Card writes of a restart with mode 3 (DT2 1.17): sector `0x48000 + 28364` (the record slot the firmware chose) and `0x40000` (one sector, the other slot
invalidated), plus what the new boot writes by itself (`0x50000 + 28364`, the working slot). Nothing in the module store besides the selection record.
These are the sectors the firmware's own writer touches.

### Did the edit survive? No, and the emulator shows why it could not **[D]**

The test edits (a panel TRIG key, two encoder turns; the frame hash changes after each, and the TRIG LED lights) do not survive `dkrt_restart`
in any variant:

| Restart variant (DT2 1.17, TRIG 1 pressed first) | After the next boot |
| --- | --- |
| mode 0 (no save, the old stub) | LED off |
| mode 2 (record write only) | LED off |
| mode 3 (serialize, then record write) | LED off |
| SAVE PROJECT AS 001 first, then mode 0 or mode 3 | LED off |
| FUNC+SETTINGS quick save first, then mode 0 | LED off |

Why (all measured):
- The record written in mode 3 is **byte for byte equal** with and without the edit, and equal to the one written in mode 2: serializing the live project
  does not change the arena at all (`cf_script` run: calling `FUN_40030c9c` after the edit changes 0 bytes of `[0x4099d588, +0xdd9714)`; it ran
  22 million instructions). The arena already equals `serialize(project)`.
- The slot written by SAVE PROJECT AS is **byte for byte equal** with and without the edit (TRIG, and two encoder turns on the main screen).
  So the data the save path serializes (`Project` vfunc `+0x28`) does not carry these panel edits in the emulator state tested either.
- Probes on the ColdFire (`cf_script` `probe`/`hits`): `Project::updateMirror` (`0x400435a0`), `FUN_40041b1c`, `FUN_40030c9c`, `FUN_400df508`, the project
  save `FUN_40043a30` and `ValueWithMirror<pattern_t>` change notification `0x401a572a` are **never executed** by a TRIG press, an encoder turn,
  PLAY/STOP or FUNC+YES, over up to 1.5 billion instructions after the edit; the arena did not change either. They do run for a menu action that
  changes the project (a SETTINGS entry; `updateMirror` once, `FUN_40043a30` twice, `FUN_400df508` three times).
- `Project::updateMirror(project, container, changeInfo)` is the firmware's live mirror: `ValueWithMirror<T>::vfunc_4` (the change notification) calls
  vtable slot `+0x44` when the value's mirror pointer is set and posts the BgWorker job "Project::updateMirror", which serializes the changed parts
  (`FUN_40041b1c` -> `FUN_400df508`) into the arena. So the arena is kept current by the mirror, not by a save call; the stock power-off writer
  (`FUN_400efe74`) writes the arena as it is. Panel pattern and sound edits do not go through it in the emulator.

So the claim "the firmware saves the working state with serialize + record write" holds only for what is already mirrored; the panel edits tried
here are not (the stock power-off path writes the same stale arena). **[O]** where a TRIG/encoder edit lives until a save flushes it (the engine's
pattern and sound state; the save path obviously flushes it on a device), and whether a real device keeps unsaved pattern edits across power-off. I did not
find the flush. The first thing to try next: a bounded watch of the writes that follow a TRIG press, from the key handler to the first store into a
structure the save path later reads.

### Decision

**[C]** Coordinator, 2026-10-17: the default is **0** (no save). The experiment has no positive control (SAVE PROJECT AS did not keep the
test edits either, so the edits may not be project edits at all), the save brings no measured benefit, it writes 14.5 MB from our code on every
restart, and the serialize may race the BgWorker. The menu prompts for a project save before APPLY & RESTART. Mode 3 stays for the next experiment,
which must start with an edit that SAVE PROJECT keeps. The original proposal follows.


Ship mode 3 as the restart sequence (it is exactly what the firmware does before an OS update, writes only the firmware's own sectors, and
keeps whatever the mirror holds: project-level settings), **and** have the menu say it plainly: APPLY & RESTART keeps what the project menu
saved and the mirrored settings, not unsaved pattern edits. The safe form of that is a prompt before the restart, "SAVE PROJECT FIRST (FUNC+SETTINGS)",
and the menu lane owns it. Cost: about 22 million instructions for the serialize and a 14.5 MB eMMC write for the record (a second or more on a device;
**[O]** not timed).
`DKRT_SAVE_MODE` = 0 turns the save off. Risk **[O]**: the serialize runs in the UI task while the BgWorker may be inside a mirror job; the emulator never
showed a problem, a device may.

## 2. The selection record in the +Drive folder **[D]**

`packages/emulator/plusdrive/src/selection.rs` (record encode/decode with the runtime's rules: magic, version, count <= 8, path NULs, CRC; the copy with the
higher `seq` wins, A on a tie) and the folder file `plus_drive/<device>/digikit/selection.txt`: one store path per line in load order, `#` lines and blank lines
ignored.

- **Builder** (`scan_folder` and `build_tree_image`): `selection.txt` is not a store file; it becomes both copies at `end_sector - 2` and `end_sector - 1`
  with `seq` 1, counter 0, no flags. A path that is not in `digikit/` is reported in the scan's `skipped` list (the runtime would answer NOT FOUND). A bad
  file (path over 55 bytes, more than 8, not UTF-8) stops the build with the line number. No file: both sectors stay zero.
- **Store limit:** the store's files now keep out of the two sectors (`end - first - selection_sectors`); a file set that reached into them is refused
  (it used to be allowed to fill the whole range).
- **Exporter:** reads both copies from the card plus the overlay, and writes the winner to `digikit/selection.txt`. A file in the folder that holds the same
  list (other comments, spacing) is unchanged; a different list replaces the file with the old one in `backup/`. No valid copy: the folder file is left alone.
  The boot counter and the flags are the runtime's own and are not exported (the builder starts them at 0).
- Tests: `selection.rs` (3), `tree.rs` (`the_selection_is_written_as_two_copies...`: both devices, store limit, no selection), `folder.rs`
  (`selection_txt_is_the_record...`), `export.rs` (`the_selection_record_round_trips_with_the_folder`: builder record exported, comment-only difference unchanged,
  a runtime-written copy B with a higher seq wins and the old file is backed up, a torn copy loses, no valid copy leaves the file). All 53 plusdrive tests pass.
  `mise run plusdrive:check` (DT2 and DN2) now also reads the two record sectors through the firmware's block read and compares them with an independent
  Python encoding of the record (`tools/firmware/plusdrive_check.py`).

## 3. The DSP at boot is the default **[C]**

Finding 37 said the emulator booted a patched firmware with the stock DSP and applied the patched section after the ready state, and added
`DIGI_COLD_DSP=patched` for the proofs. Checked in `packages/emulator/control/src/controller.rs` (`load_patched_coupled`, the one place both the daemon's
`firmware.load` and the desktop app's `Command::Load` go through): **yes, `mise run emu -- PATCHED.syx` ran the stock DSP code through the whole boot** and
injected the DSP diff once the machine was ready. That is not what a device does, and any DSP code that initialises itself at boot (the runtime's boot-time load,
`core/dsp-link`'s area) saw the wrong order.

Fix: the coupled cold boot always starts with the image's own DSP section when it differs from the base's (the `DIGI_COLD_DSP` switch is removed, the proof
tool no longer sets it). The report says `"dsp": {"applied": true, "at": "cold boot"}`. The non-coupled and snapshot paths still inject the diff after ready
(no DSP model there to boot). What it affected:
- A patched image with a changed DSP section now needs one full cold boot per distinct DSP section (the ready pair's cache key already contained the DSP
  digest; before, it was one per MAIN section). Images with the base's DSP are unchanged: same pair, same cache.
- Pins, `check`, tier sweeps: stock images, no DSP diff, untouched by the change. Patched-image proofs that load through the daemon (`pk_dsplink`,
  `pk_dkrt_boot`, `pk_dkrt_machine`, the patchkit smoke tours) boot differently now; the ones run here are listed under "Checks".

## Checks run for this finding **[D]**

- `uv run python -m tools.patchkit.pk_dsplink proof --device dt2` and `--device dn2`: pass, with the DSP at boot as the default.
- `uv run python -m tools.patchkit.pk_dkrt_boot proof --image dt2-1.17 --quick`: all 26 checks pass without `DIGI_COLD_DSP`.
- `cargo test -p plusdrive-format` (53 tests), `mise run plusdrive:check` (DT2 20 calls, DN2 6 calls, the record sectors included).
- Restart with the save sequence: DT2 1.17 and DN2 1.12 boot again; DN2 1.12 writes `0x48000 + 25175` (the DN2 arena) and invalidates `0x40000`.
- Not run: the patchkit smoke matrix and the other daemon-loaded proofs (the coordinator's integration run).
