// test/sine-voice on the Digitone II: a pitched sine rendered by the DN2 voice hook (core/dsp-voice, dsp-voice-dn2.s)
// into the slot's voice buffer (32 floats at 48 kHz), the proof that a registered machine type runs through the engine's
// later stages (gain, amplitude envelope, filter, overdrive, sends, FX, master).
//
// Routine ABI: see core/dsp-voice (DN2): R4 = voice buffer, R0 = type, R2 = slot v, R12 = 32. Per slot state in
// _sv_state: phase[v] (0 .. 1, float). The pitch follows the frame: n = pitch word / 256 (the frame halfword 1 + t,
// note << 8), f = 440 * 2^((n - 69) / 12); the level is the velocity word (halfword 26 + t, velocity << 8) /
// (127 << 8) * 0.4 (an FM TONE voice buffer peaks near 0.6). The amplitude envelope and the gate are the engine's.
//
// sin(2 pi p) is -sin(2 pi (p - 0.5)); the argument is folded into [-0.25, 0.25] (2 clip(x, 0.25) - x) and a
// degree-9 odd Taylor polynomial gives |error| < 4e-6.

.SECTION/PM seg_pmco;
.GLOBAL _sv_render;
.EXTERN _dv_exp2;
.EXTERN _dv_frame_hw;

_sv_render:
    R5 = R2;                              // t
    R7 = R4;                              // output block
    R0 = LSHIFT R5 BY 2;
    R1 = _sv_state;
    R6 = R1 + R0;                         // &phase[t]
    // velocity: silent while zero
    R1 = 26;
    R0 = R1 + R5;
    CALL _dv_frame_hw;
    R0 = PASS R0;
    IF EQ JUMP _sv_zero;
    F9 = FLOAT R0;
    R1 = 0x374e69a0;                      // 0.4 / 32512
    F9 = F9 * F1;
    // frequency
    R1 = 1;
    R0 = R1 + R5;
    CALL _dv_frame_hw;                    // pitch word: note << 8
    F10 = FLOAT R0;
    R1 = 0x39aaaaab;                      // 1 / (256 * 12)
    F0 = F10 * F1;
    CALL _dv_exp2;
    R1 = 0x39329a62;                      // 440 * 2^(-69/12) / 48000
    F12 = F0 * F1;                        // phase increment
    I2 = R6;
    R13 = DM(0, I2);                      // phase
    I0 = R7;
    R14 = 0x3f000000;                     // 0.5
    R15 = 0x3e800000;                     // 0.25
    R11 = 0x3f800000;                     // 1.0
    LCNTR = 32, DO _sv_samples UNTIL LCE;
        F0 = F13 - F14;                   // x = p - 0.5
        F1 = CLIP F0 BY F15;              // clamp to +-0.25
        F2 = F1 + F1;
        F0 = F2 - F0;                     // x' = 2 clip(x) - x
        F1 = F0 * F0;                     // y = x'^2
        R2 = 0x42283c1a;                  // a9
        F2 = F2 * F1;                     // Horner in y: a9 y + a7, ...
        R3 = 0xc2996966;                  // a7
        F2 = F2 + F3;
        F2 = F2 * F1;
        R3 = 0x42a335e3;                  // a5
        F2 = F2 + F3;
        F2 = F2 * F1;
        R3 = 0xc2255de7;                  // a3
        F2 = F2 + F3;
        F2 = F2 * F1;
        R3 = 0x40c90fdb;                  // a1
        F2 = F2 + F3;
        F2 = F2 * F0;                     // sin(2 pi x')
        F2 = F2 * F9;                     // level
        F2 = -F2;
        F13 = F13 + F12;
        F3 = F13 - F11;                   // wrap p at 1
        IF GE F13 = PASS F3;
_sv_samples: DM(I0, M6) = R2;
    I2 = R6;
    DM(0, I2) = R13;
    JUMP (M13, I13);

_sv_zero:
    I0 = R7;
    R0 = 0;
    LCNTR = 32, DO _sv_zloop UNTIL LCE;
_sv_zloop: DM(I0, M6) = R0;
    JUMP (M13, I13);

.SECTION/DM seg_dmda;
.PKDATA _sv_state, 64;
