# fx/master-drive tests

The Phase 2 harness step format has not landed in this tree, so the module's tests are pytest files under `tests/`:

- `tests/patchkit/test_master_drive.py`: the DSP routine on the concrete SHARC+ core against a float32 reference (fast, needs
  Selache).
- `tests/patchkit/test_master_drive_emu.py`: the full chain in the emulator on DT2 1.17 and DN2 1.12 through
  `tools/pk_proof_drive.py` (slow; skips without `DIGI_EMUD`, the ready inputs `out/ready-md-dt2|dn2` and the stock images).
