# Handover 2026-09-20: building a custom machine on DT2 1.16

Machine-building track. **Complements, does not supersede,
`HANDOVER-2026-09-20-a2-sport-pcg.md`** (the A2 emulator/DSP-link track). Results
today are in `docs/FINDINGS.md`; this file carries state and next steps only.
Target is **DT2 1.16 only** (Em: stop using 1.15C entirely). MAIN OS
`out/sections/dt2-1.16/section_3_MAIN_OS.bin`, sha
`57bb4dfa8df07d846adc72fdb4fb0d3cd3c5680c524bf498338460207e008e7d`; SHARC
`out/sections/dt2-1.16/section_7_BLOB.bin`, sha `0f514a12...`.

## Goal

Clone an existing track machine and change how it manipulates sound, to add
functionality to DT2. Em is scoping *which* machine — leaning toward a
time-stretch variant, but DT2 already has a STRETCH machine (type 2), so the
open question is what a custom one should do that STRETCH doesn't. Em wants a
menu of feasible new-machine ideas.

## What this session established (detail in docs/FINDINGS.md)

- **A machine = a ColdFire-side parameter recipe on a uniform, parameter-driven
  SHARC engine.** The SHARC receives and caches the per-track machine type
  (`0x94+2i`) but has **no per-machine dispatch** — verified across the receive
  site, the fully-decoded per-track routine `0x1c24e9` (uniform clamp/param
  pipeline), 6/8 field readers, and a whole-image jump-table scan. Two residual
  two-way compares (`0x1cc225`, `0x1cc44e`/`0x1cc4cb`) can't select among 7
  machines. See FINDINGS "The SHARC machine-type consumer…", "The SHARC audio
  engine…", and the firming pass.
- **The audio synthesis engine's ingredients are located but its control flow is
  runtime-assembled** (task/callback entry, internal-memory tables, runtime
  buffer pointers). Output = two 2048 B transmit ping-pong DMA rings
  (`0x262138`/`0x262938`, `0x263138`/`0x263938`); synthesis tables incl. a
  quarter-wave cosine oscillator at `0x8055c440` (reader `FUN_1c71ec`).
- **Clone-and-patch tooling works** (`tools/machinepatch.py`, 9 parts, proven
  bootable in the emulator on 1.15C as a SLICE→type-7 clone). It patches a live
  snapshot only — **produces nothing flashable**. `tools/patchimg.py` +
  `tools/bootcheck.py` exist for static-image patching (Workstream B step 4, not
  yet done for a machine).
- **Workstream-D kickoff:** the generic param→mirror-index resolver is
  `FUN_400d9ed8` (table `0x4020f18c`, stride `0x3c`, index at +4). LEV chosen as
  the first parameter to trace; static path mapped (UI → `FUN_4002d7a4` → `0x8e`
  mirror `0x80003362+track*0x8e+idx*2` → dirty → vector-191 `FUN_400d90ac` →
  `0x80005b50+i*0x8e` → TX offset `0x74`). LEV's exact param_code is [O].
- **selache is a full open-source SHARC+ toolchain, not decoder-only**
  (corrects an in-session claim of "no assembler"): `selas` assembler, `selcc`
  C99 compiler, `selinstr` encoder+VISA, `seld` linker, `selload` boot-stream
  writer, targeting ADSP-21569 = DT2's family. GPLv3, independent of ADI (not
  the forbidden vendor toolchain). Clone at
  `<scratchpad>/selache` this session; upstream `https://github.com/js216/selache`.
  This reopens Tier 2 (new DSP code) as *possible*.

## The two tiers (decision frame)

- **Tier 1 — reparametrize/remap an existing engine (ColdFire-side).** No DSP
  code. Fastest, safest path to a *usable* custom machine. Reuses a stock
  engine (e.g. STRETCH for a stretch variant) with different SRC params, ranges,
  or a new engine input exposed. This is the recommended route for a first
  deliverable.
- **Tier 2 — new/modified DSP behavior via selache.** Newly *possible* (selache
  can emit SHARC+ code), but gated on hard problems selache does NOT solve:
  where to put code in Elektron's SHARC image, how to hook it (the engine has no
  dispatch seam and its render loop is unlocated/runtime-only = A2 territory),
  matching engine conventions, and verifying encodings + hardware risk.

## Validation reality (applies to both tiers)

- The **emulator cannot render audio or load samples**: eSDHC/eMMC is a
  zero-returning stub (FINDINGS:946), and the DSP audio path doesn't run
  naturally (A2). So a machine cannot be *heard* in the emulator.
- **Hearing/validating a machine = real hardware** (Em's 1.16 device), which
  needs a flashable static image (Workstream B step 4) and Em's bootstrap-install
  decision. The emulator is still useful for control-flow checks, frame
  inspection, and bounded boot-without-crash of a patched image.
- Every current `snapshots/` snapshot is 1.15C. A 1.16 ladder was built this
  session at `snapshots/dt2-1.16/boot{60..400}M.snap` (from
  `Digitakt_II_OS1.16.syx` in the repo root, via
  `DT2_SYX=Digitakt_II_OS1.16.syx DT2_SECTIONS=out/sections/dt2-1.16 uv run
  python -m emu.checkpoint make 60000000,120000000,200000000,280000000,400000000
  snapshots/dt2-1.16/boot`). These are **cold-boot with no pattern/sample
  loaded**, so the frame carries no live parameter data (the LEV A/B needs a
  content-loaded snapshot — see next steps).

## Next steps (prioritized)

1. **Scope the machine ideas (what Em asked for last).** Enumerate each of the 6
   machines' SRC-page parameters AND the full per-track TX-frame field set the
   SHARC reads, then find engine inputs no stock machine exposes = the "headroom"
   for genuinely new (Tier-1) machines. Deliverable: a ranked, feasibility-tagged
   menu ("reuse engine X, expose control Y, effort Z"). Start from the descriptor
   9-ID fields per machine (SLICE = `0xf8,0xf9,0,0xfb,0xfc,0xfd,0,0xfe,0x0a`),
   `SourcePageView` (`0x4001753a`, builds 3 rows), the frame map (FINDINGS ~1929),
   and the `0x4020f18c` param table. Also: characterize what STRETCH (type 2)
   actually does and its params, since it's the natural stretch-variant base.
2. **selache↔image round-trip feasibility probe (de-risks Tier 2, cheap).**
   `cargo build --release` selache; encode a handful of instructions already
   decoded from `section_7_BLOB` with `selinstr`/`selas` and confirm the bytes
   match the image (and decode our bytes with selache), to prove the encoder
   lines up with DT2's actual ISA/VISA. Doubles as a strong decode-table
   cross-check. Verify, don't trust — see [[selache-is-decoder-only]] memory
   (its tables' provenance means confirm against our decoder/the image).
3. **Finish the Workstream-D LEV trace (needs a content snapshot).** Produce a
   1.16 snapshot with track 0 carrying MANUAL SLICE + a sample (UI-drive via
   `emu/panelin.py`/`tools/uidrive.py`, or Em captures one), then run the poke
   A/B with `tools/sharcframe.py --poke 0x80005b50+2t=… --compare …` to measure
   param → `0x80005b50` → TX `0x74` (turns the doc-traced [D] into measured), and
   pin LEV's param_code/index. NB: on cold-boot the src-pointer cache
   `0x8000470c` stays zero (no live Sound object) — that's why the earlier A/B was
   a null; a content snapshot is the fix.
4. **For a real deliverable: make machinepatch.py emit a flashable static 1.16
   image** (Workstream B step 4): apply its plan via `tools/patchimg.py` to
   `section_3_MAIN_OS.bin`, validate with `bootcheck` + container roundtrip +
   bounded boot. Note the `clone` part's behavioral sites are wired only for
   SLICE (type 6); cloning STRETCH (type 2) needs its `type==2` sites found first.

## Blockers / open

- No content-loaded 1.16 snapshot (step 3). Emulator can't load samples (eMMC
  stub) or render audio (A2). Hardware validation needs Em's bootstrap decision.
- Tier 2 injection seam: the SHARC render loop is unlocated (runtime-only control
  flow) and the engine has no per-machine dispatch — the hard, open part.
- machinepatch.py `clone` part is SLICE-only; other clone bases need their
  `type==N` sites enumerated.

## Evidence rules (binding)

- Record results in `docs/FINDINGS.md` with [V]/[D]/[O]/[C]; a [V] needs a second
  agent's byte-check. Handovers carry state/next-steps only.
- Static SHARC reads parallelise (decode_at/sqlite/sharcflow are contention-free;
  the live Ghidra JVM `~/ghidra-projects/elektron-sharc` is the only serial
  resource) — see [[static-sharc-work-parallelises]].
- Firmware and derivatives are never committed (`*.syx`, `sections/`, `out/`,
  `snapshots/`). Commit only when Em asks.
- Emulator: 1.16 only; bound runs with `--limit`; verify image/snapshot sha
  before trusting a measurement (Em runs the emulator concurrently in this tree).

## Agents

- scout reads code (ask for quotes in its final message); coder applies fully
  specified edits (check `git diff`); general-purpose runs processes (emulator,
  Ghidra, selache build, tests). Static SHARC passes fan out well in parallel.

## Repo state

- Branch `a2-notification-invalidation`. Uncommitted this session: `docs/FINDINGS.md`
  (+ new sections: working-set RAM map, SHARC machine-type consumer, SHARC audio
  engine + firming pass, Workstream-D kickoff), `docs/MIDI-SYSEX-RPC.md` (§5/§9/§11
  corrections: Screenshot, `#MMCDUMP`, `#MRAM_DUMP` correction, closed gaps).
- Leave alone (foreign untracked): `Untitled.mmon`, `Untitled2.mmon`, `scratch/`,
  `docs/refs/dspi2-edma-blocker-and-register-sources.md`. Also new & git-ignored:
  `snapshots/dt2-1.16/`.
- No commits made this session (Em: no commits).

## New-session prompt

> Continue the DT2 1.16 custom-machine track from `HANDOVER-2026-09-20-machine-build.md`.
> The machine architecture is understood (ColdFire parameter recipe on a uniform
> SHARC engine, no per-machine dispatch); clone tooling works but emits nothing
> flashable; selache is a usable SHARC+ toolchain (reopens new-DSP-code as
> possible but not easy). Pick up at "Next steps": (1) build the ranked
> feasible-machine-ideas menu (enumerate all 6 machines' params + the frame-field
> headroom), (2) the selache↔image encoder round-trip probe, then (3) the
> content-snapshot LEV measurement and (4) the flashable-image path. Target 1.16
> only; the emulator can't render audio or load samples, so hearing a machine is
> a hardware step.
