# 44. Modules as files in a folder of the Digitakt II path filesystem (store spike, static plus bounded emulator runs)

Plan: `docs/plans/digikit-runtime.md` Phase 9 ("Store spike (DT2)"). Lane r1, 2026-10-18. Question (Em): keep `.dkm` files as ordinary
+Drive data in a folder (say `/digikit`) of the DT2 sample ("path") filesystem instead of the raw store range `0x38000-0x3E000`, so the
firmware allocates the space and the files show in Transfer. Background: finding 23 S3 (C file API), finding 14 (format), finding 25 and
42 (FsSample writes), finding 37 (boot hook), `docs/design/digikit-store.md`, `docs/design/digikit-upload.md`.

Images: DT2 1.16 (decompile and emulator), 1.17 where stated (function bodies compared by loose hash only: the picker's loader, the directory
load and the delete handler exist there at shifted addresses, `0x4015481c`, `0x40159d08`, `0x4002b35a`/`0x4002b4c2`). Nothing ran on a
device. Marks per AGENTS.md: all **[D]** (read or measured once, by one agent) unless marked **[O]**; no second agent has checked any of
it against the image bytes, so nothing is **[V]**. Addresses are DT2 1.16.

## Result in one paragraph

The folder route works on the Digitakt II and is cheaper than it looked. (1) The mount happens before the boot hook, so the firmware's own
file API can read the modules at the hook. (2) The earlier "FsSample writes never reach the card" was a wrong test input (wrong handle), not
an emulator gap: a correct host write puts the bytes on the card at once, and creates the folder itself. So the host can upload with the
firmware's own FsSample requests and the runtime needs no write code for DT2 modules. (3) The sample picker shows the folder and its files,
does not refuse a `.dkm`, and would load it as noise if the user picked it for a track; and the user can delete, rename or move the files
from the device. (4) The small boot record (selection and boot counter) stays in two raw sectors. DN2 has no path filesystem, so its raw store
stays. Recommendation at the end.

## 1. The sample picker

The file browser is `SampleManager` with `SampleBrowserPopupMenuView` and `SamplePicker`; `SampleFSSelectView` (`0x4001efac`) is the
"PROJECT RAM / RECONF. +DRIVE" memory screen, not the browser **[D]**.

**What it lists. Everything, with no name or type filter [D].** The directory load is `FileSystemDirectory` `0x40159a2c`:

```c
iVar3 = FUN_40157072(iVar1,local_18);            // readdir
...
while( true ) {
  iVar3 = FUN_40157072(iVar1,local_18);
  if ((iVar3 < 1) || (local_18 == 0)) break;     // push the entry, no test on name, type, size or header
```

The only conditional is `FUN_401596a8` -> vfunc `0x30` (`FileSystemDirectory::vfunc_12`, the "valid" flag at `+0x120`) -> `FUN_4015bc56`
(`id == 2`): it skips the leading `.` and `..` entries of the root. A search of the decompile for `'.'` / `0x2e` finds only path
normalisation (`0x40152010`, `0x40151ea8`), so a name that starts with "." is not hidden by anything found. A root folder `digikit` and the
files in it show **[D]**. The row builder and draw code of `SampleManager` (which turns entries into rows, icons per type byte) was not
read, so a hidden-name or type rule there is not excluded **[O]**. Consequence: a `.`-prefixed folder name is no protection; the folder is
visible, which the plan already expected.

**What happens when a `.dkm` is chosen. No refusal, no error [D].** The picker only stores a 16-byte reference (`{id, hash|1, size, seq}`)
in the project; the data load is `FUN_40154540` (`sample_loader_load_sample`, from the project-reload paths `0x4004b502`, `0x4004b60a`,
`0x4004e8be`), which

- resolves the reference with `FUN_4015b178`; it needs the record's `hash|1` word at `+0x0c`, which the firmware sets itself at file close
  (`FUN_4015b0ce`), so a file written through the filesystem passes;
- needs the slot below `0x400` and the rounded size `(size + 0x200f) & ~0x1fff` below `0x19000001` and within the free pool; otherwise the
  popup says "SAMPLE MEMORY FULL" or "SAMPLES EXCEED AVAIL MEMORY" (a clean refusal for a large file only);
- copies the first `0x40` bytes and tests only `header[1] == 1` (stereo, de-interleaved) else mono; it never reads the length (`+4`) or the
  rate (`+8`) and does not compare the size with `length + 0x50`. The loop runs until the bytes read equal the file size.

So a `.dkm` of 4 to 100 KB loads as mono noise and costs its rounded size in sample RAM; if byte 1 of the file happens to be 1 it is read as
stereo. No division by zero, no huge allocation and no page-count mismatch was found on the path. A header that imitates a native header
adds nothing, because nothing is validated. Not read: files smaller than `0x50` bytes (the tail arithmetic) and the DSP-side page transfer
of odd lengths **[O]**. This was not run: no panel script in the repo reaches the picker, and the only attempt (finding 07) died on a null
string in `SampleManager::vfunc_40` on a cardless boot, which later cards no longer show, but nobody has re-driven it **[O]**. A short
emulator run (SRC, encoder press, a card with `plus_drive/dt2/digikit/x.dkm`) would settle the "plays noise, does not crash" claim; it is
not needed for the decision.

**The user can destroy the files [D].** The browser has "DELETE %d SAMPLE%s?" (handler `0x4002b35a`), "DELETE DIR?" (`0x4002b62a`,
"ERROR (NOT EMPTY?)"), "RENAME", "CREATE FOLDER", "MOVE SELECTED"; Transfer can delete too. The only guard is "WRITE PROTECTED" /
"WRITE PROTECTED FOLDER", from record byte `+1` bit 0 (`FileSystemDirectory::vfunc_9`; `/factory` has it). Nothing was found that sets
that bit on a user folder, and a parent with the bit set refuses new children (`FUN_40158626`), so setting it would also block the
firmware's own writes **[O]**. The runtime must therefore treat a missing file as normal (skip the module, say so in HEALTH), and the
`.dkm` needs its own integrity check (below).

## 2. Mount timing

**The path filesystem is mounted before the boot hook runs [D, static, confidence high].** The mount is in the boot task `FUN_400cc864`
(1.17: `FUN_400cc9c8`), which has no callers and ends in an endless loop:

```c
if (_DAT_42940a48 == 0) {                              // eMMC identity check passed
  FUN_4015a43a();                                      // creates the fs mutex 0x46f4deb0, clears _DAT_44f2bd68
  if ((DAT_4029e9b0 & 0x10) == 0) FUN_4015a450(0);     // mount: reads sector 0x5d8000, sets _DAT_44f2bd68 = 1
  else FUN_4015a424();                                 // format request
}
...
FUN_40006c52(uVar2);                                   // scheduler/timer setup
if ((DAT_4029e9b0 & 0x40) == 0) { FUN_401206d2(); FUN_4015394a(); FUN_4003324e(); }
```

`FUN_4003324e` is the last call: `FUN_400012c8(0x40966ee8, FUN_400337ba, 6, stack, 0x28000); FUN_40001314(0x40966ee8);` creates and starts
the main-OS task whose entry is our hook (`0x400337ba`). That task does not exist while the boot task mounts, so the order is sequential
with nothing to wait for. In 1.17 the order (flag word `DAT_4029eef0 & 0x10`, then `FUN_40006c52`, then `FUN_4003324e` at `0x400cce4a`) is the same,
not compared byte for byte.

- Finding 37's constraint (machine rows before the stock machine list is built) is met by the existing hook; **no second hook is needed**.
- The file API is usable at the hook: the mutex exists, the mount flag is 1. Finding 37 shows the block read and write work there. **An
  actual `open`/`read` call at the hook has not been run [O]**; it is the first step of the implementation (an hour).
- Caveats: the flag is 0 (every file call returns -1) when the eMMC identity check fails, when the superblock magic, checksum or version
  (3 or 4) is wrong, or on a format request (`DAT_4029e9b0 & 0x10`). The runtime must treat -1 as "no modules" and still boot. A mode with
  bit `0x40` skips most of the boot task and was not looked at **[O]**. The mount time on a device is **[O]**.
- What the boot task does right after the mount (`FUN_4012d0dc`, `FUN_4002dc62`, `FUN_400d766a`, `FUN_400d30c2`, `FUN_400d8dfa(1)`) was
  not traced **[O]**.

**The alternative, a read-only path reader in the runtime over the block read, is not needed**, and would only matter if the file API
turned out unusable at the hook. The block read does not need the mount flag (the capacity bound is set by the eMMC init before). Size if
wanted: record for id at sector `0x5d8180 + (id / 256) * 0x40`, offset `(id % 256) * 128`; content page `p` at `0x5ee180 + p * 0x40`
(ids at or above `0x1000800` use the region at `0x458000`); root id 2; a linear scan of the directory entries (`id u32, slot u16, name length u8, kind u8, name`) is enough for a handful of
files; extents `{logical, pages, physical}` inline up to 8, indirect above. About 150 to 250 lines of `no_std` Rust, 2 to 4 KB of code (the std
reader `packages/emulator/plusdrive/src/reader.rs` is 759 lines, about 100 of them the record/extent core) **[D, estimate]**. Its risks: it
depends on the on-disk format being equal on 1.16 and 1.17 (the superblock takes versions 3 and 4), it cannot lock against a firmware write
(read only at boot), and a card whose superblock checksum fails shows no files anyway.

## 3. The write path

**[C] Finding 25 and 42 section 3: the FsSample bytes did reach the card; the old probe sent the wrong handle.** Reproduced twice on DT2
1.16 (bounded 10 s runs through `tools.coldfire.panel_script._rpc` and `arena_safety.run`, an `OpenFileForWrite` of `digikit/t.bin` and a
3000-byte `WriteFileV1`):

- The reply of `OpenFileForWrite 0x40` is `01 <u32 handle>`. For a new file the handle is the new **record id** (4 here), not 0. The write
  handler `FUN_40127fa4` compares the request's handle with `FUN_40127d9a` (the open File's id, `File+8`). A mismatch sets status 3 and
  calls `FUN_40127f28`, which destroys the stream, so the file closes **empty** (hash of nothing, `43fa243b`). The old probe sent handle 0:
  `FUN_40127b36` and `File::write` were never reached (0 PC hits).
- The first reply byte of `WriteFileV1` is 1 on success (then a `u32` position); the old text read the 0 as "status 0 = ok", but 0 was the
  failure. Both findings' "status zero" is wrong in that sense.
- With the handle 4 in request and close: `/digikit` became record 3 (directory), `t.bin` record 4 (file, size 3000), content page `0xa7a` at
  sectors `0x618000-0x618005`; the 40-byte needle from the middle of the data is found there byte for byte; `hash|1` is in record `+0x0c`.
  PC hits: open 1, write handler 1, `File::write` 2, close 1. Every sector the run wrote is in `0x5d8000-0x5f017f` or `0x618000` and above;
  **none in the module store range** (`0x38000-0x3E000`) **[D]**.
- Why it looked like a hold-back: the old run's path had no `/` (`arenaprobe.wav`), and `File::ctor` in write mode first removes the path
  and then does `mkdir -p` of the dirname (`FUN_40151d1c` dirname, `FUN_40158c28` mkdir -p). With no parent part it made a **directory** named
  like the file, which is the "record is a directory" the finding saw. Transfer always sends a folder path.
- **There is no deferred flush [D].** `File::write` (`FUN_4015838e`) allocates pages (`FUN_4015816c`) and writes full 32 KiB pages straight
  to the card (`FUN_40155ba8` -> block write `FUN_4012e0c0` at `page * 0x40 + 0x5ee180`); partial pages are written back at once
  (`FUN_40155958`); the record is read-modify-written on each write (`FUN_4015b58c`); close (`FUN_401585e4`) only clears the "open" bit and
  the RPC close (`FUN_4015b0ce` -> `FUN_4015af0c`) hashes (reads the file back). The 128 KiB `FileOutputStream` buffer is flushed after every
  write request (`FUN_40127b36`). The caches (16-slot record cache, page cache) are write-through; no flush-all exists and none is needed.
  A 20 s inactivity timer (`FileWriter`, 500 ms ticks, `FUN_40127f62`) closes an abandoned file. So this was a test-input error, not an
  emulator gap and not a missing protocol step.

**Folder creation: yes [D].** Two ways: the open itself (a path with a folder part gets `mkdir -p`; one request made `/digikit`), or
`FUN_4015bede(path)` (mkdir; builds the record with `FUN_4015bd48`: bytes `01 07`, size `0x8000`, content page 0 and the three index pages;
its static callers are the mount and `File::ctor`). The `CreateDir` RPC (0x11) exists but its handler is in an undecompiled part of the big
RPC function and its payload is unread **[O]**; it is not needed. No bit hides the folder. A parent link count of 2000 or more fails.

**The C-level API for the runtime [D].** `FUN_40158626(path, mode, h)` returns -1 when the mount flag is 0 or the path is 256 bytes or more;
the path walker `FUN_40159f50` returns `0x19` for a missing last name with an existing parent (open then links a new id with `FUN_401568fc`),
and a missing middle component makes open return -1: **the C open does not create parents**, only the File class does. An existing file of
the same name is deleted first. Write `FUN_4015838e(buf, len, h)`, close `FUN_401585e4(h)`, read `FUN_4015832e`, size `FUN_4015ba22`.
The mutex `0x46f4deb0` (`FUN_400015a0` / `FUN_400016d2`) is owner-aware: a task that holds it takes it again without blocking, but one
unlock releases it fully, so open, write, close in sequence from one task are safe and a locked call must not nest in another. The FsSample
handlers run in the MIDI/RPC task (dispatcher `FUN_401243c6` -> `FUN_401283e2`). Stack depth and CPU per page were not measured **[O]**:
about 64 sectors plus a 32 KiB record read-modify-write per page, and the close hashes by reading the file back.

**Two ways to upload into the folder:**

- **A. The host uses the firmware's FsSample requests** (`OpenFileForWrite 0x40` with `u32 0` + `digikit/NAME.dkm\0`; `WriteFileV1 0x42`
  with `{handle, length, offset, data}`, offsets in order, one reply each; `CloseFileWriter 0x41` with the handle). The handle is read from
  the open reply. Nothing runs on the device that we wrote. Same route that Transfer takes; atomicity is the firmware's (the old file is
  deleted at open, a cut leaves a short file). The 8-in-7 framing is `tools/device/devrpc.py` (finding 42). Not available on DN2.
- **B. The runtime writes** (from our existing MIDI-task SysEx hook, open "w" + write + close). It is possible (above) but adds the code and
  the stack/mutex questions for no gain on DT2.

## 4. The boot record

Keep the selection and boot-counter record (`docs/design/digikit-store.md`, "The selection record") in **two raw sectors**, as now. Reasons:

- It is written before any module runs and at every boot that has modules (counter raise, clear 30 s later); finding 37's order depends on
  it surviving a cut. A file write rewrites a record, may allocate, and hashes: heavier, and an interrupted write of the filesystem's own
  metadata at power-off is the case this record must never be part of. Two raw sectors with CRC and sequence number are already proven.
- A file in the user's folder could be deleted or renamed, which would silently clear safe mode.
- The firmware allocates nothing in `0x3dffe-0x3dfff`; the Phase 2 write-watch and the Phase 6 cold-boot proof saw only the runtime write
  there. It is the one place the "no collision by construction" argument does not hold; two sectors in an otherwise unused range, with the same
  evidence as today, is a small remaining risk. A cheaper range than 12 MiB is enough: `first_sector`..`end_sector` shrinks to the record
  (the upload's header copies and staging area go away on DT2).
- The record's paths become folder file names (`dyn-pluck.dkm` or `/digikit/dyn-pluck.dkm`, at most 55 characters as now).

## 5. Integrity of a module file

The raw store gets integrity from the upload's SHA-256 and the table CRC. A file in the folder has only the firmware's `hash|1` (it makes
the sample loader's reference resolve; it is not checked for us). A short or torn `.dkm` (a cut during upload, the 20 s inactivity close, a
user who half-deleted) must be refused by the loader. Does the `.dkm` already carry a length and a check? The upload design says nothing
about a field inside the file **[O]**. Add a CRC32 (or SHA-256 first 8 bytes) trailer and the total length to the module header in
`patchkit pack` and check both before relocation; a loader refusal is then a HEALTH line, not a crash. Host tools keep the hash of what they
sent and can read it back with the firmware's `ReadDir`/`Query` (size) and the close reply.

## Recommendation

**Do it for the Digitakt II: modules live in `/digikit/*.dkm`, the runtime reads them with the firmware's file API at the existing boot
hook, the host uploads with the firmware's FsSample requests, and the two selection sectors stay raw. The Digitone II keeps the raw store.**

Why: the firmware owns the space, so no module collides with a sample; Transfer shows the files (whether Transfer lists non-sample files
at all is **[O]**; the browser on the device does); the DT2 gets no upload protocol of our own (BEGIN/DATA/END, the SHA-256 that costs 91 %
of the upload CPU, header copies and CLEAR repair all stay only for DN2); a pure-Python/host route is the same one Transfer uses.

Costs and risks (agent time; Em's time for the device checks not counted):

| Piece | Agent time |
| --- | --- |
| Runtime: module reader over `open/size/read/close` behind the store interface, "missing/short file" paths, HEALTH lines, hook-time proof that `open` works (anchors by hash on 1.16 and 1.17) | 0.75 to 1 day |
| Module trailer (CRC and length) in `patchkit pack` and the loader, tests | 0.5 day |
| Host: `dkupload` FsSample mode (handle from the open reply, in-order writes, close, read-back by `Query`/`ReadDir`), emulator scenarios (upload, cut, replace, delete by the user) | 1 day |
| Builder and exporter: on DT2 `plus_drive/dt2/digikit/` becomes an ordinary folder (no `digikit/` store routing, no `digikit~` renaming), `plusdrive:check` | 0.5 day |
| Device TOML and docs (design store/upload, SAMPLES.md, PATCHING.md) | 0.5 day |
| **Total** | **about 3 to 3.5 days; confidence 75 % that it works first time on the device, 90 % in the emulator** |

Open risks: (a) the user deletes, renames or loads a `.dkm` from the sample browser (a deleted file costs a module until re-uploaded; a picked
file costs sample RAM and plays noise), (b) a power cut during a firmware write has the filesystem's own exposure (not ours; samples have
it), (c) `open` at the hook unproven, (d) the 20 s inactivity close and the hash-at-close time for a larger file are unmeasured on a device,
(e) DT2 1.17 addresses of the file API are found by hash and not yet checked.

If (a) is unacceptable, the fallback is today's raw store (no work). A hybrid (module in the folder, a verified copy in the raw store) is
possible but doubles state and is not recommended.

**What stays in `devices/digitakt-ii.toml` as data** (new `[card.modules]` table; the raw `[card.store]` shrinks to the record for DT2):
`kind = "path-folder"`, `dir = "digikit"`, `ext = ".dkm"`, `max_name = 55`; `[card.store]` `selection_sectors = 2` with the range reduced to
the record sectors (`0x3dffe-0x3dfff`); the mount flag word (`0x44f2bd68` on 1.16, 1.17 by anchor rule `dkrt.fw.fs_mounted`), the file API
anchors (`open`, `read`, `size`, `close`, `lock`, `unlock`; found by hash like `dkrt.fw.block_write`). The Digitone II keeps
`kind = "raw-store"` and the whole of `[card.store]`.

## Repeat

```text
uv run python -I <script> sub4 OUTDIR     # DT2 1.16: OpenFileForWrite digikit/t.bin, WriteFileV1 handle 4, Close 4, ReadDir /digikit
```

The script was a scratch file built on `tools.coldfire.panel_script._rpc` / `arena_safety.run` with `--pc` counters on `0x40158626`,
`0x40127fa4`, `0x40127b36`, `0x4015838e`, `0x401585e4`, `0x40158c28`, `0x4015bede`, `0x40156d9c`; the card overlay (`overlay.bin`) is read for
the data sectors. It is not kept as a tool; the implementation will replace it with a scenario in `tests/digikit/scenarios/`.
