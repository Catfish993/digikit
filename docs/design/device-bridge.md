# Device bridge and `dkflash`

> **Deferred (Em, 2026-10-19).** Phase 10 now starts with the module loop (`dk run`), the firmware's own Screenshot command (MidiRpc 0x04,
> docs/MIDI-SYSEX-RPC.md), verify speed and DSP debugging. Known since this draft: Transfer sends a 1.9 MB image in about 20 s and the device asks
> YES after the data. Kept for later.

Status: design, 2026-10-19 (plan `docs/plans/digikit-runtime.md` Phase 10 item 1). Nothing here is built. Marks: **[V]** read in the
code or a finding for this design, **[D]** measured once elsewhere, **[O]** open. No device command was sent and no emulator was run to
write this file.

## 1. Goals, non-goals, guardrails

Goal: tools, scenarios and agents address "a device" (the emulator, or Em's Digitakt II on OS 1.17) through one interface, chosen by an
address string. The set of facts about the device (peek ranges, store, panel codes) comes from `devices/*.toml`
(`docs/design/device-data.md`), not from the bridge's code.

Non-goals: no card or flash write except the OS update through the vendor path (`dkflash`); no sample transfer (Phase "Later");
no DSP peek, tracepoints or PC sampler (Phase 10 items 3 and 4); no GDB bridge (item 6); no remote access (local socket only); no
change to the stock MidiRpc route.

| Guardrail | How it holds |
|---|---|
| Read-only by default | The device backend offers INFO, LIST, LOG, PEEK, SCREEN and raw notes. Every other method is refused with `E_READONLY` before a byte is sent. |
| Writes need DEBUG and a hand-set REMOTE CONTROL row | KEY (and a later POKE) are accepted by the runtime only when the debug log is ON **and** SETTINGS > DIGIKIT > REMOTE CONTROL is ON. The row is set on the device by hand; no SysEx command sets it; it is not stored and is OFF after every boot. |
| No card or flash writes except the OS update | The bridge never calls BEGIN, DATA, END, DELETE or CLEAR unless the client asked for `store.*`; `store.*` is the upload protocol, which writes only store sectors and is already guarded by the runtime ("Card safety", `docs/design/digikit-upload.md:100`). The OS update goes only through `dkflash`. |
| YES on the device stays human | No command presses YES at the OS-update prompt. The runtime cannot tell which prompt is on screen, so KEY refuses YES (code 10) and NO (12) always; Em presses them. [O] whether this is too strict for tests (open question 3). |
| Rate limits | Bridge side: one request in flight per command class; SCREEN at most 10 per second, KEY at most 20 per second, LOG poll 1 per second by default. Runtime side: a mailbox of 8 key slots, a full mailbox answers `E_BUSY`; every reply keeps the TX-ring guard (`digikit-upload.md:15`). |
| Nothing in interrupt context allocates | The runtime handles SysEx in the firmware's MIDI task, not in an interrupt (`digikit-upload.md` "Where the device handles it"). KEY only writes a mailbox slot there; the key is posted to the UI queue from the UI loop hook (`modules/core/runtime/src/rs/src/lib.rs:776`, task context), as the module mailbox is polled today. SCREEN copies 512 bytes into the static reply buffer. No heap use. |

## 2. What exists today

| Piece | Where | Facts |
|---|---|---|
| Emulator control server | `packages/emulator/control/src/server.rs:109` (`serve`), discovery file `current.json` `server.rs:81`, run dir `out/native/run` | JSON-RPC 2.0, one object per line, Unix socket; served by the desktop app and `digi-emud` (`docs/CONTROL.md:1-20`). **[V]** |
| Method list | dispatch `packages/emulator/control/src/controller.rs:1365-1381`; table `docs/CONTROL.md:49-90` | `status`, `step`, `input.button/turn/tap`, `framebuffer.get` (`controller.rs:1901`: 1024 bytes, 128x64, `oled-1bpp-column-major`), `mem.cf.peek/poke`, `midi.in/out` (DIN), `midi.usb.in/out`, `leds.get`, `events.subscribe`, `firmware.load`, `restart`. **[V]** |
| Emulator client | `tools/control/digi_client.py` (`mise run emu:ctl`, `mise.toml:36`) | `Emu` wrapper: `usb_in`, `usb_out`, `frame`, `peek32`, `tap`, ... **[V]** |
| USB MIDI in the emulator | `controller.rs:1677` (`midi.usb.in` docs), `docs/CONTROL.md` paragraph "midi.in" | Bytes go to the firmware's USB receive callback in 64-byte chunks one USB frame apart; `midi.usb.out` returns what the firmware passed to its USB transmit function. USB itself is not modelled. **[V]** |
| Upload protocol | `docs/design/digikit-upload.md:59-68` | INFO, LIST, BEGIN, DATA, END, DELETE, CLEAR, ABORT, LOG 0x20, PEEK 0x21. Frame `F0 7D 64 6B 01 cmd seq body F7`; reply `cmd|0x40`. **[V]** |
| Host tools | `tools/device/dkupload.py` (`EmuTransport:109`, `RtMidiTransport:146`, `Uploader:184`, `info:228`, `log_page:271`, `peek:284`); `tools/device/dklog.py` (`Follower:302`, `follow:345`, `peek_range:519`) | A transport is any object with `send(bytes)` and `recv(timeout) -> bytes`. The emulator transport already runs the same `Uploader` over `midi.usb.in/out`. **[V]** |
| Runtime side | `modules/core/runtime/src/rs/src/upload.rs:468-492` (`cmd_info`, 25-byte body), `dlog.rs:34-38` (`debug_on`, `set_debug`), `lib.rs:776` (UI loop hook) | `RUNTIME_VERSION = 3` (`modules/core/runtime/sdk/src/lib.rs:13`). **[V]** |
| Device data | `devices/digitakt-ii.toml:104` `peek = [[0x40000000, 0x47e30000], [0x47e8b000, 0x47fe1000]]`; `:91` `safe_key = 19`; `[panel]` `:121` | Peek ranges (at most four) become `dkrt_peek_ranges` in the runtime (`docs/design/device-data.md`). **[V]** |
| Preflight | `mise.toml:255`, `tools/patchkit/patchkit.py:306-313`, Rust `packages/patchkit/src/main.rs:360-363`, `tools/patchkit/pk_isacheck.py` | Rust checks (known stock base, sections 2/4/5/8 identical, checksum, HMAC, limits) print `PREFLIGHT OK: <path>` or `PREFLIGHT FAILED`; then the ISA scan of section 7; exit 1 when either fails. **[V]** |

On the device today (runtime 3, finding 47): INFO, LIST, LOG, PEEK work over USB MIDI through `RtMidiTransport` **[D]**
(`docs/findings/47-dt2-device-rungs.md:3-4,10-18`). So the read-only half of the bridge needs no flash.

## 3. `dkflash`

`uv run python -m tools.device.dkflash IMAGE.syx --device dt2:Digitakt [--yes-prompt-timeout S]`. It never opens a port in tests; the
tests use the fake and emulator transports of section 6.

### 3.1 Steps

| # | Step | Refuses when |
|---|---|---|
| 1 | Run `patchkit preflight IMAGE` as a subprocess (`[sys.executable, "-m", "tools.patchkit.patchkit", "preflight", IMAGE]`). Read stdout for `PREFLIGHT OK: IMAGE`. | Exit status not 0, or the `OK` line is missing. The ISA scan is part of this command (`patchkit.py:308-313`), so "PREFLIGHT OK and ISA ok" is one check. No `--force`. |
| 2 | Identify the target: open the port, send INFO. Record `device id`, `RUNTIME_VERSION`, flags, `log_newest`. | INFO fails when the device runs stock firmware (no runtime); that is allowed only with `--stock-ok`. Otherwise the image's device (`dt2`/`dn2`, from the `.syx` header byte `syx_id`, `devices/*.toml` `[device]`) must equal INFO's device id. |
| 3 | Tell Em what to do: "On the device: SETTINGS > SYSTEM > OS UPGRADE (it shows OS UPGRADE WAITING FOR SYSEX)". Wait for Em to confirm with Enter. | No confirmation within 120 s: exit 2, nothing sent. |
| 4 | Send the `.syx` as a stream of its 128-byte SysEx messages (`F0 00 20 3C 14 00 ...`, about 14,700 for a 1.88 MB image; `01-container-and-patching.md:47`), paced per section 3.2. | A send error, or a port that disappears before the last message: stop, print how many messages went out. |
| 5 | Print "press YES on the device when it asks". Poll for the end: the port disappears (USB disconnect while the device writes and reboots). | No port change within the timeout (default 300 s): print the state, exit 3. dkflash never sends anything more. |
| 6 | Wait for the port to come back (poll the port list once a second, 180 s). | Timeout: exit 4 with the recovery note (3.4). |
| 7 | Send INFO; compare with the image's identity (3.3). Then `dklog --once` into `out/runs/device-logs/`. | Mismatch: exit 5 with both identities. |

Where the YES prompt appears (before the data, or after the last message) is **[O]**: Em's description of the Transfer route says only
"drop the file, wait for the device to finish" (`docs/PATCHING.md:441-445`, "the screens were not checked here"). dkflash does not depend on
it: it sends everything it was given, then waits for the port to disappear.

### 3.2 Pacing

What is known **[V]**: the image is a stream of 128-byte messages, each with a per-message checksum; a message that fails it makes the
receiver reset silently, so the transfer never completes (`01-container-and-patching.md:281-286`, bootstrap DIN path). The MAIN OS path used at
the main screen is a different state machine (validator `0x400d9e4c` on 1.16, `01:140-166`); its per-message behaviour (acknowledges? queue depth?
pool?) is not documented in the findings **[O]**. The MIDI task's SysEx gather holds about 150 KiB and drops a message over 128 KiB
(`digikit-upload.md:14`), so 128-byte messages do not stress it. The USB bulk endpoint is 512 bytes (`42-usb-transfer-routes.md:29`).
What Transfer's rate is: unknown **[O]**; no capture exists in the repository.

Safe determination without a device experiment, in this order:

1. **Read the MAIN OS receive path** (the state machine that calls the validator; static, on 1.17: `cf.load("dt2-1.17-emac")`, the 1.16 validator
   address found again there by hash): does the device answer each message, and where are messages stored? If it answers, dkflash is acknowledgement-paced (send the
   next after the reply) and no rate guess is needed.
2. **Run the update in the emulator**, USB path: `midi.in` and `midi.usb.in` exist; the emulator takes the stock image over DIN end to end
   (`01:407-413`). Feed the image through `midi.usb.in` at the main-screen OS UPGRADE and record what `midi.usb.out` returns per message and
   how many messages the firmware consumes per emulated millisecond. This gives correctness (no message lost at 64 bytes per USB frame, i.e.
   the slowest model) and the reply pattern, not the real timing.
3. **Default when neither gives an answer:** a fixed gap of 2 ms per message (about 30 s for 14,700 messages), with the rate as a flag. The
   reasoning: USB MIDI moves 64-byte packets per 1 ms frame at full speed, so one 128-byte message per 2 ms is slower than the wire and below
   the 3.2 MB/s the bulk pipe could carry. This is a safe floor, not a measured limit; the first device use is Em watching (section 7, rung D2).

A fast rate is a risk only for the transfer, not for the flash: the flash writes happen after the receive completes and the checksum and HMAC
pass.

What a cut transfer does. **Known [V], emulated bootstrap on the DIN path:** the receive path stages each message at `0x40000000 + seq*101`;
the erase and write loop runs only after the message count is reached and the content checksum and HMAC pass; a bad content checksum or HMAC ends in
UPGRADE ABORTED with a spin loop and "nothing erased or written"; a flipped transport byte makes the receiver reset and "the transfer never
completes", and "in all three nothing is erased or written" (`01:255-290`, `01:407-413`). **Not known:** (a) that a transfer which simply stops
(fewer messages than the framing message announced) writes nothing is an inference from that structure, not a test: the finding tests flipped bytes,
not truncation **[O]**; (b) all of this was traced in the bootstrap's DIN receive path; the MAIN OS main-screen path (`0x4013a978`, which "only erases and
writes from 0x80000", `01:426`) is a separate implementation, read only for the validator, not for a truncated stream **[O]**; (c) nothing was
tested on hardware. dkflash therefore treats a cut as "state unknown", not "safe": it prints that, tells Em to power-cycle and check the main
screen, and never retries on its own. Step 1 of section 7 checks (a) and (b) in the emulator by truncating a stream.

### 3.3 Identifying the running image

INFO today reports protocol, `RUNTIME_VERSION`, device id, free bytes, flags and the log range (`digikit-upload.md:59`; `upload.rs:468-492`).
**It does not identify the image**: two images with the same runtime version look equal, and the DIGIKIT > ABOUT page shows the runtime
version only. The runtime has no build identity today **[V]** (no field in `cmd_info`, `RUNTIME_VERSION` is a constant `3`).

Needed: an `image_id`, 8 bytes, appended to the INFO body after `log_newest` (runtime 2 -> 3 already grew INFO by appending, so an old host
ignores the tail). Definition: the first 8 bytes of sha256 over the patch record, that is the base firmware sha256 followed by the sorted
(module name, module object sha256) list that patchkit linked, plus the DSP patch hashes. The whole `.syx` hash cannot be embedded, because it
contains the id. patchkit already knows these inputs when it builds the runtime object; it writes the 8 bytes into a data word of
`core/runtime` (a patch-time constant, like `dkrt_device_id`), and prints the same id on `preflight` and `pack` output so dkflash compares
two values it can both get without the device. Cost on the device: 8 bytes of data and about 20 instructions in `cmd_info`.

Until runtime 4 is on the device, dkflash falls back to: INFO says `RUNTIME_VERSION` and the set of loaded modules from LIST/LOG
(`BOOT_START`, module load events) match what the image should contain. This is weaker and is printed as "image id not available".

### 3.4 Failure modes and recovery

| What the user sees | Meaning | Action |
|---|---|---|
| `PREFLIGHT FAILED` | The image is not a known-safe patch of a known stock image | Fix the build; dkflash does nothing. |
| Port not found | Cable, USB mode (USB MIDI must be enabled in SETTINGS > SYSTEM > USB CONFIG) | Fix; nothing sent. |
| Sent all, no port change in 300 s | No YES pressed, or the device refused (the validator returns 1..6: checksum, HMAC, downgrade floor `build >= 0060`, `01:140-175`) | Look at the OLED; the device still runs the old image. |
| Port back, INFO has the old image id | The update was not applied | Retry from step 3. |
| Port back, INFO times out | The new image does not reach the runtime (boot loop, safe mode, a broken hook) | Power-cycle; read the OLED; step to recovery. |
| Port never comes back | The new image faults or hangs before USB | Recovery. |

Recovery **[V in the emulator, not on hardware]**: hold FUNC while powering on; the bootstrap's STARTUP menu comes up (patchkit never changes
section 2); TRIG 4 selects OS UPGRADE; it receives over **MIDI DIN only**, so Em needs a DIN interface; send the stock `.syx` of the installed
version (`docs/PATCHING.md:376-384`, finding 01 "Recovery"). dkflash gets a `--din PORT` mode for exactly this, with the same pacing code; it
prints the recovery text on every failure above and does not run it unattended.

## 4. The bridge

### 4.1 Process model and addresses

`dkbridge.py` (new, in tools/device): one Mac process per device. It owns the MIDI port (python-rtmidi, as `RtMidiTransport`; not a repository dependency,
so the bridge is started with `uv run --with python-rtmidi`). It listens on a Unix socket `out/runs/bridge/<name>.sock` (mode 0700 directory) and
writes `current.json` beside it, the same layout as the emulator's run dir, speaking the same JSON-RPC 2.0 lines. A client that already talks to
the emulator (`tools/control/digi_client.py`) therefore connects unchanged for the methods both support. Only the bridge reads the port; a second
reader would steal replies.

| Address | Meaning |
|---|---|
| `emu` | the emulator found through `out/native/run/current.json` (direct, no bridge) |
| `emu:<socket>` | a given emulator socket |
| `dt2:<port substring>` | a Digitakt II through a bridge for that MIDI port (the bridge is started on the first use, or found by name) |
| `dn2:<port substring>` | a Digitone II (device id from `[digikit] name`; the file decides, nothing else is hard-coded) |
| `bridge:<socket>` | an already running bridge |
| `emu-usb:<socket>` | the bridge's device backend on the emulator's USB MIDI (section 6) |

`device.py` (new, in tools/device) resolves the string and returns an object with the methods of section 4.2; the existing `Emu` class is one backend,
the bridge socket client the other. Scenarios and agents write `Device("dt2:Digitakt").peek(addr, n)` or `Device("emu").peek(addr, n)`.

### 4.2 Command set

| Method | Emulator backend | Device backend | Exists today |
|---|---|---|---|
| `status` | `status` | INFO plus bridge state (port up, last reboot seen, image id) | emulator yes; device mapping new |
| `info` | the INFO command through `EmuTransport` | INFO | tool yes (`dkupload.py:228`); bridge method new |
| `mem.cf.peek` | `mem.cf.peek` | PEEK in 512-byte pieces, only inside `[digikit] peek` (`E_PEEK` otherwise) | both yes; bridge new |
| `mem.cf.poke` | `mem.cf.poke` | refused (`E_READONLY`); a later `POKE` limited to a `[digikit] poke` range is possible, not designed here | emulator yes |
| `log.follow` (JSON lines) | LOG through `EmuTransport` | LOG, polled | tool yes (`dklog.py:345`); stream method new |
| `framebuffer.get` | `framebuffer.get` | SCREEN (new, 4.4) | emulator yes; device new |
| `input.button`, `input.tap` | same | KEY (new, 4.6), needs the RC row | emulator yes; device new |
| `input.turn` | same | KEY with an encoder record | emulator yes; device new, **[O]** |
| `midi.usb.in` / `midi.usb.out` | same | raw bytes to and from the port (notes); dk replies are taken out of the stream by the bridge | emulator yes; device new, no runtime change |
| `store.ls`, `store.put`, `store.rm`, `store.clear` | the Uploader over `EmuTransport` | the Uploader over the port | tool yes (`dkupload.py`); bridge methods new |
| `flash` | `firmware.load` (the emulator's flash is a load) | `dkflash` steps 4-7 after the human steps | device new |
| `events.subscribe` | emulator kinds | `log` events, `port.up`, `port.down`, `reboot` | emulator yes; device new |
| `restart`, `step`, `run.mode`, `state.*`, `dsp.*`, `guard.*`, `pcm.tap`, `leds.get` | yes | `E_NOT_ON_DEVICE` | emulator yes |

A client asks `hello` for `{backend, device, capabilities}`; the capability list comes from INFO (a `caps` byte in the same tail as `image_id`)
so a client does not call SCREEN on a runtime that lacks it.

### 4.3 Log streaming and notes

`log.follow` reuses `dklog.Follower` unchanged: the bridge polls LOG once a second (every 250 ms while a client asks), and pushes each event as
one JSON-RPC notification whose params are the object `dklog.event_dict` builds, plus the gap and device-restart notes `Follower` already
makes. A client that wants a file gets the format `dklog follow` writes today (`# ` lines and JSON lines).

Notes: plain USB MIDI. `midi.usb.in {bytes}` writes the bytes to the port; the device's USB-MIDI receive path routes notes to the sequencer
or the external-input handling as it does for any keyboard. `midi.usb.out` returns the non-dk bytes that arrived (clock, notes out). The bridge
drops `F0 7D 64 6B` replies from this stream. The firmware's USB receive callback is the SysEx assembler (`docs/CONTROL.md` paragraph on
`midi.usb.in`), so a note message needs no special case. Channel and port selection (USB MIDI has two cables, `42:29`) is a parameter, default cable 0.

### 4.4 Screen capture

Emulator: the OLED frame comes from the panel serial records the firmware sends to the panel MCU (`packages/emulator/boot/src/panel_leds.rs:3`,
`runtime.rs:652`), 1024 bytes, `oled-1bpp-column-major`. The real device has no host-visible copy, so the options are:

| Route | Anchor | Cost | Confidence |
|---|---|---|---|
| A. PEEK of the firmware's Bitmap object | The panel framebuffer instance (`0x4313b298` in the 1.15C-era image, `07-emulator.md:830`; the 1.17 address and the pixel layout of the Bitmap object are not established **[O]**: `setPixel` is called once per pixel, 8192 per frame, so the storage may be a byte per pixel, not 1 bpp) | No runtime change if the address is in a peek range (the first range of `devices/digitakt-ii.toml:104` covers it). 8192 bytes would be 16 PEEKs per frame. | Medium: needs the 1.17 address as a `[digikit] oled_addr` and `oled_layout` in the device file, found with `fw.py`/cfdb. |
| B. New SCREEN command | Same address, read by the runtime, converted to the emulator's 1024-byte format in the MIDI task | Runtime change, about 100 bytes of code; one request per 512 bytes (2 requests, 2 replies of about 590 wire bytes, under the 1 KiB reply limit). | Medium-high once A has found the address. |
| C. Hook the panel UART transmit | The ColdFire driver that sends `0x10..0x17` OLED rows (`panel_leds.rs:3`) | Code in a time-critical path, a new hook | Low; not recommended. |

Recommendation: A first as a read-only experiment through the existing PEEK (zero runtime change, finds the layout), then B so that a frame is
one consistent snapshot. Rate: 2 requests per frame at about 3-5 ms per round trip gives 20 to 50 frames per second for the wire; the bridge
caps at 10 per second. A frame read across the UI task's redraw can tear; SCREEN reads twice and compares, A reads twice and keeps the second if
they differ.

### 4.5 Upload

`store.*` is the existing protocol through `Uploader` with the port transport; the bridge adds a lock so a LOG poll never interleaves with the
2-in-flight DATA window (`digikit-upload.md` "DATA rules"): while an upload runs, log polling pauses and resumes after END.

### 4.6 Panel input

A key must reach the firmware's UI queue, not the panel hardware. Known **[V]** (finding 03, `03-ui-and-panel.md:150-175`, 1.15C-era addresses): the UI
main loop pops a queue of item pointers, 16-byte records `type, code, flags, timestamp`; type 0 is a key event; flags `0x01` press, `0x10`
release; edges are posted from `0x40110d7c`. Needed anchor: the firmware function that posts a key edge (and what allocates the item), for 1.17,
as a rule in the runtime module (`dkrt.fw.ui_post_key`). Design: KEY (`u8 code, u8 flags`) writes a mailbox slot in the MIDI task; the UI loop
hook (task context) calls the firmware's post function; this is the same shape as the module mailbox. Encoders use a different record type, not
yet traced **[O]**. Confidence: medium for buttons (the post function is identified for 1.15C/1.16; the 1.17 anchor and the item allocation
need one static pass, about 3 hours), low for encoders until traced. Checks in the runtime: debug ON, RC ON, code not YES/NO, rate and mailbox
limits.

## 5. Runtime changes and cost

| Change | Where | Cost on the device |
|---|---|---|
| `RUNTIME_VERSION` 3 -> 4 | `modules/core/runtime/sdk/src/lib.rs:13` | none |
| INFO tail: `image_id[8]`, `caps u8` | `upload.rs` `cmd_info`; patchkit stamps the id | 9 bytes data, about 20 instructions per INFO |
| SCREEN 0x22 `u8 part` (route B) | `upload.rs`, anchor `dkrt.fw.oled` | about 100 bytes code; runs only when asked, 512-byte copy in the MIDI task |
| KEY 0x30 `u8 code, u8 flags` | `upload.rs` plus the UI loop hook | mailbox of 8 slots (64 bytes); the hook adds one load and compare per UI pass when empty |
| REMOTE CONTROL row | DIGIKIT menu (`core/digikit-menu`) | one more row; one RAM flag, not stored |
| Error codes `E_RC_OFF` (-64), `E_BUSY` (-65), `E_NOT_SUPPORTED` (-66) | protocol table | none |

Protocol version stays 1 (commands are only added; `digikit-upload.md` status line). Everything is behind DEBUG; with DEBUG off the runtime
answers INFO, LIST, LOG-errors and PEEK as today and refuses SCREEN and KEY. Idle cost is zero: the device sends nothing unless asked.

## 6. Tests (emulator first)

The device backend takes a transport, so the same bridge code runs on three: the real port, a fake port and the emulator. The emulator case is
possible **[V]**: `EmuTransport` (`dkupload.py:109`) already carries the `Uploader` over `midi.usb.in/out`. One difference: it steps the emulator
inside `recv`; a bridge needs `LiveEmuTransport` that polls `midi.usb.out` against a daemon in `run.mode free` (`digi-emud --free`, small new
code). The emulator does not model USB timing, enumeration, the 4 KiB TX ring timing or a port disappearing, so those are tested with the fake.

| Piece | Emulator-first proof | Kept test (new) |
|---|---|---|
| Bridge framing, rate limits, refusals, reconnect | A fake `rtmidi` loopback driven by a reference decoder of the protocol; port vanishes and returns | `test_bridge_fake.py` (new) |
| Same commands on both addresses | `emu` and `emu-usb:` give equal results for `info`, `mem.cf.peek` (inside the ranges), `log.follow`, `store.ls` | `test_bridge_emu.py` (new) |
| Log streaming | Events from `emu-usb:` equal `dklog --emu` output (existing `tests/digikit/test_dklog.py` pattern) | same |
| `dkflash` refusals | Failing preflight, wrong device id, no confirmation: nothing is sent (fake transport records zero bytes) | `test_dkflash.py` (new) |
| `dkflash` happy path | Emulator: the update streamed through `midi.usb.in` at the main-screen OS UPGRADE; `status` shows the new image after `restart`; INFO `image_id` equals patchkit's. Depends on step 1 below | `pk_proof_flash.py` under `tools/patchkit/` |
| Cut transfer | Emulator: truncate the stream at N messages; assert the flash model is unchanged and the old image boots (answers 3.2 (a), (b)) | same |
| Image id | Build, inject, INFO equals; stock image gives none | `test_image_id.py` (new) |
| SCREEN | Emulator: SCREEN bytes equal `framebuffer.get` data for the same frame (hash compare) once the emulator exposes the Bitmap object to the runtime; layout test on the Bitmap object for 1.17 | `tests/digikit/scenarios` case |
| KEY | Emulator: RC off refuses; RC on, KEY(code) gives the same frame hash after N instructions as `input.tap(code)`; YES/NO refused | scenario case |
| Existing pins | PCM pins and stock MidiRpc byte equality do not change (a stock image has no runtime) | `mise run verify` routed |

Hardware is not needed for any of these. A proof that the device accepts the pacing needs the device (rung D2).

## 7. Steps, estimates and first device rung

Estimates are agent time, not calendar time. Payoff: one interface for emulator and hardware removes the hand-run `dklog`/`dkupload` commands,
makes the flash step checked (preflight, image id) and is the base of the device-versus-emulator diff (Phase 10 item 5). Confidence: high for
bridge, log, peek, upload, notes and `dkflash` on the emulator; medium for SCREEN (layout of the 1.17 Bitmap object unknown); medium-low
for KEY (anchor and encoder record). About 4 days in total, as the plan says.

| # | Step | Estimate | Output |
|---|---|---|---|
| 1 | Static read of the MAIN OS update receive path (ack, storage, truncated stream) and an emulator run of the USB update, with a truncated stream | 3 h | finding with [V]/[O] marks; pacing decision |
| 2 | `device.py`, `dkbridge.py` (log, info, peek, store, notes), `LiveEmuTransport`, fake-port tests | 4 h | `emu-usb:` and fake proofs |
| 3 | `dkflash` (steps 1-7, refusals, `--din`), emulator proof of the happy path and the cut | 3 h | tests |
| 4 | Runtime 4: INFO tail with `image_id` and `caps`, patchkit stamp, `preflight` prints the id | 3 h | emulator proof |
| 5 | Find the 1.17 Bitmap object; PEEK-based screen (route A) in the bridge; SCREEN (route B) if the layout is simple | 4 h | frame equals `framebuffer.get` |
| 6 | REMOTE CONTROL row, KEY, anchor `ui_post_key`, emulator proof | 6 h | key proofs |
| 7 | Device-versus-emulator use: `mise run` tasks and `emu:ctl`-like `dev:ctl` CLI | 2 h | docs in `docs/CONTROL.md` style |

First device rung (**D1**, no flash): with the runtime 3 image already on the device (rung C4, finding 47), run the bridge read-only: INFO, LOG
follow, PEEK inside the ranges, LIST, notes. It proves the port handling and the JSON-lines stream on silicon and needs only Em to plug in the
cable. D2: Em flashes the runtime 4 image with `dkflash` at 2 ms per message, watching the OLED and pressing YES; this is also the first
real-device test of the pacing and the image id. D3: SCREEN. D4: REMOTE CONTROL and KEY, with Em setting the row by hand.

## 8. Open questions for Em

1. Does the device answer each message of the OS update, and does the YES prompt appear before the data or after the last message? (From
   what you saw in Transfer: its progress and how long the 1.88 MB took, which bounds the real rate.)
2. Is 2 ms per message acceptable as the first device pacing, with you watching, or do you want step 1 (static read plus emulator run) to
   decide the rate before any send?
3. Should KEY ever press YES and NO? The design refuses both so a test cannot confirm an OS update or a restart; some menu tests need them.
4. Encoders: needed for the first version, or buttons only?
5. Should the bridge offer a `restart` on the device (a runtime command behind RC that does what APPLY & RESTART does), or stay human?
6. Image identity: is the hash of the patch record (base plus module objects) the right definition, or do you want the sha256 of the `.syx`
   file shown by `shasum` (cannot be embedded; would need a side table keyed by the id)?
7. `dn2` is in the address scheme but there is no Digitone II on the desk; keep it design-only until one is connected?
