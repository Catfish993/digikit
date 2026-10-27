/* The effect page of __ID__: one effect with one GAIN knob and the framework's MIX knob. patchkit pack reads this record;
   the device never sees it. Saved kits keep the effect by module id + effect id: never rename either. */
#include <dk/fx.h>
#include "__NAME__.h"

DK_FX(__NAME__, .process = "___NAME___process", .desc = "___NAME___desc",
      .tab = "__TAB__", .title = "__TITLE__", .mix = 127,
      .knob = {DK_KNOB(__UPPER___GAIN_WORD, "Gain", "GAIN", DK_PERCENT, 0, 1.27, 0.5)});
