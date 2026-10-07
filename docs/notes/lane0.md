# Lane 0 notebook (DT2 +Drive launcher, encoders)

Firmware: DT2 1.16, sha256 278541e4...a9ec (matches sections/.source-sha256). The .syx is
in the main tree root: /Users/em/src/digi/digitakt2/Digitakt_II_OS1.16.syx.

## Task 1: `--samples DIR` in the launcher

- `tools/native_emu.py` (the `mise run emu` entry): `--samples DIR` builds
  `native/plusdrive` (`cargo build --release --bin plusdrive`, shared nogen target dir),
  then `plusdrive build DIR -o img --syx SYX`, cached in
  `out/plusdrive/native/<key12>/dt2.img`. Key = sha256(firmware, plusdrive binary, each
  sample name + bytes). Then passes `--card-image`. SYX = positional arg or DIGI_EMU_SYX.
  Combine with `--cf-only` for DT2 (project seeding is DT2-only).
  `mise run emu -- --cf-only --samples DIR /path/Digitakt_II_OS1.16.syx`
- Built card from 2 WAVs (test-perc-click, test-sine-1khz): 2 sample entries, project seeded.
- Evidence: `native/boot/examples/card_boot.rs`
  `card_boot SYX IMG 150000000 test-sine-1khz test-perc-click`:
  ready_at=914986053, main_ui=true, fs_verified=Some(true); both names found in guest state
  (ascii 3 and 6 hits). Same run with card `none`: 0 hits. Screenshot of the booted card
  project shows project "WAXSAFETY" (seeded), SRC sample slot "A 7".
- Launcher build checked: `tools/native_emu.py --cf-only --build-only --samples DIR SYX`.
- Not done: a screenshot of the +Drive list itself (needs navigating to the samples pool).

## Task 2: encoders

- UI: `Faceplate.tsx` drag path checked in real Chrome (astro dev, left_click_drag on a
  knob): pointerdown, gotpointercapture, pointermove -> worker `turn` messages with
  detents 1,1,2,1,2, pointerup. The drag logic is fine in Chromium. Not tested in WebKit.
- Backend: `native/boot/examples/turn_probe.rs` (boot, tap buttons, turn encoders, report
  OLED pixel changes; `CARD=img`, `TURN_RENDER=1`, `FRAME_OUT/FRAME_DIR` raw frames,
  `d:ENC:DET:COUNT:GAP_K` drag simulation, `w:M` wait, `b:CODE` tap).
  Packet format (0x30|(enc-1), signed delta) matches emu/panelin.py and findings.
- Why headless turns "changed nothing": after boot the firmware shows the modal
  "FILE SYSTEM OK / PRESS Y/N TO CLOSE" which swallows every key (also with a card).
  After `b:12` (NO) turns work: with card, FLTR page enc2 +20 -> 144 px changed, AMP 100,
  FX 64, MOD 80; no card, SRC enc1 41/123 px. Drag simulation (20 x +/-1 every 250k instr)
  changes 156/45 px. Display lags: the first turn right after a page change often shows 0
  px (page animation); wait ~300M instr or turn again.
- Anomaly: with the seeded card on the SRC page (sample slot 7), encoders 1-9 changed 0 px
  (other pages fine). Not investigated (likely empty slot 7 makes SRC params inert).
- Backend fault not found. If Em still sees dead drags in the desktop app, suspects: the
  modal above, or WebKit pointer-capture on SVG <g> (untested).
- DN2 DSPI log check not done (needs ~4 min sharc-gen compile in this worktree).

Suggested findings text (03-ui-and-panel.md): **[V]** Panel encoder rotation reaches the
1.16 firmware through the UART8 ring (header 0x30|(enc-1), signed byte). While the boot
modal "FILE SYSTEM OK PRESS Y/N TO CLOSE" is up, rotation does nothing; after NO, rotation
changes the OLED on FLTR/AMP/FX/MOD (native/boot/examples/turn_probe.rs).

## Follow-ups

### (2) +Drive list on screen (done, with a caveat)
- Card without project seeding: `plusdrive build DIR -o img --no-project`. Probe sequence
  (DT2 1.16, `CARD=img turn_probe SYX b:12 b:12 b:10 b:2 w:60 b:44 w:100 c:17:10 w:100`):
  NO x2, YES, SRC, push encoder D (SAMP), FUNC+YES -> screen "ASSIGN SAMPLE > A001" lists
  "Factory", "test-perc-click", "test-sine-1khz". Screenshot (do not commit, firmware UI):
  /private/tmp/claude-501/drive-list.png. No panic, error=None.
- Caveat [O]: the seeded-project card (`plusdrive build ... --syx SYX`, project WAXSAFETY,
  slot "A 7") makes the SRC page hang the UI task: after SRC, FLTR/other pages never draw,
  encoder pushes/turns do nothing, no CF fault (pc idle). Same card, FLTR first, works.
  No-card and --no-project cards are fine. So `--samples` (seeded) yields a UI that looks
  dead once the SRC page is visited. Launcher should probably pass `--no-project` or the
  seeded project needs fixing (likely the known SampleManager null-string path, 07-emulator
  "Opening the sample-pool list ... panics the UI task"). Not root-caused.
- turn_probe gained `c:HOLD:TAP` chords, `d:` drags, `w:` waits and a final status line.

### (1) WebKit drag
- No Safari/WebKit automation available (safaridriver needs remote-automation enabling = a
  system setting; Playwright has only chromium). Not reproduced in WebKit.
- Hardened `Faceplate.tsx` knob drag: no pointer capture and no lostpointercapture release
  for knobs (WebKit can fire it early, ending the drag); pointermove/up/cancel on `window`
  during a drag; `preventDefault()` on knob pointerdown (no text selection / native drag);
  deltas from clientY differences; turn() rejections now surface via update(). CSS adds
  `-webkit-user-select: none; -webkit-touch-callout: none`. Chrome re-test with a real drag:
  turn e9 d 1,1,1,2,1 then click -> button 49 down/up, both still work. tsc clean.
- Em should try: rebuild the panel (DIGI_EMU_PANEL=1 mise run emu), drag a knob. If the
  SRC page was opened with a seeded `--samples` card, the UI may be hung (see above), which
  looks exactly like dead encoders; the boot modal "FILE SYSTEM OK / PRESS Y/N" also swallows
  rotation until NO/YES is pressed.

## Seeded-project hang: root cause and fix (done)

Symptom: with the seeded card (`plusdrive build ... --syx`), the SRC page hangs the UI task;
FLTR etc. never draw, encoders look dead. `--no-project` cards were fine.

Not a seeding bug: the Python-built and Rust-built seeded images behave the same, and the
Rust one matches findings 14 (297 refs + 1471 copies pointed at one sample).

Trace (`turn_probe` with `PROBE_PCS` / `PROBE_RETS` / `MUTEX`, new `Emulator::probe_pcs`,
`probe_rets`, `peek32`):
- reloadAllSamples (0x4004e8be) runs, `load_sample` (0x40154540) was called once, not 297 times.
- The loader task (TCB 0x43176e38) was parked inside sem_pend (parked pc 0x40001488) holding the
  DSPI1 mutex 0x40965b74 and mutex 0x450d1b10; the UI task queued behind 0x450d1b10.
- The wait is `FUN_400124a0` (DSPI1 command/response, finding 04 inventory row "ch 14"): it arms
  eDMA channel 14 (RX, source DSPI1 POPR 0xfc03c03a), whose completion (INTC0 source 22,
  vector 86, handler 0x400ccd74) runs callback 0x400124fa (CS pulse). The peer then raises its
  IRQ1 line (EPORT, INTC0 source 1, vector 65, handler 0x400ccd1c) which runs `FUN_40012500`:
  second transfer (read-back, 0x300 bytes) whose completion gives the semaphore 0x40965b84.
- The Rust board modelled neither the ch14 completion nor the IRQ1. The Python harness hid this
  with `unblock` (13 forced pends, finding 07 "A +Drive cold boot loads a sample").

Fix (model):
- `native/machine/src/board.rs`: SERQ for channel 14 with SADDR = DSPI1 POPR now retires the
  major loop, zero-fills the destination (peer answers zeros), reloads CITER, marks
  `dspi1_irq`. `snap_save` refuses while it is pending.
- `native/boot/src/runtime.rs`: `service_dspi1` delivers vector 86 then the peer's IRQ1
  (vector 65) honouring INTC0 ICR/IMRL and the SR mask; fast paths stop while pending;
  `save_state` refuses while IRQ1 is pending.
- Result (seeded card, `turn_probe` b:12 b:2 w:60 b:3 w:60): `load_sample` 297 calls (as in
  Python), vector 86 x4, vector 65 x4, SRC then FLTR draw ("Filter (1/2)"), encoders turn
  (FLTR enc1 +20 -> 33 px, enc4 -> 55 px; SRC enc4 -> 1517 px), SRC sample list (push enc D,
  FUNC+YES) shows "Factory, test-perc-click (A001), test-sine-1khz" with the seeded project.
- Launcher: `--samples` is back on `--syx` (seeded project); the `--no-project` stopgap is removed.
- `cargo test` machine (all ok) and boot lib (27 ok).

Findings text: **[V]** DT2 1.16: DSPI1 (eDMA ch 14, vector 86) plus the peer's IRQ1 (vector 65)
form a command/response link; `FUN_400124a0` blocks on it. Unmodelled, the first sample load
never finishes and holds mutex 0x450d1b10, which hangs the UI on the SRC page.
Peer identity remains **[O]**; zero responses are enough for sample loading to complete.
