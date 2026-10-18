# 47. Digitakt II on the device: the flash rungs

Em's Digitakt II, OS 1.17, 2026-10-18. Images and steps: docs/PATCHING.md "Flashing a Digitakt II, rung by rung"; plan
`docs/plans/digikit-runtime.md` Phase 9. Marks: **[D]** measured once on the device, **[O]** open. The log lines are what
`tools/device/dklog.py follow --once` printed over USB (the runtime's LOG command, finding 46).

## Rung A: the runtime with no DSP change **[D]**

Image `out/patch/rungA-dt2-1.17.syx` (`core/runtime,core/digikit-menu,core/health`; sha256 `01e8cae4a713f5a7...`; preflight: section 3
only, sections 2, 4, 5, 7 and 8 identical to stock), flashed through SETTINGS > SYSTEM > OS UPGRADE with Transfer.

- It boots to the main screen; SETTINGS lists DIGIKIT; the log arrives over USB SysEx.
- `BOOT_START runtime=3 device=1`, `UI_FIRST blocks=4481` (2.99 s of firmware blocks after the hook; the coupled emulator: 5,972 on a
  blank card, finding 37), `STABLE blocks=49485` (the 30 s period ran out), no safe mode, nothing loaded, `DSP_ABSENT` (no `dl_diag`
  on this rung, as designed).
- **The module store range on this card is blank:** `CARD_CHECK state=blank first_nonblank=None read_errors=0` (all of sectors
  0x38000-0x3E000 read through the firmware's block read; finding 46). The runtime wrote nothing to the card (a blank card gets the
  record only when the selection changes).
- `RAM_KEEP kept=0 generation=0`: the first boot after a flash has no marker. Whether RAM survives a soft reset on this board is still
  **[O]** (the menu offers APPLY & RESTART only with a store; rung C).
- HEALTH, steady over 70 s with the device idle at the main screen: CPU 27.3-28.1 % (relative to the boot's idle floor, finding 41;
  the emulator reads 20-34 %), DSP 40.8 % average, 44.2 % maximum (EMUCLK block time at the assumed 1 GHz), heap free 27,448 KiB
  (emulator 26.7 MB). The DSP figure is about 1.3 times the emulator's 31 % on an idle sample card, inside the DT2 cycle factors of
  `devices/cycle-factors.json` (1.13 nominal, 1.41 high); with the same project on both sides it would calibrate the factor, or show the
  clock is not 1 GHz **[O]**.

## Rung B: with the DSP link and the voice hook, no modules **[D]**

Image `out/patch/rungB-dt2-1.17.syx` (rung A plus `core/dynreg,core/dyndsp`, which add `core/machines`, `core/params`, `core/dsp-link`,
`core/dsp-voice`; sha256 `7d2851ee8f7460eb...`; preflight: sections 3 and 7). Em played a project with sample tracks for about a minute and
previewed a sample; `dklog follow` ran for 172 s.

- The DSP ran the whole session: no `DSP_STOPPED`; both heartbeats of `dl_diag` (finding 45) rose by exactly 1500 per second (the link
  hook's word 1 and the voice hook's word 2: the hook ran every block on stock voices).
- **The context registers at the voice hook on silicon equal the emulator's, all eleven**, constant over the session:
  M5 0, M6 1, M7 -1, M13 0, M14 1, M15 -1, I6 `0x2cb2c8`, I7 `0x2cb2c0`, B7 `0x2ca5a0`, L7 `0x3e2`, MODE1 `0x39011cf8`
  (`uv run python -m tools.patchkit.pk_ctxprobe --device dt2 --fw 1.17 --modules core/dsp-voice,core/dsp-link` gives the same words). So the
  2026-10 hardware report's two causes (M6 and M13 not having these values) did not hold at this hook on this device, which agrees with
  finding 45; what halted the SHARC there is still **[O]**.
- `RAM_KEEP kept=1 generation=1`: the keep marker that rung A's boot wrote survived the reboot that ends the OS update, so RAM above
  `cf.expansion` (`[digikit] keep_base`) is kept across that reset on this board. That the same holds for the runtime's own soft reset is
  likely but not yet seen.
- HEALTH: idle CPU about 30 %, DSP 41.5 % average idle (rung A: 40.8 %; the hook and link cost about 0.7 points), 55 % average and 59.6 %
  maximum while the pattern played; CPU 95-100 % for about 20 s while the project loaded. Heap free 27,416-27,449 KiB.
- The card check again reported the store range blank; nothing was written.
