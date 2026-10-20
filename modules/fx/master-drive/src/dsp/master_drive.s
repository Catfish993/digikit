// digikit/master-drive: a soft-clip saturator on the final stereo block, before the firmware converts it to Q31.
//
// Hook site and register contract are those of digikit/master-gain (docs/findings/06, "Master hook"): the one CALL of
// the Q31 converter is retargeted here, the 64 planar floats (32 left, then 32 right) are processed in place and
// the routine tail-jumps to the converter, so the frame, the return address and the arguments in R4 and R8 are
// untouched. M5 = 0 and M6 = 1 are the firmware's global modifier constants; SIMD is off and no DO loop is active.
//
// Parameter: the master compressor's Sidechain Filter word (a signed 16-bit word in the DSP's copy of the frame,
// 0 .. 0x7f00 on the UI scale 0 .. 127); address _fw_drive_word by port rule. The factory kits and every saved kit
// hold 0x5000 (UI 80) there, so that is the neutral value: d = (word - 0x5000) / (0x7f00 - 0x5000), clamped to 0 .. 1
// (UI 80 and below: off; 127: full drive).
//
//   G = 1 + 7 d                 pre-gain, up to +18 dB
//   s(t) = t - t^3 / 3          soft clip, |t| clipped to 1 first; slope 0 at the knee (C1)
//   m = 1 - 0.3 d               makeup (a loud signal comes out about 5 dB lower, a quiet one about 15 dB higher)
//   out = x (1 - d) + m s(G x) d        a dry/wet crossfade, so d -> 0 is continuous with bypass
//
// d moves toward the word at most 1/128 per block (85 ms for the full range) and the three coefficients (G, the
// dry weight A, the wet weight B = m d) ramp linearly across each 32-sample channel of the block, so a knob move cannot click. When the
// previous and the current d are both 0 the block is not touched at all: PCM is bit-identical to stock.
//
// No division and no RECIPS. Registers: R0-R3 are dead at the hook; R5-R7 and R9-R13 are saved to _md_s0 .. _md_s7
// and restored (nothing documents them as dead). Cost per 32-sample block on the concrete core: 20 instructions with
// no drive, 968 with the effect fully on (a block of 320,000 to 450,000 is free: docs/findings/06, "Headroom").

.SECTION/PM seg_pmco;
.GLOBAL _md_hook;
.EXTERN _fw_q31_convert;
.EXTERN _fw_master_buf;
.EXTERN _fw_drive_word;

_md_hook:
    // d target in [0, 1]
    R0 = DM(_fw_drive_word) (SWSE);
    F0 = FLOAT R0;
    R1 = 0x46a00000;                      // 20480.0 = 0x5000, the stock default word: no drive at or below it
    F0 = F0 - F1;
    R1 = DM(_md_inv_max);
    F0 = F0 * F1;
    R2 = 0;
    F0 = MAX(F0, F2);
    R1 = 0x3f800000;                      // 1.0
    F0 = MIN(F0, F1);
    // slew: d = d_prev + clip(target - d_prev, 1/128)
    R3 = DM(_md_d);
    F2 = F0 - F3;
    R1 = DM(_md_step);
    F2 = CLIP F2 BY F1;
    F0 = F3 + F2;                         // F0 = d, F3 = d_prev
    DM(_md_d) = R0;
    // bypass when both are zero (d >= 0 always)
    F2 = F0 + F3;
    F2 = PASS F2;
    IF EQ JUMP _md_exit;

    // save R5-R7, R9-R13
    DM(_md_s0) = R5;
    DM(_md_s1) = R6;
    DM(_md_s2) = R7;
    DM(_md_s3) = R9;
    DM(_md_s4) = R10;
    DM(_md_s5) = R11;
    DM(_md_s6) = R12;
    DM(_md_s7) = R13;

    // end-of-block coefficients from d (F0): G in F5, A in F7, B in F10
    R1 = 0x3f800000;                      // 1.0
    R2 = 0x40e00000;                      // 7.0
    F5 = F0 * F2;
    F5 = F5 + F1;                         // G = 1 + 7 d
    F7 = F1 - F0;                         // A = 1 - d
    R2 = 0xbe99999a;                      // -0.3
    F10 = F0 * F2;
    F10 = F10 + F1;                       // m = 1 - 0.3 d
    F10 = F10 * F0;                       // B = m d

    // deltas over the block: (end - start) / 32, exact (power of two); then store the end values
    R1 = DM(_md_g);
    R2 = DM(_md_a);
    R3 = DM(_md_b);
    DM(_md_g) = R5;
    DM(_md_a) = R7;
    DM(_md_b) = R10;
    R12 = 0x3d000000;                     // 1/32
    F6 = F5 - F1;
    F6 = F6 * F12;                        // dG
    F9 = F7 - F2;
    F9 = F9 * F12;                        // dA
    F11 = F10 - F3;
    F11 = F11 * F12;                      // dB
    F5 = PASS F1;                         // start values
    F7 = PASS F2;
    F10 = PASS F3;
    DM(_md_g0) = R1;                      // kept for the second channel
    DM(_md_a0) = R2;
    DM(_md_b0) = R3;

    R12 = 0x3f800000;                     // 1.0, the clip magnitude
    R13 = 0xbeaaaaab;                     // -1/3
    I4 = _fw_master_buf;
    LCNTR = 32, DO _md_last_l UNTIL LCE;  // left: 32 samples
        R0 = DM(I4, M5);
        F1 = F0 * F5;                     // t = G x
        F1 = CLIP F1 BY F12;
        F2 = F1 * F1;
        F2 = F2 * F1;
        F2 = F2 * F13;                    // -t^3 / 3
        F2 = F1 + F2;                     // s(t)
        F2 = F2 * F10;                    // B s
        F3 = F0 * F7;                     // A x
        F5 = F5 + F6;
        F7 = F7 + F9;
        F10 = F10 + F11;
        F0 = F2 + F3;
_md_last_l: DM(I4, M6) = R0;
    R5 = DM(_md_g0);                      // right: the same ramp again
    R7 = DM(_md_a0);
    R10 = DM(_md_b0);
    LCNTR = 32, DO _md_last_r UNTIL LCE;
        R0 = DM(I4, M5);
        F1 = F0 * F5;
        F1 = CLIP F1 BY F12;
        F2 = F1 * F1;
        F2 = F2 * F1;
        F2 = F2 * F13;
        F2 = F1 + F2;
        F2 = F2 * F10;
        F3 = F0 * F7;
        F5 = F5 + F6;
        F7 = F7 + F9;
        F10 = F10 + F11;
        F0 = F2 + F3;
_md_last_r: DM(I4, M6) = R0;

    R5 = DM(_md_s0);
    R6 = DM(_md_s1);
    R7 = DM(_md_s2);
    R9 = DM(_md_s3);
    R10 = DM(_md_s4);
    R11 = DM(_md_s5);
    R12 = DM(_md_s6);
    R13 = DM(_md_s7);
_md_exit:
    JUMP _fw_q31_convert;

.SECTION/DM seg_dmda;
.GLOBAL _md_d;
.GLOBAL _md_g;
.GLOBAL _md_inv_max;
.VAR _md_d = 0x00000000;                  // current drive 0 .. 1 (float)
.VAR _md_g = 0x3f800000;                  // G at the end of the last processed block
.VAR _md_a = 0x3f800000;                  // dry weight
.VAR _md_b = 0x00000000;                  // wet weight
.VAR _md_inv_max = 0x38ae4c41;            // 1 / (0x7f00 - 0x5000) (float)
.VAR _md_step = 0x3c000000;               // 1/128
.VAR _md_g0 = 0x3f800000;                 // start values of the block, scratch
.VAR _md_a0 = 0x3f800000;
.VAR _md_b0 = 0;
.VAR _md_s0 = 0;
.VAR _md_s1 = 0;
.VAR _md_s2 = 0;
.VAR _md_s3 = 0;
.VAR _md_s4 = 0;
.VAR _md_s5 = 0;
.VAR _md_s6 = 0;
.VAR _md_s7 = 0;
