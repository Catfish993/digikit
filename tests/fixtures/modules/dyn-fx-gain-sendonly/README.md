# FX gain sendonly, run time (test)

The same effect as `test/dyn-fx-gain` with `route = "send"`, `routes = ["send"]` in its `DK_FX` record (`src/module.c`): it runs on the send route only (the FX ROUTES page says `SEND ONLY`). Used by the scenario `tests/digikit/scenarios/fx-routes-dt2.toml`.
