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

1. [ ] Findings: `docs/findings/59-storage-regions.md`, the file-command table in `docs/MIDI-SYSEX-RPC.md` section 12.
2. [ ] Store v3: format doc, profile areas, runtime, builder, reader, exporter, mirror, CLI, tests; retire the old area.
3. [ ] Streaming upload of large files, LIST per folder, delete frees space, INFO/CAPACITY report store free and total; TS `DkSession` and Python.
4. [ ] DT2 samples: the TS MidiRpc file client, the wasm WAV converter, a DT2 scenario.
5. [ ] DN2 samples through the store, and a scenario.
6. [ ] Workbench Samples tab and the backend choice; the Card panel.
7. [ ] Proofs on both devices (module install, load, delete in the new area; the capacity check with a small emulated card), this plan, the cookbook.
