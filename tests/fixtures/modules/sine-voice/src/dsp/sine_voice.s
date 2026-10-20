// test/sine-voice: a pitched sine voice for the per-voice hook (core/dsp-voice), the proof that a registered machine
// type renders through the engine's later stages (amp envelope, filter, FX, sends, master).
//
// Routine ABI: see core/dsp-voice. Per track state in _sv_state: phase[16] (0 .. 1, float) and the sample count
// [16] (integer). The pitch follows the frame: n = note + (tune - 64) semitones, f = 440 * 2^((n - 69) / 12), where
// the note is the pitch word (0x02 + 2t, note << 8) and the tune is the first machine parameter (mirror index 25,
// 0x4000 = 64.0). The level is the velocity word (0x34 + 2t, velocity << 8) / (127 << 8) * 0.5. The voice stops
// after 96,000 samples (2 s) by clearing byte +0x1b8 of its record.
//
// sin(2 pi p) is -sin(2 pi (p - 0.5)); the argument is folded into [-0.25, 0.25] (2 clip(x, 0.25) - x) and a
// degree-9 odd Taylor polynomial gives |error| < 4e-6.

.SECTION/PM seg_pmco;
.GLOBAL _sv_render;
.EXTERN _dv_exp2;
.EXTERN _dv_frame_hw;

_sv_render:
    R5 = R2;                              // t
    R7 = R8;                              // output block
    I1 = R4;
    MODIFY(I1, 440);                      // &record[0x1b8]: byte 0 ACTIVE, 1 mark, 2 pending, 3 reverse
    R9 = DM(I1, M5);
    R0 = 0xff;
    R0 = R9 AND R0;
    IF EQ JUMP _sv_idle;
    // state pointers: phase at _sv_state + 4t, count at +64
    R0 = LSHIFT R5 BY 2;
    R1 = _sv_state;
    R6 = R1 + R0;                         // &phase[t]
    R0 = 0xff00;
    R0 = R9 AND R0;
    IF NE JUMP _sv_running;
    R0 = 0x5a00;                          // note-on: mark the voice, restart the oscillator
    R9 = R9 OR R0;
    DM(I1, M5) = R9;
    I2 = R6;
    R0 = 0;
    DM(0, I2) = R0;
    DM(16, I2) = R0;
_sv_running:
    I2 = R6;
    // count += 32; at 96000 the voice ends
    R10 = DM(16, I2);
    R0 = 32;
    R10 = R0 + R10;
    DM(16, I2) = R10;
    R0 = 96000;
    COMP(R10, R0);
    IF GE JUMP _sv_stop;
    // frequency
    R0 = R5 + 1;
    CALL _dv_frame_hw;                    // pitch word: note << 8
    F10 = FLOAT R0;
    R0 = LSHIFT R5 BY 5;
    R1 = LSHIFT R5 BY 4;
    R0 = R1 + R0;
    R1 = 0x6d;
    R0 = R1 + R0;
    CALL _dv_frame_hw;                    // parameter 0 (tune), 0x4000 = 64.0
    F11 = FLOAT R0;
    R1 = 0x46800000;                      // 16384.0
    F11 = F11 - F1;
    F10 = F10 + F11;                      // (n - 60) * 256 + 60 * 256
    R1 = 0x39aaaaab;                      // 1 / (256 * 12)
    F0 = F10 * F1;
    CALL _dv_exp2;
    R1 = 0x39329a62;                      // 440 * 2^(-69/12) / 48000
    F12 = F0 * F1;                        // phase increment
    // level from the velocity word
    R1 = 26;
    R0 = R1 + R5;
    CALL _dv_frame_hw;
    F0 = FLOAT R0;
    R1 = 0x38010204;                      // 1 / 32512
    F0 = F0 * F1;
    R1 = 0x3f000000;                      // 0.5
    F9 = F0 * F1;
    // the block
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

_sv_stop:
    R0 = 0xffffff00;
    R9 = R9 AND R0;                       // ACTIVE = 0 and the mark cleared
    R0 = 0xffff00ff;
    R9 = R9 AND R0;
    DM(I1, M5) = R9;
_sv_idle:
    R0 = 0xff00;
    R0 = R9 AND R0;
    IF EQ JUMP _sv_zero;
    R0 = 0xffff00ff;                      // a stopped voice: clear the mark
    R9 = R9 AND R0;
    DM(I1, M5) = R9;
_sv_zero:
    I0 = R8;
    R0 = 0;
    LCNTR = 32, DO _sv_zloop UNTIL LCE;
_sv_zloop: DM(I0, M6) = R0;
    JUMP (M13, I13);

.SECTION/DM seg_dmda;
.PKDATA _sv_state, 128;
