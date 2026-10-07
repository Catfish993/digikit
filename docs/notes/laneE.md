# Lane E notebook: emulator control (P0, P1) and firmware injection (2026-10-07)

State: built and verified; nothing committed. Worktree
`.claude/worktrees/agent-a85dce58e014716d5` (main tree patch + lane B engine change
applied). DN2 core: `out/native/dn2-audio/gen-ranges`, target dir
`out/native/target-wt/agent-a85dce58e014716d5/g-21725942` (first build ~5 min).
Results are in `docs/findings/17-emulator-control.md`; the design doc now says
what is built.

## Files

- `native/control/` (new crate `digi-control`): `proto.rs`, `controller.rs` (the old
  desktop `actor()` plus jobs, run modes, `firmware.load`), `dispatch.rs`,
  `server.rs` (Unix socket, `current.json`), `events.rs`, `firmware.rs`, `cli.rs`,
  `bin/digi_emud.rs`. Workspace member added.
- `packages/desktop/src-tauri/src/main.rs`: now thin Tauri commands over
  `digi_control::Handle`; serves the socket unless `--no-control`; scenario exit
  is a `scenario.done` event handled in the host.
- `native/boot/src/dsp_stream.rs` (new): boot stream -> loaded memory -> byte diff
  as `BYTES` patch ops. `sharc_peer.rs`: `Msg::Patch`, `ThreadedHandle::apply_patch`,
  `DspEngine::apply_dsp_patch`; the start-time refusal of a patch with fast regions
  on is gone (the fast tier verifies its code words now).
- `native/boot/src/runtime.rs`: `Emulator::new_patched(syx, card, base_sha256)`,
  `current_frame()`, `status()` public. `device-profile`: `boot_for_base`.
- `native/coupled/src/lib.rs`: `new_patched`, `inject_dsp_patch`, `restore_state`,
  `rebase_clock`, `sync_audio`, `has_audio`.
- `tools/digi_client.py` (client + CLI), `tools/dsp_patch.py syx` (patched .syx
  writer on `dt2/build.py`), `tools/native_emu.sh --daemon`, `mise run emu:serve`
  and `emu:ctl`, `tests/test_digi_client.py`, launcher tests.

## Choices

- Writing a patched .syx: `dt2/build.py rebuild` already packs (store-only aPLib), so
  `tools/dsp_patch.py syx BASE PATCH OUT` is the smallest addition. `firmware.load`
  also takes `dsp_patch` (patch file on top of the .syx), so both ways work.
- The diff is done in Rust over the decoded section-7 streams, not by the client:
  any host (browser later) can call `firmware.load` with just a .syx.
- A patched MAIN boots CF-only (no DSP state to attach); cache key is the MAIN
  SHA-256 at `<run_dir>/patched/<sha>.snap`.
- UI session id is kept when control replaces the runtime (`epoch` counts them),
  so the webview keeps stepping the new runtime.

## Evidence (short)

- step(N) exact for 1, 7, 250000, 250001, 600000; `at_instr` input recorded at that
  count; replay of `inputs.recorded` after `restart` gives the same state digest.
- Hot inject of the master-half patch: rms 0.2096 -> 0.1048, peak 0.6930 -> 0.3465,
  PCM `6a7f5cec..` (= lane A's start-time overlay); original re-injected:
  `a0a0f426..` = reference; `trig1-tap-release` 8.5 s over the socket = pinned
  `aa1dbd9e..`; injection 0.13 s; 3 s audio in about 5 s wall with fast regions.
- Patched MAIN: cold 4.4 s, cache 0.13 s, same state digest; stock restore 0.22 s.
- `coupled_ready_exactness` fails on the merged tree (CF digest `75b6a52d..` vs pin
  `ab27c5c9..`, PCM `849157bd..` vs `3b600f6d..`); the tree has lane 0's DSPI1 model.
  Not bisected.

## Open

- Hot ColdFire code overlay (proposal in the design doc), audio for a patched MAIN,
  P2 methods (captures, memory reads, state save/load, scenario.run, replay), PGO for
  `digi-emud`, the desktop socket was not exercised through the GUI (same controller
  and server as the daemon; compiled and unit-tested only), the "chunk timing with no
  client" A/B was not run (the UI step path adds a mode/job check per chunk).

## CF idle measurement and DSP-port poll fast path (2026-10-07)

Profile: feature `cf-profile` (boot `src/cfprof.rs`, RPC `cf.profile`; CPU per path from
`thread_cpu_ns`, PC histogram, `run_fast` exit reasons). `status` now carries
`controller_cpu_ns`. Numbers below are native, one machine, other lanes running.

| workload | CF instr skipped by the idle skip | CF CPU in it | CPU s / emulated s |
|---|---|---|---|
| DN2 trig1-taps, 10.5 s (coupled, fast regions) | 38.1 % | 1.3 % | 0.72-0.80 |
| DT2 main screen with card, 10 s after boot | 95.3 % | 1.3 % | 0.09-0.10 |

- Idle shape 1: BRA-to-self (`60 fe`) idle task (DN2 0x400cf0e2, DT2 0x400cccd8). Already
  skipped analytically in `advance_idle`, bounded by the next timer/SSI deadline and by the
  model's forced yield every 20,000 passes (vector 32). It costs about 1 % of CF CPU; nothing
  left to win. The yield pass runs the firmware main loop (about 880 instr, 70k per 10 s on DT2),
  which is real work, not skippable.
- DN2's CPU is real work, not idle: 62 % of instructions, 95 % of CF CPU, in the SSI audio
  block path (vector_191_handler: FUN_400db12a EMAC unpack loop 25 %, FUN_400cf7be buffer fill
  18 %, memcpy FUN_40134490 10 %).
- Idle shape 2: polling a status register. DT2 polls the FlexBus coprocessor port STATUS
  (0x8C000002) about 9 times per word it writes to 0x8C00000A (37k words/s). Each access bumped
  `mmio_epoch`, so `run_fast` returned 3.86M times in 10 s (16 instr per call).
- Prototype (exact): `Board` no longer bumps `mmio_epoch` for the DSP port slot
  (`periph::dsp::Fifo` keeps a poll counter and an echo register only). `run_fast` exits drop
  3.86M -> 0.14M per 10 s. CF digest after DT2 boot + 5 s + a Trig tap, and DN2 trig1-taps PCM
  SHA-256 and CF digest, are identical before and after (`c4d19bbd..`, `0aa5b3f7..`,
  `4d42ee6c..`).
- CPU s per emulated s, before -> after: native DT2 0.099 -> 0.088, native DN2 0.750 -> 0.744;
  wasm (CF-only core, Node) DT2 0.339 -> 0.279, DN2 CF-only 0.181 -> 0.178. The profile build
  exaggerates the old per-call cost (1.62 -> 0.80 with timers on).
- wasm costs 3.4x native on DT2; ready from cold takes 9 s (DT2) / 7.9 s (DN2) in wasm.
- Next if CF CPU matters: the DN2 audio-block loops (fuse FUN_400db12a, the 0x8001 fill loop of
  FUN_400cf7be, the word memcpy), not idle.
