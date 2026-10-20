#!/usr/bin/env python3
"""Module sources -> `.dko.json` objects (patchkit's relocatable object format).

    uv run python -m tools.patchkit.pk_build build modules/fx/master-gain [--check]
    uv run python -m tools.patchkit.pk_build build-all [--check]

For every `[[object]]` of a module's `module.toml` that lists `sources`, the sources are compiled by the
pinned toolchain (SHARC+: Selache `selas`; ColdFire: m68k-elf gcc/as, tools/patchkit/pk_link.py; ColdFire Rust: a `Cargo.toml` as the only source,
tools/patchkit/pk_rust.py), turned into the
canonical object JSON (sorted keys, integers only; format in packages/patchkit/src/dko.rs) and written to the
object's `path`. Every instruction is re-decoded by our own decoders (SHARC: tools/patchkit/pk_sharc.py `verify`;
ColdFire: tools/patchkit/pk_link.py `check_isa`), so a toolchain encoding bug cannot reach an image.

`--check` writes nothing and fails when a committed object differs from a fresh build (objects are
committed; the check keeps them current with `src/`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

from tools.patchkit import (  # noqa: E402
    pk_link,  # noqa: E402
    pk_rust,  # noqa: E402
    pk_sharc,  # noqa: E402
)

FORMAT = 1
SHARC_TYPES = {pk_sharc.R_ABS24: "sharc.abs24", pk_sharc.R_ABS32: "sharc.abs32"}
CF_TYPES = {"32": "cf.32", "PC32": "cf.pc32", "PC16": "cf.pc16", "PC8": "cf.pc8"}
CF_PLT = {"PLT32": "PC32", "PLT16": "PC16", "PLT8": "PC8"}


class BuildError(RuntimeError):
    pass


ISA_CHECK_KEY = "isa_check"


def isa_digest(sections: list[dict], symbols: list[dict], relocs: list[dict]) -> str:
    """The ISA check record of a ColdFire object: SHA-256 over its sections, symbols and relocations in a fixed framing, the same as
    packages/patchkit/src/dko.rs `Dko::isa_digest`. `patchkit pack` refuses a ColdFire object whose record does not match its bytes, so an
    object that was not built (and checked, `pk_link.check_isa`) here, or was edited afterwards, cannot become a `.dkm`."""
    import hashlib

    h = hashlib.sha256(b"dko-isa-v1")

    def s(x: bytes) -> None:
        h.update(len(x).to_bytes(4, "big") + x)

    h.update(len(sections).to_bytes(4, "big"))
    for x in sections:
        s(x["name"].encode())
        s(x["kind"].encode())
        h.update(x["align"].to_bytes(4, "big") + x["size"].to_bytes(4, "big"))
        s(bytes.fromhex(x["hex"]))
    h.update(len(symbols).to_bytes(4, "big"))
    for x in symbols:
        s(x["name"].encode())
        s((x["section"] or "").encode())
        h.update(bytes([x["section"] is not None]) + x["value"].to_bytes(8, "big"))
        h.update(bytes([bool(x["global"])]))
    h.update(len(relocs).to_bytes(4, "big"))
    for x in relocs:
        s(x["section"].encode())
        h.update(x["offset"].to_bytes(8, "big"))
        s(x["type"].encode())
        s(x["symbol"].encode())
        h.update((x["addend"] & ((1 << 64) - 1)).to_bytes(8, "big"))
    return h.hexdigest()


def canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=True) + "\n"


INCLUDE = re.compile(r'^#include\s+"([^"]+)"[ \t]*$', re.M)


def expand_includes(text: str, dirs: list[Path], depth: int = 0) -> str:
    """Replace `#include "name"` lines by the file's text (searched in `dirs`; the generated includes of
    `patchkit gen` are in the module's `build/gen/`). The assembler is run on a temporary copy, so it cannot
    find includes itself."""
    if depth > 4:
        raise BuildError("#include nests deeper than 4 levels")

    def one(m: re.Match) -> str:
        for d in dirs:
            f = d / m.group(1)
            if f.is_file():
                return expand_includes(f.read_text(), dirs, depth + 1)
        raise BuildError(
            f'#include "{m.group(1)}" not found in {[str(d) for d in dirs]}'
        )

    return INCLUDE.sub(one, text)


COND = re.compile(r"^#(ifdef|ifndef|else|endif)(?:[ \t]+(\w+))?[ \t]*$")


def apply_conditionals(text: str, defines: frozenset[str] = frozenset()) -> str:
    """Build options: `#ifdef NAME` / `#ifndef NAME` / `#else` / `#endif` lines (alone on their line, nestable). A line outside the
    taken branches is dropped together with the directive lines, so with no define the text is the source without the option's
    alternatives, and a source that gains an option stays byte-identical to the old one when the option is off (the object's
    `source_sha256` hashes this text)."""
    out: list[str] = []
    stack: list[bool] = []  # per open #ifdef: its current branch is taken
    for n, line in enumerate(text.split("\n"), 1):
        m = COND.match(line)
        if not m:
            if all(stack):
                out.append(line)
            continue
        kind, name = m.groups()
        if kind in ("ifdef", "ifndef"):
            if not name:
                raise BuildError(f"line {n}: #{kind} needs a name")
            stack.append((name in defines) == (kind == "ifdef"))
        elif not stack:
            raise BuildError(f"line {n}: #{kind} without #ifdef")
        elif kind == "else":
            stack[-1] = not stack[-1]
        else:
            stack.pop()
    if stack:
        raise BuildError("#ifdef without #endif")
    return "\n".join(out)


def sharc_object(
    sources: list[Path],
    include_dirs: list[Path] | None = None,
    defines: frozenset[str] = frozenset(),
) -> dict:
    """One or more `.s` files, concatenated into one translation unit, to an object. `defines` select the `#ifdef` alternatives."""
    dirs = [*(include_dirs or []), *{p.parent for p in sources}]
    text = apply_conditionals(
        expand_includes("\n".join(p.read_text() for p in sources), dirs), defines
    )
    obj = pk_sharc.assemble(text)
    # ISA check: link at a placeholder base with placeholder externs; verify() re-decodes every instruction
    # and reads every relocated field back.
    undefined = sorted({r.symbol for r in obj.relocs} - set(obj.symbols))
    externs = {n: 0xB90000 + 0x40 * i for i, n in enumerate(undefined)}
    for i, name in enumerate(obj.data_symbols):
        externs[name] = 0x20090000 + 0x10 * i
    linked = pk_sharc.link(obj, 0xB90000, externs)
    sections = []
    code_boot = pk_sharc.sel.swap_parcels(obj.section)
    sections.append(
        {
            "name": pk_sharc.SECTION,
            "kind": "text",
            "align": 8,
            "size": len(code_boot),
            "hex": code_boot.hex(),
        }
    )
    for name, (blob, align, bss) in obj.data.items():
        sections.append(
            {
                "name": name,
                "kind": "bss" if bss else "data",
                "align": align,
                "size": len(blob),
                "hex": ""
                if bss
                else blob.hex(),  # bss is reserved by size (dko.rs does not read its hex)
            }
        )
    symbols = []
    for name, (off, glob) in sorted(obj.symbols.items()):
        symbols.append(
            {"name": name, "section": pk_sharc.SECTION, "value": off, "global": glob}
        )
    for name, (sec, off, glob) in sorted(obj.data_symbols.items()):
        symbols.append({"name": name, "section": sec, "value": off, "global": glob})
    for name in undefined:
        if name not in obj.data_symbols:
            symbols.append({"name": name, "section": None, "value": 0, "global": True})
    relocs = [
        {
            "section": pk_sharc.SECTION,
            "offset": r.parcel,
            "type": SHARC_TYPES[r.rtype],
            "symbol": r.symbol,
            "addend": r.addend,
        }
        for r in obj.relocs
    ]
    if linked.ureg_class:
        # a store through an index register whose source register the ISA excludes (pk_sharc.ureg_class_problems): the silicon does not run it
        raise BuildError(
            f"{len(linked.ureg_class)} store(s) the ISA does not allow (finding 49 section 10): "
            + "; ".join(linked.ureg_class)
        )
    return {
        "format": FORMAT,
        "arch": "sharc",
        "source_sha256": obj.source_sha256,
        "toolchain": {**obj.toolchain, "instructions_redecoded": str(linked.checked)},
        "sections": sections,
        "symbols": symbols,
        "relocs": relocs,
    }


def cf_object(sources: list[Path], include_dirs: list[Path] | None = None) -> dict:
    base = 0x47E8B000
    if any(p.name == "Cargo.toml" for p in sources):
        # A Rust crate (tools/patchkit/pk_rust.py): the manifest is the only source entry
        if len(sources) != 1:
            raise BuildError("a Rust object lists its Cargo.toml as the only source")
        try:
            proto = pk_rust.object_proto(sources[0])
        except pk_rust.RustBuildError as e:
            raise BuildError(str(e)) from e
    else:
        # Compile once to learn the undefined symbols, then place them near the base so PC16/PC8 reach.
        with tempfile.TemporaryDirectory(prefix="pk-build-") as d:
            objs = []
            for i, src in enumerate(sources):
                o = Path(d) / f"{i}_{src.stem}.o"
                pk_link.compile_source(src, o, include_dirs)
                objs.append(o)
            merged = objs[0]
            if len(objs) > 1:
                merged = Path(d) / "all.o"
                pk_link.merge_objects(objs, merged)
            with_helpers = Path(d) / "helpers.o"
            if pk_link.resolve_helpers(merged, with_helpers):
                merged = with_helpers  # __udivdi3 and friends, from the ColdFire libgcc
            proto = pk_link.object_from_elf(merged.read_bytes(), sources)
    undefined = [s["name"] for s in proto["symbols"] if s["section"] is None]
    symmap = {n: base + 0x100 + 8 * i for i, n in enumerate(undefined)}
    linked = pk_link.link(proto, base, symmap)
    hist = pk_link.check_isa(linked)
    kind_map = {"text": "text", "rodata": "data", "data": "data", "bss": "bss"}
    sections = []
    for s in proto["sections"]:
        sections.append(
            {
                "name": s["name"],
                "kind": kind_map[s["kind"]],
                "align": s["align"],
                "size": s["size"],
                "hex": s["hex"],
            }
        )
    symbols = [
        {
            "name": s["name"],
            "section": s["section"],
            "value": s["value"],
            "global": s["global"],
        }
        for s in proto["symbols"]
    ]
    seen = {s["name"] for s in symbols}
    relocs = []
    for r in proto["relocs"]:
        t = CF_PLT.get(r["type"], r["type"])
        if t not in CF_TYPES:
            raise BuildError(
                f"relocation type {r['type']} in {r['in']} is not supported (use 32-bit absolute or PC-relative calls)"
            )
        if "to_section" in r:
            name = ".sec." + r["to_section"]
            if name not in seen:
                symbols.append(
                    {
                        "name": name,
                        "section": r["to_section"],
                        "value": 0,
                        "global": False,
                    }
                )
                seen.add(name)
            sym = name
        else:
            sym = r["to_symbol"]
        relocs.append(
            {
                "section": r["in"],
                "offset": r["offset"],
                "type": CF_TYPES[t],
                "symbol": sym,
                "addend": r["addend"],
            }
        )
    symbols = sorted(symbols, key=lambda s: s["name"])
    return {
        "format": FORMAT,
        "arch": "cf",
        "source_sha256": proto["source_sha256"],
        "toolchain": {
            **proto["toolchain"],
            "instructions_redecoded": str(sum(hist.values())),
            # the check passed above (`check_isa` raises otherwise): record it over these bytes, for `patchkit pack`
            ISA_CHECK_KEY: isa_digest(sections, symbols, relocs),
        },
        "sections": sections,
        "symbols": symbols,
        "relocs": relocs,
    }


def build_module(
    moddir: Path,
    check: bool,
    defines: frozenset[str] = frozenset(),
    out_root: Path | None = None,
) -> list[str]:
    """Build every object of a module. With `defines` (build options for the DSP sources) the committed objects are not touched: the
    objects go under `out_root` at their `path` (tools/patchkit/pk_variant.py builds a module variant this way)."""
    if defines and (check or out_root is None):
        raise BuildError("--define needs --out and cannot be combined with --check")
    manifest = tomllib.loads((moddir / "module.toml").read_text())
    messages = []
    for o in manifest.get("object", []):
        srcs = o.get("sources")
        if not srcs:
            continue
        paths = [moddir / s for s in srcs]
        kinds = {p.suffix for p in paths}
        if kinds <= {".s"} and any("dsp" in Path(s).parts for s in srcs):
            obj = sharc_object(paths, [moddir / "build" / "gen", ROOT / "sdk"], defines)
        else:
            if defines:
                raise BuildError("--define applies to DSP sources only")
            obj = cf_object(paths, [moddir / "build" / "gen"])
        text = canonical(obj)
        out = (out_root or moddir) / o["path"]
        if check:
            if not out.exists() or out.read_text() != text:
                raise BuildError(
                    f"{out} is stale: uv run python -m tools.patchkit.pk_build build {moddir}"
                )
            messages.append(f"{out}: current")
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
            n = obj["toolchain"]["instructions_redecoded"]
            messages.append(f"{out}: {n} instructions re-decoded OK")
    return messages


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("module", type=Path)
    b.add_argument("--check", action="store_true")
    b.add_argument(
        "--define",
        action="append",
        default=[],
        metavar="NAME",
        help="a build option (#ifdef NAME in a DSP source)",
    )
    b.add_argument(
        "--out",
        type=Path,
        help="with --define: the directory the objects are written under (at their `path`)",
    )
    c = sub.add_parser("build-all")
    c.add_argument("--check", action="store_true")
    c.add_argument("--root", type=Path, default=ROOT / "modules")
    argv = sys.argv[1:]
    # `mise run module:build [-- MODULE] [--check]`: no subcommand builds every module, a path builds that one
    if not argv or argv[0].startswith("-"):
        argv = ["build-all", *argv]
    elif argv[0] not in ("build", "build-all"):
        argv = ["build", *argv]
    a = ap.parse_args(argv)
    dirs = (
        [a.module]
        if a.cmd == "build"
        else sorted(p.parent for p in a.root.rglob("module.toml"))
    )
    try:
        for d in dirs:
            for m in build_module(
                d,
                a.check,
                frozenset(getattr(a, "define", [])),
                getattr(a, "out", None),
            ):
                print(m)
    except (BuildError, pk_sharc.PkSharcError, pk_link.PkLinkError) as e:
        print(f"pk_build: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
