//! The primitives generated forms call (`gen_forms.rs`, written by
//! `tools/sharc_lowergen.py` from `tools/sharc_core`).
//!
//! A generated form is the decision tree the core's handler makes over the
//! instruction's fields and MODE1, with the effects of each leaf in program
//! order. This file holds what those effects mean for the kernel: it reuses
//! the lowering's window, anchor, circular-buffer and guard logic
//! (`mem_addr`, `Lower::post_value`) and keeps the bookkeeping the hand-written
//! forms do (register moves for the anchor hint, linear index writes).
//! Nothing here is specific to a form.

use super::mem_addr::{Width, Xfer};
use super::shift::shiftimm_field;
use super::sysops::MODE1_CODE;
use super::*;

/// MODE1's register-bank selection bits (`state._BANK_MASK`).
const BANK_BITS: u32 = 0x4F8;

/// Where a value came from, for anchors and moves.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Tag {
    /// The register (UREG code below `NREGS`) the value is derived from.
    pub src: u8,
    /// The value of that register read at the start of the instruction.
    pub iv: Val,
    pub kind: TagKind,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TagKind {
    /// The register itself.
    Rd,
    /// The register plus something (an address, a MODIFY result).
    Add,
    /// The post-modify update of the register.
    Post,
}

impl Lower {
    fn tag_of(&self, v: Val) -> Option<Tag> {
        self.gtag.get(v as usize).copied().flatten()
    }

    fn set_tag(&mut self, v: Val, t: Tag) {
        if self.gtag.len() <= v as usize {
            self.gtag.resize(v as usize + 1, None);
        }
        self.gtag[v as usize] = Some(t);
    }

    /// MODE1 bits MASK as the lowering knows them here; the region is made
    /// for those bits.
    pub fn m1(&mut self, mask: u32) -> u32 {
        self.mode1_mask |= mask;
        self.mode1_cur & mask
    }

    pub fn g_bin(&mut self, b: Bin, x: Val, y: Val) -> Val {
        // Both operands constants of the lowering: the result is one too.
        if let (Some(p), Some(q)) = (self.konst_of(x), self.konst_of(y)) {
            let (p, q) = (p as u32, q as u32);
            let k = match b {
                Bin::Add => Some(p.wrapping_add(q)),
                Bin::Sub => Some(p.wrapping_sub(q)),
                Bin::Mul => Some(p.wrapping_mul(q)),
                Bin::And => Some(p & q),
                Bin::Or => Some(p | q),
                Bin::Xor => Some(p ^ q),
                Bin::Shl => Some(p.wrapping_shl(q)),
                Bin::ShrU => Some(p.wrapping_shr(q)),
                _ => None,
            };
            if let Some(k) = k {
                return self.ci(k);
            }
        }
        let r = self.bin(b, x, y);
        if b == Bin::Add
            && let Some(t) = self.tag_of(x).or_else(|| self.tag_of(y))
            && t.kind != TagKind::Post
        {
            self.set_tag(
                r,
                Tag {
                    kind: TagKind::Add,
                    ..t
                },
            );
        }
        r
    }

    pub fn g_rd(&mut self, code: u32) -> LR<Val> {
        let v = if (16..48).contains(&code) {
            self.rd_i(code)?
        } else {
            self.rd_bits(code)?
        };
        if code < NREGS {
            self.set_tag(
                v,
                Tag {
                    src: code as u8,
                    iv: v,
                    kind: TagKind::Rd,
                },
            );
        }
        Ok(v)
    }

    pub fn g_wr(&mut self, code: u32, v: Val) -> LR<()> {
        if code == MODE1_CODE {
            return self.g_wr_mode1(v);
        }
        if code < NREGS {
            if let Some(t) = self.tag_of(v) {
                match t.kind {
                    // Writing a register back unchanged is no write.
                    TagKind::Rd if t.src as u32 == code => return Ok(()),
                    TagKind::Rd if t.src as u32 != code => {
                        self.moves.push((code as u8, t.src));
                    }
                    TagKind::Add if (16..32).contains(&code) && (16..32).contains(&(t.src as u32)) => {
                        // `Ia = MODIFY(Ib, ...)` as the interpreter does it: a
                        // plain sum, so neither index register may be circular.
                        self.require_linear(t.src as u32)?;
                        self.require_linear(code)?;
                        self.note_modify(t.src as u32, code);
                    }
                    _ => {}
                }
            }
            if (16..48).contains(&code) {
                return self.wr_i(code, v);
            }
        }
        self.wr(code, v)
    }

    /// A write of MODE1: the new value must be a constant of the lowering.
    /// The kernel then runs on with MODE1 changed (`mode1_cur`, static) and
    /// writes the register like any other (the region's exit state).
    fn g_wr_mode1(&mut self, v: Val) -> LR<()> {
        let Some(k) = self.konst_of(v) else {
            return refuse("MODE1 written with a run-time value");
        };
        let k = k as u32;
        if self.env.bank_model && (k ^ self.mode1_cur) & BANK_BITS != 0 {
            // A write that changes the bank selection starts a delayed one.
            return refuse("MODE1 write changes the register bank selection");
        }
        self.bank_pair()?;
        if k == self.mode1_cur {
            return Ok(());
        }
        if self.cond.is_some() {
            return refuse("conditional MODE1 write");
        }
        self.pending.writes.push((MODE1_CODE as u8, v));
        self.mode1_cur = k;
        self.mode1_mask = u32::MAX;
        Ok(())
    }

    fn index_anchor(&self, addr: Val) -> Option<(u32, Val, bool)> {
        let t = self.tag_of(addr)?;
        if !(16..32).contains(&(t.src as u32)) || t.kind == TagKind::Post {
            return None;
        }
        Some((t.src as u32, t.iv, addr == t.iv))
    }

    /// A load of BYTES bytes (4: a normal word) at byte address ADDR.
    pub fn g_load(&mut self, addr: Val, bytes: u8, signed: bool) -> LR<Val> {
        let w = Width { bytes, signed };
        self.xout = None;
        match self.index_anchor(addr) {
            Some((ic, iv, post)) => self.xfer_at(ic, iv, addr, post, Xfer::LoadV, w)?,
            None => self.mem_xfer_w(addr, Xfer::LoadV, w)?,
        }
        self.xout.take().ok_or(Refuse("load produced no value".into()))
    }

    pub fn g_store(&mut self, addr: Val, v: Val, bytes: u8) -> LR<()> {
        let w = Width {
            bytes,
            signed: false,
        };
        match self.index_anchor(addr) {
            Some((ic, iv, post)) => self.xfer_at(ic, iv, addr, post, Xfer::StoreV(v), w),
            None => self.mem_xfer_w(addr, Xfer::StoreV(v), w),
        }
    }

    /// The post-modify update of index register IC (a UREG code 16..31) by
    /// DELTA (already scaled), IV its value: the new register value.
    pub fn g_postmod(&mut self, ic: u32, iv: Val, delta: Val, scale: i64) -> LR<Val> {
        if !(16..32).contains(&ic) {
            return refuse("post-modify of a non-index register");
        }
        let modified = self.bin(Bin::Add, iv, delta);
        let v = self.post_value(ic, iv, delta, modified, scale)?;
        self.set_tag(
            v,
            Tag {
                src: ic as u8,
                iv,
                kind: TagKind::Post,
            },
        );
        Ok(v)
    }

    /// The instruction's execution condition (PEx only).
    pub fn g_cond(&mut self, cond: u32, y: bool) -> LR<()> {
        if y {
            return refuse("PEy condition");
        }
        let cv = self.cond_value(cond)?;
        self.cond = Some(cv);
        Ok(())
    }

    /// Apply the instruction's compute (`kind` "compute") or shift
    /// immediate ("shiftimm") on PEx, or on PEy (SIMD).
    pub fn g_apply(&mut self, d: &Dec, kind: &str, y: bool, short: bool) -> LR<()> {
        if y {
            self.pe_off = 80;
            let r = self.g_apply_pe(d, kind, short);
            self.pe_off = 0;
            return r;
        }
        self.g_apply_pe(d, kind, short)
    }

    fn g_apply_pe(&mut self, d: &Dec, kind: &str, short: bool) -> LR<()> {
        match kind {
            "compute" if short => {
                let f = d.field("compute").ok_or(Refuse("2c compute".into()))? as u32;
                self.compute_short(f)
            }
            "compute" => {
                let f = d.compute().ok_or(Refuse("compute field".into()))?;
                self.compute_full(f)
            }
            "shiftimm" => {
                let f = shiftimm_field(d)?;
                let dataex = d.field("dataex").ok_or(Refuse("dataex".into()))? as u32;
                self.shiftimm(f, dataex)
            }
            other => refuse(format!("apply {other}")),
        }
    }

    /// Lower a decoded instruction with the generated form of its type, if
    /// there is one. `Ok(false)`: the form is not generated.
    pub fn gen_lower(&mut self, d: &Dec) -> LR<bool> {
        gen_forms::dispatch(self, d)
    }
}
