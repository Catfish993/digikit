# XSLICE-D (test)

XSLICE added at run time: a data-only `.dkm` that registers a machine (a clone of MANUAL SLICE with its own name and SRC page)
and one parameter through the digikit runtime's registry. Needs `core/runtime`, `core/dynreg`, `core/machines` and
`core/params` in the patched image. Pack it with `patchkit pack test/dyn-xslice --device dt2 -o plus_drive/dt2/digikit/modules/xslice-d.dkm`.
Emulator only. The DSP plays it as SAMPLE (the DSP side of a run-time machine is not loaded yet).
