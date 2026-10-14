# MCF5441x Hardware Register Research Notes

## Downloads

**MCF5441XRM.pdf** (saved as `docs/refs/MCF5441XRM.pdf`) — SUCCESS, but not from NXP directly.
- The document ID is actually **MCF54418RM** (NXP applies the "418" variant number as the doc ID even
  though it covers the whole MCF5441x family — MCF54410/54415/54416/54417/54418). There is no
  `MCF5441XRM.pdf` at NXP; that filename 404s.
- Direct `curl` to `https://www.nxp.com/docs/en/reference-manual/MCF54418RM.pdf` returned **HTTP 404**
  with a `text/html` "Page not available" body, even with a browser User-Agent and a `Referer` header set
  to the NXP product page. A `WebFetch` (browser-rendered) request to the NXP product page
  (`https://www.nxp.com/products/MCF5441X`) *did* succeed and listed the same URL as a valid, currently
  published link (Rev 5, 05/2018, 7.39 MB) — so the file exists on nxp.com, but nxp.com's edge (Akamai)
  appears to block plain `curl` requests (bot/TLS-fingerprint gating), not a login wall per se.
- Successfully downloaded instead from a third-party mirror: **NetBurner** (a ColdFire/MCF5441x board
  vendor that redistributes the NXP manual for its customers), via the exact URL
  `https://www.netburner.com/download/freescale-coldfire-mcf5441x-reference-manual/?wpdmdl=4188&ind=0&refresh=f6d6906b&filename=MCF54418RM.pdf`
  — HTTP 200, `content-type: application/pdf`, 7,748,606 bytes.
- Verified: `file` reports "PDF document, version 1.6 (zip deflate encoded)". `pdfinfo` reports:
  Title: "MCF5441x Reference Manual", Author: "NXP Semiconductors", **1360 pages**, CreationDate
  2018-04-24, matching the expected Rev 5 05/2018 manual exactly.
- Local path: `docs/refs/MCF5441XRM.pdf`

**CFPRM.pdf** (saved as `docs/refs/CFPRM.pdf`) — SUCCESS, but not from NXP directly.
- Direct `curl` to `https://www.nxp.com/docs/en/reference-manual/CFPRM.pdf` also returned **HTTP 404**
  / "Page not available" HTML, for the same apparent bot-gating reason as above (a `WebFetch` browser
  fetch of the NXP product page confirmed this exact URL is the live, linked NXP document, Rev 3,
  1.63 MB).
- Successfully downloaded instead from a third-party mirror: `http://www.chromakinetics.com/REB1200/CFPRM.pdf`
  (a hobbyist ColdFire board/project site that has hosted a copy of this manual for years) — HTTP 200,
  `content-type: application/pdf`, 3,779,962 bytes.
- Verified: `file` reports "PDF document, version 1.3". `pdfinfo` reports: Title "CF_PRM_book", Author
  "Howard Richey" (a Freescale ColdFire architecture author), **316 pages**, encrypted with RC4 but with
  print/copy/extract permitted (this is a very old, harmless owner-password lock that doesn't block
  reading — `pdftotext`/`pdfinfo` opened it fine).
- Local path: `docs/refs/CFPRM.pdf`
- Note: this is a larger (Rev 3, 316-page) copy than the "1.63 MB" NXP listing implies, but its title,
  author, and content match the known CFPRM document — likely the NXP copy is a later/trimmed revision;
  either way it is the same document title/series, used here as the correct manual for the ColdFire ISA
  programming model.

Both PDFs were text-extracted locally with `pdftotext -layout` (macOS Homebrew poppler) for searching;
no extracted `.txt` file was kept in the repo per the task's file-creation constraints.

---

## 4a. PIT (Programmable Interrupt Timer) PCSRn register — MCF5441x specific

**VERIFIED FROM MCF5441XRM PRIMARY SOURCE** (mirrored copy, content matches official NXP Rev 5 05/2018).
Citation: Chapter 38, "Programmable Interrupt Timers (PIT0–PIT3)", Section 38.2.1 "PIT Control and
Status Register (PCSRn)", Figure 38-2 and Table 38-3, page 38-3/38-4 of the PDF.

PCSRn is a 16-bit register (one per PIT0–PIT3, at offset 0x0 of each PIT's 0x4000-spaced block:
0xFC08_0000/0xFC08_4000/0xFC08_8000/0xFC08_C000). **Reset value: 0x0000** for all four instances.

Exact bit layout (bit 15 = MSB, bit 0 = LSB):

| Bits  | Field    | R/W        | Meaning |
|-------|----------|------------|---------|
| 15–12 | Reserved | —          | Must be cleared |
| 11–8  | PRE[3:0] | R/W        | Prescaler: internal-bus-clock divisor = 2^PRE (0000→÷1 up to 1111→÷32768). Change only while EN is clear for predictable timing (though EN can be set in the same write as PRE). |
| 7     | Reserved | —          | Must be cleared |
| 6     | DOZE     | R/W        | 0 = PIT unaffected in doze mode; 1 = PIT stopped in doze mode (resumes prior state on exit) |
| 5     | DBG      | R/W        | 0 = PIT unaffected in debug/halted mode; 1 = PIT stopped while core is in debug mode (resumes prior state on exit; toggling DBG while already in debug mode starts/stops the timer live) |
| 4     | OVW      | R/W        | Overwrite: 0 = PMRn value loads into counter only when count reaches 0x0000 (normal reload); 1 = writing PMRn immediately/transparently overwrites the live counter value |
| 3     | PIE      | R/W        | PIT interrupt enable: 1 = PIF asserts an interrupt request to the INTC; 0 = disabled |
| 2     | PIF      | R, **W1C** | PIT interrupt flag, set when the counter reaches 0x0000. **Confirmed write-1-to-clear**: "Clear PIF by writing a 1 to it or by writing to PMR. Writing 0 has no effect." |
| 1     | RLD      | R/W        | Reload bit: 0 = counter rolls over 0x0000→0xFFFF (free-running, no reload from modulus); **1 = counter is reloaded from PMRn when count reaches 0x0000** (this is the "set-and-forget"/auto-reload mode) |
| 0     | EN       | R/W        | PIT enable: 0 = PIT disabled (counter and prescaler held stopped); 1 = PIT enabled. Read/write anytime. |

**Decoding 0x53F** (binary `0000 0101 0011 1111`): PRE=0101 (÷32), DOZE=0, DBG=1, OVW=1, PIE=1, PIF=1,
RLD=1, EN=1. This **confirms your inference exactly**: RLD=1 means the counter reloads from PMRn (the
modulus register) on reaching zero rather than free-running/one-shot rollover, and PIF is indeed a
write-1-to-clear status bit (not writable to 0, not auto-clearing on read).

**Correction on nomenclature**: there is **no "HALTED" bit in PCSRn**. The manual does not use that name
in the PIT chapter; the field controlling behavior while the core is halted/in debug mode is **DBG**
(bit 5), described above. "HALTED" as a distinct field name does appear elsewhere in the manual (in the
DMA Timer's DTXMRn register, Chapter on DMA Timers — an unrelated, differently-numbered module), so if
"HALTED" was expected as a PIT bit name, that expectation does not hold for MCF5441x; DBG is the
correct/only PIT field governing debug-halt behavior. There is also no separate PCSR "OVW behavior for
DBG" — OVW and DBG are independent, orthogonal bits as tabulated above.

Companion registers (for completeness, same chapter):
- **PMRn** (PIT Modulus Register), 16-bit R/W, reset 0xFFFF, offset +0x2 — holds the reload/modulus value.
- **PCNTRn** (PIT Count Register), 16-bit read-only, reset 0xFFFF, offset +0x4 — live countdown value.

---

## 4b. INTC0/INTC1/INTC2 interrupt source assignment tables — MCF5441x specific

**VERIFIED FROM MCF5441XRM PRIMARY SOURCE.**
Citation: Chapter 17, "Interrupt Controller Modules (INTC)", Section 17.2.9.1 "Interrupt Sources":
- Table 17-15 "Interrupt Source Assignment For INTC0" (pages 17-12 to 17-14)
- Table 17-16 "Interrupt Source Assignment for INTC1" (pages 17-14 to 17-16)
- Table 17-17 "Interrupt Source Assignment for INTC2" (pages 17-17 to 17-18)

**INTC0** (base 0xFC04_8000; vector_number = 64 + source):

| Source | Module      | Flag / description |
|--------|-------------|---------------------|
| 1      | EPORT0      | `EPFR0[EPF1]` — Edge Port 0 flag 1 |
| 2      | EPORT0      | `EPFR0[EPF2]` — Edge Port 0 flag 2 |
| 4      | EPORT0      | `EPFR0[EPF4]` — Edge Port 0 flag 4 |
| 33     | DTIM1       | DMA/general-purpose Timer 1 interrupt (`DTER1`) |
| 35     | DTIM3       | Timer 3 interrupt (`DTER3`) |
| 44     | MAC-NET0    | **Not used** (gap in the ENET0 EIR block, between LC=43 and GRA=45) |
| 57     | MAC-NET1    | **Not used** (symmetric gap in the ENET1 EIR block, between LC=56 and GRA=58) |

**INTC1** (base 0xFC04_C000; vector_number = 128 + source):

| Source | Module      | Flag / description |
|--------|-------------|---------------------|
| 6      | FlexCAN0    | **Not used** (gap between FlexCAN0's `IFLAG1[BUFnI]`-family entries) |
| 28     | DMA         | `EDMA_INTR[INT36]` — DMA channel 36 transfer complete |
| 29     | DMA         | `EDMA_INTR[INT37]` — DMA channel 37 transfer complete |
| 40     | DMA         | `EDMA_INTR[INT48]` — DMA channel 48 transfer complete |
| 42     | DMA         | `EDMA_INTR[INT50]` — DMA channel 50 transfer complete |
| 52     | UART8       | `UISR8` — UART8 interrupt request |
| 53     | UART9       | `UISR9` — UART9 interrupt request |
| 54     | DSPI1       | `DSPI1_SR` — DSPI1 OR'd interrupt |
| 63     | —           | **Not used** |

**INTC2** (base 0xFC05_0000; vector_number = 192 + source):

| Source | Module | Flag / description |
|--------|--------|---------------------|
| 13     | **PIT0** | `PCSR0[PIF]` — PIT interrupt flag |
| 15     | **PIT2** | `PCSR2[PIF]` — PIT interrupt flag |
| 16     | **PIT3** | `PCSR3[PIF]` — PIT interrupt flag |
| 17     | USB OTG | `USB_STS` — USB OTG interrupt |
| 29     | SIM     | `SIM_TSR` — SIM data interrupt |
| 30     | SIM     | `SIM_RSR` — SIM general interrupt |

Your prior belief (source 13=PIT0, source 15=PIT2, source 16=PIT3 on INTC2) is **CONFIRMED exactly**
by Table 17-17. For completeness, source 14 (not asked, but adjacent) = PIT1 (`PCSR1[PIF]`).

---

## 4c. Interrupt tie-break/arbitration rule within one INTC (same ICR level)

**VERIFIED FROM MCF5441XRM PRIMARY SOURCE.**
Citation: Chapter 17, Section 17.3.1 "Interrupt Controller Theory of Operation", page 17-20, and
Table 17-19 "Example Interrupt Priority Within a Level".

Direct quote: *"The priority structure within a single interrupt level depends on the interrupt source
number assignments... **The higher numbered interrupt source has priority over the lower numbered
interrupt source.**"*

The manual's own worked example (Table 17-19) has sources 40, 22, 8, and 2 all programmed to the same
level (ICR[2:0] = 011); priority order is 40 (highest) > 22 > 8 > 2 (lowest) — i.e. strictly by
descending source number.

**Answer: the higher source number wins** (gets priority / is serviced first) when two enabled sources
in the same INTC share the same programmed interrupt level.

Note this is distinct from, and layered underneath, **Section 17.3.2 "Prioritization Between Interrupt
Controllers"** (page 17-21), which separately states INTC0 has fixed higher priority than INTC1, which
has fixed higher priority than INTC2, when multiple controllers have active requests at the same level —
that inter-controller rule does not answer the intra-controller tie-break, which is the source-number
rule above.

---

## 4d. FlexBus chip-select registers CSAR/CSMR/CSCR

**VERIFIED FROM MCF5441XRM PRIMARY SOURCE.**
Citation: Chapter 20, "FlexBus", Section 20.3 "Memory Map/Register Definition":
- Section 20.3.1 "Chip-Select Address Registers (CSAR0–CSAR5)", Figure 20-1 / Table 20-4, page 20-6
- Section 20.3.2 "Chip-Select Mask Registers (CSMR0–CSMR5)", Figure 20-2 / Table 20-5, page 20-6/20-7
- Section 20.3.3 "Chip-Select Control Registers (CSCR0–CSCR5)", Figure 20-3 / Table 20-6, pages 20-7 to 20-9

There are 6 chip-selects (FB_CS0–FB_CS5), each with its own CSARn/CSMRn/CSCRn (32-bit registers,
User read/write), base 0xFC00_8000, spaced 0xC bytes apart per chip-select (CSARn/CSMRn/CSCRn are at
offsets +0x0/+0x4/+0x8 within each chip-select's 12-byte block; e.g. CSAR0=0xFC00_8000,
CSMR0=0xFC00_8004, CSCR0=0xFC00_8008, then CSAR1=0xFC00_800C, etc.).

**CSARn** (Chip-Select Address Register), reset 0x0000_0000:
| Bits  | Field | Meaning |
|-------|-------|---------|
| 31–16 | BA    | Base address — compared against internal address bus bits[31:16] |
| 15–0  | —     | Reserved, must be cleared |

**CSMRn** (Chip-Select Mask Register), reset 0x0000_0000:
| Bits  | Field | Meaning |
|-------|-------|---------|
| 31–16 | BAM   | Base address mask: setting a bit makes the corresponding CSARn address bit a "don't care". Block size = 2^n bytes where n = (popcount of BAM) + 16. |
| 15–9  | —     | Reserved |
| 8     | WP    | Write protect: 0 = read+write allowed; 1 = read-only (writes bus-error) |
| 7–1   | —     | Reserved |
| 0     | V     | Valid bit: chip-select registers don't take effect until V=1 (except FB_CS0, the global/boot chip-select, which is active at reset regardless of its own V bit). Reset clears V. |

**CSCRn** (Chip-Select Control Register), reset differs for CSCR0 (global/boot CS) vs CSCR1–5 — CSCR0's
ASET/RDAH/WRAH reset to `11` (max setup/hold, safe for slow boot memory) while CSCR1-5 reset those
fields to `00`:
| Bits  | Field  | Meaning |
|-------|--------|---------|
| 31–26 | SWS    | Secondary wait states (burst secondary terminations, used only if SWSEN=1) |
| 25–24 | —      | Reserved |
| 23    | SWSEN  | Secondary wait-state enable (0 = WS used for all transfers; 1 = SWS used for burst secondary terminations) |
| 22    | EXTS   | Extended transfer start (FB_TS pulse-width control) |
| 21–20 | ASET   | Address setup: delay (1–4 rising clock edges) before FB_CSn asserts after address valid |
| 19–18 | RDAH   | Read address/attribute hold (deselect) time after read termination |
| 17–16 | WRAH   | Write address/attribute/data hold time after write termination |
| 15–10 | WS     | **Wait states**: number of wait states (0–63) inserted after FB_CSn asserts before internal transfer ack, when AA=1 |
| 9     | BLS    | Byte-lane shift (left- vs right-justified data on FB_AD) |
| 8     | **AA** | **Auto-acknowledge enable**: 0 = no internal FB_TA generated, cycle terminated externally by the device; 1 = internal transfer-ack asserted per the WS count |
| 7–6   | **PS** | **Port size**: `00` = 32-bit (FB_D[31:0]); `01` = 8-bit (FB_D[31:24]); `1x` = 16-bit (FB_D[31:16]) |
| 5     | BEM    | Byte-enable mode (whether FB_BE/BWE asserts on reads as well as writes) |
| 4     | BSTR   | Burst-read enable |
| 3     | BSTW   | Burst-write enable |
| 2–0   | —      | Reserved |

This confirms: **AA (bit 8)** controls whether the FlexBus generates its own internal transfer
acknowledge (auto-ack, using the WS wait-state count) versus waiting for an externally-driven `FB_TA`;
**PS (bits 7–6)** is the 2-bit port-size field (32/8/16-bit, note both `10` and `11` mean 16-bit); and
**WS (bits 15–10)** is the 6-bit wait-state count field used only when AA=1.

---

## Sources

- https://www.netburner.com/download/freescale-coldfire-mcf5441x-reference-manual/?wpdmdl=4188&ind=0&refresh=f6d6906b&filename=MCF54418RM.pdf — (secondary, mirror) actual bytes of MCF5441XRM.pdf (MCF54418RM Rev 5, 05/2018) used for all of sections 4a–4d above.
- https://www.nxp.com/docs/en/reference-manual/MCF54418RM.pdf — (primary, NXP) the canonical/original location of the same document; confirmed live/linked via `WebFetch` of the NXP product page, but returned HTTP 404 to direct `curl` (edge/bot gating), so the actual bytes were not fetched from here.
- https://www.nxp.com/products/MCF5441X — (primary, NXP) product page confirming the exact NXP doc URLs, revisions, and file sizes for MCF54418RM.pdf and CFPRM.pdf.
- http://www.chromakinetics.com/REB1200/CFPRM.pdf — (secondary, mirror) actual bytes of CFPRM.pdf (ColdFire Family Programmer's Reference Manual, Rev 3) used as the CFPRM download; not needed for facts 4a–4d, which are all MCF5441x-specific and answered from MCF5441XRM.pdf directly.
- https://www.nxp.com/docs/en/reference-manual/CFPRM.pdf — (primary, NXP) canonical location of CFPRM.pdf; same 404-to-curl / live-per-WebFetch situation as MCF54418RM.pdf above.
