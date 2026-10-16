Scales the final stereo block by a gain word, right before the DSP converts it to the 24-bit output.

- Gain 1.0 (the value the module is built with) is **bit-identical** to the stock output.
- Gain 0.5 lowers the level by exactly 6.02 dB.
- Works on the Digitakt II 1.17 and the Digitone II 1.12 (the same routine, placed in free DSP memory).

This is a building block and a proof of the patch flow: it adds no knob yet, so the built-in gain is unity. It
changes only the DSP image, not the main operating system.
