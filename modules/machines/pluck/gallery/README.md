# Pluck

**Category:** machines  **Devices:** Digitakt II 1.16, 1.17  **Status:** experimental (emulator only)

A new machine in the SRC machine list: a plucked string (Karplus-Strong). Every track with the PLUCK machine
is one string tuned to the note you play, so it follows the keyboard, the sequencer's notes and the TUNE knob. The
sample is not used.

The SRC page has these knobs (UI 0 to 127):

| Knob | Name | What it does | Default |
|---|---|---|---|
| A | TUNE | fine tuning in the usual SAMPLE way | 64 (no offset) |
| B | DECAY | how long the string rings: about 0.25 s at 32, 1 s at 64, longer above | 64 |
| C | PICK | where the string is plucked: 0 is the end (bright), 63 is the middle (hollow, even harmonics soften) | 32 |
| E | BRIGHT | damping of the string: low is a dull thump, high rings with all its harmonics | 80 |
| F | COLOR | softness of the excitation: 0 is a sharp click, high is a soft finger | 40 |
| H | LEV | track level | 100 |

D and G are empty. The track's amp envelope, filter, FX sends and reverb work on it as on any machine.

Needs `core/machines`, `core/params` and `core/dsp-voice`, which patchkit adds for you. Changes both processors: ColdFire
tables for the machine and its four parameters, and DSP code in free DSP memory.

Projects saved with a PLUCK track keep the machine and its knob values when loaded on the same patched firmware. On
stock firmware (or a firmware patched with other machines) the track loads as SAMPLE and its values land in SAMPLE's
slots; saving there removes the machine.

MIDI CC and NRPN do not reach the machine's knobs yet (stock tracks take them through a table the patch does not
extend). Not tested on hardware. Digitone II is not supported.
