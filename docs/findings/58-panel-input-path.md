# 58. The panel input path (DT2 1.17, DN2 1.12)

Plan: `docs/plans/remote-control.md` (F1). Marks as in AGENTS.md: **[D]** read or measured once here, **[O]** open, **[C]** corrects an earlier claim, **[V]** checked by a second agent against the image bytes
and the decompiles (2026-10-22, static only; the items marked [V] below). Emulator only; the device has not run any of it.

## Result

**[D]** The firmware turns the panel MCU's UART packets into events with two plain C functions that a module can call. Calling them with the previous-mask state
they keep in RAM (`core/input`) produces the screens that the emulator's own `input.button` / `input.turn` produce. The same code, the same hashes on both devices.

## The path

- **[D]** The panel MCU sends 2-byte packets on UART8. A key packet is `0x20 | channel`, then the 8-bit button mask of that channel; an encoder packet is `0x30 | (encoder - 1)`,
  then a signed 8-bit delta. A key code is `channel * 8 + bit + 1` for the linear channels (codes 1 to 48 on both devices); `devices/*.toml` `[panel.exceptions]` lists the others
  (DT2: 49 = (6, 1), 50 = (6, 0); DN2 adds 51 to 54 = (6, 4 to 7)). Nine encoders, ids 1 to 9.
- **[V]** The button decoder `void f(u32 channel, u32 new_mask)`: DT2 1.17 `0x4011e318`, DN2 1.12 `0x4011fc8c`; cfdb `h_strict` `157be8bc0a941e9aa4c960f56894c227`, unique in each image (181 instructions, 640 bytes; also resolved on 1.16
  and 1.11 by the same rule **[D]**). It compares the new mask with the channel's previous one (a byte at `0x44dc8644 + channel` on DT2 1.17), updates the
  previous one, and queues one 16-byte record per changed bit through the queue-send function (`jsr (a4)` = `FUN_40001896` on DT2 1.17) onto the UI queue. Chords, key repeat and the
  key-down query (`FUN_4011e150`) read the same previous-mask state. A record is `+0` type (0 for a key), `+4` key code, `+8` state flags (1 or 5: a repeat window decides), `+12` a timer value.
- **[V]** The encoder decoder `void f(u32 encoder0, int delta)`: DT2 `0x4011e598`, DN2 `0x4011ff0c`; `h_strict` `173900efe85a324d026b5b84e6c72999`, unique (61 instructions). It adds the delta to a pending count; a
  periodic tick flushes it, clamped to +-30 when `DAT_402b4590` (DN2 `0x4029f0c0`) is 4, its initial value (so every 4 ticks). A turn shows on the screen some emulated seconds later.
- **[V]** Calling convention: both arguments are pushed as 32-bit longs, the value first, the caller cleans up. Both decoders clobber D0, D1, A0, A1 and CCR, and use no EMAC or FPU. The encoder delta is sign-extended
  (`mvs.b`); the button mask is used as a byte. (D2-D7 and A2-A6 are saved by the callee **[D]**.)
- **[V]** The mask base is DT2 `0x44dc8644`, DN2 `0x445a2e30`: the imm32 at decoder offset `+0x10` (the `lea abs.l,A0` at `+0xe`). The key-down query (DT2 `0x4011e150`, DN2 `0x4011fac4`) reads it. A `cf_func` anchor of 4 bytes
  at `+0x10` of the hashed decoder (`input.fw.key_mask_lit`) locates the state without an address in the code: this is how `core/input` anchors it today. The UI queue object is read as the runtime's boot hook reads it (`dkrt.ui.queue_lit`): count at `+4`, mask at `+16`, so room is `mask + 1 - count`.
- **[V]** The UART8 byte callback is `0x4011e648` (DN2 `0x4011ffbc`), `h_strict` `8a1e573c...`, registered with `FUN_4000243e(0x2625a, cb)`. It dispatches the high nibble 2 to buttons, 3 to the encoder and 7 to the console.
- **[V]** `queue_send` at `0x40001896`: it sets SR to `0x2700`, has no full check, and the record offsets are as written above.
- **[V]** The key code tables are DT2 `0x4020b150`, DN2 `0x401f3dec`; the encoder maps DT2 `0x4020af94`, DN2 `0x401f3c10` (identity 1 to 9).
- **[D]** The byte callback shares header state with the interrupt; `core/input` never calls it, only the two decoders, from the MIDI task, with SR masked to `0x2700`.

## Limits of the decoders

- **[V]** Neither decoder checks the channel. The tables are indexed `channel * 32` for buttons and `channel` for encoders. Safe values: button channels 0 to 6, encoders 0 to 8 (ids 1 to 9).
- **[V]** DN2 channel 6, bits 2 and 3 map to key code 0: toggling them queues a key-0 event. No code of the profile reaches them.
- **[V]** An encoder turn is dropped for 40 ticks after the release of that encoder's click key (the array at `75b0`).
- **[V]** The decoders do unprotected read-modify-write. Preemption by the tick and by the UART ISR is **[O]**; the runtime masks SR to `0x2700` around the call, which covers it.
- **[V]** Cleaner anchors than function addresses are the `lea` immediates inside the hashed decoder: on the button decoder `+0x10` is the mask base and `+0x5a` the key code table; on the encoder `+0x10` is the map and `+0x20` the
  click-release ticks. `core/input` uses the first of these (`input.fw.key_mask_lit`); it needs no other.

## What `core/input` does

`input_key(code, down)` maps the code to (channel, bit) from the device profile, reads the channel's previous mask, sets or clears the bit and calls the decoder only when the mask changed;
`input_turn(encoder, delta)` splits a delta into parts of at most 127. Both check the UI queue for room (`-2` when full), refuse a code that is no key or an encoder that does not exist (`-1`), and
return `-3` when the device profile names no backend this module has. Backend and key layout are the device file's (`[input] backend = "panel-uart"`, `[panel]`).
Before it calls a decoder it refuses a channel at or above the profile's `[panel] button_channels` (7 on both devices: the decoder's tables) and an encoder above `encoders` (9); a code that maps to a hole (DN2 channel 6, bits 2 and 3) is
not in the profile, so it is refused too (the profile generator also rejects an exception on a channel outside `linear_channels <= channel < button_channels`).

**[D]** Proof: scenario `input-adapter` (both devices, full tier): the same key, turn and second key on two free-running daemons, once through the emulator's panel path and once through
`test/input-probe` (SysEx commands calling `input_key` and `input_turn`); the screens (OLED hash) match after every step.

## Above the adapter: digikit/remote, the guard, the screen

- **[D]** `digikit/remote` (a run-time module, one `.dkm` for both devices) registers KEY, TURN, RELEASE (guarded range `0x38..0x3d`) and STATE (`0x3e`) and calls `input_key` / `input_turn`. A TURN takes at most
  30 detents per request (the firmware's flush clamp); the reply says how many it took, and the clients send the rest again. Scenario `remote-control` (both devices, device patch set): REMOTE is off at the boot, STATE says
  so, KEY / TURN / RELEASE answer `E_DISABLED` (-65), the DIGIKIT > REMOTE row turns the switch on (the commands work, a TURN of 100 is taken as 30 and then 0), and off again.
- **[D]** Scenario `remote-keys` (both devices, emulator patch set, REMOTE on at the boot): KEY presses open SETTINGS > DIGIKIT > ABOUT and the rows the menu keeps (`DK_ABOUT`) hold the runtime version
  (`expect_text`). On the main screen a TURN of 20 detents on encoder 1 (TUNE of the SRC page) changes the screen: the value label shows, and goes again after a few emulated seconds, so the scenario polls
  the frame hash for 8 s of wall time instead of looking once (a single look failed on DN2 when the emulator ran faster; the emulator runs below real time, and a turn reaches the screen through the firmware's flush tick).
- **[O]** The value is not understood yet: after +20 the screen showed the value label `0.00` and no title bar (touch only); after -20 it showed `Tune=-0.07` in some runs and not in others; the emulator's own
  `input.turn(1, d)` for d = 20, -20, 20, 5, 5, -5, 40 left the screen at the `0.00` label every time. So the detent-to-value mapping (acceleration? a first turn that only touches?) is open; the proof above says
  that a turn reaches the firmware and changes the screen, not what value it sets, and a turn back is not proved.
- **[D]** An encoder does not scroll the DIGIKIT menu (one detent on each of the nine encoders left the screen as it was, DT2 1.17, emulator's own `input.turn`): menus move with the arrow keys.
- **[D]** The stock Screenshot (MidiRpc 0x04) answers on DN2 1.12 in the emulator as on DT2 (`upload-screenshot`, both devices): the 1024 bytes equal the frame capture.
- **[O]** None of this has run on a real device: not the key and turn calls, not the screenshot on a DN2, not the MIDI port path of the app.

## Open

- **[O]** On a real device the panel's next packet for a channel carries the whole mask and overrides a bit set here; the emulator has no panel MCU. Whether the decoders are safe to call from the MIDI task while
  the UART task runs on a device is not measured (the SR mask is the discipline of the runtime's other calls into the queue code).
- **[O]** A second agent has not checked the hashes, addresses and offsets against the image bytes.
