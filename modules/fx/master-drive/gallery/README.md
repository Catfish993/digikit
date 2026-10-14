# Master drive

**Category:** FX  **Devices:** Digitakt II 1.17, Digitone II 1.12  **Status:** experimental (emulator only)

Turn the master page's encoder 7 up past its default and the master bus saturates: a soft clipper with up to
+18 dB of pre-gain, level-compensated, smooth when you move the knob. At the default (UI 80) and below the
audio is bit-identical to the stock firmware.

The knob is the master compressor's Sidechain Filter, relabelled `DRV` / `Master Drive`. If you also mix the master
compressor in (its MIX knob above 0), that knob keeps moving the compressor's sidechain filter as well.

Changes both processors: new DSP code in free DSP memory, and two label strings plus two pointers on the ColdFire
side. Cannot be combined with `fx/master-gain` (both own the master hook).

Not tested on hardware.
