# 37. Boot-time loading: the hook, the selection record, safe mode, restart (digikit runtime, Phase 6)

Plan: `docs/plans/digikit-runtime.md` (decision 9, Phase 6). Spike: `docs/findings/36-boot-load-spike.md`. Design: `docs/design/digikit-runtime.md`
("Boot-time loading"), `docs/design/digikit-store.md` ("The selection record"). Code: `modules/core/runtime` (`boot.rs`, `dsp.rs`, `loader.rs`,
`sdk` `sel`), `packages/emulator` (the RCM soft-reset model), proof `tools/patchkit/pk_dkrt_boot.py`. Lane r4, 2026-10-17, native emulator, Digitakt II 1.16
and 1.17 and Digitone II 1.11 and 1.12 (stock images; nothing ran on a device). Everything is **[D]** (measured here once; no second agent has
checked it against the image bytes) unless marked **[O]**.

## Result

**[D]** A selection saved in the card's store range is loaded at boot, before the firmware builds its machine tables, and plays like the same
module patched in. `uv run python -m tools.patchkit.pk_dkrt_boot proof --image dt2-1.17` (also `dt2-1.16`, `dn2-1.12`, `dn2-1.11`; `--quick` runs fewer cut
cases and one restart):

| Scenario | Result |
| --- | --- |
| equivalence, DT2: `test/dyn-pluck` selected, cold boot | `results [0]`; its voice row `{18, 0xb8d740}`; the DSP area holds the relocated image byte for byte; the machine row is named; the stock machine list takes it (the track gets type 18); `view_at_boot` 0 and `view_rebuilds` 0; **PCM of the note equals the patched-in `machines/pluck` sample for sample (95 808 samples, 0 differ)** |
| equivalence, DN2 1.12 and 1.11: `test/dyn-dn2` + `test/dyn-sine-dn2` | `results [0, 0]`; machine row 16 named; voice row 18 claimed; the DSP image byte for byte; the sine's PCM equals the patched-in `test/dn2-sine` (type forced in the DSP frame copy as in finding 32) sample for sample from the onset, the note starting one block apart (onset shift -64) as in finding 32 |
| restart | a card with no record boots with no module; the mailbox selection writes both copies (same sequence number); `dkrt_restart` + the emulator's soft reset: the next boot loads the module; select nothing + restart: the next boot loads nothing and the voice rows are free |
| power cut between the two writes | A new / B old: the boot uses A (seq 2); A torn (bad CRC) / B old: the boot uses B (seq 1) and loads what it holds; A old / B new: B; both invalid: no module; and the real case, one copy written by the runtime (`CMD_SELECT_ONE`) then restart: the new copy is used |
| safe mode, counter | a selection with a module whose `init` resets the machine: boot 1 raises the counter to 1, boot 2 to 2, boot 3 is safe (`reason` counter, counter seen 2), loads nothing, and its first UI-loop pass clears the counter to 0 with the selection kept |
| safe mode, flag | the record's flag bit 0: safe (`reason` flag), no module, the flag cleared, the selection kept |
| write-watch, flush check, card store | 0 stray writes into `cf.expansion` over every emulator of the run (cold boots, reset boots, the installed runtime; the staging buffer and the record buffer are DMA targets and left out), 0 flush hits; the only sectors in the store range the guest wrote are the two record sectors |

## What was found

- **[C]** Finding 36 put the hook just before the MainScreenView constructor and said the machine view is built after it. **Measured here: the view
  already exists there.** The call at DT2 `0x40033cf2` only uses the MainScreenView singleton getter (25 call sites); one of the singletons the task builds
  first made it, and with it `KitBrowser` and `MachineSelectionView`, which reads the machine list once. `DkrtBootInfo::view_at_boot` was non-zero at that
  hook and at the view-system-init call before it, so the registry had to call the builder again (`view_rebuilds` 1). The hook is now a `hook` op at the
  **entry of the main-OS task** (DT2 `0x400337ba`, DN2 `0x4002ed34`; anchor `dkrt.hook.boot`, the displaced `link`/`movem` run in the tail):
  `view_at_boot` is 0, no view hook and no second builder run, and the stock list shows the run-time machine.
- **[D]** At that point the DSP cannot take code: a transfer started at the task entry was never acknowledged (the sender's 900-block timeout, `E_DSP_XFER`).
  The frames that carry the link start later (with the sound engine; the order inside the task was not traced further **[O]**). Design: a module with a
  DSP section goes live in two steps, the CF part at the hook (machine and parameter rows, `init`, slots) and the DSP part (image, voice rows) over
  the first UI-loop passes, queued behind each other (`DspState.waiting`); the result shows `LOADING` until then. A failure of the DSP part takes the
  module down again (`undo_early`). **Consequence:** until the voice row is in, a saved track on the machine plays as type 0 (the selector of a reserved
  type). By the ready point of the cold boot the DSP part is done (`at_ready` in the report).
- **[D]** The block read and write work at the task entry (the eMMC mutex and the scheduler exist). The firmware's block write
  (`FUN_4012e0c0` / `FUN_4012c780`, hash `0dd5a176...` on all four ports) has the signature `int f(sector, bytes, buf)` of the read, and takes the mutex itself.
- **[D]** Boot delay: the cold boot of the image with the module selected and without it reach the ready point at the same ColdFire instruction
  count (1 114 485 926 on DT2 1.17), i.e. the loads cost less than the resolution of the ready detector (one DSP frame, 88 000 instructions).
  The ColdFire is idle-bound during boot. **[O]** the delay on a device, where the eMMC reads take real time (about 10 sectors for the record and the
  module: the record is read twice at boot, once more for the counter write).
- **[C]** The emulator booted a patched firmware with the **stock** DSP code and applied the patched DSP section after the ready state. A boot-time DSP load
  needs the image's own DSP from the first instruction, as on a device. The coupled cold boot starts with the DSP patch applied
  (`digi_coupled::cold::ColdInputs::dsp_patch`; the ready pair's cache key includes the DSP section). **[C]** (finding 40) It was off by default, behind
  `DIGI_COLD_DSP=patched`; it is now the only behaviour, so `mise run emu -- PATCHED.syx` boots the patched DSP too.
- **[D]** The RCM soft reset is modelled: a store of `0x80` into `0xEC090000` (`Board::soft_reset_requested`, `Status::soft_reset`) makes the controller cold-boot
  the same patched firmware again on the card as the guest left it (its writes laid over the image), the DSP as above. A reset **before** the boot is ready (a
  module that resets in `init`; the firmware's exception path ends in the same store, `0x40001ab8`, but the emulator vectors an exception and flags it as a fault
  instead of running the handler, so `test/dyn-fault` writes the reset itself) restarts the boot too, up to 8 times in a row. After a soft reset the DT2
  filesystem-check worker the readiness contract waits for does not run again (the card was used), so the reset boot is ready at the main panel
  (`Emulator::relax_fs_check`).
- **[D]** Selection record: layout and write order in `docs/design/digikit-store.md`. Two copies at `end_sector - 2` and `- 1` (`0x3dffe`, `0x3dfff`), a CRC and
  a sequence number each. A selection writes A then B with `seq + 1`; the boot-counter updates write the copy that is not current. `[card.store] selection_sectors`
  is a new profile field (default 2) that the store builder must keep its files out of (lane D).
- **[D]** `dkrt_restart` is the firmware's own sequence (interrupts masked, console drained, DDR word 0 cleared, `0x80` into `RCM.RCR`).
  The unsaved-state save is `save_working_state` (finding 40).

## Open **[O]**

- **The safe-mode key** (RECORD, button 19, `SAFE_KEY_CODE`): the code peeks the UI queue (`+4` key, `+8` state of a type-0 item) for a waiting key-down.
  The layout is read from the dispatch code, not observed; on a device the held key's event may not be queued yet at the task entry; the emulator delivers no
  key before the main OS runs. The counter and the record flag are the proven routes.
- **`dkrt_restart` during a card transfer**: the check reads the eSDHC present-state register (`0xFC0CC024`, bits 0 to 2) twice, the second time with
  interrupts masked. The emulator's controller never reports a transfer busy there, so the refusal is not exercised. A task between two blocks of a
  multi-block write holds the eMMC mutex without a transfer in flight; whether the stock firmware's own reset (`FUN_400f044a`) has the same exposure is not known.
- **Saving the working state before the reset** (finding 36, question 2): done in finding 40 (serialize + boot-record write, anchored on all four ports); the panel edits tried do not survive.
- **The module store must keep clear of the record sectors** (`selection_sectors`): `plusdrive` still lets a store fill the whole range.
- **A track that names a machine whose DSP part is not in yet** plays as type 0 for the first moments of a boot; saved projects keep the type byte.
- **Hot-only code remains** (unload, queue, in-use scan, the view hook): the coordinator's archive commit removes it; `view_rebuilds` stays 0 at boot.
