# Handover 2026-10-12: real-time desktop, DT2 samples, speed work closed

Branch `work/scenario-oracles`, all work committed (no remote). It supersedes
`HANDOVER-2026-10-11-scenario-oracles-realtime.md`. Read AGENTS.md, then this file. Read finding sections only when
a step names them (mostly `docs/findings/18-dsp-performance.md`; start with "Performance: where it stands").

## State

- **Cores (2026-10-12):** register-form shifter ops fixed the DT2 DSP halt at 0xb88e8b; DN2 and shared cores were
  regenerated and swapped (old ones in `gen-pre-fext-20261012`); DT2 runs on `out/native/dt2-audio/gen-fext` via its
  PGO manifest.
- **Desktop app: real time.** The emulator free-runs on the controller thread, paced by the audio queue
  (`Mode::Free`); the webview polls once per frame. App run: 0 underruns, DSP 0.955 CPU s per audio s (headless PGO
  0.93). Measure with `DIGI_SPIKE_LOG=DIR mise run emu`, then `uv run python tools/spikes.py DIR`
  ("Desktop app real time"). Latency: the launcher's default prefill is now 0.3 s; DN2 and DT2 held 0 underruns at 0.3 s and DN2 at 0.15 s ("App soak: latency and DT2"). Unattended app runs: `tools/app_soak.py`.
- **DSP speed:** emulation-speed work is stopped: every cheap lever is measured and closed, and the remaining ones
  are large and uncertain (finding 18 "Performance: where it stands", 2026-10-12). Tools from that work: the HLE
  hook and checker (`SHARC_HLE`, `mise run hle-check`), `SHARC_PCDUMP=all` per-call traces.
- **Browser: 0.22 -> 0.47x real time** on the dev page (MessageChannel yield, ColdFire/DSP split workers, fast tier
  in wasm with `Op::Helper`). DSP-bound at 2.16 s per audio s; wasm codegen of the AOT core is 1.74x native and no
  build flag helps. Real time is not in reach with current approaches; the browser is a demo for now. Bench:
  `mise run web:bench -- --seconds 10 --runs 3 [--split 0] [--fast desktop]` (use `--url` with your own dev server
  in a worktree). Rebuild the dev page cores with `uv run python tools/native_wasm.py`.
- **DT2:** entry search core in `out/native/dt2-audio/gen-lane-entry` (paired DSP 0.871; promotion needs Em). 60 DT2
  FX scenarios with oracle tests (`mise run scenarios:dt2`), tier-equal; profiling them into the core does not pay.
  DT2 PGO (own profile, -19% DSP) is merged; the launcher picks the profile per device. The DT2 app runs real time at 0.95 DSP load with `SHARC_GEN_DIR=out/native/dt2-audio/gen-lane-entry` (the profile's gen) until gen-lane-entry is promoted and the profile retrained (`native_pgo.py train --device dt2`).
- **Correctness:** fast_diff triage fixed one real lowering bug (MODE1 write in a loop body) and the harness
  expectations; one fuzzer-only SIGSEGV in a guarded load is open (finding 18 "fast_diff mismatch triage", possibly
  the same family as K1's Jump-slot SIGBUS). Find it before widening flat or dynamic windows.
- **Gate:** `mise run verify` (quick) / `mise run verify:full` (about 45 min); green at 0914408.

## Next steps

1. Em: play at the new 0.3 s default and report any dropout;
   promote the DT2 core (`cd out/native/dt2-audio && mv gen gen-pre-entry-20261011 && cp -a gen-lane-entry gen`);
   remove the lane worktrees (`git worktree list`; `git worktree remove --force PATH` for `.claude/worktrees/agent-*`).
2. DT2: hardware A/B over Overbridge to settle the open audio questions (delay feedback threshold, send/mix taper,
   absolute level, delay width); MIDI input (eDMA channel 34 ring) for exact parameter setting.
3. Retrain the DN2 PGO profile (`mise run pgo`, stale since today's source changes) at the next milestone.
4. Open: the fast_diff SIGSEGV; browser cold boot and the IndexedDB ready cache are unwired (`session.ts`,
   `ready-cache.ts`, `cf-worker.ts`); `coi-serviceworker.js` is not loaded (GitHub Pages needs it).

## Working notes

- Delegation: scout reads, coder edits the main tree, general-purpose agents run things. Worktree lanes may edit and
  test themselves: start them with `tools/worktree-setup.sh work/scenario-oracles` (the default base is wrong). The
  first native build in a worktree takes ~25 min. In worktrees the hook blocks heredocs and long git commands: use
  the Write tool and `/usr/bin/git`. Lanes must not share a dev server: real `node_modules` directory with symlinked
  entries, own `astro dev` port, `--url`.
- Merging lanes: findings sections conflict at the end of finding 18; resolve with `git merge-file --union`.
- `tools/coupled_ab.py`, `native_pgo.py` and `sharc_regionbench.py` take the benchlock themselves.
- Proceed through measured steps; check in with Em only when a step exceeds about a day or a result contradicts the
  plan. State payoff and confidence before proposing work; recommend stopping when they are weak.
