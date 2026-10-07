# Handover: the main OS boots and draws; the job pipeline is what stalls

Cold-start document, written for someone with no memory of the sessions that
produced it. `docs/NEXT.md` is the project overview, `docs/FINDINGS.md` is the
older evidence, this file is the current state and what to do next.

**One-line status:** with `build(weakptr=True, slc=True)` the Digitakt II main
OS boots to its **idle main screen** — pattern `A01`, project `WAXSAFETY`,
tempo `102.0`, encoder labels, no dialog — and both job workers settle into a
proper RTOS wait. On that path `channels=(3, 1)` no longer faults. What is not
yet established is whether the boot truly completed: the coprocessor does zero
transfers there, against 27,624 on the old path. See section 1.

**The previous edition of this file said "the panel still shows the last intro
frame". That was wrong, and it was wrong for the whole of two sessions.** It
measured `Bitmap::setPixel`, an HLE intercept the main OS does not draw
through. The real framebuffer was sitting in RAM, fully rendered, unobserved.
Read section 0 warning 6 before you measure anything about the screen.

**Everything in the tree is uncommitted.** `emu/dtim.py`, `emu/dsp.py`,
`emu/panel.py`, `emu/uiprobe.py`, `docs/refs/` and `patches/` are new;
`emu/harness.py`, `emu/longrun.py`, `emu/pit.py` and `emu/gui.py` are
modified.

---

## 0. Read this before believing any measurement

Six standing warnings. Every one has cost a session.

**1. `INSTR_PER_SEC` is wrong by about fifty times, and no sweep can settle
it.** It converts emulated seconds into instructions, and was calibrated at
4.68M from the intro — 312k instructions per frame at 15 fps. But the intro is
*idle* for most of each frame waiting on a frame semaphore, so that measures
how much work the intro does, not how fast the CPU runs. It is a floor.

The manual gives a real anchor: `fsys` is the core clock and the peripheral bus
is `fsys/2` (chapter 22), the part is rated 250 MHz core / 125 MHz bus, and we
have the bus at **132 MHz** independently — three PIT rates landing on round
numbers, and DTIM3 landing on 30.05 Hz. So the core runs at about **264 MHz**
and a ColdFire V4m retires roughly 0.8–1.0 instructions per cycle. The
defensible rate is of order **200–260 million**.

It shows up concretely. The panel-link enqueue at `0x4000220c` masks every
interrupt (`move.w #$2700,sr` at `0x4000225a`) and copies up to 4096 bytes at
about seven instructions each — roughly 28,700 instructions with interrupts
off. On hardware that is 217 µs against PIT2's 16.7 ms period, or 1.3%. At
4.68M it is 28,700 against 78,002 — **37% of a tick**. After a 150M warmup the
CPU sits at IPL 7 at *every* sampled boundary and two thirds of ticks are
missed.

**Do not try to settle it with a sweep.** Holding instructions constant
compares different amounts of emulated time; holding emulated time constant
compares different program phases, because the point of the change is that more
work happens per tick. There is no invariant to hold. The prediction "raising
the rate collapses the miss fraction" was tested over matched emulated time at
4.68M / 12M / 30M / 80M / 132M and came out 10.9 / 10.1 / 9.7 / 8.4 / 47.9
percent — **falsified**. The rate has to come from the part, not from a fit.

**2. Instrumentation changes the result, and this is the dominant effect.**
Adding probe hooks moves task counts and fire counts. The harness is
deterministic — identical runs agree byte for byte — so these are real changes,
not noise. Trust only what does not move when you change the probes, and always
run the same-config control in the same script. `emu.uiprobe sweep` runs its
control row twice for exactly this reason.

**3. `spin`'s `cap` argument is for pacing, never for measurement.**
Subdividing `emu_start` changes the emulated result once interrupts are being
injected. Section 10 now explains why, and the explanation does not make it
safe.

**4. Reading SR from a hook still lies, even with the patch in `patches/`.**
`uc.reg_read(UC_M68K_REG_SR)` returns real condition codes only *after*
`emu_start` returns. Inside a `UC_HOOK_CODE` TCG has not written the lazy flags
back and you get the last value written. Section 10.

**6. `setPixel` is not the screen.** `emu.frame` and
`build(bitmap=True, on_pixel=...)` observe `Bitmap::setPixel`. The *intro*
draws through it, so it is the right instrument there and the frame canary
below depends on it. The **main OS does not** — it composes straight into a
framebuffer. So `setPixel` reads 8, or 59, or 66 while a complete user
interface sits in RAM. The real panel is the pair of 1024-byte buffers behind
`0x4029f650` / `0x4029f654`; `emu.panel.read` returns one and installs no hook.
Anything phrased as "the panel is blank" that was not measured with
`emu.panel` is unfounded. And the panel's page order is **inverted**; getting
it wrong yields a screen that still looks readable in places (section 1).

**5. Two conclusions in the git log have been overturned.** `937541f`
("Identify the post-intro blocker: a C++ throw nothing can unwind") is wrong —
see section 9. And older notes calling `0x4012606a` the display task and PIT3
the display frame timer are wrong — they are the *loading screen*; see
section 1. A third is now overturned too: `7325810` and this file's own
previous status line both said the panel was still showing the intro. See
warning 6.

---

## 1. Where things stand

`python -m emu.uiprobe sweep`, from `snapshots/postintro.snap` over 60M
instructions, `dsp=True` throughout, identical hook set on every row, only the
DMA timer channels changing:

| channels | instrs | tasks | setPixel | mainloop | msgs | DTIM3 | flush | **panel** | stop |
|---|---|---|---|---|---|---|---|---|---|
| `()` | 60,061,748 | 6 | 8 | 1 | 0 | 0 | 11 | 1895 | limit |
| `()` | 60,061,748 | 6 | 8 | 1 | 0 | 0 | 11 | 1895 | limit |
| `(3,)` | 60,060,760 | 6 | 59 | **153** | **153** | 246 | 62 | **2373** | limit |
| `(1,)` | 60,061,748 | 6 | 8 | 1 | 0 | 0 | 11 | 1895 | limit |
| `(3, 1)` | 55,615,618 | 6 | 88 | 202 | 202 | 212 | 91 | 2050 | **vector 257 @ 0x4010fd52** |

`panel` is the real framebuffer's lit-pixel count, and it is the column that
matters. Note it reads **1,895 even on the control**, where `setPixel` reads 8:
the firmware has drawn a screenful with no DMA timer delivered at all. That one
column is the whole correction of warning 6.

The two `()` rows are the control and agree exactly.

### The display framing in older notes is wrong

`0x4012606a` is **not** "the display task" and PIT3 is **not** "the display
frame timer". That task is the **loading/progress screen**: it waits for a
non-zero item count at `0x44e2d5cc`, then draws a six-phase spinner and a
progress bar at `(0x44e2d5d0 * 0x27) / 0x44e2d5cc`. Its init `0x40125faa`,
which starts PIT3, is called only from the "Factory reset" and "Migrate
presets" branches of the main task. Chasing `0x44e2d5cc` was chasing a progress
bar.

The real panel path is `0x40126332` — a 128×64 double-buffer diff that flushes
through `0x40126264`, driven by the PIT2 timer-wheel callback `0x4012651e`. It
was already running before any of this session's work.

### What the main task actually does

`0x40032f5a` is the main application task (priority 6, tcb `0x4094eee8`). It
runs init, enqueues five jobs, and blocks in `queue_receive` at its message-loop
head `0x40033492` on queue `0x4094ef3c`, dispatching on a byte through a jump
table at `0x400334b4`.

**The only producer for that queue at boot is `0x400c30e4` — the DMA timer 3
ISR.** Deliver it and the loop runs 153 times; every message is type 5 and every
one dispatches to `0x4012a874` in the display module. The UI was never dead, it
was waiting on a queue nothing fed.

### The screen: it renders

`emu/panel.py` reads the framebuffer the firmware actually draws into. Two
1024-byte buffers, pointers at `0x4029f650` (just rendered) and `0x4029f654`
(on the panel); `0x40126332` diffs them page by page, flushes changed runs
through `0x40126264`, and swaps the pointers at `0x401263b0`..`0x401263c6`.
Layout, taken from the diff's own indexing and confirmed against what the
flush sends (`0x10|page`, then column, then eight bytes):

    byte index = page + 8 * column          page 0..7, column 0..127
    bit n      = row 8 * (7 - page) + n     least significant bit first

An ordinary SSD1306-style page layout **except that the page order is
inverted** — page 0 is the bottom eight rows. That is a remapped COM scan
direction, a routine panel-wiring option; columns are not reversed with it.

**Get the page order wrong and it still looks like it worked.** With the pages
the natural way round the screen shows crisp readable text in several bands,
which is exactly convincing enough to stop there. What gives it away is that
circles render as hourglasses and the value rows overlap, because each
eight-row band is mirrored in place. Three things settle the correct mapping:
knobs come out round, boxes come out rectangular, and the error dialog reads
`MMC NOT IN SLC MODE` — a string that is verbatim in the firmware image at file
offset `0x2245bd`. Checking a rendered string against the image is the cheapest
possible proof that a display mapping is right; do that before believing one.

What is on the panel, from `postintro.snap` under `(3,)`, is the **sample-source
parameter page**: `Loading...` in an inverted status bar at the top with a
spinner, `SMP 1` on the left rail, three knob widgets with pointers, `AMP` on
the right, `LEV STRT LEN LOOP LEV` labelling the encoders along the bottom —
and a modal dialog across the middle reading **`MMC NOT IN SLC MODE`**.
2,373 of 8,192 pixels lit.

Corroboration from the firmware's own mouth. `0x40000e82` is a printf-style
logger, `f(stream, fmt, ...)`, with the format string at `A7+8` on entry —
a function entry, so the read is reliable (trap 4). Over 60M instructions it is
called 529 times with only six distinct formats:

    198  '%d'          132  '%s%d.%02d'
     66  'FWD'          66  'OFF'          66  '0.00'        1  '%s: %.16s'

`'%s%d.%02d'` arrives with `(0x402292b0, 0x78, 0)` — **0x78 = 120**, the
default project tempo — and with `(0x40210de4, 3, 0)`. `FWD` is playback
direction, `OFF` a parameter value. That is a UI formatting real values, once
per frame.

### Where it stops now

**The weak pointer is real, and `build(weakptr=True)` disposes of it.** Two
branches in `weak_ptr::lock`, both contradicting the memory they were taken on:

    40188b40  6714 beq.b $40188b56  ->  4e71 nop     ; a0 is NOT null
    40188b50  660a bne.b $40188b5c  ->  600a bra.b   ; d0 is NOT zero

Note `0x40188b52` is a `clr.l $4(a0)` that *undoes* the increment on the way
into the hang, and `0x40188b5c` is the success path storing the object pointer
from `0x44f1df40` — the disassembly in the previous edition of this section was
a summary and got both wrong. Patch them as two-byte memory writes before
`emu_start` is ever called and no hook is added and no translation block can be
stale, which is what makes the A/B legitimate under warning 2:

| over 300M instructions | control | `weakptr=True` |
|---|---|---|
| terminal-loop iterations | **252,977,319** | **0** |
| main-loop messages | 153, frozen | 168 |
| `weak_ptr::lock` reaching its success path | 305 | 337 |

The control is not slow, it is dead: 84% of the run in a `bra.b` to itself.

**But `weakptr=True` deadlocks too, just quietly.** At 300M every count is
identical to the same run at 60M — 168 messages, 69 flushes, 15,250 display
tick callbacks — while PIT2 fired 3,804 times and DTIM3 1,785. Interrupts keep
arriving and no task consumes them. `emu.tasks.parked_pc` over the live machine
at the stall:

    0x4315eaf0  prio 3  job worker   parked 0x400cf532   coprocessor writer's rts
    0x4315aa9c  prio 2  job worker   parked 0x400f1eb6   never started
    0x44e2d5d4  prio 6  progress     parked 0x40001416
    0x44e32044  prio 5               parked 0x40001488
    0x44e31fd0  prio 4               parked 0x40001488
    0x44e31f5c  prio 4               parked 0x40001488

and the ready list holds exactly one node, the main task.

### The actual blocker: DTIM1 is the job pipeline's heartbeat

The coprocessor freezes at **27 pages** — `bursts=27,624`, `words=220,992`,
byte for byte the same at 60M and at 300M — after **26** calls to
`0x40128c7c`, the microsecond sleep. `0x400cfd40` sleeps 100 µs after every
4 KB page, and that sleep runs on **DMA timer 1**. The default `channels=(3,)`
does not deliver DTIM1, so the 27th sleep never expires and the worker never
wakes. Nothing else in the system has anything to do, so everything blocks
behind it.

This is why `Loading...` is on the screen and stays there.

### The MMC dialog is advisory, and the flag behind it is never written

The dialog reads **`MMC NOT IN SLC MODE`** (`0x402249bd`, file offset
`0x2245bd`). **It is not a blocker.** Traced and verified twice:

    40033358  jsr $401204a4        ; -> 1 = SLC ok, 0 = unset, -1 = error
    40033360  cmp.l d0,d1          ; d1 = 1
    40033362  beq.b $4003336e      ; ok -> check the capacity instead
    40033366  pea.l $402249bd      ; "MMC NOT IN SLC MODE"
    40033382  jsr $4010902c        ; both paths join here: show the dialog
    40033390  ...                  ; and both fall through into the rest of init

The `bne` at `0x40033378` skips the dialog outright, and nothing returns early:
execution continues into the message loop whichever way it goes. The factory
self-test at `0x400cd50c` logs `MMC NOT RECONFIGURED` (`0x4022b4a6`) off the
same primitive and is the same advisory shape.

`0x401204a4` is nothing but a tri-state read of the byte at **`0x4fe49198`**.
And **nothing in any firmware section ever writes it** — a byte search over all
of `sections/` finds exactly two references and both are reads (`0x401204a6`
and `0x40120714`); Ghidra's `Callers.java` agrees. So an earlier boot stage sets
it on hardware, our snapshots start past that, and it reads **0**.

`build(slc=True)` writes 1 there. That is a model of the missing boot stage
rather than an override of a firmware decision — though since the writer has
not been found, it stays opt-in.

### What `slc=True` does, and it is a lot

From `postintro.snap` over 60M with `weakptr=True`:

| | `slc=False` | `slc=True` |
|---|---|---|
| panel | `Loading...` + error dialog, 2371 lit | **the real main screen**, 1895 lit |
| coprocessor bursts | 27,624 | 0 |
| job pump entries | 1 | 2 — both workers start |
| prio-3 worker parked at | `0x400cf532`, wedged in the coprocessor | `0x4000165c` |
| `channels=(3, 1)` | **faults at 55.6M, vector 257** | **runs to the limit** |

`0x4000165c` is the instruction after the `trap #0` at `0x4000165a`, which is
the RTOS block-and-yield — so both workers are *properly asleep in a wait*,
not spinning. And the DTIM1 fault, which section 6 called the whole game, does
not happen at all on this path.

The panel shows a complete, idle Digitakt II main screen: `A01`, the project
name **`WAXSAFETY`**, tempo `102.0`, `SMP 1`, three knob widgets labelled
`TUNE` / `PLAY` / `SAMP`, and `LEV STRT LEN LOOP LEV` along the encoders. No
`Loading...`, no dialog, no terminal loop.

**The open question, and do not skip it.** With `slc=True` the coprocessor does
**zero** transfers where it previously did 27,624. The five enqueued jobs are
byte-identical under both flags, so nothing is skipped at enqueue time, but
something downstream decides there is no DSP work to do. Either the boot
genuinely completed and there was nothing to mirror, or this path quietly
bypasses sample loading. Settle that before calling the boot finished — the
screen looking right is not proof, as warning 6 should have taught by now.

### The jobs

Five are enqueued at boot, in this order: `KitActive::updateSingleMirror`,
`saveProjectToMmc(tempProject)`, `Migrate presets`, `Update MMC Caches`,
`Load all samples`. `0x400f1ce0` only *enqueues* — it locks, pushes a
`std::function` pair into a ring at `+0x24` and posts the semaphore at `+0x14`.
The pump `0x400f1b80` is shared by two worker tasks, `0x400f1eb6` at priority 2
and `0x400f1fce` at priority 3.

**Priority runs high-number-wins here**: 7 > 6 > 3. The intro task at 7 starving
everything is the precedent. So the priority-6 main task preempts the priority-3
worker, and once the main task enters the terminal loop it spins there forever
and starves the worker completely — 53 million passes measured.

Job one alone is 1024 coprocessor page writes and it **completes** (exactly
1,024, verified). The pump then reaches its second job and no further.

---

## 2. Verify the tree first

    uv run python -m emu.hle          # header-mutation ok=True
    uv run python -m emu.softfloat    # 0 mismatches
    uv run python emu/oracle.py
    uv run python emu/screen.py selftest
    uv run python -m emu.frame snapshots/boot400M.snap 20000000 out/frame.png
        # expect: setPixel 616823, 75 frames, 344 lit   <- the canary

    uv run python -m emu.uiprobe sweep
        # expect the table in section 1; the two () rows must agree exactly

    uv run python -m emu.panel snapshots/postintro.snap 60000000 3
        # expect lit=2373 and a readable UI: LEV STRT LEN LOOP, "Loading..." 

The frame canary is the one that matters. If those three numbers move,
something changed the emulated CPU, and nothing else here should be trusted
until you know what. (It moves to 616613 if `srtrap` is enabled — see section
10 — which is expected and explained.)

---

## 3. The working harness

`snapshots/postintro.snap` (54.7M instructions past `boot400M.snap`) is the one
for OS work: intro over, iterating takes seconds.

    from emu.longrun import build, spin, INTRO_DONE
    from emu.pit import Pits, intro_running
    from emu.dtim import Dtims, Timers

    m, ev, st, pc, inq, at = build('snapshots/postintro.snap', unblock=True,
                                   softfloat=True, bitmap=True, dsp=True)
    t = Timers(Pits(m, hold=intro_running(m)),
               Dtims(m, channels=(3,), hold=intro_running(m)))
    if t.held:
        at(INTRO_DONE, lambda uc, a, s, d: t.release())
    pc, done, why = spin(m, pc, 60_000_000, pits=t)   # do not pass cap

`Timers` composes any number of timer sources into the single object
`spin(pits=...)` takes; `step` is the minimum across them and `service` runs
them in construction order.

**From `boot400M.snap` you must hold the timers through the intro** — see
section 4. `postintro.snap` predates `raise_vector` using format nibble 4, so
frames already on task stacks in it carry format 0. Harmless while `rte` is
implemented in `on_intr` and ignores the field.

---

## 4. Invariants you must not break

**Deliver nothing while the intro is still running.** From `boot400M.snap` over
90M instructions, channels `()` and `(0,)` both reach six tasks, while `(2,)`,
`(3,)`, `(0, 2)` and `(3, 2, 0)` all reach **zero**, and `(3,)` never lets the
intro exit. `intro_running(m)` tells the snapshots apart from the registers.

**A hook must not call `emu_stop` under `spin`.** It makes the instruction
accounting a lie and every timer deadline drifts off the instructions actually
executed.

**In `pits` mode `instrs` is a floor, not a ceiling.** `spin` finishes the
deadline step it is on, so every `emu_start` boundary is a timer deadline
however the caller splits its work. 20M instructions taken as 1, 4, 20 or 100
successive calls give byte-identical state.

**Timer deadlines are absolute and resume from `.now`.** A caller making
repeated short `spin` calls keeps one continuous clock.

**A resumed run needs the same hook set, not just the same Machine.** And note
the timer state is *not* in a snapshot — see section 11.

---

## 5. Interrupt delivery

### The PITs — `emu/pit.py`

Four programmable interval timers at `0xFC080000` + n·0x4000, vectors 205–208,
INTC2 sources 13–16. Rates come from the firmware's own registers:
`prescaler = 1 << (((PCSR >> 8) & 0xF) + 1)`, `period = prescale * (PMR + 1)`
bus cycles at 132 MHz. That reproduces PIT0 at 20.0000 ms, PIT2 at 16.6672 ms
and PIT3 at 66.6686 ms — three independent timers on round numbers is what
validates the bus clock.

PIT0 is the RTOS time slice and vector 205's handler *is* the context switcher.
PIT2 drives the software timer wheel. PIT3 is the progress screen's frame timer.

**The channel order `(3, 2, 0)` is correct hardware behaviour, not a stopgap.**
The manual (chapter 17, §17.3.1, table 19) says that at equal ICR level the INTC
takes the **highest source number** first. PIT3 is source 16 and PIT2 is source
15, so PIT3-before-PIT2 is what the hardware does. What is still unmodelled is
a latched PIF — a refused tick held and re-offered when IPL drops.

### The DMA timers — `emu/dtim.py`

Four at `0xFC070000` + n·0x4000, INTC0 sources 32–35, so vectors 96–99.
Register layout verified against manual chapter 39: DTMR 16-bit at +0x00,
DTXMR at +0x02, DTER at +0x03 (write-1-to-clear, bit 1 = REF), DTRR 32-bit at
+0x04, DTCN at +0x0C. In DTMR: `PS[15:8]` divides by PS+1, `ORRI` bit 4, `FRR`
bit 3, `CLK[2:1]` = 00 stop / 01 bus / 10 bus÷16 / 11 pin, `RST` bit 0 enables.

**DTIM3, vector 99, ISR `0x400c30e4` — the user interface's clock.** DTMR3 =
`0x001d` (÷1, bus÷16, ORRI, restart), DTRR3 = `0x43238` = 274,488, which at
132 MHz is 33.27 ms — **30.05 Hz**. The ISR acks DTER3 and calls `queue_send`
on `0x4094ef3c`. Landing on 30.05 Hz out of the independently-established bus
clock is the same corroboration the PIT rates gave.

**DTIM1, vector 97, ISR `0x40128c4c` — the microsecond sleep.** `0x40128c7c(n)`
writes DTRR1 = n and DTMR1 = `0x841b` (÷133 on the bus clock: a 992.5 kHz tick,
so 1 µs resolution), then pends `0x44e4d69c` at `0x40128d08`. The ISR acks,
writes DTMR1 = 0 to stop the one-shot, and posts. Twelve call sites. It paces
the coprocessor transfers.

DTIM0 and DTIM2 have never been seen enabled.

**Every snapshot carries a stale DTIM1 arm, and delivering it is fatal.** From
`boot60M` onward, DTMR1 reads `0x841b` with `0x44e4d69c` at zero: armed, nobody
waiting. That cannot happen on hardware. It exists because `0x40128c7c` arms and
pends, `unblock` satisfies the pend, and the ISR that would have written
DTMR1 = 0 was never delivered — so the arm rolled forward into every snapshot
ever written. `boot40M` is the only clean one. With channels `(1,)` and no
repair, DTIM1 fires once at instruction 4,721 and the run halts at 9.6M with
zero tasks. `Dtims.clear_stale` (default on) does what the missing ISR would
have. **Turn it off for a snapshot written by a run that was itself delivering
DMA timer interrupts** — there the arm is real.

**Default channels are `(3,)`.** `(3, 1)` reaches further into the UI and then
faults; revisit once section 10 is settled.

**A caution about the model.** A timer armed part-way through a step is not
noticed until the step ends, so a sleep runs long by up to one step — up to a
PIT2 period against DTIM1's ~480 instructions. Closing that needs sub-step
resolution, and `emu_stop` under `spin` is forbidden.

---

## 6. What to do next, in order

1. **Checkpoint the timer state into snapshots.** About twenty lines: stash
   each source's `next` and `now` in `save(extra=...)` and restore them in the
   constructor. Without it a resumed run re-arms from phase zero and diverges —
   measured, deterministically, halting at 1,326,039 instructions with an
   unhandled vector where the continuous run carries on. With it, a checkpoint
   cut just before the stall takes iteration from **23 s to 0.7 s, about 30×**.
   Everything below gets thirty times cheaper, so do this first. Note
   `emu/checkpoint.py` has no awareness of timer objects at all today — neither
   `resume()` nor `extend()` even passes `pits=` to `spin`.

2. **Find out whether the `slc=True` boot is real.** It reaches the idle main
   screen and stops faulting, but the coprocessor does zero transfers. Work out
   what consumes the five jobs on that path and why `KitActive::updateSingleMirror`
   pushes nothing. If the answer is "there was genuinely nothing to do", the
   boot is done; if it is a bypass, the old path is the honest one and item 3
   is still the whole game.

3. **Make `(3, 1)` + `weakptr=True` survive with `slc=False`.** On the old path
   it dies in the firmware's HALT at `0x4010fd52`. Items 4 and 5 serve this.

4. **Unit-test the exception path in isolation.** Section 10 is the cause and
   the reason correcting the condition codes makes the boot worse. Build a toy
   program where an interrupt lands between a flag-setting instruction and its
   branch, and check the frame push and `rte` against documented m68k semantics
   — *not* against this firmware, where warning 2 means every change reshuffles
   the run.

   Start from a defect that is not in doubt: **`Machine.raise_vector`'s default
   path never writes a new SR on exception entry** (`emu/harness.py:254-261`).
   It pushes the frame and jumps to the handler, so IPL is never raised to the
   interrupt's level, S is never set and T is never cleared. The `srtrap`
   trampoline does all three. That is a second, independent defect alongside
   the stale condition-code byte, and the combination — the Unicorn SR patch
   *and* a corrected entry SR in the default path — has never been tested.
   Patch-alone and srtrap-alone each hit a wall; that may be why.

5. **Read the RTOS context switcher at `0x40000410` in Ghidra.** It is PIT0's
   handler and it saves and restores exception frames itself, so it is exactly
   where our synthesised frame meets the firmware's expectations. If the layout
   or semantics differ, correct flags would break it — which is what was
   observed. This is the one static question worth asking, and it is
   independent of everything above, so it costs no wall-clock.

6. **Diff stock against patched by first divergence, not by totals.** The
   harness is deterministic byte for byte, so the question "why does correcting
   the flags take six tasks to two" has an exact answer: the first instruction
   at which the two runs differ. That is immune to warning 2 in a way that
   comparing totals never is.

7. **Find what writes `0x4fe49198`.** No firmware section does. Until it is
   found, `slc=True` is a guess that happens to work, not a model that is
   known right.

8. **Fix `INSTR_PER_SEC`.** Warning 1. Needs the run to be affordable first;
   see section 11 for why that is not the Python layer.

9. **Decode the front-panel protocol.** Section 8, RX direction. It is how the
   UI would get driven by something other than a timer — and now that the UI
   demonstrably renders, this is what turns the project into something you can
   press buttons on.

## 6b. Storage: the eSDHC, and why there is none

**README.md states storage as unsupported**, which is the honest public
position until block data is served. Keep it that way until item 2 below lands.


The goal is an emulated MMC backed by a host folder. This is the reconnaissance
for it. **Nothing here is implemented yet.**

### It is eSDHC, not NAND

The controller is the **eSDHC** at **`0xFC0CC000`**, 16 KB, PBC0 slot 51
(RM Table 1-3). The image references that window **128 times**; the NAND flash
controller at `0xFC0FC000` is referenced **zero** times. Worth stating because
the manual's only mention of "SLC" is in the *NFC* chapter, and this part has no
SD/MMC boot mode — both facts point the wrong way. The card is an **eMMC**
(CMD3 carries a host-assigned RCA, which is illegal for SD), and "SLC mode" is
the eMMC *enhanced user-data area*, an `EXT_CSD` concept invisible to the
controller.

### The interrupt is vector 222, and the census had it mislabelled

Read out of the firmware's own configuration, not the manual:

    vec 221 -> 0x400019bc      INTC2 src 29  ICR 0x06
    vec 222 -> 0x400019e6      INTC2 src 30  ICR 0x05   <- SDHC
    vec 223 -> 0x40001252      INTC2 src 31  ICR 0x00   <- unconfigured, shared stub

`0x40001252` is the shared default handler (vectors 219, 220, 224 all point at
it too). Source 31 has ICR 0, so it is not armed at all. **Section 7 lists
vector 222 as "SIM"; it is the SD/MMC controller.** A `pdftotext` extraction of
RM Table 17-17 scrambles the source-number column and reads 31 — do not trust
it over the ICRs.

### The driver

| | |
|---|---|
| command primitive | `0x4011fe10` — writes CMDARG, then XFERTYP from a 64-entry flag table at `0x40209754`, then **blocks** |
| how it waits | `sem_pend 0x4000141a` on **`0x44e26f38`**. Not a poll. |
| read / write blocks | `0x401208fe` (CMD18), `0x40120ae4` (CMD25), ~35 callers each |
| erase | `0x40120816` (CMD35/36/38) |
| card init | `0x4011fed6`, called from `0x400cf218` inside OS root init `0x400cef6c` |
| pre-init gate | `0x4011fe60` — GPIO handshake, 10 retries |
| data path | the SoC's **eDMA**, `SADDR = DATPORT`. `XFERTYP[DMAEN]` is never set; `DSADDR` and `ADMASAR` are never referenced. The eSDHC's own DMA engine is unused. |

**`0x44e26f38` is never posted anywhere in the image.** Nor are its three
siblings `0x44e26f30`, `0x44e26f28`, `0x44e26f20`. On hardware vector 222 drives
that. So *any* real storage access hangs until we deliver it — which is very
likely the whole reason the job pipeline stalls while boot itself does not.

### Why none of it ran: the loopback gate — SOLVED

From `boot40M.snap` over 400M instructions and `boot400M.snap` over 140M, the
driver had never executed at all: zero eSDHC register accesses, `0x4011fed6`
never entered, `0x4011fe10` never called. The cause:

    400cf20a  jsr $4011fe60      ; the gate
    400cf214  tst.l d0
    400cf216  bne.b $400cf258    ; NON-ZERO -> skip card init entirely
    400cf218  jsr $4011fed6      ; card init, only when d0 == 0

The gate returns zero only by falling out of its ten-iteration loop, and it
completes an iteration only when **`PPDSDR_C` bit 3 follows `PPDSDR_D` bit 4** —
drive high, sense high; drive low, sense low. A board continuity check. Note
the success case is the loop *running out*, which reads backwards.

`0xEC094000` is the GPIO port module (PODR +0x00, PDDR +0x0C, PPDSDR +0x18,
PCLRR +0x24, one byte per port A..K); `PPDSDR` writes-1-to-set and `PCLRR`
writes-0-to-clear. Unmodelled GPIO reads zero, so bit 3 is low on the first
read and the gate early-returns 1 on its first pass.

**`emu/gpio.py` models it; `build(sdgate=True)` turns it on.** Measured from
`boot40M.snap` over 120M:

| | `sdgate=False` | `sdgate=True` |
|---|---|---|
| card init | skipped at `0x400cf216` | **called** |
| eSDHC registers | none, ever | `SYSCTL` 36,988,467, `WML` 1, `PRSSTAT` 1 |
| gate | early-out on pass 1 | 10 sets, 10 clears, 20 senses, loop exhausts |

**It is off by default because on its own it makes the boot worse**, which is
the model being honest rather than broken: past the gate the driver spins
forever at `0x4012001e` waiting for `SYSCTL` bit 27 `INITA` to self-clear. Our
registers are plain RAM and hand back whatever was written, so it never does.
That spin is the next thing to fix, and it is item 1 in the list below.

### The controller model — `emu/esdhc.py`, and card init now completes

`build(sdgate=True, esdhc=True)`. The gate alone reaches the driver; the model
is what gets through it. From `boot40M.snap`, the full eMMC identification
sequence runs and `0x4011fed6` returns instead of spinning:

    CMD0  CMD1(OCR)  CMD2(CID)  CMD3(RCA 2)  CMD10  CMD9(CSD)  CMD7(select)
    CMD6(HS_TIMING)  CMD6(BUS_WIDTH)  CMD19/CMD14(bus test)  CMD16(blocklen)
    CMD6  CMD8(SEND_EXT_CSD)

`PRSSTAT` reads go from **36,988,314 to 7**. Requirements 0-4 of the old list
are met; what satisfied them:

* **`SYSCTL` self-clearing bits (INITA, RSTA/RSTC/RSTD)** are cleared on
  **read**, not on write. This is the one that matters: *a Unicorn write hook
  runs before the store lands*, so clearing a bit from inside one is undone by
  the store that follows, and reading the register back there gives the
  previous value. Handled wrongly it looks exactly like the model not being
  installed at all. The same applies to XFERTYP -- take the command word from
  the hook's `value` argument.
* **`PRSSTAT`** starts at the documented `0xFF8800F8` **plus `CINS`**, since a
  card is inserted. `BWEN`/`BREN` are raised when a data command is issued,
  per direction, or the driver spins at `0x40120236` and `0x401202b2`.
* **The bus test is a real handshake, not the magic number it looks like.**
  `0x40120242` writes `0x5A` to DATPORT under CMD19 (BUSTEST_W); CMD14
  (BUSTEST_R) must return a word whose low byte XORed with `0xA5` is zero.
  `~0x5A == 0xA5` -- the card returns the inverse of what the host sent, so
  the model inverts the captured pattern instead of hardcoding `0xA5`.
* **CMD1** returns an OCR with bit 31 set immediately; the retry loop at
  `0x40120074` has no timeout, so this cannot be deferred.

### What is still missing

1. ~~**EXT_CSD content.**~~ **Done — and it settles where the SLC flag came
   from.** `0x40120302` programs eDMA channel 59's DADDR to **`0x4fe49100`**
   and `0x4012037e` arms it before issuing CMD8, so EXT_CSD lands there. The
   SLC flag `0x4fe49198` is therefore **EXT_CSD byte 0x98 (152)** — the same
   buffer. `build(slc=True)` had been poking a byte of EXT_CSD all along,
   which is why nothing appeared to write it: nothing does, the card supplies
   it.

   `emu/esdhc.py` now serves a 512-byte EXT_CSD through the channel's TCD
   (SOFF 0, NBYTES 16, CITER 32) and **the flag reads 1 without `slc=True`**.
   The guess is now a consequence.

   One caveat kept deliberately: byte 152 sits inside `GP_SIZE_MULT` in the
   JEDEC map, which is not a plausible home for an SLC flag. The **offset** is
   established; the JEDEC field identity is not. `ext_csd()` is deliberately
   sparse — only observed-read fields are set — so any further dependency
   shows up as a spin rather than hiding behind a plausible value.
2. **Bulk block data.** `read_blocks` (`0x401208fe`, CMD18) is called and
   returns zeros. Data does not come through DATPORT reads by the CPU — it
   moves through the SoC's eDMA programmed with `SADDR = DATPORT`, TCD
   registers at `0xFC0457xx`, channel config at `0xFC044018`. Backing this is
   what makes storage real, and it is the bridge to the folder below.
3. **Card identity.** The CID/CSD the model returns are placeholders. The
   descriptor built by `0x401205b4` must match one of the seven entries at
   `0x4029e01c` (0x20 bytes each, tag bytes `0x11`/`0x15`/`0x70`, name strings
   at `0x40243e4f`+ compared by memcmp) or `0x40120450` reports "unknown card".
4. **Vector 222 is still not delivered**, and `0x44e26f38` is still never
   posted by the firmware. Today `unblock` satisfies the wait and the model
   writes the ISR's status word `0x44e26f1c` itself. That is HLE standing in
   for an interrupt; it works, and it is not the hardware's behaviour. Revisit
   when the exception model of section 10 is sound.

### Then, and only then, the folder

Serving `CMD18`/`CMD25` from a flat host image is ordinary work once 1-6 hold.
Turning a `samples/` directory into an image the firmware understands needs
Elektron's on-disk format, which nobody has reverse-engineered. Get the block
device working against a sparse image first and learn the format from what the
firmware reads out of it.

## 7. The interrupt census

For every vector: is a real handler installed, is its INTC source enabled and
unmasked, and do we ever deliver it? Read `VBR + vec*4` for all 256 vectors,
and for each handler below `0x48000000` read the source's ICR at
`<intc>+0x40+source` and its mask bit in IMRH/IMRL.

From `postintro.snap` over 60M instructions, **nineteen** vectors were armed,
unmasked, had a real handler and were never delivered. Two are now identified
and delivered:

| vec | INTC | src | level | handler | what it is |
|---|---|---|---|---|---|
| 65 | 0 | 1 | 5 | `0x400cf424` | EPORT pin 1 — acks `0xFC090006` bit 1 |
| 66 | 0 | 2 | 7 | `0x40001a10` | EPORT0 edge flag |
| 68 | 0 | 4 | 5 | `0x400cf450` | EPORT pin 4 |
| **97** | 0 | 33 | 2 | `0x40128c4c` | **DTIM1 — the µs sleep. Delivered.** |
| **99** | 0 | 35 | 2 | `0x400c30e4` | **DTIM3 — the 30 Hz UI tick. Delivered.** |
| 108 | 0 | 44 | 5 | `0x400d8158` | |
| 121 | 0 | 57 | 2 | `0x400da136` | |
| 134 | 1 | 6 | 4 | `0x40006ece` | |
| 156 | 1 | 28 | 4 | `0x400021a0` | eDMA ch36 |
| 157 | 1 | 29 | 4 | `0x40002102` | eDMA ch37 |
| 168 | 1 | 40 | 7 | `0x40001252` | eDMA ch48; same handler as PIT1's vector 206 |
| 170 | 1 | 42 | 6 | `0x400d56a0` | eDMA ch50 |
| 180 | 1 | 52 | 3 | `0x40001d00` | UART8 |
| 181 | 1 | 53 | 4 | `0x40001f86` | UART9 |
| 182 | 1 | 54 | 2 | `0x400ced8c` | DSPI1 |
| 191 | 1 | 63 | 5 | `0x4002d652` | |
| 209 | 2 | 17 | 6 | `0x40005e10` | USB OTG |
| 221 | 2 | 29 | 6 | `0x400019bc` | SIM |
| **222** | 2 | 30 | 5 | `0x400019e6` | **SD/MMC (eSDHC) — see section 6b** |

An armed source is not proof the device would assert. But it is the complete
list of places the firmware waits for something we never send, and naming them
is a lookup: **the manual is in `docs/refs/` and the table is chapter 17,
tables 17-15/16/17**. Two have been done that way and both mattered — vector 99
is what the whole user interface was waiting for.

---

## 8. The models that exist, and why

**The DMA timers — `emu/dtim.py`.** Section 5.

**The panel reader — `emu/panel.py`.** Not a model, an instrument: it reads
the framebuffer the firmware draws into, described in section 1. `read` is a
plain memory read and installs no hook, so it is free and cannot perturb a run;
`Capture` hooks the diff entry to collect untorn frame *sequences* and
therefore does change the run it watches. `emu.uiprobe.measure` uses `read`, so
the sweep gained a `panel` column without any of its other numbers moving.

**The eSDHC and its eMMC — `emu/esdhc.py`.** Section 6b. Needs `sdgate=True`
to be reached at all.

**The SD gate — `emu/gpio.py`.** The board loopback on GPIO ports C and D that
`0x4011fe60` checks before storage init. Section 6b. Off by default: it is
needed to reach the eSDHC driver and insufficient to get through it.

**The coprocessor port — `emu/dsp.py`.** `0x8C000000` is a FlexBus device
addressed in 4 KB pages. `0x400cf4a8(word)` writes four bytes most-significant
first as pairs of 16-bit writes to `0x8C000002` — `(byte << 8) | 0x80` then
`(byte << 8)` — so bit 7 of the low byte is a write clock and the data rides in
the high byte; bit 0 of a read is the ready line. `0x400cfd40(cmd, buf, swap)`
locks the scheduler, sends a four-byte header, sends 4096 bytes, then sleeps
100 µs. `cmd` of `0xFFFFFFFF` marks a command block, anything else is a page
index. `0x40146148(addr, src, len)` writes `len` bytes as 4 KB pages;
`0x401465fe(i)` initialises sample slot `i`, bounded at `0x3ff` in
`0x40146f54` — so 1024 slots.

Nothing backed that address, so the ready bit read zero forever and the
priority-3 worker wedged on its very first transfer: `0x400cf4a8` entered
exactly once and never returning, which is what the "9.7M of 60M instructions
spinning" in older notes actually was. **The model provides the ready line and
nothing else** — the pacing is the firmware's own 100 µs sleep, not ours. If
the firmware ever needs a *response*, the model has to grow.

**eDMA channel 35 — `emu/edma.py`.** Boot stalled at exactly frame 88 of 175
because `0x4000220c` spins waiting for room in a 4096-byte TX ring and nothing
advanced the channel. The model runs the major loop on a write of 35 to
EDMA_SERQ, advances SADDR with the ring modulo, reloads CITER from BITER, and
raises vector 155 so the firmware's own ISR does the bookkeeping.

**UART8 TX is not a console.** It carries a binary front-panel protocol.
Observed uploading an RGB palette as `B5 <index> <r> <g> <b>` records with
6-bit channels ramping `02,04,08,0c,10,18,1e,…,3f` across red, then green, then
blue. So `ev['uart_out']` is panel traffic, and the RX direction (eDMA channel
34, vector 154) is where button and encoder events would arrive.

**`unblock` is narrowed by caller, not by semaphore.** `RECHECK_PENDS` in
`emu/longrun.py` holds four sites where the caller re-checks a condition and
loops, so force-satisfying turns a sleep into an infinite spin:
`QUEUE_RECV 0x40001946`, `INTRO_PARK 0x400d4068`, `DISPLAY_WAIT 0x401260c2`,
and `PUMP_WAIT 0x400f1bb0`. The last is new and is described in section 9.

**Exception frames use format nibble 4.** The ColdFire PRM is explicit that an
RTE whose frame format is not 4–7 raises a format error.

**The GUI drives the timers.** `emu/gui.py` runs `spin(..., pits=Timers(Pits,
Dtims))` with `dsp=True`, reports PIT0/2/3, DTIM3, main-loop passes and job
count on the status line, flags the terminal loop, and prints a progress line
to stdout every ~20M instructions.

---

## 9. Ruled out, and things that were wrong

* **Not a Unicorn CPU bug.** `move.l An,<ea>` sets the condition codes
  correctly; so do `move.l`, `move.w`, `tst.l`. Verified by letting the CPU
  branch on the result rather than reading SR, which is the only reliable test
  (warning 4). An earlier claim of a CPU bug here was made on the strength of
  an SR read and was wrong.
* **Not user/supervisor confusion, not INTC gating, not the frame format on
  their own, not the synthetic vector-32 idle tick, not `unblock` breaking a
  mutex, not the depack shortcut, not a corrupt timer-callback list.**
* **`reg_read_batch` is not a perf win** — measured 0.74×.
* **The panel was never blank.** Every claim that boot "does not draw" or
  "still shows the intro" came from `setPixel`, which the main OS does not use.
  See warning 6 and section 1. The user interface had been rendering, with
  correct text and live values, through all of it.
* **Not inexact instruction counts.** `emu_start(count=N)` executes exactly N.
* **Porting to QEMU is not the answer.** Evaluated properly and written up in
  `docs/refs/qemu-coldfire-feasibility.md`. QEMU's ColdFire target carries the
  *same* instruction bugs already patched here — `cfv4e` never sets the ISA
  flag `FF1` decodes on (its own V2 model does), and `cf_movec_to` calls
  `cpu_abort()` on any control register outside a list of about five, which is
  this repo's own MOVEC SIGABRT. No MCF5441x prior art exists anywhere, and
  there is no eDMA, DMA-timer or triple-INTC model for any Freescale part.
  Estimates: QEMU machine type 20–40+ days, Musashi 25–45+, patching Unicorn
  5–12.

### The C++ exception: cause found, old explanation wrong

`basic_string::_S_construct null not valid`, thrown at `0x401d3fba`, aborting
in libgcc's unwinder. The previous entry blamed **preempting the heap
allocator**, because every injection preceding a failure landed in
`0x40111044`–`0x4011137e` and the faulting PC `0x40111458` is in the same
allocator. **That was reading the crime scene as the crime.**

`0x40111432` is `free`, and it bounds-checks its argument with three `illegal`
opcodes — `0x4011144a` below the heap base, **`0x40111458` above
`base + 0x1FFFFF0`**, `0x40111462` if not 16-byte aligned. The trap is
deliberate. Caught in the act the argument is `0xfffffff4`, which is `0 - 12`,
and 12 bytes is exactly `std::basic_string::_Rep` in the old GCC COW ABI. So it
is `_M_dispose` on a string with a null data pointer — the *same* null string
the `_S_construct` message reports, seen from the other end.

The null came from `unblock` force-satisfying the job pump's "is there work"
pend at `0x400f1bb0`: the worker took a ring slot nobody had written and
destroyed a record that was never constructed. `PUMP_WAIT` is now in
`RECHECK_PENDS`. Measured with `dsp=True`, that takes a run which faulted at
70.2M with `unhandled vector 257` to a clean 100M+, and the pump from one job
to two.

Two tools worth knowing: `0x401d0e24` is a throw helper taking a `const char*`
— hook it and read argument 1 as a C string. And the firmware's own fault
handler prints a full report through `0x40000e82`: hook that and you get
`EXCEPTION DS0071` / `V04 M0 P40111458` (vector, mode, PC) plus a register
dump, which is faster than any backtrace.

---

## 10. The live blocker: our exception model

**The cause of the old "`emu_start` subdivision changes the result" mystery is
found.** Unicorn's m68k `reg_read(UC_M68K_REG_SR)` does not return computed
condition codes. `qemu/target/m68k/unicorn.c` sets `env->cc_op = CC_OP_FLAGS`
*before* calling `cpu_m68k_get_sr`, and `cpu_m68k_get_ccr` COMPUTES the flags
with `COMPUTE_CCR(env->cc_op, ...)` — so forcing `cc_op` first tells it the
flags are already materialised and it returns the raw lazy operands.

Proof, independent of the firmware: an **identity**
`reg_write(SR, reg_read(SR))` around a `tst.l d0` / `beq` flips the branch. So
every delivered interrupt corrupted the interrupted code's flags three times —
`raise_vector` pushed a stale SR, `Pits.service` wrote a stale-derived one back,
and `rte` restored the stale frame value. "A `bgt` at `0x40111070` taking
opposite branches from identical PC, A7, D0 and A0" is exactly that. So is the
`beq` at `0x40188b40` in section 1.

That patch was later replaced by
`patches/unicorn-2.1.4-m68k-hook-ccr-sync.patch`, which also commits lazy
condition codes at code-hook and count stops, and
`patches/unicorn-2.1.4-m68k-emac-mac-load.patch` was added for EMAC MAC and
MSAC with load. `tools/install-patched-unicorn.sh` builds and installs both;
see `docs/UNICORN.md`.

**Two things stop this being a win, and both are measured.**

1. The fixed read is correct only *outside* emulation. Inside a `UC_HOOK_CODE`
   TCG has not written the lazy flags back and you still read the stale value —
   `0x2004` where the true SR is `0x2000`. `Pits.service` runs between
   `emu_start` calls and gets the fix; `raise_vector` called from `on_intr` for
   a `trap #N` does not.

2. **With the patch in, the boot gets worse**: from `postintro.snap` over 60M
   instructions, six spawned tasks become two, setPixel 8 becomes 0, and DTIM3
   never even gets armed. That holds with the SR write-back left in and with it
   removed, and for every channel tuple. The canary is untouched, because traps
   still read stale and the intro is bit-identical.

So the 153-pass result in section 1 was obtained *with* corrupted flags, and
correcting them exposes something else in the exception model that the
corruption was masking. **That is the next thing to chase** — via section 6
items 2 and 3, not by re-running the firmware and looking at the totals.

A guest-side entry trampoline that has the CPU write its own SR into the frame
is written and works mechanically — `build(srtrap=True)`,
`Machine.install_srtrap`, nine per-level trampolines at `0x10000000` stride
`0x40`, each ending at a hooked `nop` that redirects PC. It is off by default
because it hits the same wall. Note it is *not* self-modifying code, on
purpose: patching a shared trampoline meant QEMU's cached translation block
re-ran the first handler ever patched in (DTIM3 delivered 283 times, its ISR
running zero times), and `ctl_remove_cache` "fixes" that and takes the run to
zero tasks.

---

## 11. Speed — and the old numbers in this section were wrong

**Profiled, 8M instructions, full config, sorted by `tottime`:**

| | tottime |
|---|---|
| `emu_start` — Unicorn actually executing | **3.935 s** |
| `mem_read` | 0.016 s |
| `_do_reg_read` | 0.012 s |
| `satisfy` (our `unblock` hook) | 0.010 s |
| everything else | noise |

**Our Python layer is about 3% of runtime, not the 54% this section used to
claim.** Confirmed independently: turning off *both* the softfloat and bitmap
HLEs moves the rate from 2.05 to 2.11M instr/s. The old to-do list here —
preallocated `mem_read` buffers, collapsing the 239 scoped ISA hooks — is aimed
at the wrong 3%. `isa='global'` is 2.7× *slower* than `isa='scoped'` (0.77 vs
2.11M instr/s), so scoped is already right.

The run is CPU-bound inside Unicorn's TCG at ~2M instr/s against 250.8M
hook-free. The gap is hook-induced translation-block fragmentation plus
`count=`, inside the C engine, and there is no cheap Python-side win.

**So the iteration lever is checkpointing, not optimisation** — section 6 item
1. A checkpoint cut just before the hang takes an experiment from 23 s to
0.7 s. It needs the timer state saved or the resume diverges.

---

## 12. Traps that have already cost time

1. **Only trust an A/B where both sides do the same work.** Has bitten six
   times, including a fictitious 118× speedup from stopping at a function entry.
2. **Two runs that executed a different number of instructions are not an
   A/B** — and neither are two runs covering different amounts of *emulated
   time*, which is how the `INSTR_PER_SEC` sweep was got wrong twice.
3. **Always run the same-config control first, in the same script.**
4. **Register values read inside a mid-function `UC_HOOK_CODE` lag.** Hook
   function entries, which are basic-block boundaries. Reading *memory* in a
   hook is reliable; reading SR is not (warning 4). Hook addresses themselves
   are always reliable — tracing which addresses were visited is how the wrong
   branch in section 1 was pinned down, after register reads had given
   contradictory answers.
5. **Do not cache anything read from firmware structures** without proving it
   immutable.
6. Only call `uc.emu_stop()` from a hook that has advanced PC past the current
   instruction — and never under `spin`.
7. A hook-only stop condition needs a wall-clock timeout as a floor.
8. **A resumed run needs the same hook set, not just the same Machine.**
9. Snapshots are gitignored. `postintro.snap` for OS work, `boot400M.snap` for
   intro/draw work, `console450M.snap` for the console task.
10. `softfloat` and `bitmap` default **off** in `longrun.build`; `edma`
    defaults **on**; `dsp`, `real_sleep` and `srtrap` default **off**.
11. If you write a snapshot, `extra['tasks']` keys must be hex *strings*;
    `restore_into` does `int(k, 16)`.
12. **`real_sleep=True` has never been observed to change any measured
    outcome.** The sleep semaphore already reads 1 when the pend is reached, so
    `unblock` never satisfies it and skipping the site changes nothing. Why it
    reads 1 is unresolved; do not assume the option works.
13. `uc.mem_write` from Python does **not** fire `UC_HOOK_MEM_WRITE`, so a
    write hook cannot see the harness's own writes. That cost time chasing a
    semaphore that appeared to change with no writer.
14. **Check what an instrument actually observes before believing a zero.**
    Two sessions were spent on "the panel is blank" because `setPixel` reported
    near zero, and `setPixel` was the wrong probe. A null result from an
    instrument you have not verified against a known-good signal is not
    evidence. Warning 6.
15. **A branch that contradicts the memory it branched on is our bug, not the
    firmware's.** Both `weak_ptr::lock` branches were like this, and forcing
    them was worth thirteen million instructions of information in half an
    hour. When registers and memory disagree, trace visited *addresses* —
    those are always reliable (trap 4).

---

## 13. Key addresses

| | |
|---|---|
| **main application task** | `0x40032f5a` — init, enqueues 5 jobs, then its loop |
| **main loop head / its queue** | `0x40033492` / `0x4094ef3c`; jump table `0x400334b4` |
| **main loop msg 5 -> UI redraw** | `0x4012a874`, returns to `0x40033598` |
| **DMA timer bases / vectors** | `0xFC070000`+n·0x4000 / 96+n (INTC0 src 32+n) |
| **DTIM3 = 30.05 Hz UI tick / ISR** | `0xFC07C000` / `0x400c30e4` -> `0x4094ef3c` |
| **DTIM1 = µs sleep / ISR / pend site** | `0xFC074000` / `0x40128c4c` / `0x40128d0e` |
| **sleep entry / semaphore / init flag** | `0x40128c7c` / `0x44e4d69c` / `0x44e4d6b0` |
| **job enqueue / pump / pump pend site** | `0x400f1ce0` / `0x400f1b80` / `0x400f1bb0` |
| **job worker tasks** | `0x400f1eb6` (prio 2), `0x400f1fce` (prio 3) |
| **coprocessor port / latch / bursts** | `0x8C000002` / `0x8C00000A` / `0x400cf4a8`, `0x400cf534` |
| **coprocessor push / pages / command** | `0x400cfd40` / `0x40146148` / `0x401465a4` |
| **sample-slot init (0..0x3ff)** | `0x401465fe`, bounded in `0x40146f54` |
| **free() and its bounds trap** | `0x40111432`, traps at `0x40111458` (`illegal`) |
| **terminal loop (`bra.b` to itself)** | `0x4012d2fa` |
| **weak-pointer lock / use count** | `0x40188b00` / `0x40188aec`; ptr `0x44f1df44`, obj `0x44f1df40` |
| **panel-link TX enqueue / copy loops** | `0x4000220c` / `0x4000226a`, `0x400022a4` (IPL 7) |
| **TX ring state** | base `0x4094cd7c`=`0x4FE1B000`, head `..88`, widx `..8c` |
| **RTOS context switcher** | `0x40000410` — acks PIT0, unmasks its source every switch |
| **PIT2 ISR -> timer-wheel sem** | `0x40002a18` -> posts `0x47d9ade0` |
| **timer-wheel task / callback list** | `0x40002a46` / `0x4094cdb8` (7 nodes) |
| **display tick callback** | `0x4012651e` (mask 1) |
| **panel flush (double-buffer diff)** | `0x40126332` -> `0x40126264` |
| **panel buffers: rendered / displayed** | `0x4029f650` / `0x4029f654`, 1024 B, swapped at `0x401263b0` |
| **panel layout** | `index = page + 8*column`, bit n = row `8*(7-page)+n`, LSB first |
| **on-screen error strings** | `MMC NOT IN SLC MODE` `0x402249bd`, `MMC NOT RECONFIGURED` `0x4022b4a6` |
| **SLC flag / its reader / the test** | `0x4fe49198` / `0x401204a4` / `0x40033360`, dialog `0x4010902c` |
| **RTOS block-and-yield** | `trap #0` at `0x4000165a`, resumes `0x4000165c` |
| **printf-style logger** | `0x40000e82` — `f(stream, fmt, ...)`, fmt at `A7+8` |
| **weak_ptr::lock wrong branches** | `0x40188b40` (`beq`), `0x40188b50` (`bne`) |
| **coprocessor word write / burst** | `0x400cf4a8` / `0x400cf534`, rts at `0x400cf532` |
| **progress-screen init / task** | `0x40125faa` / `0x4012606a`, sem `0x44e2d148` |
| **progress geometry setter** | `0x40125f6a` — writes `0x44e2d5d0` (pos), `0x44e2d5cc` (count) |
| **PIT3 start / stop (progress)** | `0x40126004` / `0x4012604a` |
| **PIT0..3 PCSR / vectors** | `0xFC080000`+n·0x4000 / 205+n |
| **INTC ICR / IMR** | `<intc>+0x40+source` / `+0x08` IMRH, `+0x0C` IMRL |
| **INTC bases** | INTC0 `0xFC048000`, INTC1 `0xFC04C000`, INTC2 `0xFC050000` |
| **heap allocator** | malloc `0x4011122c`; body `0x40111044`-`0x4011137e` |
| **C++ throw helper (takes a message)** | `0x401d0e24` |
| **fault handler / HALT / log fmt** | `0x4010fcae` / `0x4010fd50` / `0x40000e82` |
| **queue_receive / queue_send** | `0x40001928` / `0x400018e4`, also `0x40001896` |
| **sem_pend A / B, sem_post** | `0x4000141a` / `0x400013a6`, `0x4000148c` |
| **current TCB / ready cursor** | `0x47d9adb4` / `0x4094c914` |
| **exception trampoline (opt-in)** | `0x10000000`, 9 slots, stride `0x40` |

---

## 14. Tools

    uv sync
    uv run python -m emu.run [syx] [--weakptr]     # syx -> running, one command
    uv run python -m emu.gui [snap] [--weakptr] [--slc] [--scale N]  # live panel
    uv run python -m emu.uiprobe sweep             # the section 1 table
    uv run python -m emu.uiprobe run <snap> <n> 3  # one run + panel + backtrace
    uv run python -m emu.panel <snap> <n> 3,1 out/panel.png   # the REAL screen
    uv run python -m emu.frame <snap> <instrs>     # one frame, ASCII + PNG
    uv run python -m emu.tasks <snap>              # parked PC per task
    uv run python -m emu.probe <snap> <instrs>     # blocking sites, hot PCs
    uv run python -m dt2.coldfire sections/section_3_MAIN_OS.bin 0x40000400 <a> <b>

    tools/ghidra.sh import                          # one-time, ~3 min
    tools/ghidra.sh run Callers.java out.txt 0x...  # real xrefs, incl PC-relative
    tools/ghidra.sh run Decompile.java out.txt 0x...

**Use Ghidra for anything structural.** `dt2/coldfire.py` is a linear sweep
with no cross-references, and a byte search for the address constant misses
every PC-relative call. Batch many addresses per invocation — each one costs
1–2 minutes, which is why `pyghidra` 3.1.0 (in the venv) is better for
iterating:

    GHIDRA_INSTALL_DIR=/opt/homebrew/Cellar/ghidra/12.1.3/libexec uv run python ...

Open the existing project with `pyghidra.open_project()` +
`pyghidra.program_context()` — **not** `open_program()`, which tries to
re-import. Import `ghidra.*` only *after* `pyghidra.start()`. Scripts in
`tools/ghidra/` must be Java; this Ghidra build has no PyGhidra. Note the
decompiler goes to garbage on some large functions (`0x40188b00`,
`0x40146f54`) — fall back to `dt2.coldfire` for raw disassembly there.

For grep and SQL, dump the program once with `tools/ghidradump.py` (about a
minute, to `out/ghidra/<tag>/`); for live queries use `tools/ghidraq.py`,
which chains queries in one JVM. See CLAUDE.md.

**The manuals are in `docs/refs/`** (untracked, ~11 MB of NXP PDF — decide
before committing). `MCF5441XRM.pdf` is MCF54418RM Rev 5, 1360 pages;
`CFPRM.pdf` is the ColdFire Programmer's Reference; `MCF5441X-notes.md` holds
extracted answers with chapter citations; `qemu-coldfire-feasibility.md` is the
port evaluation. `pdftotext MCF5441XRM.pdf rm.txt` makes the manual greppable.

**Backtracing by hand:** scan the stack for words preceded by a real `jsr`/`bsr`
opcode — `4EB9` at −6, `4EBA`/`4EB8`/`6100` at −4, `4E8x` at −2. Naive stack
scanning gives nonsense. `emu.uiprobe.backtrace` implements it.
