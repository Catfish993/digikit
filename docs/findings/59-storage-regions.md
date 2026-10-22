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

## What this retires

**[C]** The claim in finding 23 S6 and `docs/design/digikit-store.md` that `0x38000..0x3E000` is "above every kit pool, below the sample area" and so free: it is below the 0x800000 line the crafted Data API reaches, and
12 MiB cannot hold samples. The version 3 store (`docs/design/digikit-store.md`) uses the areas above. Nothing in the repository places a store at `0x500000` or `0x600000`; those numbers were only ever in conversation.

## Open

- **[O]** The `StorageSpace` reply length (17 bytes here, 20 seen once on a real device): settle it with a capture (`docs/MIDI-SYSEX-RPC.md`, section 12).
- **[O]** None of the above is checked on a real card. A second agent has not read the guard and the layout from the image bytes (**[V]** is for that).
