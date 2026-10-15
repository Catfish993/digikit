"""DSP budget of a patched firmware: emulator busy instructions per block -> projected real load.

The emulator counts SHARC+ instructions (idle loop skipped analytically). The real core also pays stalls, cache and
branch-predictor misses. The cycle model of `tools.sharc_dev.sharc_cpi` (out/clock/sharc.json, finding at the end of
docs/findings/06-sharc-engine-and-startup.md) gives, per device, real cycles per block / busy instructions per block
for the stock firmware. The factors below are those ratios for the held-note scenario (`dn2-trig1-hold`,
`dt2-sample-trig`): nominal and high bound of the assumed latencies. A module's added instructions are projected with
the same factor (its code mix is assumed to cost like the stock mix).

    load = busy_per_block * factor / BLOCK_CYCLES        BLOCK_CYCLES = 1 GHz / 1500 blocks per second
"""

from __future__ import annotations

from typing import Any

BLOCK_RATE = 1500  # DSPI2 frames (blocks) per audio second
BLOCK_CYCLES = (
    1_000_000_000 // BLOCK_RATE
)  # 666,666 whole cycles; the finding rounds up to 666,667
BLOCK_CYCLES_EXACT = 1_000_000_000 / BLOCK_RATE
DEFAULT_BUDGET_PCT = 85.0

# real cycles per block / emulator busy instructions per block (stock, held note), from out/clock/sharc.json
FACTORS: dict[str, dict[str, float]] = {
    "dn2": {"nominal": 1.138261, "high": 1.326075},
    "dt2": {"nominal": 1.134327, "high": 1.406521},
}


def busy_per_block(before: dict[str, Any], after: dict[str, Any]) -> float | None:
    """Mean busy instructions per block between two `status.audio.dsp_tiers` samples (None: no block ran)."""
    blocks = (after["audio_s"] - before["audio_s"]) * BLOCK_RATE
    if blocks <= 0:
        return None
    busy = (after["instructions"] - before["instructions"]) - (
        after["idle_skip"] - before["idle_skip"]
    )
    return busy / blocks


def load_pct(busy: float, device: str, bound: str) -> float:
    return 100.0 * busy * FACTORS[device][bound] / BLOCK_CYCLES_EXACT


def judge(
    device: str,
    busy_mean: float,
    busy_max: float,
    stock_mean: float,
    budget_pct: float = DEFAULT_BUDGET_PCT,
) -> dict[str, Any]:
    """The report of one cell: busy and added instructions per block, projected loads, the verdict."""
    added = busy_mean - stock_mean
    high = load_pct(busy_mean, device, "high")
    return {
        "device": device,
        "busy_mean": busy_mean,
        "busy_max_window": busy_max,
        "stock_busy_mean": stock_mean,
        "added_instructions_per_block": added,
        "added_load_nominal_pct": load_pct(added, device, "nominal"),
        "added_load_high_pct": load_pct(added, device, "high"),
        "load_nominal_pct": load_pct(busy_mean, device, "nominal"),
        "load_high_pct": high,
        "budget_pct": budget_pct,
        "ok": high <= budget_pct,
    }


def summary(r: dict[str, Any]) -> str:
    return (
        f"DSP busy {r['busy_mean']:.0f}/block (max window {r['busy_max_window']:.0f}), "
        f"added {r['added_instructions_per_block']:+.0f}; projected load {r['load_nominal_pct']:.1f} % nominal / "
        f"{r['load_high_pct']:.1f} % high (budget {r['budget_pct']:g} %)"
    )
