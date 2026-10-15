"""Find GCC C++ RTTI, vtables and Class::method strings in a ColdFire image.

    uv run python -m tools.coldfire.rttiscan sections/section_3_MAIN_OS.bin
        [--base 0x40000400] [--code LO HI]...
        --json out/symbols/dt2-1.15C-rtti.json

Raw and deterministic: it reads the image and uses the repo disassembler
(tools.coldfire.disasm), not Ghidra. Ghidra's RecoverClassesFromRTTIScript recovers
no classes from this image, even with the program's compiler set to gcc.

Itanium C++ ABI, 32-bit big-endian; the objects are 4-byte aligned:
  typeinfo  [kind vtable + 8][name]                 __class_type_info
            [kind vtable + 8][name][base]           __si_class_type_info
            [kind vtable + 8][name][flags][count][base, offset_flags]...
                                                    __vmi_class_type_info
  vtable    [offset_to_top][typeinfo][slot 0][slot 1]...
Each kind vtable is found from its N10__cxxabiv1...E name: the typeinfo
whose +4 points at the name, then the vtable whose +4 points at that
typeinfo and whose offset_to_top is 0.

The JSON lists typeinfo objects with demangled names and bases; vtables with
their code-pointer slots and, for each slot, the most basic class whose
primary vtable has the same function in that slot; instructions that load a
vtable address plus 8 (constructors and destructors); and strings of the
form Class::method with the instructions that load them.
tools/coldfire/ghidraapply.py applies the result. Names are demangled with
`c++filt -t` when it is on PATH.
"""

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys

from tools.coldfire.codeseeds import BASE, CODE, in_code  # noqa: E402
from tools.coldfire.disasm import disasm  # noqa: E402

KINDS = {
    "class": "N10__cxxabiv117__class_type_infoE",
    "si_class": "N10__cxxabiv120__si_class_type_infoE",
    "vmi_class": "N10__cxxabiv121__vmi_class_type_infoE",
    "pointer": "N10__cxxabiv119__pointer_type_infoE",
    "pbase": "N10__cxxabiv117__pbase_type_infoE",
    "function": "N10__cxxabiv120__function_type_infoE",
    "fundamental": "N10__cxxabiv123__fundamental_type_infoE",
    "enum": "N10__cxxabiv116__enum_type_infoE",
    "array": "N10__cxxabiv117__array_type_infoE",
}
CLASS_KINDS = ("class", "si_class", "vmi_class")

MANGLED = re.compile(r"[A-Za-z0-9_]+")
STRING = re.compile(rb"[\x21-\x7e][\x20-\x7e]{3,}\x00")
QUALIFIED = re.compile(r"[A-Za-z_][\w<>,*&]*(?:::~?[A-Za-z_][\w<>,*&]*)+")
TOKEN = re.compile(r"\$([0-9a-fA-F]+)")


class Image:
    def __init__(self, data, base):
        self.data, self.base = data, base

    def u32(self, addr):
        off = addr - self.base
        if off < 0 or off + 4 > len(self.data):
            return None
        return struct.unpack_from(">I", self.data, off)[0]

    def cstr(self, addr, limit=512):
        off = addr - self.base
        if off < 0 or off >= len(self.data):
            return None
        end = self.data.find(b"\0", off, off + limit)
        if end <= off:
            return None
        raw = self.data[off:end]
        if not all(0x20 < b < 0x7F for b in raw):
            return None
        return raw.decode("ascii")


def where(base, vals, targets):
    """-> {value: addresses of the 4-byte-aligned longwords equal to it}"""
    hits = {t: [] for t in targets}
    for i, v in enumerate(vals):
        if v in hits:
            hits[v].append(base + 4 * i)
    return hits


def demangle(names):
    """-> {mangled: demangled} via `c++filt -t`; unchanged without it."""
    tool = shutil.which("c++filt")
    if tool is None or not names:
        return {n: n for n in names}
    out = subprocess.run(
        [tool, "-t"],
        input="\n".join(names) + "\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    if len(out) != len(names):
        return {n: n for n in names}
    return dict(zip(names, out, strict=False))


def kind_vptrs(img, vals):
    """-> {kind vtable + 8: kind}"""
    names = {}
    for kind, mangled in KINDS.items():
        pat = mangled.encode() + b"\0"
        off = img.data.find(pat)
        while off >= 0:
            names[img.base + off] = kind
            off = img.data.find(pat, off + 1)
    typeinfo_kind = {
        a - 4: names[s]
        for s, addrs in where(img.base, vals, names).items()
        for a in addrs
    }
    vptrs = {}
    for t, addrs in where(img.base, vals, typeinfo_kind).items():
        for a in addrs:
            if img.u32(a - 4) == 0:
                vptrs[a + 4] = typeinfo_kind[t]
    return vptrs


def find_typeinfos(img, vals, vptrs, demangle):
    found = {}
    for i, v in enumerate(vals):
        kind = vptrs.get(v)
        if kind is None:
            continue
        t = img.base + 4 * i
        name_addr = img.u32(t + 4)
        mangled = img.cstr(name_addr) if name_addr is not None else None
        if mangled is None or not MANGLED.fullmatch(mangled):
            continue
        bases = []
        if kind == "si_class":
            bases = [img.u32(t + 8)]
        elif kind == "vmi_class":
            count = img.u32(t + 12)
            if count is None or not 0 < count <= 64:
                continue
            bases = [img.u32(t + 16 + 8 * k) for k in range(count)]
        found[t] = {
            "addr": t,
            "kind": kind,
            "name_addr": name_addr,
            "mangled": mangled,
            "bases": bases,
        }
    # A class whose base is not a typeinfo object is a false match.
    while True:
        bad = [t for t, ti in found.items() if any(b not in found for b in ti["bases"])]
        if not bad:
            break
        for t in bad:
            del found[t]
    names = demangle(sorted({ti["mangled"] for ti in found.values()}))
    for ti in found.values():
        ti["name"] = names.get(ti["mangled"], ti["mangled"])
    return found


def owner_of(vt, i, primary, typeinfos):
    """The most basic class whose primary vtable has the same function in slot i."""
    owner = vt["typeinfo"]
    if vt["offset_to_top"] != 0:
        return owner
    seen = {owner}
    while typeinfos[owner]["bases"]:
        base = typeinfos[owner]["bases"][0]
        bvt = primary.get(base)
        if (
            base in seen
            or bvt is None
            or i >= len(bvt["slots"])
            or bvt["slots"][i] != vt["slots"][i]
        ):
            break
        seen.add(base)
        owner = base
    return owner


def find_vtables(img, vals, typeinfos, code):
    classes = [t for t, ti in typeinfos.items() if ti["kind"] in CLASS_KINDS]
    vtables = []
    for t, addrs in where(img.base, vals, classes).items():
        for a in addrs:
            v = a - 4
            off = img.u32(v)
            if off is None:
                continue
            off -= (1 << 32) if off & 0x80000000 else 0
            if not -0x10000 <= off <= 0:
                continue
            slots = []
            s = img.u32(v + 8)
            while s is not None and in_code(s, code):
                slots.append(s)
                s = img.u32(v + 8 + 4 * len(slots))
            if slots:
                vtables.append(
                    {
                        "addr": v,
                        "typeinfo": t,
                        "class": typeinfos[t]["name"],
                        "offset_to_top": off,
                        "slots": slots,
                    }
                )
    vtables.sort(key=lambda vt: vt["addr"])
    primary = {}
    for vt in vtables:
        if vt["offset_to_top"] == 0:
            primary.setdefault(vt["typeinfo"], vt)
    for vt in vtables:
        vt["owners"] = [
            typeinfos[owner_of(vt, i, primary, typeinfos)]["name"]
            for i in range(len(vt["slots"]))
        ]
    return vtables


def code_refs(data, base, code, targets):
    """-> {target: addresses of instructions whose operands contain $target}"""
    hits = {t: [] for t in targets}
    for lo, hi in code:
        for pc, _hx, _mn, ops in disasm(data, base, lo, hi):
            for v in {int(tok, 16) for tok in TOKEN.findall(ops)}:
                if v in hits:
                    hits[v].append(pc)
    return hits


def scan(data, base, code, demangle=demangle):
    img = Image(data, base)
    vals = struct.unpack_from(">%dI" % (len(data) // 4), data)
    vptrs = kind_vptrs(img, vals)
    typeinfos = find_typeinfos(img, vals, vptrs, demangle)
    vtables = find_vtables(img, vals, typeinfos, code)
    points = {vt["addr"] + 8: vt for vt in vtables}
    qualified = {}
    for m in STRING.finditer(data):
        text = m.group()[:-1].decode("ascii")
        if QUALIFIED.fullmatch(text):
            qualified[base + m.start()] = text
    refs = code_refs(data, base, code, set(points) | set(qualified))
    return {
        "base": base,
        "code_ranges": [list(r) for r in code],
        "kind_vptrs": {"%#x" % v: k for v, k in sorted(vptrs.items())},
        "typeinfos": sorted(typeinfos.values(), key=lambda ti: ti["addr"]),
        "vtables": vtables,
        "vtable_refs": [
            {"site": s, "vtable": points[p]["addr"], "class": points[p]["class"]}
            for p in sorted(points)
            for s in refs[p]
        ],
        "qualified_strings": [
            {"addr": a, "text": qualified[a], "sites": refs[a]}
            for a in sorted(qualified)
        ],
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Find GCC RTTI, vtables and Class::method strings."
    )
    p.add_argument("image")
    p.add_argument("--base", type=lambda s: int(s, 0), default=BASE)
    p.add_argument(
        "--code",
        action="append",
        nargs=2,
        type=lambda s: int(s, 0),
        metavar=("LO", "HI"),
        help="code range, repeatable",
    )
    p.add_argument("--json", required=True)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    with open(args.image, "rb") as f:
        data = f.read()
    code = [tuple(r) for r in args.code] if args.code else list(CODE)
    report = scan(data, args.base, code)
    report["image"] = args.image
    report["sha256"] = hashlib.sha256(data).hexdigest()
    os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
    with open(args.json, "w") as f:
        json.dump(report, f, indent=1)
    kinds = collections.Counter(ti["kind"] for ti in report["typeinfos"])
    loaded = sum(1 for s in report["qualified_strings"] if s["sites"])
    print("kind vtables: %s" % report["kind_vptrs"])
    print(
        "%d typeinfo objects (%s)"
        % (
            len(report["typeinfos"]),
            ", ".join("%s %d" % kv for kv in sorted(kinds.items())),
        )
    )
    print(
        "%d vtables, %d vtable loads, %d Class::method strings (%d loaded by code)"
        % (
            len(report["vtables"]),
            len(report["vtable_refs"]),
            len(report["qualified_strings"]),
            loaded,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
