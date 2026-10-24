# Frequency shifter

**Category:** fx  **Devices:** Digitakt II 1.17, Digitone II 1.12  **Status:** experimental (emulator only)  **Kind:** run-time module (`.dkm`)

Moves every frequency of the master output up or down by the same number of Hz. This is a frequency shift, not a pitch shift: a note
with overtones at 100, 200 and 300 Hz that is shifted by +50 Hz has them at 150, 250 and 350 Hz, so a small shift makes a sound
slightly out of tune and inharmonic, and a large shift turns it into a metallic, bell-like or robotic sound. A shift of a few Hz mixed
with the dry sound is a slow phaser-like beating. The effect is a single-sideband modulator: the input is split into two signals 90 degrees apart (a pair of
allpass filters), which are modulated by a sine and a cosine of the shift frequency and added. The image on the other side of the input frequency is
at least 40 dB down (46 dB from 30 Hz to 18 kHz).

The effect has a page of its own in the FX view (FUNC + FX, then UP or DOWN after CHO): tab **FSH**, title **Freq Shifter**.

- **SHIFT** (first knob, shown from -64 to +63, 0 in the middle): the shift. 0 is no shift (the sound passes through the allpass pair, magnitude flat). Positive values move the frequencies up,
  negative values down. One step near 0 is about 0.1 Hz, and the steps grow exponentially to about +-2 kHz at the ends. The knob is a sweep: turning it ramps
  the shift inside each block of 32 samples and never jumps the phase, so there are no clicks.
- **MIX** (the framework's level knob): the blend of the dry and the shifted sound. It starts at **0**, so loading the effect does not change the sound until MIX is turned up.
  MIX 127 is only the shifted sound, 0 only the dry sound (bit for bit).

| SHIFT | shift | | SHIFT | shift |
| ---: | ---: | --- | ---: | ---: |
| 0 | 0 Hz | | +40 | +96.8 Hz |
| +1 | +0.085 Hz | | +48 | +266 Hz |
| +4 | +0.41 Hz | | +56 | +731 Hz |
| +8 | +1.10 Hz | | +63 | +1,767 Hz |
| +16 | +4.10 Hz | | -64 | -2,005 Hz |
| +24 | +12.3 Hz | | -40 | -96.8 Hz |
| +32 | +34.9 Hz | | -1 | -0.085 Hz |

The formula is in the cookbook (docs/findings/54-module-cookbook.md): `f = sign(v) 0.63 (2^(|v| / 5.5) - 1)` Hz.

Things to know:

- The input is the whole master mix (after the master mix and before the output), so every track and the send effects are shifted together. A long reverb tail that is shifted keeps moving.
- Frequencies above 18 kHz and below 30 Hz are shifted with less image rejection (the image is still 5 dB down at 5 Hz); a DC offset becomes a tone at the shift frequency.
- A shift upward moves high frequencies past 24 kHz, where they fold back down (aliasing): a high-pitched sound shifted up by 2 kHz gets a faint reflection.
- It costs about 4,100 DSP instructions per block of 32 samples (about 2 % of the busy budget of one effect slot; the declared maximum is 4,200).

A run-time module: it is not patched into the firmware. The digikit runtime loads the `.dkm` from the card and sends the DSP code to the DSP. Pack it with
`mise run module:pack -- digikit/freqshift --device dt2` (or `dn2`) and upload `out/patch/freqshift-dt2.dkm`, or load it with `mise run dk:run -- digikit/freqshift --emu`. The image needs the digikit runtime with
`core/dynfx` and `core/fxpages` (the `emulator` and `device` patch sets of both devices have them).

Source and reference: `src/dsp/freqshift.s`, the float32 model `tools/patchkit/freqshift_ref.py` (defines the arithmetic; `mise` is not needed to run it), the tests `tests/patchkit/test_freqshift.py`,
`tests/patchkit/test_freqshift_ref.py` and the emulator scenario `tests/digikit/scenarios/fx-freqshift-dt2.toml` (and `-dn2`). A demo render for listening: `tests/support/freqshift_demo.py`.
