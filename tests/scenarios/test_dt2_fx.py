"""Oracle tests for the Digitakt II effects, rendered headless on the one-sample
card (a 1 kHz sine, 1 s; tools/dt2_sample_check.py): run with ``--device dt2``.

    uv run python -m pytest tests/scenarios/test_dt2_fx.py --device dt2 --slow --sweep

Scenarios are scenarios/dt2-trackfx.json and dt2-sendfx.json (tools/gen_dt2fx.py).
Each is checked twice: the read-back of the live Sound and Fx arrays (the intended
parameter reached its value, nothing else changed) and a relationship of the
audio against the dry reference or against its neighbours in the sweep (the
effect moves in the expected direction). The DN2 tests (test_dn2_fx.py) are
dropped under ``--device dt2`` and these under ``--device dn2``.
"""

from __future__ import annotations

import json
import math
import pathlib
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

sweep = pytest.mark.sweep
SCENARIOS = pathlib.Path(__file__).resolve().parents[2] / "scenarios"

TF_FAST = (
    "tf-dry-hold", "tf-dry-tap", "tf-dry-flt",
    "tf-br-0", "tf-br-22", "tf-br-54", "tf-br-127",
    "tf-over-22", "tf-over-54", "tf-over-127",
    "tf-srr-22", "tf-srr-54", "tf-srr-127",
    "tf-del-54", "tf-del-127", "tf-rev-54", "tf-rev-127", "tf-chr-54", "tf-chr-127",
    "tf-srr-127-flt", "tf-srr-127-flt-rt1", "tf-over-127-flt", "tf-over-127-flt-rt1",
)  # fmt: skip
SF_FAST = (
    "sf-dry-tap", "sf-dry-hold",
    "sf-del-ref", "sf-del-time-d50", "sf-del-time-u20", "sf-del-fb-d50",
    "sf-del-fb-u20", "sf-del-width-d50", "sf-del-width-u50", "sf-del-lpf-d50",
    "sf-del-mix-d50", "sf-del-mix-u50",
    "sf-rev-ref", "sf-rev-decay-d50", "sf-rev-decay-u20", "sf-rev-pre-u50",
    "sf-rev-mix-d50",
    "sf-chr-ref", "sf-chr-depth-d50", "sf-chr-speed-d50", "sf-chr-speed-u50",
    "sf-chr-mix-d50",
    "sf-mst-mix", "sf-mst-thr0", "sf-mst-ratio", "sf-mst-makeup",
)  # fmt: skip
# Feedback above unity swells until the output saturates: bounded, not silent.
RUNAWAY = ("sf-del-fb-u50",)
# One echo (2 s after the note) lands in the last tenth of the run while the
# middle is silent, which the growth heuristic reads as growth.
SPARSE_ECHO = ("sf-del-time-u50",)


def params(fast: tuple[str, ...], more: tuple[str, ...] = ()) -> list[Any]:
    out = [pytest.param(n, marks=pytest.mark.scn(n), id=n) for n in fast]
    out += [pytest.param(n, marks=[sweep, pytest.mark.scn(n)], id=n) for n in more]
    return out


def every_scenario() -> tuple[str, ...]:
    known = set(TF_FAST + SF_FAST)
    names = []
    for fn in ("dt2-trackfx.json", "dt2-sendfx.json"):
        for sc in json.loads((SCENARIOS / fn).read_text())["scenarios"]:
            if sc["name"] not in known:
                names.append(sc["name"])
    return tuple(names)


# --- helpers ------------------------------------------------------------------


def dry_of(render: Any, r: Any) -> Any:
    from fxcheck import reference_for

    return render(reference_for(r.name, r.hold))


def sf_ref_of(render: Any, r: Any) -> Any:
    return render(f"sf-{r.name.split('-')[1]}-ref")


def ordered(
    render: Any, names: tuple[str, ...], key: str, metric: Callable[[Any], float]
) -> list[tuple[str, int | None, float]]:
    """(name, stored value, metric) sorted by the stored value of ``key``."""
    rs = render(*names)
    rs = rs if isinstance(rs, list) else [rs]
    rs = sorted(rs, key=lambda r: r.raw(key) or 0)
    got = [(r.name, r.raw(key), metric(r)) for r in rs]
    assert len({v for _, v, _ in got}) == len(got), f"settings not distinct: {got}"
    assert all(math.isfinite(m) for _, _, m in got), got
    return got


def assert_rises(
    render: Any,
    names: tuple[str, ...],
    key: str,
    metric: Callable[[Any], float],
    increasing: bool = True,
) -> None:
    got = ordered(render, names, key, metric)
    vals = [m if increasing else -m for _, _, m in got]
    ok = all(b > a for a, b in zip(vals, vals[1:], strict=False))
    assert ok, f"not monotone {'rising' if increasing else 'falling'}: {got}"


# --- every scenario --------------------------------------------------------------


@pytest.mark.parametrize("name", params(TF_FAST + SF_FAST, every_scenario()))
def test_healthy(render: Any, name: str) -> None:
    """Finite samples, no DC, gates ok, no runaway growth except above unity."""
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


# --- read-back ----------------------------------------------------------------------

# scenario -> stored parameters that differ from its reference (the dry run for
# tf-*, sf-dry-hold for sf-mst-*, sf-<fx>-ref for the other sf-*), by read-back
# prefix, with the UI value (high byte of the s16) the firmware reached. The
# encoder accelerates by the time between events, so the counts of
# tools/gen_dt2fx.py were measured to land on these values (UPS).
INTENT: dict[str, dict[str, int]] = {
    "sf-del-time-d50": {'fx[32]': 0},
    "sf-del-time-u20": {'fx[32]': 30},
    "sf-del-time-u50": {'fx[32]': 100},
    "sf-del-pp-u20": {'fx[33]': 1},
    "sf-del-width-d50": {'fx[34]': 0},
    "sf-del-width-u50": {'fx[34]': 127},
    "sf-del-fb-d50": {'fx[35]': 0},
    "sf-del-fb-u20": {'fx[35]': 39},
    "sf-del-fb-u50": {'fx[35]': 109},
    "sf-del-lpf-d50": {'fx[37]': 0},
    "sf-del-rsend-u50": {'fx[38]': 127},
    "sf-del-mix-d50": {'fx[39]': 0},
    "sf-del-mix-u50": {'fx[39]': 127},
    "sf-rev-pre-u50": {'fx[41]': 127},
    "sf-rev-decay-d50": {'fx[42]': 0},
    "sf-rev-decay-u20": {'fx[42]': 37},
    "sf-rev-decay-u50": {'fx[42]': 108},
    "sf-rev-lpf-d50": {'fx[46]': 0},
    "sf-rev-mix-d50": {'fx[47]': 0},
    "sf-chr-depth-d50": {'fx[25]': 0},
    "sf-chr-speed-d50": {'fx[26]': 0},
    "sf-chr-speed-u50": {'fx[26]': 127},
    "sf-chr-mix-d50": {'fx[31]': 0},
    "sf-chr-dsend-u50": {'fx[29]': 127},
    "sf-del-ppwidth-d50": {'fx[33]': 1, 'fx[34]': 0},
    "sf-del-ppwidth-u50": {'fx[33]': 1, 'fx[34]': 127},
    "sf-mst-mix": {'fx[67]': 127},
    "sf-mst-thr0": {'fx[60]': 0, 'fx[67]': 127},
    "sf-mst-ratio": {'fx[60]': 0, 'fx[64]': 7, 'fx[67]': 127},
    "sf-mst-makeup": {'fx[63]': 127, 'fx[67]': 127},
    "tf-br-22": {'snd[64]': 22},
    "tf-br-54": {'snd[64]': 54},
    "tf-br-127": {'snd[64]': 127},
    "tf-over-22": {'snd[67]': 22},
    "tf-over-54": {'snd[67]': 54},
    "tf-over-127": {'snd[67]': 127},
    "tf-srr-22": {'snd[65]': 22},
    "tf-srr-54": {'snd[65]': 54},
    "tf-srr-127": {'snd[65]': 127},
    "tf-br-0": {},
    "tf-srr-127-rt1": {'snd[65]': 127, 'snd[66]': 1},
    "tf-over-127-rt1": {'snd[67]': 127, 'snd[68]': 1},
    "tf-del-54": {'snd[55]': 54},
    "tf-del-127": {'snd[55]': 127},
    "tf-rev-54": {'snd[56]': 54},
    "tf-rev-127": {'snd[56]': 127},
    "tf-chr-54": {'snd[57]': 54},
    "tf-chr-127": {'snd[57]': 127},
    "tf-srr-127-flt": {'snd[65]': 127},
    "tf-srr-127-flt-rt1": {'snd[65]': 127, 'snd[66]': 1},
    "tf-over-127-flt": {'snd[67]': 127},
    "tf-over-127-flt-rt1": {'snd[67]': 127, 'snd[68]': 1},
}  # fmt: skip


@pytest.mark.parametrize("name", params(tuple(INTENT)))
def test_readback(render: Any, name: str) -> None:
    """The intended parameters reached their value and no other stored
    parameter differs from the scenario's reference."""
    r = render(name)
    fx = name.split("-")[1]
    use_sf_ref = name.startswith("sf-") and fx != "mst"
    ref = sf_ref_of(render, r) if use_sf_ref else dry_of(render, r)
    changed = {k.split(" ")[0]: v for k, v in r.changed(ref).items()}
    assert changed == INTENT[name], changed


@pytest.mark.parametrize(
    "name",
    params(
        ("tf-br-0", "sf-del-mix-d50", "sf-rev-mix-d50", "sf-chr-mix-d50"),
    ),
)
def test_zero_is_dry(render: Any, name: str) -> None:
    """A control at 0 (or a send FX mix volume at 0) leaves the dry sound: bit
    identical for BR, within -80 dB (a residue of the send path) for the mixes."""
    r = render(name)
    dry = dry_of(render, r)
    assert r.x.shape == dry.x.shape
    diff = float(np.abs(r.x - dry.x).max())
    if name == "tf-br-0":
        assert diff == 0.0, diff
    else:
        assert diff < 1e-4, diff


MONO = (
    "tf-dry-hold", "tf-br-22", "tf-br-127", "tf-over-22", "tf-over-127",
    "tf-srr-22", "tf-srr-127", "tf-del-54", "tf-del-127", "sf-del-ref",
)  # fmt: skip


@pytest.mark.parametrize("name", params(MONO))
def test_mono_stays_mono(render: Any, name: str) -> None:
    """Mono source and mono effect (bit/rate reduction, overdrive, the delay
    at its defaults): left equals right sample for sample."""
    x = render(name).x
    assert np.array_equal(x[:, 0], x[:, 1])


# --- track FX: bit reduction, overdrive, sample-rate reduction ------------------------

BR = ("tf-br-0", "tf-br-22", "tf-br-54", "tf-br-127")
OVER = ("tf-dry-hold", "tf-over-22", "tf-over-54", "tf-over-127")
SRR = ("tf-srr-22", "tf-srr-54", "tf-srr-127")


@pytest.mark.scn(*BR)
@pytest.mark.parametrize("metric", ["thd_db", "diff_db"])
def test_bit_reduction_error_rises(render: Any, metric: str) -> None:
    """More bit reduction: more distortion, more error against the dry note."""
    assert_rises(render, BR, "snd[64]", lambda r: r.row["bits"][metric])


@pytest.mark.scn(*OVER)
@pytest.mark.parametrize("metric", ["thd_db", "diff_db"])
def test_overdrive_distortion_rises(render: Any, metric: str) -> None:
    """The overdrive adds harmonics (3rd and 5th rise) and gain as it rises."""
    names = OVER[1:]
    assert_rises(render, names, "snd[67]", lambda r: r.row["bits"][metric])
    assert_rises(render, names, "snd[67]", lambda r: r.row["tf"]["h3_db"])
    assert_rises(render, names, "snd[67]", lambda r: r.row["tf"]["fund_change_db"])


@pytest.mark.scn(*SRR)
def test_srr_rate_falls(render: Any) -> None:
    """The effective sample rate (lowest alias image of the 1 kHz tone) falls as
    the setting rises, and the error against the dry note rises."""
    assert_rises(
        render, SRR, "snd[65]", lambda r: r.row["tf"]["fs_candidates"][0][0],
        increasing=False,
    )  # fmt: skip
    assert_rises(render, SRR, "snd[65]", lambda r: r.row["bits"]["diff_db"])


@pytest.mark.scn("tf-dry-hold", "tf-dry-flt")
def test_filter_closes(render: Any) -> None:
    """Filter Frequency at about UI 60 takes at least 30 dB off the 1 kHz note."""
    open_, closed = render("tf-dry-hold", "tf-dry-flt")
    assert closed.row["bits"]["fund_db"] < open_.row["bits"]["fund_db"] - 30


@pytest.mark.scn(
    "tf-srr-127-flt", "tf-srr-127-flt-rt1", "tf-over-127-flt", "tf-over-127-flt-rt1"
)
def test_routing_changes_the_sound(render: Any) -> None:
    """With the filter closed, SRR or overdrive before or after it (the routing
    encoder at 1) gives a different signal."""
    for fx in ("srr", "over"):
        a, b = render(f"tf-{fx}-127-flt", f"tf-{fx}-127-flt-rt1")
        assert a.x.shape == b.x.shape
        assert float(np.abs(a.x - b.x).max()) > 1e-3, fx


# --- track FX: sends ------------------------------------------------------------------


@pytest.mark.scn("tf-dry-tap", "tf-del-54", "tf-del-127", "tf-rev-54", "tf-rev-127")
@pytest.mark.scn("tf-chr-54", "tf-chr-127")
@pytest.mark.parametrize("fx", ["del", "rev", "chr"])
def test_send_adds_wet_signal(render: Any, fx: str) -> None:
    """A higher send puts more wet signal in the output."""
    key = {"del": "snd[55]", "rev": "snd[56]", "chr": "snd[57]"}[fx]
    names = (f"tf-{fx}-54", f"tf-{fx}-127")
    assert_rises(render, names, key, lambda r: r.row["tf"]["wet_rms_db"])
    assert render(names[0]).row["tf"]["wet_rms_db"] > -80


# --- delay ----------------------------------------------------------------------------

TIME = ("sf-del-time-d50", "sf-del-ref", "sf-del-time-u20", "sf-del-time-u50")
FB = ("sf-del-fb-d50", "sf-del-ref", "sf-del-fb-u20", "sf-del-fb-u50")


@pytest.mark.scn(*TIME)
def test_delay_time_orders_the_echo(render: Any) -> None:
    """The first echo comes later as the delay time rises, and the repeat spacing
    stays within 8% of the first echo's time (a regular train)."""
    assert_rises(render, TIME, "fx[32]", lambda r: r.row["sf"]["wet_onset30_s"])
    for r in render(*TIME):
        if r.name in ("sf-del-time-d50", "sf-del-time-u50"):
            continue  # time 0 has overlapping repeats, 100 has one echo
        first, sp = r.row["sf"]["wet_onset30_s"], r.row["sf"]["spacing_s"]
        assert abs(sp - first) < 0.08 * first, (r.name, first, sp)


@pytest.mark.scn(*FB)
def test_delay_feedback_orders_the_repeats(render: Any) -> None:
    """More feedback: repeats fall by fewer dB each, and grow above unity."""
    got = ordered(render, FB, "fx[35]", lambda r: r.row["sf"]["db_per_repeat"])
    steps = [m for _, _, m in got]
    assert steps == sorted(steps), got
    assert steps[0] < -6 and steps[-1] > 0, got
    assert render("sf-del-fb-u50").row["analysis"]["basic"]["peak"] <= 1.0


@pytest.mark.scn("sf-del-ref", "sf-del-width-d50", "sf-del-width-u50")
def test_delay_width_places_the_echo(render: Any) -> None:
    """Stereo width at its minimum: the delay is hard left; at its maximum, right."""
    ref, lo, hi = render("sf-del-ref", "sf-del-width-d50", "sf-del-width-u50")
    assert ref.row["sf"]["wet_l_db"] == ref.row["sf"]["wet_r_db"]
    assert lo.row["sf"]["wet_l_db"] - lo.row["sf"]["wet_r_db"] >= 40, lo.row["sf"]
    assert hi.row["sf"]["wet_r_db"] - hi.row["sf"]["wet_l_db"] >= 30, hi.row["sf"]


@pytest.mark.scn("sf-del-ppwidth-d50", "sf-del-ppwidth-u50")
def test_delay_pingpong_alternates(render: Any) -> None:
    """Pingpong on with the width away from the centre: both channels carry
    echoes, the louder one following the width."""
    lo, hi = render("sf-del-ppwidth-d50", "sf-del-ppwidth-u50")
    assert lo.row["sf"]["wet_l_db"] > lo.row["sf"]["wet_r_db"] + 3, lo.row["sf"]
    assert hi.row["sf"]["wet_r_db"] > hi.row["sf"]["wet_l_db"] + 3, hi.row["sf"]


@pytest.mark.scn("sf-del-ref", "sf-del-lpf-d50", "sf-del-mix-d50", "sf-del-mix-u50")
def test_delay_lpf_and_mix(render: Any) -> None:
    """The feedback low-pass at 0 takes 20 dB off the echoes; the delay mix at 0
    removes them and at its maximum raises them."""
    ref, lpf, off, up = render(
        "sf-del-ref", "sf-del-lpf-d50", "sf-del-mix-d50", "sf-del-mix-u50"
    )
    assert lpf.row["sf"]["wet_rms_db"] < ref.row["sf"]["wet_rms_db"] - 20
    assert off.row["sf"]["wet_rms_db"] < -100
    assert up.row["sf"]["wet_rms_db"] > ref.row["sf"]["wet_rms_db"]


# --- reverb ---------------------------------------------------------------------------

REV = ("sf-rev-decay-d50", "sf-rev-ref", "sf-rev-decay-u20", "sf-rev-decay-u50")


@pytest.mark.scn(*REV)
def test_reverb_rt60_rises_with_decay(render: Any) -> None:
    assert_rises(render, REV, "fx[42]", lambda r: r.row["sf"]["rt60_s"])


@pytest.mark.scn("sf-rev-ref", "sf-rev-pre-u50", "sf-rev-lpf-d50", "sf-rev-mix-d50")
def test_reverb_pre_delay_lpf_mix(render: Any) -> None:
    """Pre-delay at its maximum delays the onset by 0.3 s or more; the input
    low-pass at 0 takes 20 dB off; the mix at 0 leaves no reverb."""
    ref, pre, lpf, off = render(
        "sf-rev-ref", "sf-rev-pre-u50", "sf-rev-lpf-d50", "sf-rev-mix-d50"
    )
    assert pre.row["sf"]["onset35_s"] > ref.row["sf"]["onset35_s"] + 0.3
    assert lpf.row["sf"]["wet_rms_db"] < ref.row["sf"]["wet_rms_db"] - 20
    assert off.row["sf"]["wet_rms_db"] < -100


# --- chorus ---------------------------------------------------------------------------

CHR_SPEED = ("sf-chr-speed-d50", "sf-chr-ref", "sf-chr-speed-u50")


@pytest.mark.scn(*CHR_SPEED)
def test_chorus_rate_rises_with_speed(render: Any) -> None:
    assert_rises(render, CHR_SPEED, "fx[26]", lambda r: r.row["sf"]["mod_phase_hz"])


@pytest.mark.scn("sf-chr-ref", "sf-chr-depth-d50", "sf-chr-mix-d50")
def test_chorus_depth_and_mix(render: Any) -> None:
    """Depth at 0: no modulation (the wet channels are the same signal); the
    chorus mix at 0 leaves no wet signal."""
    ref, depth, off = render("sf-chr-ref", "sf-chr-depth-d50", "sf-chr-mix-d50")
    assert ref.row["sf"]["wet_corr"] < 0.5
    assert depth.row["sf"]["wet_corr"] > 0.99, depth.row["sf"]
    assert off.row["sf"]["wet_rms_db"] < -100


# --- master compressor --------------------------------------------------------------------


@pytest.mark.scn("sf-mst-mix", "sf-mst-thr0", "sf-mst-ratio", "sf-mst-makeup")
def test_master_compressor_moves_the_level(render: Any) -> None:
    """With the compressor fully wet: threshold at 0 lowers the level, a higher
    ratio lowers it further, and makeup gain raises it."""
    mix, thr0, ratio, makeup = render(
        "sf-mst-mix", "sf-mst-thr0", "sf-mst-ratio", "sf-mst-makeup"
    )
    rms = lambda r: r.row["sf"]["rms_change_db"]  # noqa: E731
    assert rms(ratio) < rms(thr0) < rms(mix) < rms(makeup), [
        rms(x) for x in (ratio, thr0, mix, makeup)
    ]
