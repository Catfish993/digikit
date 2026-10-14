# NetBurner MCF5441x layout evidence

Cross-checks the emulator's MCF5441x MMIO facts against the NetBurner SDK's
register-layout headers -- a *different* independent source from the
`torvalds/linux` cross-check in `docs/refs/linux-m5441x-893e1178.md`, useful
in particular for register fields Linux's in-tree drivers never exercise for
this exact SoC (see that file's "open question" notes).

Source: NetBurner NNDK, vendored at `docs/refs/netburner-coldfire/` (see
`docs/refs/netburner-coldfire/README.md` for the scrape provenance and
original URL, `https://www.netburner.com/NBDocsTest/Developer/html/`). The
corpus is **incomplete and stripped** per that README -- header declarations
and macro tables only, no `.c` implementation files, no register reset
values, no timing/behavioral documentation. Every fact below is a static
declaration (a struct layout or a `#define`), not an observed behavior.

Files used:

- `arch/coldfire/cpu/MCF5441X/include/sim5441x.h` -- peripheral register
  struct layouts and their base-address comments.
- `arch/coldfire/cpu/MCF5441X/include/intcdefs.h` -- the `SETUP_*_ISR`
  macro table, i.e. NetBurner's own (controller, source) assignment for
  every interrupt.
- `arch/coldfire/cpu/MCF5441X/include/periph_clocks.h` -- clock-gate bit
  IDs into the SCM's `ppmsr0/1`/`ppmcr0/1` registers.
- `platform/NANO54415/include/esdhc.h`, `platform/SB800EX/include/esdhc.h`
  -- eSDHC driver API surface only (function prototypes), no register
  offsets; confirms API policy, not layout. Not cited for any fact below.
- `arch/coldfire/cpu/MCF5441X/include/dspi.h` -- incomplete; only the DSPI
  base addresses below are taken from it, no register fields.

Labels are the same as the Linux doc: **HW** (address/offset/bit fact),
**NEW** (a fact this corpus adds that the Linux cross-check could not
confirm), **API** (NetBurner's own driver-facing policy, not a hardware
fact).

## 1. Interrupt controller: IMRH/IMRL now confirmed (NEW)

`sim5441x.h:534-566`, `intcstruct` (three instances at `intc[3]`, base
`0xFC04_8000`, each `0x4000` bytes apart -- matching INTC0/1/2 bases already
confirmed against Linux):

    +0x00  iprh      Interrupt Pending Register High
    +0x04  iprl      Interrupt Pending Register Low
    +0x08  imrh      Interrupt Mask Register High
    +0x0C  imrl      Interrupt Mask Register Low
    +0x10  intfrch   Interrupt Force Register High
    +0x14  intfrcl   Interrupt Force Register Low
    +0x1A  iconfig   Interrupt Configuration Register
    +0x1C  simr      Set Interrupt Mask (byte, write source number)
    +0x1D  cimr      Clear Interrupt Mask (byte, write source number)
    +0x1E  clmask    Current Level Mask
    +0x1F  slmask    Saved Level Mask
    +0x40..0x7F  icrn[64]  Interrupt Control Registers 0-63

This is new relative to the Linux cross-check: `docs/refs/linux-m5441x-893e1178.md`
section 1 states Linux's own `intc-simr.c` driver for this SoC only ever
touches SIMR/CIMR (confirmed here at +0x1C/+0x1D, matching) and *never*
exercises IMRH/IMRL, leaving `emu/pit.py`'s `IMR_BASE = 0x08` an open
question resting solely on MCF5441XRM.pdf. This NetBurner header is an
independent declaration of the same struct and puts IMRH/IMRL at exactly
+0x08/+0x0C -- matching `emu/pit.py:91` (`IMR_BASE = 0x08`, and IMRL follows
at +0x0C by the 32-bit stride). Corroborates the base offset; still says
nothing about IMRH/IMRL's *behavior* (raw mask bits vs. some other
encoding) -- neither corpus exercises a read/write of it.

Also confirms `ICR_BASE = 0x40` (`icrn[64]` at +0x40) exactly, independently
of the Linux `MCFINTC0_ICR0 = 0xfc048040` fact already cited.

## 2. eDMA / TCD (HW)

`sim5441x.h`: `edmastruct edma` at `0xFC04_4000` (control registers) with
`edma_tcdstruct tcd[64]` at `0xFC04_5000` (`sim5441x.h:483-497,527,1404`).

TCD layout (`sim5441x.h:483-497`), each entry 0x20 bytes:

    +0x00  saddr      Source Address
    +0x04  attr       Transfer Attributes
    +0x06  soff       Signed Source Address Offset
    +0x08  nbytes     Signed Minor Loop Offset/Minor Byte Count
    +0x0C  slast      Last Source Address Adjustment
    +0x10  daddr      Destination Address
    +0x14  citer      Current Minor Loop Link/Major Loop Count
    +0x16  doff       Signed Destination Address Offset
    +0x18  dlast_sga  Last Destination Addr. Adjustment/Scatter Gather Addr.
    +0x1C  biter      Beginning Minor Loop Link/Major Loop Count
    +0x1E  csr        Control and Status

Matches `emu/edma.py:49-63` exactly: `EDMA_BASE = 0xFC044000`,
`TCD_BASE = 0xFC045000` with the same `n * 0x20` stride, and the same
CITER-at-+0x14/BITER-at-+0x1C ordering the module docstring already flags
as "NOT the Kinetis order". This corpus independently confirms the
ColdFire-specific field order, not just the two offsets the firmware trace
in `emu/edma.py`'s docstring (TCD35 at `0xFC045460`) already established.

## 3. DTIM base and register layout (HW; TRR+1 still open)

`sim5441x.h:1426`: `timerstruct timer[4]` at `0xFC07_0000 -> 0xFC07_FFFF`,
i.e. base `0xFC07_0000` with a `0x4000`-byte stride between the four
instances -- matching `emu/dtim.py`'s `BASES` exactly, and independently of
the Linux cross-check (which, per `docs/refs/linux-m5441x-893e1178.md`
section 3, could only confirm the *field layout* of a sibling ColdFire
part's DMA-timer IP block via `dma_timer.c`, not this SoC's actual base
address -- no in-tree Linux driver targets the MCF5441x's DTIM instances at
all). This NetBurner header is address-specific to the MCF5441x and closes
that gap.

`timerstruct` (`sim5441x.h:663-674`):

    +0x00  tmr    DMA Timer Mode Register (16-bit)
    +0x02  txmr   DMA Timer Extended Mode Register (8-bit)
    +0x03  ter    DMA Timer Event Register (8-bit)
    +0x04  trr    DMA Timer Reference Register (32-bit)
    +0x08  tcr    DMA Timer Capture Register (32-bit)
    +0x0C  tcn    DMA Timer Counter Register (32-bit)

Matches `emu/dtim.py:59-65` offsets exactly (`DTMR, DTXMR, DTER, DTRR,
DTCN = 0x00, 0x02, 0x03, 0x04, 0x0C`).

**Still not confirmed by either cross-check:** whether the reference
register's reload count is `TRR` or `TRR + 1`. This header declares the
field (`trr`) but not its semantics -- NetBurner ships no `.c` here to show
how it is used, and Linux's own DMA-timer driver (per the Linux doc) never
runs in reload mode on this part either. `emu/dtim.py:212-214`'s `(dtrr + 1)`
convention remains sourced only from MCF5441XRM.pdf table 39-2. **Open.**

## 4. SDHC interrupt source == 31, vector == 223 (NEW)

`intcdefs.h`: `#define SETUP_SDHC_ISR(f, l) SetIntc(2, (long)f, 31, l)` --
NetBurner's own ISR-registration table assigns the eSDHC controller to
**INTC2 source 31**. With INTC2's vector base 192 (confirmed against Linux
in `docs/refs/linux-m5441x-893e1178.md` section 1), that is vector
`192 + 31 = 223`. This is a fact the Linux cross-check did not state (Linux's
`sdhci-esdhc-mcf.c` driver map in that doc covers register layout, not the
INTC source number); recorded here as a NetBurner-only confirmation, not
merged into a Linux-attributed fact.

## 5. Peripheral clock-gate IDs (HW, `periph_clocks.h`)

Bit indices into the SCM's power-management set/clear registers
(`ppmsr0`/`ppmcr0` for the low-numbered gates, `ppmsr1`/`ppmcr1` for the
`_1` register pair -- see `sim5441x.h`'s `scmstruct`). Static IDs only; no
firmware in this repo has been observed gating any of these, so this is
recorded for future use, not as something exercised today:

    PERIPH_CLOCK_INTC_0/1/2   = 18, 19, 20   (PPM Low Register 0)
    PERIPH_CLOCK_DMA_TMR_0..3 = 28..31       (PPM Low Register 0)
    PERIPH_CLOCK_PIT_0..3     = 32..35       (PPM High Register 0)
    PERIPH_CLOCK_SDHC         = 52           (PPM High Register 0)
    PERIPH_CLOCK_UART_8       = 28           (PPM Low Register 1)
    PERIPH_CLOCK_UART_9       = 29           (PPM Low Register 1)
    PERIPH_CLOCK_GPIO         = 37           (PPM High Register 1)

## 6. DSPI base addresses only (HW, incomplete corpus -- no behavior)

`sim5441x.h:1396,1420`: `dspi1` at `0xFC03_C000`, `dspi0` at `0xFC05_C000`.
Recorded because the corpus happens to state them; `dspi.h` itself is
explicitly incomplete (function prototypes only, no register field macros),
and nothing in `emu/` models DSPI, so there is no cross-check target and no
behavior is claimed or implied here. Per this task's scope, DSPI behavior is
not encoded anywhere from this reading.

## Ghidra labeling

`tools/coldfire/ghidra/McfLabels.java` now cites `docs/contracts/mcf5441x-reference-v1.json`
and both `docs/refs/linux-m5441x-893e1178.md` and this file in its header. Its
eDMA rows (`EDMA_BASE`, `EDMA_SERQ`, `EDMA_TCD_BASE`) are still sourced from
`emu/edma.py` and MCF5441XRM.pdf primarily, with the `EDMA_ERQL`/`EDMA_CINT`
additions and the `edma_tcdstruct` layout cross-checked against this corpus
(`sim5441x.h:483-497,527,1404`), as the in-script comment now states.
