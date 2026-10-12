#!/usr/bin/env python3
"""Call functions on the emulated ColdFire core (the patchkit unit-test harness).

Boots a ready DT2/DN2 image once (state cached under out/cache/pk_cfcall/), pokes linked module bytes into
RAM, then runs functions with the gcc m68k ABI (arguments on the stack, result in D0, D2-D7/A2-A6 callee-saved)
and returns the registers and requested memory. Backed by native/boot/examples/cf_call.rs.

    from pk_cfcall import CfMachine
    m = CfMachine("Digitakt_II_OS1.17.syx")
    r = m.call(0x47e8b000, [ptr, n], pokes={0x47e8b000: code, ptr: data}, reads=[(ptr, 16)])
    r.d0, r.reads[0]

The scratch stack and the return sentinel default to the top of the spike 0.3 expansion region
(sp 0x47fe0000, ret 0x47fdf000); interrupts are masked (SR 0x2700) unless ``sr`` is given.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "target" / "release" / "examples" / "cf_call"
CACHE = (ROOT / "out" / "cache" / "pk_cfcall").resolve()
CALLEE_SAVED = [f"d{i}" for i in range(2, 8)] + [f"a{i}" for i in range(2, 7)]


class CfCallError(RuntimeError):
    pass


@dataclass
class CallResult:
    ok: bool
    d: list[int]
    a: list[int]
    pc: int
    insns: int
    reads: list[bytes]
    error: str | None
    clobbered: list[str]

    @property
    def d0(self) -> int:
        return self.d[0]

    @property
    def a0(self) -> int:
        return self.a[0]


_built = False


def build_example() -> Path:
    """cargo keeps the example current (incremental, about a second when nothing changed)."""
    global _built
    if not _built:
        r = subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "-p",
                "elektron-native-boot",
                "--example",
                "cf_call",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if r.returncode:
            raise CfCallError(f"cannot build cf_call:\n{r.stderr[-2000:]}")
        _built = True
    return EXAMPLE


class CfMachine:
    def __init__(
        self, syx: str | Path, state: str | Path | None = None, timeout: int = 600
    ):
        self.syx = Path(syx)
        if not self.syx.is_absolute():
            self.syx = ROOT / self.syx
        CACHE.mkdir(parents=True, exist_ok=True)
        self.state = Path(state) if state else CACHE / (self.syx.stem + ".state")
        self.timeout = timeout

    def run(self, pokes: dict[int, bytes], calls: list[dict]) -> list[CallResult]:
        """Poke bytes, then run each call dict ({pc, args, reads:[(addr,len)], sp, ret, sr, max}); memory persists between calls."""
        req = {
            "pokes": [{"addr": hex(a), "hex": b.hex()} for a, b in pokes.items()],
            "calls": [
                {
                    **{
                        k: (
                            hex(v)
                            if isinstance(v, int) and k in ("pc", "sp", "ret", "sr")
                            else v
                        )
                        for k, v in c.items()
                        if k != "reads"
                    },
                    "args": [str(x & 0xFFFFFFFF) for x in c.get("args", [])],
                    "reads": [
                        {"addr": hex(a), "len": n} for a, n in c.get("reads", [])
                    ],
                }
                for c in calls
            ],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            json.dump(req, f)
            f.flush()
            r = subprocess.run(
                [
                    str(build_example()),
                    str(self.syx),
                    f.name,
                    "--state",
                    str(self.state),
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        if r.returncode:
            raise CfCallError(f"cf_call failed:\n{r.stderr[-2000:]}")
        out = []
        for line in r.stdout.splitlines():
            if not line.startswith("{"):
                continue
            j = json.loads(line)
            names = [f"d{i}" for i in range(8)] + [f"a{i}" for i in range(8)]
            before, after = j["d_in"] + j["a_in"], j["d"] + j["a"]
            bad = [
                n
                for n, x, y in zip(names, before, after, strict=True)
                if n in CALLEE_SAVED and x != y
            ]
            if j["ok"] and j["a"][7] != j["sp0"] + 4:
                bad.append("a7")
            out.append(
                CallResult(
                    j["ok"],
                    j["d"],
                    j["a"],
                    j["pc"],
                    j["insns"],
                    [bytes.fromhex(h) for h in j["reads"]],
                    j["error"],
                    bad,
                )
            )
        if len(out) != len(calls):
            raise CfCallError(
                f"expected {len(calls)} results, got {len(out)}:\n{r.stdout}"
            )
        return out

    def call(
        self,
        pc: int,
        args: list[int] = (),
        pokes: dict[int, bytes] | None = None,  # type: ignore[assignment]
        reads: list[tuple[int, int]] | None = None,
        **kw,
    ) -> CallResult:
        return self.run(
            pokes or {}, [{"pc": pc, "args": list(args), "reads": reads or [], **kw}]
        )[0]
