// core/dsp-link: the DSP end of the code channel (docs/findings/31-dsp-link.md; plan: docs/plans/digikit-runtime.md, Phase 5).
//
// The ColdFire writes a chunk into the tail of its per-block frame (the data halves of the last frame entries, which no
// stock code reads, finding 23 S4b). This routine runs once per block at the very end of the block handler, reads the chunk
// from the receive ring that handler worked on, checks it and copies the payload into a reserved area of free L2 (_dl_area).
// After the last chunk it invalidates the instruction caches, so that code written to the area can run. It reports progress
// in a 12-byte acknowledgement written into the record the DSP sends back to the ColdFire (offset DL_ACK_OFF).
//
// Where it runs. The hook replaces `DM(<ring toggle>) = R1`, the second last instruction of the block handler's common tail
// (every command path ends there). At that point the handler has built the whole record (the engine clears and refills the
// record each block, so an acknowledgement written earlier would be erased) and is about to store the record's first word:
//   in    I4 = the record pointer (live), R1 = the ring toggle value (live), R2 = the record's first word (live),
//         I6 = the handler's frame: DM(I6 - 3) is the pointer to the receive ring in use, DM(I6 - 2) the record pointer.
//   free  R10, R11, R13, R14, R15, I2, I3, I5: the epilogue reloads them from the frame right after.
//   saved here  R0, R3, R4, R5, R6. Not touched: R1, R2, I4, I6, I7, I12, the M registers (M5 = 0, M6 = 1 are the
//         firmware's constants, finding 06). No DO loop is open at the site of a hook that itself runs two (the epilogue's own).
// The displaced instruction runs in the patchkit tail after the final jump.
//
// Wire format of a chunk (little-endian words of the ring; the ColdFire writes the halves, low half first):
//   w0  bits 0-15 DL_MAGIC, bits 16-29 idx (chunk number, 0 for the first of a transfer), bits 30-31 win (the window)
//   w1  bits 0-16 off (destination, in words from the start of the window), bits 17-24 n (payload words),
//       bit 25 LAST, bit 26 END (stop acknowledging), bits 27-31 xid (transfer id, 1..31)
// Windows: 0 is _dl_area (DL_AREA_WORDS long); 1 is the voices registry (_dl_vrows, 2 * _dl_vcount words: one {type, pc} row is
// two words), which the digikit runtime fills one word per transfer so that the render scan never sees a half-written row.
// Both externs are 0 when core/dsp-voice is not in the image, which makes window 1 refuse every write.
//   w2 ... w(n+1)  the payload
//   w(n+2)  check = s1 XOR s2 over w0 ... w(n+1), where s1 += w and s2 += s1 for each word w (both start at 0)
// A chunk is taken when xid and magic are right, idx equals the number of chunks taken so far (cum) and the check holds.
// idx 0 with a new xid starts a transfer. A chunk with an old idx (resent) or a later idx (one was lost) is ignored.
// A bad check is counted (nerr) and the chunk is dropped; the ColdFire resends from cum. A destination or size outside
// the area stops the transfer for good (status 3) until a new xid starts.
//
// Acknowledgement (three halfwords the ColdFire reads from the record copy, in the order of the ring bytes):
//   a0  bits 0-15 DL_ACK_MAGIC, bits 16-31 cum;   a1  bits 0-7 xid, bits 8-15 status, bits 16-31 nerr
// status: 0 running, 1 done (every chunk taken, last one flagged, caches invalidated), 3 destination out of range.
// It is written in every block while a transfer is active (xid != 0): the engine clears the record each block.

#define DL_MAGIC 0xD15C
#define DL_ACK_MAGIC 0xA5D1
#define DL_ACK_OFF 0x60
#define DL_SHL1C_CFG 0x3E000

.SECTION/PM seg_pmco;
.GLOBAL _dl_hook;
.EXTERN _dl_back;
.EXTERN _dl_area;
.EXTERN _dl_vrows;
.EXTERN _dl_vcount;

_dl_hook:
    DM(_dl_sv0) = R0;
    DM(_dl_sv3) = R3;
    DM(_dl_sv4) = R4;
    DM(_dl_sv5) = R5;
    DM(_dl_sv6) = R6;
    R0 = DM(-3, I6);                      // the receive ring in use
    R3 = DL_TAIL_OFF;
    R0 = R3 + R0;
    I3 = R0;
    R10 = DM(I3, M6);                     // w0
    R3 = 0xffff;
    R4 = R10 AND R3;
    R3 = DL_MAGIC;
    COMP(R4, R3);
    IF NE JUMP _dl_ack;
    R13 = FEXT R10 BY 16:14;              // idx
    R14 = DM(I3, M6);                     // w1
    R4 = FEXT R14 BY 27:5;                // xid
    R4 = PASS R4;
    IF EQ JUMP _dl_ack;
    R3 = DM(_dl_xid);
    COMP(R4, R3);
    IF EQ JUMP _dl_same;
    // another transfer: only its first chunk (idx 0) starts it
    R5 = 0x4000000;
    R5 = R14 AND R5;                      // END of a transfer that is not the current one: nothing to do
    IF NE JUMP _dl_ack;
    R13 = PASS R13;
    IF NE JUMP _dl_ack;
    DM(_dl_xid) = R4;
    R3 = 0;
    DM(_dl_cum) = R3;
    DM(_dl_status) = R3;
    DM(_dl_nerr) = R3;
_dl_same:
    R5 = 0x4000000;
    R5 = R14 AND R5;
    IF EQ JUMP _dl_notend;
    R3 = 0;
    DM(_dl_xid) = R3;                     // END: stop acknowledging
    JUMP _dl_out;
_dl_notend:
    R3 = DM(_dl_status);
    R5 = 2;
    COMP(R3, R5);
    IF GE JUMP _dl_ack;                   // a stopped transfer stays stopped
    R3 = DM(_dl_cum);
    COMP(R13, R3);
    IF NE JUMP _dl_ack;                   // old (resent) or later (lost) chunk
    // n and off, and the limits
    R5 = FEXT R14 BY 17:8;                // n (zero extended)
    R6 = FEXT R14 BY 0:17;                // off
    R3 = DL_TAIL_WORDS - 3;
    COMP(R5, R3);
    IF GT JUMP _dl_range;
    R0 = FEXT R10 BY 30:2;                // the window
    R0 = PASS R0;
    IF NE JUMP _dl_win1;
    R3 = DL_AREA_WORDS;                   // window 0: the area
    R4 = _dl_area;
    JUMP _dl_wdone;
_dl_win1:
    R3 = 1;
    COMP(R0, R3);
    IF NE JUMP _dl_range;                 // windows 2 and 3 do not exist
    R3 = _dl_vcount;                      // window 1: the voice rows (two words each, the terminator row excluded)
    R3 = LSHIFT R3 BY 1;
    R4 = _dl_vrows;
_dl_wdone:
    DM(_dl_wbase) = R4;
    R4 = R5 + R6;
    COMP(R4, R3);
    IF GT JUMP _dl_range;
    // the check over w0, w1 and the payload
    R3 = R10;                             // s1
    R4 = R10;                             // s2
    R3 = R14 + R3;
    R4 = R3 + R4;
    R5 = PASS R5;
    IF EQ JUMP _dl_sumdone;
    R15 = I3;                             // payload start
    LCNTR = R5, DO _dl_suml UNTIL LCE;
        R0 = DM(I3, M6);
        R3 = R0 + R3;
_dl_suml: R4 = R3 + R4;
    I3 = R15;
_dl_sumdone:
    R3 = R4 XOR R3;
    // I3 = payload start; the check word follows the payload
    R0 = LSHIFT R5 BY 2;                  // 4 n bytes
    R0 = R15 + R0;
    I2 = R0;
    R0 = DM(I2, M5);
    COMP(R0, R3);
    IF EQ JUMP _dl_good;
    R3 = DM(_dl_nerr);
    R3 = R3 + 1;
    DM(_dl_nerr) = R3;
    JUMP _dl_ack;
_dl_good:
    // copy the payload to the window base + 4 off
    R0 = LSHIFT R6 BY 2;
    R3 = DM(_dl_wbase);
    R0 = R3 + R0;
    I2 = R0;
    R5 = PASS R5;
    IF EQ JUMP _dl_copied;
    LCNTR = R5, DO _dl_copyl UNTIL LCE;
        R0 = DM(I3, M6);
_dl_copyl: DM(I2, M6) = R0;
_dl_copied:
    R3 = DM(_dl_cum);
    R3 = R3 + 1;
    DM(_dl_cum) = R3;
    R5 = 0x2000000;
    R5 = R14 AND R5;                      // LAST
    IF EQ JUMP _dl_ack;
    // The code is in place: drop what the instruction caches may hold for it. FLUSH CACHE clears the instruction-conflict
    // cache; the ICAINV bit of SHL1C_CFG invalidates the L1 instruction cache (sc58x-2158x-prm, ch. 4 and 8, 33).
    FLUSH CACHE;
    R0 = DM(DL_SHL1C_CFG);
    R3 = BSET R0 BY 6;
    DM(DL_SHL1C_CFG) = R3;
    LCNTR = 40, DO _dl_wait UNTIL LCE;    // the invalidation takes about 32 cycles; keep the bit set that long
        NOP;
_dl_wait: NOP;
    DM(DL_SHL1C_CFG) = R0;                // the register as it was
    NOP; NOP; NOP; NOP; NOP; NOP; NOP; NOP; NOP; NOP; NOP; NOP;
    R3 = 1;
    DM(_dl_status) = R3;
    JUMP _dl_ack;
_dl_range:
    R3 = 3;
    DM(_dl_status) = R3;
_dl_ack:
    R3 = DM(_dl_xid);
    R3 = PASS R3;
    IF EQ JUMP _dl_out;
    R4 = I4;                              // the record pointer
    R5 = DL_ACK_OFF;
    R4 = R5 + R4;
    I2 = R4;
    R4 = DM(_dl_cum);
    R4 = LSHIFT R4 BY 16;
    R5 = DL_ACK_MAGIC;
    R4 = R5 OR R4;
    DM(I2, M6) = R4;                      // a0
    R4 = DM(_dl_status);
    R4 = LSHIFT R4 BY 8;
    R4 = R3 OR R4;
    R5 = DM(_dl_nerr);
    R5 = LSHIFT R5 BY 16;
    R4 = R5 OR R4;
    DM(I2, M6) = R4;                      // a1
_dl_out:
    R0 = DM(_dl_sv0);
    R3 = DM(_dl_sv3);
    R4 = DM(_dl_sv4);
    R5 = DM(_dl_sv5);
    R6 = DM(_dl_sv6);
    JUMP _dl_back;

.SECTION/DM seg_dmda;
.GLOBAL _dl_xid;
.GLOBAL _dl_cum;
.GLOBAL _dl_status;
.GLOBAL _dl_nerr;
.VAR _dl_xid = 0;
.VAR _dl_cum = 0;
.VAR _dl_status = 0;
.VAR _dl_nerr = 0;
.VAR _dl_wbase = 0;
.VAR _dl_sv0 = 0;
.VAR _dl_sv3 = 0;
.VAR _dl_sv4 = 0;
.VAR _dl_sv5 = 0;
.VAR _dl_sv6 = 0;
