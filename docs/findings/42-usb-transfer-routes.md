# 42. USB transfer routes for modules and samples (spike, static)

Spike, 2026-10-17. Static only; the emulator models USB at message level, so no rate here is measured.

Marks: **[D]** read once here, **[O]** open, **[C]** corrects an earlier claim. Nothing here is **[V]**: no second-agent byte check and
no hardware run. All addresses are DT2 1.16 `section_3_MAIN_OS.bin`, file offset = VA - 0x40000400. Manual: out/refs/MCF5441XRM
(chapter 24), user manual out/refs/Digitakt-2-User-Manual_ENG_OS1.03A_241023.

## 1. USB controller, speed, descriptors

- **Controller [D]**: MCF5441x USB OTG at 0xFC0B0000, device mode, EHCI-style registers. `FUN_40006c52` (0x40006c52) is the init:
  MISCCR2 (0xEC09001A) bit 0 cleared = OTG uses the external ULPI PHY (manual 10.3.10), PORTSC1 (0xFC0B0184) PTS = 10b (ULPI),
  pin-mux writes at 0xEC094050/4D/5E, USB timer 0xFC0B0080/84, vectors: 134 = USB (`vector_134_handler` 0x40006e9a), 209 =
  USB timer/OTG status (0x40005e10). `FUN_40005cde` (0x40005cde) starts the device: BURSTSIZE 0x204, USBMODE (0x1A8) = 0x1e (device),
  EPLISTADDR = 0x80000000 (the QH list sits in the on-chip SRAM, dTDs at 0x80008000+), USBINTR 0x157, then RS.
- **High speed [D]**: PORTSC bit 24 (PFSC, force full speed) is cleared when the port entry's flag word (+0x24) is 1. Entries 2, 4, 5, 6
  of the port table (0x4029ea40 + n*0x28) have flag 1; entries 1 and 3 (flag 0) are full-speed variants and are not selected by the
  normal boot (`FUN_400cc864` picks 6, 5 or 2; 4 in a service flag mode). The user manual's spec list says "Hi-speed USB 2.0 port".
  All bulk descriptors say 512-byte packets and `FUN_400043e2` sets QH EP1 OUT = 512. DCCPARAMS = 0x184: **only 4 endpoint
  numbers (EP0-EP3)**, 8 QHs at 0x80000000 + {0x00,0x80,0x100,0x180} (OUT) / {0x40,0xC0,0x140,0x1C0} (IN). A cheap host check
  (not done): `system_profiler SPUSBDataType` should show "Up to 480 Mb/s". Actual negotiation cannot be seen statically **[O]**.
- **Descriptors [D]**: .data at 0x40311000-0x4031152b; port table entry = {+0 connect cb, +4 disconnect cb, +8 device desc, +0xC config desc,
  +0x10 wTotalLength, +0x14/0x18 other-speed ptr/len, +0x1C string table 0x4029eb58, +0x20 string count 9, +0x24 HS flag}.
  The configs' wTotalLength bytes are written at run time by `FUN_4000677a` (0x4000677a, immediates 0x65, 0x4b, 0x107, 0x148). Device
  descriptors: bcdUSB 2.00, EP0 max packet 64, VID 0x1935, nconf 1.

  | mode (setting `DAT_4099d5c4`: 1, 2, other) | port | device desc / PID | config | interfaces and endpoints |
  |---|---|---|---|---|
  | USB MIDI (other) | 2 | 0x40311298 / 0x012b, class 0 | 0x403111ce, 101 B | AudioControl + MIDIStreaming; **EP1 OUT bulk 512, EP1 IN bulk 512**; 2 embedded jacks each way = 2 cables |
  | USB AUDIO/MIDI (2) | 5 | 0x4031150b / 0x102b, EF/02/01 | 0x403113c3, 328 B | UAC2 (EP3 OUT iso 56 B, EP2 IN iso feedback, EP3 IN iso 56 B) + MIDI (EP1 bulk 512 pair) |
  | OVERBRIDGE (1) | 6 | 0x403113b1 / 0x0b2b | 0x403112aa, 263 B | **vendor class 0xFF**: if1 EP3 IN interrupt (32/88/1012 B alts), if2 EP3 OUT interrupt (32/88/256 B), if3 **EP2 OUT bulk 512 + EP2 IN interrupt 64**; MIDI pair EP1 bulk 512 |
  | service (4) | 4 | 0x403111bc / 0xffff | 0x40311126, 75 B | CDC-ACM + CDC data (EP2 bulk 512 pair, EP3 IN interrupt) |

  The mode-to-setting mapping is read from `FUN_400cc864` (1 -> port 6, 2 -> port 5, else port 2) and the descriptor classes; the
  menu order (OVERBRIDGE, USB MIDI, USB AUDIO/MIDI) was not tied to the stored value **[O]**.
- EP0 handler `FUN_40005ff6` (0x40005ff6): standard requests plus vendor requests 0xC0/0x40 (0..5), 0xC1 (0x0A, 0x0B, 0x11, 0x12). No BOS
  and no string index 0xEE: Windows will not bind WinUSB by itself **[D]**. DEVICE_QUALIFIER is stalled (HS-only device).

## 2. SysEx receive path (what a MidiRpc byte goes through)

Stages, tasks and sizes **[D]**:

1. **ISR** `vector_134_handler`: walks the 8 queue heads, runs the dTD completion callback (dTD+0x24) with (bytes, dTD). EP1 OUT callback
   `FUN_40004292`: posts `FUN_40004504(len, buf)` to the deferred-call queue (`FUN_40000fe0`, 128 entries) and re-primes
   (`FUN_400041c2`). **Two 512-byte dTDs outstanding** at 0x4fe3d000 + slot*0x200 (one USB packet each).
2. **Deferred-call task** (entry `FUN_40000ea0`, priority arg 9, 4 KB stack, made by `FUN_40001118`): `FUN_40004504` splits the packet
   into 4-byte USB-MIDI events. **Per event it copies a 64-byte CIN length table onto the stack (`FUN_401360ac`)**, then a 9-instruction
   loop per payload byte (3 per event). About 95 instructions per event = 32 per wire byte. Cable bit 4 selects cable 0 or 1.
   Cable 0 goes to `FUN_4011d76c`, cable 1 to `FUN_4011d9a0`.
3. **SysEx assembler** `FUN_4011d76c` (cable 0): about 24 instructions per data byte, absolute globals. It builds linked 160-byte
   blocks (151 data bytes, 1024-block pool at 0x47db6b80 = 160 KiB). The message is queued at F7 only. When the pool is empty the
   task waits on a semaphore (`FUN_4000141a(0x47db4b70)`); `FUN_4011d564` frees blocks. Cable 1 (`FUN_4011d1f6`, contiguous buffer,
   32,767 data bytes at most) feeds queue 3, which the MIDI task pops and ignores; DIN feeds queue 1. **MidiRpc is cable 0 -> queue 2
   (sources 2/4)**, which fits finding 25 (device 0x10 handler `FUN_4011efb8` returns unless source is 2 or 4).
4. **MIDI task** (`FUN_4011edae`, priority 7, 32 KB stack, created in `FUN_401206d2`): `FUN_4011d5e0` gathers the blocks into one contiguous
   buffer at 0x44d2e8d8 with a **hard limit of 0x20000 bytes (128 KiB)**; larger messages return -1 and are dropped. Then the
   router `FUN_4011e9f0` -> `FUN_4011efb8` -> `FUN_40125184` -> factory `0x4013db36` -> `FUN_40152566`.
5. **[C] The device-0x10 body is 8-in-7 packed.** `FUN_4011efb8` passes (message + 6, length - 7) = from the byte after `10 00`, no F7. `FUN_40152566`
   unpacks it through a virtual `VectorOutputStream::vfunc_2` call per output byte (about 30 instructions per byte): the first byte of each
   8-byte group is the collector, data byte j (1..7) gets bit 7 from collector bit (7-j). **The "cmd" byte 6 in docs/MIDI-SYSEX-RPC.md section 11
   is the first collector** (0 for a header-only Ping, which is why it looked ignored); the `DeviceUID` reply is 5 bytes = 4 + 1 collector.
   docs/MIDI-SYSEX-RPC.md section 11 ("raw body, no packing"), `devrpc.build_request` and `RPC_WRITE` (raw 0x55 x 1024) are wrong for any payload
   byte >= 0x80 or longer than 7 bytes; this may be why finding 25 never found the written bytes. Net wire efficiency 7/8 on RX.
   The reply path has the same framing.
6. **Handlers** (same task): the FsSample requests are dispatched in `FUN_401283e2` (0x401283e2, 9.7 KB); copies on the write path: gather
   (1), unpack into a growing vector (doubling copies), `FUN_401d3b8e` message copy, `FUN_401dcd40` payload copy, then
   `FileOutputStream` (vtable 0x402257ec, `array::at` 0x401587d2 is its write) memcpy into a **128 KiB buffer** flushed by
   `FileOutputStream::vfunc_3` (0x4015897c) -> `FUN_4015893c` -> path filesystem -> `FUN_4012e0c0` (CMD25, eDMA, up to 64 KiB per command,
   direct DMA when 16-byte aligned).
7. **Reply TX**: `FUN_40004734` (called in the MIDI task) packs bytes into 4-byte events on cable 0 only, a 4 KB ring at 0x4fe3c000, and
   **spins** (`do {} while`) when the ring is full; TX efficiency 3/4. This matters for reading samples from the device, not for writes.

Per-byte cost (instructions per payload byte, lower bound; no allocator, no per-request work): USB split 32 + assembler 24 = 56 per
wire byte = 64 per payload byte; unpack about 30; copies about 3; so **about 100 instructions per payload byte**. Core clock: bus 132 MHz
measured, core 264 MHz (finding 07); busy CPI 1.37-1.43 (finding 07). That is about 190 M instructions per second at zero wait states, so a
CPU-bound ceiling of **about 1.9 MB/s with the whole CPU, about 1.0-1.4 MB/s with the other firmware load** (idle firmware already uses
37-47 % of the core; the manual says the sequencer stops during a transfer). Real misses (8-bit DDR2 port, 12-20 cycle line fills),
shared_ptr/allocation per request and the ISR add to it **[O]**: the measured 500 KB/s is within 2-3x of this estimate.

## 3. Transfer file-write protocol as the firmware implements it

- Requests (type byte after the 5-byte MidiRpc header, all packed): OpenFileForWrite 0x40 (path, NUL), WriteFileV1 0x42, CloseFileWriter 0x41,
  WriteFileV2 0x46 (answers "File is not open" in finding 25; layout unread **[O]**). All 44 advertised commands are in the Ping reply (MIDI-SYSEX-RPC.md).
- **WriteFileV1 request [D]** (ctor 0x401e0be8): `u32 handle, u32 length, u32 offset, data[length]`, big-endian, after unpacking. (finding 25's
  script used handle, offset, length; the ctor reads length second.) Handler `FUN_40127fa4`: handle must match; **offset must equal the current
  file position** (a repeat of the previous offset is accepted as a retry; anything else = status 6); optional checksum fields give status 4;
  status 0 ok, 1 not open, 2 write failed, 3 wrong handle. Appends only, no seek.
- **Acknowledgement**: one response per request (FsSampleWriteFileV1Response). Nothing in the firmware forces the host to wait: messages
  queue in the 160 KiB block pool and the in-order offset check makes pipelining safe. The DPC task keeps assembling the next message while the
  MIDI task writes the previous one; back-pressure comes from the pool semaphore and USB NAK.
- **Chunk size**: not set by the device. Limits: one SysEx message <= 128 KiB of wire bytes (gather buffer 0x20000) and <= about 150 KiB pool;
  so up to about 100 KiB of payload per request in theory. What Transfer sends is **[O]** (no capture in the repo with write traffic). The 1024-byte
  figure in finding 25 is the emulator test's choice.
- **Where the bytes go**: file bytes unchanged into the 128 KiB `FileOutputStream` buffer, flushed to the path filesystem (32 KB extent pages, 128-byte
  inode records, finding 14), CMD25 up to 64 KiB. After Close, `FUN_4015b0ce` -> `FUN_4015af0c` hashes the whole file (reads it back) **[D]**; cost for a
  300 MB sample unknown **[O]**.
- eMMC: CMD6 switches HS timing and 4-bit bus (`FUN_4012d4b2`: args 0x03b90100, 0x03b70100; PROCTL DTW = 4-bit). Bus ceiling 52 MHz x 4 bit =
  26 MB/s; the firmware's SDCLK divider was not decoded **[O]**. Real eMMC sustained program rate is unknown (part not identified).
- **Which of stage 2 or stage 3 makes 500 KB/s**: the CPU estimate (100 instr/byte, about 1.0-1.4 MB/s) is the right order and is the hard
  floor of any SysEx route; the rest of the gap (2-3x) is plausibly per-request round trips with small chunks plus host MIDI driver limits.
  Statically they cannot be separated. A one-hour bench settles it: write one file with chunk sizes 1, 4, 16, 64 KB and pipeline depth 1, 2, 4;
  throughput that rises with chunk size and depth = latency bound, flat at about 1 MB/s = CPU bound (needs Em's go: it writes a file to +Drive).

## 4. A vendor bulk interface

- **Where**: USB MIDI mode (port 2) uses only EP0 and EP1, so EP2/EP3 are free there. Patch points: (a) the config descriptor: it has no
  slack (next descriptor starts at 0x40311233), so copy it to a cave, append interface 2 (class 0xFF, EP2 OUT bulk 512 + EP2 IN bulk 512,
  30 bytes), set bNumInterfaces and the table pointer/length (table entry 2 at 0x4029ea90, +0xC and +0x10) and the immediate in `FUN_4000677a`;
  (b) the connect callback (`FUN_400044b8` -> `FUN_400043e2`) must also set QH 0x100 / 0x140 caps (512, ZLT), ENDPTCTRL2 (0xFC0B01C8) like
  EP1's 0xc800c8, and prime dTDs with `FUN_40005a86(2, dtd)` (copy `FUN_400041c2`); (c) a completion callback (ISR context) that queues
  the buffer to a task which calls the firmware's file API. The engine is endpoint-generic (dTD+0x24 callback), so no ISR change is needed.
  The dTD pool is small (8 shared) **[D]**.
- **Overbridge mode** already has a vendor interface 3 with EP2 bulk OUT 512; its protocol is unknown, so the safe design is a new interface in MIDI mode
  and, if wanted in Overbridge mode, EP2 shared by changing its handler **[O]**. Audio mode uses EP2 for feedback: not available there.
- **Bandwidth coexistence**: EP3 audio takes at most 1012 + 256 bytes per 125 us of about 7.5 KB per microframe; bulk keeps the rest. The limiting resource is
  the dTD pool and CPU, not the wire **[D]**.
- **Host side**: macOS and Linux Chrome WebUSB can claim a class-0xFF interface of a composite device (audio/MIDI interfaces are protected
  classes and cannot be claimed, so the existing MIDI pipe is not usable from the browser). Windows needs WinUSB bound: the firmware has no BOS/MS OS 2.0
  or OS string descriptor, so add them (about 1-2 days) or ship an .inf. Desktop: nusb/libusb on the vendor interface.
- **Risks**: no USB model in the emulator (message-level injection only) so enumeration is hardware-only; a bad descriptor leaves USB unusable
  but DIN OS upgrade still works (finding MIDI-SYSEX-RPC 7); descriptor length fields live in code immediates; the disconnect callback of port 2
  (`FUN_4000436e`) is a tight loop in the decompile, unexplained **[O]**.
- DN2 1.11: same controller and engine (callbacks at 0x400cedb2 / 0x4011f0e0 per finding 25) but no FsSample dispatch, so DN2 needs the vendor
  route or a new RPC regardless **[O]**.

## 5. Disk mode

- Controller init a standalone program needs (manual chapter 24, firmware init above): ULPI pin mux, MISCCR2[ULPI]=0, USB clock, USBCMD reset, USBMODE
  device (0x1e), QH list in SRAM, EP0 control enumeration, bulk pair, Bulk-Only Transport and SCSI (INQUIRY, READ CAPACITY, READ10, WRITE10,
  REQUEST SENSE), plus the eSDHC bring-up (about 400 bytes of register sequence in `FUN_4012d4b2`: reset, 400 kHz identify, CMD6 HS and 4-bit, eDMA
  channel 59).
- The cheaper variant of "own driver": run inside the firmware. Add port entry 7 (MSC descriptors) to the firmware's USB engine and serve SCSI
  READ/WRITE with `FUN_4012deda` / `FUN_4012e0c0` (multi-block CMD18/CMD25, up to 64 KiB per call). What the bootstrap leaves configured is unread **[O]**;
  MAIN OS configures USB and eSDHC itself, so the standalone program cannot rely on it.
- eMMC write ceiling: 26 MB/s bus (4-bit, 52 MHz) minus eMMC programming; realistic 8-20 MB/s **[O]**. USB HS bulk 35 MB/s is not the limit.
- The inbox: the module store (12 MiB) or a reserved region; the path filesystem allocates 32 KB pages across the card, so a larger FAT inbox for
  samples needs those pages reserved in its bitmaps **[O]**. The host mounts the volume in the OS file manager; WebUSB cannot (mass storage is a
  protected class), so the browser route does not exist for disk mode. Import at next boot through the mutex-guarded file API as in finding 23 S3.

## 6. Sample format

The device does **not** convert. The write path stores bytes as given (no RIFF/WAVE string anywhere in the image, finding 14), and the loader
requires the native file: 0x40-byte header (+1 stereo flag, +4 length, +8 rate 48000, +0x14 = 0x7F), big-endian int16 PCM, 16-byte trailer (finding 14
"The native sample file", **[V]** there). So the host (Transfer, our desktop app, the browser) converts: 48 kHz, 16-bit BE, as packages/emulator/plusdrive
does. The Query key `sample_file.interleaved_stereo_support` is only a capability answer **[D]**.

## Summary table

| route | ceiling | limiting stage | cost (agent days) | risk |
|---|---|---|---|---|
| A own SysEx (corrected framing, 32-96 KiB chunks, depth 2) | 0.8-1.4 MB/s | ColdFire per-byte chain (about 100 instr/B) | 1-2 | low |
| B vendor bulk interface | 5-10 MB/s (4-20) | path filesystem + eMMC flush path | 5-8 (+1-2 Windows) | medium (hardware-only test) |
| C disk mode (in-firmware MSC) | 10-20 MB/s | eMMC programming | 8-12 | high |
