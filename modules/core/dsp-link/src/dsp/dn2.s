// core/dsp-link, Digitone II: the geometry of the frame tail.
//
// The ColdFire sends 0xabc bytes per block and fills 0xa80 of them with data (finding 23, S4b): the tail is words 672
// to 686 of the ring, 15 words.
#define DL_TAIL_OFF 0xa80
#define DL_TAIL_WORDS 15
#include "dsp-link.inc"
