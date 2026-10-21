# Remote control of the device: panel input, screen, and a Remote page (F1, 2026-10-22)

Status: in progress on lane `tasks-images`, one commit per step. Rule from Em: stay device-agnostic (Syntakt, Rytm MK2 and maybe Octatrack may follow):
device facts in `devices/*.toml`, firmware addresses as per-version anchors, modules and hosts see only neutral APIs, a profile chooses a backend,
no device literals in code. A third device must be a profile, anchors and at most a backend.

## Steps

1. **W4: the command table and service commands** (runtime 0.2.5, api 1.2). Done. `dkrt_midi_rx` looks commands up in a static table; a module registers commands
   with `Api.register_cmds` inside a range `sdk/sdk.toml` `[protocol.ranges]` reserves (`remote = 0x38..0x3d` guarded, `remote_state = 0x3e..0x3f`; a request number is 6 bits, bit 6 marks a reply, so the
   range cannot start at 0x40 as first sketched). Wire side: `docs/design/digikit-upload.md` ("Service commands"); API: `docs/design/module-sdk.md` 11.6.
2. `core/input`: the input adapter (anchors for the panel decoders, `input_key(code, down)`, `input_turn(enc, delta)` as imports, a profile-chosen backend).
3. `digikit/remote` (`modules/tools/remote`): a `.dkm` service with KEY, TURN, RELEASE and STATE behind a REMOTE switch in the DIGIKIT menu. The runtime guards the range (`E_DISABLED`); the switch starts on when `core/remote-open` is in the patch set (the emulator's), off otherwise (the device's, which now has `core/input`). Done.
4. The DN2 screenshot (stock MidiRpc 0x04).
5. Host side: the commands in the TypeScript and Python clients, a target selector (Emulator or MIDI port through Rust midir), and a Remote page.
6. Proof scenarios on both devices and `docs/findings/58-panel-input-path.md`.
