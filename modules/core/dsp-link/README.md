# DSP link

`core/dsp-link` is a framework module: it lets the digikit runtime send bytes (DSP code and data) to the DSP while
the device runs. Nothing changes for the player. Emulator only.

- Firmware: Digitakt II 1.16 and 1.17, Digitone II 1.11 and 1.12 (the same anchor rules on all four).
- What it adds: a hook on the ColdFire's per-block frame sender (to put a chunk in the unused end of the frame) and a hook
  at the end of the DSP's block handler (to take the chunk, copy it into a reserved area of free L2 (512 KiB on the Digitakt II, 320 KiB on the Digitone II), and
  acknowledge). The ColdFire interface is `dl_send(dst_l2, buf, len)`, `dl_send_win(win, dst_word, buf, len)` (window 1: the voices registry of
  `core/dsp-voice`, one word at a time) and `dl_poll(status)` (C ABI).
- Speed: about 960 KB/s on the Digitakt II and 72 KB/s on the Digitone II; with no transfer it costs the DSP about 25
  instructions per block (about 0.004 % of a block's cycles).
- Limits: the destination must lie in the module's area (`_dl_area`); the DSP code that runs from it must be linked for
  that address or relocated by the runtime (`docs/design/digikit-dkm.md`, "DSP section"). The emulator does not model the DSP caches, so the cache invalidate after the last chunk has not run on
  a device.

Design, measurements and open points: [docs/findings/31-dsp-link.md](../../../docs/findings/31-dsp-link.md) and, for the windows and
the wider chunk header, [docs/findings/32-runtime-dsp-machines.md](../../../docs/findings/32-runtime-dsp-machines.md). Proof:
`uv run python -m tools.patchkit.pk_dsplink proof --device dt2|dn2`; tests: `tests/patchkit/test_dsplink.py`.
