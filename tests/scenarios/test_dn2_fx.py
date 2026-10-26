"""Oracle tests for the DN2 effects: relationships that were measured in
docs/findings/15-sharc-sample-path.md and checked against the Digitone II
manual, rendered headless. A failure means the emulated DSP (or the
translator) changed an effect.

The fast subset runs by default; ``--sweep`` (or ``-m sweep``) adds the full
sweeps. Analysis lives in tools/emulator/fxcheck.py and tools/emulator/audiocheck.py.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
from support.paths import REPO_ROOT

sweep = pytest.mark.sweep
SCENARIOS = REPO_ROOT / "tests" / "scenarios"

# --- scenario sets ------------------------------------------------------------

SF_FAST = (
    "sf-del-ref", "sf-del-time-u20", "sf-del-fb-d127", "sf-del-width-d127",
    "sf-rev-ref", "sf-rev-decay-d127", "sf-rev-decay-u20",
    "sf-chr-ref", "sf-chr-speed-d127", "sf-chr-speed-u20", "sf-chr-width-d127",
)  # fmt: skip
SF_SWEEP = (
    "sf-del-time-u44", "sf-del-time-u127", "sf-del-width-d44", "sf-del-width-u44",
    "sf-del-width-u127", "sf-del-mix-d127", "sf-rev-mix-d127", "sf-chr-mix-d127",
    "sf-rev-decay-u44", "sf-chr-speed-u44", "sf-chr-speed-u127",
    "sf-chr-width-d44", "sf-chr-width-d20", "sf-del-fb-u44", "sf-del-fb-u127",
    "sf-rev-decay-u127",
)  # fmt: skip
# Feedback beyond unity swells until the output saturates: bounded, not silent.
RUNAWAY = ("sf-del-fb-u44", "sf-del-fb-u127", "sf-rev-decay-u127")
# One echo (2 s apart) lands in the last tenth of the run while the middle is
# silent, which the growth heuristic (audiocheck.basic) reads as growth: check
# that the peak stays near the dry peak instead.
SPARSE_ECHO = ("sf-del-time-u127",)


def params(fast: tuple[str, ...], more: tuple[str, ...] = ()) -> list[Any]:
    """pytest params for scenario names; the extra ones belong to the sweep."""
    out = [pytest.param(n, marks=pytest.mark.scn(n), id=n) for n in fast]
    out += [pytest.param(n, marks=[sweep, pytest.mark.scn(n)], id=n) for n in more]
    return out


def series(fast: tuple[str, ...], full: tuple[str, ...]) -> list[Any]:
    return [
        pytest.param(fast, marks=pytest.mark.scn(*fast), id="fast"),
        pytest.param(full, marks=[sweep, pytest.mark.scn(*full)], id="sweep"),
    ]


# --- helpers --------------------------------------------------------------------


def dry_of(render: Any, r: Any) -> Any:
    from tools.emulator.fxcheck import reference_for

    return render(reference_for(r.name, r.hold))


def sf_ref_of(render: Any, r: Any) -> Any:
    return render(f"sf-{r.name.split('-')[1]}-ref")


def rising(values: list[float]) -> bool:
    return all(b > a for a, b in zip(values, values[1:], strict=False))


def assert_ordered(
    render: Any,
    names: tuple[str, ...],
    key: str,
    metric: Callable[[Any], float],
    increasing: bool = True,
) -> None:
    """Metric of the scenarios, ordered by the stored value of ``key`` (a
    read-back prefix such as 'fx[26]'), rises (or falls) strictly."""
    rs = render(*names)
    rs = rs if isinstance(rs, list) else [rs]
    rs = sorted(rs, key=lambda r: r.raw(key) or 0)
    got = [(r.name, r.raw(key), metric(r)) for r in rs]
    vals = [m for _, _, m in got]
    assert len({v for _, v, _ in got}) == len(got), f"settings not distinct: {got}"
    assert all(math.isfinite(v) for v in vals), got
    ok = rising(vals) if increasing else rising([-v for v in vals])
    assert ok, f"not monotone {'rising' if increasing else 'falling'}: {got}"


# --- every scenario ---------------------------------------------------------------


def every_scenario() -> tuple[str, ...]:
    """Names in the sweep files that no other list names (the full sweep)."""
    known = set(SF_FAST + SF_SWEEP)
    names = []
    for fn in ("dn2-sendfx.json",):
        for sc in json.loads((SCENARIOS / fn).read_text())["scenarios"]:
            if sc["name"] not in known:
                names.append(sc["name"])
    return tuple(names)


@pytest.mark.parametrize(
    "name",
    params(SF_FAST, SF_SWEEP + every_scenario()),
)
def test_healthy(render: Any, name: str) -> None:
    """Finite samples, no DC, gates ok; no runaway growth except where the
    feedback is above unity (then peak <= 1.0)."""
    r = render(name)
    b = r.row["analysis"]["basic"]
    assert r.entry["gates"]["ok"], r.entry["gates"]
    assert b["nonfinite"] == 0
    assert max(abs(v) for v in b["dc"]) < 1e-3, b["dc"]
    assert b["peak"] <= 1.0, b["peak"]
    if name in SPARSE_ECHO:
        dry_peak = dry_of(render, r).row["analysis"]["basic"]["peak"]
        assert b["peak"] <= 1.5 * dry_peak, (b["peak"], dry_peak)
    elif name not in RUNAWAY:
        assert not b["growth"], b


# --- read-back --------------------------------------------------------------------

# scenario -> (read-back prefix, intended UI value); None: nothing may change.
INTENT: dict[str, tuple[str, int] | None] = {
    "sf-del-time-u20": ("fx[32]", 32), "sf-del-fb-d127": ("fx[35]", 0),
    "sf-del-width-d127": ("fx[34]", 0), "sf-rev-decay-d127": ("fx[42]", 0),
    "sf-rev-decay-u20": ("fx[42]", 40), "sf-chr-speed-d127": ("fx[26]", 0),
    "sf-chr-speed-u20": ("fx[26]", 52), "sf-chr-width-d127": ("fx[28]", 0),
}  # fmt: skip


@pytest.mark.parametrize("name", params(tuple(INTENT)))
def test_readback(render: Any, name: str) -> None:
    """The intended parameter reached its value and no other stored
    parameter differs from the scenario's reference."""
    r = render(name)
    ref = sf_ref_of(render, r) if name.startswith("sf-") else dry_of(render, r)
    changed = r.changed(ref)
    want = INTENT[name]
    if want is None:
        assert changed == {}, changed
        return
    prefix, ui = want
    assert {k.split(" ")[0]: v for k, v in changed.items()} == {prefix: ui}, changed


# --- controls at 0 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    params(
        (),
        ("sf-del-mix-d127", "sf-rev-mix-d127") + ("sf-chr-mix-d127",),
    ),
)
def test_zero_is_dry(render: Any, name: str) -> None:
    """A control at 0 (or a mix volume at 0) is bit-identical to dry."""
    r = render(name)
    dry = dry_of(render, r)
    assert r.x.shape == dry.x.shape
    assert np.array_equal(r.x, dry.x), float(np.abs(r.x - dry.x).max())


# --- L == R --------------------------------------------------------------------------

MONO = ("sf-del-ref",)


@pytest.mark.parametrize("name", params(MONO))
def test_mono_stays_mono(render: Any, name: str) -> None:
    """Mono source and mono effect (bit/rate reduction, overdrive, the delay
    with X off and WID centred): left equals right sample for sample."""
    x = render(name).x
    assert np.array_equal(x[:, 0], x[:, 1])


# --- delay ---------------------------------------------------------------------------

SPACING = ("sf-del-ref", "sf-del-time-u20")
SPACING_ALL = ("sf-del-ref", "sf-del-time-u20", "sf-del-time-u44", "sf-del-time-u127")


@pytest.mark.parametrize("names", series(SPACING, SPACING_ALL))
def test_delay_spacing(render: Any, names: Any) -> None:
    """Echo spacing = (TIME + 1) x 15.625 ms at 120 BPM (manual 12.2: TIME is
    in 128th notes), within 2 ms. TIME is the stored value / 256."""
    for r in render(*names):
        time = r.raw("fx[32]") / 256
        want = (time + 1) * 0.015625
        got = r.row["sf"]["spacing_s"]
        assert got is not None and abs(got - want) < 0.002, (r.name, got, want)


@pytest.mark.scn("sf-del-fb-d127")
def test_delay_feedback_zero_one_echo(render: Any) -> None:
    r = render("sf-del-fb-d127")
    assert r.raw("fx[35]") == 0
    assert len(r.row["delay"]["echo_onsets_s"]) == 1, r.row["delay"]


@pytest.mark.scn("sf-del-ref")
def test_delay_default_feedback_minus_6_db(render: Any) -> None:
    """Feedback 32 (default) = gain 0.5: about -6 dB per repeat."""
    r = render("sf-del-ref")
    assert r.ui("fx[35]") == 32
    step = r.row["sf"]["db_per_repeat"]
    assert r.row["sf"]["n_echoes"] >= 4
    assert abs(step + 6.0) <= 1.0, step


@pytest.mark.scn("sf-del-width-d127")
def test_delay_wid_min_is_left(render: Any) -> None:
    """X off, WID at its minimum: the delay is hard left (manual 12.2)."""
    r = render("sf-del-width-d127")
    sf = r.row["sf"]
    assert r.ui("fx[33]") == 0 and r.ui("fx[34]") == 0
    assert sf["wet_l_db"] - sf["wet_r_db"] >= 40, (sf["wet_l_db"], sf["wet_r_db"])


@sweep
@pytest.mark.scn("sf-del-width-u44")
def test_delay_wid_max_is_right(render: Any) -> None:
    r = render("sf-del-width-u44")
    sf = r.row["sf"]
    assert r.ui("fx[34]") == 127
    assert sf["wet_r_db"] - sf["wet_l_db"] >= 30, (sf["wet_l_db"], sf["wet_r_db"])


@sweep
@pytest.mark.scn("sf-del-fb-u127", "sf-del-fb-u44")
def test_delay_feedback_above_unity_swells(render: Any) -> None:
    """Above unity the repeats grow (+dB per repeat) and stay bounded."""
    for n in ("sf-del-fb-u44", "sf-del-fb-u127"):
        r = render(n)
        assert r.row["sf"]["db_per_repeat"] > 0, r.row["sf"]
        assert r.row["analysis"]["basic"]["peak"] <= 1.0


# --- reverb --------------------------------------------------------------------------

REV = ("sf-rev-decay-d127", "sf-rev-ref", "sf-rev-decay-u20")
REV_ALL = ("sf-rev-decay-d127", "sf-rev-ref", "sf-rev-decay-u20", "sf-rev-decay-u44")


@pytest.mark.parametrize("names", series(REV, REV_ALL))
def test_reverb_rt60_rises_with_decay(render: Any, names: Any) -> None:
    assert_ordered(render, names, "fx[42]", lambda r: r.row["sf"]["rt60_s"])


# --- chorus --------------------------------------------------------------------------

CHR_SPEED = ("sf-chr-speed-d127", "sf-chr-ref", "sf-chr-speed-u20")
CHR_SPEED_ALL = (*CHR_SPEED, "sf-chr-speed-u44", "sf-chr-speed-u127")
CHR_WIDTH = ("sf-chr-width-d127", "sf-chr-ref")
CHR_WIDTH_ALL = (
    "sf-chr-width-d127",
    "sf-chr-width-d44",
    "sf-chr-width-d20",
    "sf-chr-ref",
)


@pytest.mark.parametrize("names", series(CHR_SPEED, CHR_SPEED_ALL))
def test_chorus_rate_rises_with_speed(render: Any, names: Any) -> None:
    assert_ordered(render, names, "fx[26]", lambda r: r.row["sf"]["mod_phase_hz"])


@pytest.mark.parametrize("names", series(CHR_WIDTH, CHR_WIDTH_ALL))
def test_chorus_decorrelates_with_width(render: Any, names: Any) -> None:
    """The wet left and right correlate fully at width 0 and less as width rises."""
    assert_ordered(
        render, names, "fx[28]", lambda r: r.row["sf"]["wet_corr"], increasing=False
    )
