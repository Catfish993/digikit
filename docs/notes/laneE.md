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

## CF cost on the coupled DN2 path (2026-10-07, on 16e23a0)

Option B does not exist here: the ColdFire core is our own interpreter (`native/coldfire`), not
Unicorn. Option A was already half done: the three named routines are fused loops
(`native/coldfire/src/fused.rs`), not 95 % of the cost any more.

Profile (feature `cf-profile`, fused table added): DN2 trig1-taps, 10.5 s. CF CPU 8.2 s:
fused loops 2.2 s (MacA 0x400db160 1.22 s at 6.0 ns/insn, MacB 0x400db1f4 0.52 s, word-copy
0x401344a6 0.27 s, interleave 0x400cf918 0.18 s), the remaining 5.1 s is 393M unfused instructions at
13 ns each in many functions (FUN_40137726 12 % of instructions, vector_191_handler 6 %, ...).

Changes (exact; `native/coldfire/tests/fused.rs` compares fused against stepping over random
registers, MACSR modes 0x00-0xc0, accumulators, masks and memory holes, whole `Cpu` equality):
- MacA and MacB iterations written out (`mac_a_body`, `mac_b_body`): the interpreter's own `mac_op`,
  `acc_to_reg`, flag helpers, no decode or operand handling; MacA 6.0 -> 2.4 ns/insn, MacB 8.7 -> 5.7.
- Copy16/Clear16 check one 16-byte range instead of four longs.
- `fused_clean` (no observed PC inside a loop body) is precomputed in `rebuild_watch`.

Exactness: all four pinned DN2 scenarios through `coupled-run` match their pins before and after
(exactness-100m `b3f5230f`, trig1-noqa-40m `2861ce03`, trig1-taps `d60ad339`, trig1-tap-release
`88347621`); DN2 CF digest and PCM (emud, trig1-taps 4 s) and DT2 boot with card + 5 s + a Trig tap
(`c4d19bbd..`) are identical; CF-only restored-snapshot digest identical (`4c3889c9..`).
The `coupled_ready_exactness` lib gate fails the same way on HEAD (CF digest `75b6a52d..` vs pin
`55dc699e..`, fixture/env mismatch in my runs), so it is not evidence either way.

CF CPU per emulated s (coupled-run `cf_thread_per_audio_s`, native): trig1-taps 0.617 -> 0.521,
trig1-tap-release 0.630 -> 0.523, exactness-100m 0.965 -> 0.909 (short, startup bound).
CF-only audio path restored from the DN2 ready snapshot (`examples/cf_snap_bench.rs`, wasm export
`digi_load_snapshot`; no DSP, so the wasm number excludes the DSP): native 0.577 -> 0.499, wasm (Node)
0.873 -> 0.722 CPU s per emulated s. wasm target 0.8: met on this workload by 10 %; the coupled
wasm core itself was not built (needs the generated core compiled for wasm).

Floor: 393M instructions through the generic interpreter, about 60 % of what is left. Next lever is
interpreter speed (decode-cache copy of the 40-byte `Insn`, per-instruction bus bookkeeping in
`run_fast`), not more leaf routines: FUN_40137726 alone is 100 instructions per call, branchy.
