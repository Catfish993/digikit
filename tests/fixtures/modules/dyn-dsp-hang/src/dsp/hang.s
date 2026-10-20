// test/dyn-dsp-hang: a DSP routine of the core/dsp-voice ABI that stops the SHARC on purpose (docs/findings/48-dsp-breadcrumbs.md).
// A machine of this type renders silence until a voice has been active for HANG_AFTER blocks, then the routine enters an endless loop
// and never returns: the core is halted for the engine, the block handler never reaches its tail, the heartbeats freeze. The point of
// the module is the diagnosis: the ColdFire's debug log must name the step (routine called), the voice and the machine type.
//
// Entry (core/dsp-voice's ABI): R4 = voice record (byte +0x1b8 ACTIVE), R8 = output block (32 floats), R2 = track.

#define HANG_AFTER 3

.SECTION/PM seg_pmco;
.GLOBAL _hg_render;

_hg_render:
    I0 = R8;
    I1 = R4;
    MODIFY(I1, 440);                      // record byte +0x1b8: ACTIVE
    R9 = DM(I1, M5);
    R0 = 0xff;
    R0 = R9 AND R0;
    IF EQ JUMP _hg_zero;                  // an idle voice: silence
    R1 = DM(_hg_n);
    R1 = R1 + 1;
    DM(_hg_n) = R1;
    R2 = HANG_AFTER;
    COMP(R1, R2);
    IF GE JUMP _hg_stuck;
_hg_zero:
    R0 = 0;
    LCNTR = 32, DO _hg_zloop UNTIL LCE;
_hg_zloop: DM(I0, M6) = R0;
    JUMP (M13, I13);
_hg_stuck:
    NOP;
    JUMP _hg_stuck;

.SECTION/DM seg_dmda;
.PKDATA _hg_n, 4;
