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

## What the archive commit removed (Phase 6, lane r4; `git revert` of the commit "digikit runtime: archive hot reload" restores all of it)

Runtime (`modules/core/runtime`):
- `dkrt_unload` (C ABI), `loader::unload` and `CMD_UNLOAD` (mailbox command 2, now answered `E_FORMAT`); `loader::unload_rec` stays (same name) as the take-down of a failed boot-time DSP part (`undo_early`).
- The in-use scan `registry::in_use` and its `dynreg_info` words `F_PROJECT`, `F_KIT`, `F_TRACK`, `TRACKS` (words 61 to 64; the table keeps
  zeros there).
- The machine-view rebuild: `registry::view_now`, `registry::refresh_view` (and its calls at the end of `apply` and `remove`), the words
  `VIEW_VAR` and `VIEW_BUILD` (48, 49), `DkrtBootInfo::view_rebuilds` and `view_at_boot`.
- The load queue: `dsp::QUEUE`, `dsp::enqueue`, `dsp::dequeue`, `DspState::queued` and `queue`, `loader::start_queued`, `dsp::busy` and its
  queue branch in `loader::load`, `DkrtStatus::queued`.
- The run-time DSP load path: the `used = 2` branch of `loader::load`, `loader::complete_pending`, `loader::abort_pending`, the non-early
  branch of `dsp::fail` and of the `PH_ROWS` end. `dkrt_load` outside the boot hook now returns the new `E_BOOT_ONLY` (-45).
- Kept on purpose (the boot path needs them): `dsp::release`, `clear_rows`, `unreserve` (a boot-time DSP failure frees rows and range),
  `registry::remove`, `E_BUSY`, `E_TYPE_IN_USE` and `STATE_UNLOADING` as reserved numbers, the `Pending` struct (its `reg`, `init`, `mask`,
  `fns` fields are always 0 now; kept so the `DKRT_DSP` layout the proof tools read does not move).

`core/dynreg`: the `dynreg_view_hook` hook op, claim and anchor `cf.dynreg.msv_build`; the anchors `cf.dynreg.project`, `cf.dynreg.kit_of`,
`cf.dynreg.track_type`; the `dynreg_view` variable; the externs `_dr_t_view`, `_dr_f_build`, `_dr_f_project`, `_dr_f_kit`, `_dr_f_track`
(both devices); the four anchors per port in `devices/ports/*.toml` (regenerate with `port_resolve resolve-all`).

Tools and tests: `digikit-hot-reload-v1:tools/patchkit/pk_registry.py` (all of it: `proof`, `proof_dn2`, `ui_proof`) and `digikit-hot-reload-v1:tests/patchkit/registry_frames.json`; in
`pk_dkrt_machine.py` the `proof` functions, `build_folder`, `Rig.load_and_wait`, `watch_checks`, `main`; `pk_dkrt.py` `proof` was rewritten for
boot-time loading (selection with bad files, `E_BOOT_ONLY` after the boot), as was `pk_cdemo.py`; `panel_script.py` `digikit_errors`,
`digikit_gate` and their constants; the hot tests in `tests/patchkit/test_patchkit_registry.py`, `test_patchkit_dkrt_machine.py`
and `test_digikit_menu.py` (`test_gate_loads_and_unloads_from_the_menu`); the mise task `dkrt:machine`.
The menu (`core/digikit-menu`) needed no change: finding 38 had already moved it to the selection calls.

Coverage that replaces them: `tests/digikit` (equivalence of the rows, tables and PCM for a selected module, round trips, safe mode, power
cuts), `pk_dkrt_boot`, `pk_dkrt proof` (refusals of bad files at boot), `digikit_menu_proof`.

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
