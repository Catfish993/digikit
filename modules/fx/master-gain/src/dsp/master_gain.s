// fx/master-gain: scale the final stereo block before the firmware converts it to Q31.
//
// The hook site is the one CALL of the Q31 converter (patchkit retargets it to _mg_hook). The converter reads
// 64 planar floats (32 left, then 32 right) from the master work buffer, so the routine multiplies those 64
// words in place by the gain and tail-jumps to the converter: its frame, return address, delay-slot work and
// the arguments in R4 and R8 are untouched.
//
// Registers: only R0, R1 and I4 are used; all are dead at the call (docs/findings/06, "Master hook").
// M5 = 0 and M6 = 1 are the firmware's global modifier constants. SIMD is off and no DO loop is active here.
//
// _mg_gain is the module's own data word (an IEEE float). It reads 1.0, which leaves the audio bit-identical;
// change it at run time (the emulator's dsp.patch) or in a build of the module.

.SECTION/PM seg_pmco;
.GLOBAL _mg_hook;
.EXTERN _fw_q31_convert;
.EXTERN _fw_master_buf;

_mg_hook:
    R0 = DM(_mg_gain);
    I4 = _fw_master_buf;
    LCNTR = 64, DO _mg_last UNTIL LCE;
        R1 = DM(I4, M5);
        F1 = F1*F0;
_mg_last: DM(I4, M6) = R1;
    JUMP _fw_q31_convert;

.SECTION/DM seg_dmda;
.GLOBAL _mg_gain;
.VAR _mg_gain = 0x3f800000;
