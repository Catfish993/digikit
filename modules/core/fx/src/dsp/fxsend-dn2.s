// core/fx, the send route on the Digitone II (docs/findings/67-fx-routes-dn2.md): the send route of fxsend.s with the slot mix instead of the track mix. An effect row with
// route 1 does not process the master. Its input is a send bus: the sum over the 16 voice slots of (the row's amount for the slot's track) squared times the slot's bus. Its wet
// output times the row's MIX is added to the stock FX return bus, which the master function mixes like the return of the stock send effects.
//
// The hook is the first instruction of the master function (FUN_1c1f1e, called at 0x1c305d; 48 bits, `I7 = MODIFY(I7, -16)`, one caller). The sends driver (FUN_1c1738) has
// finished: the 16 slot buses 0x257ee8 + 0x100 * v (32 left then 32 right floats; the same buses the driver reads) are intact, the FX return bus 0x25b868 is written (by the
// driver at 0x1c1d96) and not yet read (by the master at 0x1c20fa). The master function itself sums the slots into the track buses (0x258f68 + 0x100 * t), after clearing them;
// the send hook sums the slot buses on its own, with the map the master uses (0x257898). The master mixes the slot buses and the FX return with the same factor (14.126), so the
// return of a unity effect on amount 127 is the track's own sound. Everything else is fxsend.s.
//
// Register use, the row words, the amounts, the gain law and the cost model: fxsend.s (the slot mix costs the 16 map entries more: about 100 instructions).

.EXTERN _fxs_slotbus;
.EXTERN _fxs_slotmap;
.EXTERN _fxs_fadetab;

#include "fxsend-a.inc"
#include "fxsend-slots.inc"
#include "fxsend-b.inc"

// The scalars: 0 the row's index, 8 the slot's bus, 16 the row's old gains, 24 the slot's own bus while a faded copy is mixed.
.PKDATA _fxs_c, 32;
.PKDATA _fxs_trk, 64;                     // the track of each of the 16 slots (0 to 15)
.PKDATA _fxs_flg, 64;                    // the fade flag of each of the 16 slots (0 or the flag byte)
.PKDATA _fxs_fbuf, 256;                   // a fading slot's bus times the fade weights (32 left, 32 right floats)
