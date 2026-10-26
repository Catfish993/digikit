# DN2 track FX audio oracles

`dn2-trackfx.ts` is the canonical source for all 47 stock DN2 1.12 scenarios. Run it with Node >=24 to print the scenario JSON document. `tools/emulator/coupled.py` accepts that source through `--scenarios`, evaluates it afresh, and atomically materializes content-addressed JSON under `out/scenarios/` for the native runner. Duration enumeration writes nothing. The full expanded objects (names, descriptions, seconds, exact-event flag and ordered events) were compared against the retired JSON before deletion: all 47 matched. Fixed instruction slots include the original zero-detent padding, routing window and filter prelude.

`dn2-trackfx.test.ts` owns the former trackFX assertions in `tests/scenarios/test_dn2_fx.py`; Python retains sendFX and DT2 oracles, descriptor/decode/shared-timeline units, and all NumPy signal analysis in `tools/emulator/fxcheck.py` and `tools/emulator/audiocheck.py`.

| Former Python test identity | Vitest identity (same prefix) | Quick | Additional sweep |
| --- | --- | ---: | ---: |
| `test_healthy[tf-*]` | `test_healthy tf-*` | 10 | 37 |
| `test_readback[tf-*]` | `test_readback tf-*` | 9 | 0 |
| `test_zero_is_dry[tf-*]` | `test_zero_is_dry tf-*` | 3 | 3 |
| `test_mono_stays_mono[tf-*]` | `test_mono_stays_mono tf-*` | 7 | 0 |
| `test_bit_reduction_error_rises[metric-fast/sweep]` | `test_bit_reduction_error_rises metric fast/sweep` | 2 | 2 |
| `test_overdrive_distortion_rises[metric-fast/sweep]` | `test_overdrive_distortion_rises metric fast/sweep` | 2 | 2 |
| `test_srr_rate_falls[fast/sweep]` | `test_srr_rate_falls rate fast/sweep` | 1 | 1 |
| Total | 79 distinct assertion identities | 34 | 45 |

Health retains finite samples, gate success, DC <1e-3, peak <=1 and no growth for every scenario. Readback retains exact changed-parameter membership and UI values. Zero and mono checks use `np.array_equal` observations, including reference shape equality, rather than rounded audio metrics or WAV metadata. BR/OVER retain both strictly increasing THD and dry-difference metrics; SRR retains strictly decreasing effective rate. Series remain ordered by unsigned readback values, distinct settings and finite metrics. Undefined/nonfinite analysis measurements become JSON null only at the interchange boundary and cannot pass the finite-metric assertion.

```sh
mise run test:fx                                          # 34 quick identities
DIGI_FX_SWEEP=1 mise run test:fx                           # all 79, all 47 scenarios
mise run test:fx -- -t 'test_healthy tf-br-127'             # selected scenario + dry reference
DIGI_FX_NO_AOT_SUBSET=1 mise run test:fx                    # 8 former trackFX no-AOT identities
```

These commands never build. They require the existing stock DN2 coupled-run binary, current generated core, DN2 1.12 ready firmware/DSP/CF inputs and MAIN OS descriptor image. Preflight fails on missing/stale inputs; `--no-build` establishes binary existence, not binary freshness. The caller must establish that freshness. Vitest writes its already imported document to the owned run directory as `scenarios.json`. Two existing Python CLI commands perform rendering/cache/sharding and NumPy analysis; there is no separate preflight process or Python test runner. The renderer uses `--check-generated` to reject a stale core before rendering, and the TypeScript fixture checks the descriptor image exists. The analysis command reads canonical event metadata through the source resolver. One Vitest worker invokes the existing Python coupled CLI with explicit `DIGI_JOBS` (default 1), retaining its shared benchlock, independent ready-snapshot restore per scenario, content render cache and budgeted sharding. Selected native Vitest tests declare their needed scenarios; one batch adds dry references and routing companions. Filtering does not render all 47. No daemon pooling or reset contract is introduced.

Runs use `out/regression/trackfx/<timestamp>-<pid>/`. Reports, decoded measurements and command logs survive success/failure; WAVs are pruned after the suite and remain available through the existing render cache. Owned subprocess groups are bounded and killed on timeout, failure or cancellation; failures identify the command log. Python remains the renderer and numerical analysis owner.

Normal verify `check` includes the 34 quick identities alongside the remaining Python sendFX tests. `--full` includes all 79 trackFX identities: this deliberately closes the old full-gate omission of the Python sweep assertions (the old tier-sweep compared PCM without running those assertions). Default `check-no-aot` retains exactly 8 trackFX identities even in full verification: health for dry/br127/over127/srr127, readback for br127/over127/srr127, and the quick SRR series. `--all-no-aot` selects 34 normally or 79 with full verification. The same FX file membership and fixed no-AOT PCM subset feed tier checks through the TS resolver. Scenario source/helper/config edits select host, audio oracle and tier checks before the broad regression host route; conservative smoke remains.

Migration validation includes exact source equivalence, native collection counts (34/79/8 and one filtered case), strict regression types and focused Python tooling/routing checks. Coordinator acceptance passed all 79 assertions across all 47 scenarios with two render workers and no builds: the initial render had 0 cache hits and 47 renders (240.59 s); the final implementation had 47 cache hits and 0 renders (14.24 s). Artifacts are in `out/regression/trackfx/1792982548335-13715/` and `out/regression/trackfx/1792982886830-19350/`. These are observed runs, not a controlled speedup comparison; no performance improvement is claimed.

Final focused checks passed 492 Python tooling/routing tests (18.14 s), regression TypeScript types, six Python owner type checks, scoped lint/format and documentation pathcheck (zero stale references). The final host suite passed 126 tests in 20 files (4.06 s) with authorized local socket access; its sandbox attempt hit Unix-socket EPERM. Read-only verify explain was inspected through the existing virtualenv after the mise/uv wrapper hit a sandbox cache-access error. No broad gate, build, hardware or no-AOT device acceptance ran.
