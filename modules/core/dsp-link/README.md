# DSP link

`core/dsp-link` is a framework module: it lets the digikit runtime send bytes (DSP code and data) to the DSP while
the device runs. Nothing changes for the player. Emulator only.

- Firmware: Digitakt II 1.16 and 1.17, Digitone II 1.11 and 1.12 (the same anchor rules on all four).
- What it adds: a hook on the ColdFire's per-block frame sender (to put a chunk in the unused end of the frame) and a hook
  at the end of the DSP's block handler (to take the chunk, copy it into a reserved area of free L2 (512 KiB on the Digitakt II, 320 KiB on the Digitone II), and
  acknowledge). The ColdFire interface is `dl_send(dst_l2, buf, len)`, `dl_send_win(win, dst_word, buf, len)` (window 1: the voices registry of
  `core/dsp-voice`, one word at a time) and `dl_poll(status)` (C ABI).
- Speed: about 960 KB/s on the Digitakt II and 72 KB/s on the Digitone II; with no transfer it costs the DSP about 80
  instructions per block (about 0.04 % of a block's cycles: 24 for the link itself, 23 to pin and restore M5, M6, L2 and L3, 33 for
  the diagnostic words below; finding 45).
- Diagnostics: `int dl_diag(u32 *out, u32 n)` (ColdFire C ABI, optional link) returns the DSP's diagnostic words of the last
  block, 14 words, or a negative number when there are none (-1: no record address in the image; -2: the reply carries no diagnostic
  block, `core/dsp-voice` is not in the image or the DSP has stopped writing it). Layout of the words:

  | word | content |
  | --- | --- |
  | 0 | `0xD1A6` in the low half, the word count (14) in the high half |
  | 1 | blocks that ran the link's hook (a heartbeat of the block handler) |
  | 2 | blocks that reached `core/dsp-voice`'s hook (its heartbeat) |
  | 3-8 | M5, M6, M7, M13, M14, M15 |
  | 9-13 | I6, I7, B7, L7, MODE1 |

  Words 3-13 are the registers as the firmware left them at the first voice of the block, before the wrapper pins anything. A
  frozen heartbeat is a halted core. The words travel in the DSP's reply record, two per slot row (rows 1 to 7, bytes +0x44 and +0x48 of
  the row, the entries 11 and 12 that no DSP code writes); the exact offsets are in `src/dsp/dsp-link.inc`, and `tools/patchkit/pk_ctxprobe.py`
  reads them in the emulator.
- Limits: the destination must lie in the module's area (`_dl_area`); the DSP code that runs from it must be linked for
  that address or relocated by the runtime (`docs/design/digikit-dkm.md`, "DSP section"). The emulator does not model the DSP caches, so the cache invalidate after the last chunk has not run on
  a device.

Design, measurements and open points: [docs/findings/31-dsp-link.md](../../../docs/findings/31-dsp-link.md) and, for the windows and
the wider chunk header, [docs/findings/32-runtime-dsp-machines.md](../../../docs/findings/32-runtime-dsp-machines.md). Proof:
`uv run python -m tools.patchkit.pk_dsplink proof --device dt2|dn2`; tests: `tests/patchkit/test_dsplink.py`.
