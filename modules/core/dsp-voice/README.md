# DSP voice hook

**Category:** core  **Devices:** Digitakt II 1.16, 1.17; Digitone II 1.11, 1.12  **Status:** experimental (emulator only)

A framework module: it lets other modules render a machine's sound with their own DSP routine (`machines/pluck` is the
example). On its own it changes nothing: a patched image with no registered machine plays exactly the stock audio.

- What changes on the device: nothing you can hear. The first instruction of the DSP's per-voice render function is
  replaced by a jump into this module, which looks the voice's machine type up in a table and runs the registered routine,
  or continues in the stock code.
- The DSP records, once per block, a heartbeat and the context registers it found (see `core/dsp-link`, `dl_diag`).
- Limits: at most 16 registered types; the Digitakt II's Stretch machine (type 2) is not hooked; type 0 cannot be registered.

## For module authors: the routine's register contract

The firmware restores a number of registers per DSP context, so the wrapper never assumes their values: before a registered
routine runs it saves them and **sets** them, and it puts the caller's values back when the routine returns.

| register | value while the routine runs |
| --- | --- |
| M5, M6, M7 | 0, 1, -1 (a post-modify of `M6` steps one word; `M7` steps back one) |
| M13 | 0 (`JUMP (M13, I13)` is the return) |
| M14, M15 | 1, -1 |
| L0-L5 | 0 (linear addressing for I0-I5) |
| MODE1 | SIMD off, ALUSAT and TRUNCATE clear, RND32 set (32-bit floats, round to nearest) |

The routine may clobber R0-R15, I0-I5, I13, M1-M4 and B3, and must keep I6-I12, I14, I15, M0, M5-M15, the L registers and
MODE1 as it found them (the pinned values above). Details, the entry registers and the voice record: the header of
`src/dsp/dsp-voice.s` (Digitakt II) and `src/dsp/dsp-voice-dn2.s` (Digitone II); the audit and its tests:
[docs/findings/45-dsp-voice-context.md](../../../docs/findings/45-dsp-voice-context.md).

The gallery shows the text above the rule. Keep it free of firmware bytes and vendor tool names.
