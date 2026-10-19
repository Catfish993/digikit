"""The SHARC include that `patchkit gen` writes (modules/*/build/gen/<device>.inc) is usable from a DSP source.

The adapter of core/dsp-voice includes `adapter-<device>.inc` (its committed objects are checked by `pk_build --check`); this file
tests the include expansion and the form of the generated defines. Skips when Selache is not built."""

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

MOD = REPO_ROOT / "modules" / "core" / "dsp-voice"

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
