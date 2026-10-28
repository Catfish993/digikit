"""test/registry-demo: the generic patchkit primitives (P1 section, P2 registry, P3 expressions, P4 hook) on DT2 1.17,
booted on the ColdFire core and called through the harness.

Slow (one ColdFire boot to ready). Skips without the toolchain or the image."""

import json
import struct
import subprocess

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.firmware import config  # noqa: E402
from tools.patchkit import patchkit as front_end  # noqa: E402
from tools.patchkit import (  # noqa: E402
    pk_cfcall,  # noqa: E402
    pk_toolchains,  # noqa: E402
)

DT2_117 = config.stock("Digitakt_II_OS1.17.syx")
SETTER = 0x40035260  # Kit::vfunc_12(obj, v): if (v) obj->field20 = v

pytestmark = pytest.mark.skipif(
    pk_toolchains.m68k_bin("gcc") is None or not DT2_117.exists(),
    reason="m68k toolchain or firmware image missing",
)


def patchkit(*args):
    exe = (
        front_end.binary()
    )  # one build under a lock, then an immutable copy (tools/devtools/stablebin.py)
    r = subprocess.run(
        [str(exe), "--root", str(ROOT), *map(str, args)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    return r.stdout


def test_registry_demo_boots_and_the_hook_runs_the_displaced_instructions(tmp_path):
    out, plan_path = tmp_path / "demo.syx", tmp_path / "plan.json"
    patchkit(
        "apply",
        "--firmware",
        DT2_117,
        "--modules",
        "test/registry-demo",
        "-o",
        out,
        "--plan",
        plan_path,
    )
    plan = json.loads(plan_path.read_text())
    sym = {
        k.split(":", 1)[1]: v
        for k, v in plan["symbols"].items()
        if k.startswith("test/registry-demo")
    }
    reg = plan["registries"][0]
    sec = plan["sections"][0]
    assert reg["count"] == 2 and sec["appended_rows"] == 2
    assert (
        sym["rdemo.one.index"] == reg["first"]
        and sym["rdemo.two.index"] == reg["first"] + 1
    )

    obj = 0x47E90000
    m = pk_cfcall.CfMachine(
        out, state=tmp_path / "boot.state", base_sha256=plan["base"]["syx_sha256"]
    )
    hits = sym["rd_hits"]
    field = (obj + 0x20, 4)
    calls = [
        {"pc": SETTER, "args": [obj, v], "reads": [field, (hits, 4)]} for v in (5, 0, 9)
    ] + [
        {
            "pc": SETTER,
            "args": [obj, 0],
            "reads": [(reg["addr"], reg["bytes"]), (sec["addr"], sec["bytes"])],
        }
    ]
    r = m.run({obj: bytes(64)}, calls)
    assert all(x.ok and not x.clobbered for x in r)
    word = lambda b: struct.unpack(">I", b)[0]  # noqa: E731
    # the setter behaves as stock (the displaced move/branch ran), and every call was counted
    assert [(word(x.reads[0]), word(x.reads[1])) for x in r[:3]] == [
        (5, 1),
        (5, 2),
        (9, 3),
    ]
    # registry rows (value u32, label ptr, fn): defaults and entries, strings behind the rows
    rows, pool = r[3].reads[0][:24], r[3].reads[0][24:]
    v1, p1, f1, v2, p2, f2 = struct.unpack(">6I", rows)
    assert (v1, f1, v2, f2) == (0x1234, sym["rd_hook"], 77, reg["first"])
    assert pool == b"ONE\0TWO\0" and p1 == reg["addr"] + 24 and p2 == p1 + 4
    # the section: the setter's bytes as the image has them, then the appended rows
    copy, rows = r[3].reads[1][:14], r[3].reads[1][14:]
    assert copy == bytes.fromhex("202f00086708206f0004214000204e75")[:14]
    assert struct.unpack(">IBH", rows[:7]) == (0x1234, reg["first"], reg["first"])
    assert struct.unpack(">IBH", rows[7:]) == (77, reg["first"] + 1, reg["first"])


def test_build_options_select_ifdef_alternatives():
    """pk_sharc.apply_conditionals: `#ifdef`/`#ifndef`/`#else`/`#endif` lines; with no define the text is the source without the option's lines."""
    from tools.patchkit import pk_sharc

    src = "a\n#ifdef X\nb\n#else\nc\n#endif\n#ifndef X\nd\n#endif\ne"
    assert pk_sharc.apply_conditionals(src) == "a\nc\nd\ne"
    assert pk_sharc.apply_conditionals(src, frozenset({"X"})) == "a\nb\ne"
    nested = "#ifdef X\n#ifdef Y\ny\n#else\nnoy\n#endif\n#endif\nz"
    assert pk_sharc.apply_conditionals(nested, frozenset({"X"})) == "noy\nz"
    assert pk_sharc.apply_conditionals(nested, frozenset({"Y"})) == "z"
    with pytest.raises(pk_sharc.PkSharcError):
        pk_sharc.apply_conditionals("#ifdef X\na")
    with pytest.raises(pk_sharc.PkSharcError):
        pk_sharc.apply_conditionals("#endif")
