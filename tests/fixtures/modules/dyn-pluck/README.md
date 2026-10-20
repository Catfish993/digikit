# PLUCK-D (test)

digikit/pluck loaded at run time, on both devices: a `.dkm` with the typed machine and parameter rows and the pluck DSP routine in its DSP section. Digitone II: the `own_dsp` machine of type 17 with its SYN page (DECAY, PICK, BRIGHT, COLOR on knobs A to D) and `pluck_dn2.s`.
Needs `core/runtime`, `core/dynreg`, `core/dyn-dsp`, `core/machines`, `core/params` in the patched image. Pack it with
`patchkit pack test/dyn-pluck --device dt2|dn2 -o plus_drive/dt2/digikit/modules/pluck-d.dkm`. Emulator only. Proof:
the `equiv-dyn-pluck` scenario of tests/digikit (PCM equal to the patched-in machine, module selected at boot, both devices) and `menu-pluck-demo` (ticked in the menu, APPLY & RESTART).
