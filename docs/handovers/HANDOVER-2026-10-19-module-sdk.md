# Handover 2026-10-19: Digitakt II runtime on the device, next the module SDK

Branch `runtime/digikit-runtime` (not merged to main; ask Em before merging). Plan: docs/plans/digikit-runtime.md, Phases 9-12.
Results are in docs/findings (47 device rungs, 49 section 10 the excluded stores, 50 the SRC page, 51 pluck TUNE and the guard).

## State
- Device (Em's Digitakt II, OS 1.17): runtime 4 image `out/patch/rt4-dt2-1.17.syx` built; rung C4 (default build) plays pluck, one SRC
  page, back to ONESHOT clean. Runtime 5 (`out/patch/rt5-dt2-1.17.syx`, sha256 `ced22f18...`, PREFLIGHT OK) has the pluck pitch clamp,
  the line clear at note-on and the runtime guard; **not yet tested on the device**: flash it, `dk run test/dyn-pluck` (docs/PATCHING.md
  "Runtime 5"), LFO on TUNE and the other pluck parameters with the log running; expect no silence, `MACHINE_FAULT` only if a third bug.
- Tools: `tools/device/dklog.py` (follow), `dkupload.py`, `dkrun.py` (`dk run`: build, pack, upload, select, restart, follow),
  `dkscreen.py` (firmware screenshot), `mise run module:check NAME` (machine conformance + the module's tests), `mise run verify:pre`
  (lanes), `mise run verify` (routed; the merge gate), `verify --explain [--head REV]`.
- Verify routing: any change to tools/devtools/verify.py or verify_route.py still selects every step, even a table-only edit; narrow it.

## Next
1. Em answers the open questions of docs/design/module-sdk.md (sections 11.2 and 13.7).
2. Build the SDK, pluck first (Phase 12; design section 10). Do not touch other modules until pluck is migrated and tested on the device.
3. Phase 10 debugging still open: log levels (designed in module-sdk.md section 13), DSP peek, DSP exception handler, PC sampler, named
   dumps and device-versus-emulator diffs; the emulator refusing the excluded stores (decode-time trap in sharc-rt).
4. Deferred: dkflash, the desktop Device tab (needs Em's OK for `midir`), key input, the Digitone II guard (comes with the SDK adapter).

## Cleanup for Em
Lanes merged or superseded: `git worktree remove ../lanes/<name> && git branch -D lane/<name>` for b, srcpage, dkrun, v-slow, and the
superseded speed, v-route, v-rust (their work is in 632dc41b).
