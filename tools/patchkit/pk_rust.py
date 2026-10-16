#!/usr/bin/env python3
"""Rust crates for ColdFire modules: crate -> one relocatable ELF that pk_link.object_from_elf turns into a `.dko.json`.

    uv run python -m tools.patchkit.pk_rust modules/test/rust-demo/src/rs/Cargo.toml [-o out.o]

The crate is `#![no_std]`, `panic = "abort"`, `crate-type = ["staticlib"]` and has its own `[workspace]`. It is built with
the GCC back end for -mcpu=54418 (tools/patchkit/pk_toolchains.py codegen-gcc) and `-Zbuild-std=core,compiler_builtins`
with the `mem` feature: `core`, the integer helpers (`__muldi3`, `__udivdi3`, ...) and `memcpy`/`memset`/`memcmp`
come from Rust sources compiled for the ColdFire, not from the repo's m68k-elf libgcc (68020 code; no ColdFire
multilib). The staticlib is combined with `ld -r --gc-sections`, keeping the crate's own global symbols, so only what
they reach stays. Checked here: ELF flags (ISA_C + EMAC), undefined symbols that should have been provided. Checked by
pk_build: only 32-bit absolute relocations, and every instruction re-decoded by our decoder (pk_link.check_isa).

Rules for the crate: integers and pointers on the `extern "C"` boundary, no struct results (an 8-byte struct result
comes back through a0 from Rust but in d0:d1 from C; finding 23 S1b), statics only in `.rodata`/`.data`/`.bss`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import struct
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from tools.patchkit import pk_toolchains

ROOT = Path(__file__).resolve().parents[2]
CACHE = (ROOT / "out" / "cache" / "rust").resolve()
# Emitted into the object, so identical on every machine: no source paths in panic locations.
RUSTFLAGS_EXTRA = "-Zlocation-detail=none -Zfmt-debug=none"
CARGO_ARGS = [
    "-Zbuild-std=core,compiler_builtins",
    "-Zbuild-std-features=compiler-builtins-mem",
    "-Zjson-target-spec",
]
# Names a Rust crate may leave undefined only if the module's [object.externs] or another module supplies them;
# these are the compiler's own helpers, which must have been resolved inside the object.
HELPER = re.compile(
    r"^(mem(cpy|move|set|cmp)|bcmp|__(mul|div|udiv|mod|umod|ashl|lshr|ashr|clz|ctz|popcount|bswap)\w*"
    r"|__(add|sub|mul|div|neg|eq|ne|lt|le|gt|ge|float|fix|trunc|ext)\w*[sd][fi]\d?|__\w*(ti|di)\d)$"
)
EF_M68K_CF_ISA_MASK, EF_M68K_CF_ISA_C, EF_M68K_CF_MAC_MASK, EF_M68K_CF_EMAC = (
    0x0F,
    6,
    0x30,
    0x20,
)


class RustBuildError(RuntimeError):
    pass


def _tool(name: str) -> str:
    p = pk_toolchains.m68k_bin(name)
    if p is None:
        raise RustBuildError("m68k-elf toolchain not installed (pk_toolchains m68k)")
    return str(p)


def require_toolchain() -> None:
    if not pk_toolchains.codegen_gcc_ready():
        raise RustBuildError(
            "Rust-for-ColdFire toolchain not installed "
            "(uv run python -m tools.patchkit.pk_toolchains codegen-gcc)"
        )


def crate_files(manifest: Path) -> list[Path]:
    """The crate's manifest and sources, and those of its path dependencies (for example `dkrt-sdk`)."""
    d = manifest.parent
    out = [manifest, *sorted((d / "src").rglob("*.rs"))]
    for extra in ("build.rs", "rust-toolchain.toml"):
        if (d / extra).exists():
            out.append(d / extra)
    deps = tomllib.loads(manifest.read_text()).get("dependencies", {})
    for dep in deps.values():
        if isinstance(dep, dict) and "path" in dep:
            sub = (d / dep["path"] / "Cargo.toml").resolve()
            out += [p for p in crate_files(sub) if p not in out]
    return out


def source_sha256(manifest: Path) -> str:
    """The crate's sources (relative names + bytes) and the toolchain pins: what a build depends on."""
    h = hashlib.sha256()
    for p in crate_files(manifest):
        h.update(os.path.relpath(p, manifest.parent).encode() + b"\0" + p.read_bytes())
    h.update(
        "\0".join(
            [
                pk_toolchains.RUST_NIGHTLY,
                pk_toolchains.CG_REV,
                pk_toolchains.GCC_JIT_REV,
                RUSTFLAGS_EXTRA,
            ]
        ).encode()
    )
    h.update(pk_toolchains.rust_target_json().read_bytes())
    return h.hexdigest()


def toolchain_info() -> dict[str, str]:
    env = pk_toolchains.codegen_gcc_env()
    rustc = subprocess.run(
        ["rustc", f"+{pk_toolchains.RUST_NIGHTLY}", "--version"],
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    return {
        "rustc": rustc,
        "rustc_codegen_gcc": pk_toolchains.CG_REV,
        "libgccjit_gcc": pk_toolchains.GCC_JIT_REV,
        "rustflags": f"-Cllvm-args=-mcpu=54418 -Cpanic=abort {RUSTFLAGS_EXTRA}",
        "target": "m68k-cf-none-elf-a2.json",
    }


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        raise RustBuildError(
            f"{' '.join(cmd[:3])} failed:\n{r.stdout[-3000:]}{r.stderr[-3000:]}"
        )
    return r


def build_staticlib(manifest: Path) -> Path:
    require_toolchain()
    manifest = manifest.resolve()
    meta = tomllib.loads(manifest.read_text())
    name = meta["package"]["name"].replace("-", "_")
    if "staticlib" not in meta.get("lib", {}).get("crate-type", []):
        raise RustBuildError(f'{manifest}: [lib] crate-type must be ["staticlib"]')
    if meta.get("profile", {}).get("release", {}).get("panic") != "abort":
        raise RustBuildError(f'{manifest}: [profile.release] panic must be "abort"')
    target_dir = CACHE / f"{ROOT.name}-{manifest.parent.parent.parent.name}"
    env = pk_toolchains.codegen_gcc_env(RUSTFLAGS_EXTRA)
    target = pk_toolchains.rust_target_json()
    cmd = [
        "cargo", f"+{pk_toolchains.RUST_NIGHTLY}", "rustc", "--release", *CARGO_ARGS,
        "--target", str(target), "--target-dir", str(target_dir),
        "--manifest-path", str(manifest),
    ]  # fmt: skip
    _run(cmd, env=env, cwd=manifest.parent)
    lib = target_dir / target.stem / "release" / f"lib{name}.a"
    if not lib.exists():
        raise RustBuildError(f"{lib} was not produced")
    return lib


def _members(lib: Path, workdir: Path) -> list[Path]:
    _run([_tool("ar"), "x", str(lib.resolve())], cwd=workdir)
    return sorted(workdir.glob("*.o"))


def _is_empty(obj: Path) -> bool:
    """True when the object has no code, data or bss (`size` prints 0 0 0)."""
    out = _run([_tool("size"), str(obj)]).stdout.split("\n")[1].split()
    return out[:3] == ["0", "0", "0"]


def _globals(obj: Path) -> tuple[list[str], list[str]]:
    """(defined global symbols, undefined symbols) of an object, by `nm`."""
    out = _run([_tool("nm"), "--no-demangle", str(obj)]).stdout.splitlines()
    defined, undefined = [], []
    for line in out:
        parts = line.split()
        if len(parts) == 2 and parts[0] == "U":
            undefined.append(parts[1])
        elif len(parts) == 3 and parts[1] in "TDBRWVSA":
            defined.append(parts[2])
    return defined, undefined


def e_flags(blob: bytes) -> int:
    return struct.unpack_from(">I", blob, 36)[0]


def check_flags(blob: bytes, what: str) -> None:
    f = e_flags(blob)
    if (f & EF_M68K_CF_ISA_MASK) != EF_M68K_CF_ISA_C or (
        f & EF_M68K_CF_MAC_MASK
    ) != EF_M68K_CF_EMAC:
        raise RustBuildError(
            f"{what}: ELF flags {f:#x} are not ColdFire ISA_C + EMAC (a 68020 or generic m68k object got in)"
        )


def build_object(manifest: Path, out: Path) -> list[str]:
    """Crate -> one relocatable ELF at `out`. Returns the exported symbols."""
    lib = build_staticlib(manifest)
    crate = tomllib.loads(manifest.read_text())["package"]["name"].replace("-", "_")
    with tempfile.TemporaryDirectory(prefix="pk-rust-") as d:
        work = Path(d)
        members = _members(lib, work)
        exports: list[str] = []
        for m in members:
            if _is_empty(m):
                continue  # a crate with only types (dkrt-sdk) compiles to an object with no sections; its flags are 0
            check_flags(m.read_bytes(), f"archive member {m.name}")
            if m.name.startswith((crate + "-", crate + ".")):
                exports += _globals(m)[0]
        if not exports:
            raise RustBuildError(
                f"{manifest}: the crate exports no global symbol (#[no_mangle] pub ...)"
            )
        # Rust-mangled names (_R...) are internal (the panic handler, helpers with crate hashes): not roots
        exports = sorted({e for e in exports if not e.startswith("_R")})
        cmd = [_tool("ld"), "-r", "--gc-sections", "-o", str(out)]
        for e in exports:
            cmd += ["-u", e]
        _run([*cmd, str(lib.resolve())])
        # Internal names carry crate hashes: make them local, then drop the locals nothing relocates against, so
        # the object does not depend on where the crate was built.
        _run([_tool("objcopy"), "-w", "-L", "_R*", str(out)])
        _run([_tool("strip"), "--strip-unneeded", str(out)])
    check_flags(out.read_bytes(), str(out))
    undefined = _globals(out)[1]
    missing = sorted(u for u in undefined if HELPER.match(u))
    if missing:
        raise RustBuildError(
            f"{manifest}: compiler helper(s) left undefined: {', '.join(missing)} "
            "(they must come from compiler_builtins; the repo's libgcc is 68020 code and is not linked)"
        )
    return exports


def check_relocs(proto: dict) -> None:
    for r in proto["relocs"]:
        if r["type"] != "32":
            raise RustBuildError(
                f"relocation type R_68K_{r['type']} in {r['in']} is not supported in Rust modules "
                "(only R_68K_32; a PC-relative reference came from a codegen change)"
            )


def object_proto(manifest: Path) -> dict:
    """The object prototype of pk_link.object_from_elf for a crate (pk_build finishes it as for C)."""
    from tools.patchkit import pk_link

    manifest = manifest.resolve()
    with tempfile.TemporaryDirectory(prefix="pk-rust-o-") as d:
        o = Path(d) / "crate.o"
        exports = build_object(manifest, o)
        proto = pk_link.object_from_elf(o.read_bytes(), [manifest])
    proto["source_sha256"] = source_sha256(manifest)
    proto["sources"] = [
        os.path.relpath(p, manifest.parent) for p in crate_files(manifest)
    ]
    proto["toolchain"] = {**toolchain_info(), "exports": " ".join(exports)}
    check_relocs(proto)
    return proto


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("crate.o"))
    a = ap.parse_args()
    try:
        ex = build_object(a.manifest.resolve(), a.out)
    except RustBuildError as e:
        print(f"pk_rust: {e}", file=sys.stderr)
        return 1
    print(f"{a.out}: exports {' '.join(ex)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
