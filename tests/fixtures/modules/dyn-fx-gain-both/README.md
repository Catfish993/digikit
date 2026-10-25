# FX gain both, run time (test)

The same effect and page as `test/dyn-fx-gain` (tab GAN) with `route = "master"` and `routes = ["master", "send"]` in its `[[fx]]`: an insert by default that the settings key `fx/route/<module id>` can move to a send. Used by the send-page scenario `tests/digikit/scenarios/fx-route-dt2.toml` (the manifest before the route fields could not be a send effect without the key: `test/dyn-fx-gain` allows the master only now).
