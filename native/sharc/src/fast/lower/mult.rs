//! Floating-point multiply (the only multiplier op the fast tier lowers).
//!
//! The full form `FN = FX * FY` sets MN from the result's sign and clears
//! MV, MU, MI for results in the fast domain (finite normal or zero); the
//! sticky MUS, MVS, MIS then stay as they are. A denormal operand (the device
//! reads it as a zero) and a product below the smallest normal (the device
//! returns a zero and sets MU and MUS) leave the kernel. The short form
//! `FN = FX * FY` (2a_short, 2c) sets them like the full form (2a).

use super::*;
use crate::fast::FlagKind;

impl Lower {
    pub fn fmul(&mut self, rn: u32, rx: u32, ry: u32, forget_flags: bool) -> LR<()> {
        let x = self.rd_f_op(rx)?;
        let y = self.rd_f_op(ry)?;
        let r = self.bin(Bin::FMul, x, y);
        result_no_underflow(self, x, y, r);
        self.wr_f(rn, r)?;
        if forget_flags {
            self.pend_flag_none(FlagKind::FmulForget);
        } else {
            self.pend_flag(FlagKind::Fmul, vec![r]);
        }
        Ok(())
    }

    /// `RN = RX * RY (SSI)`: the low word of the signed product. A product
    /// that does not fit 32 bits sets MV (and the sticky MOS), so it exits;
    /// otherwise MN is the sign and MV MU MI are cleared.
    pub fn mul_ssi(&mut self, rn: u32, rx: u32, ry: u32) -> LR<()> {
        let a = self.rd_i(rx)?;
        let b = self.rd_i(ry)?;
        let lo = self.bin(Bin::Mul, a, b);
        let hi = self.bin(Bin::MulHs, a, b);
        let k31 = self.ci(31);
        let sign = self.bin(Bin::ShrS, lo, k31);
        let fits = self.bin(Bin::Eq, hi, sign);
        self.guard(fits);
        self.wr_i(rn, lo)?;
        self.pend_flag(FlagKind::Fmul, vec![lo]);
        Ok(())
    }

    /// Short `RN = RN * RX` (ssi): the same signed multiply as the full
    /// form, with its exact MN MV MU MI (the core runs `MULTIPLY_SSI_RN`).
    pub fn mul_short(&mut self, rn: u32, rx: u32) -> LR<()> {
        self.mul_ssi(rn, rn, rx)
    }
}
