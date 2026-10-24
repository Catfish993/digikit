# Effect pages (core/fxpages)

Gives each run-time effect row of `core/dynfx` its own page in the FX view of the Digitakt II, after CHO: DEL, REV, CHO, then the pages of the loaded effects in effect-row
order. It reserves the parameter rows of the pages (24: 7 knobs and MIX for each of the 3 effect slots) and four small hooks (the page accessor, the FX view's page list,
the tab label and the filter graph). It changes nothing by itself: with no effect loaded the FX view is the stock one. The digikit runtime fills the rows from the
`[[fx]]` page of a `.dkm` (`docs/design/digikit-registry.md`, "Effect pages"; finding 61). Needs `core/runtime`, `core/params`, `core/dynreg` and `core/dynfx`.
Device facts (page numbers, kit value indices, template rows) are in the device file's `[fx]` section. Digitakt II 1.16 and 1.17, emulator only for now.
