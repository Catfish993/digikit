# Handover — 2026-09-14, the eighth machine

This thread continues `HANDOVER-2026-09-13-sharc-and-repack.md`. Goal A (the
repack chain) is closed to the limit of what can be checked without hardware.
This thread is Goal B. Everything established is in `docs/FINDINGS.md` — "The
ColdFire machine dispatch" and its subsection "The display names are a
separate table". Read those, not this file, for facts.

## The goal, and how far "done" is

**A new machine on the Digitakt II that appears in the machine list, can be
selected, and makes a sound that the seven stock machines do not.**

That is a ladder, and it is worth being explicit about which rung the work is
on, because the five patches below only reach rung 2:

1. **Listed** — an eighth row appears in MACHINE SEL with its own name.
2. **Selectable** — selecting it does not crash, and the track plays. At this
   rung the machine is a *clone*: the cave descriptor copies entry 6's nine
   parameter IDs verbatim, so it behaves exactly like MANUAL SLICE and only
   the name differs. This is deliberate — a known-good payload while the
   plumbing is proven.
3. **Distinct behaviour** — its own parameter set. The nine literal IDs in the
   descriptor (`0xca`-`0xfe` across the stock entries) are not decoded yet; we
   do not know what they select or what a valid new combination would be. MIDI
   (entry 5) carries all nine zeroed, which proves they are not mandatory but
   says nothing about what they mean.
4. **A new algorithm** — DSP code the SHARC does not already run. This needs
   the SHARC assembler and semantic model that do not exist, and is the
   deepest item in the whole project. A machine that reuses an existing DSP
   mode with different parameters avoids it entirely.

So: the five patches below get to rung 2. Rung 3 is the next real research
question and is not started. Rung 4 is out of scope for a first machine.

## Where it stands

An eighth machine is dispatched and named in the emulator, proven by direct
in-guest call for all nine dispatch inputs. It is not yet visible on screen.
All five patches are implemented, and **with all five applied the image boots**
under the GUI's emulator configuration: 6 tasks, `DTIM3` firing, no exception,
to 400M instructions. The type-7 boot failure was a fifth bound, the machine
list's sort comparator; see `docs/FINDINGS.md`, "The thrower is
`std::map<int,int>::at`". Nobody has looked at the list on screen yet.

| # | patch | what it does | state |
|---|---|---|---|
| 1 | source list | relocate table D to the cave with an eighth entry; repoint the `pea` immediates at `0x40052000`/`0x4005200a` | implemented |
| 2 | dispatch | trampoline `FUN_400caf48` for `type == 7`; descriptor and COW string reps in the cave | implemented, boots clean on its own |
| 3 | grouping | `FUN_4005d7b8`: `7206b2806604` -> `7207b2806504` at `0x4005d7ca`, making the exact-6 test a `<= 7` range test so type 7 gets group `1` instead of `0` | implemented; not a boot blocker, rendering effect unobserved |
| 4 | display name | copy the 7-row `0x401fbc50` name table to the cave with an eighth row; repoint `FUN_400dcc50`'s bound (`0x400dcc51`) and `lea` immediate (`0x400dcc62`) | implemented, table verified correct |
| 5 | rank | cave shim on the sort comparator's one-time map insert (`jsr` at `0x40051872`), supplying eight `(type, position)` pairs instead of seven | implemented; `list+rank` and all five boot |

Two more are known but not blocking: the filter-list bounds (`pea` at
`0x40052296`/`0x400522a0`), only needed if the new machine should be a filter
target; and `FUN_4005e022`'s `param_1[0x73] + 1 < 8` auto-scroll gate, which
is cosmetic.

## The boot failure — resolved

It was `std::out_of_range` from `map::at(7)` in `FUN_400517c4`, the comparator
`FUN_40051fbc` uses to `stable_sort` the machine list. Patch 5 fixes it. The
full account, including two corrections to what this handover used to say,
is in `docs/FINDINGS.md`. Not carried here.

## How to reproduce

The machine-select screen renders, and modifier chords work, with:

    uv run python -m emu.gui --weakptr --patch-machine snapshots/boot400M.snap

Click FUNC (it latches, staying sunken), then SRC. In the GUI the list stays
open and scrolls to PLACEHOLDER; a scripted headless replay closes it, for
reasons still open (see FINDINGS.md). Every delivered feed is printed as
`[gui] input --feed ...`, replayable with `tools/guirun.py --feed`.
`clear` in the latch box releases held modifiers.

- `--patch-machine` bare applies all five parts; `=list+rank` style
  combinations apply exactly those, and an optional `:N` suffix sets the
  eighth list entry's value (e.g. `--patch-machine=list:6`). Unknown part
  names are refused rather than silently ignored.
- `--machine=NAME:SHORT[:CLONE_OF[:POSITION]]` sets the new machine's names,
  the stock machine whose descriptor fields it copies, and its list position
  (e.g. `--machine=Lofi:LOF:3:0`). The default is Placeholder/PLC, a copy of
  MANUAL SLICE, last in the list. `tools/machinepatch.py` has the same flag.
- `--panel-dwell N` sets the emulated dwell between panel state changes in
  chunks, default 16 (~50 ms). `0` restores the old coalescing and reproduces
  the flicker the dwell fixed.
- `--weakptr` is required on this snapshot or the main task traps before the
  UI comes up.

Headless, in the GUI's exact configuration (same build flags, timers, intro
handover and hook set), with code hooks and a stack scan at a chosen address:

    uv run python tools/guirun.py snapshots/boot400M.snap --weakptr \
        --patch-machine=list+rank --at 0x401d5680=cxa_throw \
        --stack-at 0x401d5680 --limit 400000000

It reproduces GUI-only failures that `tools/machinepatch.py` and
`tools/uidrive.py` do not, and runs 400M instructions in under a minute. Two
agent-run gotchas: the shell is zsh, so a `$FLAGS` variable arrives as one
argument (and a bare `--patch-machine` will swallow it); and there is no
`timeout` binary, so bound runs with `--limit`.

## Retractions from this thread

1. **The cave is not 58,188 free bytes.** The tail of MAIN OS is `.bss`. Only
   `0x40303e5c`-`0x40307f60` (16,644 bytes) and `0x402f9c14`-`0x402fa000`
   (1,004 bytes) are clear. See `docs/PATCHING.md`, corrected.
2. **A clean runtime dump does not prove memory is free** — it only proves
   nothing wrote there on the path observed. Use `tools/refscan.py` and
   `tools/ghidraq.py ... range` as well; all three have different blind spots
   and none is a proof.
3. **The descriptor's name pointers are not the UI's display names.** Those
   come from a separate static table. Corrected in FINDINGS.md.
4. **"The UI task is never scheduled" was wrong** — it was true only of
   headless resume runs. Under `emu/gui.py` the UI runs normally.
5. **Group `0` does not break boot, and the two "range-checkers" named for
   the terminal loop were message strings, not functions.** The failure was
   the sort comparator's `map::at`. Both corrected in `docs/FINDINGS.md`.

## Suggested order

1. **Look at it.** Run the GUI with bare `--patch-machine`, open MACHINE SEL
   (FUNC then SRC), and see whether an eighth row draws, what it is called, and
   where its separator falls — that is patch 3's and patch 4's first real
   test. `emu/gui.py` needs Tk, which the agent environment lacks, so this one
   is a human run.
2. **Select it** — rung 2. Scroll to the eighth row and select it; the track
   should then behave as MANUAL SLICE. If that throws, `tools/guirun.py
   --at 0x401d5680 --stack-at 0x401d5680` names the thrower in one run.
3. **Sweep for remaining bounds.** Five patches, and four of them were found
   only when something broke. `FUN_400caf48` and `FUN_400dcc50` are accessors
   of the same shape — a `moveq #6` bound followed by a base address — and
   between them have eleven callers; the comparator was a different shape
   entirely, a static `std::map` built from immediates. Grep the image for
   both shapes, and for other functions referencing `"map::at"`, before the
   next crash finds them.
4. Decode the descriptor's nine parameter IDs — rung 3, and the first thing
   that makes the machine actually different rather than a renamed clone.
5. Convert the whole thing to a real image patch through `tools/patchimg.py`
   and `dt2/build.py`, and put it through `tools/roundtrip.py`.
6. Only then hardware — and the standing advice from the previous handover
   still holds: prove recovery mode while the device is healthy, then flash
   an unmodified rebuild before anything patched.

## Tools added this thread

    tools/memdump.py      resume, spin to post-intro, dump a guest range
    tools/memfind.py      search mapped guest memory for a pattern
    tools/refscan.py      exhaustive static scan for absolute references into a range
    tools/machinepatch.py install the eighth machine live; --parts bisects it
    tools/uidrive.py      script panel input and watch for UI-side signals
    tools/guirun.py       the GUI's emulator configuration, headless: --at hooks, --stack-at scans, --input clicks, --feed replay, --png-at captures
    tools/ghidraq.py      gained a `range LO HI` subcommand

`emu/gui.py` gained `--patch-machine`, `--panel-dwell`, latching modifiers,
and unconditional memory-fault reporting (`[gui] FAULT page=... pc=...`),
which was previously recorded and never surfaced.
