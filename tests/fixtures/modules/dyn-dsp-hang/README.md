# HANG-D (test)

A run-time machine (Digitakt II) whose DSP routine enters an endless loop after a voice has been active for three blocks. It exists to show
what the debug log reports when the SHARC stops inside a machine routine: `DSP_STOPPED` with the last breadcrumb (step, voice, machine type),
the ABOUT row `DSP STOPPED`. Pack it with `patchkit pack test/dyn-dsp-hang --device dt2 -o hang-d.dkm`. Emulator scenario: `debug-hang`
(finding 48). Do not select it on a real device: the DSP stays stopped until the restart.
