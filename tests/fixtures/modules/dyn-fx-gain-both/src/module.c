/* The effect page of test/dyn-fx-gain-both: one effect `gain` with one knob. patchkit pack reads this record; the device never sees it.
   Tab GAN. GAIN is p0, 0 to 1.27: the shown value 0 .. 127 is the gain in percent, so the default 50 is 0.5.
   The MIX knob starts at 127 (wet). The effect starts as an insert on the master and can be switched to a send. */
#include <dk/fx.h>

DK_FX(gain, .process = "_gbt_process", .desc = "_gbt_desc",
      .tab = "GAN", .title = "Test Gain", .mix = 127,
      .route = DK_ROUTE_MASTER, .routes = DK_ROUTE_MASTER | DK_ROUTE_SEND,
      .knob = {DK_KNOB(0, "Gain", "GAIN", DK_PERCENT, 0, 1.27, 0.5)});
