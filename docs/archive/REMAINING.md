# What remains: new machines, and a patching framework

Status review, 2026-09-13. Two goals are in play and they are **not** equally
blocked, so they are costed separately:

- **Goal A — a patching framework**: select patches, apply them to an existing
  firmware, repack, emit a flashable `.syx`. Tractable. Mostly engineering, with
  three genuine reverse-engineering gaps, all small and all located.
- **Goal B — new machines on DT2 and DN2**: blocked behind reading SHARC+ code.
  One specific, cheap-to-attempt defect currently gates the entire approach.

Everything below that is a measurement was measured on this branch, not
inherited from earlier docs. Claims inherited from earlier docs are marked.

---

## Goal A — the patching framework

### A.1 What already exists

| Step | Tool | State |
|---|---|---|
| `.syx` → 8-in-7 decode → ELE3 → section table | `dt2/container.py` | works |
| sections → decompressed blobs | `emu/extract.py` (`dt2/elz.py` by default, all four firmwares; `--oracle` runs the device's own depacker under Unicorn, 1.15C and 1.10E only) | works |
| byte-exact, length-preserving edit of a decompressed section | `tools/patchimg.py` | works; validates old bytes, refuses overlaps, enforces `len(old)==len(new)` |
| CRC-32 oracle (`0x80001bd0`) | `emu/oracle.py` | works, byte-exact vs zlib |
| depack oracle (`0x80000432`) | `emu/oracle.py` | works |
| boot the patched image to a rendered UI | `emu/run.py` | works (DT2; DN2 does not boot) |

So the *inbound* half of the pipeline is finished and the acceptance check for
"did my edit break the image" exists in emulation.

### A.2 What is missing, in pipeline order

The outbound half — patched sections back to a flashable `.syx` — **does not
exist at all**. No repacker is in this repo. Earlier docs describe a verified
round trip, but that was performed by an external tool
(`mischa85/elektron-firmware-tool`), which **is not present on this machine**
and which this repo never shows a pack invocation for. Treat "repacking is
solved" as unverified.

Ordered checklist. `[W]` = write it, algorithm fully known; `[R]` = needs
reverse engineering first.

1. `[W]` **aPLib compressor.** None exists anywhere — not in the repo, not in
   the venv, not on PyPI (the `aplib` package's `pack()` is
   `raise NotImplementedError`). Crucially this is *cheap*: earlier work
   established that byte-exact packing is unnecessary, since the device's own
   depacker accepts any valid stream. A **store-only encoder** — tag bit `0` +
   literal byte, per the documented tag-bit format, plus the end code — is a few
   dozen lines and needs no LZ77 matching at all. Sections grow; nothing checks
   that. Do this first and defer a real packer indefinitely.
2. `[W]` **Per-section 8-byte header**: big-endian `[u32 compressed_len][u32 byte_sum]`.
   The reader is `dt2/container.py:70` / `emu/extract.py:59`; only the writer is
   missing. Note §4 (updater) is stored raw with sum 0 and §5 (meta) has no
   header — preserve both quirks.
3. `[W]` **ELE3 section-table writer**: magic, count at `0x1C`, 16-byte entries
   `(id, offset, comp_len, dest)` from `0x20`. Reader at `dt2/container.py:16,57`;
   the writer is a direct inverse.
4. **32-bit content checksum.** Done — traced at `0x80003ca6` (not
   `0x40003ca6`, a transcription slip in earlier notes; that address is the
   bootstrap's own address space and is the instruction
   `move.l (0x40000000).l,D2` that reads the length word the checksum
   covers), written into preamble bytes 4..8. The algorithm is
   `sum(i ^ word_i)` over 1-based big-endian u32 words of the whole container
   including the 32-byte trailer, compared against preamble bytes 4-7.
   Confirmed byte-exact on all four firmwares. Implemented as
   `dt2.build.content_checksum`.
5. **HMAC-SHA256 trailer.** Done — trailer computed at `0x80005e2a`; the key
   is derived at `0x80005d90`, same code in both devices:
   `key[i] = CONST[i] ^ sha256(STRING)[i] ^ sha256(STRING[::-1])[i]`, with
   Digitakt II `STRING="Master Overdrive"` @`0x80006ff8` / CONST
   @`0x80007009`, and Digitone II `STRING="Multiplier"` @`0x8000706c` / CONST
   @`0x80007077`. It is textbook HMAC-SHA256 over `container[:total_len-32]`.
   The "32-byte constant beginning `69 5d 82 bc`" noted above is only one of
   the three XOR operands, not the key itself. Implemented as
   `dt2/authcode.py`.
6. `[W]` **8-in-7 re-encoding.** Direct inverse of `dt2/container.py:38-44`.
   No ambiguity.
7. **Per-packet SysEx checksum.** Done — `decode_syx()` had sliced
   `body[9:125]` and discarded byte 125 without ever computing or checking
   it. The algorithm is `(K + sum(body[6+i] ^ (i+K), i=0..118)) & 0x7F`,
   where `body` is the bytes between F0 and F7 and `K` is byte 7 of the
   16-byte framing message (`0x0F` Digitakt II, `0x10` Digitone II).
   Verified on 64,053 messages across four firmwares. Implemented as
   `dt2.build.packet_checksum`. Worth recording: an earlier exhaustive
   black-box search over 30,603 pairs failed because the construction folds
   each byte's own index in, a family the search never tried.
8. `[W]` **Round-trip gate.** Repack → re-extract with `emu/oracle.py` → assert
   all five sections are byte-identical to what went in. Cheap, and it is the
   thing that makes flashing defensible.

### A.3 What "framework" means, and what is there today

There is no patch framework — only a good single-shot CLI. `tools/patchimg.py`
takes `--str ADDR=OLD:NEW` / `--bytes ADDR=OLDHEX:NEWHEX` on the command line
and *emits* a JSON manifest for audit, but nothing ever *reads* a manifest back.
There is no patch file format, no named-patch catalogue, no versioning of a
patch against a firmware hash, and no selection mechanism. `patches/` is
unrelated — it holds two source patches for Unicorn's m68k translator (see
`patches/README.md`).

For the stated goal, what is needed on top of the repack chain:

- a patch description format (one file per patch: id, description, target
  device + firmware SHA-256, section id, and a list of length-preserving edits);
- a loader/selector that takes a set of patch ids, checks them against the
  firmware's SHA-256, and refuses to apply two patches that overlap;
- `apply` as the inverse of the manifest `patchimg.py` already writes — this is
  a small refactor of existing, working code, not new logic;
- device parameterisation. `emu/device.py` + `devices/*.toml` already do this
  properly for the **panel**, but every container/address constant is hardcoded
  DT2-1.15C (`emu/oracle.py` says so in its own header). Addresses must move
  into the device TOMLs before DN2 can be patched at all.

**Estimate for Goal A:** the `[W]` items are a day or two of ordinary work. The
three checksum/HMAC items above are done, traced under a harness that already
existed and had already been used to do exactly this twice (CRC-32,
depacker). None of them was a research risk. Goal A is achievable.

---

## Goal B — new machines

### B.1 Where machines actually live

Settled, and it is the uncomfortable answer: **machines are only labels and
parameter IDs on the ColdFire; the audio runs on the SHARC+.** The MAIN OS holds
parameter state under a `Digisharc` namespace and RPCs changes across. So the
ColdFire side — which is the side this project can read, boot, and patch — can
rename a machine and reshape its parameter pages, but cannot make a new sound.

What is located on the ColdFire side: a string table at `0x4022b20e` with the
machine and filter names (`WERP`, `STRETCH`, `REPITCH`, `SLICED SMP`, `SVAR`,
`LP4`, `COMB±`, …). There *is* a machine-ID dispatch and a per-machine
descriptor array — found by emulator read-watch, not statically, which is why
static analysis missed it. Dispatch is `FUN_400caf48`:
`type < 7 ? type*0x2c + 0x42923644 : 0x4292374c` (the fallback is exactly
entry 6, so an out-of-range type yields MANUAL SLICE rather than crashing).
The descriptor array itself — base `0x42923644`, stride `0x2c` (44 bytes), 7
entries — lives in bss, so it exists only at runtime; field accessor
`FUN_4001762c(obj, field) -> *(descriptor + 8 + field*4)`. The 7 entries are
`0 SAMPLE, 1 WERP, 2 STRETCH, 3 REPITCH, 4 SLICED SMP, 5 MIDI, 6 MANUAL
SLICE`. The UI list length is not a numeral but a rodata range copied into a
`std::vector<int>`: source list `0x401e1958`-`0x401e1974` = `{0,1,2,3,6,4,5}`,
filter list `0x401e1940`-`0x401e1958` = `{0,1,4,3,5,2}`.

On the SHARC side (container §7) `tools/sharcscan.py` finds, by literal
pattern-matching two cjump encodings, candidate indirect-call tables. Reproduced
on this branch:

- **Digitakt**: 11-entry ascending, 100%-indirect table at `0x0034fc`
  (`0x1c3c3e, 0x1c3c4f, 0x1c3c72, 0x1c3c95, …`). DT2 exposes 5 source machines +
  6 filters = 11.
- **Digitone**: structurally parallel 6-entry table at `0x003a94`.

That 11 is a striking coincidence and it is *still* only a coincidence. The tool
says so itself and it is right to: confirming it means disassembling the code
that indexes the table, which needs a working VISA decoder.

### B.2 The VISA decoder: measured, then fixed

The repo's SHARC tooling had only synthetic self-tests; nobody had run it
against firmware. Measured on this branch, both blobs, past the shared
11,248-byte ADI loader prologue, 2,000 evenly spaced 2-byte-aligned starts per
device, with a random-bytes control run through the same code:

| | Digitakt | Digitone | random control |
|---|---|---|---|
| median instrs before stall | 20 | 145 | 15 / 13 |
| max | 550 | 33,515 | 165 / 211 |
| stalls on `GROUP_5A_5B_MOVE` | 88.5% | 93.0% | ~68% |

Digitakt real firmware decoded only **1.33×** further than literal noise. The
decoder was not tracking code.

The stalls were concentrated in one place: `GROUP_5A_5B_MOVE`, the unresolved
48-bit-vs-32-bit length ambiguity for the Type5 register-move pair. The
transcription's own note said the likely cause was a missed gray bit in one PDF
figure crop.

**It was.** Figure 13-15 (PRM PDF p340, Type5b_move), re-rendered at 400 DPI,
has **eight** gray cells in its second word — bit 30, and bits 22:16 — where the
revision-2 transcription recorded none. Figure 13-13 (p337, Type5a_move) shows
those same bits 22:16 as `compute[22:16]`, white and variable. That is the
discriminator, and it is unambiguous:

| second word, bits 22:16 | type | length |
|---|---|---|
| all zero | `5b_move` | 32-bit |
| non-zero | `5a_move` | 48-bit |

The same re-read caught a second, independent error: `srcureglow[0:0]` sits at
source bit 31 in both figures, but was recorded at own-bit 23 (`5a_move`) and
own-bit 7 (`5b_move`) — in each case colliding with `dstureg`'s range. That
affects operand extraction, not length.

### B.3 Result after the fix — measured, with a control

| | Digitakt | Digitone |
|---|---|---|
| median instrs, before → after | 20 → **632** | 145 → **1,850** |
| max, before → after | 550 → 9,758 | 33,515 → 68,144 |
| stalls on `GROUP_5A_5B_MOVE` | 88.5% → **0%** | 93.0% → **0%** |
| random-bytes control median | 15 → 45 | 13 → 44 |
| **real ÷ control** | 1.33× → **14.1×** | 11.2× → **42.1×** |

The control matters: a more permissive grammar walks further through noise too.
It improved ~3×. Real firmware improved 13–32×. The signal-to-noise ratio grew
10.5× on Digitakt and 3.8× on Digitone, so this is not grammar loosening.

One further consistency check, which is the most convincing part: targeted walks
that were *already* stalling on "no pattern matches" before the fix are
**byte-for-byte unchanged** — same instruction count, same stall offset. Only
walks that had been hitting the Type5 ambiguity moved, by 3.6–105×. A blanket
loosening would have shifted everything.

The dominant stall reason is now "no pattern in TYPES matches word0 at all"
(99.6–99.9%), which is the honest remaining gap rather than a known defect.
Behind it sit `Type3a` and `Type25c_rframe`, both with `opcode_mask=0` (never
match) because their PRM figures are confirmed pixel-duplicates of other types'
figures. Given that revision 2's "missed gray cell" hypothesis has now been
proven right once, those two figures are worth re-rendering at 400 DPI before
anything more exotic is attempted.

**Goal B is no longer gated on the decoder.** With a 632-instruction median run
on Digitakt, disassembling the code that indexes the candidate dispatch table at
`0x0034fc` is now a reasonable thing to attempt — which is precisely the test
that would confirm or kill the 11-entry coincidence.

### B.4 The SHARC load map is now exact, not inferred

`tools/sharcldr.py` had `FILL_BIT = 12`. In the ADI boot-stream flag set that
bit is `BFLAG_IGNORE`; `BFLAG_FILL` is bit 8 (`0x100`). A FILL block has no
payload in the stream, so treating one as if it did desynchronised the parse at
the fourth block. That is the whole reason 309,532 of 320,780 bytes were
recorded as "a format this tool does not decode".

With the right bit, **the entire image parses as one unbroken block chain**:

| | blocks | bytes consumed | entry point |
|---|---|---|---|
| Digitakt II 1.15C | 104 | 320,780 / 320,780 (100%) | `0x1c1338` |
| Digitone II 1.10E | 96 | 833,060 / 833,060 (100%) | `0x1c12e2` |

The final block in each carries `BFLAG_FINAL` with `byte_count` 0, and its
target address is the image entry point.

This replaces the fitted affine constants with a real map. The loader writes
payload to byte addresses in the `0x28xxxxxx` / `0x80xxxxxx` spaces, while the
core executes at short-word addresses in `0x1cxxxx` / `0x12xxxx`. The relation
is a width alias:

    byte_address = 2 * short_word_address + 0x28000000

Two independent confirmations, neither of them a fit: the declared entry point
`0x1c1338` maps to byte `0x28382670`, which is *exactly* a loaded block's
target address; and the base `0x28000000` is a round region base rather than a
derived residual. `tools/sharcldr.py` now exposes this as `sw_to_byte()`,
`byte_to_sw()`, `offset_for_address()`, `address_for_offset()` and a
`--addr` / `--addr-space` CLI.

A consequence worth noting: the float-vs-other region heuristic in the old
output is now obsolete for locating code. Block targets are ground truth.

See also docs/sharc/SPEC-FINDINGS.md §2 for the same boot stream in 1.16 and
1.11.

### B.5 The dispatch table is real, and it is an RPC dispatcher

The 11-entry table at file offset `0x0034fc` is no longer a lead. Immediately
after its last entry, at `0x3528`, the firmware carries its own label:

    000034fc: 3e3c 1c00 4f3c 1c00 722c 1c00 953c 1c00   (11 entries, 44 bytes)
    ...
    0000351c: 023e 1c00 c73e 1c00 163f 1c00 5250 4320   ....RPC
    0000352c: 6469 7370 6174 6368 6572 0000             dispatcher..

The ADI block header at `0x34ec` targets byte `0x282577c4` with `byte_count`
60 — exactly the 44-byte table plus the 16-byte label. `"RPC"` and
`"dispatch"` each occur exactly once in the whole 320,780-byte blob, both here.

**This reframes the finding, and not entirely in our favour.** It is a named
dispatch table, read directly out of the bytes rather than inferred — but it
dispatches *RPC messages*, not necessarily machines. The 11 = 5 machines + 6
filters arithmetic is still only arithmetic. There are no machine or filter
name strings anywhere in the SHARC blob to check it against, and Digitone's
blob contains no such label at all (its build appears to have stripped the
string), so the convention could not be cross-validated there.

Structure of the 11 targets, from disassembly:
- Entries 1 and 2 are genuine parallel twins: identical 12-instruction type
  sequence, 54 bytes each, differing at 6 of 54 bytes — all inside two embedded
  constants. Entries 4 and 9 share an opening prefix the same way.
- Entries 3–8 all terminate their linear decode at the same file offset with
  progressively shorter runs, i.e. they are successive entry points into one
  shared stream rather than six independent functions. No return instruction
  was found in that span, which our no-semantics decoder cannot resolve.
- Several entries converge on two common absolute jump targets, suggesting a
  shared engine routine.

That shape — thin per-entry stubs differing only in a constant, falling into a
common body — is what a table of similar-signature handlers looks like. It is
consistent with machines. It is equally consistent with eleven unrelated RPC
operations.

The runtime code that indexes the table was **not** found, and the obvious
mechanism is now ruled out rather than merely unsearched. A full
instruction-level scan — decoding every immediate-bearing instruction type
across 99.5% of Digitakt's loaded bytes and 99.8% of Digitone's, 41,341 and
85,257 absolute immediates respectively — finds the table's address nowhere.
Nor does any of the 11 handler addresses appear as an immediate, on either
device. The one tempting near-miss cluster turned out to be the handler
bodies loading local self-references.

So the table is reached through a pointer that is computed or fixed up at
load time, not one baked into an instruction. Recovering it needs data-flow,
not another address search.

### B.6 The ColdFire side of the seam, and one retraction

**Retraction first.** `0x40128c7c` is not the ColdFire→SHARC transport. It is
a blocking `usleep`, backed by DMA Timer 1 — Ghidra's own symbols name
`0xFC074000` `DTIM1_DTMR` and `0xFC074004` `DTIM1_DTRR`, and all seven of its
callers pass a bare microsecond count. `emu/dspboot.py`'s body already said
the argument was a timeout; its title said "transport", and the title is what
propagated.

The real path, and it matches `emu/dsp.py`'s model exactly:

```c
FUN_400cf4a8:  _DAT_8c00000a = 0x80;
               do { } while ((_DAT_8c000002 & 1) == 0);   // ready line
               _DAT_8c000002 = (ushort)param_1 << 8;
```

Above it, `FUN_400cfd40` takes a mutex, pushes one 4 KB page, then sleeps
100 µs — the same 100 µs `emu/dsp.py` records. Everything reaching the SHARC
goes through exactly three callers of it:

- `FUN_40146148` — bulk 4 KB page upload.
- `FUN_4014653c` / `FUN_401465a4` — build a 4 KB buffer and send it with
  `cmd = 0xFFFFFFFF`, the sentinel `emu/dsp.py` names.
- `FUN_4014666c` — worker task on mailbox `0x40588f44`: 32 KB chunks, stereo
  de-interleave into two page bases, re-queued until drained.

**That cluster is the sampler, not the parameter RPC.** What looked like an
opcode in the message header is a voice index — its callers walk arrays of
1024 slots. It fits the SHARC-side assert from
`lib/esp5-dsp/dsp-lib/sampler/digitakt_rompler_update.c`, and it means the
sample-streaming path is now understood end to end.

The parameter RPC is still unlocated. `Digisharc::rpcMsgHeader_t`,
`rpcMsgOpReq_t` and `rpcMsgPingRequest_t` exist as RTTI in both images, but
their typeinfo has no code xrefs — Ghidra never linked them to a vtable. And
since all SHARC traffic goes through those three functions and none of them
carries a parameter message, either parameter changes ride `FUN_4014653c`
with a different shape, or they use a path not yet found. That is a narrow
question, not an open hunt.

### B.7 SHARC is now in Ghidra

`tools/sharc_import.py` builds a Ghidra program from the loader's own block
targets. Three things the format does not advertise had to be handled: the
boot stream writes some regions more than once (so blocks are merged and
payloads replayed in stream order), a `byte_count` 0 block carries a
short-word execution address rather than a loader byte address, and the 32 MB
DDR clear has to be skipped rather than reserved.

Nothing in the processor module knows where code starts, so auto-analysis
alone disassembles **nothing**. Seeded at the entry point, the 11 handlers
and the call targets `tools/sharcscan.py` recovers, it reaches **289
functions** on Digitakt.

The SLEIGH generator also grew real control flow. It emitted p-code for 2 of
the 10 documented control-flow types; it now does 8a, 9a, 9b and 11a as well
— which is what took function recovery from 232 to 289. Two things there had
to be measured rather than assumed:

- **Which `cond` means "always"**: 31, dominant for 8a (48.8% / 47.2%) and 9a
  (83.7% / 44.8%) across both images. Except `11a`, which cannot encode 31 at
  all — its opcode fixes bit 32, the low bit of its own cond field, and
  `sleigh` rejects the constructor as an impossible pattern. Its
  unconditional form is 30, independently the measured dominant value there
  (87.5%). The encoding constraint and the histogram agree.
- **That `10a` must be excluded.** Its opcode mask constrains two bits, and
  its cond field is near-uniform in real firmware (top value 14.9%, the rest
  ~5% each) where genuine control-flow types spike hard at 31. Most 10a
  matches are not instructions; making them indirect jumps would have
  corrupted the analysis.

`11c`, the compact 16-bit return, is still excluded as `uncertain`. Only 40
of the 48-bit returns appear in 224 KB of code, so most returns are the form
the spec cannot see — which is why function bounds remain poor.

`tools/ghidraq.py` queries either processor's program (strings, symbols,
xrefs, callers, decompile, read), chaining queries through one JVM load.

**What this does and does not buy.** It buys a browsable SHARC corpus with a
real call graph, and the ability to prove a negative at 99.5% coverage
instead of guessing — which is how B.5's dead end got settled. It does not
resolve data references: the spec has no p-code for immediate loads or memory
loads, so Ghidra sees `call [I3]` and cannot say what `I3` holds. That single
gap is why the dispatch question is still open.

### B.8 Honest cost of Goal B

Even with a working decoder, adding a machine needs, in order: read the dispatch
table's indexing code; understand the per-machine algorithm's calling
convention, parameter-block layout and RPC contract; write SHARC+ code; fit it
into §7; and repack. The "unloaded gaps of unknown purpose" this section used
to cite are gone as an unknown, and not in our favour: with the FILL bit fixed,
all 320,780 bytes parse as one block chain — 319,116 of payload and 1,664 of
headers — so there is no slack region to drop new code into. Anything added has
to displace something or extend the image.
And there is **no SHARC emulator**, so every iteration is flash-and-listen on
real hardware, with no acceptance oracle of the kind Goal A enjoys.

Note the cost shape has changed since this list was written: the ColdFire
selection/descriptor layer (§B.1) is now solved, so a machine that reuses an
existing DSP mode needs no SHARC work at all — only a new ColdFire
descriptor entry and string. Only a genuinely new algorithm still needs the
SHARC-side work above.

Goal B is not architecturally closed. The hour of figure-reading is spent and it
paid; what remains is the substantial, genuinely hard work above, and the
flash-and-listen iteration loop is still the thing that makes it expensive.

---

## Recommended order

Goal A has had no work at all, and it gates everything: nothing this project
produces can reach hardware without it, including any machine change that
might eventually succeed. It is also the only part with no research risk.
Start there.

1. **Write the store-only aPLib packer.** The piece that sounds hardest and is
   about forty lines — tag bit `0`, literal byte, repeat, end code, no LZ77
   matching. Byte-exact packing was already established as unnecessary.
2. The three mechanical writers: per-section header, ELE3 table, 8-in-7
   encode. Each is the direct inverse of a reader in `dt2/container.py`.
3. **Build the round-trip gate early**: repack → re-extract with the device's
   own depacker → assert all five sections byte-identical. This is what makes
   flashing defensible, and it exists as a test before anything else needs to.
4. Done: the content checksum (traced at `0x80003ca6`, not `0x40003ca6` as
   earlier noted), the HMAC key derivation at `0x80005d90`, and the
   per-packet SysEx checksum are all recovered. Same method that produced
   the CRC-32 and depacker oracles byte-exact, twice.
5. Move the DT2-1.15C addresses into `devices/*.toml`, so Digitone can be
   patched at all.
6. Turn `patchimg.py`'s audit manifest into a patch format that can be read
   back, and add selection plus overlap checking.

Then, on the DSP side, in this order:

7. **Re-read the `11c` figure at 400 DPI.** Same defect class as the
   Type5b_move fix that worked. It repairs function bounds, which are poor
   today because most returns are invisible.
8. **Data semantics for the immediate-move and load types.** The largest
   single unlock left: it is what would let Ghidra resolve `call [I3]`, and
   therefore what answers B.5. Scope it deliberately — it is a substantial
   job, not an afternoon.
9. Locate the parameter RPC on the ColdFire side (B.6). Narrow, and it runs on
   the processor that is already readable.

And one standing caveat that none of the above removes: there is still no
SHARC emulator, so every DSP change is flash-and-listen on hardware, with no
acceptance oracle of the kind Goal A enjoys. That, not readability, is what
makes new machines expensive.
