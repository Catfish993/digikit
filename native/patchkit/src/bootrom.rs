//! The SHARC+ boot ROM's rules for a boot stream (firmware section 7), as far as the public ADSP-2156x
//! Hardware Reference states them (chapter 40, "Boot Loader Stream"). A port of the error rules of
//! `tools/sharc_bootcheck.py` (rule ids H1..H12, S1 are the same; that file cites each rule). The
//! emulator never runs the ROM, so this is what stands in for "the ROM would reject it".

use crate::error::*;

const HEADER: usize = 16;
const FILL: u32 = 1 << 8;
const CALLBACK: u32 = 1 << 10;
const INIT: u32 = 1 << 11;
const IGNORE: u32 = 1 << 12;
const INDIRECT: u32 = 1 << 13;
const FIRST: u32 = 1 << 14;
const FINAL: u32 = 1 << 15;
/// HDRSIGN of a block for core 0.
const CORE0: u8 = 0xAD;
/// The boot kernel's 8 KB workspace, not a bootable region (HWR 40-2, Table 40-1 and note).
pub const ROM_WORKSPACE: (u32, u32) = (0x200F_E000, 0x2010_0000);
/// The ColdFire boot routine sends at most this many depacked bytes.
const MAX_SECTION: usize = 0xF_FFFF;

/// Every rule the stream breaks, as "rule @offset: text". Empty when the ROM would accept it.
pub fn check(blob: &[u8]) -> Vec<String> {
    let mut out = Vec::new();
    let mut off = 0usize;
    let mut index = 0usize;
    let mut firsts: Vec<(usize, u32)> = Vec::new();
    let mut saw_final = false;
    let mut stopped = false;
    while off < blob.len() {
        if off + HEADER > blob.len() {
            out.push(format!(
                "H8 @{off:#x}: {} bytes left, a header needs 16",
                blob.len() - off
            ));
            stopped = true;
            break;
        }
        let h = &blob[off..off + HEADER];
        let word = |i: usize| u32::from_le_bytes(h[i * 4..i * 4 + 4].try_into().unwrap());
        let (code, target, count, argument) = (word(0), word(1), word(2), word(3));
        if h[3] != CORE0 {
            out.push(format!(
                "H1 @{off:#x}: HDRSIGN {:#04x}, core 0 is 0xad",
                h[3]
            ));
            stopped = true;
            break;
        }
        if h.iter().fold(0u8, |a, &b| a ^ b) != 0 {
            out.push(format!("H2 @{off:#x}: header XOR is not 0"));
            stopped = true;
            break;
        }
        let fill = code & FILL != 0;
        let ignore = code & IGNORE != 0;
        let mut effective = code;
        if ignore {
            effective &= !(INIT | CALLBACK | FINAL | (1 << 5));
        }
        let loads = !ignore && count > 0;
        let at = format!("block {index} {target:#x}+{count:#x}");
        if index == 0 && code & FIRST == 0 {
            out.push(format!(
                "H4 @{off:#x}: {at}: the first block lacks BFLAG_FIRST"
            ));
        }
        if count % 4 != 0 || (loads && target % 4 != 0) {
            out.push(format!(
                "H3 @{off:#x}: {at}: count or load address not a multiple of 4"
            ));
        }
        if fill && code & FIRST != 0 {
            out.push(format!("H5 @{off:#x}: {at}: FIRST combined with FILL"));
        }
        if code & (INDIRECT | CALLBACK) != 0 {
            out.push(format!(
                "H10 @{off:#x}: {at}: INDIRECT/CALLBACK not modelled"
            ));
        }
        if loads
            && target < ROM_WORKSPACE.1
            && target as u64 + count as u64 > ROM_WORKSPACE.0 as u64
        {
            out.push(format!(
                "H9 @{off:#x}: {at}: inside the boot kernel workspace 0x200fe000..0x20100000"
            ));
        }
        let end = off + HEADER + if fill { 0 } else { count as usize };
        if end > blob.len() {
            out.push(format!("H8 @{off:#x}: {at}: payload runs past the stream"));
            stopped = true;
            break;
        }
        if code & FIRST != 0 {
            firsts.push((off, argument));
        }
        off = end;
        index += 1;
        if effective & FINAL != 0 {
            saw_final = true;
            break;
        }
    }
    if !saw_final && !stopped {
        out.push(format!("H8 @{off:#x}: no FINAL block"));
    }
    if saw_final && off < blob.len() {
        out.push(format!(
            "H8 @{off:#x}: {} bytes after FINAL",
            blob.len() - off
        ));
    }
    for (n, &(at, arg)) in firsts.iter().enumerate() {
        let next = firsts.get(n + 1).map_or(blob.len(), |&(o, _)| o);
        let want = next.saturating_sub(at + HEADER);
        if arg as usize != want {
            out.push(format!(
                "H12 @{at:#x}: FIRST ARGUMENT {arg:#x}, the next application is {want:#x} bytes on"
            ));
        }
    }
    if blob.len() > MAX_SECTION {
        out.push(format!(
            "S1 @0: {:#x} bytes, the ColdFire sends at most {MAX_SECTION:#x}",
            blob.len()
        ));
    }
    out
}

/// `Err` listing the broken rules, for callers that refuse such a stream.
pub fn require(blob: &[u8]) -> Result<()> {
    let found = check(blob);
    if found.is_empty() {
        Ok(())
    } else {
        Err(err!(
            "the SHARC boot ROM would reject this DSP stream: {}",
            found.join("; ")
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hdr(target: u32, count: u32, flags: u32, argument: u32) -> Vec<u8> {
        let mut h = Vec::new();
        for w in [0xAD00_0001 | flags, target, count, argument] {
            h.extend_from_slice(&w.to_le_bytes());
        }
        h[2] = h.iter().fold(0u8, |a, &b| a ^ b);
        h
    }

    /// An initcode application, then a main one ending in FINAL, like the real streams.
    fn stock_like() -> Vec<u8> {
        let mut a = hdr(0x120230, 0, FIRST | IGNORE, 0);
        a.extend(hdr(0x2824_0000, 16, 0, 0));
        a.extend([0u8; 16]);
        a.extend(hdr(0x120230, 0, INIT, 0));
        let arg = (a.len() - HEADER) as u32;
        a.splice(0..HEADER, hdr(0x120230, 0, FIRST | IGNORE, arg));
        let mut body = hdr(0x2000_0000, 16, 0, 0);
        body.extend(0..16u8);
        body.extend(hdr(0x8000_0000, 0x40, FILL, 0xDEAD_BEEF));
        let mut out = a;
        out.extend(hdr(
            0x1C_1338,
            0,
            FIRST | IGNORE,
            (body.len() + HEADER) as u32,
        ));
        out.extend(body);
        out.extend(hdr(0x1C_1338, 0, FINAL, 0));
        out
    }

    fn rules(blob: &[u8]) -> Vec<String> {
        check(blob)
            .iter()
            .map(|s| s.split(' ').next().unwrap().to_string())
            .collect()
    }

    #[test]
    fn synthetic_stock_passes() {
        assert_eq!(check(&stock_like()), Vec::<String>::new());
    }

    #[test]
    fn bad_headers_and_alignment() {
        let s = stock_like();
        let mut b = s.clone();
        b[3] = 0xAC;
        assert_eq!(rules(&b), vec!["H1".to_string()]);
        let mut b = s.clone();
        b[2] ^= 0x10;
        assert_eq!(rules(&b)[0], "H2");
        let at = s.len() - HEADER;
        let mut b = s[..at].to_vec();
        b.extend(hdr(0x2000_0002, 16, 0, 0));
        b.extend([0u8; 16]);
        b.extend(&s[at..]);
        assert!(rules(&b).contains(&"H3".to_string()));
    }

    #[test]
    fn final_workspace_and_next_pointer() {
        let s = stock_like();
        assert!(rules(&s[..s.len() - HEADER]).contains(&"H8".to_string()));
        let mut b = s.clone();
        b.extend([0u8; 16]);
        assert!(rules(&b).contains(&"H8".to_string()));
        // an inserted block without the FIRST pointer growing
        let at = s.len() - HEADER;
        let mut ins = s[..at].to_vec();
        ins.extend(hdr(0x2001_0000, 8, 0, 0));
        ins.extend([0u8; 8]);
        ins.extend(&s[at..]);
        assert_eq!(rules(&ins), vec!["H12".to_string()]);
        // in the workspace
        let mut ws = s[..at].to_vec();
        ws.extend(hdr(0x200F_E000, 8, 0, 0));
        ws.extend([0u8; 8]);
        ws.extend(&s[at..]);
        assert!(rules(&ws).contains(&"H9".to_string()));
    }

    #[test]
    fn indirect_and_callback_are_refused() {
        let s = stock_like();
        let at = s.len() - HEADER;
        for flag in [INDIRECT, CALLBACK] {
            let mut b = s[..at].to_vec();
            b.extend(hdr(0x2001_0000, 4, flag, 0));
            b.extend([0u8; 4]);
            b.extend(&s[at..]);
            assert!(rules(&b).contains(&"H10".to_string()));
        }
    }

    #[test]
    fn append_block_keeps_the_stream_acceptable_and_avoids_the_workspace() {
        use crate::dsp::Stream;
        let mut st = Stream::parse(stock_like()).unwrap();
        st.append_block(0x2001_0000, &[7u8; 100]).unwrap();
        st.append_block(0x2002_0000, &[1u8; 7]).unwrap();
        assert_eq!(check(&st.blob), Vec::<String>::new());
        assert!(st.append_block(0x200F_E000, &[0u8; 4]).is_err());
        assert!(st.append_block(0x200F_DFFC, &[0u8; 8]).is_err());
        st.append_block(0x200F_DFFC, &[0u8; 4]).unwrap();
        assert_eq!(check(&st.blob), Vec::<String>::new());
    }
}
