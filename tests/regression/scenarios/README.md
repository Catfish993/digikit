# DN2 FX audio oracles

`dn2-trackfx.ts` and `dn2-sendfx.ts` are the canonical sources for all 47 trackFX and 87 sendFX stock DN2 1.12 scenarios. Run it with Node >=24 to print the scenario JSON document. `tools/emulator/coupled.py` accepts that source through `--scenarios`, evaluates it afresh, and atomically materializes content-addressed JSON under `out/scenarios/` for the native runner. Duration enumeration writes nothing. The full expanded objects (names, descriptions, seconds, exact-event flag and ordered events) were compared against the retired JSON before deletion: all 47 trackFX and 87 sendFX cases matched. Fixed instruction slots include the original zero-detent padding, routing window and filter prelude.

`dn2-trackfx.test.ts` and `dn2-sendfx.test.ts` own the former DN2 Python assertions; DT2 assertions now live in `dt2-fx.test.ts`; Python retains descriptor/decode/shared-timeline units, and all NumPy signal analysis in `tools/emulator/fxcheck.py` and `tools/emulator/audiocheck.py`.

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

| Former sendFX Python identity | Vitest identity | Quick | Additional sweep |
| --- | --- | ---: | ---: |
| `test_healthy[sf-*]` | `test_healthy sf-*` | 11 | 76 |
| `test_readback[sf-*]` | `test_readback sf-*` | 8 | 0 |
| `test_zero_is_dry[sf-*]` | `test_zero_is_dry sf-*` | 0 | 3 |
| `test_mono_stays_mono[sf-del-ref]` | `test_mono_stays_mono sf-del-ref` | 1 | 0 |
| `test_delay_spacing[fast/sweep]` | `test_delay_spacing fast/sweep` | 1 | 1 |
| `test_delay_feedback_zero_one_echo` | same | 1 | 0 |
| `test_delay_default_feedback_minus_6_db` | same | 1 | 0 |
| `test_delay_wid_min_is_left` | same | 1 | 0 |
| `test_delay_wid_max_is_right` | same | 0 | 1 |
| `test_delay_feedback_above_unity_swells` | same | 0 | 1 |
| `test_reverb_rt60_rises_with_decay[fast/sweep]` | `test_reverb_rt60_rises_with_decay fast/sweep` | 1 | 1 |
| `test_chorus_rate_rises_with_speed[fast/sweep]` | `test_chorus_rate_rises_with_speed fast/sweep` | 1 | 1 |
| `test_chorus_decorrelates_with_width[fast/sweep]` | `test_chorus_decorrelates_with_width fast/sweep` | 1 | 1 |
| SendFX total | 112 identities | 27 | 85 |
| Combined FX total | 191 identities, 134 scenarios | 61 | 130 |

SendFX preserves delay spacing within 2 ms, one echo at zero feedback, default feedback within 1 dB of -6 dB/repeat, left/right width separation (40/30 dB), positive repeat growth above unity, rising reverb RT60 and chorus rate, and falling chorus correlation. Readback uses unsigned stored values. Every sendFX scenario retains generic health: feedback-above-unity exceptions remain `sf-del-fb-u44`, `sf-del-fb-u127`, `sf-rev-decay-u127`; the sparse `sf-del-time-u127` echo retains its peak <=1.5 times dry exception. Master compression has generic health coverage only. No new compressor behavior is asserted.

Health retains finite samples, gate success, DC <1e-3, peak <=1 and no growth except the sendFX exceptions above. Readback retains exact changed-parameter membership and UI values. Zero and mono checks use `np.array_equal` observations, including reference shape equality, rather than rounded audio metrics or WAV metadata. BR/OVER retain both strictly increasing THD and dry-difference metrics; SRR retains strictly decreasing effective rate. Series remain ordered by unsigned readback values, distinct settings and finite metrics. Undefined/nonfinite analysis measurements become JSON null only at the interchange boundary and cannot pass the finite-metric assertion.

```sh
mise run test:fx                                          # 61 quick identities
DIGI_FX_SWEEP=1 mise run test:fx                           # all 191, all 134 scenarios
mise run test:fx -- -t 'test_healthy tf-br-127'             # selected scenario + dry reference
DIGI_FX_NO_AOT_SUBSET=1 mise run test:fx                    # 17 former DN2 no-AOT identities
```

These commands never build. They require the existing stock DN2 coupled-run binary, current generated core, DN2 1.12 ready firmware/DSP/CF inputs and MAIN OS descriptor image. Preflight fails on missing/stale inputs; `--no-build` establishes binary existence, not binary freshness. The caller must establish that freshness. Vitest writes its already imported document to the owned run directory as `scenarios.json`. Two existing Python CLI commands perform rendering/cache/sharding and NumPy analysis; there is no separate preflight process or Python test runner. The renderer uses `--check-generated` to reject a stale core before rendering, and the TypeScript fixture checks the descriptor image exists. The analysis command reads canonical event metadata through the source resolver. One Vitest worker invokes the existing Python coupled CLI with explicit `DIGI_JOBS` (default 1), retaining its shared benchlock, independent ready-snapshot restore per scenario, content render cache and budgeted sharding. Selected native Vitest tests declare their needed scenarios; one batch adds dry references and routing companions. Filtering renders only that closure. No daemon pooling or reset contract is introduced.

Runs use `out/regression/{trackfx,sendfx}/<timestamp>-<pid>/`. Reports, decoded measurements and command logs survive success/failure; WAVs are pruned after the suite and remain available through the existing render cache. Owned subprocess groups are bounded and killed on timeout, failure or cancellation; failures identify the command log. Python remains the renderer and numerical analysis owner. `DIGI_FX_KEEP_RUNS=1` retains WAVs for the existing clock-sweep tool, which harvests both family directories and runs the native quick assertions with its render job budget.

Normal verify `check` runs all 61 quick identities. Full verification runs all 191: this deliberately closes the old full-gate omission of Python sweep assertions. Default `check-no-aot` retains exactly 17 identities even in full verification: trackFX health for dry/br127/over127/srr127, readback for br127/over127/srr127 and the quick SRR series; sendFX health/readback for delay-time-u20, reverb-decay-u20 and chorus-speed-u20, default delay feedback, and quick reverb RT60/chorus speed series. `--all-no-aot` selects 61 normally or 191 with full verification. Tier PCM comparisons use the same source files and fixed scenario subset through the existing resolver. Source/helper/config edits select host, audio oracle and tier checks before the broad regression host route; conservative smoke remains. DT2 stays on its existing Python task.

SendFX migration validation: the one-off comparison matched all 87 expanded objects exactly; old pytest collection matched all 27 quick and 112 full native assertion identities. Combined native collection is 61 quick / 191 full / 17 default no-AOT; a name filter collects one assertion. Strict regression types, six scoped Python owner type checks, focused Python tooling/routing, lint/format and path checks passed. The focused run passed 495 retained tests plus two temporary clock-migration checks (497 total); the temporary checks were removed after use. A later focused routing rerun passed 11 tests, including the DT2-only fixture owner route. Scoped pathcheck reports zero stale references. **Migration acceptance is blocked by the chorus-width sweep oracle.** The full sendFX run rendered all 87 scenarios and finished with 111 passed / 1 failed in 572.49 s (`out/regression/sendfx/1792983951037-39457/`). The combined warm run finished with 190 passed / 1 same failure in 33.18 s, including all 79 trackFX assertions passing with the shared fixture (`out/regression/sendfx/1792984608380-49634/`, `out/regression/trackfx/1792984627203-49977/`). These are observed runs, not a paired performance comparison.

The failing identity is `test_chorus_decorrelates_with_width sweep`: unsigned width readback `0, 10752, 27392, 32512` yields wet correlation `1.0, 0.372, -0.469, -0.452`; the final increase violates strict decrease. The original Python assertion, extracted unchanged and replayed on these same measurements, fails identically. Direct cached-WAV NumPy recomputation matches the JSON values and original rounding, while the recorded run's complete scenario document matches the captured original JSON exactly. This demonstrates faithful migration of the assertion; it does not establish a historical DSP regression or that the oracle always failed. An audited DN2 1.11 historical run passed, whereas current DN2 1.12 chorus PCM/readback differs. Whether the current oracle or DSP behavior requires correction remains unresolved. Assertions, thresholds and selection remain unchanged; no skip was added.

Filtered native acceptance passed one assertion with 111 skipped in 13.61 s (`out/regression/sendfx/1792984685019-50861/`). The existing clock parser reports 191 executed / 1 failure for combined JUnit and 1 executed / 0 failures for filtered JUnit, excluding skipped cases. With `DIGI_FX_KEEP_RUNS=1`, the filtered closure contains only `sf-del-ref` and `sf-dry-tap`; both WAVs remain, and the existing clock harvester finds the report using its actual 132000000 `cf_ips` and timestamp guard. The coordinator then rebuilt stock DN2 coupled-run with two Cargo jobs under the build lock (40.99 s). A fresh-binary rerender of the five-case chorus-width closure had zero cache hits and failed identically in 29.46 s (`out/regression/sendfx/1792984943484-54800/`); all five PCM hashes matched the earlier full run. The same bounded closure using `verify.no_aot_env()` had zero cache hits and failed identically in 102.33 s (`out/regression/sendfx/1792985036334-56163/`). All five AOT/no-AOT PCM hashes match exactly and all gates pass. These checks rule out a stale executable and an AOT-specific cause for this observed failure; the general emulator-versus-oracle assumption remains unresolved, and the historical DN2 1.11 run is not a matched baseline. Assertions remain unchanged. No real clock sweep, full branch gate, full no-AOT suite or hardware validation ran. Next: resolve the chorus oracle/DSP question before calling the migration fully accepted.

The preceding trackFX slice passed coordinator acceptance for all 79 assertions across 47 scenarios using two render workers without builds. Its retained artifact directories are `out/regression/trackfx/1792982548335-13715/` and `out/regression/trackfx/1792982886830-19350/`; that acceptance predates the sendFX migration and shared-helper change.


DT2 uses canonical `dt2-trackfx.ts` and `dt2-sendfx.ts` sources with `dt2-fx.test.ts`:
`mise run test:fx:dt2` selects 135 quick assertions, and `DIGI_FX_SWEEP=1` selects all 146 across
60 scenarios. Full lane acceptance passed in 420.48 seconds with 60 renders and zero cache hits;
all expanded inputs matched the retired JSON exactly. The native fixture retains independent ready
restores, reference closures, NumPy analysis and strict assertions. The known DN2 chorus-width
failure above is unchanged. Integrated filtered routing acceptance passed in 7.90 seconds with five scenarios (145 assertions
skipped). The broad branch gate remains separate;
no speed improvement is claimed.
