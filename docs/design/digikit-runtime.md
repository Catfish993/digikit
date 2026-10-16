# The digikit runtime, v0 (`core/runtime`)

Status: built, 2026-10-16; emulator only (DT2 1.16, 1.17 and DN2 1.11, 1.12; nothing ran on a device). Plan:
`docs/plans/digikit-runtime.md` (Phase 3). Results: `docs/findings/29-runtime-v0.md`. File format:
`docs/design/digikit-dkm.md`. Store: `docs/design/digikit-store.md`.

## What it is

A patchkit module (`modules/core/runtime`, requires `core/expansion`) written in Rust for the ColdFire (rustc_codegen_gcc,
finding 26) with two assembly files. Everything is in `cf.expansion` and part of the module's own object, so patchkit places it
like any static object:

| Part | Size (bytes) | What |
| --- | --- | --- |
| code and read-only data | 10 054 code (+ about 1 KiB data) | loader, store reader, allocator, SHA-256, API |
| `DKRT_STAGE` (bss) | 65 536 | staging buffer for the store directory and one module file |
| `DKRT_ARENA` (bss) | 524 288 | the module arena, 16-byte aligned blocks |
| `DKRT_STATE`, `DKRT_MAILBOX` (bss) | 1 256 + 108 | slots, module records, log ring; the host mailbox |
| trampoline, device id | 78 + 4 | `src/cf/tramp.s`, `src/cf/<dev>/device.s` |

Total 602 324 bytes of the 1 396 736 in `cf.expansion` on DT2 1.16 (794 412 left for other modules); 26 MB regions on DN2. The
arena is the object's bss, so patchkit reserves it with the code and no static object of any module can overlap it; the reset-path
loader zeroes it before the firmware runs. Other statics are exported by name (`DKRT_STATE`, `DKRT_MAILBOX`, `DKRT_ARENA`,
`DKRT_STAGE` in `plan.json` symbols).

## Hook

One firmware site: the call at the top of the UI task's event loop that pops the next event from the UI queue. DT2 1.16
(`FUN_400337ba`, 0x40033d00; 1.17 the same address; DN2 1.11 and 1.12 0x4002f178 in code Ghidra has not made into a function):

    caseD_2:
    40033d00  pea   (0x40966f3c).l        ; the UI queue
    40033d06  jsr   FUN_40001928.l        ; pop: pends until an item arrives, returns it in d0
    40033d0c  addq.l 0x4,SP
    40033d0e  moveq 0x29,D1
    40033d10  movea.l D0,A2
    40033d12  mvz.b (A2),D0               ; the item type, then the switch (0 key, 1.., 5 timer tick ...)

A `retarget` op changes the `jsr` target to `dkrt_loop_hook`, which saves d0/d1/a0/a1, calls `dkrt_tick` (C ABI, so d2-d7 and
a2-a6 are kept) and jumps to the stock pop with the stack as the caller left it. The site is found by a masked signature of the
six words around it (`cf_words` with `as = "code"`), the same rule on all four images. Why here: it runs in task context (the UI
task, which also calls the card filesystem and block read, finding 23 S3), once per event (key, timer tick, ...; about 0.2 passes
per million instructions in an idle emulator, **[D]** finding 29), before the task blocks, and nothing live is in d0/d1/a0/a1 there.
It is not an interrupt, so the loader may block in the card read.

`dkrt_tick` (a) polls the host mailbox for a load or unload command and (b) calls slot 0 when a module bound one, counting
the call in `entered[0]`.

## C ABI (stable names; declared in `modules/core/runtime/sdk/src/lib.rs`, crate `dkrt-sdk`)

All three are for the UI task (or another task context); not from an interrupt; not reentrant. Results are `i32`; no small-struct
results.

    int32_t dkrt_load(const char *path);            // "modules/dyn-demo.dkm": store-relative; 0 or a negative E_* code
    int32_t dkrt_unload(const char *name);          // the module's name ("test/dyn-demo")
    void    dkrt_status(DkrtStatus *out);           // magic 'DKRT', runtime version, device, arena base/size/free/largest,
                                                    //   module count, ticks, last result, loads, slot calls
    int32_t dkrt_module(uint32_t index, DkrtModule *out);   // name, version, base, size, slot mask; E_NOT_FOUND past the end

On an error nothing is left behind. Codes: `E_NOT_FOUND` -1 (no store / no such file or module), `E_BAD_STORE` -2, `E_READ` -3,
`E_SHA` -4, `E_FORMAT` -5, `E_RUNTIME` -6, `E_IMPORT` -7, `E_NOMEM` -8, `E_INIT` -9, `E_LOADED` -10, `E_SLOT` -11, `E_BUSY` -12,
`E_TOO_BIG` -13, `E_TABLE_FULL` -14. The addresses of these functions are symbols of the patch plan (`plan.json`
`symbols`: `core/runtime@VERSION:dkrt_load`, ...); a module that wants to call them (the DIGIKIT menu) names them as
`[object.externs]`/module symbols like any cross-module symbol.

`dkrt_tick` is also exported, for the hook only.

### API table (`Api`, passed to `init`, `fini` and slot functions; also `dkrt_api`)

Versioned (`version` = 1, `size` = bytes present), fields only appended. `device` (1 = DT2, 2 = DN2), `runtime`, then function
pointers (gcc m68k cdecl): `import(name) -> addr or 0`, `alloc(n)`, `free(p)` (the arena; 16-byte aligned; not zeroed),
`store_read(path, dst, cap) -> size or code` (reads a store file; through the staging buffer, so at most 64 KiB), `log(msg, value)`
(24 bytes of message and one word into a 16-entry ring in `DKRT_STATE`; no `core::fmt`), then addresses of `memcpy`, `memset`,
`memcmp`, `__muldi3`, `__udivdi3`, `__umoddi3` so a C module need not carry them. A Rust module built with the repo's pipeline
still links its own copies of the compiler helpers (`compiler_builtins`) into its object; sharing them needs the module build to
leave them undefined, which is not done in v0 **[O]**.

### Slots

`SLOTS = 8` function pointers in `DKRT_STATE`. A module binds slot n by exporting `dkm_slot_<n>`; the loader publishes it after
`init` with one aligned 32-bit store and unload clears it first. Slot 0 is called once per UI-loop pass as
`void (*)(const Api *)`. Slots 1 to 7 are bound but nothing calls them yet (later hooks: the sequencer, key handlers, ...).
`entered[n]` counts tasks inside slot n's function (maintained by the caller of the slot); unload spins on it (2 000 000
iterations) after clearing the slot and, if it never drops, restores the slot and returns `E_BUSY`. Only slot 0, from the UI
task, exists today, so the count is 0 whenever unload runs from the same task.

### Mailbox (emulator, tests)

`DKRT_MAILBOX {cmd, seq, result, arg[96]}`: write the NUL-terminated path or name into `arg`, then `cmd` (1 load, 2 unload); the
next `dkrt_tick` runs it, stores `result`, adds one to `seq` and zeroes `cmd`. The emulator control socket does this with
`mem.cf.poke` (`tools/patchkit/pk_dkrt.py` uses the `cf_script` example for the same pokes). A hardware menu calls `dkrt_load`
directly instead.

## Arena allocator

First fit over an address-ordered free list; blocks of 16-byte multiples with a 16-byte header (size, next-free or `USED`); a free
block is merged with its neighbours. 524 288 bytes minus 16 at start. `free` ignores a pointer that is not a live block.

## Safety rules kept

- The loader reads and checks everything before the first write to the arena (`E_SHA`, `E_IMPORT`, `E_FORMAT`, ... leave the
  arena byte for byte unchanged; checked by comparing the whole arena before and after).
- Code written through the data cache is pushed (`cpushl bc`, every 16-byte line of the block) before it can be fetched; the
  emulator's flush check reports 0 hits, and reports hits when the flush is disabled (finding 29).
- Writers into `cf.expansion` are the runtime's code and code running from the arena; the write watch sees nothing else.
- Slots are published after `init` and cleared before `fini`; the runtime never runs a partly loaded module.
- v0 does not unload a module that something else still points into (a registry row, a machine in a project); that comes with
  the registry (Phase 5).

## Limits (v0)

Module file at most 64 KiB; 8 modules; 8 slots; imports at most 64; no streaming; one task context; no autoload; no safe-mode key.
DN2: the flush check sees few CPUSHL on DN2 images (finding 25), so the 0-hit result there is weaker evidence.
