# digikit/remote

Presses keys and turns encoders from a computer, over the USB MIDI SysEx channel the digikit runtime already speaks. A run-time module (`.dkm`),
loaded like any other from the card store; it needs `core/input` in the image (both device patch sets have it).

| Command | Number | Body | Reply |
| --- | --- | --- | --- |
| KEY | 0x38 | code, down (0 or 1) | status 0 |
| TURN | 0x39 | encoder (1 to 9), delta (signed byte) | the delta taken (signed byte), at most 30 either way; the host sends the rest again |
| RELEASE | 0x3a | none | status 0: every key this module pressed is released |
| STATE | 0x3e | none | enabled (0 or 1), state format (1), turn limit (30) |

- **Codes** are the device file's `[panel]` codes (`devices/*.toml`); the host lays the keys out from them. The module knows no device.
- **Guard.** KEY, TURN and RELEASE are in a guarded range: while DIGIKIT > REMOTE is off, the runtime answers `E_DISABLED` (-65). STATE is always answered, so a host can show the state.
  The switch starts on in the emulator and off on a device, and is never saved.
- **Pacing.** The firmware flushes a turn at most 30 detents every 4 ticks. The module keeps an estimate of what is still pending per encoder and takes only what fits.
- **Held keys.** The module remembers the keys it holds. RELEASE lets go of all of them (a host sends it when its Remote page closes).

Design: `docs/plans/remote-control.md`.
