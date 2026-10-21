# The emulated card as a folder (2026-10-22)

Status: built on lane `tasks-images` (after the Transfer work, W3). Design agreed with Em: one host folder per device, the module store mirrored to it
while the emulator runs, a Card panel on the Emulator page.

## What it does

- **One folder per device:** `plus_drive/dt2/` and `plus_drive/dn2/` (the launcher, `mise run workbench`, passes `--mirror-dir`; `--no-sync` turns it off).
- **DT2:** the card is still built from the folder at startup (unchanged) and the firmware's changes are still exported on exit (the full export:
  samples, projects and the module store). New: the module store files also follow the folder during the session.
- **DN2:** the card is MmcFs with no path filesystem and no card image at boot (the overlay holds the writes), so there is no contract to export. The store
  files are read with the same store reader (`plusdrive_format::store`) from the card as the guest sees it (the image if there is one, plus the overlay) and
  written to `plus_drive/dn2/digikit/modules/`. The DN2's own data stays out of scope.
- **When:** the emulator watches the card's write count (`Card::write_count`, one counter for every write command). A write starts a quiet period of 2 s
  (`cardmirror::QUIET`); when it runs out without another write the store is mirrored. A store that does not read (the guest is in the middle of an
  upload) is tried again after another quiet period. `card.sync` ("Sync now") mirrors at once. A quit (and the controller ending) mirrors once more.
- **One way, card to folder.** A new or different store file replaces the folder's file (the old one goes to `out/plusdrive/backup/mirror-<stamp>/`). A file the
  card no longer has is removed from the folder (to the same backup), so the next DT2 startup build does not bring it back. The mirror removes only paths it
  has seen on the card in this session (the store as the session started, then every file it read): a DN2 session that starts on an empty card leaves the files
  an earlier session mirrored. Nothing is read from the folder into the card. The selection record is not mirrored.
- **Where:** all of it runs in the emulator (`packages/emulator/plusdrive/src/mirror.rs`, `packages/emulator/control/src/cardmirror.rs` and the controller).
  The app starts no process. The store's place on the card comes from the device profile (`[card.store]`).
- **Front door:** `card.folder` (`{folder, continuous, files, last_sync, behind, error}`) and `card.sync` over `emu_call` or the control socket (docs/CONTROL.md).
- **Card panel** (`apps/desktop/ui/src/CardPanel.tsx`): collapsible, beside the faceplate on the Emulator page: the folder, "Open folder" (a native command that
  opens only the controller's folder, through the official `tauri-plugin-opener`), the module files with sizes, the time of the last sync, "Sync now". It reads
  after every Transfer job and on a 2 s poll while visible.

## The DT2 finding (step 1)

The module store **is** exported to the folder: `card.export` and the quit export both run the store pass (`export.rs`, `store_pass`), which adds
`digikit/modules/pluck.dkm` ([V]: a DT2 daemon, install through the TypeScript client, `card.export` and then `quit` each put it in the folder). But it ran only at the
end of the session, and only when the app ends the session: the controller's `quit` does it; the webview's window close dropped the controller's handle and
the process ended while the controller thread was still exporting (or never started), and a Ctrl-C does not export at all. That is why Em's folder showed only the old
`dyn-demo.dkm` after a session that installed pluck **[D]** (read from the code and the daemon runs; the GUI close was not run). Fixes in this change: the continuous
mirror (a kill loses at most the last 2 s), and the desktop app now sends `quit` when its window closes (`RunEvent::Exit`), so the full export runs too. Also found:
the exit export's `prune` is off, so a DT2 file deleted on the card stayed in the folder (listed as `deleted_by_firmware`, kept) and came back at the next startup
build; the mirror removes it.

## Tests

`plusdrive_format::mirror` (a store image written, mirrored, replaced, deleted, emptied; a blank start removes nothing; a torn store is an error to retry; the
folder listing), `cardmirror` (the quiet period and its restart), `apps/web/tests/card.test.mjs` (the panel's texts), the live scenario `transfer-live` on DT2 and
DN2 (a free-running daemon with `--mirror-dir`: install through `dktransfer`, the file appears in the folder with the card's sha256, `card.folder` lists it,
delete and `card.sync` remove it).

## Open

- **[O]** The GUI (the Card panel, the exit on window close) is built and type-checked but not run by the lane.
- **[O]** A user edit in the mirrored `digikit/` files is overwritten by the next pass that finds the card's file different (the old file is in the backup).
- **[O]** The DN2 starts on an empty card each session: the files in `plus_drive/dn2/digikit/modules/` are not loaded back (import is not part of this change).
