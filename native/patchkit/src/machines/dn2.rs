//! `core/machines` on the Digitone II (1.11, 1.12): the parts that differ from the Digitakt II framework.
//!
//! The Digitone II has five machine types (FM TONE, WAVETONE, FM DRUM, SWARMER, MIDI) and the same machine
//! machinery as the Digitakt II -- descriptor dispatch, list builder, name accessors, permission check, persistence
//! immediate (finding 02, "Digitone II 1.11 has the same machine machinery") -- which `super::generate` handles
//! unchanged. What is different, handled here:
//!
//! * **group**: the UI group helper has another shape (types 0..3 group 1, 4 group 2, anything else 0), so the added
//!   types are sent to group 1 by a shim in front of its second test;
//! * **no sort comparator, per-type byte table, clone tests or DSP selector table** (the Digitakt parts are skipped);
//! * **type maps**: three functions index per-type tables built at start-up (page count and page descriptors of a
//!   type, parameter code of a (parameter, type) pair) and fall back to MIDI's row for a type above 4. A shim in
//!   front of each maps an added type to its `clone_of` before the table is read, so a clone of FM TONE gets FM TONE's
//!   pages, parameter codes and parameter slots.

use super::*;

/// Type-map sites: (anchor, offset of the stack argument that holds the type).
pub const TYPEMAPS: [(&str, u16); 3] = [
    ("cf.machines.typemap.0", 0x4),
    ("cf.machines.typemap.1", 0x8),
    ("cf.machines.typemap.2", 0xc),
];

pub fn anchor_names() -> Vec<String> {
    let mut v: Vec<String> = [
        "dispatch",
        "list",
        "list_alloc",
        "group",
        "permit",
        "persist",
    ]
    .iter()
    .map(|n| format!("cf.machines.{n}"))
    .collect();
    v.extend((0..NAME_ACCESSORS).map(|i| format!("cf.machines.name.{i}")));
    v.extend(TYPEMAPS.iter().map(|(n, _)| n.to_string()));
    v.extend((0..READER_REAL_CALLS).map(|i| format!("cf.machines.reader_call.{i}")));
    v
}

/// Call sites of the type reader (`FUN_4004b7f2`) that must see the machine's own type (list stepping, the name of
/// the machine, sound snapshots, preset and clear): the shim of the reader leaves the type alone for them.
pub const READER_REAL_CALLS: usize = 9;

/// `moveq #3,D1; cmp.l D0,D1; bge.b group1` at the second instruction of the group helper (D0 = type >= 0).
pub(super) fn group(
    port: &Port,
    read: Reader,
    new_last: u8,
    patches: &mut Vec<GenPatch>,
    asm: &mut Asm,
) -> Result<()> {
    let g = site(port, read, "cf.machines.group")?;
    expect("group test", &g.bytes[0..4], &[0x72, 0x03, 0xb2, 0x80])?;
    if g.bytes[4] != 0x6c || g.bytes.len() != 6 {
        return Err(err!("machines: group: expected bge.b after the compare"));
    }
    let group1 = (g.addr as i64 + 4 + 2 + g.bytes[5] as i8 as i64) as u32;
    let tail = (g.addr + 6) as u32; // the stock code that tells MIDI (group 2) from anything else (group 0)
    asm.align(2);
    asm.sym("machfw_group");
    asm.moveq(1, 3);
    asm.w16(0xb280); // cmp.l D0,D1
    asm.bcc_w(0xc, "g1"); // type <= 3: group 1
    asm.moveq(1, 4);
    asm.w16(0xb280);
    asm.bcc_w(0xc, "tail"); // type <= 4: the stock code
    asm.moveq(1, new_last as i8);
    asm.w16(0xb280);
    asm.bcc_w(0xd, "tail"); // type > last added: the stock code (group 0)
    asm.label("g1");
    asm.jmp_const(group1);
    asm.label("tail");
    asm.jmp_const(tail);
    patches.push(GenPatch {
        anchor: g.name.clone(),
        at: 0,
        bytes: vec![0x4e, 0xf9, 0, 0, 0, 0],
        relocs: vec![rel(2, "machfw_group", 0)],
        what: "group shim".into(),
    });
    Ok(())
}

/// Map the added types to their `clone_of` right after the type argument is loaded from the stack. The replayed
/// bytes are loads from the stack and `moveq` only (no program counter, no branch).
pub(super) fn typemaps(
    port: &Port,
    read: Reader,
    defs: &[Declared],
    count: u32,
    asm: &mut Asm,
    patches: &mut Vec<GenPatch>,
) -> Result<()> {
    for (i, (name, arg)) in TYPEMAPS.iter().enumerate() {
        let s = site(port, read, name)?;
        let mut k = 0;
        let mut reg: Option<u16> = None;
        while k < s.bytes.len() {
            let w = u16::from_be_bytes([s.bytes[k], s.bytes[k + 1]]);
            if w & 0xf100 == 0x7000 {
                k += 2; // moveq #imm,Dn
            } else if w & 0xf1ff == 0x202f || w & 0xf1ff == 0x206f {
                // move.l d16(sp),Dn / movea.l d16(sp),An
                let d16 = u16::from_be_bytes([s.bytes[k + 2], s.bytes[k + 3]]);
                if w & 0xf1ff == 0x202f && d16 == *arg {
                    reg = Some((w >> 9) & 7);
                }
                k += 4;
            } else {
                return Err(err!(
                    "machines: {name}: {w:04x} at +{k} is neither moveq nor a load from the stack ({})",
                    hex(&s.bytes)
                ));
            }
        }
        let reg = reg.ok_or_else(|| {
            err!(
                "machines: {name}: no move.l {arg:#x}(sp),Dn in {}",
                hex(&s.bytes)
            )
        })?;
        if k != s.bytes.len() || s.bytes.len() < 6 {
            return Err(err!(
                "machines: {name}: the replayed bytes do not end on an instruction"
            ));
        }
        asm.align(2);
        asm.sym(&format!("machfw_typemap_{i}"));
        asm.bytes(&s.bytes);
        for (j, d) in defs.iter().enumerate() {
            asm.cmpi_l(reg, count + j as u32);
            asm.w16(0x6602); // bne.b +2
            asm.moveq(reg, d.def.clone_of as i8);
        }
        asm.jmp_const((s.addr + s.bytes.len() as u64) as u32);
        let mut bytes = vec![0x4e, 0xf9, 0, 0, 0, 0];
        bytes.resize(s.bytes.len(), 0x4e);
        for k in (6..s.bytes.len()).step_by(2) {
            bytes[k] = 0x4e;
            bytes[k + 1] = 0x71;
        }
        patches.push(GenPatch {
            anchor: s.name.clone(),
            at: 0,
            bytes,
            relocs: vec![rel(2, &format!("machfw_typemap_{i}"), 0)],
            what: "type map".into(),
        });
    }
    Ok(())
}

/// The frame builder (the vector-191 handler) writes the track's machine type into TX frame word `0x94 + 2t`:
/// `move.b (0xd8c,A0),D0b; ext.w D0w; move.w D0w,(0x92,A3)`. The DSP's FM engine knows five types; a type above 4 is
/// treated like MIDI (nothing rendered). A clone without a voice of its own is sent as its original.
pub(super) fn frametype(
    port: &Port,
    read: Reader,
    defs: &[Declared],
    count: u32,
    asm: &mut Asm,
    patches: &mut Vec<GenPatch>,
) -> Result<()> {
    let s = site(port, read, "cf.machines.frametype")?;
    expect(
        "frame type load",
        &s.bytes,
        &[0x10, 0x28, 0x0d, 0x8c, 0x48, 0x80],
    )?;
    let mapped: Vec<(u32, u32)> = defs
        .iter()
        .enumerate()
        .filter(|(_, d)| !d.def.own_dsp)
        .map(|(i, d)| (count + i as u32, d.def.clone_of))
        .collect();
    if mapped.is_empty() {
        return Ok(());
    }
    asm.align(2);
    asm.sym("machfw_frametype");
    asm.bytes(&[0x10, 0x28, 0x0d, 0x8c]); // move.b (0xd8c,A0),D0b
    for (t, c) in mapped {
        asm.w16(0x0c00); // cmpi.b #t,D0b
        asm.w16(t as u16);
        asm.w16(0x6604); // bne.b +4
        asm.w16(0x103c); // move.b #c,D0b
        asm.w16(c as u16);
    }
    asm.w16(0x4880); // ext.w D0w
    asm.jmp_const((s.addr + 6) as u32);
    patches.push(GenPatch {
        anchor: s.name.clone(),
        at: 0,
        bytes: vec![0x4e, 0xf9, 0, 0, 0, 0],
        relocs: vec![rel(2, "machfw_frametype", 0)],
        what: "frame type".into(),
    });
    Ok(())
}

/// Replay the `mvs.b (0xde,A0),Dn` family and map the added types to their `clone_of` in `Dn` (the register is
/// cleared of its upper bits by the sign extension, so long compares are exact).
fn map_reg(asm: &mut Asm, defs: &[Declared], count: u32, reg: u16) {
    for (j, d) in defs.iter().enumerate() {
        asm.cmpi_l(reg, count + j as u32);
        asm.w16(0x6602); // bne.b +2
        asm.moveq(reg, d.def.clone_of as i8);
    }
}

/// The type reader `FUN_4004b7f2` returns the machine type of a sound (`mvs.b (0xde,A0),D0` then a `bra.b` to the
/// epilogue). Its callers are mostly pages and parameter code that test the type against FM TONE's (type 0) and so on;
/// a clone must look like its original to them. The identity code (list, name, snapshot, persistence) calls the same
/// function from a few known places and keeps the type: `machfw_reader` compares the return address with those.
/// Also the owner test of `SoundParameterSet::vfunc_21` (type == the parameter's owner machine).
pub(super) fn reader(
    port: &Port,
    read: Reader,
    defs: &[Declared],
    count: u32,
    asm: &mut Asm,
    patches: &mut Vec<GenPatch>,
) -> Result<()> {
    let s = site(port, read, "cf.machines.reader")?;
    expect(
        "reader load",
        &s.bytes,
        &[0x71, 0x28, 0x00, 0xde, 0x60, 0x02],
    )?;
    let back = (s.addr + 4 + 2 + 2) as u32; // bra.b +2 from the instruction after the load
    let mut real: Vec<u32> = vec![];
    for i in 0..READER_REAL_CALLS {
        let c = site(port, read, &format!("cf.machines.reader_call.{i}"))?;
        let ok = match c.bytes.len() {
            4 => c.bytes[0..2] == [0x4e, 0xba],
            6 => c.bytes[0..2] == [0x4e, 0xb9],
            _ => false,
        };
        if !ok {
            return Err(err!(
                "machines: {}: {} is not a jsr (d16,PC) or jsr abs.l",
                c.name,
                hex(&c.bytes)
            ));
        }
        real.push((c.addr + c.bytes.len() as u64) as u32);
    }
    asm.align(2);
    asm.sym("machfw_reader");
    asm.bytes(&[0x71, 0x28, 0x00, 0xde]); // mvs.b (0xde,A0),D0
    asm.w16(0x222f); // move.l (4,SP),D1: the return address of the reader's caller (D1 is a scratch register)
    asm.w16(4);
    for (k, ra) in real.iter().enumerate() {
        asm.cmpi_l(1, *ra);
        asm.bcc_w(7, &format!("rd{k}"));
    }
    map_reg(asm, defs, count, 0);
    for k in 0..real.len() {
        if k == 0 {
            asm.label("rd0");
        } else {
            asm.label(&format!("rd{k}"));
        }
    }
    asm.jmp_const(back);
    patches.push(GenPatch {
        anchor: s.name.clone(),
        at: 0,
        bytes: vec![0x4e, 0xf9, 0, 0, 0, 0],
        relocs: vec![rel(2, "machfw_reader", 0)],
        what: "type reader".into(),
    });

    // `FUN_4003134e(project, track)` (the track's type, 4 when it is not a synth track) ends in a tail jump to the
    // reader: its callers (the machine list and selection views, names, kit events) want the real type too. They get
    // a verbatim copy of the reader (relative branches only).
    let f = site(port, read, "cf.machines.reader_fn")?;
    if f.bytes.len() < 0x28
        || f.bytes[0..2] != [0x2f, 0x0a]
        || f.bytes[f.bytes.len() - 2..] != [0x4e, 0x75]
    {
        return Err(err!(
            "machines: reader_fn: not the reader ({})",
            hex(&f.bytes)
        ));
    }
    asm.align(2);
    asm.sym("machfw_reader_real");
    asm.bytes(&f.bytes);
    let t = site(port, read, "cf.machines.reader_tail")?;
    expect("reader tail call", &t.bytes[0..2], &[0x4e, 0xf9])?;
    if be32(&t.bytes, 2) as u64 != f.addr {
        return Err(err!("machines: reader_tail does not jump to the reader"));
    }
    patches.push(GenPatch {
        anchor: t.name.clone(),
        at: 2,
        bytes: vec![0; 4],
        relocs: vec![rel(0, "machfw_reader_real", 0)],
        what: "reader tail call".into(),
    });

    // the owner test: `mvs.b (0xde,A0),D3; move.l D2,-(SP)`
    let o = site(port, read, "cf.machines.owner")?;
    expect(
        "owner load",
        &o.bytes,
        &[0x77, 0x28, 0x00, 0xde, 0x2f, 0x02],
    )?;
    asm.align(2);
    asm.sym("machfw_owner");
    asm.bytes(&o.bytes);
    map_reg(asm, defs, count, 3);
    asm.jmp_const((o.addr + 6) as u32);
    patches.push(GenPatch {
        anchor: o.name.clone(),
        at: 0,
        bytes: vec![0x4e, 0xf9, 0, 0, 0, 0],
        relocs: vec![rel(2, "machfw_owner", 0)],
        what: "owner test".into(),
    });
    Ok(())
}
