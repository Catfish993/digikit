# NAN-D (test)

A run-time machine (Digitakt II; the DSP source is one contract body that the rig also runs on the Digitone II) whose `render` fills its output block with NaN while the voice is active. It exists to show the runtime
guard of `core/dsp-voice`: the block is zeroed, the voice ends, the fault is counted (`MACHINE_FAULT` in the digikit log) and, after eight faults
within a second, the machine is switched off (`MACHINE_DISABLED`). Pack it with `patchkit pack test/dyn-guard-nan --device dt2 -o nan-d.dkm`.
Emulator scenario: `debug-guard`. `clamp.s` is a rig fixture of `tests/patchkit/test_dsp_guard_contract.py`; `nan_legacy.s`, `loud.s` and `clean.s` are rig fixtures of `tests/patchkit/test_dsp_guard.py` (the old form of a voice row).
