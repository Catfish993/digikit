// core/dsp-link, Digitakt II: the geometry of the frame tail.
//
// The ColdFire sends 0xabc bytes per block and fills 0x802 of them with data (finding 23, S4b). The DSP's receive ring
// holds them as 32-bit words, two wire halfwords each, the first one in the low half (finding 31). The first whole
// word of the tail is word 0x201 (byte 0x804); the last is word 0x2ae: 174 words.
#define DL_TAIL_OFF 0x804
#define DL_TAIL_WORDS 174
#include "dsp-link.inc"
