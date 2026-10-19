# 35: C modules on the ColdFire V4e, and what the MmcFs bitmaps are

Two items of the digikit runtime plan (docs/plans/digikit-runtime.md): the C-module libgcc (Phase 3) and the MmcFs bitmap
polarity (Phase 1). Marks: **[V]** verified here, **[D]** measured or read once, **[O]** open, **[C]** corrects an earlier
claim. Nothing is [V] until a second agent has checked it against the image bytes.

## 1. libgcc for `-mcpu=54418`

### The problem **[D]**

`pk_toolchains m68k` configures GCC 16.2 with `--disable-multilib`, so the installed `libgcc.a` is built for the default
CPU (68000; ELF flags 0). It is not ColdFire code: the new checker (`tools/coldfire/isacheck.py`, below) refuses it
(`_mulsi3`, `_divsi3`, `_double`, and more). A C module that needs a helper (`a / b` or `a << n` on `long long`, `__muldi3`,
soft float) compiled, but its helper stayed an undefined symbol that `pk_build` turned into an extern the patch-time linker
or the runtime (`E_IMPORT`) could not resolve. Linking the stock helpers in would have put 68000 forms into a V4e.

### Choice **[D]**

A `libgcc` rebuilt for `-mcpu=54418` from the same GCC tree, not the Rust `compiler_builtins` helpers.

- It is cheap: `pk_toolchains libgcc-cf` copies the Makefile of the existing build tree (`out/tools/build/gcc/m68k-elf/libgcc`)
  to a sibling directory and runs `make libgcc.a CC="... -mcpu=54418"`. Under one minute, no new download, run by
  `pk_toolchains m68k` as its last step. Result: `out/tools/m68k-elf/lib/libgcc-cf54418.a` (402 KB, ELF flags `0x26`: ISA_C +
  EMAC), never committed. The unwind members are dropped (no exceptions in modules; they hold float-register forms the
  checker refuses).
- It is complete: 64-bit divide/modulo/multiply/shift (signed and unsigned), `ffs/clz/ctz/popcount`, and the soft-float
  helpers, all from one source and one ABI (gcc's, which is what C modules use).
- The `compiler_builtins` route needs the Rust-for-ColdFire toolchain (`pk_toolchains codegen-gcc`, about 75 minutes) for a
  C-only module, and its object has only what Rust's `core` pulled in. The runtime does export `__muldi3`, `__udivdi3` and
  `__umoddi3` as imports (docs/design/digikit-dkm.md) and a C module could use those three instead of its own copies. That
  saves about 4 KB per module but covers neither signed division nor shifts, and a static (patched-in) C module cannot
  import. Keep it as an option for the size of many small modules.

### How a module gets the helpers **[D]**

`pk_link.resolve_helpers` (called by `pk_build` and `pk_link build`): when the merged object has undefined symbols that the
ColdFire libgcc defines, it runs `ld -r` against `libgcc-cf54418.a` (the archive supplies only what is needed, and what
that needs), makes every symbol the archive added **local** (two modules that both carry `__udivdi3` do not collide, and the
runtime's export namespace stays clean) and drops `.eh_frame`. An object with no helper call is byte for byte what it was:
`pk_build build-all --check` is unchanged for every committed module. A module that needs a helper while the archive is
not built fails with the command to run.

### The ISA checker **[D]**

`tools/coldfire/isacheck.py` decodes every text section with our decoder (`cfdis`, from `tools/coldfire/cfisa/coldfire.json`) and
refuses a 68020+ form, an FPU form, a privileged form other than `cpushl`, and an illegal word.

    uv run python -m tools.coldfire.isacheck FILE...      # .dko.json (arch cf), ELF .o, .a
    uv run python -m tools.coldfire.isacheck --committed  # every modules/**/build/*.dko.json

It already existed as `pk_link.check_isa` for the linked image of a module being built; that function now calls the
checker, so there is one decoder path. New: the file-level entry (committed objects, ELF, archives) and the test
(`tests/patchkit/test_patchkit_cmodules.py`, in the patchkit suite that `mise run verify` routes for `modules/` and
`tools/patchkit/pk_*.py` changes).

Result over the committed objects: **no committed ColdFire object contains a 68020-only instruction** (30 objects scanned with
c-demo, 16 with ColdFire code; the largest are the runtime, 4522 instructions, and the menu, 2223). No latent bug today, because
the only C modules that exist call no helper and the Rust ones carry `compiler_builtins`. Controls: the stock `libgcc.a` is
refused (`_mulsi3.o`, `_divsi3.o`, `_double.o` ...); the `-mcpu=54418` archive passes; a hand-written `divs.l` 64-bit form is
refused.

### Gate: `test/c-demo` **[D]**

`modules/test/c-demo` (C, `src/c/c_demo.c`): `dkm_init` computes nine results from inputs the compiler cannot fold: `/`, `%`
and `*` on `unsigned long long`, `/`, `%` and `*` on `long long` (negative operands), and `<<`, logical `>>`, arithmetic `>>`
by a variable count. Each is a libgcc helper call (`__udivdi3`, `__umoddi3`, `__divdi3`, `__moddi3`, `__muldi3`, `__ashldi3`,
`__lshrdi3`, `__ashrdi3`). The built object: 1831 instructions (4360 bytes of helper text, 398 bytes of module), no undefined
symbol, only `cf.32` and `cf.pc32` relocations, exported symbols `dkm_init` and its three data symbols.

    uv run python -m tools.patchkit.pk_cdemo proof --image dt2-1.17     # about 12 s
    uv run python -m tools.patchkit.pk_cdemo proof --image dn2-1.12
    uv run python -m pytest tests/patchkit/test_patchkit_cmodules.py

`proof` patches `core/runtime`, packs the module (`patchkit pack test/c-demo`), loads it from the card store through the
mailbox, reads the result block through the address `dkm_init` logged, and compares all nine 64-bit values with Python's.
Both images: load OK, nine results equal, write-watch 0 events, flush check 0 hits. A V4e that met a 68020 form in a helper
would take an illegal-instruction exception, so correct results are the run-time evidence that the helpers are ColdFire code
(the emulator's decoder is the same one that decodes the firmware). The module is not `dynamic = true`: that flag is for
modules that register machines and needs a `[[machine]]` section for the device (`patchkit pack` refuses it otherwise);
`test/dyn-demo` is a run-time module without it too.

Open **[O]**: a *patched-in* C module with helpers goes through the same `pk_build` path (the helpers are local symbols of the
object), but no committed static module has one; `test/c-demo` is run-time only. The soft-float helpers are linked the same
way and pass the checker, but no module uses float yet.

## 2. The MmcFs bitmaps at sector 0x800 are write-protect bits **[D]**

The plan asked what the firmware reads them for. Read from the decompile of DT2 1.16 (`FUN_4012ebd2` loads them,
`FUN_4013009c` writes them back; the table is 400 bytes at sector 0x800, in RAM at `MmcFs + 0x1de7d`: 16 bytes of
project bits, 256 of sound bits at `+0x1de8d`, 128 of kit bits at `+0x1df8d`; byte `n >> 3`, bit `n & 7`; DN2 1.11 has the
same load at `FUN_4012d2b4`/`FUN_4012e6e4` with the same 0x800/400 read).

**A set bit means the slot is write-protected (the padlock of the user manual), not occupied.**

- Reader, projects: `FUN_4012ecd8(slot)` returns the bit (1 when the table did not load, 0 for the "no slot" value 0x80, and
  0 when the slot's cache record is not valid). Its callers include `FUN_4004395e` and `FUN_40047fe6`, which print
  `N/A: Write-protected` instead of saving; `FUN_401302ee` (erase a slot) refuses a protected slot; `FUN_400a089e` draws the
  lock symbol in the project list. Sounds and kits: `MmcFs::vfunc_2` (`FUN_4012e6ba`) and `MmcFs::vfunc_33`
  (`FUN_40132aea`), the same test.
- Writers: `FUN_40130234` (project: clear, or set when the cache record is valid), `FUN_40130484` (toggle for a list of
  projects, from the menu action `FUN_400a085c`), `FUN_4013011e` and `FUN_401301d2` (sound: toggle, set only when the
  slot is not empty), `FUN_40132c6c` and `FUN_40132d22` (kit, same), the range setters `FUN_401303a8` and `FUN_40132b8a`
  (used by the factory install `FUN_40131282`, which marks the installed factory presets and kits protected), and
  `FUN_40130f9a` (format: clears all three).
- Strings: `Toggle sound write protection` (`400b7b02`), `Toggle kit write protection` (`4008de5a`), `WRITE PROTECTED`.
  The manual (Project, Preset and Kit menus) says the same: a protected slot "cannot be overwritten, renamed, tagged or
  deleted"; a padlock is drawn.
- A normal save writes the slot and leaves the table alone: that is why the bits did not move on a panel save (finding 24).
  A project saved into an unprotected slot loads regardless of the bit.

**[C]** Findings 14 and 24 and the plan called the table "occupancy" and left the polarity open. It is not occupancy.

### The builder was wrong **[D]**

`packages/emulator/plusdrive/src/tree.rs` set the bit of every slot it imported from `.mmcfs/`. By the above that padlocks
the imported slot: after the exporter's round trip every saved project, sound and kit would come back write-protected, and
saving over a project slot would answer `N/A: Write-protected`. Fixed: the builder writes the table all zero (no slot
protected; the firmware reads a zero table as valid). The unit test (`mmcfs_slots_land_in_their_sectors_and_leave_the_write_protect_bits_clear`)
now asserts a zero table for imported slots. The reader's "bit set but slot empty" issue text, the doc comments, `docs/SAMPLES.md`
and the device TOMLs say write-protect now. The emulator run that would show the padlock was not made (static answer was
enough, per AGENTS.md); the evidence is the `N/A: Write-protected` caller reading the bit.

Open **[O]**: protection a player sets in the emulator (TOGGLE) is lost on the next boot, because the exporter does not
export the table and the builder starts it at zero. The route: the exporter writes the set bits as a small sidecar
(`.mmcfs/write-protected.txt`, one `pool/NNNN` per line) and the builder sets exactly those bits. About an hour; worth it
only when someone uses write protection in the emulator.
