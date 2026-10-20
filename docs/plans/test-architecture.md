# Test architecture: four levels, native module tests, TypeScript (plan, 2026-10-21)

One PR per item, branch `<area>/<topic>`. Related plans (outside the repo, in ~/src/digi):
TEST-SELECTION-PLAN-2026-10-20.md (routing; this plan replaces its Phases 3 and 4 and Phase 2's representative set and
`module:test`), WORKBENCH-AND-MODULES-PLAN-2026-10-19.md (P3d FX contract carries T2).

## Status at end of session
- 2026-10-21: plan written. No code changed. Order: TEST-SELECTION Phase 2 PR 1 (gate routing) first, then the modules
  plan; T1 when module work needs it, T2 with P3d.

## Goal

Each test runs at the cheapest level that answers its question. Module tests, including sound, run in seconds on the
native SHARC engine. Tests that do not listen to audio do not run the DSP. Long term, tests and non-RE tools are
TypeScript (vitest); Python stays for reverse engineering (tools/sharc_dev, tools/coldfire, Ghidra, firmware analysis).

## Decisions (Em, 2026-10-21)

1. Local only. No CI for now.
2. Four test levels (below). Split unit and integration tests.
3. TypeScript and vitest for tests and non-RE tools, long term. Migrate by replacement, not by a bulk port: new levels
   are written in TS; a Python test is deleted when its TS replacement checks the same oracle. No new Python test code
   outside the RE tooling.
4. Little change for the sake of tests: do this work where module or plan work needs it.
5. The Python SHARC core stays the reference for the instruction semantics and generates the Rust core.
6. PCM pins (`tests/scenarios/*.json`, module pins) change only with Em's approval.

## The four levels

| Level | Question | Runs on | Speed |
|---|---|---|---|
| unit | Is the logic right (packing, manifests, routing, tables)? | Rust and TS (Python until moved), no emulator | ms to s |
| module | Is the machine or FX right, and does it sound right? | the module routine alone, through its contract, on the native SHARC engine | s |
| device-cf | Does the firmware handle it (menus, parameters, save and load, upload, type numbers, memory audit)? | ColdFire emulator, DSP stubbed | about 1.1x real time |
| device-coupled | Does it sound right inside the device (stock filter, envelope, FX, master; the hook glue)? | both CPUs | 0.21-0.24x real time |

Each module type keeps one coupled test per device. Everything else moves down a level where it can.

## Evidence (2026-10-21)

- Replay of the last 25 merged PRs (static, verify's routing against the import graph): 12 touched verify.py or
  verify_route.py and ran everything (about 1,350 file-seconds); 5 Rust or `core/*` PRs expand to tests/patchkit and
  tests/digikit (860-1,335 file-seconds even with the import graph); 8 Python-only PRs drop from 100-1,270 to 3-92.
- The Python SHARC core runs every isolated routine test (MachineRig, dsp_voice_rig, pk_sharc): test_pluck 322 s,
  test_dsp_voice 99, test_patchkit (part) 73, test_machine_conformance 65, test_sine 60, the pluck tune and range files 67,
  others about 15 (medians). About 700 of the 1,350 s of a full python-tests run, and every `module:check`.
- The smoke step costs about 380 s on 4 cores on every non-docs change (17 cells).
- Coupled runs 0.21-0.24 emulated s per wall s; ColdFire-only 1.1 (finding 39). `digi-emud --dsp zero` exists; its
  cost and whether the ColdFire side waits on DSP replies are not measured.

## Items

### T1. Native call harness for module routines (0.5-1 day; payoff high, confidence medium)
- [ ] Scout: can the Rust SHARC engine run one routine outside the coupled loop? Does it have a store hook (needed by
      the P4a write-set check)?
- [ ] A Rust CLI or daemon with a plain request format: load the module objects, set the registers, call `note_on` and
      `render` for N blocks, return the output, cycle counts and recorded stores. Language neutral: Python calls it now,
      TS later. No Python dependency added.
- [ ] MachineRig and dsp_voice_rig use it; the tests stay unchanged. A cross-check test runs a few cases on both engines
      and compares the outputs.
- [ ] Before/after: the files listed in Evidence.

### T2. Module test kit (with P3d; 1-2 days; confidence medium-high)
- [ ] TS/vitest harness over T1, for machines (`render`, `note_on`, `desc`) and FX (`dk_fx_process`).
- [ ] Sound checks a module declares in its `tests/*.toml`: pitch against note, decay time, RMS, DC, parameter response
      (direction per parameter), a pinned PCM hash.
- [ ] `module:test NAME`: build check, conformance, sound checks, the smoke cells and quick scenarios that name it.
- [ ] Retire the Python conformance suite and the pluck sweeps when the TS kit checks the same pins.

### T3. DSP stub for tests that do not listen (1 hour to measure, then about 1 day)
- [ ] Measure `--dsp zero`: cost, and whether the ColdFire side needs DSP replies that a stub must answer. Stop if it does
      not pay.
- [ ] Move to ColdFire-only: menu, registry, upload, round trip, safe mode, cut, the table half of equiv. Only PCM compares
      stay coupled.

### T4. Device levels in TS (several days; by replacement)
- [ ] vitest projects `unit`, `module`, `device-cf`, `device-coupled`; device tests drive `digi-emud` through
      packages/control-client.
- [ ] Each landed TS level deletes its Python counterpart (pk_harness cells, digikit scenario runners).

### T5. Smoke and scenario selection by module (0.5-1 day)
- [ ] Filter the smoke matrix and the quick scenarios to the cells that contain a changed module.
- [ ] `core/*` and runtime changes run a representative set (one cell and one quick scenario per module type per
      device); the full sets run on request before a release.

### T6. verify.py (last; about 1 day)
- [ ] Python routing reduced to what vitest's related-test selection does not cover; keep the CPU budget.

## Rules

- One PR per item; design agreed with Em in chat before code.
- Two test systems run side by side only per level, and only until that level's replacement passes: one Python file
  deleted per TS file that replaces it.
- Each item reports before/after (files, file-seconds, gate wall time) against the recorded runs.
