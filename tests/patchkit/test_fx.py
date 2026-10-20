"""core/fx (modules/core/fx): the effects adapter at H1, on the concrete SHARC+ core (tests/support/fx_rig.py) and through patchkit.

The first group needs only the assembler; the last one patches the stock images (skipped when they are not in the repository)."""

import json
import os
import random
import subprocess
from pathlib import Path

import pytest
from support.fx_rig import FxRig, f2w, w2f
from support.paths import REPO_ROOT

from tools.firmware import config
from tools.patchkit import patchkit as front_end
from tools.patchkit import pk_sharc, pk_toolchains

ROOT = REPO_ROOT

pytestmark = pytest.mark.skipif(
    pk_toolchains.selache_dir() is None,
    reason="selache not built (tools/patchkit/pk_toolchains.py selache)",
)

SIGNALS = {
    "silence": [0.0] * 64,
    "dc": [0.5] * 64,
    "ramp": [i / 64.0 - 0.5 for i in range(64)],
    "full": [1.0 if i % 2 else -1.0 for i in range(64)],
}


def test_an_empty_chain_changes_nothing_in_any_context():
    """No row: the hook returns to the stock function with every register, the context and the master block as they came."""
    rng = random.Random(7)
    rig = FxRig([])
    for n in range(24):
        master = [w2f(f2w(rng.uniform(-1.5, 1.5))) for _ in range(64)]  # float32 values
        b = rig.block(master)
        assert b.returned, b.halt
        assert b.master == tuple(master)
        assert b.ctx_bad == [] and b.regs_bad == [], (n, b.ctx_bad, b.regs_bad)


def test_the_empty_chain_stores_only_in_the_adapters_scratch_words():
    """The only stores are the three save words of the adapter."""
    rig = FxRig([])
    rig.sess.record_stores()
    rig.block(SIGNALS["ramp"], ctx="friendly")
    syms = rig.built.data
    own = {syms[n] for n in ("_fx_s_r0", "_fx_s_i4", "_fx_s_l4")}
    # the core keeps a store to a not yet mapped address at the loader alias (pk_sharc.SW_ALIAS_BASE + address)
    stores = {
        e.address - pk_sharc.SW_ALIAS_BASE
        if e.address >= pk_sharc.SW_ALIAS_BASE
        else e.address
        for e in rig.sess.stores
    }
    assert stores == own, [hex(a) for a in stores ^ own]


def test_the_empty_chain_costs_a_few_instructions():
    rig = FxRig([])
    b = rig.block(SIGNALS["dc"], ctx="friendly")
    assert 10 <= b.instructions <= 24, b.instructions
    print(f"empty chain: {b.instructions} instructions per block on the concrete core")


# ---- patchkit: the hook on both devices ------------------------------------------------------------------------------------
DT2_117 = config.stock("Digitakt_II_OS1.17.syx")
DN2_112 = config.stock("Digitone_II_OS1.12.syx")
needs_images = pytest.mark.skipif(
    not all(p.exists() for p in (DT2_117, DN2_112)),
    reason="stock firmware images not present",
)


@pytest.fixture(scope="module")
def patchkit():
    exe = front_end.binary()

    def run(*args):
        r = subprocess.run(
            [str(exe), "--root", str(ROOT), *map(str, args)],
            capture_output=True,
            text=True,
            env={**os.environ, "PATCHKIT_MODULES": str(ROOT / "out" / "modules")},
        )
        assert r.returncode == 0, f"patchkit {args}: {r.stderr}{r.stdout}"
        return r

    return run


@needs_images
@pytest.mark.parametrize(
    ("fw", "second"),
    [(DT2_117, 0x1C14EA), (DN2_112, 0x1C2450)],
    ids=["dt2-1.17", "dn2-1.12"],
)
def test_the_hook_takes_the_first_instruction_of_the_post_master_function(
    patchkit, tmp_path: Path, fw, second
):
    out = tmp_path / "fx.syx"
    plan_path = tmp_path / "fx.plan.json"
    patchkit(
        "apply",
        "--firmware",
        fw,
        "--modules",
        "core/fx",
        "-o",
        out,
        "--plan",
        plan_path,
    )
    plan = json.loads(plan_path.read_text())
    (hook,) = [h for h in plan["hooks"] if h["anchor"] == "dsp.fx.entry"]
    assert (
        hook["cpu"] == "sharc" and hook["displaced"] == "run" and hook["site_len"] == 6
    )
    assert hook["tail"] is not None
    assert (
        hook["ret"] == second
    )  # the tail returns to the second instruction (the first is 3 parcels, 48 bits)
    assert any(k.endswith(":_fx_entry") for k in plan["symbols"])
    assert plan["symbols"]["core/fx@0.1.0:fx.rows"] > 0
