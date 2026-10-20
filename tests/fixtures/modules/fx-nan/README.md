# FX NaN (test)

A negative fixture of the FX contract: its output holds a NaN. `core/fx` plays the block dry and counts a fault, and the FX conformance suite
(`tests/modules/test_fx_conformance.py`) must report this module as failing (`pk_conform.FX_NEGATIVE`).
