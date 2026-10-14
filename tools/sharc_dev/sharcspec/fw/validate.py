#!/usr/bin/env python3
"""Validate the SHARC+ VISA instruction decoder against real DSP firmware.

For each core's main program region (dt2, dn2) this does two independent
checks starting at the program's entry point:

  1. LINEAR SWEEP - decode every instruction from entry to end of region,
     one after another, ignoring control flow. Reports the unknown rate,
     a form histogram and a width histogram. A table-driven decoder that
     is even slightly wrong desynchronizes fast on real code, so a low
     and stable unknown rate over tens of thousands of instructions is
     the main evidence the table is right.

  2. RECURSIVE TRACE - follow actual control flow (branches/calls/jumps)
     starting at entry, decoding linearly along each path until an
     unconditional branch/return/rframe or a decode failure, queuing
     computable direct/PC-relative branch targets as new paths. Reports
     how much of the region that reaches, and how many distinct decode
     failures were hit along genuinely-executed paths.

Address-space note (see ../README.md "Firmware test corpus"): SW (word)
code addresses relate to BW (byte) addresses used everywhere else in this
project's memory map as BW = 2*SW + 0x28000000 (the L1 alias base) - not
BW = 2*SW alone. The region filenames' trailing hex IS a BW address in
that same aliased space (e.g. dt2 prog1 load address 0x28382670). Using
the literal BW = 2*SW would place the entry point ~0x28000000 bytes
before the start of the region; using BW = 2*SW + 0x28000000 places the
entry point at byte offset 0 of both prog1 regions, exactly at the start
of the main program - which is what "entry point" should mean here, and
was cross-checked against both dt2 and dn2 before relying on it. This
script therefore uses the corrected formula throughout; see BASE below.
"""

import os
import re
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_DIR = os.path.dirname(HERE)

from tools.sharc_dev.sharcspec.sharc_decode import Decoder, linear  # noqa: E402

# Program regions are not committed (they are firmware bytes); put them in
# the repo's git-ignored out/sharcspec/ with the file names below.
OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(SPEC_DIR))), "out", "sharcspec"
)

# BW = 2*SW + BASE (L1 alias base). See module docstring.
BASE = 0x28000000

# Region file + entry SW address for each core's main program.
CORES = {
    "dt2": {
        "region": os.path.join(OUT_DIR, "dt2_os1.16_prog1_28382670.bin"),
        "entry_sw": 0x1C1338,
    },
    "dn2": {
        "region": os.path.join(OUT_DIR, "dn2_os1.11_prog1_283825c4.bin"),
        "entry_sw": 0x1C12E2,
    },
}

# Control-flow form-name prefixes per the task: branch/jump/call/return forms.
BRANCH_PREFIXES = ("Type8", "Type9", "Type10", "Type11", "Type25")


def region_load_addr(path):
    """Parse the BW load address from a region filename's trailing hex."""
    m = re.search(r"_([0-9a-fA-F]+)\.bin$", os.path.basename(path))
    if not m:
        raise ValueError(f"can't parse load address from {path}")
    return int(m.group(1), 16)


def sw_to_bw(sw):
    return 2 * sw + BASE


def bw_to_sw(bw):
    assert (bw - BASE) % 2 == 0, "misaligned BW address"
    return (bw - BASE) // 2


def instr_sw(region_load, pos):
    """SW address of the instruction at byte offset `pos` in the region."""
    return bw_to_sw(region_load + pos)


def signed(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


_FIELD_RANGE_RE = re.compile(r"^(\w+)\[(\d+):(\d+)\]$")


def _reassemble(fields, base):
    """Reassemble a (possibly word-split) 'base[hi:lo]'-labeled field back
    into one integer + its total bit width, or (None, 0) if `base` doesn't
    appear. Generic over field width so it covers both the 24-bit
    addr/reladdr fields (Type8a/25a) and the 6-bit reladdr fields
    (Type9a_rel/Type9b_rel/Type10a_rel -- see build_table.py SPLIT_FORMS)."""
    value, top_bit, found = 0, -1, False
    for label, val in fields.items():
        m = _FIELD_RANGE_RE.match(label)
        if not m or m.group(1) != base:
            continue
        found = True
        lo = int(m.group(3))
        value |= val << lo
        top_bit = max(top_bit, int(m.group(2)))
    return (value, top_bit + 1) if found else (None, 0)


def branch_target_offset(form, fields, region_load, pos):
    """Return the byte offset (within the region) of a statically-known
    branch/call/jump target, or None if the form has no static target
    (register-indirect jump/call, or a return/rframe with no address).

    Only forms that literally carry addr[..]/reladdr[..] fields (per the
    task spec) yield a static target:
      - addr[hi:lo]+...    -> absolute SW word address (any total width)
      - reladdr[hi:lo]+... -> signed SW word displacement, relative to THIS
                               instruction's own SW address (confirmed
                               against firmware -- see task report).
    Type9a_abs/Type9b_abs (register-indirect via PMI/PMM) and Type11/Type25
    rframe variants carry neither field, so they return None here even
    though they are still control-flow instructions for the "stop at
    unconditional branch/return" rule below.
    """
    addr, _ = _reassemble(fields, "addr")
    if addr is not None:
        return sw_to_bw(addr) - region_load

    reladdr, width = _reassemble(fields, "reladdr")
    if reladdr is not None:
        disp = signed(reladdr, width)
        target_sw = instr_sw(region_load, pos) + disp
        return sw_to_bw(target_sw) - region_load
    return None


def is_unconditional(form, fields):
    """True if this control-flow instruction always transfers control:
    cond[4:0] == 0b11111 (TRUE/FOREVER, PGR Table 10-4), or the form has
    no cond field at all (Type25a_direct/pcrel/rframe, Type25c_rframe -
    dedicated unconditional encodings)."""
    if "cond[4:0]" not in fields:
        return True
    return fields["cond[4:0]"] == 0b11111


def linear_sweep(mem, start, dec):
    """Decode every instruction from byte offset `start` to the end of mem."""
    count = (len(mem) - start) // 2 + 2  # upper bound; linear() stops early
    return linear(mem, start, count, dec)


def summarize_sweep(entries):
    total = len(entries)
    unknown = sum(1 for e in entries if e[2] is None)
    ambiguous = sum(1 for e in entries if e[2] is None and e[3])
    no_match = unknown - ambiguous
    form_hist = {}
    width_hist = {}
    for pos, nwords, form, cands, frame in entries:
        name = form["name"] if form else "UNKNOWN"
        form_hist[name] = form_hist.get(name, 0) + 1
        width = form["width"] if form else 16
        width_hist[width] = width_hist.get(width, 0) + 1
    return {
        "total": total,
        "unknown": unknown,
        "ambiguous": ambiguous,
        "no_match": no_match,
        "form_hist": form_hist,
        "width_hist": width_hist,
    }


def recursive_trace(mem, start, dec, region_load):
    """Follow control flow from `start`, decoding linearly along each path
    until an unconditional branch/return/rframe or a decode failure.
    Returns (reached: bytearray flags, decode_error_offsets: set, paths_run: int).
    """
    reached = bytearray(len(mem))  # 1 byte per BW offset: 1 if covered
    decode_errors = set()
    queued = {start}
    work = [start]
    paths_run = 0

    while work:
        pos = work.pop()
        paths_run += 1
        while 0 <= pos < len(mem):
            if reached[pos]:
                break  # already covered by an earlier path
            w = _words_at(mem, pos)
            if not w:
                break  # ran off the end of the region
            frame = _pack_frame(w)
            form, cands = dec.decode_frame(frame)
            if form is None:
                decode_errors.add(pos)
                for b in range(pos, min(pos + 2, len(mem))):  # 1 word (16 bits) probed
                    reached[b] = 1
                break
            n = form["width"] // 16
            for b in range(pos, min(pos + 2 * n, len(mem))):
                reached[b] = 1

            if form["name"].startswith(BRANCH_PREFIXES):
                fields = dec.fields(frame, form)
                target = branch_target_offset(form, fields, region_load, pos)
                if (
                    target is not None
                    and 0 <= target < len(mem)
                    and target not in queued
                ):
                    queued.add(target)
                    work.append(target)
                if is_unconditional(form, fields):
                    break  # this path ends here; no fall-through
                # conditional: fall through to the next instruction below

            pos += 2 * n
    return reached, decode_errors, paths_run


def _words_at(mem, addr):
    return [
        struct.unpack_from("<H", mem, addr + 2 * i)[0]
        for i in range(3)
        if addr + 2 * i + 2 <= len(mem)
    ]


def _pack_frame(words):
    frame = 0
    for i, v in enumerate(words + [0] * (3 - len(words))):
        frame |= v << (32 - 16 * i)
    return frame


def fmt_fields(fields):
    return ", ".join(f"{k}={v:#x}" for k, v in fields.items())


def print_first_n(entries, region_load, dec, n=60):
    print(
        f"\nFirst {n} instructions of the linear sweep (SW addr, hex words, form, fields):"
    )
    for pos, nwords, form, cands, frame in entries[:n]:
        sw = instr_sw(region_load, pos)
        raw = _words_at_from_frame(frame, nwords)
        hexwords = " ".join(f"{w:04x}" for w in raw)
        name = form["name"] if form else "UNKNOWN"
        fields = dec.fields(frame, form) if form else {}
        print(f"  SW {sw:#08x}  [{hexwords:<15}]  {name:<18} {fmt_fields(fields)}")


def _words_at_from_frame(frame, nwords):
    """Recover the nwords 16-bit words that were packed into `frame`."""
    return [(frame >> (32 - 16 * i)) & 0xFFFF for i in range(nwords)]


def run_core(name, cfg, dec, print_first=False):
    mem = open(cfg["region"], "rb").read()
    region_load = region_load_addr(cfg["region"])
    entry_bw = sw_to_bw(cfg["entry_sw"])
    offset = entry_bw - region_load

    print(f"\n{'=' * 70}\n{name}: {os.path.basename(cfg['region'])}\n{'=' * 70}")
    print(
        f"region load addr (BW) = {region_load:#010x}, region size = {len(mem)} bytes"
    )
    print(
        f"entry SW = {cfg['entry_sw']:#08x} -> entry BW = {entry_bw:#010x} -> region offset = {offset:#x} ({offset})"
    )
    if not (0 <= offset < len(mem)):
        print("  !! entry offset falls outside the region; skipping.")
        return

    # --- 1. Linear sweep ---
    entries = linear_sweep(mem, offset, dec)
    stats = summarize_sweep(entries)
    print(f"\n-- Linear sweep from entry to end of region --")
    print(f"total instructions decoded: {stats['total']}")
    pct = lambda x: 100.0 * x / stats["total"] if stats["total"] else 0.0
    print(
        f"unknown (form=None):        {stats['unknown']} ({pct(stats['unknown']):.2f}%)"
    )
    print(f"  - ambiguous (candidates>0): {stats['ambiguous']}")
    print(f"  - no match (candidates=0):  {stats['no_match']}")
    print("form histogram:")
    for fname, c in sorted(stats["form_hist"].items(), key=lambda kv: -kv[1]):
        print(f"    {fname:<20} {c:6d}  ({pct(c):5.2f}%)")
    print("width histogram (bits):")
    for width, c in sorted(stats["width_hist"].items()):
        print(f"    {width:>3}-bit  {c:6d}  ({pct(c):5.2f}%)")

    # --- 2. Recursive trace ---
    reached, decode_errors, paths_run = recursive_trace(mem, offset, dec, region_load)
    reached_bytes = sum(reached)
    span = len(mem) - offset  # entry..end-of-region span
    print(f"\n-- Recursive control-flow trace from entry --")
    print(f"paths (work-queue items) run: {paths_run}")
    print(
        f"bytes reached: {reached_bytes} / region size {len(mem)} ({100.0 * reached_bytes / len(mem):.2f}%)"
    )
    print(
        f"             : {reached_bytes} / entry->end span {span} ({100.0 * reached_bytes / span:.2f}%)"
    )
    print(f"distinct decode errors hit while tracing: {len(decode_errors)}")
    if decode_errors:
        shown = sorted(decode_errors)[:10]
        print(f"  first offsets: {[hex(o) for o in shown]}")

    if print_first:
        print_first_n(entries, region_load, dec, n=60)

    return stats


def main():
    dec = Decoder(mode="visa")
    print(f"Decoder: mode=visa, {len(dec.forms)} forms loaded from decode_table.json")
    for name, cfg in CORES.items():
        run_core(name, cfg, dec, print_first=(name == "dt2"))


if __name__ == "__main__":
    main()
