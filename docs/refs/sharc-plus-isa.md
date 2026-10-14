# SHARC+ instruction set: sources, and what they actually contain

Companion to `tools/sharc_dev/sharcldr.py`. The PDFs themselves are gitignored
(`.gitignore:21`, `docs/refs/*.pdf`); this note is the committed part, and
cites chapter/page numbers so the claims can be re-checked.

For the documents and method behind `tools/sharc_dev/sharcspec/`, see
`docs/sharc/SOURCES.md`.

## Why this matters here

Section 7 of the firmware container is the SHARC DSP image. The ColdFire MAIN
OS makes no audio -- it RPCs parameter changes to this DSP -- so every question
about adding machines, filters or audio-affecting parameters terminates in code
nobody in this project can currently read. These are the documents that would
change that.

## Provenance -- read before trusting

`www.analog.com` refuses non-browser clients: `curl` gets HTTP/2
`INTERNAL_ERROR`, and forcing HTTP/1.1 gets a connection that transfers zero
bytes. `download.analog.com` answers fine but does not host processor manuals
(404). Both PDFs below were therefore fetched from a third-party mirror,
`docs.ampnuts.ru`, under its `adsp-21569` path:

    docs/refs/sharc-plus-prm.pdf    4,375,778 B   SHARC+ Core Programming Reference
    docs/refs/adsp-2156x-hwr.pdf   11,954,389 B   ADSP-2156x SHARC+ Hardware Reference

**These have not been checksummed against Analog Devices' own copies.** A
browser can reach analog.com normally; anyone relying on these for detailed
encoding work should re-download from the vendor and compare. Treat the mirror
as convenient, not authoritative. Canonical locations:

- `https://www.analog.com/media/en/dsp-documentation/processor-manuals/sc58x-2158x-prm.pdf`
- `https://www.analog.com/media/en/dsp-documentation/processor-manuals/adsp-2156x_hwr.pdf`

The classic 48-bit SHARC manual (`adsp-2136x_2137x_214xx_pgr_rev2.4.pdf`) was
not obtainable from the mirror (404) and is not held here.

## What the Programming Reference contains

771 pages. Title page reports "SHARC+ Core Programming Reference". The part
that matters is the **Instruction Set Reference**, beginning at printed page
12-2 and running through chapters 13, 14 and 15.

Instructions are organised by *type*, each documented in both ISA (48-bit) and
VISA (compressed) form where both exist. Types seen in the contents include
1a/1b, 2a/2b/2c, 3a/3b/3c/3d, 4a/4b/4d, 5a/5b, 6a, 7a/7b/7d, 8a, 9a/9b,
10a, 11a/11c, 12a, 13a, 14a/14d, 15a/15b, 18a.

**Bit-level encodings are present**, not just syntax. Verified concretely on
Type 8a (cond + branch), printed page 14-2, PDF page 356:

    Type8a   47 46 45 44 43 42 41 40 39 38 37 36 35 34 33 32
              0  0  0  0  0  1  1  0  0  0  0  0  0  0  0  0
             fields: cond[4:0], r, b, a
             31 ... 16    fields: addr[23:16], ci, j
             15 ... 0

so the opcode prefix is bits 47..40, with a 24-bit direct or PC-relative
target and a 5-bit condition. That is exactly what a call/jump finder needs.
Pages carrying short-word-only diagrams (PDF 364, 374, 392, 398, 412) are the
VISA 16/32-bit forms.

Note the extracted text separates field labels from their bit positions,
because the diagrams are vector graphics with floating text. The information is
all there but needs reading, not scraping.

## Why VISA and not classic 48-bit SHARC

Measured, not assumed -- see `tools/sharc_dev/sharcldr.py --align` and the commit that
added it. Repeated code motifs in the 10,312-byte loader payload land on even
offsets exclusively (`f29fc09f1200` 56 times, all at `offset % 2 == 0`) while
spreading uniformly across mod 4, mod 6 and mod 8. A fixed 48-bit encoding
would pin them to one residue mod 6. So the stream is 16-bit granular and
variable length, which on SHARC+ is VISA.

Consequence: a Ghidra SLEIGH module for this target is a **SHARC+ VISA**
module. Work based on the classic ADSP-21xxx fixed 48-bit encoding does not
transfer directly.

## Tooling consequence

The vendor DSP toolchain is Windows and Linux only, with no macOS build, so
on an Apple Silicon Mac it needs a VM or an emulated x86-64 container. The
encoding tables are in the Programming Reference, so it is not needed. A
decoder or SLEIGH module can be written from this document alone.

## Not established

- Whether the mirrored PDFs match Analog Devices' originals.
- Whether the large product-specific region of section 7 is code. The
  even-offset bias is suggestive (Digitakt's 224 KB region is 55% even against
  62% for known loader code and 50% for float tables) but no instruction has
  been decoded anywhere, in the loader or beyond it.
- Whether machine algorithms are dispatched through a table of function
  pointers, which is the question that decides whether adding a machine is
  tractable.
