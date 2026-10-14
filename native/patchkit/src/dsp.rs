//! The SHARC+ boot stream (container section 7): a chain of 16-byte block headers (code, target address,
//! byte count, argument; little endian, XOR of the 16 bytes zero) each followed by its payload unless the
//! FILL flag is set. Port of the parts of `tools/sharc_dev/sharcldr.py` and `tools/patchkit/dsp_patch.py` (`Stream`) that
//! patching needs: reads with last-writer-wins, in-place writes, and `append_block`.

use crate::error::*;

pub const HEADER_LEN: usize = 16;
const FILL_BIT: u32 = 1 << 8;
const FINAL_BIT: u32 = 1 << 15;
pub const SW_ALIAS_BASE: u32 = 0x2800_0000;
pub const L2_BYTE_BASE: u32 = 0x2000_0000;
pub const L2_BYTE_LIMIT: u32 = 0x2010_0000;
/// First byte of the boot ROM's own workspace (HWR 40-2): L2 up to here is bootable (`bootrom::ROM_WORKSPACE`).
pub const L2_BOOT_LIMIT: u32 = crate::bootrom::ROM_WORKSPACE.0;
/// L2 code is addressed by short-word pc from here (pc = this + (byte - L2_BYTE_BASE) / 2).
pub const L2_SW_BASE: u32 = 0x00B8_0000;
/// The ColdFire boot routine sends at most this many depacked bytes.
pub const MAX_STREAM: usize = 0xF_FFFF;

#[derive(Clone, Debug)]
pub struct Block {
    pub offset: usize,
    pub code: u32,
    pub target: u32,
    pub count: u32,
    pub argument: u32,
    pub payload_offset: usize,
}

impl Block {
    pub fn fill(&self) -> bool {
        self.code & FILL_BIT != 0
    }
    pub fn is_final(&self) -> bool {
        self.code & FINAL_BIT != 0
    }
    fn covers(&self, a: u32) -> bool {
        self.count != 0 && self.target <= a && (a - self.target) < self.count
    }
}

#[derive(Clone, Debug)]
pub struct Stream {
    pub blob: Vec<u8>,
    pub blocks: Vec<Block>,
}

fn u32le(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(b[at..at + 4].try_into().unwrap())
}

/// A block header: flags 1 (plain data), core 0; byte 2 makes the XOR of all 16 bytes zero.
pub fn block_header(target: u32, count: u32) -> [u8; 16] {
    let mut raw = [0u8; 16];
    raw[0..4].copy_from_slice(&(0xAD00_0001u32).to_le_bytes());
    raw[4..8].copy_from_slice(&target.to_le_bytes());
    raw[8..12].copy_from_slice(&count.to_le_bytes());
    let mut x = 0u8;
    for b in raw {
        x ^= b;
    }
    raw[2] = x;
    raw
}

impl Stream {
    /// Parse the whole blob; every byte must belong to a valid block.
    pub fn parse(blob: Vec<u8>) -> Result<Stream> {
        let mut blocks = Vec::new();
        let mut off = 0usize;
        while off + HEADER_LEN <= blob.len() {
            let h = &blob[off..off + HEADER_LEN];
            let x = h.iter().fold(0u8, |a, &b| a ^ b);
            if x != 0 || !matches!(h[3], 0xAB..=0xAD) {
                break;
            }
            let code = u32le(h, 0);
            let b = Block {
                offset: off,
                code,
                target: u32le(h, 4),
                count: u32le(h, 8),
                argument: u32le(h, 12),
                payload_offset: off + HEADER_LEN,
            };
            let plen = if b.fill() { 0 } else { b.count as usize };
            if b.payload_offset + plen > blob.len() {
                break;
            }
            off = b.payload_offset + plen;
            blocks.push(b);
        }
        if off != blob.len() {
            return Err(err!(
                "DSP boot stream: block chain ends at {off:#x} of {:#x} bytes",
                blob.len()
            ));
        }
        Ok(Stream { blob, blocks })
    }

    /// The last block (in stream order) that writes loader byte address `a`.
    fn owner(&self, a: u32) -> Option<&Block> {
        self.blocks.iter().rev().find(|b| b.covers(a))
    }

    pub fn present(&self, a: u32) -> bool {
        self.owner(a).is_some()
    }

    fn byte_at(&self, a: u32) -> Option<u8> {
        let b = self.owner(a)?;
        let rel = (a - b.target) as usize;
        Some(if b.fill() {
            b.argument.to_le_bytes()[rel % 4]
        } else {
            self.blob[b.payload_offset + rel]
        })
    }

    /// `n` final loaded bytes from `a`, or None when any byte is not loaded.
    pub fn read(&self, a: u32, n: usize) -> Option<Vec<u8>> {
        (0..n as u32).map(|k| self.byte_at(a + k)).collect()
    }

    /// Write bytes the stream stores (FILL blocks have no stored bytes to change).
    pub fn write(&mut self, a: u32, data: &[u8]) -> Result<()> {
        let mut offs = Vec::with_capacity(data.len());
        for k in 0..data.len() as u32 {
            let b = self
                .owner(a + k)
                .ok_or_else(|| err!("{:#x}: the boot stream does not load this address", a + k))?;
            if b.fill() {
                return Err(err!(
                    "{:#x}: a FILL block loads this address (no stored bytes)",
                    a + k
                ));
            }
            offs.push(b.payload_offset + (a + k - b.target) as usize);
        }
        for (o, &v) in offs.iter().zip(data) {
            self.blob[*o] = v;
        }
        Ok(())
    }

    /// Insert a plain data block at `addr` before the FINAL block. The payload is padded to 4 bytes.
    /// Refused: outside L2, over loaded memory, an address not a multiple of 4, a stream over `MAX_STREAM`.
    pub fn append_block(&mut self, addr: u32, data: &[u8]) -> Result<()> {
        if data.is_empty() {
            return Err(err!("APPEND: empty block"));
        }
        if !addr.is_multiple_of(4) {
            return Err(err!("APPEND {addr:#x}: the address is not a multiple of 4"));
        }
        let mut payload = data.to_vec();
        payload.resize(data.len().div_ceil(4) * 4, 0);
        let end = addr as u64 + payload.len() as u64;
        if addr < L2_BYTE_BASE || end > L2_BOOT_LIMIT as u64 {
            return Err(err!(
                "APPEND {addr:#x}..{end:#x}: outside bootable L2 {L2_BYTE_BASE:#x}..{L2_BOOT_LIMIT:#x}"
            ));
        }
        for a in (addr..end as u32).step_by(4) {
            if self.present(a) || self.present(a + 3) {
                return Err(err!(
                    "APPEND {a:#x}: the boot stream already loads this address"
                ));
            }
        }
        let fin = self
            .blocks
            .iter()
            .rev()
            .find(|b| b.is_final())
            .ok_or_else(|| err!("the boot stream has no FINAL block"))?
            .offset;
        let mut block = block_header(addr, payload.len() as u32).to_vec();
        block.extend_from_slice(&payload);
        if self.blob.len() + block.len() > MAX_STREAM {
            return Err(err!(
                "APPEND: the stream would be {:#x} bytes, more than the {MAX_STREAM:#x} the ColdFire boot routine sends",
                self.blob.len() + block.len()
            ));
        }
        let grown = block.len() as u32;
        self.blob.splice(fin..fin, block);
        // Each FIRST block's ARGUMENT is the byte count to the next application (or the end of the stream;
        // HWR 40-85 pNextDxe, bootrom rule H12): grow the ones that span the insertion point.
        for b in &self.blocks {
            if b.code & (1 << 14) != 0
                && b.offset < fin
                && fin <= b.offset + HEADER_LEN + b.argument as usize
            {
                let o = b.offset;
                let h = &mut self.blob[o..o + HEADER_LEN];
                h[12..16].copy_from_slice(&(b.argument + grown).to_le_bytes());
                h[2] = 0;
                h[2] = h.iter().fold(0u8, |a, &x| a ^ x);
            }
        }
        *self = Stream::parse(std::mem::take(&mut self.blob))?;
        Ok(())
    }
}

/// Loader byte address of short-word pc (the 0x20000000 L2 alias for the 0xb80000 window when the low alias
/// is empty), as `tools/patchkit/dsp_patch.py pc_address`.
pub fn pc_to_byte(pc: u32, stream: &Stream) -> u32 {
    let low = SW_ALIAS_BASE + pc * 2;
    if (0xB8_0000..0xC0_0000).contains(&pc) && !stream.present(low) {
        L2_BYTE_BASE + (pc - L2_SW_BASE) * 2
    } else {
        low
    }
}

/// Short-word pc of an L2 byte address.
pub fn l2_byte_to_pc(byte: u32) -> u32 {
    L2_SW_BASE + (byte - L2_BYTE_BASE) / 2
}
