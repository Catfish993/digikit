# 66. digikit/freqshift as a send effect (FX routing step 5, Digitakt II 1.17 and Digitone II 1.12, 2026-10-25)

The frequency shifter is the first real effect on the send route (finding 62). Its manifest now says `route = "send"`, `routes = ["send", "master"]` and `mix = 127` (finding 65). Marks: **[D]** measured or read once by the author, **[O]** open. Pieces: `modules/fx/freqshift/module.toml` and README,
`tools/patchkit/pk_digikit_fxshift.py` (scenarios `fx-freqshift-dt2` and `-dn2`), `tests/support/freqshift_demo.py --send` (a listening demo).

## The manifest **[D]**

- `route = "send"`: a send effect has no dry path, so MIX is the return level; with the old default 0 the effect would add nothing. The default is MIX 127.
- `routes = ["send", "master"]`: the user may move it to the master in DIGIKIT > FX ROUTES. On the master MIX 127 is the fully shifted sound (SHIFT 0 passes it through the allpass pair, flat to 0.5 %), and **MIX is one value for both routes**: after the move, turn MIX down to hear dry sound with the shifted one.
  A kit saved before this change holds MIX 0 for the effect and keeps it by the persistence rule of finding 61 (the saved value wins over the default; **[O]** not measured here).
- **[C]** Step 6 (finding 67): the Digitone II has send cells (one), so the send default holds there too. Before step 6 the default fell back to the master (`fxsend::apply`) and the effect behaved as before, except that its MIX started at 127.

## Proof **[D]** (scenarios `fx-freqshift-dt2` PASS in 141 s with 45 checks, `fx-freqshift-dn2` PASS in 81 s)

Master route (Digitakt II: the settings record on the card stores `fx/route/digikit/freqshift` = master before the boot; Digitone II: the fallback, route word 0, CAPACITY `SEND_SLOTS` limit 0 used 0):

- Defaults: SHIFT raw 0x4000, MIX raw 0x7f00, p0 0.0, mix word 1.0, route word 0 with no amounts. MIX 127 and SHIFT 0 keep the sine: amplitude ratio 1.00000 (DT2) and 1.00004 (DN2), RMS ratio 1.0005 and 1.0006.
- MIX turned to the bottom: raw 0, mix word 0.0, and the PCM is the dry PCM bit for bit (onset shift 0, max error 0.0); back to the top: raw 0x7f00, mix word 1.0.
- SHIFT moves the sine by the routine's mapping in both directions with the image 70.1 dB down (as before, now through the settings route): DT2 -38 gives -75.085 Hz, +42 gives +124.717 Hz; DN2 +40 gives +96.790 Hz, -39 gives -85.255 Hz. Save Kit and Load Kit give SHIFT and MIX back.

Send route (Digitakt II, no setting, so the manifest's route; boots `send` and `other`):

- Route word 1, amount words 0, p0 0.0, mix word 1.0; CAPACITY `SEND_SLOTS` 2 with one used; the FX key's count cell 2. Page 2 shows `FX (2/2)` with the knob labelled `FSH`; turning it on track 1 to 40 shows `FSH Send=40` and puts the amount 40 in the DSP row (the other 15 tracks 0).
  Screenshots: `out/runs/lanes/fxr-shots/step5-send-page2.png`, `step5-send-turning.png`, `step5-fsh-page-send.png` (the effect's own page, `Freq Shifter (4/4)`).
- The FX return is the shifted track. With amount 40 on track 1 (the sine, f0 261.63 Hz), SHIFT +38 (+75.085 Hz) and MIX 127, the PCM minus the dry PCM (aligned at the onset) is a sine at 336.71 Hz of amplitude 0.006206 = (40/127)^2 x 1.0 x the dry amplitude 0.0626 (ratio 1.00001), the image at 186.54 Hz 70.1 dB down, nothing at f0.
  The same measure on the float32 model (`freqshift_ref.Shifter` on the dry sound times (40/127)^2, from silent state): the shifted tone's level 1.00001 and the image level 1.000 of the measured. The return is not compared sample by sample: the oscillator's phase at the note onset depends on the blocks since the effect switched on.
- MIX at raw 0x4000 (mix word 0.50394): the added level is 0.50394 of MIX 127's (limit 0.5 %). The tone level is compared with the model times the mix word.
- Other tracks: an amount of 100 on track 2 only (nothing plays there) leaves the PCM of track 1 equal to the dry PCM bit for bit, with the shifter running on an empty bus.

## Listening demo **[D]**

`PYTHONPATH=<repo>:<repo>/tests uv run --no-project --python pypy3.11 python -m support.freqshift_demo --send --seconds-per-part 4.8 --out FILE.wav`: kick and hat and a bass line stay dry, a plucked lead line sends to the shifter (amount 0, then 127 with +3 Hz, +100 Hz, -300 Hz, +1 kHz, a sweep from -48 to +48, then
amount 64 at +100 Hz). It runs the real send hook (`core/fx` `fxsend.s`) and the real routine on the concrete SHARC+ core (`tests/support/fx_rig.py`), not the emulator, and adds the return to the dry mix at unity as the master function does. Output: `out/runs/lanes/fxr-shots/fshift-send-demo.wav` (not committed).

## Open **[O]**

- **[C]** Step 6 is done (finding 67): both scenarios run the master proof with the stored `master` and the send proof; `image_send` (the limit the image reports) is the device's `send_slots`.
- A Digitone II kit or a Digitakt II kit that stored MIX 0 for the effect keeps MIX 0: on a send it adds nothing until MIX is turned up (the saved value wins).
- MIX shared by both routes may surprise a user who moves the effect to the master (fully shifted, no dry part); the README says so. A per-route MIX would need a second default in the manifest.
