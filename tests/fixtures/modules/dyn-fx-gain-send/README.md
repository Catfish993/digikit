# FX gain send, run time (test)

The same effect as `test/dyn-fx-gain` with `route = "send"` in its `[[fx]]`: it starts on the send route and may be switched to the master (`routes` defaults to `["send", "master"]`). Used by the scenario `tests/digikit/scenarios/fx-routes-dt2.toml`.
