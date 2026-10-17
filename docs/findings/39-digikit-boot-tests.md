# 39. Boot-time scenarios of the digikit runtime kept in the suite (Phase 6, tests)

Plan: `docs/plans/digikit-runtime.md` (Phase 6). Boot path: finding 37. Code: `tools/patchkit/pk_digikit_scen.py` (runner), `tools/patchkit/pk_digikit_project.py`
(the project round trip), `tests/digikit/` (scenario files `scenarios/*.toml`, pytest entries), twin fixtures `modules/test/twin-xslice` and `modules/test/twin-dn2`,
verify suite `digikit-boot` (`tools/devtools/verify.py`), `docs/TESTING.md` ("Digikit boot scenarios"). Lane r2b, 2026-10-17, native emulator, Digitakt II 1.16/1.17 and
Digitone II 1.11/1.12 (stock images; nothing ran on a device). Everything is **[D]** (measured here once; no second agent has checked it against the image bytes).

## Result

**[D]** Every scenario passes, on both devices, on both images of each device where it applies (times: wall clock of one scenario on one image, the boots
restored from the daemon's boot cache where the card is the same).

| Scenario file (kind) | What it proves | Result, time |
| --- | --- | --- |
| `equiv-dyn-pluck` (equiv, DT2) | `test/dyn-pluck` selected: results `[0]`, voice row claimed, the machine, parameter and page rows decoded from RAM equal those of the image with `machines/pluck` patched in (name, short, clone_of, knobs by parameter name, ranges, defaults, mirror index, the template words), no view rebuild, PCM of a note equals the patched-in machine's | pass 1.16 132 s, 1.17 108 s |
| `equiv-dyn-xslice` (equiv, DT2), `equiv-mixed` (DT2: pluck + xslice) | a data-only machine with a parameter equals its static twin `test/twin-xslice`; the mixed set equals both twins, PCM of the pluck equal | pass 1.17 89 s, 79 s |
| `equiv-dn2-set` (equiv, DN2) | `test/dyn-dn2` (own SYN page, 4 parameters) + `test/dyn-sine-dn2` (voice only): rows, page, voice row equal `test/twin-dn2` + `test/dn2-sine` (its machine entry has no run-time counterpart and is left out), PCM of the sine equal from the onset | pass 1.11 54 s, 1.12 60 s |
| `equiv-dyn-demo`, `equiv-c-demo` (equiv, both) | modules with no registry records (a counter module, a C module with libgcc helpers): result 0, every table equals a boot with no module | pass, 38 to 60 s |
| `roundtrip-dt2` (quick), `roundtrip-dt2-orders` (full), `roundtrip-dn2` (full) | see below | pass; DT2 1.16 349 s (4 boots), DN2 1.11 164 s, DN2 1.12 276 s, DT2 1.17 quick 2 boots |
| `safe-fault` (safe, both) | a module whose `init` resets the machine: boots 1 and 2 raise the counter, the third is safe mode (reason counter, counter 2), no module, result -44, voice rows free, the UI loop clears the counter, selection kept; the safe-once flag | pass 53 to 92 s |
| `cut-selection` (cut, both) | A new/B old, A torn/B old, A old/B new, both torn, and one copy written by the runtime then a restart: the boot uses the valid copy with the higher sequence number | pass; DT2 168 s, DN2 77 s (the quick tier runs the first two cases and the runtime cut) |

Every scenario ends with the common checks: write-watch over `cf.expansion` (0 stray writes over every emulator it ran, 12 to 16 per scenario), flush check (0 hits),
and the card writes (in the store range only the two selection sectors; nothing below sector 0x50000 but those two).

### The project round trip (the check that could show data loss): none found

A project with the run-time machine on track 1 (DT2: and a second one on track 2), parameter values (ENC A, ENC B turned), a note trig on step 1 with a p-lock (ENC C) and a
preset lock (LEVEL) is saved through the panel (FUNC+SETTINGS). Under set A the machine is loaded; the next boot has **set B, which lacks the track-1 machine**, loads the project
(SETTINGS, LOAD PROJECT), saves again; then set A in the other load order. The MmcFs slot is compared sector by sector (the sectors the guest wrote, read from the card files):

- **[D]** After set B and after A in the other order the slot equals the first save **byte for byte, but for one 32-bit header word** at slot offset 0x18 (after the project name).
  That word is `0x56650daa` in the first save of a project and `0x421f3ead` in every later one, with or without a module change (control: a reload under the same set A gives
  `0x421f3ead` too; the saves of boots 2, 3, 4 are byte-identical to each other, stamp included). The same two values appear on DT2 and DN2. It is not the CRC of anything we tried
  and not time-dependent in the emulator; what it is **[O]**. The scenario leaves it out of the comparison with the first save and holds it fixed from the second save on.
- **[D]** The sound records still carry the machine types after set B (the track whose machine is absent keeps its type: 18 on DT2 track 1, 16 on track 2 and on DN2 track 1; the
  type byte is at +0xd2 and +0xf4 of a record). A track whose machine is absent is saved as it was loaded.
- **[D]** The controls: the first save differs from a save of the untouched project. DN2: the trig bytes 4 and 5 of pattern 1, the lock bytes near offset 0x4a34 and 6 bytes of the kit
  (a precise control). DT2: the firmware writes only the patterns and kits that changed, so the untouched project's save does not write pattern 1 at all and the edit save's
  bytes (trig bytes 3, 4, 5, 7, 11 ...) compare against zeros (a weaker control: pattern 1 now holds data). The machine types are present in the first save.
- **[O]** Whether the preset (sound) lock was created by the panel script is not decoded separately: the changed bytes of pattern 1 are a trig and lock data, not named further.
  A project that has the machine on tracks of other patterns or kits, and the 128-kit/pool copies, are not covered (only slot 1's written sectors).

## What was found

- **[D]** A cold boot of the patched firmware on a card an earlier run saved a project on never became ready: the DT2 filesystem check the readiness contract waits for does not
  run on a used card (as after a soft reset, finding 37). `DIGI_COLD_RELAX_FS=1` (`packages/emulator/control/src/controller.rs`, also in the boot-cache key) makes the cold boot
  ready at the main panel; the runner sets it for cards made by `card_after` only.
- **[D]** The machine selection on DT2 puts a track's type byte at `0x4265338e + 0x450 * (track - 1)` (the kit's track stride, finding 02); `TRK` + `TRIG n` selects track n.
- **[D]** Most of a scenario's time is emulated panel time (a save and a load take about 10 s each, a restart about the same), not the boots: restoring the cold boots from the
  cache saves 10 to 40 s per boot only. Measured standalone, the quick tier is 97 s (equiv DT2), about 60 s (equiv DN2), 187 s (round trip DT2), 239 s (safe and cut DT2) and about 130 s (safe and cut DN2); inside `mise run verify`, with the cargo tests and the other python tests running, the same files took 97 to 620 s (five files in parallel, `DIGI_PY_JOBS`), so the budget of about 3 minutes holds only for an idle machine.
- **[D]** `verify` builds in the target directory the scenarios use while the python tests run, so a tool binary can be missing for a moment: the runner retries a missing tool for up to 3 minutes.
- **[C]** `pk_dkrt_boot.py`'s own proof (`proof --quick`) covers equiv for one device image, restart, cut and safe; the scenario suite replaces its equiv, cut and safe parts
  with data files and adds the sets, the round trip and the DN2 variants. The proof tool stays as it is (restart, the watch totals).

## Open **[O]**

- The header stamp word (what sets it, why the first save differs).
- The preset lock is not decoded; the p-lock only by changed bytes.
- Only the newest image of each device is in the quick tier; the full tier (`verify --full`, `DIGIKIT_TIER=full`) runs every scenario on all four images (about 50 minutes serial).
- Equivalence compares decoded rows, not the raw bytes of the tables (the type numbers, parameter codes and string pointers differ between a run-time and a patched-in set by construction).
