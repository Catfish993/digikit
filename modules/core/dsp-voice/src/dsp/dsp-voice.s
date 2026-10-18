// core/dsp-voice: the per-voice render hook of the SHARC engine (docs/findings/06, "Per-voice render hook").
//
// The firmware renders its 32 voices (two per track: 2t and 2t+1) with a CJUMP-called function whose first
// instruction is `MODIFY(I7, -26) (NW)` (the sample renderer, which also zero-fills an idle voice). patchkit
// overwrites that one 48-bit instruction with `JUMP _dv_entry` (op "jump"); this file executes the displaced
// instruction itself and continues in the stock renderer for every type it does not know, so a patched image with
// no registered type renders exactly the stock audio.
//
// At _dv_entry the stock contract holds: R4 = voice record, R8 = the voice's output buffer (32 floats), R12 = 32,
// R14 = the voice loop counter (32 on the first voice, 1 on the last), I6 = the frame the caller's CJUMP built, the
// delay-slot stores are done. The track is (32 - R14) >> 1; its machine type is the short word cache[t] at
// _fw_type_cache (the DSP's copy of frame word 0x94 + 2t), read as one 32-bit word that holds two tracks.
//
// A registered type (a row {type, pc} of the registry `voices` = _dv_table, filled by `[[entry]]` rows of the machine modules) is rendered by
// calling its routine once per track, on the even voice; the odd voice receives a copy of the even voice's block
// (a synthesised machine is mono; the engine's later stages pan and mix both voices as for a mono sample).
//
// Routine ABI (entered with JUMP (M13, I12); I13 holds the return address, the routine ends with JUMP (M13, I13).
// The simulator's tracer does not follow an indirect CALL, and the firmware has none; the routine may use ordinary
// CALL/RTS pairs for its own subroutines):
//   in   R4 = voice record (0x1d8 bytes; byte +0x1b8 ACTIVE, +0x1b9 note-on mark, see below), R8 = output block
//        (32 floats, written in full), R12 = 32, R0 = type, R1 = voice index (even), R2 = track t.
//   may clobber  R0-R15, I0-I5, I13, M1-M4, B3 and the ALU/multiplier state; this wrapper saves and restores the
//        registers the stock renderer preserves (and I13). Must keep: I6, I7, I8-I12, I14, I15, M0, M5-M15, L registers,
//        MODE1 (SIMD off).
//   The context registers are SET by this wrapper, never assumed. The firmware restores M5, M6, M7, M13, M14, M15, the L
//   registers and MODE1 per DSP context (blk69 context restore, finding 45), so their values at the hook are the context's,
//   not a constant of the machine. Before a registered type runs, the wrapper saves them in the frame and pins
//        M5 = 0, M6 = 1, M7 = -1, M13 = 0, M14 = 1, M15 = -1, L0-L5 = 0 (linear addressing),
//        MODE1: SIMD (PEYEN), ALUSAT and TRUNCATE clear, RND32 set (the startup value; round to nearest, 32-bit floats),
//   and restores the saved values before the stock epilogue runs. So a routine may use M5/M6/M7 as the constants above and
//   JUMP (M13, I13) as written, and it must leave them pinned. The wrapper's own code before the pin (the type read and the
//   registry scan) uses immediate offsets and no modify register; I4's L register is zeroed for it and put back on the stock path.
//   The routine may read the frame copy at _fw_frame (512 short words: pitch word 0x02 + 2t = note << 8; velocity
//   0x34 + 2t = velocity << 8; machine parameters from 0xda + 0x60 t + 2 (mirror index - 25)).
//
// Note-on: the firmware's arm routine stores the short 1 at record +0x1b8, which clears byte +0x1b9. A routine marks
// a running voice by writing a non-zero byte to +0x1b9; `+0x1b8 != 0 and +0x1b9 == 0` is therefore a note-on in this
// block, whatever the machine's selector did to the record in the meantime (docs/findings/06).
//
// Registers: R0-R2, I4 are free on the stock fall-through path (the stock code assigns them before it reads them).

.SECTION/PM seg_pmco;
.GLOBAL _dv_entry;
.EXTERN _fw_type_cache;
.EXTERN _fw_render_next;
.EXTERN _fw_frame;
.EXTERN _dv_table;

_dv_entry:
    R0 = L4;
    DM(_dv_sv_l4) = R0;                   // I4 is used below as a linear pointer: L4 = 0 until the stock path (put back there)
    L4 = 0;
    R0 = 32;
    R0 = R0 - R14;                        // v, the voice index
    IF NE JUMP _dv_nodiag;
    // once per block (voice 0): the heartbeat and the context registers as the firmware left them (finding 45, dl_diag)
    I4 = _dv_diag;
    R1 = DM(0, I4);
    R1 = R1 + 1;
    DM(0, I4) = R1;
    R1 = M5;
    DM(1, I4) = R1;
    R1 = M6;
    DM(2, I4) = R1;
    R1 = M7;
    DM(3, I4) = R1;
    R1 = M13;
    DM(4, I4) = R1;
    R1 = M14;
    DM(5, I4) = R1;
    R1 = M15;
    DM(6, I4) = R1;
    R1 = I6;
    DM(7, I4) = R1;
    R1 = I7;
    DM(8, I4) = R1;
    R1 = B7;
    DM(9, I4) = R1;
    R1 = L7;
    DM(10, I4) = R1;
    R1 = MODE1;
    DM(11, I4) = R1;
_dv_nodiag:
    R1 = LSHIFT R0 BY 8;
    R1 = BSET R1 BY 0;                    // breadcrumb stage 1 (hook entered) | v << 8, kept in the DSP's own diagnostic word 12
    DM(_dv_diag + 48) = R1;
    R2 = FEXT R0 BY 1:4;                  // t, the track
    R1 = 0xe;
    R1 = R2 AND R1;                       // t & 14
    R1 = LSHIFT R1 BY 1;                  // byte offset of the 32-bit word that holds cache[t] and cache[t^1]
    R2 = _fw_type_cache;
    R1 = R2 + R1;
    I4 = R1;
    R1 = DM(0, I4);                       // the two types
    R2 = FEXT R0 BY 1:4;                  // t
    R2 = FEXT R2 BY 0:1;                  // t & 1
    R2 = LSHIFT R2 BY 4;                  // 0 or 16: the bit position of the half
    R0 = 0x400;
    R2 = R2 OR R0;
    R1 = FEXT R1 BY R2;                   // cache[t]
    I4 = _dv_table;
_dv_scan:
    R0 = DM(0, I4);                       // row type; 0 ends the table (the last row is always empty)
    R2 = DM(1, I4);                       // row pc
    MODIFY(I4, 2) (NW);
    R0 = PASS R0;
    IF EQ JUMP _dv_stock;
    COMP(R0, R1);
    IF NE JUMP _dv_scan;
#ifdef DV_BISECT_C2
    JUMP _dv_stock;                       // bisect build: no registered path; the type takes the stock path (stage 3 for 7 or more)
#endif

    // A registered type: the even voice runs the routine, the odd voice copies the even voice's block (the buffers
    // are 0x80 bytes apart).
    I12 = R2;                             // the routine's pc
#ifdef DV_C3_FINE
    // Rung C3 (docs/PATCHING.md): fine breadcrumb 8, the type matched (R0, R2, I4 are free here; written inline, no CALL, no RTS)
    R0 = 32;
    R0 = R0 - R14;
    R0 = LSHIFT R0 BY 8;
    R2 = LSHIFT R1 BY 16;
    R0 = R0 OR R2;
    DM(_dv_bcw) = R0;
    R2 = BSET R0 BY 3;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_f1a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_f1a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_f1b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_f1b:
#endif
#ifdef DV_C3_NOIRQ
    // Rung C3: interrupts masked from here to the epilogue (MODE1.IRPTEN clear; the saved MODE1 keeps it set). R0 holds the original MODE1.
    R0 = MODE1;
    BIT CLR MODE1 0x1000;
    NOP;
#endif
    MODIFY(I7, -40) (NW);
#ifdef DV_C3_DEEP
    // deep breadcrumb 11: after the MODIFY of I7 (R0, R2, I4 only; the stage word is in R2)
    R0 = 11;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d11a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d11a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d11b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d11b:
#endif
    DM(-6, I6) = R3;
    DM(-7, I6) = R5;
    DM(-8, I6) = R6;
    DM(-9, I6) = R7;
    DM(-10, I6) = R9;
    DM(-11, I6) = R10;
    DM(-12, I6) = R11;
    DM(-13, I6) = R13;
    DM(-14, I6) = R14;
    DM(-15, I6) = R15;
#ifdef DV_C3_DEEP
    // deep breadcrumb 12: stores -6 .. -15 (R registers) done, before the first DAG1 register store (R0, R2, I4 only; the stage word is in R2)
    R0 = 12;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d12a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d12a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d12b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d12b:
#endif
#ifdef DV_C3_VIAR
    R2 = I0;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-16, I6) = R2;
#else
    DM(-16, I6) = I0;
#endif
#ifdef DV_C3_DEEP
    // deep breadcrumb 13: after DM(-16, I6) = I0 (R0, R2, I4 only; the stage word is in R2)
    R0 = 13;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d13a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d13a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d13b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d13b:
#endif
#ifdef DV_C3_VIAR
    R2 = I1;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-17, I6) = R2;
#else
    DM(-17, I6) = I1;
#endif
#ifdef DV_C3_VIAR
    R2 = I2;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-18, I6) = R2;
#else
    DM(-18, I6) = I2;
#endif
#ifdef DV_C3_VIAR
    R2 = I3;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-19, I6) = R2;
#else
    DM(-19, I6) = I3;
#endif
#ifdef DV_C3_VIAR
    R2 = I5;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-20, I6) = R2;
#else
    DM(-20, I6) = I5;
#endif
#ifdef DV_C3_DEEP
    // deep breadcrumb 14: after I1, I2, I3, I5 (R0, R2, I4 only; the stage word is in R2)
    R0 = 14;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d14a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d14a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d14b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d14b:
#endif
#ifdef DV_C3_VIAR
    R2 = M1;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-21, I6) = R2;
#else
    DM(-21, I6) = M1;
#endif
#ifdef DV_C3_VIAR
    R2 = M2;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-22, I6) = R2;
#else
    DM(-22, I6) = M2;
#endif
#ifdef DV_C3_VIAR
    R2 = M3;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-23, I6) = R2;
#else
    DM(-23, I6) = M3;
#endif
#ifdef DV_C3_DEEP
    // deep breadcrumb 15: after M1, M2, M3 (R0, R2, I4 only; the stage word is in R2)
    R0 = 15;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d15a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d15a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d15b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d15b:
#endif
#ifdef DV_C3_VIAR
    R2 = B3;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-24, I6) = R2;
#else
    DM(-24, I6) = B3;
#endif
    DM(-25, I6) = I13;
    // save the context registers, then pin them to the ABI's values (see the header)
#ifdef DV_C3_DEEP
    // deep breadcrumb 16: after B3, I13 (R0, R2, I4 only; the stage word is in R2)
    R0 = 16;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d16a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d16a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d16b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d16b:
#endif
#ifdef DV_C3_VIAR
    R2 = M5;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-26, I6) = R2;
#else
    DM(-26, I6) = M5;
#endif
#ifdef DV_C3_VIAR
    R2 = M6;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-27, I6) = R2;
#else
    DM(-27, I6) = M6;
#endif
#ifdef DV_C3_VIAR
    R2 = M7;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-28, I6) = R2;
#else
    DM(-28, I6) = M7;
#endif
#ifdef DV_C3_DEEP
    // deep breadcrumb 17: after M5, M6, M7 (R0, R2, I4 only; the stage word is in R2)
    R0 = 17;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d17a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d17a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d17b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d17b:
#endif
    DM(-29, I6) = M13;
    DM(-30, I6) = M14;
    DM(-31, I6) = M15;
#ifdef DV_C3_DEEP
    // deep breadcrumb 18: after M13, M14, M15 (R0, R2, I4 only; the stage word is in R2)
    R0 = 18;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d18a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d18a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d18b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d18b:
#endif
#ifdef DV_C3_VIAR
    R2 = L0;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-32, I6) = R2;
#else
    DM(-32, I6) = L0;
#endif
#ifdef DV_C3_VIAR
    R2 = L1;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-33, I6) = R2;
#else
    DM(-33, I6) = L1;
#endif
#ifdef DV_C3_VIAR
    R2 = L2;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-34, I6) = R2;
#else
    DM(-34, I6) = L2;
#endif
#ifdef DV_C3_VIAR
    R2 = L3;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-35, I6) = R2;
#else
    DM(-35, I6) = L3;
#endif
#ifdef DV_C3_DEEP
    // deep breadcrumb 19: after L0 .. L3 (R0, R2, I4 only; the stage word is in R2)
    R0 = 19;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d19a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d19a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d19b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d19b:
#endif
    R3 = DM(_dv_sv_l4);                   // L4 at entry (zeroed since)
#ifdef DV_C3_DEEP
    // deep breadcrumb 20: after the load of _dv_sv_l4 (R0, R2, I4 only; the stage word is in R2)
    R0 = 20;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d20a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d20a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d20b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d20b:
#endif
    DM(-36, I6) = R3;
#ifdef DV_C3_VIAR
    R2 = L5;                           // PRM p.15-7: a DM store cannot take a DAG1 register (UREGXDAG1), go through R2 as the stock code does
    DM(-37, I6) = R2;
#else
    DM(-37, I6) = L5;
#endif
#ifdef DV_C3_DEEP
    // deep breadcrumb 21: after DM(-36) and DM(-37) (R0, R2, I4 only; the stage word is in R2)
    R0 = 21;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d21a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d21a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d21b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d21b:
#endif
#ifdef DV_C3_NOIRQ
    DM(-38, I6) = R0;                     // the MODE1 of the hook entry (IRPTEN set), read before the mask
#else
    R3 = MODE1;
#ifdef DV_C3_DEEP
    // deep breadcrumb 22: after the read of MODE1 (R0, R2, I4 only; the stage word is in R2)
    R0 = 22;
    R2 = DM(_dv_bcw);
    R2 = R2 OR R0;
    I4 = _dv_diag;
    DM(12, I4) = R2;
    I4 = _dv_rec;
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d22a;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
    I4 = _dv_rec;
_dv_d22a:
    R0 = DM(1, I4);
    R0 = PASS R0;
    IF EQ JUMP _dv_d22b;
    I4 = R0;
    DM(192, I4) = R2;
    R0 = 0x11D1A6;
    DM(45, I4) = R0;
    R0 = DM(_dv_diag + 52);
    DM(193, I4) = R0;
    R0 = DM(_dv_diag + 56);
    DM(213, I4) = R0;
_dv_d22b:
#endif
    DM(-38, I6) = R3;
#endif
#ifdef DV_C3_FINE
    // fine breadcrumb 9, the frame is saved
    R3 = 9;
    R5 = DM(_dv_bcw);
    R3 = R3 OR R5;
    I4 = _dv_diag;
    DM(12, I4) = R3;
    I4 = _dv_rec;
    R5 = DM(0, I4);
    R5 = PASS R5;
    IF EQ JUMP _dv_f2a;
    I4 = R5;
    DM(192, I4) = R3;
    R5 = 0x11D1A6;
    DM(45, I4) = R5;
    R5 = DM(_dv_diag + 52);
    DM(193, I4) = R5;
    R5 = DM(_dv_diag + 56);
    DM(213, I4) = R5;
    I4 = _dv_rec;
_dv_f2a:
    R5 = DM(1, I4);
    R5 = PASS R5;
    IF EQ JUMP _dv_f2b;
    I4 = R5;
    DM(192, I4) = R3;
    R5 = 0x11D1A6;
    DM(45, I4) = R5;
    R5 = DM(_dv_diag + 52);
    DM(193, I4) = R5;
    R5 = DM(_dv_diag + 56);
    DM(213, I4) = R5;
_dv_f2b:
#endif
    M5 = 0;
    M6 = 1;
    M7 = -1;
    M13 = 0;
    M14 = 1;
    M15 = -1;
    L0 = 0;
    L1 = 0;
    L2 = 0;
    L3 = 0;
    L5 = 0;
#ifndef DV_C3_NOSYS
    BIT CLR MODE1 0x20A000;               // PEYEN, TRUNCATE, ALUSAT
    BIT SET MODE1 0x10000;                // RND32
    NOP;
#endif
#ifdef DV_C3_FINE
    // fine breadcrumb 10, the context is pinned
    R3 = 10;
    R5 = DM(_dv_bcw);
    R3 = R3 OR R5;
    I4 = _dv_diag;
    DM(12, I4) = R3;
    I4 = _dv_rec;
    R5 = DM(0, I4);
    R5 = PASS R5;
    IF EQ JUMP _dv_f3a;
    I4 = R5;
    DM(192, I4) = R3;
    R5 = 0x11D1A6;
    DM(45, I4) = R5;
    R5 = DM(_dv_diag + 52);
    DM(193, I4) = R5;
    R5 = DM(_dv_diag + 56);
    DM(213, I4) = R5;
    I4 = _dv_rec;
_dv_f3a:
    R5 = DM(1, I4);
    R5 = PASS R5;
    IF EQ JUMP _dv_f3b;
    I4 = R5;
    DM(192, I4) = R3;
    R5 = 0x11D1A6;
    DM(45, I4) = R5;
    R5 = DM(_dv_diag + 52);
    DM(193, I4) = R5;
    R5 = DM(_dv_diag + 56);
    DM(213, I4) = R5;
_dv_f3b:
#endif
    R3 = 32;
    R3 = R3 - R14;                        // v
    R3 = LSHIFT R3 BY 8;
    R5 = LSHIFT R1 BY 16;
    R3 = R3 OR R5;                        // v << 8 | type << 16: the breadcrumb word without its stage
    DM(_dv_bcw) = R3;
    R3 = 2;
#ifdef DV_C3_DB
    CALL _dv_crumb (DB);
    NOP;
    NOP;
#else
    CALL _dv_crumb;                       // stage 2: the type is registered
#endif
    R0 = R1;                              // type
    R1 = 32;
    R1 = R1 - R14;                        // v
    R2 = FEXT R1 BY 1:4;                  // t
    R3 = FEXT R1 BY 0:1;
    R3 = PASS R3;
    IF NE JUMP _dv_odd;
    R3 = DM(_dv_diag + 52);
    R3 = R3 + 1;
    DM(_dv_diag + 52) = R3;               // dispatches to registered types
    R3 = 4;
#ifdef DV_C3_DB
    CALL _dv_crumb (DB);
    NOP;
    NOP;
#else
    CALL _dv_crumb;                       // stage 4: the routine is about to be called (R3, R5, I4 are free here)
#endif
    R12 = 32;
    I13 = _dv_back;
    JUMP (M13, I12);                      // the routine returns with JUMP (M13, I13)
_dv_back:
    R3 = DM(_dv_diag + 56);
    R3 = R3 + 1;
    DM(_dv_diag + 56) = R3;               // routines that returned
    R3 = 5;
#ifdef DV_C3_DB
    CALL _dv_crumb (DB);
    NOP;
    NOP;
#else
    CALL _dv_crumb;                       // stage 5: the routine returned
#endif
    JUMP _dv_done;
_dv_odd:
    I1 = R8;
    MODIFY(I1, -128);
    I0 = R8;
    LCNTR = 32, DO _dv_copy UNTIL LCE;
        R0 = DM(I1, M6);
_dv_copy: DM(I0, M6) = R0;
    R3 = 6;
#ifdef DV_C3_DB
    CALL _dv_crumb (DB);
    NOP;
    NOP;
#else
    CALL _dv_crumb;                       // stage 6: the odd voice's copy is done
#endif
_dv_done:
#ifndef DV_C3_NOSYS
    R0 = DM(-38, I6);
    MODE1 = R0;
    NOP;
#endif
    M5 = DM(-26, I6);
    M6 = DM(-27, I6);
    M7 = DM(-28, I6);
    M13 = DM(-29, I6);
    M14 = DM(-30, I6);
    M15 = DM(-31, I6);
    L0 = DM(-32, I6);
    L1 = DM(-33, I6);
    L2 = DM(-34, I6);
    L3 = DM(-35, I6);
    R3 = 7;
#ifdef DV_C3_DB
    CALL _dv_crumb (DB);
    NOP;
    NOP;
#else
    CALL _dv_crumb;                       // stage 7: the epilogue is done but for the saved R registers and L4 (I4 needs L4 = 0 until here)
#endif
    L4 = DM(-36, I6);
    L5 = DM(-37, I6);
#ifdef DV_C3_BFIRST
    B3 = DM(-24, I6);                     // a B register load also loads its I register (PRM p.6-24): B3 before I3, as the stock epilogue
#endif
    R5 = DM(-7, I6);
    R6 = DM(-8, I6);
    R7 = DM(-9, I6);
    R9 = DM(-10, I6);
    R10 = DM(-11, I6);
    R11 = DM(-12, I6);
    R13 = DM(-13, I6);
    R14 = DM(-14, I6);
    R15 = DM(-15, I6);
    I0 = DM(-16, I6);
    I1 = DM(-17, I6);
    I2 = DM(-18, I6);
    I3 = DM(-19, I6);
    I5 = DM(-20, I6);
    M1 = DM(-21, I6);
    M2 = DM(-22, I6);
    M3 = DM(-23, I6);
#ifndef DV_C3_BFIRST
    B3 = DM(-24, I6);
#endif
    I13 = DM(-25, I6);
    I12 = DM(M7, I6);                     // the return address, as the stock renderer's epilogue
    JUMP (M14, I12) (DB);
    R3 = DM(-6, I6);
    RFRAME;

_dv_stock:
    R0 = 7;
    COMP(R1, R0);
    IF LT JUMP _dv_stock_go;              // a type below 7 is one of the engine's own: no breadcrumb
    DM(_dv_sv3) = R3;                     // a type of 7 or more that no module registered: stage 3 (stock path taken) says so
    DM(_dv_sv5) = R5;
    R0 = 32;
    R0 = R0 - R14;
    R0 = LSHIFT R0 BY 8;
    R2 = LSHIFT R1 BY 16;
    R0 = R0 OR R2;
    DM(_dv_bcw) = R0;
    R3 = 3;
#ifdef DV_BISECT_C2
    // Bisect build (docs/PATCHING.md "Rung C2"): the stage 3 breadcrumb written inline, the body of _dv_crumb with no CALL and no RTS.
    // R0 is the breadcrumb word without its stage, I4 is free (L4 is 0).
    R3 = R3 OR R0;
    R5 = DM(_dv_diag + 52);
    R5 = R5 + 1;
    DM(_dv_diag + 52) = R5;               // the dispatch counter counts the stage 3 passes here (the routine is never called, the returns stay 0)
    I4 = _dv_diag;
    DM(12, I4) = R3;
    I4 = _dv_rec;
    R5 = DM(0, I4);
    R5 = PASS R5;
    IF EQ JUMP _dv_i1;
    I4 = R5;
    DM(192, I4) = R3;
    R5 = 0x11D1A6;
    DM(45, I4) = R5;
    R5 = DM(_dv_diag + 52);
    DM(193, I4) = R5;
    R5 = DM(_dv_diag + 56);
    DM(213, I4) = R5;
    I4 = _dv_rec;
_dv_i1:
    R5 = DM(1, I4);
    R5 = PASS R5;
    IF EQ JUMP _dv_i2;
    I4 = R5;
    DM(192, I4) = R3;
    R5 = 0x11D1A6;
    DM(45, I4) = R5;
    R5 = DM(_dv_diag + 52);
    DM(193, I4) = R5;
    R5 = DM(_dv_diag + 56);
    DM(213, I4) = R5;
_dv_i2:
#else
#ifdef DV_C3_DB
    CALL _dv_crumb (DB);
    NOP;
    NOP;
#else
    CALL _dv_crumb;
#endif
#endif
    R3 = DM(_dv_sv3);
    R5 = DM(_dv_sv5);
_dv_stock_go:
    R0 = DM(_dv_sv_l4);
    L4 = R0;                              // the context's L4 for the stock code
    MODIFY(I7, -26) (NW);                 // the displaced first instruction of the stock renderer
    JUMP _fw_render_next;

// The breadcrumb (docs/findings/48-dsp-breadcrumbs.md). In: R3 = stage (1 hook entered, 2 type registered, 3 stock path taken for a type of 7
// or more, 4 routine about to be called, 5 routine returned, 6 odd copy done, 7 epilogue done); the word is stage | v << 8 | type << 16
// (`_dv_bcw` holds the upper parts). Out: nothing. Clobbers R3, R5, I4 (L4 must be 0). It goes into the DSP's diagnostic word 12 (copied
// into the reply record at the end of the block by core/dsp-link) and, at once, into both reply records (the two rings; `_dv_rec` holds
// their addresses, written by core/dsp-link's block tail, 0 before the first block or without that module): a core that stops inside the
// routine never reaches the block tail, so the record is where the last breadcrumb has to be already. The header and the count of
// dispatch and return counts go with it, so that a record the engine had cleared still decodes.
_dv_crumb:
    R5 = DM(_dv_bcw);
    R3 = R3 OR R5;
    I4 = _dv_diag;
    DM(12, I4) = R3;
    I4 = _dv_rec;
    R5 = DM(0, I4);
    R5 = PASS R5;
    IF EQ JUMP _dv_cb;
    I4 = R5;
    DM(192, I4) = R3;
    R5 = 0x11D1A6;
    DM(45, I4) = R5;
    R5 = DM(_dv_diag + 52);
    DM(193, I4) = R5;
    R5 = DM(_dv_diag + 56);
    DM(213, I4) = R5;
    I4 = _dv_rec;
_dv_cb:
    R5 = DM(1, I4);
    R5 = PASS R5;
    IF EQ JUMP _dv_cc;
    I4 = R5;
    DM(192, I4) = R3;
    R5 = 0x11D1A6;
    DM(45, I4) = R5;
    R5 = DM(_dv_diag + 52);
    DM(193, I4) = R5;
    R5 = DM(_dv_diag + 56);
    DM(213, I4) = R5;
_dv_cc:
#ifdef DV_C3_DB
    RTS (DB);
    NOP;
    NOP;
#else
    RTS;
#endif

// ---- helpers for machine routines (ordinary subroutines: CALL _dv_..., return with RTS) ----

// F0 = 2 ** F0 for F0 > -64 (relative error 8e-8: a degree-5 minimax polynomial of 2^f on [0, 1) and an exact
// scale by 2^floor). Clobbers R0-R3.
.GLOBAL _dv_exp2;
_dv_exp2:
    R1 = 0x42800000;                      // 64.0
    F0 = F0 + F1;
    R1 = TRUNC F0;
    F2 = FLOAT R1;
    F0 = F0 - F2;                         // f in [0, 1)
    R2 = 0x3af61942;
    F2 = F2 * F0;
    R3 = 0x3c1347f4;
    F2 = F2 + F3;
    F2 = F2 * F0;
    R3 = 0x3d64aa27;
    F2 = F2 + F3;
    F2 = F2 * F0;
    R3 = 0x3e75ead4;
    F2 = F2 + F3;
    F2 = F2 * F0;
    R3 = 0x3f31727b;
    F2 = F2 + F3;
    F2 = F2 * F0;
    R3 = 0x3f7fffff;
    F2 = F2 + F3;
    R3 = -64;
    R1 = R3 + R1;
    F0 = SCALB F2 BY R1;
#ifdef DV_C3_DB
    RTS (DB);
    NOP;
    NOP;
#else
    RTS;
#endif

// R0 = the unsigned 16-bit word k of the DSP's frame copy (R0 = k, 0..511); _dv_frame_sw sign-extends.
// Clobbers R0-R2, I4.
.GLOBAL _dv_frame_hw;
.GLOBAL _dv_frame_sw;
_dv_frame_hw:
    R1 = 0x1fe;
    R1 = R0 AND R1;
    R1 = LSHIFT R1 BY 1;
    R2 = _fw_frame;
    R1 = R2 + R1;
    I4 = R1;
    R1 = DM(0, I4);
    R2 = FEXT R0 BY 0:1;
    R2 = LSHIFT R2 BY 4;
    R0 = 0x400;                           // len6 = 16 at Ry[11:6], bit6 below it
    R2 = R2 OR R0;
    R0 = FEXT R1 BY R2;
#ifdef DV_C3_DB
    RTS (DB);
    NOP;
    NOP;
#else
    RTS;
#endif
_dv_frame_sw:
    R1 = 0x1fe;
    R1 = R0 AND R1;
    R1 = LSHIFT R1 BY 1;
    R2 = _fw_frame;
    R1 = R2 + R1;
    I4 = R1;
    R1 = DM(0, I4);
    R2 = FEXT R0 BY 0:1;
    R2 = LSHIFT R2 BY 4;
    R0 = 0x400;                           // len6 = 16 at Ry[11:6], bit6 below it
    R2 = R2 OR R0;
    R0 = FEXT R1 BY R2;
    R0 = LSHIFT R0 BY 16;
    R1 = -16;
    R0 = ASHIFT R0 BY R1;
#ifdef DV_C3_DB
    RTS (DB);
    NOP;
    NOP;
#else
    RTS;
#endif

.SECTION/DM seg_dmda;
.PKDATA _dv_sv_l4, 4;
.PKDATA _dv_sv3, 4;
.PKDATA _dv_sv5, 4;
.PKDATA _dv_bcw, 4;
// The diagnostic block (finding 45): word 0 counts the blocks that reached the hook, words 1-11 are M5, M6, M7, M13, M14, M15,
// I6, I7, B7, L7, MODE1 as the firmware left them at the first voice's entry; word 12 is the last breadcrumb, word 13 counts the
// dispatches to registered types, word 14 the routines that returned (finding 48). core/dsp-link copies the block into the record it sends to the ColdFire each block
// (dl_diag): its words 2 to 15.
.PKDATA _dv_diag, 64;
// The addresses of the two reply records (rings): [0] the one the last block built, [1] the one before it. Written by core/dsp-link.
.PKDATA _dv_rec, 8;
