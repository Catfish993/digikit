# 58. The panel input path (DT2 1.17, DN2 1.12)

Plan: `docs/plans/remote-control.md` (F1). Marks as in AGENTS.md: **[D]** read or measured once here, **[O]** open, **[C]** corrects an earlier claim; nothing is
**[V]** (a second agent has not checked these against the image bytes). Emulator only; the device has not run any of it.

## Result

**[D]** The firmware turns the panel MCU's UART packets into events with two plain C functions that a module can call. Calling them with the previous-mask state
they keep in RAM (`core/input`) produces the screens that the emulator's own `input.button` / `input.turn` produce. The same code, the same hashes on both devices.

## The path

- **[D]** The panel MCU sends 2-byte packets on UART8. A key packet is `0x20 | channel`, then the 8-bit button mask of that channel; an encoder packet is `0x30 | (encoder - 1)`,
  then a signed 8-bit delta. A key code is `channel * 8 + bit + 1` for the linear channels (codes 1 to 48 on both devices); `devices/*.toml` `[panel.exceptions]` lists the others
  (DT2: 49 = (6, 1), 50 = (6, 0); DN2 adds 51 to 54 = (6, 4 to 7)). Nine encoders, ids 1 to 9.
- **[D]** The button decoder `void f(u32 channel, u32 new_mask)`: DT2 1.17 `0x4011e318`, DN2 1.12 `0x4011fc8c`; cfdb `h_strict` `157be8bc0a941e9aa4c960f56894c227`, identical and unique on
  both images (and resolved on 1.16 and 1.11 too, by the same rule). It compares the new mask with the channel's previous one (a byte at `0x44dc8644 + channel` on DT2 1.17), updates the
  previous one, and queues one 16-byte record per changed bit through the queue-send function (`jsr (a4)` = `FUN_40001896` on DT2 1.17) onto the UI queue. Chords, key repeat and the
  key-down query (`FUN_4011e150`) read the same previous-mask state. A record is `+0` type (0 for a key), `+4` key code, `+8` state flags (1 or 5: a repeat window decides), `+12` a timer value.
- **[D]** The encoder decoder `void f(u32 encoder0, int delta)`: DT2 `0x4011e598`, DN2 `0x4011ff0c`; `h_strict` `173900efe85a324d026b5b84e6c72999`. It adds the delta to a pending count; a
  periodic tick flushes it, clamped to +-30 every 4 ticks. A turn shows on the screen some emulated seconds later.
- **[D]** Calling convention: plain C (stack arguments after the prologue's `movem`: `4(sp)` the first, `8(sp)` the second; the result in d0; d2-d7 and a2-a6 saved by the callee), read from the
  disassembly of the DT2 function.
- **[D]** The literal of the previous-mask table is at function offset `0x10` on both devices (`lea (<table>).l,a0`): a `cf_func` anchor of 4 bytes there (`input.fw.key_mask_lit`) locates the state without
  an address in the code. The UI queue object is read as the runtime's boot hook reads it (`dkrt.ui.queue_lit`): count at `+4`, mask at `+16`, so room is `mask + 1 - count`.
- **[D]** The byte callback of the UART (`FUN_4011e648`) shares header state with the interrupt; `core/input` never calls it, only the two decoders, from the MIDI task, with SR masked to `0x2700`.

## What `core/input` does

`input_key(code, down)` maps the code to (channel, bit) from the device profile, reads the channel's previous mask, sets or clears the bit and calls the decoder only when the mask changed;
`input_turn(encoder, delta)` splits a delta into parts of at most 127. Both check the UI queue for room (`-2` when full), refuse a code that is no key or an encoder that does not exist (`-1`), and
return `-3` when the device profile names no backend this module has. Backend and key layout are the device file's (`[input] backend = "panel-uart"`, `[panel]`).

**[D]** Proof: scenario `input-adapter` (both devices, full tier): the same key, turn and second key on two free-running daemons, once through the emulator's panel path and once through
`test/input-probe` (SysEx commands calling `input_key` and `input_turn`); the screens (OLED hash) match after every step.

## Open

- **[O]** On a real device the panel's next packet for a channel carries the whole mask and overrides a bit set here; the emulator has no panel MCU. Whether the decoders are safe to call from the MIDI task while
  the UART task runs on a device is not measured (the SR mask is the discipline of the runtime's other calls into the queue code).
- **[O]** A second agent has not checked the hashes, addresses and offsets against the image bytes.
