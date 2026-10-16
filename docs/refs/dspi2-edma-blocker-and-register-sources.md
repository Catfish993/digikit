# DSPI2/eDMA blocker, MCF5441x register sources, and Elektron prior art

Session date: 2026-09-09. Companion to `MCF5441X-notes.md` (register manual notes),
`qemu-coldfire-feasibility.md` (why we stay on Unicorn) and
`unicorn-m68k-ccr-and-fuzzing-research.md` (the SR/CCR bug).

## TL;DR

**The coprocessor "zero transfers" symptom is structural, not a tuning problem.**
The firmware routes the SHARC link through DSPI2 with the FIFO events wired to
eDMA via two *select* bits in DSPI's `RSER`. We model none of it: `emu/dsp.py`
models a different device entirely (a FlexBus port at `0x8C000000`), DSPI2's
status register is a one-shot constant, and `emu/edma.py` implements exactly one
hardcoded channel that only fires on an explicit `SERQ` write. There is no code
path by which a DSPI2 FIFO event can start a DMA transfer, so zero transfers is
guaranteed by construction regardless of timing or instruction rate.

**A correction to a belief that was circulating:** `0xFC0C4000` is the **RNG** on
MCF5441x, not the PLL. PLL is `0xFC0C0000`; CCM is a separate module at
`0xEC090000`. The 264 MHz figure is unaffected — it rests on the DTIM3/PIT
derivation in `HANDOVER.md`, not on that register.

**Do not commit the MQX material.** It is proprietary Freescale/NXP and the
mirrors are near-certainly unauthorized. See section 5.

---

## 1. The DSPI2/eDMA blocker

### 1.1 What the hardware does (VERIFIED, two independent sources)

DSPI's `RSER` register (U-Boot names it `IRSR`) carries per-flag *enable* bits and,
critically, two **select** bits that decide whether a FIFO event raises an
interrupt or a **DMA service request**:

    DSPI_IRSR_TFFFE   0x02000000   TX FIFO fill flag -> request enable
    DSPI_IRSR_TFFFS   0x01000000   TX FIFO fill flag -> DMA (1) vs interrupt (0)
    DSPI_IRSR_RFDFE   0x00020000   RX FIFO drain flag -> request enable
    DSPI_IRSR_RFDFS   0x00010000   RX FIFO drain flag -> DMA (1) vs interrupt (0)

Source: U-Boot [`arch/m68k/include/asm/coldfire/dspi.h`](https://github.com/u-boot/u-boot/blob/master/arch/m68k/include/asm/coldfire/dspi.h) lines 99-106.
Independently corroborated by Freescale MQX 4.2 `mcf5xxx_dspi.h`, which names the
same bits `TFFF_DIRS` / `RFDF_DIRS`.

The status register is not just edge flags — it exposes **live FIFO occupancy**:

    DSPI_SR_TCF     0x80000000   transfer complete            (W1C)
    DSPI_SR_TXRXS   0x40000000   TX/RX running
    DSPI_SR_EOQF    0x10000000   end of queue                 (W1C)
    DSPI_SR_TFUF    0x08000000   TX FIFO underflow            (W1C)
    DSPI_SR_TFFF    0x02000000   TX FIFO fill (room available) (W1C)
    DSPI_SR_RFOF    0x00080000   RX FIFO overflow             (W1C)
    DSPI_SR_RFDF    0x00020000   RX FIFO drain (data ready)   (W1C)
    DSPI_SR_TXCTR   bits 15:12   TX FIFO entry count
    DSPI_SR_RXCTR   bits  7:4    RX FIFO entry count

MQX `spi_dspi_common.c` confirms `SR` is write-1-to-clear (`dspi_ptr->SR = ~DSPI_SR_TFFF_MASK;`).
`PUSHR` is a combined command+data mailbox (CONT/CTAS/EOQ/CTCNT/PCS packed with
TXDATA); `POPR` is read-only data with no command word — the two are asymmetric.

The usage pattern that matters: firmware sets `RSER` **once** with both DIRS bits,
permanently binding the FIFO to a pair of eDMA channels, then never touches DSPI
again. The hardware pumps the link autonomously thereafter. MQX's `spi_dspi_dma.c`
shows the canonical shape — two channels (one on `PUSHR`, one on `POPR`), with the
completion callback armed only on the RX channel.

### 1.2 What we actually model (VERIFIED by direct grep, 2026-09-09)

- **No DSPI register model exists.** Grepping `emu/` for `RSER|PUSHR|POPR|CTAR|TFFF|RFDF`
  returns two hits, both inert: a comment in `emu/flashboot.py:5`, and a static
  poke in `emu/dspboot.py:325`.
- **`emu/dsp.py` is not DSPI.** It models a bit-banged coprocessor port at
  `BASE = 0x8C000000` — FlexBus space, outside the internal peripheral map
  entirely. Different device.
- **DSPI2's status register is a constant.** `m.mmio[0xEC03802C] = 0x80000000`
  appears in `emu/console.py:74`, `emu/dspboot.py:326`, `emu/fastrun.py:59` and
  `emu/longrun.py:405`. That value is exactly `DSPI_SR_TCF` — "transfer complete",
  asserted forever, never reflecting FIFO state. `0xEC038000` is DSPI2's base and
  `+0x2C` is the standard SR offset, so the address is right; the behaviour is a stub.
  (`emu/dspboot.py:325` does the same for DSPI0 with `0x100000F0` = EOQF + RXCTR=15 —
  correctly decoded per the table above, but still a constant.)
- **`emu/edma.py` has one hardcoded channel.** `TX_CHAN = 35` (UART8 TX,
  `emu/edma.py:53`), fired only by a write hook on `SERQ` (`EDMA_BASE+0x18`,
  `emu/edma.py:50,167`) that matches `val & 0x3F == chan`. There is **no path for a
  peripheral to raise a hardware DMA service request.**

### 1.3 Conclusion and the falsification test

Even if the firmware sets the DIRS bits perfectly, nothing in `emu/` can fire the
channel: `PUSHR` writes are inert, `SR` never changes, and eDMA only responds to
explicit `SERQ` writes for channel 35. Zero transfers is the only possible outcome.

This explains the symptom but is **not yet confirmed against our firmware.** The
inference chain (firmware uses DSPI2 -> sets DIRS -> expects autonomous DMA) comes
from the register semantics plus lalzart's frame description (section 6), not from
a trace of our own image.

**Cheap falsification first — do this before writing any model.** Log every guest
access to `0xEC038000`-`0xEC03803F` and watch for a write to `RSER` (offset `+0x30`).
If `TFFFS`/`RFDFS` are never set, this explanation is wrong and the transfers are
routed some other way. If they are set, the model in 1.1 is the thing to build.

---

## 2. INTC mask registers are indexed, not bitmasks (second latent bug)

    INTC_SIMR(x)     ((x) & 0x3F)    write a 6-bit SOURCE INDEX
    INTC_SIMR_ALL    0x40            mask all
    INTC_CIMR(x)     ((x) & 0x3F)
    INTC_CIMR_ALL    0x40

Source: U-Boot [`arch/m68k/include/asm/coldfire/intctrl.h`](https://github.com/u-boot/u-boot/blob/master/arch/m68k/include/asm/coldfire/intctrl.h) lines 215-221.
Corroborated by MQX `int_ctrl_mcf5441.c` (`SIMR = MCF54XX_ICTRL_IMR_N(idx)`).

`SIMR`/`CIMR` are **self-decoding**: you write the source *number* and hardware
sets/clears that one bit inside IMRH/IMRL. They are not bitmask stores.

We read `IMRH/IMRL` as plain 64-bit bitmask words (`emu/pit.py:228`,
`emu/dtim.py:226-229`, with `INTC = ((0xFC048000,64),(0xFC04C000,128),(0xFC050000,192))`
and `IMR_BASE = 0x08` at `emu/pit.py:90-91`), and grepping `emu/` for `SIMR|CIMR`
returns **zero hits**. If firmware unmasks an interrupt by writing an index to
SIMR, we silently no-op it. Unverified whether our firmware does this — worth a
trace before acting.

---

## 3. Correction: 0xFC0C4000 is the RNG

`immap_5441x.h:50-51` gives `MMAP_PLL 0xFC0C0000` and `MMAP_RNG 0xFC0C4000`. CCM is
a third module at `0xEC090000` (`immap_5441x.h:76`), with `ccr` at +0x04 and `cir`
at +0x0A.

A claim was circulating (from octabam's Octatrack work, section 6) that `0xFC0C4000`
is a load-bearing PLL multiplier whose top byte must read `0x16` (22x12MHz = 264MHz).
That address is the RNG on **this** part; Octatrack uses a different ColdFire. Treat
it as not applicable here and **not** as corroboration of our 264 MHz figure, which
stands on its own empirical footing (`HANDOVER.md:40`).

Related: `CCM_CIR_PIN_MCF54415 = (0xA0<<6) = 0x2800` (`m5441x.h:221`). If firmware
reads the part ID at CCM+0x0A, it expects bits [15:6] = `0xA0`. Note the teardown
(section 6) identifies the part as **MCF54415**CMJ250 specifically.

U-Boot's reference `speed.c:76` programs `pcr = 0x13` -> multiplier 20, not 22 — a
generic default, so it neither confirms nor refutes our clock; recorded only so
nobody mistakes it for evidence.

---

## 4. Peripheral base map (from `immap_5441x.h`)

| Peripheral | Base | Peripheral | Base |
|---|---|---|---|
| XBS | 0xFC004000 | PLL | 0xFC0C0000 |
| FBCS | 0xFC008000 | **RNG** | **0xFC0C4000** |
| CAN0/1 | 0xFC020000/024000 | SSI1 | 0xFC0C8000 |
| I2C1 | 0xFC038000 | ESDHC | 0xFC0CC000 |
| DSPI1 | 0xFC03C000 | FEC0/1 | 0xFC0D4000/D8000 |
| SCM | 0xFC040000 | L2_SW0/1 | 0xFC0DC000/E0000 |
| **EDMA** | **0xFC044000** | NFC | 0xFC0FF000 |
| INTC0/1/2 | 0xFC048000/04C000/050000 | 1WIRE | 0xEC008000 |
| IACK | 0xFC054000 | I2C2-5 | 0xEC010000-01C000 |
| I2C0 | 0xFC058000 | **DSPI2** | **0xEC038000** |
| DSPI0 | 0xFC05C000 | DSPI3 | 0xEC03C000 |
| UART0-3 | 0xFC060000-06C000 | UART4-9 | 0xEC060000-074000 |
| DTMR0-3 | 0xFC070000-07C000 | RCM/CCM | 0xEC090000 |
| PIT0-3 | 0xFC080000-08C000 | GPIO | 0xEC094000 |
| EPORT0 | 0xFC090000 | SSI0 | 0xFC0BC000 |
| ADC/DAC0/1 | 0xFC094000/098000/09C000 | RRTC | 0xFC0A8000 |
| SIM | 0xFC0AC000 | USBOTG/EHCI | 0xFC0B0000/B4000 |

**Agreeing with our models:** EDMA (`emu/edma.py:49`), PIT0-3 (`emu/pit.py:81`),
DTMR0-3 (`emu/dtim.py:68`), GPIO (`emu/gpio.py:48`), ESDHC (`emu/esdhc.py:51`),
UART8 at `0xEC070000` (`emu/boot.py:34`), DSPI0 SR offset (`emu/flashboot.py:64`).
No mismatches found.

**Not modelled at all:** DSPI1/2/3, I2C0-5, CAN0/1, SSI0/1, FEC0/1, L2 switches,
EPORT0, ADC, DAC0/1, SCM, SIM, RRTC, RNG, NFC, 1WIRE, USB.

---

## 5. Source material, and what may be committed

Local copies live in the session scratchpad only, except NetBurner.

### U-Boot — GPL-2.0+, safe to reference
<https://github.com/u-boot/u-boot/tree/master/arch/m68k>
- `include/asm/immap_5441x.h` — peripheral base addresses + register structs
- `include/asm/m5441x.h` — bit definitions, INTC source numbers, CCM part IDs
- `include/asm/coldfire/{dspi,intctrl,edma,ssi}.h` — the per-peripheral bit definitions
- `cpu/mcf5445x/{cpu_init.c,speed.c,start.S}` — real bring-up sequences
  (MCF5441x is folded into the `mcf5445x` directory via `CONFIG_MCF5441x` branches)

Best single source for the register map. GPL applies if text is copied into our tree.

### Freescale MQX 4.2 — PROPRIETARY, DO NOT COMMIT
Mirror: <https://github.com/Mr-Shannon/Freescale_MQX_4_2> @ `402f872a`
(`gaodebang/eth-com-project` is a partial mirror missing `io/dma/`.)

Every file carries the Freescale MQX RTOS License: `.c` files **may not be
redistributed in source form**, use restricted to systems containing an NXP
processor, no reverse engineering. The GitHub mirrors are near-certainly
unauthorized. **Reference locally; do not vendor into this repo.**

Valuable parts: `psp_coldfire/mcf5xxx_dspi.h` (DSPI bitfields), `mcf5441.h`
(189 KB SIM register map), `io_int_ctrl/int_ctrl_mcf5441.c`, `io_spi/spi_dspi_dma.c`
(architectural template for FIFO->eDMA binding), `bsp/twrmcf54418/*` (bring-up).

Caveat: **MQX has no ColdFire eDMA binding.** The TWR-MCF54418 board wires DSPI in
*interrupt* mode. Its `edma.c` is the Kinetis/Vybrid driver; the 32-byte TCD layout
(SADDR/SOFF/ATTR/NBYTES/SLAST/DADDR/DOFF/CITER/DLAST_SGA/CSR/BITER) is the same
cross-family Freescale IP and is *plausibly* register-compatible, but ColdFire's
request-routing table is **not** confirmed there — that must come from MCF5441XRM.pdf.

### NetBurner Doxygen headers — vendor docs, already in-tree
`docs/refs/netburner-coldfire/` (94 headers, scraped via `tools/nbdocs-scrape.py`).
Most useful: `intcdefs.h` (INTC source numbers), `periph_clocks.h` (clock-gate bit
numbers), `sim5441x.h` (98 register structs). Note the site is built with
`STRIP_CODE_COMMENTS=YES` — see that directory's README for what is missing and why
`cfinter.h` is unrecoverable.

---

## 6. Elektron prior art (all discovered 2026-09-09)

A cluster of Digitakt/Octatrack RE repos appeared Aug-Sept 2026. They are
internally consistent and hash-verifiable, but new, self-citing, largely
AI-assisted, and externally unvetted. **Treat specific addresses as leads to verify
against our own image, not settled fact.**

- **[sambanks/octabam](https://github.com/sambanks/octabam)** — the most relevant.
  `docs/EMU.md` documents a Unicorn ColdFire harness booting Octatrack MKII firmware
  to the RTOS `trap #0` handoff. Claims worth testing against `emu/harness.py`:
  `ctl_set_cpu_model(UC_CPU_M68K_CFV4E)` is required (the default core mis-executes
  EMAC `macl`/`movclrl`); SR must be seeded **before** A7 or the stack banks swap
  wrong; Unicorn's CFV4E treats VBR as a no-op and does **not** auto-dispatch `trap`
  (catch `UC_HOOK_INTR` intno=32 and dispatch by hand). Its `0xFC0C4000` PLL claim
  does not apply to our part — see section 3.
- **[lalzart/digitakt-ii-firmware-research-public](https://github.com/lalzart/digitakt-ii-firmware-research-public)**
  — Digitakt II OS 1.15C. Maps the full-duplex **DSPI2/eDMA inter-processor frame**
  (0xabc-byte frame, 0x802-byte CPU->DSP payload) and a separate sample path
  (Rapid-GPIO -> SHARC LP0/DMA30). Directly about the channel in section 1.
  Deliberately redacted of addresses — a map, not a toolkit.
- **[mischa85/elektron-firmware-tool](https://github.com/mischa85/elektron-firmware-tool)**
  — `.syx` container format (ELE3/ELE2/ELEK, aPLib LZ77). Not encrypted:
  compression plus **HMAC-SHA256 keyed from material embedded in the image**, so
  images can be re-signed. Byte-exact on 22/23 images.
- **[bryantysinger/elektron-models-teardown](https://github.com/bryantysinger/elektron-models-teardown)**
  — Model:Samples/Cycles. MAIN OS at `0x40000400`, stack `0x48000000`, bootstrap at
  `0x80000400`. Notes **RTTI left in: ~407 class names recoverable** (cf. `tools/coldfire/symbols.py`).
- **[angellinares/dn2_firmware_explore](https://github.com/angellinares/dn2_firmware_explore)**
  — Digitone II. Reports Ghidra's stock ColdFire disassembly validates against
  `objdump` while **Capstone fails**.
- **[genosdk/analog-rytm-mkii-research](https://github.com/genosdk/analog-rytm-mkii-research)**
  — same container format; traces one parameter end-to-end to the DSPI/eDMA transport.
- **Teardown:** <https://www.elektronauts.com/t/digitakt-ii-teardown/212476> —
  identifies **NXP MCF54415CMJ250** and a SHARC (likely ADSP-21569).

Explicit negatives: no public JTAG/BDM flash-dump method for any Digi device; no
Ghidra loader; no named RTOS kernel; no prior Digitakt emulation attempt.

Overbridge/SysEx work is mature but not firmware-internals:
[overwitch](https://github.com/dagargo/overwitch), [dtdump](https://github.com/droelfdroelf/dtdump).

---

## 7. What is NOT verified

- That our firmware sets `RSER` DIRS bits at all (section 1.3 test settles it).
- That our firmware uses SIMR/CIMR rather than direct IMRH/IMRL writes.
- ColdFire eDMA request-routing: which channel numbers DSPI2 TX/RX map to. Neither
  U-Boot nor MQX gives this; it must come from MCF5441XRM.pdf.
- Every address in section 6, against our own image.
- MQX's Kinetis TCD layout being byte-compatible with ColdFire eDMA.
