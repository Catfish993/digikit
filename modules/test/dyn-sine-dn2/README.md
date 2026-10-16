# SINE-D (test, Digitone II)

test/dn2-sine's DSP routine as a run-time module: a `.dkm` with a DSP section and a voice row, no machine record. Needs `core/runtime`
and `core/dyndsp` in the image. Pack it with `patchkit pack test/dyn-sine-dn2 --device dn2 -o plus_drive/dn2/digikit/modules/sine-d.dkm`.
Emulator only. Proof: `tools/patchkit/pk_dkrt_machine.py proof --image dn2-1.12`.
