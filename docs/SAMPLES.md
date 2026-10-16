# The emulated +Drive: `plus_drive/` and `samples/`

`mise run emu` builds the emulated +Drive from a folder, `plus_drive/<device>/`
(`plus_drive/dt2/` for the Digitakt II, `plus_drive/dn2/` for the Digitone II),
ignored by git. The device is the one the firmware file belongs to. Use
`mise run emu -- --plus-drive DIR` for another folder (`--samples DIR` is the same
option, kept for the old `samples/` flow). A folder that is missing or holds nothing
the device can use leaves the emulator on its built-in empty card; `--card-image`
overrides the folder. The image is built by `packages/emulator/plusdrive`
(`plusdrive build`, also `mise run plusdrive -- build DIR -o IMAGE --syx FW`) and cached by
content in `out/plusdrive/native/<key>/<device>.img` (key: firmware, builder
binary, device, every file's relative path and bytes).

## Folder contract

- **Files and folders map to the path filesystem as they are** (DT2). Nested folders,
  empty folders, directories of up to 4,092 entries, files of any size up to the card.
  Symbolic links, `.DS_Store` and non-regular files are reported and skipped.
- **A `.wav` is converted to a native sample**: 48 kHz, 16-bit big-endian PCM, mono or
  stereo, a 0x40 header and a 0x10 trailer. The stored name loses the `.wav` extension
  (case-insensitive), non-ASCII characters become `?`, 255 bytes at most. Two names that
  become equal in one folder are an error.
- **Any other file is copied byte for byte** and keeps its name (same character rule). If
  the bytes pass the native-sample check (the check the builder applies to every sample)
  it is a native sample, otherwise a plain file; plain files are not given to the boot project.
- **`.mmcfs/projects/NNNN.bin`, `.mmcfs/sounds/NNNN.bin`, `.mmcfs/kits/NNNN.bin`**: raw
  slot contents of the three MmcFs pools, NNNN = the slot index (4 decimal digits; projects
  0 to 127, sounds 0 to 2047, kits 0 to 1023). The builder writes each into its fixed
  slot and sets its bit in the pool occupancy bitmaps (sector 0x800). A file larger than
  its slot (DT2: project 16 MiB, sound 4 KiB, kit 64 KiB; DN2: 16 MiB, 1 KiB, 32 KiB) is an
  error. Anything else under `.mmcfs/` is reported and skipped.
- **The boot project** (the active project at sectors 0x40000 and 0x48000, DT2 only) is
  generated when the folder holds at least one native sample: its references point at the
  samples in id order. It lies outside the three pools, so `.mmcfs/projects/` cannot collide
  with it: both are written. (Plan decision 6 says the folder wins on a collision; there is
  none by sector. A way to replace the boot project itself is not defined yet.)
- **DN2 has no path filesystem**: only `.mmcfs/` applies; every other file is reported
  and skipped. DN2 pool addresses are from the 1.11 decompile.

## `samples/` (the older flow)

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
