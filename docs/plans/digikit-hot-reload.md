# digikit hot reload: archived state and what is left

Status: archived 2026-10-17 (Em). The module set is now fixed per session and loaded at boot (docs/plans/digikit-runtime.md,
decision 9). Everything below is at tag `digikit-hot-reload-v1` (commit 928dc1d); the hot-only code leaves the active tree in one
"archive" commit (Phase 6), so resuming starts with `git revert` of that commit.

## What was built and proven (emulator only; nothing on hardware)

- Load and unload from the menu while the firmware runs, all four ports (findings 28, 29, 34): checks before any write,
  arena allocator, slots, code-cache flush, the UI-loop hook, popups and texts, the `LOADING` state.
- Run-time machines on both devices (findings 30, 33): reserved rows, the list getter filter, the view hook that re-runs the
  machine view builder after a change, DN2 page rows.
- Machines with DSP code loaded while running (finding 32): DSP relocation, voice rows written atomically, the load as a
  state machine over UI ticks, unload order (machine row, voice row, range); DT2 pluck PCM bit-identical with the key held.
- Unload refusal while a track of the active kit uses the module's machine (-26; finding 33).

Shared with the boot design, so not archived: the store and its reader, `.dkm`, the loader's checks, the registry writes,
DSP relocation, `core/dsp-link`, `core/dyndsp`, `core/dynreg` rows.

## What hot reload still needs before it can be trusted (the risk review of 2026-10-17)

1. Transport guard: refuse a load or unload while the sequencer plays or records, a sample is recorded, or a card operation runs.
   Nothing guards it today, and no proof ran with the transport running.
2. In-use scan of the whole open project (every kit and pattern, sound locks, the sound pool), not only the active kit.
3. Rows written while other tasks read them: a project saved with a run-time machine names its type; a load fills that row
   under a track that may be playing. Order the writes for concurrent readers or forbid it with (1).
4. Project round trip: save, unload, reopen, save, reload in another order, reopen; sounds, values and locks byte-identical.
   (Parameter codes may stay first-free: sounds and p-locks are keyed by slot, finding 36.)
5. A load/unload soak with write-watch and flush check, including refused files and requests during playback.
6. Unload of a module whose DSP voice still sounds (release tail) and range reuse by the next load: covered by the
   voice-row order and the acknowledgement (finding 32), not stress-tested.
7. The menu shows a finished background load only after the next key press (no timer).

Suggested route when resuming: hot reload for code-only modules first (no machine rows, no DSP voice), behind (1).
