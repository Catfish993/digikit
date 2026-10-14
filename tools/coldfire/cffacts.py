#!/usr/bin/env python3
"""Per-function facts of a ColdFire MAIN OS image for cross-version matching.

tools/coldfire/cfdb.py calls compute() while it builds a database; tools/coldfire/cfmatch.py reads
the result. The instruction stream comes from the repo's ColdFire decoder
(packages/emulator/coldfire, through its `cfdis --ranges` mode), over the body ranges that
Ghidra recorded for each function. Each instruction is turned into a token in
which what a relink moves is masked:

  strict   mnemonic and operands. Branch targets inside the function become an
           offset from its entry, targets outside it `@X`; a literal inside the
           image becomes `@I`, a RAM or heap address (0x4... or 0x8... outside
           the image) `@R`. Other numbers (displacements, small immediates,
           peripheral registers) are kept.
  loose    as strict, but every number is masked.
  mnem     the mnemonic only, branches without their size suffix.

The hash of a level is the SHA-1 of the token lines, cut to 16 bytes. A function
with several body ranges is the concatenation of its ranges in address order.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRATE = ROOT / "packages" / "emulator" / "coldfire"
DEFAULT_BASE = 0x40000400
IMAGE_LIMIT = 0x40400000  # nothing in a MAIN OS image lies beyond this

_HEX = re.compile(r"0x[0-9a-fA-F]+")
_BRANCH_FORMS = frozenset({"bcc", "bra", "bsr"})
_SIZE_SUFFIX = re.compile(r"\.[bwl]$")


def cfdis_path() -> Path:
    """The cfdis binary: $DIGI_CFDIS, else out/native/cfdis-tool built on demand."""
    env = os.environ.get("DIGI_CFDIS")
    if env:
        return Path(env)
    main = ROOT
    # lanes link out/ to the main tree's: build there so every lane shares it
    out = (ROOT / "out").resolve()
    target = out / "native" / "cfdis-tool"
    exe = target / "release" / "cfdis"
    src = CRATE / "src" / "bin" / "cfdis.rs"
    if not exe.exists() or exe.stat().st_mtime < src.stat().st_mtime:
        subprocess.run(
            [
                "cargo", "build", "--release", "--quiet",
                "--manifest-path", str(main / "packages" / "emulator" / "coldfire" / "Cargo.toml"),
                "--bin", "cfdis", "--target-dir", str(target),
            ],
            check=True,
        )  # fmt: skip
    return exe


def decode_ranges(
    image: Path, base: int, ranges: Iterable[tuple[int, int]]
) -> dict[tuple[int, int], list[tuple[int, int, str, str]]]:
    """{(lo, hi): [(addr, length, form, text)]} for half-open ranges."""
    ranges = list(ranges)
    stdin = "".join("%x %x\n" % r for r in ranges)
    res = subprocess.run(
        [str(cfdis_path()), str(image), "%x" % base, "--ranges"],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[tuple[int, int], list[tuple[int, int, str, str]]] = {}
    cur: list[tuple[int, int, str, str]] | None = None
    for line in res.stdout.splitlines():
        if line.startswith("R "):
            _r, lo, hi = line.split()
            cur = out.setdefault((int(lo, 16), int(hi, 16)), [])
            continue
        addr, n, form, text = line.split(" ", 3)
        assert cur is not None
        cur.append((int(addr, 16), int(n), form, text))
    return out


def tokens(
    insns: list[tuple[int, int, str, str]],
    entry: int,
    lo: int,
    hi: int,
    image_lo: int,
    image_hi: int,
) -> tuple[list[str], list[str], list[str]]:
    """(strict, loose, mnem) token lists of one function body."""
    strict: list[str] = []
    loose: list[str] = []
    mnem: list[str] = []
    for _addr, n, form, text in insns:
        if n == 0:
            strict.append(".illegal")
            loose.append(".illegal")
            mnem.append(".illegal")
            continue
        head, _sp, ops = text.partition(" ")
        if form in _BRANCH_FORMS:
            head = _SIZE_SUFFIX.sub("", head)
        mnem.append(head)

        def strict_sub(m: re.Match[str], form: str = form) -> str:
            v = int(m.group(0), 16)
            if form in _BRANCH_FORMS or form in ("jsr", "jmp"):
                if lo <= v < hi:
                    return "@+%x" % (v - entry)
                return "@X"
            if image_lo <= v < image_hi:
                return "@I"
            if 0x40000000 <= v < 0x50000000 or 0x80000000 <= v < 0x90000000:
                return "@R"
            return m.group(0)

        strict.append(head + " " + _HEX.sub(strict_sub, ops))
        loose.append(head + " " + _HEX.sub("N", ops))
    return strict, loose, mnem


def loose_token(insn: tuple[int, int, str, str]) -> str:
    """The loose token of one decoded instruction (see tokens())."""
    _addr, n, form, text = insn
    if n == 0:
        return ".illegal"
    head, _sp, ops = text.partition(" ")
    if form in _BRANCH_FORMS:
        head = _SIZE_SUFFIX.sub("", head)
    return head + " " + _HEX.sub("N", ops)


def _digest(lines: list[str]) -> str:
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()[:32]


def compute(
    image: Path,
    base: int,
    func_ranges: dict[int, list[tuple[int, int]]],
) -> dict[int, dict[str, object]]:
    """Facts per function entry. `func_ranges` maps entry -> inclusive (lo, hi)
    body ranges as in a Ghidra dump's function_ranges table."""
    image_hi = min(base + image.stat().st_size, IMAGE_LIMIT)
    spans = {
        entry: sorted((lo, hi + 1) for lo, hi in rs)
        for entry, rs in func_ranges.items()
    }
    wanted = sorted({r for rs in spans.values() for r in rs})
    decoded = decode_ranges(image, base, wanted)
    out: dict[int, dict[str, object]] = {}
    for entry, rs in spans.items():
        s: list[str] = []
        lo_: list[str] = []
        m: list[str] = []
        nbytes = 0
        flo = rs[0][0]
        fhi = rs[-1][1]
        for r in rs:
            a, b, c = tokens(decoded[r], entry, flo, fhi, base, image_hi)
            s += a
            lo_ += b
            m += c
            nbytes += r[1] - r[0]
        out[entry] = {
            "ninsn": len(m),
            "nbytes": nbytes,
            "h_strict": _digest(s),
            "h_loose": _digest(lo_),
            "h_mnem": _digest(m),
            "mnem_seq": " ".join(m),
        }
    return out
