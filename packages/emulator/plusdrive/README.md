# plusdrive-format

Builds sparse +Drive card images in the firmware's own on-disk format
(`docs/findings/14-plus-drive-format.md`). No firmware bytes are stored here.

| Module | What it holds |
| --- | --- |
| `format.rs` | Superblock, directory pages (multi-page, with the three index pages), name hash, native sample wrapper, project header |
| `tree.rs` | `build_tree_image(tree, TreeOptions { layout, .. })`: nested directories, native samples, plain files, files with several (inline or indirect) extents, raw `MmcFs` slot import, boot and working project regions, the module store |
| `folder.rs` | A `plus_drive/<device>/` folder as a `Tree` (`scan_folder`), `write_image_file` |
| `store.rs` | The `digikit/` module store: encoder, reader (`docs/design/digikit-store.md`) |
| `image.rs` | `SparseImage`, the flat `build_sample_image*` wrappers, error types |
| `wav.rs` | WAV to native sample (24 to 16 bit, resample to 48 kHz) |
| `project.rs`, `seed.rs` | The boot project of a verified DT2 1.16 firmware |
| `list.rs` | Root listing of a built image |
| `reader.rs`, `export.rs` | Read the path filesystem and the `MmcFs` slots back; `plusdrive export` (finding 24) |
| `bin/plusdrive.rs` | The CLI |

The card layout (pool addresses, project regions, path filesystem, store range) is not in this crate: it is the `[card]`
section of `devices/<device>.toml`, parsed by `device-profile` into `CardLayout`, which every entry point takes
(`card_layout("dt2")` looks one up by the device's short name). A new device needs a TOML section, not Rust. The crate
parses no TOML itself.

```sh
mise run dev:plusdrive -- build plus_drive/dt2 -o out/plusdrive/dt2.img --device dt2 --syx firmware/Digitakt_II_OS1.16.syx
mise run dev:plusdrive -- ls out/plusdrive/dt2.img
mise run dev:plusdrive -- store ls out/plusdrive/dt2.img --device dt2     # the digikit/ module store
```

`DIR` follows the `plus_drive/<device>/` contract in `docs/SAMPLES.md`: files and folders
as they are (a `.wav` becomes a native sample named without the extension), and
`.mmcfs/{projects,sounds,kits}/NNNN.bin` raw slots, `.mmcfs/boot/SSSSSS.bin` project regions, `digikit/` for the module store. `--max-extent-pages N` splits every
file into extents of at most N pages (only useful to test multi-extent records).

Tests: `cargo test -p plusdrive-format` (layout invariants, hash index search, a
multi-page directory, multi-extent and 64 MiB files, slot import, boot regions, the store); the firmware's own view
of a folder-built image and of the store (block read, DT2 and DN2) is `mise run dev:plusdrive-check`.
