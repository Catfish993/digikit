# Handover 2026-09-20: close A2 through the SPORT4A and PCG-C values

Root handover. Supersedes `HANDOVER-2026-09-19-a2-sharc-dma.md` (deleted) and
`HANDOVER-2026-09-16-machine-to-dsp.md`. Results live in `docs/FINDINGS.md`;
this file carries state, constraints and the next move only.

## Goal

A2 is the gate that unblocks A3-A5 and then the machine work in workstreams B
and D, which is the point of the project. A2 needs a real-panel machine commit
to propagate end to end on DT2 1.16. Both halves are proved; the missing join
is natural vector-191 consumption, which needs the SSI0 receive cadence to
fire on its own. That is the only reason the SHARC peer matters.

Three open values block it (roadmap section 3):

1. the SPORT4A control value: direction, `SLEN`, packing, bit order;
2. the PCG-C divisors and source frequency;
3. the producer of the ColdFire `0x007fffff` marker.

All three are one question: **what does the SHARC audio init write to SPORT4A
(`0x31002400`) and PCG-C (`0x310ca300..0x310ca314`)?**

## Next move

Recover those register values and their provenance. Do not push the strict
SHARC run's frontier, and do not take on decode-table work this question does
not need.

1. Find the writer of the SPORT4A control register. No instruction or data
   word in the image holds an absolute SPORT4A or DMA10 address (two agents,
   whole image), so the driver reaches them only through the object chain
   `P -> L1 = DM(P+0x14) -> L2 = DM(L1+0x14) -> DM(L2) = SPORT base ->
   L3 = DM(L2+0x14) -> DM(L3) = DMA base`, with record `0x0a` at DM `0x26968c`
   pairing SPORT4A with DMA10. Start from the SPORT setup at
   `0x1ca6a6..0x1ca6f9` and the generic driver it calls.
2. Same for PCG-C, whose driver is the family near `0xb8b706`, reached by
   direct calls from the caller family `0x1c7524..0x1c7b9e`.
3. For each value, state its provenance: an immediate, a word in a config
   table (give the DM address and bytes), or computed from runtime state. A
   literal in the image is firmware-backed wherever the slice starts; a value
   computed from runtime state means the static route is closed and the
   emulator is the answer. Either outcome is worth having.
4. Only then decode the bits: `SLEN` and justification against the marker, the
   divisors against the `bit clock / 192` relation.

A direct-entry slice is acceptable here and must still be marked
`qualifying: false`. What stays forbidden is a guessed clock or a calibrated
marker standing in for a firmware value.

## Evidence rules that remain binding

- Qualifying runs use real firmware paths, immutable host-replayed panel
  input, `--exact --no-unblock`, no `--weakptr`, no generic semaphore bypass.
- Vector 191 must be forced by the guest vector-170 ISR. Never inject it.
- Direct calls, direct entry, host pokes, seeded registers, patched vectors
  and guessed clocks are calibration. Mark them `qualifying: false`.
- Use exactly `out/sections/dt2-1.16/section_7_BLOB.bin` for 1.16 SHARC work,
  SHA-256 `0f514a12a2255f5c081e292c47f1f29462003177658da4bbae0a22fd737fffa2`.
  `sections/section_7_BLOB.bin` is 1.15C and is not interchangeable.
- Loader-backed `LoadedMemory.from_stream(...)` plus `sharc_trace.decode_at`
  is authoritative. SQLite is candidate discovery only.
- Do not infer `0x007fffff` from `0x7fffffff` without the serial path.
- One checkout writer. Read-only agents may run in parallel. Serialize live
  emulator and Ghidra work.
- A finding needs a second agent's byte check before `[V]`.

## Repository state

- Branch `a2-notification-invalidation`, HEAD `30f5cbf`.
- Today's commits: `58b0e66` VISA width, returns and flags; `15f70bc` those
  findings; `cef0798` Type 7a modify, ACONV, Type 14d and Type 15a;
  `30f5cbf` provisional forms and the Type 7 findings.
- `uv run --with pytest python -m pytest tests -q` gives 472 passed,
  5 skipped, 149 subtests. `tools/sharcpcode.py compare` shows no regressions
  and all three images decode every aligned instruction.
- Untracked, leave alone: `docs/refs/dspi2-edma-blocker-and-register-sources.md`,
  `scratch/`.
- Commit only when Em asks. Never commit firmware or anything derived from it.

## What the tracer now models, so it is not re-derived

`docs/FINDINGS.md` sections "Tracer decode and call-model corrections move
strict startup to `0x1c1460`" and "Type 7 corrections carry strict startup
into the DAI setup" hold the evidence. In short:

- A VISA word with first byte `0x01` and frame bit 39 set is a 32-bit
  unconditional compute (`Type2a_short`), not a 48-bit Type2a. This was the
  cause of most "undocumented" words and of the old stops at `0xb893fb`,
  `0xb87838`, `0xb8b0df`, `0xb8c5e8` and `0x1c0efa`. Those boundaries are gone.
- A delayed call returns after its second delay slot, not at call + 7. The old
  rule landed inside the return-address literal and invented two undocumented
  words at `0xb89376` and `0xb868c0`.
- Type 9 indirect branches use DAG2, so the return idiom is
  `JUMP (M14, I12)`, not I4/M6. The tracer checks every such return against
  its call site; that check found two of the bugs above.
- Type3a, Type6a and Type7a scale their modifier by four under 32-bit normal
  words. Type7a's M selector is at bits 29-27.
- Type7d is ACONV; the strict run executes the byte-word round trip at
  `0x1c1460..0x1c1478`.
- ASTATX flags follow the PRM instruction pages, LT/GE/LE/GT and the
  SV/SZ/AC/MV/MS conditions resolve, and identical states merge.
- `--allow-provisional-form NAME` executes a form the table marks unconfirmed
  and records it on each state that used one. Such a run is calibration.

## Current boundaries

- Strict run from `0x1c1338`: two states, `0x1c144c` after 7,545 instructions
  and `0xb8cdaf` after 7,566.
- `0x1c144c` is not a tracer gap. Startup probes two boot sources; the second
  reads `DM(0x10000000)`, which no section of the firmware populates, then
  jumps through the value it read. The target is hardware state. A natural
  SHARC run from reset cannot pass this statically, so the old plan of running
  the peer from real entry does not work as written.
- With `14d` allowed, so as calibration only, the run reaches 11,597
  instructions and writes the DAI and PADS setup at `0x1cb28e..0x1cb323`,
  reproducing the 34 stores already recorded from a direct entry. It reaches
  no SPORT4A, DMA10 or PCG register, and ends on the state budget, on a
  floating-point compare (ALU `0x8a`) and on the boot probe.
- Open decision for Em: Type14d has 75 instances and stays unconfirmed. Its
  PRM figure is right, but nothing independent pins its seven fixed bits and
  the public Selache decoder models the form wrongly. Promoting it on firmware
  counts alone would set the precedent for every SHARC+-only form (3d, 4d,
  12a-ureg, 22a, 25a-rframe, 26a). Until Em rules, the flag above covers it.

## Useful commands

```bash
# Tests
uv run --with pytest python -m pytest tests -q

# Strict SHARC run (qualifying shape)
uv run python tools/sharc_trace.py \
  out/sections/dt2-1.16/section_7_BLOB.bin --blob --start 0x1c1338 \
  --concrete-memory --follow-loaded-calls --assume-32bit-normal-words \
  --core-reset-state --max-steps 100000 --max-states 1024 --summary

# The same as calibration, naming an unconfirmed form
#   ... --allow-provisional-form 14d

# Interface and descriptor probe
uv run python tools/sharc_interface_probe.py \
  out/sections/dt2-1.16/section_7_BLOB.bin

# Exact decode
uv run python - <<'PY'
import sys
sys.path.insert(0, "tools")
from sharcldr import LoadedMemory
import sharc_trace as trace
memory = LoadedMemory.from_stream(
    open("out/sections/dt2-1.16/section_7_BLOB.bin", "rb").read())
for pc in (0x1ca6a6, 0x1ca6d6, 0x1ca7e4, 0xb8b706):
    d = trace.decode_at(memory, None, pc)
    print(hex(pc), d.type_name, d.fields)
PY

# Candidate discovery only; recheck every hit with decode_at
sqlite3 -header -column out/sharcpcode/t34/dt2-1.16.sqlite \
  "select printf('0x%x',sw), mnemonic, raw, flow from insn
   where sw between 0x1ca6a6 and 0x1ca700 order by sw;"
```

## Address crib sheet

```text
SHARC entry                 0x1c1338
strict stops                0x1c144c (boot probe), 0xb8cdaf (Type14d)
DAI/PADS setup              0x1cb28b, stores 0x1cb28e..0x1cb323
SPORT/DMA object setup      0x1ca58a, 0x1ca6a6..0x1ca6f9, base store 0x1ca6d6
descriptor submit           0x1ca7e4, callers 0x1c7971/0x1c79f8/0x1c7af9/0x1c7b9e
PCG driver family           0xb8b706
caller family               0x1c7524..0x1c7b9e
marker store / selector     0x1c7586 / DM 0x25f780
machine reader              0x1c76e5, 0x1c771e -> 0x1c2b24, load 0x1c33d2
SPORT record 0x0a           DM 0x26968c, DMA base field at 0x2696a0
descriptor heads            0x2620c8, 0x262100, 0x264138, 0x264170
large buffers               0x262138, 0x262938
SPORT4A / DMA10 / PCG C     0x31002400 / 0x31023000 / 0x310ca300..0x310ca314

ColdFire SSI0 row           0x80003cd0 + 0x9a*track
ColdFire cache              0x8000470c + 4*track
ColdFire TX machine word    0x94 + 2*track
ColdFire DSPI2 candidate    0x82a00000..0x82a001c8
```

## Gotchas

- The aligned-decode percentage cannot see a wrong instruction width, because
  the sweep follows the decoder's own lengths. Test a suspected width by
  whether the next word decodes at each candidate width.
- The public Selache decoder is a cross-check only: no simulator, it cannot
  read our loader streams, and some of its tables come from watching a vendor
  toolchain. Never name that toolchain in this repo.
- Ghidra's tables miss code outside functions; confirm "no caller" and
  "no writer" claims with raw-image tools.
- One JVM per Ghidra project.
- `tools/sharcspec/ghidra/gen_sleigh.py` keeps a hand-maintained list of
  crossing forms. A decode-table change can make an entry contradict itself
  and stop the language compiling, which fails `test_sharc_pcode`.
- The shell is zsh: an unquoted `$VAR` is one word, and there is no `timeout`.
- Manuals: read `out/refs/<pdf stem>/`, not the PDFs.

## Key files

- Findings: `docs/FINDINGS.md`; spec notes: `docs/sharc/SPEC-FINDINGS.md`
- Roadmap: `digikit-re-roadmap.md`; boundary plan:
  `docs/plans/EMULATOR-SHARC-BOUNDARY.md`
- Tracer: `tools/sharc_trace.py`; probe: `tools/sharc_interface_probe.py`
- Decode table: `tools/sharcspec/` (`build_table.py` generates
  `decode_table.json`; `ghidra/gen_sleigh.py` generates the language)
- Interface artifacts: `out/experiments/sharc-interface-reader/`
- Qualified ColdFire front half:
  `out/experiments/a2-notification-invalidation/qualify-001/report.json`

## New-session prompt

> Continue A2 on branch `a2-notification-invalidation` from `30f5cbf`. The
> ColdFire halves are proved and the join needs a natural SSI0 receive
> cadence, so the gate now rests on three values: the SPORT4A control word,
> the PCG-C divisors and source clock, and the producer of the ColdFire
> `0x007fffff` marker. Recover them by following the SPORT/DMA object chain to
> the generic driver that writes `0x31002400` and the PCG driver near
> `0xb8b706`, and report each value's provenance: immediate, config table with
> its bytes, or computed from runtime state. Do not push the strict SHARC
> frontier and do not extend the decode table unless this question needs it. A
> direct-entry slice is calibration and must say so; a guessed clock or a
> calibrated marker is not evidence.
