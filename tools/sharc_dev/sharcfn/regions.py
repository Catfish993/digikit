"""Named memory regions and float constants, and the labels of 32-bit literals."""

from __future__ import annotations

import os

from tools.sharc_dev import sharcimm, sharcinv, sharcldr

_here = os.path.dirname(os.path.abspath(__file__))

EXPECTED_SHA256 = {
    "0f514a12a2255f5c081e292c47f1f29462003177658da4bbae0a22fd737fffa2": "dt2-1.16 section_7_BLOB.bin",
}

# --- named regions not already in sharcinv.NAMED_TABLES --------------------
#
# tools/sharc_dev/sharcinv.py's NAMED_TABLES only carries the tables its FFT labeler
# needs (cosine pair, 1024-float pair, exp829, table32, stage5 index table).
# The rest of the task's list of known regions lives here instead of being
# duplicated into that file.
EXTRA_NAMED_REGIONS = {
    0x264138: "ring_head_C",
    0x264170: "ring_head_D",
    0x256388: "pair128_a",
    0x256588: "pair128_b",
    0x252D3C: "shared_context",
    0x25D940: "ram_table_25d940",
    0x2C2CC0: "ram_table128_2c2cc0",
    0x2C2018: "ram_table1024_2c2018",
}
RING_BUFFER_NAMES = {
    0x262138: "ring_C_buf0",
    0x262938: "ring_C_buf1",
    0x263138: "ring_D_buf0",
    0x263938: "ring_D_buf1",
}
PARAM_FRAME_LO, PARAM_FRAME_HI = sharcinv.FRAME_LO, sharcinv.FRAME_HI
PARAM_TRACK_BASE = 0x2559B6
PARAM_TRACK_STRIDE = 0x60
PARAM_SRC_PAGE_LEN = 0x14

# Any DM address in this band, or one matching a named region below, is
# worth reporting RAM/ROM for; a plain compute immediate that happens to
# decode as a plausible float is not.
_ADDR_LIKE_BAND = (0x200000, 0x30000000)


def name_literal_region(value: int):
    """Human label for a 32-bit literal, or None. Prefers
    tools/sharc_dev/sharcinv.py's NAMED_TABLES (the canonical list this tool does not
    duplicate); falls through EXTRA_NAMED_REGIONS, the audio ring buffers,
    the per-track parameter frame, then tools/sharc_dev/sharcimm.py's peripheral-
    register namer."""
    if value in sharcinv.NAMED_TABLES:
        return sharcinv.NAMED_TABLES[value]
    if value in EXTRA_NAMED_REGIONS:
        return EXTRA_NAMED_REGIONS[value]
    if value in RING_BUFFER_NAMES:
        return RING_BUFFER_NAMES[value]
    if PARAM_FRAME_LO <= value <= PARAM_FRAME_HI:
        if value == PARAM_FRAME_LO:
            return "param_frame_base"
        off = value - PARAM_TRACK_BASE
        if off >= 0:
            track, byte = divmod(off, PARAM_TRACK_STRIDE)
            page = " (SRC page)" if byte < PARAM_SRC_PAGE_LEN else ""
            return "param_frame:track%d+0x%x%s" % (track, byte, page)
        return "param_frame+0x%x" % (value - PARAM_FRAME_LO)
    peripheral = sharcimm.name_address(value)
    if peripheral:
        return "peripheral:" + peripheral
    return None


def _is_addr_like(value: int, region) -> bool:
    lo, hi = _ADDR_LIKE_BAND
    return bool(region) or lo <= value < hi or (value >> 24) == 0x80


# --- float immediates --------------------------------------------------

# name, exact value; matched within a small relative tolerance.
KNOWN_FLOAT_CONSTANTS = (
    ("pi", 3.14159265358979),
    ("pi/8", 0.39269908169872414),
    ("ln(10)", 2.302585092994046),
    ("sqrt(10)", 3.1622776601683795),
    ("2^32", 4294967296.0),
    ("96000.0 (sample rate)", 96000.0),
    ("1/12 (semitone)", 1.0 / 12),
    ("220.0", 220.0),
    ("127.0", 127.0),
    ("128.0", 128.0),
)


def float_constant_name(value: float):
    for name, const in KNOWN_FLOAT_CONSTANTS:
        tol = max(abs(const), 1.0) * 1e-4
        if abs(value - const) <= tol:
            return name
    return None


def describe_literal(value: int, float_capable: bool, mem: sharcldr.LoadedMemory):
    """-> dict describing a 32-bit literal: hex, region (if any), RAM/ROM
    (only when it looks address-shaped), and, if float_capable, the
    IEEE-754 decode and any matched named constant."""
    value &= 0xFFFFFFFF
    out = {"hex": "0x%08x" % value}
    region = name_literal_region(value)
    if region:
        out["region"] = region
    if _is_addr_like(value, region):
        out["memory"] = "ROM" if mem.read(value, 1) is not None else "RAM"
    if float_capable:
        f = sharcinv.float32(value)
        if sharcinv._plausible_float(f) or region is None:
            out["float"] = f
            name = float_constant_name(f)
            if name:
                out["float_constant"] = name
    return out


def _annotate_literal(value: int, float_capable: bool, mem) -> str:
    d = describe_literal(value, float_capable, mem)
    bits = []
    if "float" in d:
        bits.append("%.6g" % d["float"])
        if "float_constant" in d:
            bits.append("== " + d["float_constant"])
    if "region" in d:
        bits.append(d["region"])
    if "memory" in d:
        bits.append(d["memory"])
    return " ; ".join(bits)
