/* The machine declaration of __ID__: one machine with one LEVEL parameter. patchkit pack reads these records;
   the device never sees them. The DSP symbols are in src/dsp/__NAME__.s. Parameter word 0 is the first word the DSP reads. */
#include <dk/machine.h>

DK_MACHINE(__NAME__, .name = "__TITLE__", .short_name = "__SHORT__", .render = "___NAME___render",
           .note_on = "___NAME___note_on", .desc = "___NAME___desc");
DK_PARAM(__NAME__, 0, __NAME___level, "Level", "LEVEL", 0, 127, 127);
DK_PAGE(__NAME__, "__SHORT__", "__TITLE__", DK_KNOB(0));
