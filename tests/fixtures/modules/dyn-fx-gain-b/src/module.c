/* The effect page of test/dyn-fx-gain-b: one effect `gain` with one knob. patchkit pack reads this record; the device never sees it.
   Tab GNB. GAIN is p0, 0 to 1.27: the shown value 0 .. 127 is the gain in percent, so the default 25 is 0.25.
   The MIX knob starts at 64. */
#include <dk/fx.h>

DK_FX(gain, .process = "_dgb_process", .desc = "_dgb_desc",
      .tab = "GNB", .title = "Test Gain B", .mix = 64,
      .knob = {DK_KNOB(0, "Gain", "GAIN", DK_PERCENT, 0, 1.27, 0.25)});
