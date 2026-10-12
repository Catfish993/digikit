# Handover 2026-10-13: digikit, sequencer and LEDs, browser path, merge prep

Branch `work/scenario-oracles`, all work committed. There is no remote: do not add one or push until Em says so.
This handover supersedes `HANDOVER-2026-10-12-realtime-dt2-samples.md`. Read AGENTS.md, then this file. Results are
in docs/findings/18-dsp-performance.md (the last two sections) and docs/findings/19-sequencer-and-leds.md.

## State

- **Project and licence.**
  - The project is now **digikit**, licensed AGPL-3.0-or-later. NOTICE carries the section 7 terms: attribution,
    origin and the name.
  - Canonical URL: https://github.com/m-dwyer/digikit.
  - The web and desktop UI show the attribution footer.
- **Unicorn is gone.** The Python reference emulator, its patches, tests and tools were removed in e6deb23. Tag
  `unicorn-reference-final` (54964c1) keeps them. `emu.extract`, `dt2/`, `snapread` and `ready` do not need Unicorn.
- **Sequencer works.**
  - The emulator never delivered INTC software-forced interrupts, so the firmware's step chain (vector 191, then 108,
    then 121) never ran.
  - Delivery is now on by default. `DIGI_INTC_FORCE=0` opts out natively; wasm is always on.
  - Em approved the re-pins: four DN2 scenarios (exactness-100m, trig1-noqa-40m, trig1-taps, trig1-tap-release), the
    `coupled_ready_exactness` gate, and the DT2 oracle `sf-rev-decay-u20` (37 to 38; encoder acceleration).
  - `dn2-rec-play` plays a hit every 0.50 s.
- **Panel LEDs.**
  - UART8 TX carries OLED rows plus LED records (`b9 idx val`). The decoder is in native/boot/src/panel_leds.rs.
  - LED state reaches the browser snapshot and the desktop status.
  - The faceplate lights legends from the firmware value: bits 4-7 are the colour mask, the low nibble the mode
    (inferred).
  - Per-key maps for both devices are in `devices/*-leds.toml`.
  - Capture tools: `DIGI_PANEL_TX_LOG`, `tools/panel_tx.py`, `tools/led_map.py`.
  - Encoder notches rotate 15 degrees per detent (UI only).
- **Browser.**
  - Per-firmware AOT core (dev page, stock DN2): about 2.0-2.2 DSP s per audio s in Chrome. It cannot ship on
    GitHub Pages, because it is firmware-derived.
  - Generic core (the GitHub Pages path, user firmware): fast tier at 4,2,1 by default, with the faster interpreter.
    It runs at 5.8 DSP s per audio s (about 0.17x real time).
  - V8 runs the generated code at about 1.6x native; JavaScriptCore at about 1.33x.
  - Firefox is about 1.5x slower than Chrome. Safari is not measured: it needs Develop > Allow Remote Automation,
    then `tools/browser_bench.mjs --browser safari`.
  - `wasm-opt -O3` is the default (0.96x DSP).
- **Desktop.**
  - Real time with PGO (about 0.93-0.95 DSP s per audio s). Without PGO it is about 1.15, which underruns.
  - The launcher uses a partly stale profile when only sources changed, and builds without PGO only when rustc or the
    generated core changed.
  - Retrain with `mise run pgo` (both devices, then a rebuild), `pgo:dn2` or `pgo:dt2`. The DN2 profile in use was
    trained with forced IRQ delivery off, so retrain at the next quiet moment.
- **Cores.** DT2 `gen` is the promoted lane-entry core, regenerated 2026-10-12. Backups are in `gen-stale-20261012`
  and `gen-lane-entry-stale-20261012`.
- **Tooling.**
  - `tools/verify.py` runs steps in parallel (`--jobs`, half the CPUs by default): about 10 min for the quick set.
  - Every step takes the bench lock shared.
  - Doc-only changes skip the Rust steps.
  - HLE, dead features and env switches, and the duplicate mise tasks are removed.

## Next steps

1. **Merge to main** through a PR (the description is in out/runs/prep/PR.md). `verify:full` ran on the branch tip.
2. **Browser DSP for GitHub Pages** (generic core).
   - Two fast-tier levers landed through a lane merge without a measurement: a jump past the delayed-branch budget
     now ends the region instead of refusing it (FUN_1c8ef1), and the PC a kernel exits at is noted as a region
     candidate. Measure them first: Chrome, generic core, 4,2,1, paired against core-A from 2026-10-12.
   - Then the cheap levers, hours each: the `compare on PEy` and `MR data move` lowerings; diagnose the 32% "no stop
     recorded" pool; trim the interpreter's `block_plan` and `exec_insn`. Expected about 4 DSP s per audio s.
   - Then the sharc-jit spike: docs/plans/sharc-jit.md, reference branch `lane/jit`, estimated 4-6 days at about
     35% confidence.
3. **GitHub Pages plumbing.**
   - Load `coi-serviceworker.js`.
   - Wire browser cold boot from a user `.syx`, plus the IndexedDB ready cache (`session.ts`, `ready-cache.ts`,
     `cf-worker.ts`).
   - Extract firmware in the page.
   - Add a mode that works below real time: UI in real time, audio rendered offline.
4. **LEDs.** Confirm the value-byte meaning (low nibble, bit 7) on hardware. LED state is not in save_state, so a
   restore starts dark until the firmware re-sends.
5. **Open.**
   - Test isolation in full pytest: test_sharc_lowergen leaves state that breaks test_sharc_register_banks. Not part
     of verify.
   - The fast_diff guarded-load SIGSEGV.
   - DT2 hardware A/B over Overbridge.

## Working notes

- **Delegation, since 2026-10-13.** Make small edits and targeted checks directly in the main context. Use subagents
  only for long or parallel work: builds, timed runs, lanes. Run the full verify only before a merge.
- **Lanes.** `tools/lane.sh NAME [BASE]`; a warm lane avoids cold builds of about 5 min for sharc-gen. Em removes
  worktrees: give the commands.
- **Build cost.** sharc-gen is 95% of a cold build: 109 s single-threaded frontend plus 200 s of parallel codegen,
  20 GB peak memory. Avoid touching sharc-rt casually: it rebuilds sharc-gen in every target dir.
- **PGO.** Retrain only at milestones. Profiles are keyed per device under `out/native/pgo/`.
