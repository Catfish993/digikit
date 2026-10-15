# Samples for the emulated +Drive

Put WAV files in a local `samples/` directory at the repository root (ignored by git; create it if it is missing).
WAV files there become the emulated Digitakt II's +Drive: `tools/firmware/plusdrive.py
build samples/ -o out/plusdrive/dt2.img` reads every `.wav` file directly in
this folder (not recursively) and writes them into a card image in the
firmware's own +Drive filesystem format
(`docs/findings/14-plus-drive-format.md`), which the emulator can then boot
from (`--card-image out/plusdrive/dt2.img` on the runners that accept it).

**Nothing in `samples/` is committed** (`.gitignore` excludes the
directory), including the two test files below -- generate them
instead of expecting them to already be here:

```
uv run python -m tools.emulator.gen_test_samples
```

That writes:

- `test-sine-1khz.wav` -- a 1 kHz sine, 48 kHz/16-bit/mono, 0.5 s.
- `test-perc-click.wav` -- a short exponentially-decaying click (a low thump
  plus a little noise), same format, 50 ms -- stands in for a percussive
  drum sample without needing a real recording.

Both are fully synthetic and safe to regenerate at any time; add your own
`.wav` files alongside them for a real test.

`tools/firmware/plusdrive.py` converts each input WAV to the drive's native sample
format (the one the firmware's recorder writes and its loader reads): 48 kHz,
16-bit big-endian PCM, mono or stereo, behind a 64-byte header. Other rates
are resampled and other bit depths (8/24/32-bit integer, 32/64-bit float) are
converted; more than two channels is refused.

Unless `--no-project` is given, the image also carries an active project (the
firmware's built-in one, taken from `out/sections/dt2-1.16/` at build time)
whose sample references all point at the first file in name order, so the
firmware loads that file into every track's slot at boot with no UI.
