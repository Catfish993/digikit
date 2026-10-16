# plusdrive-format

Builds sparse +Drive card images in the firmware's own on-disk format
(`docs/findings/14-plus-drive-format.md`). No firmware bytes are stored here.

| Module | What it holds |
| --- | --- |
| `format.rs` | Superblock, directory pages (multi-page, with the three index pages), name hash, native sample wrapper, project header |
| `tree.rs` | `build_tree_image`: nested directories, native samples, plain files, files with several (inline or indirect) extents, raw `MmcFs` slot import |
| `image.rs` | `SparseImage`, the flat `build_sample_image*` wrappers, error types |
| `wav.rs` | WAV to native sample (24 to 16 bit, resample to 48 kHz) |
| `project.rs`, `seed.rs` | The boot project of a verified DT2 1.16 firmware |
| `list.rs` | Root listing of a built image |
| `bin/plusdrive.rs` | The CLI |

```sh
mise run plusdrive -- build plus_drive/dt2 -o out/plusdrive/dt2.img --device dt2 --syx firmware/Digitakt_II_OS1.16.syx
mise run plusdrive -- ls out/plusdrive/dt2.img
```

`DIR` follows the `plus_drive/<device>/` contract in `docs/SAMPLES.md`: files and folders
as they are (a `.wav` becomes a native sample named without the extension), and
`.mmcfs/{projects,sounds,kits}/NNNN.bin` raw slots. `--max-extent-pages N` splits every
file into extents of at most N pages (only useful to test multi-extent records).

Tests: `cargo test -p plusdrive-format` (layout invariants, hash index search, a
multi-page directory, multi-extent and 64 MiB files, slot import); the firmware's own view
of a folder-built image is `mise run plusdrive:check`.
