# Storage: a large digikit store, samples on both devices (F2, 2026-10-22)

Status: in progress on lane `tasks-images`, one commit per step. Rules: device facts only in `devices/*.toml` and `devices/ports`; new module and runtime code is Rust (no new C); Python only for
reverse engineering and dev tooling, the app starts no process; pre-1.0, so no migrations (existing modules are lost on a reflash and installed again).

Facts: `docs/findings/59-storage-regions.md`, `docs/MIDI-SYSEX-RPC.md` section 12.

## Design

- **Store format v3** in a per-device area from `[card.store]`: DT2 `0x2DF1000..0x2E1A000` (82 MiB), DN2 `0x800000` to the card end (about 22 GiB). Folders `modules/` and `samples/`. The old area
  `0x38000..0x3E000` retires everywhere (profiles, runtime, builder, reader, exporter, mirror, docs). Needs: folders (paths with `/`, listed per folder), reusable space (allocate into gaps), paths of at least 160
  bytes, more entries, streaming upload of large files (DATA chunks go to the card, not staged whole). Kept: copy-on-commit table and headers, power-cut safety, sha256 per file and VERIFY, the module load staging
  limit (64 KiB).
- **Runtime 0.3.0**: checks at boot that the card capacity covers the area; if not there is no store, it logs that and the boot goes on.
- **DT2 samples** go to the +Drive through the stock MidiRpc file commands (TS client, over the existing transports); the host converts WAV to the native file (the converter of `packages/emulator/plusdrive`
  compiled to wasm). **DN2 samples** go to the store's `samples/` folder, in the same native format, so a later sampling engine reads one format on both devices (the DN2 firmware cannot play them yet).
- **Workbench:** Transfer gets Modules and Samples, the same on both devices; the device layer picks the backend from the profile (`[storage] modules = "digikit"`, `samples = "stock-rpc" | "digikit"`).
  The Card panel and the mirror cover the new area (`plus_drive/<device>/digikit/...`).

## Steps

1. [x] Findings: `docs/findings/59-storage-regions.md`, the file-command table in `docs/MIDI-SYSEX-RPC.md` section 12.
2. [x] Store v3: format doc, profile areas, runtime, builder, reader, exporter, mirror, CLI, tests; retire the old area. Runtime 0.3.0. Decisions: the table is sorted by first sector and streamed (no entry limit), free space is reused (first fit), E_FULL carries the sum of the free space, the ownership check reads the markers and the first 1 MiB, empty folders are not built from a folder (the runtime makes folder entries in step 3), HEALTH store figures are KiB.
3. [x] Large files, folders on the wire, free space: LIST by prefix (paged), MKDIR (0x18), VERIFY by range, CAPACITY kind STORE in KiB, capability bit 10 `FOLDERS`; host clients in Python
   (`Uploader.ls(prefix)`, `mkdir`, `put_file`, `verify_file`), Rust (`Session::list_prefix`, `mkdir`, `verify_range`, the mock follows) and TypeScript (`DkSession.list(prefix)`, `mkdir`, `verify`, `verifyFile`).
   Decisions: DATA already went straight to the card (32 KiB chunks, nothing staged), but END read the whole file back and hashed it with a loop that cost about 1000 ColdFire instructions a byte (minutes for a
   sample): the hash now runs as the chunks arrive and END compares it; the block function is unrolled (about 120 instructions a byte); VERIFY reads the card and takes a range, so a host checks a large file in
   pieces. A folder entry is dropped when something is stored inside it (folders with files are implied). Scenario `upload-folders` (both devices): MKDIR, prefix paging (40 entries), a streamed 6 MiB upload,
   delete frees space and the next file takes the first gap, CAPACITY STORE, ranged VERIFY, +Drive round trip.
4. [x] DT2 samples: the TS MidiRpc file client (`apps/web/src/modules/rpcfiles.ts`, over `DkSession.rpc`), the converter and hash as one crate (`packages/dksample`, shared by `plusdrive-format`, built to
   `apps/web/public/dksample.wasm`; `apps/web/src/modules/dksample.ts`), profile `[storage]` (`samples = "stock-rpc"` DT2, `"digikit"` DN2, `modules = "digikit"`; `Registry::storage`), `plusdrive fs ls|cat`,
   scenario `samples-live` (full tier; `tools/devtools/samples_live.mjs`), node tests `rpcfiles` and `dksample`. Decisions: a WAV is converted, a native file goes without its trailer, any other file unchanged. The upload is
   verified by the firmware's `hash|1` against the host's hash over the bytes sent and a 16-byte zero trailer (proved in the emulator, not on a device). Findings: leading slash needed, one writer, no ReadFileV1 yet
   (finding 59, `docs/MIDI-SYSEX-RPC.md` section 12). Not done: the on-device sample browser check (needs menu navigation, not cheap), the page using the client (step 6).
5. [ ] DN2 samples through the store, and a scenario.
6. [ ] Workbench Samples tab and the backend choice; the Card panel.
7. [ ] Proofs on both devices (module install, load, delete in the new area; the capacity check with a small emulated card), this plan, the cookbook.
