/* The effect declaration of __ID__: one effect with one GAIN knob and the framework's MIX knob. patchkit pack reads these records;
   the device never sees them. Saved kits keep the effect by module id + effect id: never rename either. */
#include <dk/fx.h>
#include "__NAME__.h"

DK_FX(__NAME__, .process = "___NAME___process", .desc = "___NAME___desc", .mix = 127);
DK_FX_PARAM(__NAME__, __UPPER___GAIN_WORD, "Gain", "GAIN", DK_PERCENT, 0, 1.27, 0.5);
DK_PAGE(__NAME__, "__TAB__", "__TITLE__", DK_KNOB_AT(__UPPER___GAIN_WORD, 0));
