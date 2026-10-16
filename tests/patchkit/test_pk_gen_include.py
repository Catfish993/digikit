"""The SHARC include that `patchkit gen` writes (modules/*/build/gen/<device>.inc) is usable from a DSP source.

pluck's DSP sources keep their literals (the committed objects stay as they are); this test rewrites the frame
constants in a copy to the generated names and checks that the assembled code, data, symbols and relocations equal
the committed object. Skips when Selache is not built."""

import json
import re
import tempfile
from pathlib import Path

import pytest
from support.paths import REPO_ROOT

from tools.patchkit import (
    pk_build,
    pk_toolchains,
)

pytestmark = pytest.mark.skipif(
    pk_toolchains.selache_dir() is None,
    reason="selache not built (tools/patchkit/pk_toolchains.py selache)",
)

MOD = REPO_ROOT / "modules" / "machines" / "pluck"

# (source, include, committed object, [(literal in the raw-parameter block, generated name)])
CASES = [
    (
        "pluck.s",
        "dt2.inc",
        "pluck-dsp.dko.json",
        [
            ("R1 = 0x6d;", "R1 = FRAME_BASE;"),
            ("R0 = R13 + 1;", "R0 = R13 + PLUCK_DECAY_FRAME_OFF;"),
            ("R1 = 2;", "R1 = PLUCK_PICK_FRAME_OFF;"),
            ("R1 = 6;", "R1 = PLUCK_BRIGHT_FRAME_OFF;"),
            ("R1 = 7;", "R1 = PLUCK_COLOR_FRAME_OFF;"),
        ],
    ),
    (
        "pluck_dn2.s",
        "dn2.inc",
        "pluck-dn2-dsp.dko.json",
        [
            ("R1 = 0x6d;", "R1 = FRAME_BASE;"),
            ("R1 = 1;", "R1 = PLUCK_PICK_FRAME_OFF;"),
            ("R1 = 2;", "R1 = PLUCK_BRIGHT_FRAME_OFF;"),
            ("R1 = 3;", "R1 = PLUCK_COLOR_FRAME_OFF;"),
        ],
    ),
]


@pytest.mark.parametrize("src,inc,dko,pairs", CASES)
def test_generated_include_reproduces_the_committed_object(src, inc, dko, pairs):
    text = (MOD / "src" / "dsp" / src).read_text()
    at = text.index("// raw parameters")
    head, block = text[:at], text[at:]
    for old, new in pairs:
        assert old in block, old
        block = block.replace(old, new, 1)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / src
        p.write_text(f'#include "{inc}"\n' + head + block)
        new_obj = pk_build.sharc_object([p], [MOD / "build" / "gen"])
    old_obj = json.loads((MOD / "build" / dko).read_text())
    for key in ("sections", "symbols", "relocs"):
        assert new_obj[key] == old_obj[key], key


def test_include_expansion():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.inc").write_text("#define A 1\n")
        out = pk_build.expand_includes('x\n#include "a.inc"\ny', [Path(d)])
        assert out == "x\n#define A 1\n\ny"
        with pytest.raises(pk_build.BuildError):
            pk_build.expand_includes('#include "missing.inc"', [Path(d)])


def test_generated_include_has_no_hex_literals_after_a_plus():
    for inc in MOD.glob("build/gen/*.inc"):
        assert not re.search(r"^#define \S+ 0x", inc.read_text(), re.M), inc
