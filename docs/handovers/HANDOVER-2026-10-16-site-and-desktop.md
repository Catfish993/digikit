# Handover 2026-10-16: public site and desktop console

## State

Branch `ui/site-and-desktop`, merged to `main` on 2026-10-16. All of it passed the routed verify gate; the
browser walk `tools/devtools/web_shots.mjs` (patch, download equal to the CLI output, boot in the emulator,
a real key press) passed on DT2 1.17 and DN2 1.12.

- Tooling: Prettier (with the Astro plugin) and oxlint at the root, `.editorconfig`, `.gitattributes`, Node
  pinned in `mise.toml`; `mise run fmt`, `mise run lint`; verify's web step runs both. The format-only commit is
  in `.git-blame-ignore-revs`.
- Patched firmware boots: the emulator reads the `DKPR` patch record (`patchkit::verify::patch_record`) when
  neither the file nor the MAIN digest is known (`packages/emulator/boot/src/runtime.rs`; test
  `packages/emulator/boot/tests/patched_boot.rs`).
- Site (`apps/web`): `/` home, `/patch/`, `/emulator/` (pick a device, choose firmware, run). Tokens and fonts in
  `apps/web/src/styles/site.css`; the panel drawing is `PanelArt.tsx`, its live input and display `LivePanel.tsx`;
  cards read `module.toml` and `gallery/icon.svg` at build time (`apps/web/src/data/modules.ts`;
  `apps/web/tests/icons.test.mjs` keeps icons to line art). A module's notes are its `README.md`.
- Desktop (`apps/desktop/ui`): its own Vite + Solid console (top bar, status bar, metrics drawer). It imports the
  shared code through the `@web` alias. `mise run emu -- FILE` watches FILE and reboots it when it changes
  (`emu_source_info` / `emu_source_read` in `apps/desktop/src-tauri/src/main.rs`).

## Next

1. Browser audio on the public site: steps B1-B6, with what each gains, in
   `~/src/digi/SHARC-PERFORMANCE-PLAN-2026-10-14.archived.md` (historical "Browser audio on the public site").
   Current priorities are in `~/src/digi/SHARC-PERFORMANCE-PLAN-2026-10-23.md`: native DT2 first, browser follow-up
   deferred. Recheck current implementation before resuming any historical B1-B6 step.
2. Open points: XSLICE shows "Works with Digitone II 1.12" because patchkit's check accepts it, while its
   `[targets]` list only DT2 (decide whether cards also show the test status); files opened with Open... in the
   desktop console are not watched (the page has no path); a shared `packages/panel` is deferred until a third
   consumer appears; `rust_ws fmt` already failed at the branch base on `packages/patchkit/tests/codec.rs`.
3. `uv run python -m tools.emulator.native_wasm --cf-only` deletes `apps/web/.local/emulator-core-sharc.wasm`;
   rebuild it with the plain command.
