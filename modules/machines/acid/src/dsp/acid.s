// josh/acid: a TB-303-style bass voice of the SDK contract (sdk/dk_machine.h). The model follows Open303 (Robin Schmidt, MIT) in
// outline, not in code: one oscillator, a resonant four-pole lowpass with its own decaying envelope, and an accent that shortens
// that envelope, opens the filter further and lifts the level. The track's own FLTR, AMP and FX pages still run after it.
//
// Knobs (SAMPLE's TUNE and LEV sit at the ends of the row; the adapter maps the four parameters to param_slots 1, 2, 4, 5):
//   1 CUTOFF  base cutoff, 40 Hz .. 5.1 kHz (7 octaves)
//   2 RESO    feedback 0 .. 3.9 (self-oscillation sits at 4)
//   4 ENVMOD  how far the filter envelope opens the cutoff: 0 .. 5 octaves
//   5 DECAY   filter envelope time 0.15 s .. 2.4 s (the envelope falls to 1/e^3 in that time)
//   6 DRIVE   dry/wet into a diode distortion after the filter: 0 dry, 127 fully distorted
// (knob 6 needs param_slots [1, 2, 4, 5, 6, ...] in devices/digitakt-ii.toml). Knob 7 stays SAMPLE's LEV: on the device the engine
// applies knob 7 of a SAMPLE clone as the level, so a module parameter there doubles as a volume (measured 2026-10-28).
// Accent comes from the trig velocity: 112 and above. It replaces DECAY with 0.2 s and adds 60 % level, as the 303's accent does
// (there it also bypasses the decay pot). Its filter sweep goes through the 303's "accent sweep" smoothing: a charge c follows
// A x envelope with a 100 ms time constant and opens the cutoff by 5 c octaves. A single accent gives a rounded rise; accents in a
// row do not let c discharge, so they ratchet the cutoff up. A trig clears the voice state (the adapter does, and so does the
// engine on hardware), so c lives in _acid_tab, one entry per voice, keyed by the voice's state address.
//
// Slide: the Digitakt's own slide trig towards a trigless lock moves TUNE without a note-on, so the filter envelope keeps falling,
// as on a 303 slide. The pitch here glides to the pitch word with a 15 ms time constant so the move is smooth; a note-on clears
// the glide (word 7 = 0) and the new note starts on its pitch.
//
// Per block (control rate, 32 samples = 0.67 ms): the increment and its reciprocal from the pitch, the cutoff from the envelope
// value at the block start, the one-pole coefficient g = 1 - exp(-2 pi fc / fs), then the envelope steps one block down.
// Per sample:
//   saw = 2p - 1 + a^2 - b^2        PolyBLEP: a = max(0, 1 - p/inc), b = max(0, 1 - (1 - p)/inc)
//   osc = saw(p) - W saw(p + 1/2)   W = 0 for ACID SAW, 1 for ACID SQR (two band-limited saws half a cycle apart: a square)
//   u   = clip(osc - k s4, 1.5);  u = u (1 - 4/27 u^2)      cubic soft clip, |u| <= 1: bounds the loop at any RESO
//   s1 += g (u - s1); s2 += g (s1 - s2); s3 += g (s2 - s3); s4 += g (s3 - s4)
//   x = 30 s4;  d = max(soft(x), 0) + min(soft(2x) / 2, 0)   an asymmetric diode pair: +1 on one side, -1/2 on the other
//   soft(x) = c (1 - 4/27 c^2), c = clip(x, 1.5);  y = MAKEUP (d - z), z += 0.0013 (d - z)   (a 10 Hz DC blocker)
//   out = (s4 + m (y - s4)) level, m = DRIVE
//
// State (16 words = 64 bytes, all cleared at note-on): 0 phase, 1..4 the filter stages, 5 the filter envelope (note_on sets 1.0),
// 6 W (the machine's waveform, written by its render entry every block), 7 the gliding pitch in octaves (0 = not started),
// 9 the DC blocker's z, and per block for the loop: 13 level, 14 the wet amount m (8, 10..12 and 15 are unused).
//
// Registers through the sample loop: F13 phase, F4 increment, F5 1/increment, F6 g, F7 k, F8..F11 s1..s4,
// F14 1.0, F15 0.0, I4 the constant table, I3 the state, I0 the output; F0..F3 and F12 are scratch. _dv_exp2 clobbers R0..R3 only.

#include "dk_machine.inc"

.SECTION/PM seg_pmco;
.GLOBAL _acid_render;
.GLOBAL _acid_sq_render;
.GLOBAL _acid_note_on;
.EXTERN _dv_exp2;

// the two machines' entries: only W differs
_acid_render:
    I3 = R4;                              // the state
    R0 = 0;                               // W = 0.0: saw
    JUMP _acid_body;
_acid_sq_render:
    I3 = R4;
    R0 = 0x3f800000;                      // W = 1.0: square
_acid_body:
    DM(6, I3) = R0;
    I2 = R8;                              // the voice input
    I1 = R12;                             // the parameter words

    // ---- this voice's accent memory: I5 = its tag slot in _acid_tab (words 0..15 tags, 16..31 charges, 32..47 the spare) ----
    R0 = DM(_acid_tinit);
    R1 = 0x41434944;                      // "ACID": the table has been cleared
    COMP(R0, R1);
    IF EQ JUMP _acid_tok;
    I5 = _acid_tab;
    R0 = 0;
    LCNTR = 48, DO _acid_tclr UNTIL LCE;
_acid_tclr: DM(I5, M6) = R0;
    DM(_acid_tinit) = R1;
_acid_tok:
    I5 = _acid_tab;
    R0 = R4;                              // the tag: this voice's state address (never 0)
    R3 = 16;
    R6 = 1;
_acid_scan:
    R1 = DM(0, I5);
    COMP(R1, R0);
    IF EQ JUMP _acid_found;
    R1 = PASS R1;
    IF EQ JUMP _acid_claim;               // tags fill from the front: the first empty one ends the search
    R1 = DM(I5, M6);                      // next tag
    R5 = R3 - R6;
    R3 = PASS R5;
    IF NE JUMP _acid_scan;
    JUMP _acid_found;                     // more voices than tags (never on a Digitakt II): I5 is at word 16, its charge the spare
_acid_claim:
    DM(0, I5) = R0;
    DM(16, I5) = R1;                      // R1 = 0: no charge
_acid_found:

    // ---- pitch -> increment (F4) and its reciprocal (F5), through the glide ----
    R0 = DM(DK_IN_W_PITCH, I2);
    F0 = FLOAT R0;
    R1 = 0x46fe0000;                      // 32512.0 = note 127
    F0 = MIN(F0, F1);
    R1 = 0x39aaaaab;                      // 1 / (256 * 12)
    F7 = F0 * F1;                         // x = octaves from note 0
    R2 = DM(7, I3);                       // the gliding pitch
    F2 = PASS F2;
    IF NE JUMP _acid_glide;
    F2 = PASS F7;                         // 0 after a note-on: start on the pitch
_acid_glide:
    F0 = F7 - F2;
    R1 = 0x3d320eec;                      // 1 - exp(-block / 15 ms)
    F0 = F0 * F1;
    F7 = F2 + F0;
    DM(7, I3) = R7;
    F0 = F7;
    CALL _dv_exp2;
    R1 = 0x39329a62;                      // 440 * 2^(-69/12) / 48000
    F4 = F0 * F1;                         // increment
    F0 = -F7;
    CALL _dv_exp2;
    R1 = 0x45b777e3;                      // 48000 / (440 * 2^(-69/12))
    F5 = F0 * F1;                         // 1 / increment

    // ---- accent A (F9): 0 below velocity 112, 1 from 113, a ramp in between ----
    R0 = DM(DK_IN_W_VELOCITY, I2);        // velocity << 8
    F0 = FLOAT R0;
    R1 = 0xc6e00000;                      // -28672.0 = -(112 << 8)
    F0 = F0 + F1;
    R1 = 0x3b800000;                      // 1 / 256
    F0 = F0 * F1;
    R1 = 0;                               // 0.0
    F0 = MAX(F0, F1);
    R1 = 0x3f800000;                      // 1.0
    F9 = MIN(F0, F1);

    // ---- the four knobs as 0 .. 1: F10 CUTOFF, F11 RESO, F12 ENVMOD, F13 DECAY ----
    R15 = 0x38010204;                     // 1 / 32512
    R0 = DM(0, I1);
    F0 = FLOAT R0;
    F10 = F0 * F15;
    R0 = DM(1, I1);
    F0 = FLOAT R0;
    F11 = F0 * F15;
    R0 = DM(2, I1);
    F0 = FLOAT R0;
    F12 = F0 * F15;
    R0 = DM(3, I1);
    F0 = FLOAT R0;
    F13 = F0 * F15;

    // ---- the accent sweep charge: c += (A e - c) x (1 - exp(-block / 100 ms)) ----
    R8 = DM(5, I3);                       // F8 = the envelope at the block start
    R3 = DM(16, I5);                      // c
    F0 = F9 * F8;                         // A e
    F0 = F0 - F3;
    R1 = 0x3bd9ba0e;                      // 1 - exp(-block / 100 ms)
    F0 = F0 * F1;
    F3 = F3 + F0;
    DM(16, I5) = R3;

    // ---- cutoff: log2 fc = log2 40 + 7 CUTOFF + 5 ENVMOD e + 5 c, at most log2 12000 ----
    R1 = 0x40a00000;                      // 5.0
    F0 = F12 * F1;
    F0 = F0 * F8;
    R1 = 0x40a00000;                      // 5.0
    F1 = F3 * F1;
    F0 = F0 + F1;
    R1 = 0x40e00000;                      // 7.0
    F1 = F10 * F1;
    F0 = F0 + F1;
    R1 = 0x40aa4d3c;                      // log2 40
    F0 = F0 + F1;
    R1 = 0x4158cfdc;                      // log2 12000
    F0 = MIN(F0, F1);
    CALL _dv_exp2;                        // fc in Hz
    R1 = 0x39460597;                      // 2 pi / 48000 * log2 e
    F0 = F0 * F1;
    F0 = -F0;
    CALL _dv_exp2;                        // exp(-2 pi fc / fs)
    R1 = 0x3f800000;                      // 1.0
    F6 = F1 - F0;                         // g

    // ---- resonance ----
    R1 = 0x4079999a;                      // 3.9
    F7 = F11 * F1;                        // k

    // ---- level: 0.45, x 1.6 on an accent ----
    R1 = 0x3f19999a;                      // 0.6
    F0 = F9 * F1;
    R1 = 0x3f800000;                      // 1.0
    F0 = F0 + F1;
    R1 = 0x3ee66666;                      // 0.45
    F12 = F0 * F1;                        // level (ENVMOD is used up)

    // ---- the envelope steps one block: e x 2^E, E = (1 - A) En + A Ea; En = -0.0192 2^(-4 DECAY), Ea for 0.2 s ----
    R1 = 0xc0800000;                      // -4.0
    F0 = F13 * F1;
    CALL _dv_exp2;
    R1 = 0xbc9d94ad;                      // -32 * 3 / (48000 ln 2) / 0.15
    F3 = F0 * F1;                         // En
    R1 = 0xbc6c5f04;                      // -32 * 3 / (48000 ln 2) / 0.2
    F1 = F1 - F3;                         // Ea - En
    F1 = F1 * F9;
    F0 = F3 + F1;                         // E
    CALL _dv_exp2;
    F8 = F8 * F0;
    DM(5, I3) = R8;                       // the envelope for the next block

    // ---- distortion: the level and the wet amount for the loop ----
    DM(13, I3) = R12;                     // level
    R0 = DM(4, I1);                       // DRIVE (knob 6)
    F0 = FLOAT R0;
    R1 = 0x38010204;                      // 1 / 32512
    F0 = F0 * F1;
    DM(14, I3) = R0;                      // m = DRIVE

    // ---- the sample loop ----
    R0 = DM(DK_IN_W_OUT, I2);
    I0 = R0;
    I4 = _acid_k;
    R13 = DM(0, I3);                      // phase
    R8 = DM(1, I3);                       // s1
    R9 = DM(2, I3);                       // s2
    R10 = DM(3, I3);                      // s3
    R11 = DM(4, I3);                      // s4
    R14 = 0x3f800000;                     // 1.0
    R15 = 0;                              // 0.0
    LCNTR = DK_BLOCK, DO _acid_last UNTIL LCE;
        // PolyBLEP saw
        F0 = F13 * F5;                    // p / inc
        F0 = F14 - F0;                    // a = 1 - p / inc
        F0 = MAX(F0, F15);
        F0 = F0 * F0;                     // a+^2: the step's leading half
        F1 = F14 - F13;                   // 1 - p
        F1 = F1 * F5;
        F1 = F14 - F1;                    // b = 1 - (1 - p) / inc
        F1 = MAX(F1, F15);
        F1 = F1 * F1;                     // b+^2: the trailing half
        F0 = F0 - F1;
        F2 = F13 + F13;
        F2 = F2 - F14;                    // 2p - 1
        F2 = F2 + F0;                     // the band-limited saw
        // the second saw, half a cycle on: q = p + 1/2 wrapped
        F3 = DM(2, I4);                   // 0.5
        F3 = F13 + F3;
        F0 = F3 - F14;
        IF GE F3 = PASS F0;
        F0 = F3 * F5;
        F0 = F14 - F0;
        F0 = MAX(F0, F15);
        F0 = F0 * F0;
        F1 = F14 - F3;
        F1 = F1 * F5;
        F1 = F14 - F1;
        F1 = MAX(F1, F15);
        F1 = F1 * F1;
        F0 = F0 - F1;
        F1 = F3 + F3;
        F1 = F1 - F14;
        F1 = F1 + F0;                     // saw(q)
        F0 = DM(6, I3);                   // W
        F1 = F1 * F0;
        F2 = F2 - F1;                     // saw(p) - W saw(q)
        // resonance feedback through the soft clip
        F1 = F11 * F7;                    // k s4
        F2 = F2 - F1;                     // u
        F3 = DM(0, I4);                   // 1.5
        F2 = CLIP F2 BY F3;
        F1 = F2 * F2;
        F3 = DM(1, I4);                   // 4/27
        F1 = F1 * F3;
        F1 = F14 - F1;
        F2 = F2 * F1;                     // u (1 - 4/27 u^2), |.| <= 1
        // four one-pole stages
        F1 = F2 - F8;
        F1 = F1 * F6;
        F8 = F8 + F1;
        F1 = F8 - F9;
        F1 = F1 * F6;
        F9 = F9 + F1;
        F1 = F9 - F10;
        F1 = F1 * F6;
        F10 = F10 + F1;
        F1 = F10 - F11;
        F1 = F1 * F6;
        F11 = F11 + F1;
        // diode distortion of x = 30 s4: the positive half soft-clips at +1, the negative half at -1/2
        F3 = DM(3, I4);                   // 30.0
        F2 = F11 * F3;                    // x
        F3 = DM(0, I4);                   // 1.5
        F0 = CLIP F2 BY F3;
        F1 = F0 * F0;
        F3 = DM(1, I4);                   // 4/27
        F1 = F1 * F3;
        F1 = F14 - F1;
        F0 = F0 * F1;                     // soft(x)
        F12 = MAX(F0, F15);               // the positive side
        F2 = F2 + F2;                     // 2x
        F3 = DM(0, I4);                   // 1.5
        F0 = CLIP F2 BY F3;
        F1 = F0 * F0;
        F3 = DM(1, I4);                   // 4/27
        F1 = F1 * F3;
        F1 = F14 - F1;
        F0 = F0 * F1;                     // soft(2x)
        F3 = DM(2, I4);                   // 0.5
        F0 = F0 * F3;
        F0 = MIN(F0, F15);                // the negative side
        F12 = F12 + F0;                   // d
        // the DC the asymmetry makes: y = d - z, z += a (d - z)
        F3 = DM(9, I3);                   // z
        F0 = F12 - F3;
        F1 = DM(5, I4);                   // a = 1 - exp(-2 pi 10 / 48000)
        F1 = F0 * F1;
        F3 = F3 + F1;
        DM(9, I3) = R3;
        F3 = DM(4, I4);                   // makeup
        F12 = F0 * F3;                    // y
        F12 = F12 - F11;
        F3 = DM(14, I3);                  // m
        F12 = F12 * F3;
        F12 = F12 + F11;                  // s4 + m (y - s4)
        F3 = DM(13, I3);                  // level
        F1 = F12 * F3;                    // out
        // advance the phase
        F13 = F13 + F4;
        F2 = F13 - F14;
        IF GE F13 = PASS F2;
_acid_last: DM(I0, M6) = R1;

    DM(0, I3) = R13;
    DM(1, I3) = R8;
    DM(2, I3) = R9;
    DM(3, I3) = R10;
    DM(4, I3) = R11;
    R0 = DK_OK;
    JUMP (M13, I13);

// not at offset 0 of the image: an offset of 0 reads as "no note_on" in the manifest and the pack
_acid_note_on:
    I3 = R4;
    R0 = 0x3f800000;                      // the filter envelope starts fully open
    DM(5, I3) = R0;
    R0 = 0;
    JUMP (M13, I13);

.SECTION/DM seg_dmda;
// the sample loop's constants (I4)
.VAR _acid_k = 0x3fc00000;                // 1.5: the soft clip's input bound
.VAR _acid_k1 = 0x3e17b426;               // 4/27
.VAR _acid_k2 = 0x3f000000;               // 0.5: the square's second saw, the diode's negative side
.VAR _acid_k3 = 0x41f00000;               // 30.0: the diode's input gain
.VAR _acid_k4 = 0x3eb33333;               // 0.35: the diode's makeup (rig: 0.5 put full wet 3.1 dB over dry on a held note)
.VAR _acid_k5 = 0x3aab75ea;              // 1 - exp(-2 pi 10 / 48000): the DC blocker

// the accent memory: 16 tags (state addresses), their 16 charges, 16 spare words; cleared on first use (bss is not
// zero on the rig)
.VAR _acid_tinit = 0;
.PKBSS _acid_tab, 192;

// dk_machine_desc: magic, contract, n_params, voices, state_bytes, state_clear_bytes, max_cycles_block, max_cycles_note_on,
// max_call_depth, stack_bytes.
.GLOBAL _acid_desc;
.VAR _acid_desc = DK_MAGIC;
.VAR _acid_desc1 = DK_MACHINE_V1;
.VAR _acid_desc2 = 5;
.VAR _acid_desc3 = DK_MAX_VOICES;
.VAR _acid_desc4 = 64;
.VAR _acid_desc5 = 64;
.VAR _acid_desc6 = 5000;                  // measured on the rig: 4583 per block (saw, square, glide, accent sweep, distortion)
.VAR _acid_desc7 = 5100;                  // measured: 4679 (the first block also clears the accent table)
.VAR _acid_desc8 = 2;
.VAR _acid_desc9 = 0;
