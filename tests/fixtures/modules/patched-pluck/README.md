# Pluck, patched in (test)

The patched-in twin of the run-time module `digikit/pluck` (`modules/machines/pluck/`): the same machine, parameter and page rows and the same DSP source (`../../../../modules/machines/pluck/src/dsp/pluck.s`), but applied to a firmware image by patchkit. The Digitakt II machine has the permanent type 9; on the Digitone II it is the `own_dsp` machine of type 9 with its SYN page.
It keeps the patched-in path under test: the equivalence scenarios of tests/digikit use it as the twin of the run-time machine (PCM and rows must be equal), and the panel tests in `tests/`, the smoke matrix and the `pk_proof_pluck*`, `pk_proof_voice*` and `pk_tiers` tools patch it into an image.
Needs `core/dsp-voice`, `core/machines` and `core/params`, which patchkit adds. Emulator only. It is not in the public module list of the browser page.
