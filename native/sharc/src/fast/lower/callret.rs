//! The forms of a compiler call and return sequence (stack model): the CJUMP
//! frame move, the immediate store that pushes the return address in the
//! delay slot of a call (16a), and RFRAME in the delay slot of a return
//! (25c_rframe).
//!
//! `tools/sharc_core/forms_flow.py::_type_25a_direct`, `forms_move.py::_type_16a`
//! and `forms_flow.py::_type_25c_rframe` are the definitions. The memory
//! accesses are the plain-RAM normal-word transfers the other memory forms
//! lower (the addresses lie where a normal-word access is not mapped, a
//! requirement checked once at entry), so windows, guards and index updates
//! are `mem_addr`'s and `mem_imm`'s.

use super::mem_addr::Xfer;
use super::*;

const R2: u32 = 2;
const I6: u32 = 22;
const I7: u32 = 23;

impl Lower {
    /// Register C (an index register) is written by a plain move.
    fn plain_write(&mut self, c: u32) {
        self.plain_out |= 1 << c;
    }

    /// CJUMP's register effect (25a_direct with the stack model on, where
    /// it is no call and pushes no PC stack entry): `R2 = I6; I6 = I7`, both
    /// read before the instruction.
    pub fn cjump_frame(&mut self) -> LR<()> {
        let i6 = self.rd_bits(I6)?;
        let i7 = self.rd_bits(I7)?;
        self.wr(R2, i6)?;
        self.wr(I6, i7)?;
        self.plain_write(I6);
        self.commit()
    }

    /// 16a: `DM(Ii, Mm) = imm32` with a post-modify of Ii by Mm. The `by` and
    /// `sl` variants and the PM space (g) are refused.
    pub fn form_16a(&mut self, d: &Dec) -> LR<()> {
        if d.field("by") != Some(0) || d.field("sl") != Some(0) {
            return refuse("16a by/sl");
        }
        if d.field("g") != Some(0) {
            return refuse("16a in PM space");
        }
        if !self.env.assume_nw32 {
            return refuse("assume_nw32 off");
        }
        let i = d.field("i").ok_or(Refuse("16a i".into()))? as u32;
        let m = d.field("m").ok_or(Refuse("16a m".into()))? as u32;
        let value = d.wide("data").ok_or(Refuse("16a data".into()))?;
        let (ic, mc) = (16 + i, 32 + m);
        let iv = self.rd_i(ic)?;
        let mv = self.rd_i(mc)?;
        let four = self.ci(4);
        let scaled = self.bin(Bin::Mul, mv, four);
        let modified = self.bin(Bin::Add, iv, scaled);
        self.require_plain(iv, 4)?;
        let v = self.ci(value);
        let (p, o) = self.window_access(iv, 4, true)?;
        self.pend_store(p, o, v);
        self.post_update(ic, iv, scaled, modified)
    }

    /// 25c_rframe: `I7 = I6; I6 = DM(I6)` (the 32-bit word at the byte
    /// address I6).
    pub fn form_25c(&mut self) -> LR<()> {
        let frame = self.rd_i(I6)?;
        self.wr_i(I7, frame)?;
        if !self.tail_write {
            self.plain_write(I7);
            self.plain_write(I6);
        }
        self.dm_imm(I6, 0, false, Xfer::Load(I6))
    }
}
