# PLUCK-D (test)

machines/pluck loaded at run time: a `.dkm` with the typed machine and parameter rows and the pluck DSP routine in its DSP section.
Needs `core/runtime`, `core/dynreg`, `core/dyn-dsp`, `core/machines`, `core/params` in the patched image. Pack it with
`patchkit pack test/dyn-pluck --device dt2 -o plus_drive/dt2/digikit/modules/pluck-d.dkm`. Emulator only. Proof:
the `equiv-dyn-pluck` scenario of tests/digikit (PCM equal to the patched-in machine, module selected at boot).
