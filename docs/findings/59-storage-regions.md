# 59. Card regions: what is free for the digikit store (DT2 1.17, DN2 1.12)

Plan: `docs/plans/storage.md` (F2). Marks as in AGENTS.md: **[D]** read or measured once here (static decompile plus reused write logs), **[O]** open, **[C]** corrects an
earlier claim. Nothing here is **[V]** (a second agent has not checked it). Nothing has run on a real device.

## Result

**[D]** The 12 MiB store range of version 2 (`0x38000..0x3E000`, finding 23 S6) is far too small for samples and is not where a large store belongs. Both cards are 0x2E1A000 sectors (about 23.8 GiB of
addressable sectors in the whitelist, 32 GB parts). Where the firmware writes, and where it never does:

| Device | Area (absolute sectors) | Use |
| --- | --- | --- |
| both | `0x00000..0x457FFF` | the firmware's own: boot, tables, projects, sound and kit pools, presets (finding 14, `[card.pools.*]`) |
| both | `0x457FFF` and up | **MmcFs refuses writes** (DT2 1.17 `FUN_4012ff92`, the `cmpi` at `0x4012ffd8`; DN2 1.12 `FUN_4012e866`) |
| DT2 1.17 | `0x458000` (64 sectors) | the factory sample table "MaGj"; never touch it |
| DT2 1.17 | `0x458000..0x5D7FFF` | 1265 factory samples; never touch them |
| DT2 1.17 | `0x5D8000..0x2DF017F` | the ekFS +Drive: superblock at `0x5D8000`; `erase(0x5d8000, 0x2818180)` in `FUN_4015a440`; the allocator stops at page `0xA0080` |
| DT2 1.17 | `0x2DF0180..0x2E19FFF` | free and never written (the card ends at `0x2E1A000`) |
| DN2 1.12 | `0x458000` and up | nothing stock writes here: FORMAT trims up to `0x457FFF`; the maintenance writes there are dead code |

**[D]** So the store area per device is:

- DT2: `0x2DF1000..0x2E1A000` (82 MiB), inside the free tail after the +Drive.
- DN2: `0x800000` to the card end (about 22 GiB). `0x800000` is also above everything the crafted Data-API hole below can reach.

## The facts behind it

- **[D] MmcFs guard.** On both devices MmcFs refuses a write at sector `0x457FFF` or above. DT2 1.17: `FUN_4012ff92`, the compare at `0x4012ffd8`. DN2 1.12: `FUN_4012e866`.
- **[D] Block driver.** `block_read(sector, bytes, buf)` checks `start < capacity`; the write checks `start + count <= capacity`; erase has no bound. The sector mode is sector (not byte) addressing. The runtime's
  card write function (finding 46) keeps its own range check on top.
- **[D] A crafted Data-API request reaches the card below `0x800000`.** Unchecked sound-bank and kit-bank ids in the Data API address every sector below `0x800000` on both devices, so a store below that
  line could be overwritten by a crafted request over USB. Areas at `0x800000` and above are out of its reach. The DT2's `0x2DF1000` is above it. This is the reason the old area (`0x38000..0x3E000`, below every pool the
  firmware owns) retires.
- **[D] DN2 FORMAT** trims to `0x457FFF` and nothing after it, so a store at `0x800000` survives a format.
- **[D] The +Drive on the DT2** ends at page `0xA0080`: `0x5D8000 + 0x2818180 = 0x2DF0180`. The first store sector `0x2DF1000` leaves `0xE80` sectors (1.8 MiB) of margin.

## Cost of a large upload on the ColdFire (step 3)

- **[D] The runtime's SHA-256 was about 1000 ColdFire instructions a byte** (emulator, `upload-folders`: VERIFY of 1 MiB took 1.06e9 instructions with the card read costing under 1e6). A 6 MiB file's END, which read the file
  back and hashed it, ran out the host's 4e9-instruction wait. An unrolled block function with an in-place 16-word schedule and no checked index gives about 120 instructions a byte (1.27e8 for 1 MiB) **[D]**.
  The hash now runs as the DATA chunks arrive (one MiB of DATA: 2.18e8 instructions, END 4e5), so END costs the same for any size. VERIFY still reads the card; it takes a range (runtime 0.3.0), so a host checks a large file in pieces.
  On the device's clock (not measured) 120 instructions a byte is the figure to scale from.
- **[D] DATA already wrote straight to the card**: the 32 KiB chunk goes through the upload buffer to the file's extent and nothing is staged whole, so the file size is limited by the store's free space only (a 6 MiB file ran on both devices).

## What this retires

**[C]** The claim in finding 23 S6 and `docs/design/digikit-store.md` that `0x38000..0x3E000` is "above every kit pool, below the sample area" and so free: it is below the 0x800000 line the crafted Data API reaches, and
12 MiB cannot hold samples. The version 3 store (`docs/design/digikit-store.md`) uses the areas above. Nothing in the repository places a store at `0x500000` or `0x600000`; those numbers were only ever in conversation.

## DT2 sample upload over MidiRpc, run in the emulator (step 4)

Scenario `samples-live` (DT2 1.17, free-running daemon, `tools/devtools/samples_live.mjs` drives the TypeScript client over the control socket; 24 checks, 58 s). **[D]** run once; nothing here is **[V]**.

- **[D]** A 44.1 kHz WAV converted by `dksample.wasm` (4410 frames become 4800), sent in three chunks to `/live/test`, closed with the byte count, is listed by ReadDir with the size sent and read by
  `plusdrive fs cat` from the guest's card writes: the bytes sent plus 16 zero bytes, equal to the file `plusdrive build` makes of the same WAV.
- **[D]** The firmware's `hash|1` (ReadDir, GetFileInfoV2, the card record) is `content_hash(bytes sent ++ 16 zero bytes) | 1`. The host computes the same over what it sent. A changed byte on the host side, and a path the device does not have, report failed.
  What is compared: the firmware's hash of the stored file with the host's hash of the bytes sent and the zero trailer. The trailer of a file written over MidiRpc is zeros in the emulator; on a real device it is **[O]** (a trailer with other bytes would make every verify fail, and the fallback is a read-back).
- **[C]** `OpenFileForWrite` needs the leading slash (a bare name fails and leaves a folder); finding 25's and `docs/MIDI-SYSEX-RPC.md` section 11's "no leading slash" holds only inside a folder. Details in section 12 there.
- **[O]** `ReadFileV1` answered status 0 for every layout tried, so no read-back exists; the hash check does not need one.

## Proof of the new area and of the size check, run in the emulator (step 7)

- **[D]** Module install, restart, load and delete in the per-device area ran in the original Python `transfer-live` on both devices (then quick tier; current coverage: `tests/regression/runtime/transfer-live.ts`, explicit/full-only runtime acceptance): the file is listed in `modules/`, MODULES shows the module after the restart, the
  mirror folder holds it with the card's sha256, a delete removes it everywhere and the next boot skips the missing path. Full tier: `upload-ops`, `upload-folders`, the power-cut scenarios.
- **[D]** A card smaller than the area (`card-small`, both devices, the emulator's `small` identity with 0x3B0000 sectors): the runtime's check reads the area's last sector, the firmware's block read refuses it (`start < capacity`,
  above), the state is "too small" (5), the log gets one error event, uploads answer E_NOSTORE, CAPACITY STORE is 0 KiB, writes inside the area answer E_CARD_RO, nothing is written there, the boot reaches the UI.
  The emulator's capacity comes from the identity, not from the image file, so this proves the runtime's reaction and the firmware's refusal, not the real cards' capacity (open, below).

## Open

- **[O]** The `StorageSpace` reply length (17 bytes here, 20 seen once on a real device): settle it with a capture (`docs/MIDI-SYSEX-RPC.md`, section 12).
- **[O]** None of the above is checked on a real card. A second agent has not read the guard and the layout from the image bytes (**[V]** is for that).
- **[O]** The real eMMC capacity of a DT2 and a DN2 (0x2E1A000 is assumed from the whitelist), that the stock firmware never writes either area on a used device, where the DT2's +Drive allocator really stops, the DT2 sample browser listing an uploaded file, the RPC chunk size on real USB, and whether a real device's trailer of a file written over MidiRpc is zeros. The list with the checks: `docs/plans/storage.md`, "Needs Em's real devices".
