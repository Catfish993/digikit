// core/dsp-link, Digitone II: the geometry of the frame tail.
//
// The ColdFire sends 0xabc bytes per block and fills 0xa80 of them with data (finding 23, S4b): the tail is words 672
// to 686 of the ring, 15 words.
#define DL_TAIL_OFF 0xa80
#define DL_TAIL_WORDS 15
// The reserved area (window 0). The offset field of a chunk is 17 bits, so an area is at most 524288 bytes. The runtime places
// the DSP objects of run-time modules in it (docs/design/digikit-dkm.md, "DSP section"): the init data (code and data) and the
// zero-initialised bss of a module live in the area together.
#define DL_AREA_BYTES 327680
#define DL_AREA_WORDS 81920
#include "dsp-link.inc"
.PKBSS _dl_area, 327680;                 // DL_AREA_BYTES (the size must be a literal here)
