# Effect pages (core/fxpages)

Gives each run-time effect row of `core/dynfx` its own page in the FX view of the Digitakt II and the Digitone II, after CHO: DEL, REV, CHO, then the pages of the loaded effects in effect-row
order. It reserves the parameter rows of the pages (24: 7 knobs and MIX for each of the 3 effect slots) and four small hooks (the page accessor, the FX view's page list,
the tab label and the filter graph). It also keeps the effects' knob values with the kit (three hooks on the firmware's kit loader, saver and initialiser of the effect value array: finding 61 "Persistence"). It changes nothing by itself: with no effect loaded the FX view is the stock one and a kit's bytes are as stock writes them (the spare halfwords it uses are zero). The digikit runtime fills the rows from the
`[[fx]]` page of a `.dkm` (`docs/design/digikit-registry.md`, "Effect pages"; finding 61). Needs `core/runtime`, `core/params`, `core/dynreg` and `core/dynfx`.
Device facts (page numbers, kit value indices, template rows) are in the device file's `[fx]` section. Digitakt II 1.16 and 1.17 and Digitone II 1.12. Both devices have it in the `emulator` and the `device` patch set (on the Digitone II the hook of `core/fx` costs the emulator x1.01 DSP CPU since the engine bars only the changed blocks, finding 55).
